"""OrderManager (PRD-02 §4): placement prices, maker-first lifecycle, fills, server stops, safety
guards, decision records, ambiguous placements and restart.

Everything runs against ``FakeExchange`` below: orders rest until a test fills them, every call is
recorded, and rejections / ambiguous placements / fills racing a cancel can be injected. Time is a
``SimClock``; ``Harness.tick(s)`` advances it and runs one ``poll``. Toy prices (BTCUSDT ~100) keep
the arithmetic readable; precision and minimums are the real ones from ``tests/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
import pytest

from wta.config import Config, apply_changes
from wta.data.store import Store
from wta.exchange.base import Exchange, OrderRejected, WallexAPIError
from wta.execution.order_manager import STOP_TYPES, ManagedOrder, OrderManager, PollResult
from wta.execution.positions import ClosedPosition, PortfolioState, PositionManager
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import DecisionLog, OrdersLog
from wta.models import (
    D0,
    Balance,
    BookSnapshot,
    Fill,
    Intent,
    MarketInfo,
    Order,
    OrderRequest,
    Position,
    RiskCheck,
    RiskDecision,
)
from wta.utils.decimalutil import D
from wta.utils.ids import make_client_id
from wta.utils.timeutil import SimClock

from .conftest import T0, BookFactory

APPROVE = RiskDecision(approved=True, checks=[RiskCheck(code="ALL", passed=True, detail="test fixture")])


# ---------------------------------------------------------------- fake exchange


class FakeExchange(Exchange):
    """In-memory exchange. Orders rest until ``fill()``; every call is recorded in ``calls``."""

    name = "fake"

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.orders: dict[str, Order] = {}
        self.calls: list[tuple[str, str]] = []  # (place|get|cancel, client_id)
        self.placed: list[OrderRequest] = []
        self.reject: Callable[[OrderRequest], str | None] = lambda req: None  # -> rejection code
        self.ambiguous: str | None = None  # next placement: "register" (it exists) or "lost" (it does not)
        self.on_cancel: dict[str, Callable[[], None]] = {}  # runs just before a cancel (fill races)
        self.double_sells: list[str] = []  # SELLs placed while another SELL was live on the symbol
        self.self_cross: list[str] = []  # non-stop orders placed opposite a live non-stop order

    # interface ------------------------------------------------------------
    async def get_markets(self) -> dict[str, MarketInfo]:
        return {}

    async def get_balances(self) -> dict[str, Balance]:
        return {}

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o.model_copy(deep=True) for o in self.orders.values() if not o.is_terminal]

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        return []

    async def place_order(self, req: OrderRequest) -> Order:
        self.calls.append(("place", req.client_id))
        self.placed.append(req)
        code = self.reject(req)
        if code:
            raise OrderRejected(400, code, f"{req.type} {req.side} refused by the fake exchange")
        if req.side == "SELL" and self.live(req.symbol, "SELL"):
            self.double_sells.append(req.client_id)
        other = "BUY" if req.side == "SELL" else "SELL"
        if req.type not in STOP_TYPES and any(
            o.req.type not in STOP_TYPES for o in self.live(req.symbol, other)
        ):
            self.self_cross.append(req.client_id)
        mode, self.ambiguous = self.ambiguous, None
        now = self.clock.now()
        if mode != "lost":
            self.orders[req.client_id] = Order(
                req=req, exchange_id=f"x{len(self.orders) + 1}", status="NEW", created_ts=now, updated_ts=now
            )
        if mode is not None:
            raise WallexAPIError(None, "ambiguous", "timeout while placing the order")
        return self.orders[req.client_id].model_copy(deep=True)

    async def get_order(self, client_id: str) -> Order | None:
        self.calls.append(("get", client_id))
        o = self.orders.get(client_id)
        return None if o is None else o.model_copy(deep=True)

    async def cancel_order(self, client_id: str) -> Order | None:
        self.calls.append(("cancel", client_id))
        hook = self.on_cancel.pop(client_id, None)
        if hook is not None:
            hook()
        o = self.orders.get(client_id)
        if o is None:
            return None
        if not o.is_terminal:
            o.status = "CANCELED"
            o.updated_ts = self.clock.now()
        return o.model_copy(deep=True)

    # test controls ----------------------------------------------------------
    def fill(
        self, client_id: str, qty: str, price: object, fee: str = "0", fee_asset: str | None = None
    ) -> None:
        """Execute ``qty`` more of an order at ``price`` (``fee`` is this fill's fee)."""
        o = self.orders[client_id]
        q, p = D(qty), D(price)
        filled = o.filled_qty + q
        o.avg_price = ((o.avg_price or D0) * o.filled_qty + p * q) / filled
        o.filled_qty = filled
        o.fee_paid = o.fee_paid + D(fee)
        if fee_asset:
            o.fee_asset = fee_asset
        o.status = "FILLED" if filled >= o.req.qty else "PARTIALLY_FILLED"

    def live(self, symbol: str, side: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if not o.is_terminal and o.req.symbol == symbol and (side is None or o.req.side == side)
        ]

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


# ---------------------------------------------------------------- harness


@dataclass
class Harness:
    ex: FakeExchange
    om: OrderManager
    portfolio: PortfolioState
    positions: PositionManager
    store: Store
    clock: SimClock
    books: dict[str, BookSnapshot]
    log_dir: Path
    make_book: BookFactory

    async def tick(self, seconds: float = 2.0) -> PollResult:
        self.clock.advance(seconds)
        return await self.om.poll(self.books, self.clock.now())

    def set_book(self, symbol: str, bid: str, ask: str) -> None:
        self.books[symbol] = self.make_book(symbol, bid, ask, qty="10", ts=self.clock.now())

    def decisions(self, kind: str | None = None, decision_id: str | None = None) -> list[dict[str, Any]]:
        path = self.log_dir / "decisions.jsonl"
        recs = (
            [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
            if path.exists()
            else []
        )
        return [
            r
            for r in recs
            if (kind is None or r["kind"] == kind)
            and (decision_id is None or r["decision_id"] == decision_id)
        ]

    def outcome(self, decision_id: str) -> dict[str, Any]:
        found = self.decisions("outcome", decision_id)
        assert len(found) == 1, f"expected one outcome for {decision_id}, got {found}"
        return found[0]["outcome"]

    def only_position(self) -> Position:
        assert len(self.portfolio.positions) == 1, self.portfolio.positions
        return next(iter(self.portfolio.positions.values()))


@pytest.fixture
def build(
    tmp_path: Path, markets: dict[str, MarketInfo], cfg: Config, sim_clock: SimClock, make_book: BookFactory
) -> Iterator[Callable[..., Harness]]:
    """``build(changes, exchange=, store=)`` -> a Harness; exchange/store can be shared (restart)."""
    stores: list[Store] = []

    def _build(
        changes: dict[str, Any] | None = None,
        exchange: FakeExchange | None = None,
        store: Store | None = None,
    ) -> Harness:
        c = apply_changes(cfg, changes) if changes else cfg
        ex = exchange or FakeExchange(sim_clock)
        if store is None:
            store = Store(tmp_path / "wta.sqlite")
            stores.append(store)
        portfolio = PortfolioState(initial_equity_tmn=D("5000000"))
        portfolio.set_balances(
            {
                "TMN": Balance(asset="TMN", free=D("5000000")),
                "USDT": Balance(asset="USDT", free=D("1000")),
                "BTC": Balance(asset="BTC", free=D("0")),
            }
        )
        positions = PositionManager(portfolio, markets)
        dlog = DecisionLog(tmp_path, "shadow", "abc123def456", "cfg000000001", sim_clock, rotate_daily=False)
        olog = OrdersLog(tmp_path, sim_clock, rotate_daily=False)
        om = OrderManager(ex, portfolio, positions, markets, c, sim_clock, dlog, olog, store)
        books = {
            "BTCUSDT": make_book("BTCUSDT", "100.00", "100.10", qty="10", ts=sim_clock.now()),
            "ETHUSDT": make_book("ETHUSDT", "2000.00", "2000.50", qty="10", ts=sim_clock.now()),
            "USDTTMN": make_book("USDTTMN", "100000", "100010", qty="1000", ts=sim_clock.now()),
        }
        return Harness(ex, om, portfolio, positions, store, sim_clock, books, tmp_path, make_book)

    yield _build
    for s in stores:
        s.close()


def mk_intent(
    id: str = "d1",
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    qty: str | None = "0.5",
    purpose: str = "entry",
    strategy: str = "S3",
    **kw: Any,
) -> Intent:
    return Intent(
        id=id,
        ts=T0,
        strategy=strategy,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type=kw.pop("order_type", "LIMIT"),
        qty=D(qty) if qty is not None else None,
        purpose=purpose,  # type: ignore[arg-type]
        reason=f"test {purpose} {side} {symbol}",
        **kw,
    )


async def open_position(
    h: Harness, qty: str = "0.5", stop: str | None = None, intent_id: str = "d_entry"
) -> Position:
    """Entry BUY on BTCUSDT, filled in full at its maker price, then one poll (entry done)."""
    stop_price = D(stop) if stop is not None else None
    mo = await h.om.submit(mk_intent(intent_id, qty=qty, stop_price=stop_price), APPROVE, h.books)
    assert mo is not None
    h.ex.fill(mo.client_id, qty, mo.order.req.price)
    await h.tick()
    assert mo.done and mo.done_reason == "FILLED"
    return h.only_position()


def exit_intent(pos: Position, id: str = "d_exit", **kw: Any) -> Intent:
    features = kw.pop("features", {"exit_reason": "target"})
    return mk_intent(
        id, side="SELL", qty=str(pos.qty), purpose="exit", linked_position_id=pos.id, features=features, **kw
    )


# ---------------------------------------------------------------- placement price & request


async def test_maker_first_price_improves_the_top_by_one_tick(build: Callable[..., Harness]) -> None:
    h = build()
    buy = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    sell = await h.om.submit(
        mk_intent("d2", symbol="ETHUSDT", side="SELL", qty="0.1", purpose="rebalance"), APPROVE, h.books
    )
    assert buy is not None and sell is not None
    assert buy.order.req.price == D("100.01")  # bid 100.00 + 1 tick, still below ask 100.10
    assert sell.order.req.price == D("2000.49")  # ask 2000.50 - 1 tick
    req = buy.order.req
    assert req.decision_id == "d1" and buy.decision_id == "d1"
    assert req.client_id == make_client_id("S3", "BTCUSDT", T0, "d1", 0)
    assert (req.type, req.side, req.qty, req.purpose) == ("LIMIT", "BUY", D("0.5"), "entry")
    assert sorted(h.portfolio.open_orders) == sorted([buy.client_id, sell.client_id])
    assert h.portfolio.placements == [T0, T0]
    order_recs = h.decisions("order", "d1")
    assert len(order_recs) == 1
    assert order_recs[0]["order"]["request"]["client_id"] == req.client_id
    assert order_recs[0]["order"]["accepted"] is True
    assert order_recs[0]["risk"]["approved"] is True


async def test_maker_price_with_a_one_tick_spread_joins_the_best_level(build: Callable[..., Harness]) -> None:
    h = build()
    h.set_book("BTCUSDT", "100.00", "100.01")
    h.set_book("ETHUSDT", "2000.00", "2000.01")
    buy = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    sell = await h.om.submit(
        mk_intent("d2", symbol="ETHUSDT", side="SELL", qty="0.1", purpose="rebalance"), APPROVE, h.books
    )
    assert buy is not None and sell is not None
    assert buy.order.req.price == D("100.00")  # bid + tick would reach the ask: stay at the bid
    assert sell.order.req.price == D("2000.01")


async def test_taker_crosses_and_an_explicit_price_is_rounded_by_side(build: Callable[..., Harness]) -> None:
    h = build()
    taker = await h.om.submit(mk_intent("d1", taker=True), APPROVE, h.books)
    priced = await h.om.submit(
        mk_intent("d2", symbol="ETHUSDT", price=D("1999.995"), purpose="rebalance"), APPROVE, h.books
    )
    assert taker is not None and priced is not None
    assert taker.order.req.price == D("100.10") and taker.crossed
    assert priced.order.req.price == D("1999.99")  # BUY rounds down


async def test_adjusted_qty_from_the_risk_manager_wins(build: Callable[..., Harness]) -> None:
    h = build()
    decision = RiskDecision(approved=True, adjusted_qty=D("0.3000009"))
    mo = await h.om.submit(mk_intent("d1"), decision, h.books)
    assert mo is not None and mo.order.req.qty == D("0.300000")  # rounded down to the 6-decimal step


# ---------------------------------------------------------------- maker-first lifecycle


async def test_exit_reprices_twice_then_crosses_and_keeps_crossing(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h)
    mo = await h.om.submit(exit_intent(pos), APPROVE, h.books)
    assert mo is not None
    assert pos.meta["exit_pending"] is True
    times, prices = [h.clock.now()], [mo.order.req.price]
    for ask in ("100.20", "100.30"):  # maker re-prices at the new top
        h.set_book("BTCUSDT", "100.00", ask)
        await h.tick(15)
        times.append(h.clock.now())
        prices.append(mo.order.req.price)
    assert prices == [D("100.09"), D("100.19"), D("100.29")] and not mo.crossed
    h.set_book("BTCUSDT", "99.90", "100.30")
    await h.tick(15)  # limit_max_reprices reached -> cross at the bid
    times.append(h.clock.now())
    assert mo.crossed and mo.order.req.price == D("99.90")
    h.set_book("BTCUSDT", "99.80", "100.00")
    await h.tick(15)  # still not filled -> re-cross at the new bid
    times.append(h.clock.now())
    assert mo.order.req.price == D("99.80") and mo.reprices == 4 and not mo.done

    exit_reqs = [r for r in h.ex.placed if r.decision_id == "d_exit"]
    assert [r.client_id for r in exit_reqs] == [
        make_client_id("S3", "BTCUSDT", t, "d_exit", n) for n, t in enumerate(times)
    ]
    assert all(r.qty == D("0.5") for r in exit_reqs)
    assert h.ex.double_sells == [] and len(h.ex.live("BTCUSDT", "SELL")) == 1
    assert len(h.portfolio.placements) == h.ex.count("place")  # every placement counted
    assert list(h.portfolio.open_orders) == [mo.client_id]

    h.ex.fill(mo.client_id, "0.5", "99.80")
    res = await h.tick()
    assert mo.done and mo.done_reason == "FILLED"
    assert len(res.closed) == 1 and res.closed[0].exit_reason == "target"
    assert h.portfolio.positions == {} and h.om.managed == {}
    entry_outcome = h.outcome("d_entry")  # the position's PnL goes to the entry decision
    assert entry_outcome["exit_reason"] == "target" and D(entry_outcome["pnl_tmn"]) < 0
    exit_outcome = h.outcome("d_exit")  # plus an order-level outcome for the exit decision
    assert exit_outcome["status"] == "FILLED" and exit_outcome["reprices"] == 4 and exit_outcome["crossed"]
    assert exit_outcome["avg_price"] == "99.80"


async def test_entry_gives_up_after_max_reprices(build: Callable[..., Harness]) -> None:
    h = build({"execution.limit_cancel_after_s": 1000})
    mo = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    assert mo is not None
    res = None
    for _ in range(3):
        res = await h.tick(15)
    assert res is not None and mo in res.done
    assert mo.done and mo.done_reason == "EXPIRED_UNFILLED" and mo.reprices == 2 and not mo.crossed
    assert h.ex.count("place") == 3 and h.ex.live("BTCUSDT") == []
    assert h.om.managed == {} and h.portfolio.open_orders == {}
    out = h.outcome("d1")
    assert (out["status"], out["done_reason"], out["filled_qty"]) == ("UNFILLED", "EXPIRED_UNFILLED", "0")


async def test_entry_hard_cap_cancels_after_limit_cancel_after_s(build: Callable[..., Harness]) -> None:
    h = build({"execution.limit_reprice_after_s": 100})
    mo = await h.om.submit(mk_intent("d1", purpose="rebalance"), APPROVE, h.books)
    assert mo is not None
    await h.tick(44)
    assert not mo.done
    await h.tick(2)
    assert mo.done and mo.done_reason == "EXPIRED_UNFILLED" and "hard cap" in mo.detail
    assert h.ex.count("place") == 1


async def test_taker_entry_recrosses_at_most_max_reprices_then_gives_up(
    build: Callable[..., Harness],
) -> None:
    h = build({"execution.limit_cancel_after_s": 1000})
    mo = await h.om.submit(mk_intent("d1", taker=True), APPROVE, h.books)
    assert mo is not None and mo.order.req.price == D("100.10")
    for ask in ("100.20", "100.30"):
        h.set_book("BTCUSDT", "100.00", ask)
        await h.tick(15)
        assert mo.order.req.price == D(ask)
    await h.tick(15)
    assert mo.done and mo.done_reason == "EXPIRED_UNFILLED" and h.ex.count("place") == 3


async def test_taker_exit_recrosses_until_filled(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h)
    mo = await h.om.submit(exit_intent(pos, taker=True), APPROVE, h.books)
    assert mo is not None and mo.order.req.price == D("100.00")
    for bid in ("99.90", "99.80", "99.70", "99.60"):
        h.set_book("BTCUSDT", bid, "100.10")
        await h.tick(15)
        assert mo.order.req.price == D(bid) and not mo.done
    assert mo.reprices == 4 > h.om.ex.limit_max_reprices
    h.ex.fill(mo.client_id, "0.5", "99.60")
    await h.tick()
    assert mo.done and mo.done_reason == "FILLED" and h.portfolio.positions == {}


# ---------------------------------------------------------------- fills


async def test_partial_fills_become_deltas_and_the_remainder_is_repriced(
    build: Callable[..., Harness],
) -> None:
    h = build()
    mo = await h.om.submit(mk_intent("d1", qty="1.0"), APPROVE, h.books)
    assert mo is not None
    first_cid = mo.client_id
    h.ex.fill(first_cid, "0.4", "100.01", fee="0.04", fee_asset="USDT")
    r1 = await h.tick()
    assert [(e.fill.qty, e.fill.price, e.fill.fee, e.fill.fee_asset) for e in r1.fills] == [
        (D("0.4"), D("100.01"), D("0.04"), "USDT")
    ]
    pos = r1.fills[0].result
    assert isinstance(pos, Position) and pos.qty == D("0.4") and mo.position_id == pos.id
    assert h.om.balances_dirty and h.portfolio.total("BTC") == D("0.4")

    h.ex.fill(first_cid, "0.2", "99.98", fee="0.02", fee_asset="USDT")  # avg on the exchange is now 100
    r2 = await h.tick()
    fill = r2.fills[0].fill
    assert (fill.qty, fill.price, fill.fee) == (D("0.2"), D("99.98"), D("0.02"))  # delta, not cumulative
    assert pos.qty == D("0.6")

    await h.tick(11)  # 15 s after placement: cancel and re-place the remaining 0.4 at the new top
    assert mo.client_id != first_cid and mo.attempt == 1
    assert mo.order.req.qty == D("0.4") and mo.order.req.decision_id == "d1"
    assert h.ex.orders[first_cid].status == "CANCELED"
    h.ex.fill(mo.client_id, "0.4", "100.01", fee="0.04", fee_asset="USDT")
    await h.tick()
    assert mo.done and mo.done_reason == "FILLED" and mo.logical_filled == D("1.0")
    assert h.only_position() is pos and pos.qty == D("1.0")
    assert mo.avg_price == (D("0.4") * D("100.01") + D("0.2") * D("99.98") + D("0.4") * D("100.01")) / D(
        "1.0"
    )
    stored = h.store.load_fills()
    assert [f.qty for f in stored] == [D("0.4"), D("0.2"), D("0.4")]


async def test_dust_remainder_ends_the_order(build: Callable[..., Harness]) -> None:
    h = build()
    mo = await h.om.submit(mk_intent("d1", symbol="ETHUSDT", qty="0.0100"), APPROVE, h.books)
    assert mo is not None and mo.order.req.price == D("2000.01")
    h.ex.fill(mo.client_id, "0.0098", "2000.01")
    await h.tick()
    await h.tick(13)  # reprice time: remainder 0.0002 x 2000 = 0.4 USDT < 1 USDT minimum
    assert mo.done and mo.done_reason == "DUST_REMAINDER"
    assert h.ex.count("place") == 1 and h.ex.live("ETHUSDT") == []
    assert h.only_position().qty == D("0.0098")


async def test_adopted_order_books_only_new_fills(build: Callable[..., Harness]) -> None:
    h = build()
    req = OrderRequest(
        client_id=make_client_id("S2", "USDTTMN", T0, "d_old", 0),
        symbol="USDTTMN",
        side="BUY",
        type="LIMIT",
        qty=D("10"),
        price=D("100001"),
        strategy="S2",
        purpose="rebalance",
        decision_id="d_old",
    )
    h.ex.orders[req.client_id] = Order(req=req, status="NEW", created_ts=T0, updated_ts=T0)
    h.ex.fill(req.client_id, "4", "100001")  # booked before (e.g. by the reconciler)
    mo = h.om.adopt(h.ex.orders[req.client_id].model_copy(deep=True))
    assert isinstance(mo, ManagedOrder) and mo.logical_filled == D("4")
    assert req.client_id in h.portfolio.open_orders
    h.ex.fill(req.client_id, "6", "100001")
    res = await h.tick()
    assert [e.fill.qty for e in res.fills] == [D("6")]
    assert mo.done and mo.done_reason == "FILLED" and h.outcome("d_old")["filled_qty"] == "10"


# ---------------------------------------------------------------- server-side stops


async def test_server_stop_is_placed_when_the_entry_is_done(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h, stop="95.00")
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None and pos.meta["stop_mode"] == "server"
    stop = h.om.managed[stop_cid]
    assert stop.is_stop and stop.position_id == pos.id
    req = stop.order.req
    assert (req.type, req.side, req.qty, req.stop_price, req.price, req.purpose) == (
        "STOP_LIMIT",
        "SELL",
        D("0.5"),
        D("95.00"),
        D("94.62"),  # 95 x (1 - 0.004), rounded up as a SELL
        "stop",
    )
    assert req.decision_id == "d_entry_stop1" and "exit_pending" not in pos.meta
    rec = h.decisions("order", "d_entry_stop1")
    assert len(rec) == 1 and rec[0]["notes"].startswith("server stop placed")
    assert rec[0]["risk"]["checks"][0]["code"] == "PROTECTIVE_STOP"

    # polled every 5 x poll_interval only, never re-priced or timed out
    for _ in range(4):
        await h.tick()
    assert h.ex.calls.count(("get", stop_cid)) == 0
    await h.tick()
    assert h.ex.calls.count(("get", stop_cid)) == 1
    await h.tick(200)
    assert h.ex.count("cancel") == 0 and stop_cid in h.om.managed

    h.ex.fill(stop_cid, "0.5", "94.70")  # the stop fires
    res = await h.tick(10)
    assert len(res.closed) == 1 and res.closed[0].exit_reason == "stop"
    assert h.outcome("d_entry")["exit_reason"] == "stop"
    assert h.outcome("d_entry_stop1")["status"] == "FILLED"
    assert h.om.managed == {} and h.portfolio.positions == {}


async def test_rejected_server_stop_falls_back_to_a_software_stop(build: Callable[..., Harness]) -> None:
    h = build()
    h.ex.reject = lambda req: "INVALID_TYPE" if req.type == "STOP_LIMIT" else None
    pos = await open_position(h, stop="95.00")
    assert pos.stop_order_client_id is None and pos.meta["stop_mode"] == "software"
    rec = h.decisions("order", "d_entry_stop1")
    assert len(rec) == 1 and rec[0]["notes"].startswith("server stop rejected -> software stop")
    # the software stop is live: PositionManager emits a stop exit once bid <= stop
    h.set_book("BTCUSDT", "94.90", "95.00")
    intents = h.positions.check_exits(h.books, h.clock.now(), software_stops=False)
    assert [(i.purpose, i.linked_position_id) for i in intents] == [("stop", pos.id)]


async def test_server_stops_disabled_by_config_use_software_stops(build: Callable[..., Harness]) -> None:
    h = build({"execution.use_server_side_stops": False})
    pos = await open_position(h, stop="95.00")
    assert pos.meta["stop_mode"] == "software" and pos.stop_order_client_id is None
    assert not any(r.type == "STOP_LIMIT" for r in h.ex.placed)
    assert h.decisions("order", "d_entry_stop1")[0]["notes"].startswith("software stop (config)")


async def test_stop_limit_intent_is_placed_as_a_server_stop(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h)  # no stop yet (e.g. an S1 lock-in leg 1)
    intent = mk_intent(
        "d_degraded",
        side="SELL",
        qty="0.5",
        purpose="stop",
        order_type="STOP_LIMIT",
        stop_price=D("99.00"),
        price=D("98.61"),
        linked_position_id=pos.id,
        features={"exit_reason": "stop", "note": "lockin_degraded_to_revert", "time_stop_s": 1200},
    )
    mo = await h.om.submit(intent, APPROVE, h.books)
    assert mo is not None and mo.is_stop
    assert (mo.order.req.type, mo.order.req.stop_price, mo.order.req.price) == (
        "STOP_LIMIT",
        D("99.00"),
        D("98.61"),
    )
    assert pos.stop_price == D("99.00") and pos.time_stop_ts == h.clock.now() + 1200
    assert pos.stop_order_client_id == mo.client_id and pos.meta["stop_mode"] == "server"


# ---------------------------------------------------------------- never two SELLs


async def test_exit_cancels_the_server_stop_before_selling(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h, stop="95.00")
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    mo = await h.om.submit(
        exit_intent(pos, taker=True, features={"exit_reason": "time_stop"}), APPROVE, h.books
    )
    assert mo is not None
    assert h.ex.calls.index(("cancel", stop_cid)) < h.ex.calls.index(("place", mo.client_id))
    assert h.ex.double_sells == []
    assert pos.stop_order_client_id is None and pos.meta["stop_mode"] == "software"
    assert pos.meta["exit_pending"] is True and stop_cid not in h.om.managed
    assert "canceled first" in h.decisions("order", "d_exit")[0]["notes"]
    res = await h.tick()  # events from submit() come with the next poll
    assert [(m.client_id, m.done_reason) for m in res.done] == [(stop_cid, "CANCELED_FOR_EXIT")]
    # a second closing SELL for the same coins is refused while the first is live
    again = await h.om.submit(exit_intent(pos, id="d_exit2"), APPROVE, h.books)
    assert again is None and h.decisions("order", "d_exit2")[0]["notes"].startswith("REFUSED")
    h.ex.fill(mo.client_id, "0.5", "100.00")
    res = await h.tick()
    assert res.closed and res.closed[0].exit_reason == "time_stop"


async def test_exit_skipped_when_the_stop_already_filled(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h, stop="95.00")
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    h.ex.on_cancel[stop_cid] = lambda: h.ex.fill(stop_cid, "0.5", "94.80")  # race: it fired first
    mo = await h.om.submit(exit_intent(pos), APPROVE, h.books)
    assert mo is None
    assert h.ex.count("place") == 2  # entry + stop: no exit SELL was sent
    assert pos.id not in h.portfolio.positions
    assert h.decisions("order", "d_exit")[0]["notes"].startswith("SKIPPED")
    res = await h.tick()
    assert [e.fill.qty for e in res.fills] == [D("0.5")]
    assert isinstance(res.closed[0], ClosedPosition) and res.closed[0].exit_reason == "stop"
    assert h.outcome("d_entry")["exit_reason"] == "stop"


async def test_exit_reduced_when_the_stop_partially_filled(build: Callable[..., Harness]) -> None:
    h = build()
    pos = await open_position(h, stop="95.00")
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    h.ex.on_cancel[stop_cid] = lambda: h.ex.fill(stop_cid, "0.2", "94.80")
    mo = await h.om.submit(exit_intent(pos), APPROVE, h.books)
    assert mo is not None and mo.order.req.qty == D("0.3") and pos.qty == D("0.3")
    assert h.ex.double_sells == []
    assert "had filled 0.2" in h.decisions("order", "d_exit")[0]["notes"]


async def test_no_buy_and_sell_open_in_the_same_symbol(build: Callable[..., Harness]) -> None:
    h = build()
    buy = await h.om.submit(
        mk_intent("d1", symbol="USDTTMN", qty="10", purpose="rebalance"), APPROVE, h.books
    )
    assert buy is not None and buy.order.req.price == D("100001")
    sell = await h.om.submit(
        mk_intent("d2", symbol="USDTTMN", side="SELL", qty="10", purpose="rebalance"), APPROVE, h.books
    )
    assert sell is None and h.ex.count("place") == 1
    rec = h.decisions("order", "d2")
    assert len(rec) == 1 and rec[0]["notes"].startswith("REFUSED: SELF_CROSS_GUARD")


async def test_resting_stop_does_not_block_a_new_entry(build: Callable[..., Harness]) -> None:
    h = build()
    await open_position(h, stop="95.00")
    second = await h.om.submit(mk_intent("d_second"), APPROVE, h.books)
    assert second is not None and h.ex.self_cross == []


async def test_exit_cancels_its_own_working_entry_first(build: Callable[..., Harness]) -> None:
    h = build()
    entry = await h.om.submit(mk_intent("d_entry", qty="1.0", stop_price=D("95.00")), APPROVE, h.books)
    assert entry is not None
    h.ex.fill(entry.client_id, "0.4", "100.01")
    await h.tick()
    pos = h.only_position()
    mo = await h.om.submit(exit_intent(pos, taker=True), APPROVE, h.books)
    assert entry.done and entry.done_reason == "CANCELED_FOR_EXIT"
    assert mo is not None and mo.order.req.qty == D("0.4")
    assert h.ex.self_cross == [] and not any(r.type == "STOP_LIMIT" for r in h.ex.placed)


# ---------------------------------------------------------------- decision records


async def test_veto_is_logged_and_nothing_is_sent(build: Callable[..., Harness]) -> None:
    h = build()
    decision = RiskDecision(
        approved=False,
        checks=[RiskCheck(code="MAX_OPEN_ORDERS", passed=False, detail="8 open >= cap 8")],
        veto_reason="MAX_OPEN_ORDERS: 8 open >= cap 8",
    )
    assert await h.om.submit(mk_intent("d1"), decision, h.books, plan_snapshot={"regime": "range"}) is None
    assert h.ex.calls == [] and h.portfolio.placements == []
    recs = h.decisions(decision_id="d1")
    assert [r["kind"] for r in recs] == ["veto"]
    assert recs[0]["notes"] == "MAX_OPEN_ORDERS: 8 open >= cap 8"
    assert recs[0]["risk"]["approved"] is False and recs[0]["intent"]["id"] == "d1"
    assert recs[0]["plan_snapshot"] == {"regime": "range"}


async def test_rejected_order_is_logged_and_not_tracked(build: Callable[..., Harness]) -> None:
    h = build()
    h.ex.reject = lambda req: "INSUFFICIENT_BALANCE"
    assert await h.om.submit(mk_intent("d1"), APPROVE, h.books) is None
    assert h.om.managed == {} and h.portfolio.open_orders == {} and h.portfolio.placements == []
    rec = h.decisions("order", "d1")
    assert rec[0]["notes"] == "REJECTED: INSUFFICIENT_BALANCE LIMIT BUY refused by the fake exchange"
    assert rec[0]["order"]["accepted"] is False


async def test_outcome_records_for_a_rebalance_and_logs_validate(build: Callable[..., Harness]) -> None:
    h = build()
    mo = await h.om.submit(
        mk_intent("d_reb", symbol="USDTTMN", qty="10", purpose="rebalance"), APPROVE, h.books
    )
    assert mo is not None
    h.ex.fill(mo.client_id, "10", "100001", fee="0.01", fee_asset="USDT")
    res = await h.tick()
    assert mo in res.done and res.fills[0].result is None  # no strategy position involved
    out = h.outcome("d_reb")
    assert (out["status"], out["done_reason"], out["filled_qty"], out["avg_price"]) == (
        "FILLED",
        "FILLED",
        "10",
        "100001",
    )
    assert (out["fee_paid"], out["fee_asset"], out["purpose"]) == ("0.01", "USDT", "rebalance")
    report = check_logs(h.log_dir)
    assert report.ok, report.summary()


async def test_cancel_where_cancels_matching_orders(build: Callable[..., Harness]) -> None:
    h = build()
    a = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    b = await h.om.submit(mk_intent("d2", symbol="ETHUSDT", qty="0.1", purpose="rebalance"), APPROVE, h.books)
    assert a is not None and b is not None
    n = await h.om.cancel_where(lambda m: m.purpose == "entry", "kill")
    assert n == 1 and a.done and a.done_reason == "kill" and not b.done
    assert h.outcome("d1")["done_reason"] == "kill"


async def test_exit_flag_is_cleared_when_its_order_was_finalized_elsewhere(
    build: Callable[..., Harness],
) -> None:
    h = build()
    pos = await open_position(h)
    mo = await h.om.submit(exit_intent(pos), APPROVE, h.books)
    assert mo is not None and pos.meta["exit_pending"] is True
    mo.done = True  # e.g. the Reconciler finalized it from the exchange's answer
    h.om.managed.pop(mo.client_id)
    await h.tick()
    assert pos.meta["exit_pending"] is False


# ---------------------------------------------------------------- ambiguous placements


async def test_ambiguous_placement_is_resolved_by_poll_without_resend(build: Callable[..., Harness]) -> None:
    h = build()
    h.ex.ambiguous = "register"  # the order did reach the exchange, but the answer was lost
    mo = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    assert mo is not None and mo.order.status == "UNKNOWN"
    assert mo.client_id in h.portfolio.open_orders and len(h.portfolio.placements) == 1
    assert h.decisions("order", "d1")[0]["notes"].startswith("AMBIGUOUS")
    await h.tick()
    assert mo.order.status == "NEW" and h.ex.count("place") == 1
    h.ex.fill(mo.client_id, "0.5", "100.01")
    await h.tick()
    assert mo.done and mo.done_reason == "FILLED" and h.ex.count("place") == 1


async def test_ambiguous_placement_not_found_is_given_up_without_resend(
    build: Callable[..., Harness],
) -> None:
    h = build()
    h.ex.ambiguous = "lost"  # it never reached the exchange
    mo = await h.om.submit(mk_intent("d1"), APPROVE, h.books)
    assert mo is not None
    await h.tick()
    await h.tick()
    assert not mo.done
    await h.tick()
    assert mo.done and mo.done_reason == "NOT_FOUND"
    assert h.ex.count("place") == 1 and h.portfolio.open_orders == {}
    assert h.outcome("d1")["status"] == "UNFILLED"


# ---------------------------------------------------------------- restart


def restart(h: Harness, build: Callable[..., Harness], state: dict[str, Any]) -> Harness:
    """A new process: fresh portfolio/positions/OrderManager on the same exchange and store."""
    h2 = build(exchange=h.ex, store=h.store)
    for pos in h2.store.load_open_positions():
        h2.portfolio.positions[pos.id] = pos
    h2.om.load_state(state, h2.store.load_orders(non_terminal_only=True))
    return h2


async def test_restart_resumes_tracking_from_state(build: Callable[..., Harness]) -> None:
    h = build()
    mo = await h.om.submit(mk_intent("d1", qty="1.0"), APPROVE, h.books)
    assert mo is not None
    cid = mo.client_id
    h.ex.fill(cid, "0.4", "100.01")
    await h.tick()
    state = h.om.to_state()

    h2 = restart(h, build, state)
    mo2 = h2.om.managed[cid]
    assert mo2.logical_filled == D("0.4") and mo2.decision_id == "d1" and cid in h2.portfolio.open_orders
    assert h2.om.balances_dirty
    h.ex.fill(cid, "0.6", "100.01")
    res = await h2.tick()
    assert [e.fill.qty for e in res.fills] == [D("0.6")]  # only the new part
    assert mo2.done and mo2.done_reason == "FILLED" and h.ex.count("place") == 1
    assert h2.only_position().qty == D("1.0")


async def test_restart_with_a_stale_state_does_not_book_fills_twice(build: Callable[..., Harness]) -> None:
    h = build()
    mo = await h.om.submit(mk_intent("d1", qty="1.0"), APPROVE, h.books)
    assert mo is not None
    cid = mo.client_id
    h.ex.fill(cid, "0.4", "100.01")
    await h.tick()
    state = h.om.to_state()  # last saved state ...
    h.ex.fill(cid, "0.2", "100.01")
    await h.tick()  # ... then one more fill is booked before the crash

    h2 = restart(h, build, state)
    mo2 = h2.om.managed[cid]
    assert mo2.logical_filled == D("0.6")
    assert h2.only_position().qty == D("0.6")
    h.ex.fill(cid, "0.4", "100.01")
    res = await h2.tick()
    assert [e.fill.qty for e in res.fills] == [D("0.4")]
    assert mo2.done and h2.only_position().qty == D("1.0")


# ---------------------------------------------------------------- review fixes (Phase 2)


async def test_a_crash_while_sending_is_resolved_with_the_original_intent(build: Callable[..., Harness]) -> None:
    """The request reached the exchange, then the process died before anything else was saved."""
    h = build()
    real_place = h.ex.place_order

    async def place_then_die(req: OrderRequest) -> Order:
        await real_place(req)
        raise KeyboardInterrupt  # stands in for the process dying mid-request

    h.ex.place_order = place_then_die  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await h.om.submit(mk_intent("d_entry", stop_price=D("95.00")), APPROVE, h.books)
    (record,) = h.store.load_orders(non_terminal_only=True)
    assert record.status == "UNKNOWN" and record.raw["intent"]["stop_price"] == "95.00"
    h.ex.place_order = real_place  # type: ignore[method-assign]

    h2 = restart(h, build, {})  # the kv state never saw the order
    mo = h2.om.managed[record.client_id]
    assert mo.intent.id == "d_entry" and mo.intent.stop_price == D("95.00") and mo.purpose == "entry"
    h.ex.fill(record.client_id, "0.5", "100.01")
    await h2.tick()
    pos = h2.only_position()
    assert pos.stop_price == D("95.00") and pos.stop_order_client_id is not None  # protected
    assert h.ex.count("place") == 2  # the entry once, then its server stop: never re-sent


async def test_a_rejected_request_is_stored_as_rejected(build: Callable[..., Harness]) -> None:
    h = build()
    h.ex.reject = lambda req: "INSUFFICIENT_BALANCE"
    assert await h.om.submit(mk_intent("d1"), APPROVE, h.books) is None
    (record,) = h.store.load_orders()
    assert record.status == "REJECTED" and h.store.load_orders(non_terminal_only=True) == []


async def test_the_fill_baseline_never_goes_down(build: Callable[..., Harness]) -> None:
    h = build()
    mo = await h.om.submit(mk_intent("d1", qty="1.0"), APPROVE, h.books)
    assert mo is not None
    h.ex.fill(mo.client_id, "0.4", "100.01", fee="0.0004", fee_asset="BTC")
    res = await h.tick()
    assert [e.fill.qty for e in res.fills] == [D("0.4")]
    real_get = h.ex.get_order

    async def lagging(cid: str) -> Order | None:  # an answer that lags the trades endpoint
        o = await real_get(cid)
        return None if o is None else o.model_copy(update={"filled_qty": D0, "avg_price": None, "fee_paid": D0})

    h.ex.get_order = lagging  # type: ignore[method-assign]
    assert (await h.tick()).fills == []
    assert mo.order.filled_qty == D("0.4") and mo.order.fee_paid == D("0.0004")  # kept
    h.ex.get_order = real_get  # type: ignore[method-assign]
    assert (await h.tick()).fills == []  # the same 0.4 is not counted again
    assert h.only_position().qty == D("0.3996")


async def test_a_store_error_never_loses_a_fill(build: Callable[..., Harness]) -> None:
    import sqlite3

    h = build()
    mo = await h.om.submit(mk_intent("d1", qty="0.5"), APPROVE, h.books)
    assert mo is not None
    h.ex.fill(mo.client_id, "0.5", "100.01")
    real_upsert = h.store.upsert_order

    def locked(order: Order) -> None:
        raise sqlite3.OperationalError("database is locked")

    h.store.upsert_order = locked  # type: ignore[method-assign]
    res = await h.tick()  # does not raise
    assert [e.fill.qty for e in res.fills] == [D("0.5")] and h.om.store_errors > 0
    assert h.only_position().qty == D("0.5") and mo.done
    h.store.upsert_order = real_upsert  # type: ignore[method-assign]
    assert (await h.tick()).fills == []


async def test_an_adopted_order_of_unknown_origin_is_canceled_not_crossed(build: Callable[..., Harness]) -> None:
    h = build()
    req = OrderRequest(  # what the exchange gives back for our id without a stored request
        client_id=make_client_id("S3", "BTCUSDT", T0, "d_lost", 0),
        symbol="BTCUSDT",
        side="BUY",
        type="LIMIT",
        qty=D("0.5"),
        price=D("99.00"),
        strategy="unknown",
        purpose="exit",
        decision_id="",
    )
    h.ex.orders[req.client_id] = Order(req=req, status="NEW", created_ts=T0, updated_ts=T0)
    mo = h.om.adopt(h.ex.orders[req.client_id].model_copy(deep=True))
    for _ in range(10):
        await h.tick(15)
    assert mo.done and mo.done_reason == "ADOPTED_UNKNOWN"
    assert h.ex.count("place") == 0 and h.ex.orders[req.client_id].status == "CANCELED"


async def test_replacement_ids_always_carry_one_of_our_prefixes(build: Callable[..., Harness]) -> None:
    from wta.utils.ids import is_ours

    h = build()
    mo = await h.om.submit(mk_intent("d1", strategy="manual", purpose="rebalance"), APPROVE, h.books)
    assert mo is not None and is_ours(mo.client_id) and mo.client_id.startswith("EX_")
    assert mo.order.req.strategy == "manual"


async def test_finish_external_runs_the_full_bookkeeping(build: Callable[..., Harness]) -> None:
    """What the Reconciler learned from the exchange is handled like an order the manager saw end."""
    h = build()
    pos = await open_position(h, stop="95.00")
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    h.ex.orders[stop_cid].status = "CANCELED"  # canceled on the exchange (e.g. while we were down)
    await h.om.finish_external(h.om.managed[stop_cid], "CANCELED", "exchange status CANCELED", h.books, h.clock.now())
    assert pos.stop_order_client_id is None and pos.meta["stop_mode"] == "software"
    h.set_book("BTCUSDT", "94.00", "94.10")
    [stop_exit] = h.positions.check_exits(h.books, h.clock.now(), software_stops=False)
    assert stop_exit.purpose == "stop"

    sell = await h.om.submit(exit_intent(pos), APPROVE, h.books)
    assert sell is not None
    h.ex.orders[sell.client_id].status = "EXPIRED"
    placed = h.ex.count("place")
    await h.om.finish_external(sell, "EXPIRED", "not found on the exchange", h.books, h.clock.now())
    assert not sell.done and h.ex.count("place") == placed + 1  # the must-finish exit re-crosses
    assert sell.order.req.price == D("94.00") and sell.crossed
