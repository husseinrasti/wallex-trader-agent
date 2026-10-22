"""RiskManager (PRD-02 §3, §6): every check code, adjust vs veto, kill/pause/deadline semantics,
rolling-state events, and a seeded property test over random intents and portfolios.

Fixture portfolio (equity ~ 4.84M TMN): 2,000,000 TMN + 9 USDT (~49.6 %) + 0.00002 BTC (~9.1 %).
The default intent — an S3 maker BUY of 0.00004 BTCUSDT at the best bid — passes every check, so
each test changes exactly one thing and asserts the one check it is about.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from wta.config import Config
from wta.execution.positions import PortfolioState, asset_price_tmn, quote_to_tmn
from wta.models import (
    D0,
    Balance,
    BookSnapshot,
    Intent,
    MarketInfo,
    Order,
    OrderRequest,
    Position,
    RiskCheck,
    RiskDecision,
)
from wta.risk.context import Deadline, EngineFlags, RecentIntent, RiskContext
from wta.risk.manager import (
    CHECK_CODES,
    KILL_ALLOWED,
    RiskEvent,
    RiskManager,
    increases_exposure,
    is_entry_like,
    is_taker,
    lockin_leg_qtys,
    min_notional,
    ref_price,
    round_trip_cost,
)
from wta.utils.decimalutil import D, round_qty
from wta.utils.timeutil import day_start

T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z, same as tests/conftest.py
NO_DEADLINE = Deadline(None, None, None, None)

BOOKS: dict[str, tuple[str, str, str]] = {  # symbol -> (bid, ask, qty per level)
    "USDTTMN": ("266700", "266760", "1000"),
    "BTCTMN": ("22000000000", "22020000000", "0.05"),
    "BTCUSDT": ("82500.00", "82520.00", "0.05"),
    "ETHTMN": ("650000000", "650200000", "1"),
    "ETHUSDT": ("2440.00", "2441.00", "1"),
    "SOLTMN": ("28000000", "28100000", "10"),
}
BALANCES = {"TMN": "2000000", "USDT": "9", "BTC": "0.00002"}


# ---------------------------------------------------------------- builders


@dataclass
class World:
    markets: dict[str, MarketInfo]
    cfg: Config
    make_book: Any

    def books(self, ts: float = T0, **overrides: Any) -> dict[str, BookSnapshot]:
        """Default books at ``ts``; ``SYMBOL=None`` drops a book, ``SYMBOL=BookSnapshot`` replaces it."""
        out = {s: self.make_book(s, b, a, qty=q, ts=ts) for s, (b, a, q) in BOOKS.items()}
        for sym, value in overrides.items():
            if value is None:
                out.pop(sym, None)
            else:
                out[sym] = value
        return out

    def ctx(
        self,
        balances: dict[str, Any] | None = None,
        books: dict[str, BookSnapshot] | None = None,
        now: float = T0,
        flags: EngineFlags | None = None,
        deadline: Deadline = NO_DEADLINE,
        positions: list[Position] | None = None,
        open_orders: list[Order] | None = None,
        recent: list[RecentIntent] | None = None,
        api_errors: int = 0,
        placements: list[float] | None = None,
        fees_paid: Decimal = D0,
        markets: dict[str, MarketInfo] | None = None,
    ) -> RiskContext:
        port = PortfolioState(initial_equity_tmn=D("5000000"))
        bal = balances if balances is not None else BALANCES
        port.set_balances(
            {a: v if isinstance(v, Balance) else Balance(asset=a, free=D(v)) for a, v in bal.items()}
        )
        books = books if books is not None else self.books(ts=now)
        port.refresh(books, now)
        port.positions = {p.id: p for p in positions or []}
        port.open_orders = {o.client_id: o for o in open_orders or []}
        port.placements = sorted(placements or [])
        port.fees_paid_tmn_total = fees_paid
        return RiskContext(
            now=now,
            portfolio=port,
            books=books,
            markets=markets if markets is not None else self.markets,
            cfg=self.cfg,
            flags=flags or EngineFlags(),
            deadline=deadline,
            recent_intents=recent or [],
            api_consecutive_errors=api_errors,
        )


@pytest.fixture
def world(markets: dict[str, MarketInfo], cfg: Config, make_book: Any, t0: float) -> World:
    assert t0 == T0
    return World(markets, cfg, make_book)


@pytest.fixture
def rm(cfg: Config) -> RiskManager:
    return RiskManager(cfg)


def entry(**kw: Any) -> Intent:
    """S3 maker BUY 0.00004 BTCUSDT at the best bid (3.3 USDT ~ 880k TMN): passes every check."""
    base: dict[str, Any] = {
        "id": "d_1",
        "ts": T0,
        "strategy": "S3",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "qty": D("0.00004"),
        "price": D("82500.00"),
        "purpose": "entry",
        "reason": "test entry",
        "expected_edge_pct": D("0.02"),
        "features": {"books_used": ["BTCUSDT", "USDTTMN"], "cost_model": "roundtrip", "margin_min_pct": "0"},
        "taker": False,
    }
    base.update(kw)
    return Intent(**base)


def exit_(**kw: Any) -> Intent:
    """SELL of the 0.00002 BTC we hold (1.65 USDT), priced from the book."""
    base: dict[str, Any] = {
        "side": "SELL",
        "purpose": "exit",
        "qty": D("0.00002"),
        "price": None,
        "expected_edge_pct": None,
        "features": {"books_used": ["BTCUSDT"]},
        "reason": "test exit",
    }
    base.update(kw)
    return entry(**base)


def order(
    cid: str,
    symbol: str = "BTCUSDT",
    side: str = "SELL",
    price: str | None = "83000.00",
    purpose: str = "exit",
    strategy: str = "S3",
    otype: str = "LIMIT",
    stop: str | None = None,
    decision_id: str = "d_old",
    qty: str = "0.00002",
) -> Order:
    req = OrderRequest(
        client_id=cid,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type=otype,  # type: ignore[arg-type]
        qty=D(qty),
        price=D(price) if price is not None else None,
        stop_price=D(stop) if stop is not None else None,
        strategy=strategy,
        purpose=purpose,  # type: ignore[arg-type]
        decision_id=decision_id,
    )
    return Order(req=req, created_ts=T0 - 10, updated_ts=T0 - 10)


def position(pid: str, strategy: str = "S3", symbol: str = "BTCUSDT", **kw: Any) -> Position:
    base, quote = (symbol[:-4], "USDT") if symbol.endswith("USDT") else (symbol[:-3], "TMN")
    return Position(
        id=pid,
        strategy=strategy,
        symbol=symbol,
        base=base,
        quote=quote,
        qty=D("0.00002"),
        entry_price=D("82000"),
        entry_ts=T0 - 600,
        meta={"entry_decision_id": f"entry_{pid}"},
        **kw,
    )


def chk(decision: RiskDecision, code: str) -> RiskCheck:
    """The (first) check with ``code``."""
    return next(c for c in decision.checks if c.code == code)


def apply(flags: EngineFlags, events: list[RiskEvent]) -> None:
    """What the engine does with RiskEvents."""
    for e in events:
        if e.kind == "kill":
            flags.killed, flags.kill_reason = True, e.reason
        elif e.kind == "pause":
            flags.paused, flags.pause_reason, flags.paused_until_ts = True, e.reason, e.until_ts
        else:
            flags.paused, flags.pause_reason, flags.paused_until_ts = False, "", None


# ---------------------------------------------------------------- basics


def test_default_entry_is_approved_with_the_full_checklist(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(), world.ctx())
    assert d.approved, d.veto_reason
    assert [c.code for c in d.checks] == list(CHECK_CODES)
    assert d.veto_reason is None and d.failed_codes == []
    assert d.adjusted_qty == D("0.000040")
    assert all(c.detail for c in d.checks)


def test_not_applicable_checks_are_logged_as_passed_na(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(exit_(), world.ctx())
    assert d.approved, d.veto_reason
    for code in ("MAX_ORDER_PCT", "COIN_EXPOSURE", "USDT_BOUNDS", "MAX_POSITIONS", "RATE_HOUR", "FEE_BUDGET"):
        c = chk(d, code)
        assert c.passed and c.detail.startswith("n/a ("), (code, c.detail)


def test_veto_reason_is_the_first_failed_check(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(flags=EngineFlags(killed=True, kill_reason="drawdown"))
    d = rm.evaluate(entry(symbol="ATMN"), ctx)
    assert d.failed_codes[:2] == ["KILLED", "BANNED_MARKET"]
    assert d.veto_reason is not None and d.veto_reason.startswith("KILLED: kill-switch active (drawdown)")


def test_evaluate_is_pure_and_deterministic(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(open_orders=[order("S3_X")], positions=[position("p1")])
    before = (ctx.portfolio.to_state(), dict(ctx.portfolio.balances), ctx.flags.to_state(), len(ctx.books))
    first = rm.evaluate(entry(qty=D("0.001")), ctx)
    second = rm.evaluate(entry(qty=D("0.001")), ctx)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    after = (ctx.portfolio.to_state(), dict(ctx.portfolio.balances), ctx.flags.to_state(), len(ctx.books))
    assert before == after


def test_helpers(world: World) -> None:
    btc = world.markets["BTCUSDT"]
    assert round_trip_cost(btc, entry_taker=False) == D("0.0023")  # 0.10 % maker + 0.13 % taker
    assert round_trip_cost(btc, entry_taker=True) == D("0.0026")
    book = world.books()["BTCUSDT"]
    assert ref_price(entry(), book) == D("82500.00")  # the limit price
    assert ref_price(entry(price=None), book) == D("82520.00")  # BUY crosses at the ask
    assert ref_price(exit_(), book) == D("82500.00")  # SELL crosses at the bid
    assert ref_price(entry(price=None), None) is None
    assert is_taker(entry(order_type="MARKET")) and is_taker(entry(taker=True)) and not is_taker(entry())
    assert is_entry_like("entry") and is_entry_like("rebalance") and is_entry_like("probe")
    assert not any(is_entry_like(p) for p in ("exit", "stop", "leg2", "flatten"))


# ---------------------------------------------------------------- KILLED / PAUSED / DEADLINE


@pytest.mark.parametrize("purpose", ["entry", "rebalance", "exit", "stop", "leg2", "flatten", "probe"])
def test_kill_blocks_everything_but_exit_stop_flatten(world: World, rm: RiskManager, purpose: str) -> None:
    ctx = world.ctx(flags=EngineFlags(killed=True, kill_reason="drawdown 5.2%"))
    d = rm.evaluate(exit_(purpose=purpose), ctx)
    if purpose in KILL_ALLOWED:
        assert d.approved, d.veto_reason
        assert chk(d, "KILLED").passed
    else:
        assert not d.approved
        assert d.failed_codes[0] == "KILLED"
        assert "only flatten/exit/stop" in chk(d, "KILLED").detail


def test_paused_rejects_entries_but_allows_exits(world: World, rm: RiskManager) -> None:
    flags = EngineFlags(paused=True, pause_reason="anomaly: no USDTTMN book")
    for purpose in ("entry", "rebalance"):
        d = rm.evaluate(entry(purpose=purpose), world.ctx(flags=flags))
        assert d.failed_codes == ["PAUSED"], d.veto_reason
        assert "anomaly: no USDTTMN book" in chk(d, "PAUSED").detail
    for purpose in ("exit", "stop", "leg2", "flatten"):
        d = rm.evaluate(exit_(purpose=purpose), world.ctx(flags=flags))
        assert d.approved, (purpose, d.veto_reason)


def _deadline(end: float, cfg: Config) -> Deadline:
    e = cfg.engine
    return Deadline(
        end,
        end - e.flatten_start_before_end_s,
        end - e.flatten_final_before_end_s,
        end - e.final_state_before_end_s,
    )


@pytest.mark.parametrize(
    ("end_offset", "allowed"),
    [
        (86400, {"entry", "rebalance", "exit", "stop", "leg2", "flatten"}),  # phase 0
        (3600, {"exit", "stop", "leg2", "flatten"}),  # phase 1: T-1h (after flatten_start)
        (600, {"flatten"}),  # phase 2: T-10min (after flatten_final)
        (-1, set()),  # phase 3: over
    ],
)
def test_deadline_phases(world: World, rm: RiskManager, end_offset: float, allowed: set[str]) -> None:
    ctx = world.ctx(deadline=_deadline(T0 + end_offset, world.cfg))
    for purpose in ("entry", "rebalance", "exit", "stop", "leg2", "flatten"):
        intent = entry(purpose=purpose) if purpose in ("entry", "rebalance") else exit_(purpose=purpose)
        c = chk(rm.evaluate(intent, ctx), "DEADLINE")
        assert c.passed == (purpose in allowed), (end_offset, purpose, c.detail)


def test_deadline_phase_forced_by_engine_flags(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(), world.ctx(flags=EngineFlags(flatten_phase=1)))
    assert d.failed_codes == ["DEADLINE"]


# ---------------------------------------------------------------- market checks


def test_banned_and_unknown_markets(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(symbol="ATMN", features={}), world.ctx())
    assert not d.approved and "ban list" in chk(d, "BANNED_MARKET").detail
    assert [c.code for c in d.checks] == list(CHECK_CODES)  # still a full checklist, no crash
    d = rm.evaluate(entry(symbol="FOOTMN", features={}), world.ctx())
    assert d.failed_codes[0] == "BANNED_MARKET" and "not a known market" in d.veto_reason  # type: ignore[operator]
    assert d.adjusted_qty is None


def test_market_order_disabled(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(
        entry(symbol="SOLTMN", order_type="MARKET", price=None, qty=D("0.1"), features={}), world.ctx()
    )
    c = chk(d, "MARKET_ORDER_DISABLED")
    assert not c.passed and "crossing" in c.detail
    assert chk(
        rm.evaluate(entry(order_type="MARKET", price=None), world.ctx()), "MARKET_ORDER_DISABLED"
    ).passed
    assert chk(rm.evaluate(entry(), world.ctx()), "MARKET_ORDER_DISABLED").detail.startswith("n/a")


def test_min_notional_usdt_and_tmn(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(qty=D("0.00001")), world.ctx())  # 0.825 USDT < 1 USDT
    assert d.failed_codes == ["MIN_NOTIONAL"] and "< min 1.0000 USDT" in d.veto_reason  # type: ignore[operator]
    d = rm.evaluate(
        entry(symbol="BTCTMN", price=D("22000000000"), qty=D("0.000002"), features={}), world.ctx()
    )
    assert d.failed_codes == ["MIN_NOTIONAL"] and "< min 50,000 TMN" in d.veto_reason  # type: ignore[operator]


def test_min_notional_falls_back_to_config_and_checks_min_qty(world: World, rm: RiskManager) -> None:
    mk = dict(world.markets)
    mk["BTCUSDT"] = mk["BTCUSDT"].model_copy(update={"min_notional": D0, "min_qty": D("0.00003")})
    assert min_notional(mk["BTCUSDT"], world.cfg) == world.cfg.risk.min_notional_usdt
    d = rm.evaluate(entry(qty=D("0.00002")), world.ctx(markets=mk))
    assert d.failed_codes == ["MIN_NOTIONAL"] and "min qty" in d.veto_reason  # type: ignore[operator]


def test_min_notional_cannot_size(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(qty=None, notional=None), world.ctx())
    assert d.failed_codes == ["MIN_NOTIONAL"] and "neither qty nor notional" in d.veto_reason  # type: ignore[operator]
    books = world.books(BTCUSDT=None)
    d = rm.evaluate(entry(price=None), world.ctx(books=books))
    assert "MIN_NOTIONAL" in d.failed_codes and "no reference price" in chk(d, "MIN_NOTIONAL").detail


def test_notional_is_converted_with_the_reference_price(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(qty=None, notional=D("3.3"), price=None), world.ctx())  # 3.3 USDT at ask 82,520
    assert d.approved and d.adjusted_qty == round_qty(world.markets["BTCUSDT"], D("3.3") / D("82520.00"))


# ---------------------------------------------------------------- adjust checks


def test_max_order_pct_adjusts_entries_down(world: World, rm: RiskManager) -> None:
    ctx = world.ctx()
    equity = ctx.portfolio.valuation(ctx.books).equity_tmn
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), qty=D("0.0001"), features={}), ctx)
    assert d.approved, d.veto_reason
    c = chk(d, "MAX_ORDER_PCT")
    assert c.passed and c.detail.startswith("adjusted qty 0.000100 -> 0.000055")
    assert d.adjusted_qty == D("0.000055")
    assert d.adjusted_qty * D("22000000000") <= D("0.25") * equity


def test_max_order_pct_does_not_cap_exits(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "BTC": "0.001"})  # 22M TMN of BTC to sell
    d = rm.evaluate(exit_(symbol="BTCTMN", qty=D("0.001"), features={}), ctx)
    assert d.approved and d.adjusted_qty == D("0.001000")
    assert chk(d, "MAX_ORDER_PCT").detail == "n/a (exit)"


def test_best_level_qty_caps_taker_orders(world: World, rm: RiskManager) -> None:
    books = world.books(BTCTMN=world.make_book("BTCTMN", "22000000000", "22020000000", qty="0.00004"))
    intent = entry(symbol="BTCTMN", price=None, taker=True, features={})
    d = rm.evaluate(intent, world.ctx(books=books))
    assert d.approved, d.veto_reason
    assert chk(d, "BEST_LEVEL_QTY").detail.startswith("adjusted qty 0.000040 -> 0.000032")
    assert d.adjusted_qty == D("0.000032")
    # maker orders are not capped by the best level
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), features={}), world.ctx(books=books))
    assert chk(d, "BEST_LEVEL_QTY").detail == "n/a (maker order)" and d.adjusted_qty == D("0.000040")


def test_best_level_qty_uses_the_bid_for_sells_and_fails_closed_for_entries(
    world: World, rm: RiskManager
) -> None:
    thin_bid = world.make_book("BTCUSDT", "82500.00", "82520.00", qty="0.00002")
    d = rm.evaluate(exit_(taker=True), world.ctx(books=world.books(BTCUSDT=thin_bid)))
    assert d.approved and d.adjusted_qty == D("0.000016")  # 80 % of the 0.00002 best bid
    one_sided = world.make_book("BTCUSDT", "82500.00", None)
    d = rm.evaluate(entry(taker=True), world.ctx(books=world.books(BTCUSDT=one_sided)))
    assert not chk(d, "BEST_LEVEL_QTY").passed
    no_bid = world.make_book("BTCUSDT", None, "82520.00")
    d = rm.evaluate(exit_(taker=True, price=D("82500.00")), world.ctx(books=world.books(BTCUSDT=no_bid)))
    assert chk(d, "BEST_LEVEL_QTY").passed  # a risk-reducing order is not blocked by a missing level


def test_min_notional_after_adjustments(world: World, rm: RiskManager) -> None:
    thin = world.make_book("BTCUSDT", "82500.00", "82520.00", qty="0.00001")
    d = rm.evaluate(entry(price=None, taker=True), world.ctx(books=world.books(BTCUSDT=thin)))
    assert not d.approved
    assert chk(d, "BEST_LEVEL_QTY").passed  # the adjust check itself passes ...
    last = d.checks[-1]  # ... and the post-adjustment size check vetoes
    assert last.code == "MIN_NOTIONAL" and not last.passed and last.detail.startswith("after adjustments:")
    assert d.veto_reason is not None and d.veto_reason.startswith("MIN_NOTIONAL: after adjustments")


def test_coin_exposure_adjusts_down(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "BTC": "0.00007"})  # coins 1.54M of 5.94M
    val = ctx.portfolio.valuation(ctx.books)
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), qty=D("0.00005"), features={}), ctx)
    assert d.approved, d.veto_reason
    assert chk(d, "COIN_EXPOSURE").detail.startswith("adjusted qty 0.000050 -> 0.000038")
    assert val.coins_tmn_value + d.adjusted_qty * D("22000000000") <= D("0.40") * val.equity_tmn  # type: ignore[operator]


def test_coin_exposure_rejects_when_room_is_below_min_notional(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "BTC": "0.00013"})  # coins ~39.4 % of equity
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), qty=D("0.00002"), features={}), ctx)
    assert d.failed_codes == ["COIN_EXPOSURE"] and "below the minimum order" in d.veto_reason  # type: ignore[operator]


def test_coin_exposure_ignores_sells_and_usdt_buys(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "BTC": "0.00013"})
    assert chk(rm.evaluate(exit_(), ctx), "COIN_EXPOSURE").detail.startswith("n/a")
    usdt_buy = entry(symbol="USDTTMN", price=D("266700"), qty=D("1"), purpose="rebalance", features={})
    assert chk(rm.evaluate(usdt_buy, ctx), "COIN_EXPOSURE").detail == "n/a (not a coin purchase)"


def _usdt(qty: str, side: str = "BUY", price: str | None = None, purpose: str = "rebalance") -> Intent:
    px = price or ("266700" if side == "BUY" else "266760")
    return entry(
        strategy="S2",
        symbol="USDTTMN",
        side=side,
        qty=D(qty),
        price=D(px),
        purpose=purpose,
        expected_edge_pct=None,
        features={"books_used": ["USDTTMN"]},
    )


def test_usdt_bounds_allows_moves_toward_the_band(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={"TMN": "5000000"})  # 0 % USDT, below the 25 % floor
    d = rm.evaluate(_usdt("4.5"), ctx)  # -> 24 %: still below, but closer
    assert d.approved, d.veto_reason
    assert "moves toward the band" in chk(d, "USDT_BOUNDS").detail


def test_usdt_bounds_rejects_moves_further_away(world: World, rm: RiskManager) -> None:
    below = world.ctx(balances={"TMN": "4800000", "USDT": "0.5"})  # 2.7 %
    d = rm.evaluate(_usdt("0.2", side="SELL"), below)
    assert d.failed_codes == ["USDT_BOUNDS"] and "further away" in d.veto_reason  # type: ignore[operator]
    above = world.ctx(balances={"TMN": "1000000", "USDT": "15"})  # 80 %
    assert rm.evaluate(_usdt("0.5"), above).failed_codes == ["USDT_BOUNDS"]
    assert rm.evaluate(_usdt("0.5", side="SELL"), above).approved


def test_usdt_bounds_adjusts_so_the_far_bound_is_not_overshot(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={"TMN": "1700000", "USDT": "12"})  # 65.3 %, BUY 4 USDT -> 87 %
    val = ctx.portfolio.valuation(ctx.books)
    d = rm.evaluate(_usdt("4"), ctx)
    assert d.approved, d.veto_reason
    assert chk(d, "USDT_BOUNDS").detail.startswith("adjusted qty 4.00 -> 0.86")
    post = (val.usdt_tmn_value + d.adjusted_qty * D("266700")) / val.equity_tmn  # type: ignore[operator]
    assert post <= D("0.70")


def test_usdt_bounds_counts_usdt_quoted_coin_buys(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "TMN": "3000000"})  # 41 % USDT; buying 4.125 USDT of BTC -> 22.6 %
    d = rm.evaluate(entry(qty=D("0.00005")), ctx)
    assert d.approved, d.veto_reason
    assert chk(d, "USDT_BOUNDS").detail.startswith("adjusted qty 0.000050 -> 0.000042")
    assert chk(rm.evaluate(exit_(), ctx), "USDT_BOUNDS").detail == "n/a (exit)"
    tmn_pair = entry(symbol="BTCTMN", price=D("22000000000"), features={})
    assert chk(rm.evaluate(tmn_pair, ctx), "USDT_BOUNDS").detail == "n/a (does not change the USDT holding)"


# ---------------------------------------------------------------- portfolio limits


def test_max_positions_global_and_per_strategy(world: World, rm: RiskManager) -> None:
    three = [position("p1", "S1"), position("p2", "S1", "ETHUSDT"), position("p3", "S2", "USDTTMN")]
    d = rm.evaluate(entry(strategy="S9"), world.ctx(positions=three))
    assert d.failed_codes == ["MAX_POSITIONS"] and ">= max 3" in d.veto_reason  # type: ignore[operator]
    d = rm.evaluate(entry(strategy="S3"), world.ctx(positions=[position("p1", "S3", "ETHUSDT")]))
    assert d.failed_codes == ["MAX_POSITIONS"] and "its cap 1" in d.veto_reason  # type: ignore[operator]
    d = rm.evaluate(
        entry(strategy="S1", symbol="ETHUSDT", price=D("2440.00"), qty=D("0.001")),
        world.ctx(positions=[position("p1", "S1", "BTCTMN")]),
    )
    assert chk(d, "MAX_POSITIONS").passed


def test_max_positions_counts_pending_entry_orders(world: World, rm: RiskManager) -> None:
    pending = [order("S3_A", "ETHUSDT", "BUY", "2400.00", purpose="entry", decision_id="d_pending")]
    d = rm.evaluate(entry(), world.ctx(open_orders=pending))
    assert "MAX_POSITIONS" in d.failed_codes and "pending" in chk(d, "MAX_POSITIONS").detail
    # a working entry order that already opened its position is not counted twice
    pos = position("p1", "S1", "ETHUSDT")
    filling = order(
        "S1_A", "ETHUSDT", "BUY", "2400.00", purpose="entry", strategy="S1", decision_id="entry_p1"
    )
    d = rm.evaluate(entry(strategy="S1"), world.ctx(positions=[pos], open_orders=[filling]))
    assert chk(d, "MAX_POSITIONS").passed, chk(d, "MAX_POSITIONS").detail


def test_max_positions_allows_adding_to_a_position_and_ignores_exits(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(positions=[position("p1", "S3")])
    assert chk(rm.evaluate(entry(linked_position_id="p1"), ctx), "MAX_POSITIONS").passed
    assert chk(rm.evaluate(exit_(linked_position_id="p1"), ctx), "MAX_POSITIONS").detail == "n/a (exit)"


def test_max_open_orders(world: World, rm: RiskManager) -> None:
    eight = [  # exits: they reserve no re-placements, so only MAX_OPEN_ORDERS can fail
        order(f"S2_{i}", "ETHTMN", "SELL", "700000000", purpose="exit", strategy=f"X{i}")
        for i in range(8)
    ]
    d = rm.evaluate(entry(), world.ctx(open_orders=eight))
    assert d.failed_codes == ["MAX_OPEN_ORDERS"]
    assert rm.evaluate(exit_(), world.ctx(open_orders=eight)).approved


def test_rate_limits_hour_and_day(world: World, rm: RiskManager) -> None:
    hour = [T0 - 60 * i for i in range(1, 11)]  # 10 placements in the last hour
    d = rm.evaluate(entry(), world.ctx(placements=hour))
    assert d.failed_codes == ["RATE_HOUR"]
    day = [T0 - 7200 + i for i in range(60)]  # 60 today (08:00 UTC), none in the last hour
    d = rm.evaluate(entry(), world.ctx(placements=day))
    assert d.failed_codes == ["RATE_DAY"]
    yesterday = [T0 - 86400 + i for i in range(60)]
    assert rm.evaluate(entry(), world.ctx(placements=yesterday)).approved
    d = rm.evaluate(exit_(), world.ctx(placements=hour + day))
    assert d.approved and "exempt" in chk(d, "RATE_HOUR").detail


def test_daily_loss_blocks_entries(world: World, rm: RiskManager) -> None:
    ctx = world.ctx()
    ctx.portfolio.day_start_equity_tmn = ctx.portfolio.equity_tmn / D("0.97")  # type: ignore[operator]
    d = rm.evaluate(entry(), ctx)
    assert d.failed_codes == ["DAILY_LOSS"] and "-3.00%" in d.veto_reason  # type: ignore[operator]
    assert rm.evaluate(exit_(), ctx).approved
    ctx.portfolio.day_start_equity_tmn = ctx.portfolio.equity_tmn / D("0.98")  # type: ignore[operator]
    assert rm.evaluate(entry(), ctx).approved


def test_drawdown_blocks_entries(world: World, rm: RiskManager) -> None:
    ctx = world.ctx()
    ctx.portfolio.peak_equity_tmn = ctx.portfolio.equity_tmn / D("0.94")  # type: ignore[operator]
    d = rm.evaluate(entry(), ctx)
    assert d.failed_codes == ["DRAWDOWN_KILL"]
    assert rm.evaluate(exit_(), ctx).approved


# ---------------------------------------------------------------- fees and edge


def test_fee_budget(world: World, rm: RiskManager) -> None:
    # budget = 2 % x 5,000,000 = 100,000 TMN; the default entry's maker fee is ~880 TMN
    assert rm.evaluate(entry(), world.ctx(fees_paid=D("99000"))).approved
    d = rm.evaluate(entry(), world.ctx(fees_paid=D("99500")))
    assert d.failed_codes == ["FEE_BUDGET"] and "> budget 100,000 TMN" in d.veto_reason  # type: ignore[operator]
    rebalance = _usdt("1")
    assert rm.evaluate(rebalance, world.ctx(fees_paid=D("99999"))).failed_codes == ["FEE_BUDGET"]
    assert rm.evaluate(exit_(), world.ctx(fees_paid=D("100000"))).approved  # exits never blocked by fees


def test_fee_budget_counts_the_lockin_leg2_taker_fee(world: World, rm: RiskManager) -> None:
    lockin = entry(
        strategy="S1",
        symbol="BTCTMN",
        price=D("22020000000"),
        taker=True,
        expected_edge_pct=D("0.01"),
        features={"cost_model": "lockin", "leg2_symbol": "BTCUSDT", "books_used": ["BTCTMN", "BTCUSDT"]},
    )
    notional = D("0.00004") * D("22020000000")
    fee_taker_only = notional * D("0.003")
    ctx = world.ctx(fees_paid=D("100000") - fee_taker_only - 1)  # fits without leg 2, not with it
    d = rm.evaluate(lockin, ctx)
    assert d.failed_codes == ["FEE_BUDGET"] and "taker leg 2 on BTCUSDT" in d.veto_reason  # type: ignore[operator]


def _spread(world: World, symbol: str) -> Decimal:
    spread = world.books()[symbol].spread_pct
    assert spread is not None
    return spread


def test_edge_vs_cost_maker_round_trip(world: World, rm: RiskManager) -> None:
    cost = D("0.001") + D("0.0013") + _spread(world, "BTCUSDT")
    assert rm.evaluate(entry(expected_edge_pct=cost), world.ctx()).approved
    d = rm.evaluate(entry(expected_edge_pct=cost - D("0.00001")), world.ctx())
    assert d.failed_codes == ["EDGE_VS_COST"] and "edge too small" in d.veto_reason  # type: ignore[operator]


def test_edge_vs_cost_taker_round_trip_costs_more(world: World, rm: RiskManager) -> None:
    maker_cost = D("0.0023") + _spread(world, "BTCUSDT")
    edge = maker_cost + D("0.0001")  # enough as maker, not as taker (+0.03 %)
    assert rm.evaluate(entry(expected_edge_pct=edge), world.ctx()).approved
    d = rm.evaluate(entry(expected_edge_pct=edge, taker=True, price=D("82520.00")), world.ctx())
    assert d.failed_codes == ["EDGE_VS_COST"] and "taker BTCUSDT" in d.veto_reason  # type: ignore[operator]


def _lockin(edge: str, taker: bool, **features: Any) -> Intent:
    return entry(
        strategy="S1",
        symbol="BTCTMN",
        price=D("22020000000") if taker else D("22000000000"),
        taker=taker,
        expected_edge_pct=D(edge),
        features={
            "cost_model": "lockin",
            "leg2_symbol": "BTCUSDT",
            "margin_min_pct": "0.0015",
            "dev_mid_pct": "-0.004",
            "books_used": ["BTCTMN", "BTCUSDT", "USDTTMN"],
            **features,
        },
    )


def test_edge_vs_cost_lockin_taker_and_maker(world: World, rm: RiskManager) -> None:
    # taker: 0.30 % TMN taker + 0.13 % USDT taker + 0.15 % margin = 0.58 %
    assert rm.evaluate(_lockin("0.0058", taker=True), world.ctx()).approved
    assert rm.evaluate(_lockin("0.0057", taker=True), world.ctx()).failed_codes == ["EDGE_VS_COST"]
    # maker leg 1: 0.25 % + 0.13 % + 0.15 % = 0.53 %
    assert rm.evaluate(_lockin("0.0053", taker=False), world.ctx()).approved
    assert rm.evaluate(_lockin("0.0052", taker=False), world.ctx()).failed_codes == ["EDGE_VS_COST"]


def test_edge_vs_cost_margin_is_the_larger_of_feature_and_s1_config(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(_lockin("0.0058", taker=True, margin_min_pct="0.003"), world.ctx())
    assert d.failed_codes == ["EDGE_VS_COST"]  # needs 0.43 % + 0.30 % = 0.73 %
    d = rm.evaluate(_lockin("0.0058", taker=True, margin_min_pct="0"), world.ctx())
    assert d.approved  # S1's configured 0.15 % still applies


def test_edge_vs_cost_requires_an_edge_and_skips_rebalances(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(expected_edge_pct=None), world.ctx())
    assert d.failed_codes == ["EDGE_VS_COST"]
    d = rm.evaluate(_lockin("0.01", taker=True, leg2_symbol=None), world.ctx())
    assert "EDGE_VS_COST" in d.failed_codes and "leg2_symbol" in chk(d, "EDGE_VS_COST").detail
    assert (
        chk(rm.evaluate(_usdt("1"), world.ctx()), "EDGE_VS_COST").detail
        == "n/a (rebalance: no edge required)"
    )


# ---------------------------------------------------------------- data quality


def test_stale_data_on_the_traded_book_and_usdttmn(world: World, rm: RiskManager) -> None:
    old = world.make_book("BTCUSDT", "82500.00", "82520.00", qty="0.05", ts=T0 - 31)
    d = rm.evaluate(entry(), world.ctx(books=world.books(BTCUSDT=old)))
    assert d.failed_codes == ["STALE_DATA"] and "BTCUSDT: 31.0s old" in d.veto_reason  # type: ignore[operator]
    old_u = world.make_book("USDTTMN", "266700", "266760", qty="1000", ts=T0 - 45)
    d = rm.evaluate(entry(features={}), world.ctx(books=world.books(USDTTMN=old_u)))
    assert d.failed_codes == ["STALE_DATA"]  # USDT quote -> USDTTMN required even if not listed


def test_stale_data_covers_books_used_and_s1(world: World, rm: RiskManager) -> None:
    books = world.books(ETHUSDT=None)
    d = rm.evaluate(entry(features={"books_used": ["BTCUSDT", "ETHUSDT"]}), world.ctx(books=books))
    assert d.failed_codes == ["STALE_DATA"] and "ETHUSDT: no book" in d.veto_reason  # type: ignore[operator]
    old_u = world.make_book("USDTTMN", "266700", "266760", qty="1000", ts=T0 - 45)
    s1 = entry(
        strategy="S1", symbol="BTCTMN", price=D("22000000000"), features={}, expected_edge_pct=D("0.02")
    )
    assert "STALE_DATA" in rm.evaluate(s1, world.ctx(books=world.books(USDTTMN=old_u))).failed_codes


def test_stale_data_never_blocks_stops_and_flatten(world: World, rm: RiskManager) -> None:
    old = world.make_book("BTCUSDT", "82500.00", "82520.00", qty="0.05", ts=T0 - 300)
    books = world.books(BTCUSDT=old)
    for purpose in ("stop", "flatten"):
        d = rm.evaluate(exit_(purpose=purpose), world.ctx(books=books))
        assert d.approved, (purpose, d.veto_reason)
    for purpose in ("exit", "leg2"):
        assert rm.evaluate(exit_(purpose=purpose), world.ctx(books=books)).failed_codes == ["STALE_DATA"]


def test_price_sanity_band(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(entry(price=D("86700.00")), world.ctx())  # +5.08 % from mid 82,510
    assert d.failed_codes == ["PRICE_SANITY"] and "band +-3.0%" in d.veto_reason  # type: ignore[operator]
    assert rm.evaluate(entry(price=D("84900.00")), world.ctx()).approved  # +2.9 %
    d = rm.evaluate(exit_(price=D("78000.00")), world.ctx())  # a fat-finger exit is rejected too
    assert d.failed_codes == ["PRICE_SANITY"]
    d = rm.evaluate(entry(order_type="MARKET", price=None), world.ctx())
    assert chk(d, "PRICE_SANITY").passed and "MARKET" in chk(d, "PRICE_SANITY").detail
    stop = exit_(purpose="stop", order_type="STOP_LIMIT", price=D("70000.00"), stop_price=D("71000.00"))
    assert rm.evaluate(stop, world.ctx()).approved


def test_price_sanity_cross_book_disagreement_is_an_anomaly_for_entries(
    world: World, rm: RiskManager
) -> None:
    d = rm.evaluate(_lockin("0.08", taker=True, dev_mid_pct="0.06"), world.ctx())
    assert d.failed_codes == ["PRICE_SANITY"] and "anomaly" in d.veto_reason  # type: ignore[operator]
    exit_with_dev = exit_(features={"dev_mid_pct": "0.06", "books_used": ["BTCUSDT"]})
    assert rm.evaluate(exit_with_dev, world.ctx()).approved


def test_price_sanity_without_a_book(world: World, rm: RiskManager) -> None:
    books = world.books(ETHUSDT=None)
    d = rm.evaluate(
        entry(symbol="ETHUSDT", price=D("2440.00"), qty=D("0.001"), features={}), world.ctx(books=books)
    )
    assert "PRICE_SANITY" in d.failed_codes  # an entry fails closed without a mid


# ---------------------------------------------------------------- orders & balances


def test_self_cross_buy_against_our_open_sell(world: World, rm: RiskManager) -> None:
    sell = order("S1_SELL", price="82510.00", strategy="S1")
    d = rm.evaluate(entry(price=D("82520.00")), world.ctx(open_orders=[sell]))
    assert d.failed_codes == ["SELF_CROSS"] and "trade with ourselves" in d.veto_reason  # type: ignore[operator]
    assert rm.evaluate(entry(price=D("82500.00")), world.ctx(open_orders=[sell])).approved
    # a MARKET buy is judged at the ask it would cross
    d = rm.evaluate(entry(order_type="MARKET", price=None), world.ctx(open_orders=[sell]))
    assert d.failed_codes == ["SELF_CROSS"]


def test_self_cross_sell_against_our_open_buy(world: World, rm: RiskManager) -> None:
    buy = order("S1_BUY", side="BUY", price="82500.00", purpose="entry", strategy="S1")
    d = rm.evaluate(exit_(price=D("82500.00")), world.ctx(open_orders=[buy]))
    assert d.failed_codes == ["SELF_CROSS"]
    assert rm.evaluate(exit_(price=D("82510.00")), world.ctx(open_orders=[buy])).approved


def test_self_cross_counts_our_stop_orders_at_their_limit_price(world: World, rm: RiskManager) -> None:
    stop = order("S3_STOP", otype="STOP_LIMIT", price="81000.00", stop="81500.00", purpose="stop")
    d = rm.evaluate(entry(strategy="S1"), world.ctx(open_orders=[stop]))
    assert d.failed_codes == ["SELF_CROSS"] and "81000.00" in d.veto_reason  # type: ignore[operator]


def test_balance_adjusts_buys_to_free_quote_minus_buffer(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "TMN": Balance(asset="TMN", free=D("600000"), locked=D("900000"))})
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), qty=D("0.000035"), features={}), ctx)
    assert d.approved, d.veto_reason
    assert chk(d, "BALANCE").detail.startswith("adjusted qty 0.000035 -> 0.000027")
    assert d.adjusted_qty * D("22000000000") * D("1.0025") <= D("600000") * D("0.995")  # type: ignore[operator]


def test_balance_adjusts_sells_to_free_base_without_buffer(world: World, rm: RiskManager) -> None:
    d = rm.evaluate(exit_(symbol="BTCTMN", qty=D("0.00003"), features={}), world.ctx())
    assert d.approved and d.adjusted_qty == D("0.000020")
    assert chk(d, "BALANCE").detail.startswith("adjusted qty 0.000030 -> 0.000020")


def test_balance_rejects_when_nothing_is_left(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={**BALANCES, "BTC": "0.000001"})
    d = rm.evaluate(exit_(symbol="BTCTMN", qty=D("0.00003"), features={}), ctx)
    assert d.failed_codes == ["BALANCE"] and "nothing tradable left" in d.veto_reason  # type: ignore[operator]
    ctx = world.ctx(balances={**BALANCES, "TMN": "0"})
    d = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), features={}), ctx)
    assert "BALANCE" in d.failed_codes


def test_duplicate_open_order_and_recent_intent(world: World, rm: RiskManager) -> None:
    same = order("S3_OPEN", side="BUY", price="82000.00", purpose="entry", decision_id="d_open")
    d = rm.evaluate(entry(), world.ctx(open_orders=[same], positions=[]))
    assert "DUPLICATE" in d.failed_codes and "S3_OPEN" in chk(d, "DUPLICATE").detail
    recent = [RecentIntent(ts=T0 - 30, strategy="S3", symbol="BTCUSDT", side="BUY", purpose="entry")]
    d = rm.evaluate(entry(), world.ctx(recent=recent))
    assert d.failed_codes == ["DUPLICATE"] and "30s ago" in d.veto_reason  # type: ignore[operator]
    old = [RecentIntent(ts=T0 - 61, strategy="S3", symbol="BTCUSDT", side="BUY", purpose="entry")]
    assert rm.evaluate(entry(), world.ctx(recent=old)).approved
    other_side = [RecentIntent(ts=T0 - 30, strategy="S3", symbol="BTCUSDT", side="SELL", purpose="entry")]
    assert rm.evaluate(entry(), world.ctx(recent=other_side)).approved


def test_duplicate_exits(world: World, rm: RiskManager) -> None:
    pos = position("p1", stop_order_client_id="S3_STOP")
    stop = order("S3_STOP", otype="STOP_LIMIT", price="80000.00", stop="80500.00", purpose="stop")
    # recent intents never delay an exit
    recent = [RecentIntent(ts=T0 - 5, strategy="S3", symbol="BTCUSDT", side="SELL", purpose="exit")]
    assert rm.evaluate(exit_(linked_position_id="p1"), world.ctx(recent=recent, positions=[pos])).approved
    # the position's own protective stop does not block its target exit ...
    ctx = world.ctx(positions=[pos], open_orders=[stop])
    assert rm.evaluate(exit_(linked_position_id="p1"), ctx).approved
    # ... nor its stop exit (a stranded STOP_LIMIT: the OrderManager cancels it before selling) ...
    assert rm.evaluate(exit_(purpose="stop", linked_position_id="p1"), ctx).approved
    # ... but a stop order that is not the position's own is a duplicate
    other = order("S3_STOP2", otype="STOP_LIMIT", price="80000.00", stop="80500.00", purpose="stop")
    ctx = world.ctx(positions=[pos], open_orders=[stop, other])
    assert rm.evaluate(exit_(purpose="stop", linked_position_id="p1"), ctx).failed_codes == ["DUPLICATE"]
    # another exit already working for the position blocks a software stop
    working = order("S3_EXIT", price="82600.00", purpose="exit")
    d = rm.evaluate(
        exit_(purpose="stop", linked_position_id="p1"), world.ctx(positions=[pos], open_orders=[working])
    )
    assert d.failed_codes == ["DUPLICATE"] and "already closes position p1" in d.veto_reason  # type: ignore[operator]


def test_duplicate_does_not_block_incremental_leg2(world: World, rm: RiskManager) -> None:
    leg2 = order("S1_LEG2", price="82500.00", purpose="leg2", strategy="S1", qty="0.00001")
    d = rm.evaluate(exit_(strategy="S1", purpose="leg2", qty=D("0.00001")), world.ctx(open_orders=[leg2]))
    assert chk(d, "DUPLICATE").passed, chk(d, "DUPLICATE").detail


# ---------------------------------------------------------------- rolling state (§6)


def test_drawdown_kill_is_emitted_once_and_never_resumed(world: World, rm: RiskManager) -> None:
    ctx = world.ctx()
    ctx.portfolio.peak_equity_tmn = ctx.portfolio.equity_tmn / D("0.95")  # type: ignore[operator]
    events = rm.update_rolling_state(ctx)
    assert [e.kind for e in events] == ["kill"] and events[0].reason.startswith(
        "drawdown_kill: drawdown 5.00%"
    )
    apply(ctx.flags, events)
    assert rm.update_rolling_state(ctx) == []  # not again while killed
    ctx.portfolio.peak_equity_tmn = ctx.portfolio.equity_tmn  # fully recovered
    for _ in range(3):
        ctx.now += 120
        assert rm.update_rolling_state(ctx) == []  # a kill is reset by hand only
    assert ctx.flags.killed


def test_no_events_in_a_healthy_state(world: World, rm: RiskManager) -> None:
    assert rm.update_rolling_state(world.ctx()) == []


def test_daily_loss_pauses_until_the_next_utc_day(world: World, rm: RiskManager) -> None:
    ctx = world.ctx()
    ctx.portfolio.day_start_equity_tmn = ctx.portfolio.equity_tmn / D("0.97")  # type: ignore[operator]
    events = rm.update_rolling_state(ctx)
    midnight = day_start(T0) + 86400
    assert events == [RiskEvent("pause", events[0].reason, until_ts=midnight)]
    assert events[0].reason.startswith("daily_loss: daily PnL -3.00%")
    apply(ctx.flags, events)
    assert rm.update_rolling_state(ctx) == []  # same day: stays paused, no event spam
    assert not rm.evaluate(entry(), ctx).approved
    # next UTC day: the engine's refresh resets the day-start equity -> resume
    later = midnight + 5
    ctx2 = world.ctx(now=later, flags=ctx.flags)
    assert ctx2.portfolio.daily_pnl_pct == 0
    events = rm.update_rolling_state(ctx2)
    assert [e.kind for e in events] == ["resume"]
    apply(ctx2.flags, events)
    assert not ctx2.flags.paused


def test_anomaly_pause_resumes_only_after_the_clear_timer(world: World, rm: RiskManager) -> None:
    flags = EngineFlags()
    events = rm.update_rolling_state(world.ctx(flags=flags, api_errors=5))
    assert [e.kind for e in events] == ["pause"] and "5 consecutive API errors" in events[0].reason
    assert events[0].until_ts is None
    apply(flags, events)
    assert rm.update_rolling_state(world.ctx(flags=flags, api_errors=7)) == []  # still bad: no spam
    assert rm.update_rolling_state(world.ctx(now=T0 + 10, flags=flags)) == []  # clear since T0+10
    assert rm.update_rolling_state(world.ctx(now=T0 + 69, flags=flags)) == []  # 59 s clear
    events = rm.update_rolling_state(world.ctx(now=T0 + 70, flags=flags))  # 60 s clear
    assert [e.kind for e in events] == ["resume"] and "cleared for 60s" in events[0].reason


def test_anomaly_clear_timer_restarts_when_the_condition_returns(world: World, rm: RiskManager) -> None:
    flags = EngineFlags()
    apply(flags, rm.update_rolling_state(world.ctx(flags=flags), anomalies=["cross-book disagreement 6%"]))
    assert flags.paused and "cross-book disagreement 6%" in flags.pause_reason
    assert rm.update_rolling_state(world.ctx(now=T0 + 10, flags=flags)) == []
    assert rm.update_rolling_state(world.ctx(now=T0 + 50, flags=flags, api_errors=9)) == []  # back again
    assert rm.update_rolling_state(world.ctx(now=T0 + 80, flags=flags)) == []  # clear since T0+80
    assert rm.update_rolling_state(world.ctx(now=T0 + 139, flags=flags)) == []
    assert [e.kind for e in rm.update_rolling_state(world.ctx(now=T0 + 140, flags=flags))] == ["resume"]


def test_stale_or_missing_usdttmn_book_pauses(world: World, rm: RiskManager) -> None:
    old_u = world.make_book("USDTTMN", "266700", "266760", qty="1000", ts=T0 - 31)
    events = rm.update_rolling_state(world.ctx(books=world.books(USDTTMN=old_u)))
    assert [e.kind for e in events] == ["pause"] and "USDTTMN book 31.0s old" in events[0].reason
    events = RiskManager(world.cfg).update_rolling_state(world.ctx(books=world.books(USDTTMN=None)))
    assert "no USDTTMN book" in events[0].reason


def test_daily_loss_overrides_an_anomaly_pause_and_hands_back_at_midnight(
    world: World, rm: RiskManager
) -> None:
    flags = EngineFlags()
    apply(flags, rm.update_rolling_state(world.ctx(flags=flags, api_errors=5)))
    ctx = world.ctx(flags=flags, api_errors=5)
    ctx.portfolio.day_start_equity_tmn = ctx.portfolio.equity_tmn / D("0.97")  # type: ignore[operator]
    events = rm.update_rolling_state(ctx)
    assert [e.kind for e in events] == ["pause"] and events[0].reason.startswith("daily_loss")
    apply(flags, events)
    # new UTC day, the API errors persist -> the pause switches back to an anomaly pause
    later = day_start(T0) + 86400 + 1
    events = rm.update_rolling_state(world.ctx(now=later, flags=flags, api_errors=6))
    assert [e.kind for e in events] == ["pause"] and events[0].reason.startswith("anomaly")


def test_foreign_pauses_are_never_resumed(world: World, rm: RiskManager) -> None:
    flags = EngineFlags(paused=True, pause_reason="exception storm (3 crashes in 600 s)")
    for i in range(3):
        assert rm.update_rolling_state(world.ctx(now=T0 + 100 * i, flags=flags)) == []
    assert flags.paused


# ---------------------------------------------------------------- property test


def _random_world_ctx(world: World, rng: random.Random) -> RiskContext:
    now = T0 + rng.randint(0, 3600)
    books: dict[str, BookSnapshot] = {}
    for sym, (bid, _ask, _qty) in BOOKS.items():
        drift = D(str(round(rng.uniform(-0.01, 0.01), 5)))
        spread = D(str(round(rng.uniform(0.0001, 0.005), 5)))
        m = world.markets[sym]
        b = round(D(bid) * (1 + drift), m.tick_decimals)
        a = max(round(b * (1 + spread), m.tick_decimals), b + m.tick)
        qty = round(D(str(rng.uniform(0.00001, 0.01))) * (D(10) ** (6 - m.step_decimals)), m.step_decimals)
        age = rng.choice([0, 1, 5, 12, 29, 45]) if rng.random() < 0.9 else 120
        books[sym] = world.make_book(sym, b, a, qty=max(qty, m.step), ts=now - age, levels=2)
    balances = {
        "TMN": str(rng.randint(0, 4_000_000)),
        "USDT": str(round(rng.uniform(0, 15), 2)),
        "BTC": str(round(rng.uniform(0, 0.00012), 6)),
        "ETH": str(round(rng.uniform(0, 0.002), 4)),
        "SOL": str(round(rng.uniform(0, 0.05), 2)),
    }
    open_orders = []
    for i in range(rng.randint(0, 3)):
        sym = rng.choice(sorted(BOOKS))
        mid = books[sym].mid or D(1)
        px = round(mid * (1 + D(str(round(rng.uniform(-0.01, 0.01), 4)))), world.markets[sym].tick_decimals)
        open_orders.append(
            order(
                f"R{i}",
                sym,
                rng.choice(["BUY", "SELL"]),
                str(px),
                rng.choice(["entry", "exit", "rebalance"]),
                rng.choice(["S1", "S2", "S3"]),
                decision_id=f"d_r{i}",
            )
        )
    positions = [
        position(f"p{i}", rng.choice(["S1", "S3"]), rng.choice(sorted(BOOKS)))
        for i in range(rng.randint(0, 3))
    ]
    return world.ctx(
        balances=balances,
        books=books,
        now=now,
        open_orders=open_orders,
        positions=positions,
        placements=[now - rng.randint(0, 20000) for _ in range(rng.randint(0, 12))],
        fees_paid=D(rng.randint(0, 99_000)),
    )


def _random_intent(world: World, rng: random.Random, ctx: RiskContext, i: int) -> Intent:
    sym = rng.choice(sorted(BOOKS) + ["ATMN"])
    m = world.markets.get(sym)
    side = rng.choice(["BUY", "SELL"])
    otype = "MARKET" if rng.random() < 0.15 else "LIMIT"
    purpose = rng.choice(["entry", "entry", "entry", "rebalance", "exit", "stop", "leg2", "flatten"])
    book = ctx.books.get(sym)
    price = None
    if (
        otype == "LIMIT"
        and book is not None
        and book.mid is not None
        and m is not None
        and rng.random() < 0.8
    ):
        price = round(book.mid * (1 + D(str(round(rng.uniform(-0.04, 0.04), 4)))), m.tick_decimals)
    notional_tmn = D(rng.randint(20_000, 2_500_000))
    qty = notional = None
    if m is not None and book is not None and book.mid:
        mid_tmn = quote_to_tmn(m.quote, book.mid, ctx.books) or book.mid
        if rng.random() < 0.7:
            qty = round(notional_tmn / mid_tmn, m.step_decimals)
        else:
            notional = notional_tmn / (mid_tmn / book.mid)
    else:
        qty = D("0.001")
    features: dict[str, Any] = {"books_used": [sym]}
    if m is not None and m.base not in ("TMN", "USDT") and rng.random() < 0.4:
        other = f"{m.base}USDT" if m.quote == "TMN" else f"{m.base}TMN"
        features.update(
            {"cost_model": "lockin", "leg2_symbol": other, "dev_mid_pct": str(rng.uniform(-0.07, 0.07))}
        )
    return Intent(
        id=f"d_prop_{i}",
        ts=ctx.now,
        strategy=rng.choice(["S1", "S2", "S3"]),
        symbol=sym,
        side=side,
        order_type=otype,
        qty=qty,
        notional=notional,
        price=price,
        purpose=purpose,
        reason="property test",
        features=features,
        expected_edge_pct=D(str(round(rng.uniform(0, 0.03), 4))) if rng.random() < 0.9 else None,
        taker=rng.random() < 0.4,
    )


def _assert_invariants(world: World, intent: Intent, d: RiskDecision, ctx: RiskContext) -> None:
    r = world.cfg.risk
    m = world.markets[intent.symbol]
    book = ctx.books.get(intent.symbol)
    q, ref = d.adjusted_qty, ref_price(intent, book)
    assert q is not None and ref is not None
    assert q == round_qty(m, q) and q > 0
    # min order
    assert q >= m.min_qty and q * ref >= min_notional(m, world.cfg)
    # never more than requested
    requested = intent.qty if intent.qty is not None else intent.notional / ref  # type: ignore[operator]
    assert q <= requested
    val = ctx.portfolio.valuation(ctx.books)
    px_tmn = quote_to_tmn(m.quote, ref, ctx.books)
    assert px_tmn is not None
    entry_like = increases_exposure(intent)  # an entry-like purpose or any BUY
    open_orders = [o for o in ctx.portfolio.open_orders.values() if not o.is_terminal]
    # order size cap for entries
    if entry_like:
        assert q * px_tmn <= r.max_order_pct_equity * val.equity_tmn
    # coin exposure for coin buys: coins held + our open coin BUYs + this order
    if entry_like and intent.side == "BUY" and m.base not in ("TMN", "USDT"):
        open_buys = D0
        for o in open_orders:
            om = world.markets[o.req.symbol]
            if o.req.side == "BUY" and om.base not in ("TMN", "USDT"):
                open_buys += quote_to_tmn(om.quote, o.remaining_qty * o.req.price, ctx.books)  # type: ignore[operator]
        assert val.coins_tmn_value + open_buys + q * px_tmn <= r.max_coin_exposure_pct * val.equity_tmn
    # USDT band: from inside the band an approved entry stays inside it (open orders counted as
    # filled; a lock-in entry on its whole cycle)
    u = asset_price_tmn("USDT", ctx.books)

    def per_unit(mk: MarketInfo, side: str, price: Decimal) -> Decimal:
        assert u is not None
        if mk.base == "USDT":
            return u if side == "BUY" else -u
        if mk.quote == "USDT":
            return -price * u if side == "BUY" else price * u
        return D0

    leg2 = world.markets.get(str(intent.features.get("leg2_symbol")))
    lockin = intent.features.get("cost_model") == "lockin" and leg2 is not None
    if entry_like and ("USDT" in (m.base, m.quote) or (lockin and "USDT" in (leg2.base, leg2.quote))):  # type: ignore[union-attr]
        lo, hi = world.cfg.strategies.s2_usdtalloc.bounds
        per = per_unit(m, intent.side, ref)
        if lockin:
            per += per_unit(leg2, "SELL", ctx.books[leg2.symbol].best_bid)  # type: ignore[arg-type,union-attr]
        pending = sum(
            (o.remaining_qty * per_unit(world.markets[o.req.symbol], o.req.side, o.req.price) for o in open_orders),  # type: ignore[arg-type]
            D0,
        )
        now_share = (val.usdt_tmn_value + pending) / val.equity_tmn
        post = (val.usdt_tmn_value + pending + q * per) / val.equity_tmn
        if lo <= now_share <= hi:
            assert lo <= post <= hi
    # balance
    taker = is_taker(intent, book)
    if intent.side == "BUY":
        fee = m.taker_fee if taker else m.maker_fee
        assert q * ref * (1 + fee) <= ctx.portfolio.free(m.quote)
    else:
        assert q <= ctx.portfolio.free(m.base)
    # taker size vs the best level (a closing order may be raised to the minimum order)
    if taker:
        level = book.best_ask_qty if intent.side == "BUY" else book.best_bid_qty  # type: ignore[union-attr]
        if level and entry_like:
            assert q <= r.max_order_pct_of_best_level_qty * level
    # hard gates
    if ctx.flags.killed:
        assert intent.purpose in KILL_ALLOWED and intent.side == "SELL"


def test_property_random_intents_never_violate_invariants(world: World, rm: RiskManager) -> None:
    rng = random.Random(20261022)
    approved = 0
    adjusted = 0
    for i in range(300):
        ctx = _random_world_ctx(world, rng)
        if rng.random() < 0.1:
            ctx.flags.killed = True
        intent = _random_intent(world, rng, ctx, i)
        d = rm.evaluate(intent, ctx)
        assert [c.code for c in d.checks[: len(CHECK_CODES)]] == list(CHECK_CODES)
        assert d.approved == (not d.failed_codes)
        assert (d.veto_reason is None) == d.approved
        if d.approved:
            approved += 1
            requested = intent.qty if intent.qty is not None else None
            if requested is not None and d.adjusted_qty != round_qty(world.markets[intent.symbol], requested):
                adjusted += 1
            _assert_invariants(world, intent, d, ctx)
    # the generator must actually exercise approvals and adjustments, not just vetoes
    assert approved >= 30, approved
    assert adjusted >= 5, adjusted


# ---------------------------------------------------------------- review fixes (Phase 2)


@pytest.mark.parametrize("purpose", ["exit", "stop", "flatten", "leg2"])
def test_a_buy_is_never_risk_reducing_whatever_its_purpose(world: World, rm: RiskManager, purpose: str) -> None:
    """A BUY labelled exit/stop/flatten/leg2 is entry-like: it never passes the kill-switch, the
    final flatten phase or the pause, and the exposure limits apply to it."""
    buy = entry(symbol="BTCTMN", price=None, qty=D("0.00022"), purpose=purpose, features={})  # ~97 % of equity
    killed = rm.evaluate(buy, world.ctx(flags=EngineFlags(killed=True, kill_reason="drawdown")))
    assert not killed.approved and not chk(killed, "KILLED").passed
    assert "only flatten/exit/stop SELLs" in chk(killed, "KILLED").detail
    final = rm.evaluate(buy, world.ctx(deadline=_deadline(T0 + 600, world.cfg)))
    assert not chk(final, "DEADLINE").passed
    paused = rm.evaluate(buy, world.ctx(flags=EngineFlags(paused=True, pause_reason="anomaly")))
    assert not chk(paused, "PAUSED").passed
    free = rm.evaluate(buy, world.ctx())
    assert chk(free, "MAX_ORDER_PCT").detail.startswith("adjusted qty")
    assert free.adjusted_qty is None or free.adjusted_qty * D("22020000000") <= D("0.25") * D("4840000")


def test_a_market_order_is_sized_at_the_book_not_at_a_stale_price(world: World, rm: RiskManager) -> None:
    book = world.books()["BTCTMN"]
    stale = entry(symbol="BTCTMN", order_type="MARKET", price=D("250000000"), qty=D("0.0002"), features={})
    assert ref_price(stale, book) == D("22020000000")  # the ask, not the 250M "price"
    ctx = world.ctx()
    equity = ctx.portfolio.valuation(ctx.books).equity_tmn
    d = rm.evaluate(stale, ctx)
    assert chk(d, "MAX_ORDER_PCT").detail.startswith("adjusted qty 0.000200 ->")
    assert d.adjusted_qty is not None and d.adjusted_qty * D("22020000000") <= D("0.25") * equity
    no_ask = world.make_book("BTCTMN", "22000000000", None)
    d = rm.evaluate(stale, world.ctx(books=world.books(BTCTMN=no_ask)))
    assert not d.approved and "cannot size" in chk(d, "MIN_NOTIONAL").detail  # fails closed


def test_coin_exposure_counts_our_open_coin_buys(world: World, rm: RiskManager) -> None:
    resting = order("S1_REST", "BTCTMN", "BUY", "22000000000", purpose="entry", strategy="S1", qty="0.00006")
    eth = entry(symbol="ETHTMN", price=D("650000000"), qty=D("0.0005"), features={})  # 325k TMN
    alone = rm.evaluate(eth, world.ctx())
    assert alone.adjusted_qty == D("0.0005")
    ctx = world.ctx(open_orders=[resting])  # held 440k + resting 1.32M = 36 % of 4.84M
    d = rm.evaluate(eth, ctx)
    assert d.approved, d.veto_reason
    c = chk(d, "COIN_EXPOSURE")
    assert c.detail.startswith("adjusted qty 0.0005 -> 0.0002") and "open BUYs 1,320,000 TMN" in c.detail
    val = ctx.portfolio.valuation(ctx.books)
    post = val.coins_tmn_value + D("1320000") + d.adjusted_qty * D("650000000")  # type: ignore[operator]
    assert post <= D("0.40") * val.equity_tmn


def test_usdt_bounds_count_open_usdt_moving_orders(world: World, rm: RiskManager) -> None:
    ctx = world.ctx(balances={"TMN": "1700000", "USDT": "12"})  # 65.3 %
    assert rm.evaluate(_usdt("0.5"), ctx).approved  # 65.3 % -> 67.9 %
    pending = order("S2_REST", "USDTTMN", "BUY", "266700", purpose="rebalance", strategy="S2", qty="0.5")
    with_pending = world.ctx(balances={"TMN": "1700000", "USDT": "12"}, open_orders=[pending])  # 68.0 %
    c = chk(rm.evaluate(_usdt("0.5", purpose="entry"), with_pending), "USDT_BOUNDS")
    assert "incl. our open orders (133,350 TMN)" in c.detail and c.detail.startswith("adjusted qty 0.50 -> 0.36")


def test_usdt_bounds_size_a_lockin_entry_on_its_whole_cycle(world: World, rm: RiskManager) -> None:
    """Leg 1 BUY BTCTMN does not touch USDT, but its leg 2 SELL BTCUSDT brings USDT in."""
    high = world.ctx(balances={"TMN": "1000000", "USDT": "7.97"})  # 68.0 % USDT
    d = rm.evaluate(_lockin("0.0058", taker=True), high)
    c = chk(d, "USDT_BOUNDS")
    assert not c.passed and "lock-in cycle" in c.detail and "below the minimum order" in c.detail
    mid = world.ctx(balances={"TMN": "2500000", "USDT": "9"})  # 49 %
    d = rm.evaluate(_lockin("0.0058", taker=True), mid)
    assert d.approved, d.veto_reason
    assert "lock-in cycle incl. leg 2 SELL on BTCUSDT" in chk(d, "USDT_BOUNDS").detail
    plain = rm.evaluate(entry(symbol="BTCTMN", price=D("22000000000"), features={}), high)
    assert chk(plain, "USDT_BOUNDS").detail == "n/a (does not change the USDT holding)"


def test_self_cross_ignores_the_positions_own_entry_buy(world: World, rm: RiskManager) -> None:
    pos = position("p1", "S3", "BTCTMN")
    own = order("S3_ENTRY", "BTCTMN", "BUY", "22000000000", purpose="entry", decision_id="entry_p1")
    stop = exit_(symbol="BTCTMN", purpose="stop", taker=True, linked_position_id="p1", features={})
    d = rm.evaluate(stop, world.ctx(positions=[pos], open_orders=[own]))
    assert d.approved, d.veto_reason
    assert "canceled before this SELL" in chk(d, "SELF_CROSS").detail
    other = order("S1_ENTRY", "BTCTMN", "BUY", "22000000000", purpose="entry", strategy="S1", decision_id="d_x")
    d = rm.evaluate(stop, world.ctx(positions=[pos], open_orders=[own, other]))
    assert d.failed_codes == ["SELF_CROSS"]  # someone else's BUY still blocks
    unlinked = exit_(symbol="BTCTMN", purpose="stop", taker=True, features={})
    assert rm.evaluate(unlinked, world.ctx(positions=[pos], open_orders=[own])).failed_codes == ["SELF_CROSS"]


def test_best_level_never_cuts_a_closing_order_below_the_minimum(world: World, rm: RiskManager) -> None:
    thin = world.make_book("BTCTMN", "22000000000", "22020000000", qty="0.000002")  # 44k TMN at the top
    books = world.books(BTCTMN=thin)
    stop = exit_(symbol="BTCTMN", purpose="stop", taker=True, features={})
    d = rm.evaluate(stop, world.ctx(books=books))
    assert d.approved, d.veto_reason
    assert d.adjusted_qty == D("0.000003")  # the smallest sellable qty (>= 50,000 TMN), not 80 % of the level
    assert "raised to the minimum order" in chk(d, "BEST_LEVEL_QTY").detail
    final = exit_(symbol="BTCTMN", purpose="flatten", order_type="MARKET", features={})
    assert rm.evaluate(final, world.ctx(books=books)).approved
    buy = entry(symbol="BTCTMN", price=None, taker=True, features={})
    assert not rm.evaluate(buy, world.ctx(books=books)).approved  # entries are still capped


def test_rate_limits_reserve_the_re_placements_of_an_entry(world: World, rm: RiskManager) -> None:
    seven = [T0 - 60 * i for i in range(1, 8)]
    d = rm.evaluate(entry(), world.ctx(placements=seven))  # 7 + 3 = 10: the worst case fits
    assert d.approved, d.veto_reason
    eight = [T0 - 60 * i for i in range(1, 9)]
    d = rm.evaluate(entry(), world.ctx(placements=eight))  # 8 + 3 = 11 > 10
    assert d.failed_codes == ["RATE_HOUR"] and "re-prices" in d.veto_reason  # type: ignore[operator]
    working = order("S2_W", "ETHTMN", "BUY", "600000000", purpose="rebalance", strategy="S2")
    d = rm.evaluate(entry(), world.ctx(placements=seven, open_orders=[working]))  # 7 + 2 reserved + 3
    assert d.failed_codes == ["RATE_HOUR"] and "2 re-placements reserved" in d.veto_reason  # type: ignore[operator]


def test_price_sanity_never_blocks_a_closing_sell_near_the_bid(world: World, rm: RiskManager) -> None:
    wide = world.make_book("SOLTMN", "26000000", "27400000", qty="10")  # 5.3 % spread (a crash)
    ctx = world.ctx(balances={**BALANCES, "SOL": "0.05"}, books=world.books(SOLTMN=wide))
    flat = exit_(symbol="SOLTMN", purpose="flatten", qty=D("0.05"), price=D("25870000"), features={})
    d = rm.evaluate(flat, ctx)
    assert d.approved, d.veto_reason
    assert "from the best bid" in chk(d, "PRICE_SANITY").detail
    fat = exit_(symbol="SOLTMN", purpose="flatten", qty=D("0.05"), price=D("25000000"), features={})
    assert rm.evaluate(fat, ctx).failed_codes == ["PRICE_SANITY"]  # 3.8 % under the bid: fat finger


def test_a_limit_that_crosses_the_book_is_a_taker(world: World, rm: RiskManager) -> None:
    book = world.make_book("BTCUSDT", "82500.00", "82520.00", qty="0.00001")
    crossing = entry(price=D("82600.00"))  # taker=False but priced above the ask
    assert is_taker(crossing, book) and not is_taker(crossing)
    assert not is_taker(entry(), book)  # at the bid: a maker
    d = rm.evaluate(crossing, world.ctx(books=world.books(BTCUSDT=book)))
    assert not chk(d, "BEST_LEVEL_QTY").detail.startswith("n/a")
    assert "taker BTCUSDT" in chk(d, "EDGE_VS_COST").detail


# ---------------------------------------------------------------- review fixes (Phase 3): lock-in leg 2


def test_lockin_leg1_shrunk_below_what_leg2_can_sell_is_vetoed(world: World, rm: RiskManager) -> None:
    """Review fix (critical): USDT share 65 % (S2's trend_up target) leaves room for 250,000 TMN of
    lock-in cycle, so USDT_BOUNDS cuts 0.000045 BTC to 0.000011 (242,220 TMN on BTCTMN, well above
    its 50,000 TMN minimum). After the 0.3 % fee paid in BTC only 0.000010967 BTC are held and leg 2
    could sell 0.000010 x 82,500 = 0.825 USDT < 1 USDT: the entry is vetoed instead of approved (it
    would have been sold back unhedged at a certain loss)."""
    ctx = world.ctx(balances={"TMN": "1750000", "USDT": "12.186"})  # 65.0 % USDT of ~5.0M TMN
    d = rm.evaluate(_lockin_qty("0.000045"), ctx)
    assert chk(d, "USDT_BOUNDS").passed and "adjusted qty 0.000045 -> 0.000011" in chk(d, "USDT_BOUNDS").detail
    assert not d.approved and d.failed_codes == ["MIN_NOTIONAL"]
    assert d.veto_reason is not None and d.veto_reason.startswith("MIN_NOTIONAL: lock-in leg 2 on BTCUSDT after adjustments")
    assert "leaves 0.000010967 BTC after the 0.300% leg-1 fee (paid in BTC); leg 2 would sell 0.000010 BTC" in d.veto_reason
    assert "notional 0.000010 x 82500.00 = 0.8250 USDT < min 1.0000 USDT" in d.veto_reason
    # with more room (49 % USDT) the same entry passes, leg 2 included
    ok = rm.evaluate(_lockin_qty("0.000045"), world.ctx(balances={"TMN": "2500000", "USDT": "9"}))
    assert ok.approved, ok.veto_reason


def test_lockin_leg2_minimum_counts_the_coin_fee_without_any_adjustment(world: World, rm: RiskManager) -> None:
    # 0.000013 BTC: 1.07 USDT gross on BTCUSDT, but 0.000012961 after the fee -> 0.000012 = 0.99 USDT
    d = rm.evaluate(_lockin_qty("0.000013"), world.ctx())
    assert d.failed_codes == ["MIN_NOTIONAL"]
    assert d.veto_reason is not None and "lock-in leg 2 on BTCUSDT: buying 0.000013 BTC" in d.veto_reason
    assert rm.evaluate(_lockin_qty("0.000014"), world.ctx()).approved  # 0.000013 x 82,500 = 1.07 USDT


def test_lockin_qty_is_landed_on_a_leg2_step(world: World, rm: RiskManager) -> None:
    """A coarser leg-2 step: 0.00004 BTC leaves 0.00003988 after the fee, leg 2 (5 decimals) sells
    0.00003; 0.000031 BTC already cover that, so the RiskManager buys no coins leg 2 cannot sell."""
    markets = {**world.markets, "BTCUSDT": world.markets["BTCUSDT"].model_copy(update={"step_decimals": 5})}
    d = rm.evaluate(_lockin_qty("0.00004"), world.ctx(markets=markets))
    assert d.approved, d.veto_reason
    assert d.adjusted_qty == D("0.000031")
    note = [c for c in d.checks[len(CHECK_CODES):] if c.code == "MIN_NOTIONAL"]
    assert note and note[0].passed and "lock-in: qty 0.000040 -> 0.000031" in note[0].detail
    assert lockin_leg_qtys(D("0.00004"), D("0.003"), markets["BTCTMN"], markets["BTCUSDT"]) == (
        D("0.000031"), D("0.00003"),
    )
    # an S1 lock-in qty sized by the same rule is never changed
    assert rm.evaluate(_lockin_qty("0.000031"), world.ctx(markets=markets)).adjusted_qty == D("0.000031")


def test_lockin_leg_qtys_properties(world: World) -> None:
    btc_tmn, btc_usdt = world.markets["BTCTMN"], world.markets["BTCUSDT"]
    rng = random.Random(7)
    for _ in range(500):
        limit = D(rng.randint(1, 10_000)) / D(10**6)
        fee = D(rng.choice(["0.003", "0.0025", "0.001", "0.0013"]))
        q1, q2 = lockin_leg_qtys(limit, fee, btc_tmn, btc_usdt)
        assert q1 <= limit and q1 == round_qty(btc_tmn, q1)
        assert q2 == round_qty(btc_usdt, q1 * (1 - fee))  # leg 2 = what is held, rounded down
        assert q1 * (1 - fee) - q2 < btc_tmn.step  # unhedged below one leg-1 step


def _lockin_qty(qty: str) -> Intent:
    return _lockin("0.0058", taker=True).model_copy(update={"qty": D(qty)})
