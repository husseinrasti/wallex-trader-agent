"""FlattenManager (PRD-02 §7): SELL-only routing to TMN, phases, idempotency, dust, final state.

Prices are the Overview §2.1 snapshot: 1 BTC ~ 22.08 bn TMN, so 0.0001 BTC ~ 2.2 M TMN and
anything below ~0.0000023 BTC is under the 50,000 TMN minimum order (dust).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from wta.config import Config, load_config
from wta.execution.positions import PortfolioState, valuate
from wta.models import Balance, BookSnapshot, Intent, MarketInfo, Order, OrderRequest, Position
from wta.risk.context import Deadline, EngineFlags, RiskContext
from wta.risk.flatten import (
    FlattenManager,
    assert_sell_only,
    cancel_at_flatten_start,
    flatten_phase_transitions,
)
from wta.utils.ids import make_client_id, make_decision_id

from .conftest import T0, BookFactory

D = Decimal
END = T0 + 2 * 3600  # the challenge ends two hours after the suite's "now"


# ---------------------------------------------------------------- fixtures / helpers


@pytest.fixture
def books(make_book: BookFactory) -> dict[str, BookSnapshot]:
    items = [
        make_book("USDTTMN", "266720", "266758"),
        make_book("BTCTMN", "22075699081", "22106074058"),
        make_book("BTCUSDT", "82845.00", "82909.16"),
        make_book("ETHTMN", "650660181", "650859142"),
        make_book("ETHUSDT", "2440.00", "2441.00"),
        make_book("SOLTMN", "28000019", "28105486"),
    ]
    return {b.symbol: b for b in items}


def bal(asset: str, free: str, locked: str = "0") -> Balance:
    return Balance(asset=asset, free=D(free), locked=D(locked))


def ctx_for(
    cfg: Config,
    markets: dict[str, MarketInfo],
    books: dict[str, BookSnapshot],
    balances: list[Balance],
    positions: list[Position] | None = None,
    phase: int = 1,
) -> RiskContext:
    pf = PortfolioState(initial_equity_tmn=D("5000000"))
    pf.set_balances({b.asset: b for b in balances})
    pf.positions = {p.id: p for p in positions or []}
    return RiskContext(
        now=T0,
        portfolio=pf,
        books=books,
        markets=markets,
        cfg=cfg,
        flags=EngineFlags(flatten_phase=phase),
        deadline=Deadline(END, END - 7200, END - 1800, END - 300),
    )


def position(pid: str, qty: str, symbol: str = "BTCUSDT", entry_ts: float = T0 - 600) -> Position:
    base, quote = ("BTC", "USDT") if symbol == "BTCUSDT" else ("BTC", "TMN")
    return Position(
        id=pid,
        strategy="S3",
        symbol=symbol,
        base=base,
        quote=quote,
        qty=D(qty),
        entry_price=D("82000"),
        entry_ts=entry_ts,
        stop_price=D("80000"),
        stop_order_client_id=make_client_id("S3", symbol, entry_ts, pid, 0),
        meta={"entry_decision_id": f"d_{pid}"},
    )


def order(
    symbol: str,
    side: str,
    qty: str,
    purpose: str,
    otype: str = "LIMIT",
    status: str = "NEW",
    strategy: str = "FLATTEN",
) -> Order:
    did = make_decision_id(T0 - 10, strategy, symbol, 1)
    req = OrderRequest(
        client_id=make_client_id(strategy, symbol, T0 - 10, did, 0),
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type=otype,  # type: ignore[arg-type]
        qty=D(qty),
        price=D("1"),
        stop_price=D("1") if otype == "STOP_LIMIT" else None,
        strategy=strategy,
        purpose=purpose,  # type: ignore[arg-type]
        decision_id=did,
    )
    return Order(req=req, status=status, created_ts=T0 - 10, updated_ts=T0 - 10)  # type: ignore[arg-type]


def by_symbol(intents: list[Intent]) -> dict[str, Intent]:
    return {i.symbol: i for i in intents}


# ---------------------------------------------------------------- phase 1: routing, maker-first


def test_phase1_maker_sells_coin_on_tmn_market_and_usdt_on_usdttmn(cfg, markets, books):
    ctx = ctx_for(cfg, markets, books, [bal("TMN", "1000000"), bal("BTC", "0.0001"), bal("USDT", "5")])
    intents = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)

    got = by_symbol(intents)
    assert sorted(got) == ["BTCTMN", "USDTTMN"]
    for it in intents:
        assert it.side == "SELL" and it.purpose == "flatten" and it.strategy == "FLATTEN"
        assert it.order_type == "LIMIT" and it.price is None and it.taker is False  # maker-first
        assert it.linked_position_id is None
        assert it.reason.startswith("Flatten phase 1")
    assert got["BTCTMN"].qty == D("0.0001")
    assert got["USDTTMN"].qty == D("5")
    assert "direct TMN market" in got["BTCTMN"].reason
    # deterministic decision ids, FLATTEN strategy -> "FL" client-id prefix downstream
    assert got["BTCTMN"].id == make_decision_id(T0, "FLATTEN", "BTCTMN", 1)
    assert got["USDTTMN"].id == make_decision_id(T0, "FLATTEN", "USDTTMN", 2)
    assert got["BTCTMN"].features["exit_reason"] == "flatten"


def test_qty_is_rounded_down_to_the_market_step(cfg, markets, books):
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001239999")])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)
    assert it.qty == D("0.000123")  # BTCTMN step = 6 decimals, rounded down


def test_falls_back_to_usdt_market_when_tmn_market_missing(cfg, markets, books):
    no_btctmn = {k: v for k, v in markets.items() if k != "BTCTMN"}
    ctx = ctx_for(cfg, no_btctmn, books, [bal("BTC", "0.0001")])
    (it,) = FlattenManager(cfg, no_btctmn).plan(ctx, [], phase=1, books=books)
    assert it.symbol == "BTCUSDT" and it.side == "SELL"
    assert it.features["route"] == "USDT"
    assert "USDTTMN next" in it.reason


def test_falls_back_to_usdt_market_when_tmn_market_banned(markets, books):
    cfg = load_config(overrides={"markets.banned": ["BTCTMN"]}, env={})
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001")])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)
    assert it.symbol == "BTCUSDT"


def test_asset_without_any_allowed_market_is_reported_not_traded(markets, books):
    cfg = load_config(overrides={"markets.banned": ["BTCTMN", "BTCUSDT"]}, env={})
    fm = FlattenManager(cfg, markets)
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001")])
    assert fm.plan(ctx, [], phase=1, books=books) == []
    assert [s.state for s in fm.last_scan] == ["no_market"]


def test_missing_book_is_skipped_until_it_arrives(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    no_book = {k: v for k, v in books.items() if k != "ETHTMN"}
    ctx = ctx_for(cfg, markets, no_book, [bal("ETH", "0.002")])
    assert fm.plan(ctx, [], phase=1, books=no_book) == []
    assert fm.last_scan[0].state == "no_book"


# ---------------------------------------------------------------- phase 2: MARKET vs crossing LIMIT


def test_phase2_market_where_enabled_else_crossing_limit(cfg, markets, books):
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001"), bal("SOL", "0.05")], phase=2)
    got = by_symbol(FlattenManager(cfg, markets).plan(ctx, [], phase=2, books=books))

    btc = got["BTCTMN"]
    assert btc.order_type == "MARKET" and btc.price is None and btc.taker is True
    assert "MARKET sell" in btc.reason

    sol = got["SOLTMN"]  # SOLTMN has isMarketTypeEnable=false
    bid = D("28000019")
    assert sol.order_type == "LIMIT" and sol.taker is True
    assert sol.price == D("27860019")  # bid x (1 - 0.5%) = 27,860,018.905 -> tick 0, SELL rounds up
    assert sol.price < bid
    assert "MARKET orders are disabled on SOLTMN" in sol.reason


def test_no_intents_outside_flatten_phases(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001")])
    assert fm.plan(ctx, [], phase=0, books=books) == []
    assert fm.plan(ctx, [], phase=3, books=books) == []  # after the end: no orders at all


# ---------------------------------------------------------------- idempotency


def test_asset_with_open_flatten_order_is_skipped(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0", "0.0001"), bal("USDT", "5")])
    working = order("BTCTMN", "SELL", "0.0001", "flatten")
    intents = fm.plan(ctx, [working], phase=1, books=books)

    assert [i.symbol for i in intents] == ["USDTTMN"]
    states = {s.asset: s.state for s in fm.last_scan}
    assert states == {"BTC": "working", "USDT": "sell"}

    # re-running with the same open orders just continues: same decision, nothing new for BTC
    again = fm.plan(ctx, [working], phase=1, books=books)
    assert [(i.symbol, i.qty) for i in again] == [("USDTTMN", D("5"))]


def test_open_exit_order_blocks_but_stop_orders_and_terminal_orders_do_not(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    ctx = ctx_for(cfg, markets, books, [bal("ETH", "0", "0.002"), bal("BTC", "0.0002", "0.0001")])
    exit_order = order("ETHUSDT", "SELL", "0.002", "exit", strategy="S1")
    stop_order = order("BTCUSDT", "SELL", "0.0001", "stop", otype="STOP_LIMIT", strategy="S3")
    done_flatten = order("BTCTMN", "SELL", "0.0001", "flatten", status="FILLED")
    intents = fm.plan(ctx, [exit_order, stop_order, done_flatten], phase=1, books=books)
    assert [i.symbol for i in intents] == ["BTCTMN"]
    assert {s.asset: s.state for s in fm.last_scan} == {"BTC": "sell", "ETH": "working"}


def test_same_inputs_give_identical_intents(cfg, markets, books):
    bals = [bal("BTC", "0.0001"), bal("USDT", "5"), bal("SOL", "0.05")]
    a = FlattenManager(cfg, markets).plan(ctx_for(cfg, markets, books, bals), [], 1, books)
    b = FlattenManager(cfg, markets).plan(ctx_for(cfg, markets, books, bals), [], 1, books)
    assert [i.model_dump() for i in a] == [i.model_dump() for i in b]


def test_ids_stay_unique_when_planned_twice_at_the_same_time(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0001")])
    first = fm.plan(ctx, [], 1, books)
    second = fm.plan(ctx, [], 1, books)  # e.g. kill-switch and deadline in the same tick
    assert first[0].id != second[0].id


# ---------------------------------------------------------------- dust


def test_dust_below_min_notional_is_reported_not_traded(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    # 0.000002 BTC ~ 44,151 TMN and 0.1 USDT ~ 26,672 TMN: both under the 50,000 TMN minimum
    ctx = ctx_for(cfg, markets, books, [bal("TMN", "5000000"), bal("BTC", "0.000002"), bal("USDT", "0.1")])
    assert fm.plan(ctx, [], phase=1, books=books) == []
    assert {s.asset: s.state for s in fm.last_scan} == {"BTC": "dust", "USDT": "dust"}
    assert "dust" in fm.last_scan[0].detail

    state = fm.final_state(ctx)
    assert [d["asset"] for d in state["dust"]] == ["BTC", "USDT"]
    assert state["non_tmn_over_min"] == [] and state["complete"] is True
    assert fm.is_complete(ctx)


# ---------------------------------------------------------------- never a BUY


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("trigger", ["deadline", "kill"])
def test_flatten_never_emits_a_buy(cfg, markets, books, phase, trigger):
    balances = [
        bal("TMN", "100"),
        bal("BTC", "0.0003", "0.0001"),
        bal("ETH", "0.002"),
        bal("SOL", "0.05"),
        bal("USDT", "5", "1"),
        bal("FOO", "100"),  # no market at all
    ]
    ctx = ctx_for(cfg, markets, books, balances, [position("p1", "0.0001")], phase=phase)
    intents = FlattenManager(cfg, markets).plan(ctx, [], phase=phase, books=books, trigger=trigger)
    assert intents, "something must be sold"
    assert all(i.side == "SELL" and i.purpose == "flatten" for i in intents)


def test_assert_sell_only_rejects_a_buy():
    buy = Intent(
        id="d_1_FLATTEN_BTCTMN_1",
        ts=T0,
        strategy="FLATTEN",
        symbol="BTCTMN",
        side="BUY",
        order_type="LIMIT",
        qty=D("0.001"),
        purpose="flatten",
        reason="must never happen",
    )
    with pytest.raises(AssertionError):
        assert_sell_only([buy])


# ---------------------------------------------------------------- strategy positions


def test_position_coins_are_sold_with_linked_position_id(cfg, markets, books):
    pos = position("p_S3_BTCUSDT_1", "0.0001")
    # the position's server stop locks its coins
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0", "0.0001")], [pos])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)
    assert it.symbol == "BTCTMN"  # sold on the TMN book although the position was opened on BTCUSDT
    assert it.linked_position_id == pos.id
    assert it.qty == D("0.0001")
    assert it.features["position_id"] == pos.id
    assert "stop order is canceled right before this sell" in it.reason


def test_coins_held_by_no_position_are_sold_first(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    pos = position("p1", "0.0001")
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.0002", "0.0001")], [pos])
    (first,) = fm.plan(ctx, [], phase=1, books=books)
    assert first.linked_position_id is None and first.qty == D("0.0002")

    # after that sale only the position's (locked) coins are left -> linked sell next round
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0", "0.0001")], [pos])
    (second,) = fm.plan(ctx, [], phase=1, books=books)
    assert second.linked_position_id == "p1" and second.qty == D("0.0001")


def test_last_position_takes_leftover_dust_with_it(cfg, markets, books):
    pos = position("p1", "0.0001")
    # 0.000001 free BTC (~22,000 TMN) is dust on its own: it rides along with the position's sell
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0.000001", "0.0001")], [pos])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)
    assert it.linked_position_id == "p1" and it.qty == D("0.000101")


def test_parts_below_minimum_are_sold_together(cfg, markets, books):
    # two positions of ~44,000 TMN each: neither reaches 50,000 TMN alone, together they do
    p1, p2 = position("p1", "0.000002", entry_ts=T0 - 900), position("p2", "0.000002", entry_ts=T0 - 600)
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0", "0.000004")], [p1, p2])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books)
    assert it.qty == D("0.000004") and it.linked_position_id == "p1"
    assert it.features["unit"] == "all"


def test_kill_trigger_closes_positions_with_exit_reason_kill(cfg, markets, books):
    pos = position("p1", "0.0001")
    ctx = ctx_for(cfg, markets, books, [bal("BTC", "0", "0.0001")], [pos])
    (it,) = FlattenManager(cfg, markets).plan(ctx, [], phase=1, books=books, trigger="kill")
    assert it.features["exit_reason"] == "kill" and it.taker is False
    assert it.reason.startswith("Kill-switch flatten")


# ---------------------------------------------------------------- final state / completion


def test_final_state_lists_what_is_not_tmn_yet(cfg, markets, books):
    fm = FlattenManager(cfg, markets)
    balances = [bal("TMN", "1000000"), bal("BTC", "0.0001"), bal("USDT", "0.1"), bal("FOO", "3")]
    ctx = ctx_for(cfg, markets, books, balances)
    state = fm.final_state(ctx)

    assert state["ts"] == T0
    assert set(state["balances"]) == {"BTC", "FOO", "TMN", "USDT"}
    assert state["balances"]["BTC"] == {"free": "0.0001", "locked": "0", "total": "0.0001"}
    assert state["equity_tmn"] == str(valuate(ctx.portfolio.balances, books).equity_tmn)
    assert state["missing_prices"] == ["FOO"]
    # BTC is sellable; FOO has no market and an unknown value -> reported as left over (alert)
    assert [e["asset"] for e in state["non_tmn_over_min"]] == ["BTC", "FOO"]
    assert state["non_tmn_over_min"][0]["market"] == "BTCTMN"
    assert [e["asset"] for e in state["dust"]] == ["USDT"]
    assert state["complete"] is False
    assert fm.is_complete(ctx) is False


def test_is_complete_with_only_tmn(cfg, markets, books):
    ctx = ctx_for(cfg, markets, books, [bal("TMN", "5100000")])
    fm = FlattenManager(cfg, markets)
    assert fm.is_complete(ctx)
    assert fm.final_state(ctx)["tmn_share"] == "1.000000"


# ---------------------------------------------------------------- phase transitions & helpers


def test_phase_transitions_move_forward_only():
    dl = Deadline(END, END - 7200, END - 1800, END - 300)
    assert flatten_phase_transitions(dl, END - 8000, 0) == []
    assert flatten_phase_transitions(dl, END - 7000, 0) == [(1, "flatten_start")]
    # restart late in the run: every missed boundary, in order
    assert flatten_phase_transitions(dl, END - 1000, 0) == [(1, "flatten_start"), (2, "flatten_final")]
    assert flatten_phase_transitions(dl, END - 1000, 2) == []
    assert flatten_phase_transitions(dl, END + 1, 2) == [(3, "challenge_end")]
    assert FlattenManager.flatten_phase_transitions(dl, END - 1000, 1) == [(2, "flatten_final")]
    assert flatten_phase_transitions(Deadline(None, None, None, None), END, 0) == []


def test_cancel_at_flatten_start_targets_entries_and_rebalances_only():
    assert cancel_at_flatten_start(order("BTCUSDT", "BUY", "0.001", "entry", strategy="S1"))
    assert cancel_at_flatten_start(order("USDTTMN", "BUY", "5", "rebalance", strategy="S2"))
    assert not cancel_at_flatten_start(order("BTCUSDT", "SELL", "0.001", "stop", otype="STOP_LIMIT"))
    assert not cancel_at_flatten_start(order("BTCUSDT", "SELL", "0.001", "exit", strategy="S1"))

    class Managed:  # ManagedOrder-shaped
        def __init__(self, o: Order) -> None:
            self.order = o

    assert cancel_at_flatten_start(Managed(order("BTCUSDT", "BUY", "0.001", "entry", strategy="S1")))


def test_markets_needed_covers_targets_and_usdttmn(cfg, markets):
    fm = FlattenManager(cfg, markets)
    needed = fm.markets_needed({"BTC": bal("BTC", "1"), "SOL": bal("SOL", "0"), "TMN": bal("TMN", "5")})
    assert needed == ["BTCTMN", "USDTTMN"]
