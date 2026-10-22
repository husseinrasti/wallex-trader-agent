"""ReplayExchange (PRD-05 §6): recorded order answers for the same client ids, divergences, balances.

Two kinds of tests:

* Scripted: ``orders.jsonl`` lines are written with the real ``OrdersLog`` in exactly the shapes
  the OrderManager writes (place/response/fill/cancel/status/error), then the exchange is asked
  what a replaying engine would ask.
* Round trip: a small engine loop (the real OrderManager, RiskManager, Reconciler and balance
  refresh in the engine's tick order) runs once against the ``PaperExchange`` (the recording) and
  once against ``ReplayExchange.from_log_dir`` (the replay). The replay must write the same
  ``orders.jsonl`` bytes, the same balance rows and decisions that ``compare_decisions`` finds
  identical.

Nothing here may open a socket: ``_no_network`` makes every connection attempt fail the test.
"""

from __future__ import annotations

import dataclasses
import socket
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta.config import Config
from wta.data.store import Store
from wta.exchange.base import OrderRejected, WallexAPIError
from wta.exchange.paper import PaperExchange
from wta.exchange.replay import (
    DIVERGENCE_CODE,
    ReplayDivergence,
    ReplayExchange,
    load_balance_snapshots,
    load_recorded_fills,
    reproducing_avg,
)
from wta.execution.order_manager import OrderManager
from wta.execution.positions import PortfolioState, PositionManager
from wta.execution.reconcile import Reconciler
from wta.logging_.decision_log import DecisionLog, EventsLog, OrdersLog
from wta.logging_.jsonl import family_files, iter_family
from wta.models import Balance, BookSnapshot, Fill, Intent, MarketInfo, Order, OrderRequest
from wta.replay.compare import compare_decisions
from wta.risk.context import Deadline, EngineFlags, RecentIntent, RiskContext
from wta.risk.manager import RiskManager
from wta.utils.decimalutil import D
from wta.utils.ids import make_decision_id
from wta.utils.timeutil import SimClock

from .conftest import T0, BookFactory

D0 = Decimal("0")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Every socket connection attempt is recorded and fails: the replay must never need one."""
    attempts: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield attempts
    assert attempts == []


# ---------------------------------------------------------------- scripted orders.jsonl


def mk_req(
    cid: str,
    qty: str = "0.5",
    price: str | None = "100.01",
    side: str = "BUY",
    decision_id: str = "d1",
    symbol: str = "BTCUSDT",
    otype: str = "LIMIT",
    purpose: str = "entry",
    stop_price: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_id=cid,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type=otype,  # type: ignore[arg-type]
        qty=D(qty),
        price=D(price) if price is not None else None,
        stop_price=D(stop_price) if stop_price is not None else None,
        strategy="S3",
        purpose=purpose,  # type: ignore[arg-type]
        decision_id=decision_id,
    )


class Script:
    """Writes OrderManager-shaped ``orders.jsonl`` lines with the real ``OrdersLog``."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.clock = SimClock(T0)
        self.log = OrdersLog(log_dir, self.clock)
        self.filled: dict[str, Decimal] = {}

    def at(self, s: float) -> Script:
        self.clock.set(T0 + s)
        return self

    def place(
        self, req: OrderRequest, why: str = "submit", status: str | None = "NEW", xid: str = "x"
    ) -> Script:
        self.log.log(
            "place",
            req.client_id,
            req.symbol,
            {"source": "order_manager", "why": why, "request": req.model_dump(mode="json")},
        )
        if status is not None:
            self.log.log(
                "response",
                req.client_id,
                req.symbol,
                {"source": "order_manager", "status": status, "exchange_id": xid},
            )
        return self

    def error(self, req: OrderRequest, code: str, status: int | None, ambiguous: bool = False) -> Script:
        data: dict[str, Any] = {"code": code, "status": status, "message": f"{code} message"}
        if ambiguous:
            data["ambiguous"] = True
        self.log.log("error", req.client_id, req.symbol, data)
        return self

    def fill(self, req: OrderRequest, qty: str, price: str, fee: str = "0", fee_asset: str = "BTC") -> Script:
        total = self.filled.get(req.client_id, D0) + D(qty)
        self.filled[req.client_id] = total
        f = Fill(
            order_client_id=req.client_id,
            symbol=req.symbol,
            side=req.side,
            price=D(price),
            qty=D(qty),
            fee=D(fee),
            fee_asset=fee_asset,
            ts=self.clock.now(),
            trade_id=f"{req.client_id}@{total}",
        )
        self.log.log(
            "fill",
            req.client_id,
            req.symbol,
            {**f.model_dump(mode="json"), "decision_id": req.decision_id, "purpose": req.purpose},
        )
        return self

    def cancel(self, req: OrderRequest, why: str = "reprice") -> Script:
        self.log.log("cancel", req.client_id, req.symbol, {"source": "order_manager", "why": why})
        return self

    def status(self, req: OrderRequest, **data: Any) -> Script:
        self.log.log("status", req.client_id, req.symbol, data)
        return self

    def done(self, req: OrderRequest, reason: str, filled: str, reprices: int = 0) -> Script:
        return self.status(req, done=True, done_reason=reason, filled_qty=filled, reprices=reprices)

    def records(self) -> list[dict[str, Any]]:
        self.log.close()
        return list(iter_family(self.log_dir, "orders"))


def recorded_delta(old_avg: Decimal, old_filled: Decimal, new_avg: Decimal, new_filled: Decimal) -> Decimal:
    """The fill price the OrderManager logs between two exchange answers (``_fill_delta``)."""
    return (new_avg * new_filled - old_avg * old_filled) / (new_filled - old_filled)


def om_fill_price(prev: Order | None, new: Order) -> Decimal:
    """The OrderManager's ``_fill_delta`` price between two answers of one placement."""
    old_filled = prev.filled_qty if prev is not None else D0
    old_avg = prev.avg_price if prev is not None else None
    qty = new.filled_qty - old_filled
    assert new.avg_price is not None
    return (new.avg_price * new.filled_qty - (old_avg or D0) * old_filled) / qty


async def test_partial_fill_reprice_and_replacement_answer_in_tick_order(tmp_path: Path) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    a2 = mk_req("S3_BTCUSDT_1792663216000_BBBB", qty="0.3", price="100.02")
    sc = Script(tmp_path)
    sc.at(0).place(a1, xid="x1")
    sc.at(4).fill(a1, "0.2", "100.01", fee="0.0002")
    sc.at(16).cancel(a1, "reprice").place(a2, why="reprice", xid="x2")
    sc.at(20).fill(a2, "0.1", "100.02", fee="0.0001")
    # what the OrderManager logs for the second fill: the delta of the exchange's two averages
    second = recorded_delta(
        D("100.02"), D("0.1"), (D("0.1") * D("100.02") + D("0.2") * D("100.03")) / D("0.3"), D("0.3")
    )
    sc.at(24).fill(a2, "0.2", str(second), fee="0.0002").done(a2, "FILLED", "0.5", reprices=1)
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    assert ex.name == "replay" and ex.placement_order == [a1.client_id, a2.client_id]

    placed = await ex.place_order(a1)
    assert (placed.status, placed.filled_qty, placed.exchange_id, placed.req) == ("NEW", D0, "x1", a1)
    clock.set(T0 + 2)
    assert (await ex.get_order(a1.client_id)).status == "NEW"  # type: ignore[union-attr]
    clock.set(T0 + 4)
    p1 = await ex.get_order(a1.client_id)
    assert p1 is not None and p1.status == "PARTIALLY_FILLED"
    assert (p1.filled_qty, p1.avg_price, p1.fee_paid, p1.fee_asset) == (
        D("0.2"),
        D("100.01"),
        D("0.0002"),
        "BTC",
    )
    assert str(om_fill_price(None, p1)) == "100.01"
    clock.set(T0 + 6)  # polled more often than recorded: the last state again
    again = await ex.get_order(a1.client_id)
    assert again is not None and again.filled_qty == D("0.2") and again.status == "PARTIALLY_FILLED"
    assert [o.client_id for o in await ex.get_open_orders()] == [a1.client_id]

    clock.set(T0 + 16)
    canceled = await ex.cancel_order(a1.client_id)
    assert canceled is not None and canceled.status == "CANCELED" and canceled.filled_qty == D("0.2")
    p2 = await ex.place_order(a2)
    assert p2.status == "NEW" and p2.exchange_id == "x2"
    clock.set(T0 + 20)
    f1 = await ex.get_order(a2.client_id)
    assert f1 is not None and f1.status == "PARTIALLY_FILLED" and str(om_fill_price(None, f1)) == "100.02"
    clock.set(T0 + 22)
    assert await ex.get_order(a2.client_id) == f1
    clock.set(T0 + 24)
    f2 = await ex.get_order(a2.client_id)
    assert f2 is not None and f2.status == "FILLED" and f2.filled_qty == D("0.3")
    assert str(om_fill_price(f1, f2)) == str(second)  # the delta gives back the recorded string
    assert await ex.get_open_orders() == []
    trades = await ex.get_my_trades()
    assert [(t.order_client_id, t.qty, t.price) for t in trades] == [
        (a1.client_id, D("0.2"), D("100.01")),
        (a2.client_id, D("0.1"), D("100.02")),
        (a2.client_id, D("0.2"), second),
    ]
    answers = [(c.method, c.answer) for c in ex.calls if c.method == "get_order"]
    assert answers == [
        ("get_order", "repeat"),
        ("get_order", "recorded"),
        ("get_order", "repeat"),
        ("get_order", "recorded"),
        ("get_order", "repeat"),
        ("get_order", "recorded"),
    ]
    assert ex.divergences == [] and ex.unplaced() == [] and ex.pending() == {}


async def test_without_a_clock_each_call_advances_one_observation(tmp_path: Path) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.at(4).fill(a1, "0.2", "100.01")
    sc.at(8).fill(a1, "0.3", "100.01").done(a1, "FILLED", "0.5")
    ex = ReplayExchange(sc.records())
    assert (await ex.place_order(a1)).status == "NEW"
    seen = [await ex.get_order(a1.client_id) for _ in range(4)]
    assert [(o.status, o.filled_qty) for o in seen if o is not None] == [
        ("PARTIALLY_FILLED", D("0.2")),
        ("FILLED", D("0.5")),
        ("FILLED", D("0.5")),
        ("FILLED", D("0.5")),
    ]


async def test_unconfirmed_cancel_is_live_and_the_retry_ends_it(tmp_path: Path) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.at(46).cancel(a1, "EXPIRED_UNFILLED")
    sc.at(48).cancel(a1, "EXPIRED_UNFILLED").done(a1, "EXPIRED_UNFILLED", "0")
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    await ex.place_order(a1)
    clock.set(T0 + 46)
    first = await ex.cancel_order(a1.client_id)
    assert first is not None and first.status == "NEW"  # the exchange did not confirm it
    clock.set(T0 + 48)
    second = await ex.cancel_order(a1.client_id)
    assert second is not None and second.status == "CANCELED"


async def test_cancel_confirmed_before_a_later_replacement(tmp_path: Path) -> None:
    """The remainder waited for a book: the cancel ended the placement, the re-place came later."""
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    a2 = mk_req("S3_BTCUSDT_1792663220000_BBBB", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.at(16).cancel(a1, "reprice")
    sc.at(20).place(a2, why="reprice")
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    await ex.place_order(a1)
    clock.set(T0 + 16)
    got = await ex.cancel_order(a1.client_id)
    assert got is not None and got.status == "CANCELED"


async def test_exchange_ended_orders_and_cancel_answers_with_late_fills(tmp_path: Path) -> None:
    stop = mk_req(
        "S3_BTCUSDT_1792663200000_STOP",
        qty="0.4",
        side="SELL",
        otype="STOP_LIMIT",
        purpose="stop",
        stop_price="95",
        price="94.62",
    )
    ent = mk_req("S3_BTCUSDT_1792663200000_ENTR", qty="0.5", decision_id="d2")
    exit_ = mk_req("S3_BTCUSDT_1792663200000_EXIT", qty="0.5", side="SELL", decision_id="d3", purpose="exit")
    nxt = mk_req("S3_BTCUSDT_1792663230000_EXI2", qty="0.5", side="SELL", decision_id="d3", purpose="exit")
    sc = Script(tmp_path)
    sc.at(0).place(stop).place(ent).place(exit_)
    sc.at(10).done(stop, "EXCHANGE_EXPIRED", "0")
    sc.at(20).cancel(ent, "EXPIRED_UNFILLED").fill(ent, "0.1", "100.01").done(ent, "EXPIRED_UNFILLED", "0.1")
    sc.at(30).place(nxt, why="cross")  # the exchange ended the exit by itself; its remainder crossed
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    for r in (stop, ent, exit_):
        await ex.place_order(r)
    clock.set(T0 + 10)
    assert (await ex.get_order(stop.client_id)).status == "EXPIRED"  # type: ignore[union-attr]
    clock.set(T0 + 20)
    late = await ex.cancel_order(ent.client_id)
    assert late is not None and (late.status, late.filled_qty) == ("CANCELED", D("0.1"))
    clock.set(T0 + 28)
    assert (await ex.get_order(exit_.client_id)).status == "NEW"  # type: ignore[union-attr]
    clock.set(T0 + 30)
    assert (await ex.get_order(exit_.client_id)).status == "CANCELED"  # type: ignore[union-attr]


async def test_recorded_placement_errors_are_raised_again(tmp_path: Path) -> None:
    rej = mk_req("S3_BTCUSDT_1792663200000_REJ1")
    amb = mk_req("S3_BTCUSDT_1792663200000_AMB1", decision_id="d2")
    sc = Script(tmp_path)
    sc.at(0).place(rej, status=None).error(rej, "min_notional", 400)
    sc.place(amb, status=None).error(amb, "timeout", None, ambiguous=True)
    sc.at(6).status(amb, found=False, misses=3, resolution="not placed")
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    with pytest.raises(OrderRejected) as rejected:
        await ex.place_order(rej)
    assert (rejected.value.code, rejected.value.status) == ("min_notional", 400)
    assert not isinstance(rejected.value, ReplayDivergence)
    with pytest.raises(WallexAPIError) as unknown:
        await ex.place_order(amb)
    assert not isinstance(unknown.value, OrderRejected) and unknown.value.code == "timeout"
    for t in (2, 4, 6):
        clock.set(T0 + t)
        assert await ex.get_order(amb.client_id) is None  # it never reached the exchange
    assert [c.answer for c in ex.calls if c.method == "place_order"] == ["recorded_error", "recorded_error"]


async def test_a_lagging_answer_lags_once(tmp_path: Path) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.at(4).fill(a1, "0.3", "100.01", fee="0.0003")
    sc.at(6).status(
        a1,
        baseline_kept=True,
        reported_filled="0.1",
        known_filled="0.3",
        reported_fee="0.0001",
        known_fee="0.0003",
    )
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    await ex.place_order(a1)
    clock.set(T0 + 4)
    assert (await ex.get_order(a1.client_id)).filled_qty == D("0.3")  # type: ignore[union-attr]
    clock.set(T0 + 6)
    lagged = await ex.get_order(a1.client_id)
    assert lagged is not None and (lagged.filled_qty, lagged.fee_paid) == (D("0.1"), D("0.0001"))
    clock.set(T0 + 8)
    after = await ex.get_order(a1.client_id)
    assert after is not None and (after.filled_qty, after.fee_paid) == (D("0.3"), D("0.0003"))


async def test_divergences_are_reported_not_crashes(tmp_path: Path) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    b1 = mk_req("S3_BTCUSDT_1792663200000_BBBB", qty="0.2", decision_id="d2")
    sc = Script(tmp_path)
    sc.at(0).place(a1).place(b1)
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    stranger = mk_req("S3_BTCUSDT_1792663200000_ZZZZ", decision_id="d1", qty="0.6")
    with pytest.raises(ReplayDivergence) as div:
        await ex.place_order(stranger)
    err = div.value
    assert isinstance(err, OrderRejected) and err.code == DIVERGENCE_CODE and err.status == 409
    assert err.kind == "unknown_client_id" and not err.retryable
    assert err.details["recorded_for_decision"] == [
        {"client_id": a1.client_id, "side": "BUY", "type": "LIMIT", "qty": "0.5", "price": "100.01"}
    ]
    with pytest.raises(ReplayDivergence) as not_placed:
        await ex.get_order(a1.client_id)
    assert not_placed.value.kind == "not_placed"
    with pytest.raises(ReplayDivergence):
        await ex.cancel_order("S3_BTCUSDT_1792663200000_NONE")
    answer = await ex.place_order(a1.model_copy(update={"qty": D("0.4")}))
    assert answer.status == "NEW" and answer.req.qty == D("0.4")  # served from the record anyway
    with pytest.raises(ReplayDivergence) as dup:
        await ex.place_order(a1)
    assert dup.value.kind == "duplicate_placement"
    kinds = [d["kind"] for d in ex.divergences]
    assert kinds == [
        "unknown_client_id",
        "not_placed",
        "unknown_client_id",
        "request_mismatch",
        "duplicate_placement",
    ]
    mismatch = ex.divergences[3]
    assert mismatch["fields"] == {"qty": {"recorded": "0.5", "replayed": "0.4"}}
    assert [d["kind"] for d in ex.all_divergences()][-1] == "not_replayed"
    assert ex.all_divergences()[-1]["client_id"] == b1.client_id
    assert ex.summary()["divergences"] == 5 and ex.summary()["unplaced"] == 1


async def test_client_traffic_lines_are_not_order_manager_steps(tmp_path: Path) -> None:
    """Lines an exchange client might add (no ``source: order_manager``) never become answers."""
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.log.log("place", a1.client_id, "BTCUSDT", {"request": {"clientOrderId": a1.client_id}, "attempt": 0})
    sc.at(16).cancel(a1, "EXPIRED_UNFILLED")
    sc.log.log("cancel", a1.client_id, "BTCUSDT", {})
    sc.log.log("cancel_response", a1.client_id, "BTCUSDT", {"found": True, "status": "CANCELED"})
    sc.log.log("error", a1.client_id, "BTCUSDT", {"status": 400, "code": "x", "message": "m", "attempt": 0})
    sc.status(a1, found=False)  # a client lookup, not the OrderManager's "not found after polls"
    sc.done(a1, "EXPIRED_UNFILLED", "0")
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    assert (await ex.place_order(a1)).status == "NEW"
    clock.set(T0 + 16)
    got = await ex.cancel_order(a1.client_id)
    assert got is not None and got.status == "CANCELED"
    assert len(ex.scripts[a1.client_id].obs) == 2


async def test_unrecorded_cancel_cancels_what_is_there(tmp_path: Path) -> None:
    stop = mk_req(
        "S3_BTCUSDT_1792663200000_STOP",
        qty="0.4",
        side="SELL",
        otype="STOP_LIMIT",
        purpose="stop",
        stop_price="95",
        price="94.62",
    )
    sc = Script(tmp_path)
    sc.at(0).place(stop)
    clock = SimClock(T0)
    ex = ReplayExchange(sc.records(), clock=clock)
    await ex.place_order(stop)
    clock.set(T0 + 30)
    got = await ex.cancel_order(stop.client_id)
    assert got is not None and got.status == "CANCELED"
    assert (await ex.get_order(stop.client_id)).status == "CANCELED"  # type: ignore[union-attr]
    assert [n["kind"] for n in ex.notes] == ["unrecorded_cancel"] and ex.divergences == []


def test_reproducing_avg_gives_back_the_recorded_fill_price() -> None:
    """Exchange averages (quote sum / filled, as the paper exchange keeps them) -> logged deltas ->
    rebuilt averages whose deltas are the logged strings again."""
    avg = reproducing_avg(None, D0, D("0.1"), D("100.01"))
    assert str(avg) == "100.01"
    fills = [("0.1", "100.01"), ("0.25", "100.04"), ("0.013", "99.97"), ("0.5", "100.33")]
    quote, filled, ex_avg = D0, D0, None
    rebuilt, r_filled = None, D0
    for qty, price in fills:
        quote += D(qty) * D(price)
        new_filled = filled + D(qty)
        new_avg = quote / new_filled
        logged = recorded_delta(ex_avg or D0, filled, new_avg, new_filled)
        nxt = reproducing_avg(rebuilt, r_filled, new_filled, logged)
        assert str(recorded_delta(rebuilt or D0, r_filled, nxt, new_filled)) == str(logged)
        filled, ex_avg, rebuilt, r_filled = new_filled, new_avg, nxt, new_filled


# ---------------------------------------------------------------- balances


def bal(**amounts: str) -> dict[str, Balance]:
    return {
        a: Balance(asset=a, free=D(v.split("/")[0]), locked=D(v.split("/")[1]) if "/" in v else D0)
        for a, v in sorted(amounts.items())
    }


async def test_balances_rows_per_tick_reconciler_and_refresh(tmp_path: Path) -> None:
    sc = Script(tmp_path)
    clock = SimClock(T0)
    engine_balances = bal(TMN="100", USDT="10")
    refresh_row = bal(TMN="90/10", USDT="10")
    reconcile_row = bal(TMN="90/10", USDT="10", BTC="0")
    ex = ReplayExchange(
        sc.records(),
        clock=clock,
        balances=[(T0 + 2, refresh_row), (T0 + 2, reconcile_row), (T0 + 4, engine_balances)],
        balances_fallback=lambda: engine_balances,
    )
    clock.set(T0 + 2)
    assert await ex.get_balances() == refresh_row  # a refresh that wrote a row
    await ex.get_open_orders()
    await ex.get_my_trades()
    assert await ex.get_balances() == reconcile_row  # the Reconciler's read: the tick's last row
    clock.set(T0 + 3)
    assert await ex.get_balances() == engine_balances  # no row: the answer equalled the engine's
    clock.set(T0 + 4)
    # the row equals the engine's balances, so a refresh did not write it: the Reconciler did
    assert await ex.get_balances() == engine_balances
    await ex.get_open_orders()
    await ex.get_my_trades()
    assert await ex.get_balances() == engine_balances
    answers = [c.answer for c in ex.calls if c.method == "get_balances"]
    assert answers == ["recorded", "recorded", "fallback", "fallback", "recorded"]


async def test_deferred_startup_reconcile_takes_the_first_row(tmp_path: Path) -> None:
    sc = Script(tmp_path)
    clock = SimClock(T0)
    rec_row, refresh_row = bal(TMN="100"), bal(TMN="90/10")
    ex = ReplayExchange(
        sc.records(), clock=clock, balances=[(T0, rec_row), (T0, refresh_row)], balances_fallback=lambda: {}
    )
    await ex.get_open_orders()
    await ex.get_my_trades()
    assert await ex.get_balances() == rec_row
    assert await ex.get_balances() == refresh_row


async def test_derived_balances_from_initial_balances_and_fills(
    tmp_path: Path, markets: dict[str, MarketInfo]
) -> None:
    a1 = mk_req("S3_BTCUSDT_1792663200000_AAAA", qty="0.5")
    sc = Script(tmp_path)
    sc.at(0).place(a1)
    sc.at(4).fill(a1, "0.2", "100", fee="0.0002", fee_asset="BTC")
    clock = SimClock(T0)
    ex = ReplayExchange(
        sc.records(), clock=clock, markets=markets, initial_balances={"USDT": "1000", "TMN": 5}
    )
    await ex.place_order(a1)
    assert await ex.get_balances() == bal(TMN="5", USDT="1000")
    clock.set(T0 + 4)
    got = await ex.get_balances()
    assert got == bal(BTC="0.1998", TMN="5", USDT="980")
    assert list(got) == ["BTC", "TMN", "USDT"]
    assert [c.answer for c in ex.calls if c.method == "get_balances"] == ["derived", "derived"]
    assert await ex.get_markets() == markets


async def test_without_a_clock_balance_snapshots_come_in_order(tmp_path: Path) -> None:
    sc = Script(tmp_path)
    one, two = bal(TMN="1"), bal(TMN="2")
    ex = ReplayExchange(sc.records(), balances=[(T0, one), (T0 + 1, two)])
    assert [await ex.get_balances() for _ in range(3)] == [one, two, two]


async def test_state_db_loaders_and_reconciler_booked_fills(tmp_path: Path) -> None:
    """Balances snapshots split by call; a my-trades fill the Reconciler booked is served by
    ``get_my_trades`` and only then by ``get_order``."""
    log_dir = tmp_path / "logs"
    stop = mk_req(
        "S3_BTCUSDT_1792663200000_STOP",
        qty="0.4",
        side="SELL",
        otype="STOP_LIMIT",
        purpose="stop",
        stop_price="95",
        price="94.62",
    )
    sc = Script(log_dir)
    sc.at(0).place(stop)
    sc.at(12).status(stop, source="reconcile", status="FILLED", why="fully filled (fills found in my-trades)")
    sc.done(stop, "FILLED", "0.4")
    db = tmp_path / "state.sqlite"
    store = Store(db)
    store.add_balances(T0, bal(TMN="1", USDT="2"))
    store.add_balances(T0, bal(BTC="3", TMN="1", USDT="2"))  # same tick, more assets: a second call
    store.add_balances(T0 + 12, bal(TMN="4"))
    booked = Fill(
        order_client_id=stop.client_id,
        symbol="BTCUSDT",
        side="SELL",
        price=D("94.70"),
        qty=D("0.4"),
        fee=D("0.03788"),
        fee_asset="USDT",
        ts=T0 + 11,
        trade_id="paper-7",
    )
    store.add_fill(booked)
    store.flush()
    store.close()
    snaps = load_balance_snapshots(db)
    assert [(ts - T0, sorted(s)) for ts, s in snaps] == [
        (0.0, ["TMN", "USDT"]),
        (0.0, ["BTC", "TMN", "USDT"]),
        (12.0, ["TMN"]),
    ]
    assert load_recorded_fills(db) == [booked]
    sc.records()

    clock = SimClock(T0)
    ex = ReplayExchange.from_log_dir(log_dir, state_db=db, clock=clock)
    await ex.place_order(stop)
    clock.set(T0 + 12)
    polled = await ex.get_order(stop.client_id)
    assert polled is not None and polled.status == "NEW"  # the poll did not see it in the recording
    assert [o.client_id for o in await ex.get_open_orders()] == []  # the exchange truth at that tick
    assert await ex.get_my_trades() == [booked]
    after = await ex.get_order(stop.client_id)
    assert after is not None and after.status == "FILLED" and after.filled_qty == D("0.4")


# ---------------------------------------------------------------- round trip: paper -> replay


class MiniEngine:
    """The engine's order path in its tick order (PRD-01): match, poll, refresh, intents, reconcile."""

    def __init__(
        self,
        exchange: Any,
        clock: SimClock,
        root: Path,
        mode: str,
        markets: dict[str, MarketInfo],
        cfg: Config,
    ) -> None:
        self.ex = exchange
        self.paper = exchange if isinstance(exchange, PaperExchange) else None
        self.clock = clock
        self.markets = markets
        self.cfg = cfg
        self.books: dict[str, BookSnapshot] = {}
        self.log_dir = root / "logs"
        self.store = Store(root / "wta.sqlite")
        self.portfolio = PortfolioState(initial_equity_tmn=D("100000000"))
        self.positions = PositionManager(self.portfolio, markets)
        self.dlog = DecisionLog(self.log_dir, mode, "a" * 40, "cfg000000001", clock)
        self.olog = OrdersLog(self.log_dir, clock)
        self.elog = EventsLog(self.log_dir, clock)
        self.om = OrderManager(
            exchange, self.portfolio, self.positions, markets, cfg, clock, self.dlog, self.olog, self.store
        )
        self.reconciler = Reconciler(
            exchange, self.portfolio, self.positions, self.om, self.store, self.elog, clock, cfg
        )
        self.risk = RiskManager(cfg)
        self.flags = EngineFlags()
        self.deadline = Deadline(None, None, None, None)
        self.recent: list[RecentIntent] = []
        self.stale = False
        self.seq = 0

    async def startup(self, now: float) -> None:
        self.clock.set(now)
        self.portfolio.set_balances(await self.ex.get_balances())  # fresh store: the exchange's
        report = await self.reconciler.run(self.books, now, reason="startup")
        report.log_outcomes(self.dlog)
        self.store.flush()

    async def refresh_balances(self, now: float) -> None:
        if not (self.om.balances_dirty or self.stale):
            return
        balances = await self.ex.get_balances()
        self.om.balances_dirty = False
        self.stale = False
        if balances != self.portfolio.balances:
            self.store.add_balances(now, balances)
        self.portfolio.set_balances(balances)
        self.portfolio.refresh(self.books, now)

    async def tick(
        self,
        now: float,
        books: dict[str, BookSnapshot],
        intents: list[Callable[[MiniEngine, float], Intent]],
        reconcile: bool,
    ) -> None:
        self.clock.set(now)
        self.books = books
        if self.paper is not None:
            self.paper.on_tick(now, books)
        self.portfolio.refresh(books, now)
        result = await self.om.poll(books, now)
        if result.fills or result.done:
            self.stale = True
        await self.refresh_balances(now)
        for make in intents:
            intent = make(self, now)
            ctx = RiskContext(
                now=now,
                portfolio=self.portfolio,
                books=books,
                markets=self.markets,
                cfg=self.cfg,
                flags=self.flags,
                deadline=self.deadline,
                recent_intents=self.recent,
            )
            decision = self.risk.evaluate(intent, self._risk_view(ctx, intent))
            placed = await self.om.submit(intent, decision, books)
            if decision.approved:
                self.recent.append(
                    RecentIntent(now, intent.strategy, intent.symbol, intent.side, intent.purpose)
                )
            if placed is not None:
                self.stale = True
            await self.refresh_balances(now)
        if reconcile:
            report = await self.reconciler.run(books, now, reason="scheduled")
            report.log_outcomes(self.dlog)
        self.store.flush()

    @staticmethod
    def _risk_view(ctx: RiskContext, intent: Intent) -> RiskContext:
        """``Engine._risk_view``: a closing SELL may count its position's server-stop coins as free."""
        port = ctx.portfolio
        pos = port.positions.get(intent.linked_position_id or "")
        stop = port.open_orders.get(pos.stop_order_client_id or "") if pos is not None else None
        if intent.side != "SELL" or pos is None or stop is None or stop.is_terminal:
            return ctx
        b = port.balances.get(pos.base)
        release = min(stop.remaining_qty, b.locked) if b is not None else D0
        if b is None or release <= 0:
            return ctx
        freed = Balance(asset=pos.base, free=b.free + release, locked=b.locked - release)
        view = dataclasses.replace(port, balances={**port.balances, pos.base: freed})
        return dataclasses.replace(ctx, portfolio=view)

    def close(self) -> None:
        for w in (self.dlog, self.olog, self.elog):
            w.close()
        self.store.close()

    def next_id(self, now: float, strategy: str, symbol: str) -> str:
        self.seq += 1
        return make_decision_id(now, strategy, symbol, self.seq)


def _entry(eng: MiniEngine, now: float) -> Intent:
    return Intent(
        id=eng.next_id(now, "S3", "BTCUSDT"),
        ts=now,
        strategy="S3",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=D("0.5"),
        stop_price=D("95"),
        purpose="entry",
        reason="S3 breakout: close above the 20-bar high",
        expected_edge_pct=D("0.05"),
        features={"time_stop_s": 3600},
    )


def _second_entry(eng: MiniEngine, now: float) -> Intent:  # vetoed: S3 already has its position
    return Intent(
        id=eng.next_id(now, "S3", "ETHUSDT"),
        ts=now,
        strategy="S3",
        symbol="ETHUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=D("0.01"),
        purpose="entry",
        reason="S3 breakout on ETH",
        expected_edge_pct=D("0.05"),
    )


def _rebalance(eng: MiniEngine, now: float) -> Intent:
    return Intent(
        id=eng.next_id(now, "S2", "USDTTMN"),
        ts=now,
        strategy="S2",
        symbol="USDTTMN",
        side="BUY",
        order_type="LIMIT",
        qty=D("2"),
        purpose="rebalance",
        taker=True,
        reason="S2: raise the USDT share toward the target",
        features={"usdt_share": "0.49"},
    )


def _exit(eng: MiniEngine, now: float) -> Intent:
    pos = next(iter(eng.portfolio.positions.values()))
    return Intent(
        id=eng.next_id(now, "S3", "BTCUSDT"),
        ts=now,
        strategy="S3",
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        qty=pos.qty,
        purpose="exit",
        taker=True,
        linked_position_id=pos.id,
        reason="S3 target reached",
        features={"exit_reason": "target"},
    )


def scenario_books(make_book: BookFactory, k: int, now: float) -> dict[str, BookSnapshot]:
    """Tick ``k``'s books. The paper exchange matches an order from the tick after its placement."""
    btc = ("100.00", "100.10", "10")
    if k == 2:
        btc = ("99.99", "100.01", "0.2")  # touches the maker BUY at 100.01 for 0.2: a partial fill
    elif k == 10:
        btc = ("99.99", "100.01", "5")  # the re-placed remainder fills
    elif k == 35:
        btc = ("94.98", "95.02", "10")  # the second position's server stop (95) triggers ...
    elif k >= 36:
        btc = ("94.90", "95.00", "10")  # ... and fills; the Reconciler books it before the next poll
    usdt = ("100000", "100010", "1000")
    if k == 16:
        usdt = ("100000", "100010", "0.5")  # the taker BUY takes 0.5, the rest rests at 100010
    elif k > 16:
        usdt = ("100005", "100020", "1000")  # moved away: the remainder is crossed again later
    return {
        "BTCUSDT": make_book("BTCUSDT", btc[0], btc[1], qty=btc[2], ts=now),
        "ETHUSDT": make_book("ETHUSDT", "2000.00", "2000.50", qty="10", ts=now),
        "USDTTMN": make_book("USDTTMN", usdt[0], usdt[1], qty=usdt[2], ts=now),
    }


def _taker_entry(eng: MiniEngine, now: float) -> Intent:
    return _entry(eng, now).model_copy(update={"taker": True, "reason": "S3 breakout again (taker)"})


INTENTS: dict[int, list[Callable[[MiniEngine, float], Intent]]] = {
    0: [_entry],
    12: [_second_entry],
    15: [_rebalance],
    30: [_exit],
    32: [_taker_entry],
}
RECONCILE_AT = {20, 37, 40}
TICKS = 45


async def run_scenario(eng: MiniEngine, make_book: BookFactory) -> None:
    eng.books = scenario_books(make_book, 0, T0)
    await eng.startup(T0)
    for k in range(TICKS):
        now = T0 + 2 * k
        await eng.tick(now, scenario_books(make_book, k, now), INTENTS.get(k, []), k in RECONCILE_AT)
    eng.close()


def _lines(log_dir: Path, family: str) -> list[bytes]:
    return [
        line for p in family_files(log_dir, family) for line in p.read_bytes().splitlines() if line.strip()
    ]


async def test_round_trip_paper_recording_is_reproduced_exactly(
    tmp_path: Path, markets: dict[str, MarketInfo], cfg: Config, make_book: BookFactory
) -> None:
    rec_clock = SimClock(T0)
    paper = PaperExchange(
        markets, lambda: {}, rec_clock, initial_balances={"TMN": D("50000000"), "USDT": D("500")}
    )
    recording = MiniEngine(paper, rec_clock, tmp_path / "rec", "shadow", markets, cfg)
    paper._books = lambda: recording.books  # noqa: SLF001 - the paper reads the engine's books
    await run_scenario(recording, make_book)

    rep_clock = SimClock(T0)
    replay_ex = ReplayExchange.from_log_dir(
        tmp_path / "rec" / "logs", state_db=tmp_path / "rec" / "wta.sqlite", clock=rep_clock, markets=markets
    )
    replaying = MiniEngine(replay_ex, rep_clock, tmp_path / "rep", "replay", markets, cfg)
    replay_ex.set_balances_fallback(lambda: replaying.portfolio.balances)
    await run_scenario(replaying, make_book)

    rec_orders = _lines(tmp_path / "rec" / "logs", "orders")
    actions = [r["action"] for r in iter_family(tmp_path / "rec" / "logs", "orders")]
    # the scenario covers what the replay must reproduce
    assert actions.count("place") >= 5 and actions.count("cancel") >= 2 and actions.count("fill") >= 5
    assert any(
        r["data"].get("why") in ("reprice", "cross")
        for r in iter_family(tmp_path / "rec" / "logs", "orders")
        if r["action"] == "place"
    )
    assert _lines(tmp_path / "rep" / "logs", "orders") == rec_orders  # byte for byte

    decisions = list(iter_family(tmp_path / "rec" / "logs", "decisions"))
    kinds = sorted({d["kind"] for d in decisions})
    assert kinds == ["order", "outcome", "veto"]
    report = compare_decisions(
        tmp_path / "rec" / "logs", tmp_path / "rep" / "logs", divergences=replay_ex.all_divergences()
    )
    assert report.ok, report.to_markdown()
    assert report.summary == f"{report.n_reference}/{report.n_reference} decisions reproduced"
    strict = compare_decisions(tmp_path / "rec" / "logs", tmp_path / "rep" / "logs", strict=True)
    assert strict.ok, strict.to_markdown()
    assert strict.provenance["mode"] == {"reference": ["shadow"], "replayed": ["replay"], "equal": False}

    # the replay's balance rows and fills are the recorded ones
    assert load_balance_snapshots(tmp_path / "rep" / "wta.sqlite") == load_balance_snapshots(
        tmp_path / "rec" / "wta.sqlite"
    )
    assert load_recorded_fills(tmp_path / "rep" / "wta.sqlite") == load_recorded_fills(
        tmp_path / "rec" / "wta.sqlite"
    )
    assert replay_ex.divergences == [] and replay_ex.unplaced() == []
    assert {c.answer for c in replay_ex.calls} <= {"recorded", "repeat", "fallback"}
    methods = {c.method for c in replay_ex.calls}
    assert {
        "place_order",
        "get_order",
        "cancel_order",
        "get_balances",
        "get_open_orders",
        "get_my_trades",
    } <= methods


async def test_round_trip_detects_a_changed_decision(
    tmp_path: Path,
    markets: dict[str, MarketInfo],
    cfg: Config,
    make_book: BookFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec_clock = SimClock(T0)
    paper = PaperExchange(
        markets, lambda: {}, rec_clock, initial_balances={"TMN": D("50000000"), "USDT": D("500")}
    )
    recording = MiniEngine(paper, rec_clock, tmp_path / "rec", "shadow", markets, cfg)
    paper._books = lambda: recording.books  # noqa: SLF001
    await run_scenario(recording, make_book)

    rep_clock = SimClock(T0)
    replay_ex = ReplayExchange.from_log_dir(
        tmp_path / "rec" / "logs", state_db=tmp_path / "rec" / "wta.sqlite", clock=rep_clock, markets=markets
    )
    replaying = MiniEngine(replay_ex, rep_clock, tmp_path / "rep", "replay", markets, cfg)
    replay_ex.set_balances_fallback(lambda: replaying.portfolio.balances)
    changed = dict(INTENTS)
    changed[0] = [lambda eng, now: _entry(eng, now).model_copy(update={"qty": D("0.4")})]
    monkeypatch.setattr(f"{__name__}.INTENTS", changed)
    await run_scenario(replaying, make_book)

    report = compare_decisions(
        tmp_path / "rec" / "logs", tmp_path / "rep" / "logs", divergences=replay_ex.all_divergences()
    )
    assert not report.ok
    first = report.mismatches[0]
    assert first.field == "intent.qty" and (first.reference, first.replayed) == ("0.5", "0.4")
    assert any(d["kind"] == "request_mismatch" for d in report.divergences)
