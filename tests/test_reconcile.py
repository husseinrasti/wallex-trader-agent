"""Reconciler + startup restore (PRD-02 §5).

Everything runs against an in-memory fake exchange and a minimal fake OrderManager that follows
the OrderManager contract (``managed`` keyed by client id, ``adopt``, ``place_stop_for``,
``balances_dirty``, ``to_state``/``load_state``). The fake exchange fails the test if the
reconciler ever tries to place or cancel an order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta.config import Config, load_config
from wta.data.store import Store
from wta.exchange.base import Exchange, WallexAPIError
from wta.execution.positions import PortfolioState, PositionManager
from wta.execution.reconcile import (
    KV_ORDER_MANAGER,
    KV_PORTFOLIO,
    MY_TRADES_LOOKBACK_S,
    Reconciler,
    delta_fill,
    persist_state,
    startup_restore,
    unaccounted_fills,
)
from wta.logging_.decision_log import EventsLog
from wta.models import Balance, BookSnapshot, Fill, Intent, MarketInfo, Order, OrderRequest, Position
from wta.risk.context import EngineFlags
from wta.utils.ids import make_client_id, make_decision_id
from wta.utils.timeutil import SimClock, day_start

from .conftest import T0, BookFactory

D = Decimal
BTC_PRICE = D("22000000000")  # TMN per BTC


# ---------------------------------------------------------------- fakes


class FakeExchange(Exchange):
    """Serves fixed balances/open orders/trades/get_order answers; can fail any call on demand."""

    name = "fake"

    def __init__(self) -> None:
        self.balances: dict[str, Balance] = {}
        self.open_orders: list[Order] = []
        self.trades: list[Fill] = []
        self.orders: dict[str, Order] = {}  # get_order answers (missing -> None = 404)
        self.fail: set[str] = set()
        self.trades_since: list[float | None] = []

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            raise WallexAPIError(503, None, f"{what} unavailable")

    async def get_markets(self) -> dict[str, MarketInfo]:
        return {}

    async def get_balances(self) -> dict[str, Balance]:
        self._maybe_fail("balances")
        return dict(self.balances)

    async def place_order(self, req: OrderRequest) -> Order:
        raise AssertionError("the reconciler must never place an order")

    async def get_order(self, client_id: str) -> Order | None:
        self._maybe_fail("get_order")
        return self.orders.get(client_id)

    async def cancel_order(self, client_id: str) -> Order | None:
        raise AssertionError("the reconciler must never cancel an order")

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._maybe_fail("open_orders")
        return list(self.open_orders)

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        self._maybe_fail("trades")
        self.trades_since.append(since_ts)
        return [t for t in self.trades if since_ts is None or t.ts >= since_ts]


@dataclass
class FakeManaged:
    """Shape of ``order_manager.ManagedOrder`` (contract fields)."""

    intent: Intent | None
    decision_id: str
    order: Order
    attempt: int = 0
    first_ts: float = T0 - 120
    placed_ts: float = T0 - 120
    max_reprices: int = 2
    crossed: bool = False
    logical_filled: Decimal = D("0")
    done: bool = False
    done_reason: str = ""
    is_stop: bool = False


class FakeOrderManager:
    def __init__(self, portfolio: PortfolioState) -> None:
        self.portfolio = portfolio
        self.managed: dict[str, FakeManaged] = {}
        self.balances_dirty = False
        self.adopted: list[Order] = []
        self.stops: list[str] = []
        self.loaded: tuple[dict[str, Any], list[Order]] | None = None

    def track(self, m: FakeManaged) -> FakeManaged:
        self.managed[m.order.client_id] = m
        self.portfolio.open_orders[m.order.client_id] = m.order
        return m

    def adopt(self, order: Order, intent: Intent | None = None) -> FakeManaged:
        self.adopted.append(order)
        return self.track(FakeManaged(intent=intent, decision_id=order.req.decision_id, order=order))

    async def place_stop_for(self, position: Position, books: dict[str, BookSnapshot]) -> object:
        position.stop_order_client_id = make_client_id(position.strategy, position.symbol, T0, position.id, 9)
        self.stops.append(position.id)
        return object()

    def to_state(self) -> dict[str, Any]:
        return {"managed": sorted(self.managed)}

    def load_state(self, state: dict[str, Any], orders: list[Order]) -> None:
        self.loaded = (state, orders)
        for o in orders:
            self.track(FakeManaged(intent=None, decision_id=o.req.decision_id, order=o))


# ---------------------------------------------------------------- fixtures / helpers


@pytest.fixture
def books(make_book: BookFactory) -> dict[str, BookSnapshot]:
    items = [
        make_book("USDTTMN", "266720", "266758"),
        make_book("BTCTMN", "22000000000", "22010000000"),
        make_book("ETHTMN", "650000000", "650100000"),
    ]
    return {b.symbol: b for b in items}


@dataclass
class Rig:
    exchange: FakeExchange
    portfolio: PortfolioState
    positions: PositionManager
    om: FakeOrderManager
    store: Store
    events_path: Path
    rec: Reconciler

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        return [r for r in rows if kind is None or r["kind"] == kind]


def make_rig(tmp_path: Path, cfg: Config, markets: dict[str, MarketInfo], clock: SimClock) -> Rig:
    exchange = FakeExchange()
    portfolio = PortfolioState(initial_equity_tmn=D("5000000"))
    positions = PositionManager(portfolio, markets)
    om = FakeOrderManager(portfolio)
    store = Store(tmp_path / "wta.sqlite")
    events = EventsLog(tmp_path / "logs", clock, rotate_daily=False)
    rec = Reconciler(exchange, portfolio, positions, om, store, events, clock, cfg)
    return Rig(exchange, portfolio, positions, om, store, tmp_path / "logs" / "events.jsonl", rec)


@pytest.fixture
def rig(tmp_path: Path, cfg: Config, markets: dict[str, MarketInfo], sim_clock: SimClock) -> Rig:
    r = make_rig(tmp_path, cfg, markets, sim_clock)
    yield r
    r.store.close()


def bal(asset: str, free: str, locked: str = "0") -> Balance:
    return Balance(asset=asset, free=D(free), locked=D(locked))


def balances(*items: Balance) -> dict[str, Balance]:
    return {b.asset: b for b in items}


def intent(
    symbol: str = "BTCTMN",
    side: str = "BUY",
    purpose: str = "entry",
    strategy: str = "S1",
    seq: int = 1,
    linked: str | None = None,
    stop_price: str | None = None,
) -> Intent:
    return Intent(
        id=make_decision_id(T0 - 120, strategy, symbol, seq),
        ts=T0 - 120,
        strategy=strategy,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type="STOP_LIMIT" if purpose == "stop" else "LIMIT",
        qty=D("0.0001"),
        purpose=purpose,  # type: ignore[arg-type]
        reason="test",
        linked_position_id=linked,
        stop_price=D(stop_price) if stop_price else None,
    )


def make_order(
    it: Intent,
    qty: str = "0.0001",
    price: str = "22000000000",
    placed_ts: float = T0 - 120,
    status: str = "NEW",
    filled: str = "0",
) -> Order:
    req = OrderRequest(
        client_id=make_client_id(it.strategy, it.symbol, placed_ts, it.id, 0),
        symbol=it.symbol,
        side=it.side,
        type=it.order_type,
        qty=D(qty),
        price=D(price),
        stop_price=D(price) if it.order_type == "STOP_LIMIT" else None,
        strategy=it.strategy,
        purpose=it.purpose,
        decision_id=it.id,
    )
    return Order(req=req, status=status, filled_qty=D(filled), created_ts=placed_ts, updated_ts=placed_ts)  # type: ignore[arg-type]


def track(rig: Rig, it: Intent, placed_ts: float = T0 - 120, **kw: Any) -> FakeManaged:
    order = make_order(it, placed_ts=placed_ts, **kw)
    return rig.om.track(
        FakeManaged(
            intent=it, decision_id=it.id, order=order, placed_ts=placed_ts, is_stop=it.purpose == "stop"
        )
    )


def fill(
    cid: str,
    qty: str,
    price: str = "22000000000",
    ts: float = T0 - 100,
    trade_id: str | None = None,
    side: str = "BUY",
    symbol: str = "BTCTMN",
    fee: str = "0",
    fee_asset: str = "",
) -> Fill:
    return Fill(
        order_client_id=cid,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        price=D(price),
        qty=D(qty),
        fee=D(fee),
        fee_asset=fee_asset,
        ts=ts,
        trade_id=trade_id,
    )


def open_position(rig: Rig, books: dict[str, BookSnapshot], qty: str = "0.0001", seq: int = 1) -> Position:
    """Open a BTCTMN position the normal way (entry fill through the PositionManager)."""
    it = intent(seq=seq, stop_price="21000000000")
    order = make_order(it, qty=qty)
    res = rig.positions.on_fill(fill(order.client_id, qty, ts=T0 - 600 + seq), order.req, books, it)
    assert isinstance(res, Position)
    return res


# ---------------------------------------------------------------- 2. missed fills


async def test_missed_fills_are_recovered_once(rig: Rig, books):
    m = track(rig, intent())
    cid = m.order.client_id
    rig.portfolio.set_balances(balances(bal("TMN", "5000000")))
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))
    t1 = fill(cid, "0.00006", trade_id="t1")
    rig.exchange.trades = [t1, fill(cid, "0.00004", ts=T0 - 90, trade_id="t2"), t1]  # t1 listed twice

    report = await rig.rec.run(books, T0)

    assert rig.exchange.trades_since == [T0 - MY_TRADES_LOOKBACK_S]
    assert report.missed_fills == 2 and report.ok
    (pos,) = rig.portfolio.positions.values()
    assert pos.qty == D("0.0001") and pos.strategy == "S1"
    assert report.finalized == [cid] and cid not in rig.om.managed and cid not in rig.portfolio.open_orders
    assert m.order.status == "FILLED" and m.logical_filled == D("0.0001") and m.done
    assert rig.om.balances_dirty is True
    assert report.drift == {}  # the recovered fills explain the balance change
    assert rig.portfolio.balances == rig.exchange.balances
    assert len(rig.store.load_fills()) == 2
    assert [o.status for o in rig.store.load_orders()] == ["FILLED"]

    again = await rig.rec.run(books, T0 + 300)
    assert again.missed_fills == 0
    assert len(rig.portfolio.positions) == 1 and pos.qty == D("0.0001")


async def test_only_the_uncounted_part_of_a_partial_fill_is_applied(rig: Rig, books):
    it = intent()
    m = track(rig, it, status="PARTIALLY_FILLED", filled="0.00006")
    cid = m.order.client_id
    t1 = fill(cid, "0.00006", trade_id="t1")
    rig.positions.on_fill(t1, m.order.req, books, it)  # the OrderManager already booked t1
    rig.exchange.open_orders = [m.order]  # still open on the exchange
    rig.exchange.trades = [t1, fill(cid, "0.00004", ts=T0 - 90, trade_id="t2")]
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))

    report = await rig.rec.run(books, T0)

    assert report.missed_fills == 1
    (pos,) = rig.portfolio.positions.values()
    assert pos.qty == D("0.0001")
    assert m.order.filled_qty == D("0.0001") and m.order.status == "FILLED"


async def test_missed_stop_fill_closes_the_position_as_stop(rig: Rig, books):
    pos = open_position(rig, books)
    stop = track(rig, intent(side="SELL", purpose="stop", seq=2, linked=pos.id), price="21000000000")
    rig.exchange.balances = balances(bal("TMN", "5000000"))
    rig.exchange.trades = [fill(stop.order.client_id, "0.0001", "21000000000", side="SELL", trade_id="s1")]

    report = await rig.rec.run(books, T0)

    (closed,) = report.closed_positions
    assert closed.position.id == pos.id and closed.exit_reason == "stop"
    assert closed.pnl_tmn == D("-100000")  # (21.0 bn - 22.0 bn) x 0.0001 BTC
    assert pos.id not in rig.portfolio.positions
    assert report.finalized == [stop.order.client_id]


# ---------------------------------------------------------------- 3. untracked open orders


async def test_foreign_orders_are_logged_once_and_never_touched(rig: Rig, books):
    foreign = Order(
        req=OrderRequest(
            client_id="web_8812",
            symbol="BTCTMN",
            side="SELL",
            type="LIMIT",
            qty=D("0.001"),
            price=D("23000000000"),
            strategy="foreign",
            purpose="exit",
            decision_id="",
        ),
        created_ts=T0 - 50,
        updated_ts=T0 - 50,
    )
    no_id = foreign.model_copy(
        update={"req": foreign.req.model_copy(update={"client_id": ""}), "exchange_id": "123"}
    )
    rig.exchange.open_orders = [foreign, no_id]

    first = await rig.rec.run(books, T0)
    second = await rig.rec.run(books, T0 + 300)

    assert first.foreign == ["exchange:123", "web_8812"] == second.foreign
    assert first.adopted == [] and rig.om.adopted == [] and rig.om.managed == {}
    events = rig.events("foreign_order")
    assert [e["data"]["client_id"] for e in events] == ["", "web_8812"]  # once per order, not per run
    assert "left untouched" in events[1]["reason"]


async def test_our_untracked_open_order_is_adopted_with_its_stored_request(rig: Rig, books):
    it = intent()
    stored = make_order(it)
    rig.store.upsert_order(stored)  # written when it was placed, before the crash
    # the exchange's view: our client id, but only a synthetic request
    seen = Order(
        req=OrderRequest(
            client_id=stored.client_id,
            symbol="BTCTMN",
            side="BUY",
            type="LIMIT",
            qty=D("0.0001"),
            price=D("22000000000"),
            strategy="unknown",
            purpose="exit",
            decision_id="",
        ),
        status="PARTIALLY_FILLED",
        filled_qty=D("0.00003"),
        exchange_id="9",
        created_ts=T0,
        updated_ts=T0,
    )
    other = intent(symbol="ETHTMN", seq=7)
    unknown = make_order(other).model_copy(
        update={"req": make_order(other).req.model_copy(update={"strategy": "unknown"})}
    )
    rig.exchange.open_orders = [seen, unknown]

    report = await rig.rec.run(books, T0)

    assert sorted(report.adopted) == sorted([stored.client_id, unknown.client_id])
    adopted = {o.client_id: o for o in rig.om.adopted}
    a = adopted[stored.client_id]
    assert (a.req.strategy, a.req.purpose, a.req.decision_id) == ("S1", "entry", it.id)
    assert a.filled_qty == D("0.00003") and a.status == "PARTIALLY_FILLED" and a.exchange_id == "9"
    # nothing stored for this one: kept as the exchange shows it
    assert adopted[unknown.client_id].req.strategy == "unknown"
    assert report.foreign == []


# ---------------------------------------------------------------- 4. tracked orders that disappeared


async def test_tracked_orders_missing_on_the_exchange_are_finalized(rig: Rig, books):
    canceled = track(rig, intent(seq=1))
    filled = track(rig, intent(seq=2))
    gone = track(rig, intent(seq=3))
    recent = track(rig, intent(seq=4), placed_ts=T0 - 3)  # within the grace period
    live = track(rig, intent(seq=5))
    rig.exchange.open_orders = [live.order]
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))
    rig.exchange.orders = {
        canceled.order.client_id: canceled.order.model_copy(update={"status": "CANCELED"}),
        filled.order.client_id: filled.order.model_copy(
            update={"status": "FILLED", "filled_qty": D("0.0001"), "avg_price": BTC_PRICE}
        ),
    }

    report = await rig.rec.run(books, T0)

    assert sorted(report.finalized) == sorted(
        [canceled.order.client_id, filled.order.client_id, gone.order.client_id]
    )
    assert canceled.order.status == "CANCELED"
    assert filled.order.status == "FILLED" and report.missed_fills == 1
    (pos,) = rig.portfolio.positions.values()  # the fill seen only via get_order opened the position
    assert pos.qty == D("0.0001") and pos.entry_price == BTC_PRICE
    assert gone.order.status == "EXPIRED" and "not found" in gone.done_reason
    assert set(rig.om.managed) == {recent.order.client_id, live.order.client_id}
    assert set(rig.portfolio.open_orders) == set(rig.om.managed)


async def test_recovered_entry_fill_gets_its_server_stop(rig: Rig, books):
    m = track(rig, intent(stop_price="21000000000"))
    rig.exchange.trades = [fill(m.order.client_id, "0.0001", trade_id="t1")]
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))

    report = await rig.rec.run(books, T0)

    (pos,) = rig.portfolio.positions.values()
    assert report.stops_placed == [pos.id] == rig.om.stops
    assert pos.stop_order_client_id is not None


async def test_no_server_stop_when_software_stops_are_configured(tmp_path, markets, sim_clock, books):
    cfg = load_config(overrides={"execution.use_server_side_stops": False}, env={})
    r = make_rig(tmp_path, cfg, markets, sim_clock)
    m = track(r, intent(stop_price="21000000000"))
    r.exchange.trades = [fill(m.order.client_id, "0.0001", trade_id="t1")]
    report = await r.rec.run(books, T0)
    assert report.stops_placed == [] and r.om.stops == []
    r.store.close()


# ---------------------------------------------------------------- 5. positions whose coins are gone


async def test_stop_fired_while_down_closes_the_position(rig: Rig, books):
    btc = open_position(rig, books)
    eth_it = intent(symbol="ETHTMN", seq=9)
    eth_order = make_order(eth_it, qty="0.002", price="650000000")
    eth = rig.positions.on_fill(
        fill(eth_order.client_id, "0.002", "650000000", symbol="ETHTMN"), eth_order.req, books, eth_it
    )
    assert isinstance(eth, Position)
    # BTC is gone (its server stop fired while we were down); ETH is 0.5% short (rounding/fees): kept
    rig.exchange.balances = balances(bal("TMN", "7000000"), bal("ETH", "0.00199"))

    report = await rig.rec.run(books, T0)

    (closed,) = report.closed_positions
    assert closed.position.id == btc.id and closed.exit_reason == "stop"
    assert set(rig.portfolio.positions) == {eth.id}
    assert [p.id for p in rig.store.load_open_positions()] == [eth.id]
    assert btc.id in {p.id for p in rig.store.load_positions()}  # kept, as closed


async def test_position_with_a_live_stop_keeps_the_coins_first(rig: Rig, books):
    older = open_position(rig, books, seq=1)
    newer = open_position(rig, books, seq=2)
    stop = make_order(intent(side="SELL", purpose="stop", seq=3, linked=newer.id), price="21000000000")
    newer.stop_order_client_id = stop.client_id
    rig.exchange.open_orders = [stop]  # newer's stop is live and locks its coins; it gets adopted
    rig.exchange.balances = balances(bal("TMN", "5000000"), bal("BTC", "0", "0.0001"))

    report = await rig.rec.run(books, T0)

    assert [c.position.id for c in report.closed_positions] == [older.id]
    assert set(rig.portfolio.positions) == {newer.id}


# ---------------------------------------------------------------- 6. drift


async def test_balance_drift_is_logged_and_overwritten(rig: Rig, books):
    rig.portfolio.set_balances(balances(bal("TMN", "5000000"), bal("USDT", "10")))
    rig.exchange.balances = balances(bal("TMN", "4900000"), bal("USDT", "10.01"))

    report = await rig.rec.run(books, T0)

    assert set(report.drift) == {"TMN"}  # 100,000 TMN > 0.5% of equity; the USDT cent is noise
    assert report.drift["TMN"]["computed"] == D("5000000")
    assert report.drift["TMN"]["exchange"] == D("4900000")
    (event,) = rig.events("reconcile_drift")
    assert "TMN" in event["data"]["assets"]
    assert rig.portfolio.balances == rig.exchange.balances  # exchange values always win


# ---------------------------------------------------------------- API errors


async def test_api_errors_are_recorded_not_raised(rig: Rig, books):
    pos = open_position(rig, books)
    m = track(rig, intent(seq=5))
    rig.portfolio.set_balances(balances(bal("TMN", "5000000")))
    rig.exchange.fail = {"balances", "open_orders", "trades"}

    report = await rig.rec.run(books, T0)

    assert not report.ok
    # balances are read last (a fill between two reads then only shows up in the balances)
    assert [e.split(":")[0] for e in report.errors] == ["open orders", "my trades", "balances"]
    # nothing was guessed: position kept, order still tracked, balances untouched
    assert pos.id in rig.portfolio.positions
    assert m.order.client_id in rig.om.managed
    assert rig.portfolio.balances == balances(bal("TMN", "5000000"))
    assert rig.events("reconcile")[0]["data"]["errors"] == report.errors


async def test_get_order_error_leaves_the_order_tracked(rig: Rig, books):
    m = track(rig, intent())
    rig.exchange.fail = {"get_order"}
    report = await rig.rec.run(books, T0)
    assert report.errors == [
        f"get_order {m.order.client_id}: " + str(WallexAPIError(503, None, "get_order unavailable"))
    ]
    assert m.order.client_id in rig.om.managed and not m.done


async def test_run_works_without_store_or_events_log(cfg, markets, sim_clock, books):
    exchange = FakeExchange()
    portfolio = PortfolioState(initial_equity_tmn=D("5000000"))
    om = FakeOrderManager(portfolio)
    rec = Reconciler(exchange, portfolio, PositionManager(portfolio, markets), om, None, None, sim_clock, cfg)
    exchange.balances = balances(bal("TMN", "5000000"))
    report = await rec.run(books)  # now from the injected clock
    assert report.ok and report.ts == T0
    assert portfolio.balances == exchange.balances


# ---------------------------------------------------------------- 7. persistence + startup restore


async def test_reconcile_persists_state(rig: Rig, books):
    pos = open_position(rig, books)
    m = track(rig, intent(seq=4))
    rig.exchange.open_orders = [m.order]
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))
    rig.portfolio.record_placement(T0 - 120)

    await rig.rec.run(books, T0)

    assert [p.id for p in rig.store.load_open_positions()] == [pos.id]
    assert [o.client_id for o in rig.store.load_orders()] == [m.order.client_id]
    assert rig.store.kv_get(KV_PORTFOLIO) == rig.portfolio.to_state()
    assert rig.store.kv_get(KV_ORDER_MANAGER) == {"managed": [m.order.client_id]}
    assert rig.store.latest_balances() == rig.exchange.balances


async def test_startup_restore_keeps_peak_day_fees_counters_and_positions(tmp_path, markets, books):
    store = Store(tmp_path / "wta.sqlite")
    now = T0 + 60
    # --- state of the process that crashed
    old = PortfolioState(initial_equity_tmn=D("5000000"))
    old.peak_equity_tmn = D("5200000")
    old.day_start_equity_tmn = D("5100000")
    old.day_start_ts = day_start(T0)
    old.fees_paid_tmn_total = D("1234.5")
    for ts in (T0 - 7200, T0 - 1800, T0 - 60):
        old.record_placement(ts)
    old.add_realized("S1", D("5000"))
    old_flags = EngineFlags(killed=True, kill_reason="drawdown 5.1%", flatten_phase=1)
    persist_state(store, T0, old, old_flags, FakeOrderManager(old))
    store.add_equity(T0 - 3600, D("5300000"), peak_tmn=D("5300000"))  # the equity table saw a higher peak
    open_pos = Position(
        id="p_open",
        strategy="S1",
        symbol="BTCTMN",
        base="BTC",
        quote="TMN",
        qty=D("0.0001"),
        entry_price=BTC_PRICE,
        entry_ts=T0 - 900,
        meta={"entry_decision_id": "d_x"},
    )
    closed_pos = open_pos.model_copy(update={"id": "p_closed"})
    store.upsert_position(open_pos, open=True, ts=T0)
    store.upsert_position(closed_pos, open=False, ts=T0)
    live = make_order(intent(seq=1))
    done = make_order(intent(seq=2), status="FILLED", filled="0.0001")
    store.upsert_order(live)
    store.upsert_order(done)

    # --- the new process
    pf = PortfolioState(initial_equity_tmn=D("5000000"))
    flags = EngineFlags()
    om = FakeOrderManager(pf)
    summary = await startup_restore(store, pf, flags, PositionManager(pf, markets), om, now)

    assert pf.peak_equity_tmn == D("5300000")  # max(kv peak, equity table)
    assert pf.day_start_equity_tmn == D("5100000") and pf.day_start_ts == day_start(T0)
    assert pf.fees_paid_tmn_total == D("1234.5")
    assert pf.orders_today(now) == 3 and pf.orders_last_hour(now) == 2
    assert pf.realized_pnl_tmn == {"S1": D("5000")}
    assert flags.killed and flags.kill_reason == "drawdown 5.1%" and flags.flatten_phase == 1
    assert list(pf.positions) == ["p_open"]
    assert om.loaded is not None
    assert om.loaded[0] == {"managed": []}
    assert [o.client_id for o in om.loaded[1]] == [live.client_id]  # only non-terminal orders
    assert summary["peak_equity_tmn"] == "5300000" and summary["positions"] == ["p_open"]

    # the first refresh after the restart (same UTC day, lower equity) resets nothing
    pf.set_balances(balances(bal("TMN", "4000000")))
    pf.refresh(books, now)
    assert pf.peak_equity_tmn == D("5300000") and pf.day_start_equity_tmn == D("5100000")
    store.close()


async def test_startup_restore_prefers_the_higher_stored_peak(tmp_path, markets):
    store = Store(tmp_path / "wta.sqlite")
    old = PortfolioState(initial_equity_tmn=D("5000000"), peak_equity_tmn=D("5400000"))
    persist_state(store, T0, old)
    store.add_equity(T0 - 3600, D("5100000"), peak_tmn=D("5100000"))
    pf = PortfolioState(initial_equity_tmn=D("5000000"))
    await startup_restore(store, pf, EngineFlags(), PositionManager(pf, markets), FakeOrderManager(pf), T0)
    assert pf.peak_equity_tmn == D("5400000")
    store.close()


async def test_startup_restore_without_store_is_a_no_op(markets):
    pf = PortfolioState(initial_equity_tmn=D("5000000"))
    out = await startup_restore(
        None, pf, EngineFlags(), PositionManager(pf, markets), FakeOrderManager(pf), T0
    )
    assert out == {"restored": False} and pf.peak_equity_tmn is None


# ---------------------------------------------------------------- pure helpers


def test_unaccounted_fills_splits_at_the_known_quantity_and_caps_at_order_qty():
    fills = [
        fill("c", "0.4", trade_id="a", fee="4"),
        fill("c", "0.4", trade_id="b"),
        fill("c", "0.4", trade_id="x"),
    ]
    out = unaccounted_fills(fills, known_qty=D("0.3"), order_qty=D("1"))
    assert [(f.qty, f.trade_id) for f in out] == [(D("0.1"), "a:part"), (D("0.4"), "b"), (D("0.2"), "x:part")]
    assert out[0].fee == D("1")  # pro rata: 4 x 0.1 / 0.4
    assert unaccounted_fills(fills, known_qty=D("1.2"), order_qty=D("1.2")) == []


def test_delta_fill_uses_the_incremental_average_price(books):
    it = intent()
    known = make_order(it, status="PARTIALLY_FILLED", filled="0.00006").model_copy(
        update={"avg_price": BTC_PRICE}
    )
    latest = known.model_copy(
        update={
            "status": "FILLED",
            "filled_qty": D("0.0001"),
            "avg_price": D("22400000000"),
            "updated_ts": T0,
        }
    )
    f = delta_fill(known, latest, books, T0 + 5)
    assert f is not None
    assert f.qty == D("0.00004") and f.price == D("23000000000") and f.ts == T0
    assert f.side == "BUY" and f.order_client_id == known.client_id


# ---------------------------------------------------------------- review fixes (Phase 2)


async def test_a_dead_server_stop_hands_its_position_to_the_software_stop(rig: Rig, books):
    """Without ``finish_external`` the reconciler itself must not leave the position unprotected."""
    pos = open_position(rig, books)
    stop = track(rig, intent(side="SELL", purpose="stop", seq=3, linked=pos.id))
    pos.stop_order_client_id = stop.order.client_id
    pos.meta = {**pos.meta, "stop_mode": "server"}
    rig.exchange.orders = {stop.order.client_id: stop.order.model_copy(update={"status": "CANCELED"})}
    rig.exchange.balances = balances(bal("TMN", "2800000"), bal("BTC", "0.0001"))

    report = await rig.rec.run(books, T0)

    assert report.finalized == [stop.order.client_id]
    assert pos.stop_order_client_id is None and pos.meta["stop_mode"] == "software"
    (saved,) = rig.store.load_open_positions()
    assert saved.meta["stop_mode"] == "software" and saved.stop_order_client_id is None


async def test_a_recovered_fill_is_never_force_closed_in_the_same_run(rig: Rig, books):
    """The balances do not show the coins of a fill the trades already show (a read race)."""
    m = track(rig, intent(stop_price="21000000000"))
    rig.exchange.trades = [fill(m.order.client_id, "0.0001", trade_id="t1")]
    rig.exchange.balances = balances(bal("TMN", "5000000"))  # read before the coins arrived

    report = await rig.rec.run(books, T0)

    (pos,) = rig.portfolio.positions.values()
    assert report.closed_positions == [] and pos.qty == D("0.0001")
    assert report.stops_placed == [pos.id]  # protected after the coins check, not closed by it


async def test_fills_without_a_tmn_price_wait_for_the_books(rig: Rig, books, make_book):
    usdt_it = intent(symbol="BTCUSDT", seq=4)
    m = rig.om.track(
        FakeManaged(
            intent=usdt_it,
            decision_id=usdt_it.id,
            order=make_order(usdt_it, price="82500"),
        )
    )
    rig.exchange.trades = [fill(m.order.client_id, "0.0001", "82500", trade_id="u1", symbol="BTCUSDT")]
    rig.exchange.open_orders = [m.order]
    no_dollar = {k: v for k, v in books.items() if k != "USDTTMN"}

    first = await rig.rec.run(no_dollar, T0)

    assert first.missed_fills == 0 and rig.portfolio.positions == {}  # never booked at 0 TMN
    assert any("no TMN price for USDT" in e for e in first.errors)
    with_books = {**books, "BTCUSDT": make_book("BTCUSDT", "82500.00", "82520.00")}
    second = await rig.rec.run(with_books, T0 + 60)
    assert second.missed_fills == 1
    (pos,) = rig.portfolio.positions.values()
    assert D(pos.meta["value_in_tmn"]) == D("0.0001") * D("82500") * D("266720")


async def test_no_force_close_without_a_price_for_the_coin(rig: Rig, books):
    pos = open_position(rig, books)
    rig.exchange.balances = balances(bal("TMN", "7000000"))  # the BTC is gone
    no_btc = {k: v for k, v in books.items() if k != "BTCTMN"}

    report = await rig.rec.run(no_btc, T0)

    assert report.closed_positions == [] and pos.id in rig.portfolio.positions
    assert any("no TMN price yet" in e for e in report.errors)
    report = await rig.rec.run(books, T0 + 60)
    assert [c.position.id for c in report.closed_positions] == [pos.id]


async def test_adoption_uses_the_intent_of_the_write_ahead_record(rig: Rig, books):
    it = intent(stop_price="21000000000")
    placeholder = make_order(it, status="UNKNOWN").model_copy(
        update={"raw": {"write_ahead": True, "intent": it.model_dump(mode="json")}}
    )
    rig.store.upsert_order(placeholder)  # stored right before the request left; then the crash
    seen = placeholder.model_copy(
        update={
            "req": placeholder.req.model_copy(update={"strategy": "unknown", "purpose": "exit", "decision_id": ""}),
            "status": "NEW",
            "raw": {},
        }
    )
    rig.exchange.open_orders = [seen]

    await rig.rec.run(books, T0)

    m = rig.om.managed[placeholder.client_id]
    assert m.intent is not None and m.intent.stop_price == D("21000000000") and m.intent.id == it.id
