"""S2 USDTAlloc (PRD-03 §4): regime -> target, bounds, hysteresis, timing guards, maker prices.

Pinned down here:
* rules regime order and thresholds (trend_up -> trend_down -> chaos -> range, strict comparisons);
* a fresh plan supplies the regime and the target; a stale plan (ai.plan_stale_s) is ignored;
* the target is always clamped to the bounds [25 %, 70 %];
* a rebalance needs |gap| >= 15 pp AND >= 30 min since the last rebalance fill AND no open S2
  order AND no recent unfilled attempt; never while flattening; no churn afterwards;
* maker-first price (bid + 1 tick for a BUY while below the ask, mirrored for a SELL);
* the reason sentence carries the numbers; state round-trips through JSON; output is deterministic.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from typing import Any

import pytest

from wta.config import Config, apply_changes
from wta.execution.positions import PortfolioState
from wta.features.engine import FeatureSnapshot
from wta.models import (
    D0,
    Balance,
    BookSnapshot,
    Fill,
    Intent,
    Level,
    MarketInfo,
    MarketPlan,
    Order,
    OrderRequest,
    RegimeCall,
)
from wta.risk.context import Deadline, EngineFlags, RiskContext
from wta.risk.manager import RiskManager
from wta.strategies.base import PortfolioView, StrategyContext
from wta.strategies.s2_usdtalloc import RETRY_AFTER_UNFILLED_S, SYMBOL, S2USDTAlloc, rule_regime
from wta.utils.decimalutil import D
from wta.utils.ids import make_client_id

from .conftest import T0

BID, ASK = "266720", "266758"
EQUITY = "5000000"

# dollar features (FeatureSnapshot.dollar) that make the rules say each regime
RANGE = {
    "trend_flag": 0,
    "ret_24h": D("0.003"),
    "ema_gap_pct": D("0.0005"),
    "atr_15m_pct": D("0.0012"),
    "atr_15m_pct_median_24h": D("0.0010"),
}
TREND_UP = {**RANGE, "trend_flag": 1, "ret_24h": D("0.024"), "ema_gap_pct": D("0.004")}
TREND_DOWN = {**RANGE, "trend_flag": -1, "ret_24h": D("-0.012"), "ema_gap_pct": D("-0.003")}
CHAOS = {**RANGE, "atr_15m_pct": D("0.0050"), "atr_15m_pct_median_24h": D("0.0020")}
DOLLAR = {"range": RANGE, "trend_up": TREND_UP, "trend_down": TREND_DOWN, "chaos": CHAOS}


# ---------------------------------------------------------------- builders


def book(bid: str | None = BID, ask: str | None = ASK, ts: float = T0, qty: str = "1000") -> BookSnapshot:
    return BookSnapshot(
        symbol=SYMBOL,
        ts=ts,
        source="rest",
        bids=[] if bid is None else [Level(price=D(bid), qty=D(qty))],
        asks=[] if ask is None else [Level(price=D(ask), qty=D(qty))],
    )


def make_plan(
    regime: str = "trend_up",
    target: str = "0.65",
    ts: float = T0,
    conf: float = 0.8,
    enabled: tuple[str, ...] = ("S1", "S2"),
    evidence: str = "USDTTMN +2.4% in 24h, ema20 above ema60",
    with_call: bool = True,
) -> MarketPlan:
    regimes = {SYMBOL: RegimeCall(regime=regime, confidence=conf, evidence=evidence)} if with_call else {}  # type: ignore[arg-type]
    return MarketPlan(ts=ts, regimes=regimes, enabled_strategies=list(enabled), usdt_target_share=D(target))


def make_ctx(
    cfg: Config,
    markets: dict[str, MarketInfo],
    *,
    now: float = T0,
    share: str | Decimal = "0",
    equity: str = EQUITY,
    plan: MarketPlan | None = None,
    dollar: dict[str, Any] | None = None,
    bid: str | None = BID,
    ask: str | None = ASK,
    orders: tuple[Order, ...] = (),
    flatten_phase: int = 0,
    with_book: bool = True,
) -> StrategyContext:
    books = {SYMBOL: book(bid, ask, ts=now)} if with_book else {}
    view = PortfolioView(
        equity_tmn=D(equity),
        free={},
        total={},
        positions=(),
        open_orders=orders,
        usdt_share=D(share),
        coin_exposure_pct=D0,
        u_bid=D(bid) if bid else None,
    )
    feats = FeatureSnapshot(ts=now, dollar=dict(RANGE if dollar is None else dollar))
    return StrategyContext(
        now=now,
        books=books,
        features=feats,
        candles={},
        markets=markets,
        portfolio=view,
        plan=plan,
        cfg=cfg,
        flatten_phase=flatten_phase,
    )


def s2_order(
    filled: str = "0", strategy: str = "S2", symbol: str = SYMBOL, decision: str = "d_s2", side: str = "BUY"
) -> Order:
    req = OrderRequest(
        client_id=make_client_id(strategy, symbol, T0, decision),
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type="LIMIT",
        qty=D("9.37"),
        price=D("266721"),
        strategy=strategy,
        purpose="rebalance" if strategy == "S2" else "entry",
        decision_id=decision,
    )
    status = "FILLED" if D(filled) >= req.qty else "CANCELED"
    return Order(req=req, status=status, filled_qty=D(filled), created_ts=T0, updated_ts=T0)  # type: ignore[arg-type]


def s2_fill(qty: str = "4", ts: float = T0 + 5, strategy: str = "S2", decision: str = "d_s2") -> Fill:
    return Fill(
        order_client_id=make_client_id(strategy, SYMBOL, T0, decision),
        symbol=SYMBOL,
        side="BUY",
        price=D("266721"),
        qty=D(qty),
        ts=ts,
    )


def only(intents: list[Intent]) -> Intent:
    assert len(intents) == 1, intents
    return intents[0]


@pytest.fixture
def s2(cfg: Config) -> S2USDTAlloc:
    return S2USDTAlloc(cfg.strategies.s2_usdtalloc)


def test_config_assumptions(cfg: Config) -> None:
    """The numbers below rely on the shipped defaults (PRD-03 §4)."""
    c = cfg.strategies.s2_usdtalloc
    assert c.bounds == (D("0.25"), D("0.70"))
    assert c.rebalance_threshold_pp == D("0.15")
    assert c.min_rebalance_interval_s == 1800
    assert c.trend_ret_24h_pct == D("0.008")
    assert c.chaos_atr_mult == D("2.0")
    assert c.target_share_by_regime == {
        "range": D("0.50"),
        "trend_up": D("0.65"),
        "trend_down": D("0.30"),
        "chaos": D("0.40"),
    }
    assert cfg.ai.plan_stale_s == 1200
    assert cfg.risk.max_order_pct_equity == D("0.25")


# ---------------------------------------------------------------- rules regime


@pytest.mark.parametrize("regime", ["range", "trend_up", "trend_down", "chaos"])
def test_rule_regime_each_regime(cfg: Config, regime: str) -> None:
    got, evidence = rule_regime(DOLLAR[regime], cfg.strategies.s2_usdtalloc)
    assert got == regime
    assert "24h" in evidence and "ATR15m" in evidence


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"trend_flag": 1, "ret_24h": D("0.008")}, "range"),  # strict: 24h must be > +0.8 %
        ({"trend_flag": 1, "ret_24h": D("0.0081")}, "trend_up"),
        ({"trend_flag": -1, "ret_24h": D("-0.008")}, "range"),
        ({"trend_flag": -1, "ret_24h": D("-0.0081")}, "trend_down"),
        ({"trend_flag": 0, "ret_24h": D("0.05")}, "range"),  # no ema trend -> no trend regime
        ({"trend_flag": 1, "ret_24h": D("-0.05")}, "range"),  # flag and return disagree
        ({"trend_flag": -1, "ret_24h": D("0.05")}, "range"),
        ({"trend_flag": 1, "ret_24h": None}, "range"),  # 24 h of candles not there yet
        # trend rules come first: a trending dollar with high volatility is still a trend
        ({"trend_flag": 1, "ret_24h": D("0.02"), "atr_15m_pct": D("0.01")}, "trend_up"),
        ({"trend_flag": -1, "ret_24h": D("-0.02"), "atr_15m_pct": D("0.01")}, "trend_down"),
        # chaos: ATR% strictly above 2x its 24 h median
        ({"atr_15m_pct": D("0.0020"), "atr_15m_pct_median_24h": D("0.0010")}, "range"),
        ({"atr_15m_pct": D("0.00201"), "atr_15m_pct_median_24h": D("0.0010")}, "chaos"),
        ({"atr_15m_pct": D("0.01"), "atr_15m_pct_median_24h": D("0")}, "range"),  # no median yet
        ({"atr_15m_pct": D("0.01"), "atr_15m_pct_median_24h": None}, "range"),
        ({"atr_15m_pct": None}, "range"),
    ],
)
def test_rule_regime_thresholds_and_order(cfg: Config, changes: dict[str, Any], expected: str) -> None:
    got, _ = rule_regime({**RANGE, **changes}, cfg.strategies.s2_usdtalloc)
    assert got == expected


def test_rule_regime_warm_up_is_range(cfg: Config) -> None:
    got, evidence = rule_regime({}, cfg.strategies.s2_usdtalloc)
    assert got == "range"
    assert "n/a" in evidence  # missing numbers are shown, not invented


def test_rule_regime_uses_config_thresholds(cfg: Config) -> None:
    c = apply_changes(cfg, {"strategies.s2_usdtalloc.trend_ret_24h_pct": "0.03"}).strategies.s2_usdtalloc
    assert rule_regime(TREND_UP, c)[0] == "range"  # 24h +2.4 % < 3 %
    c = apply_changes(cfg, {"strategies.s2_usdtalloc.chaos_atr_mult": "3"}).strategies.s2_usdtalloc
    assert rule_regime(CHAOS, c)[0] == "range"  # 0.5 % < 3 x 0.2 %


# ---------------------------------------------------------------- regime -> target


@pytest.mark.parametrize(
    ("regime", "target"),
    [("range", "0.50"), ("trend_up", "0.65"), ("trend_down", "0.30"), ("chaos", "0.40")],
)
def test_rules_regime_maps_to_target(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], regime: str, target: str
) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, dollar=DOLLAR[regime])))
    assert it.features["regime"] == regime
    assert it.features["regime_source"] == "rules"
    assert D(it.features["target_share"]) == D(target)
    assert it.features["target_clamped"] is False
    assert f"rules table for {regime}" in it.reason
    assert it.side == "BUY"
    assert it.notional is not None


@pytest.mark.parametrize(
    ("regime", "target"),
    [("range", "0.50"), ("trend_up", "0.65"), ("trend_down", "0.30"), ("chaos", "0.45")],
)
def test_plan_regime_and_target_win_over_rules(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], regime: str, target: str
) -> None:
    # the rules would say something else (chaos/range) — a fresh plan decides
    rules_dollar = RANGE if regime == "chaos" else CHAOS
    plan = make_plan(regime=regime, target=target, ts=T0 - 60, conf=0.7)
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=plan, dollar=rules_dollar)))
    assert it.features["regime"] == regime
    assert it.features["regime_source"] == "plan, confidence 0.70"
    assert it.features["rule_regime"] == ("range" if regime == "chaos" else "chaos")
    assert D(it.features["target_share"]) == D(target)  # the plan's own target, not the table
    assert it.features["plan_fresh"] is True
    assert "(plan)" in it.reason


def test_plan_without_usdttmn_call_uses_rules_regime_but_plan_target(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    plan = make_plan(target="0.55", with_call=False)
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=plan, dollar=TREND_DOWN)))
    assert it.features["regime"] == "trend_down"
    assert it.features["regime_source"] == "rules, plan has no USDTTMN call"
    assert D(it.features["target_share"]) == D("0.55")


# ---------------------------------------------------------------- bounds clamp


@pytest.mark.parametrize(
    ("asked", "clamped"), [("0.9", "0.70"), ("0.1", "0.25"), ("1", "0.70"), ("0", "0.25")]
)
def test_plan_target_clamped_to_bounds(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], asked: str, clamped: str
) -> None:
    # pick the current share far enough from the clamped target to trade
    share = "0" if D(clamped) > D("0.5") else "0.9"
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=make_plan(target=asked), share=share)))
    assert D(it.features["target_share"]) == D(clamped)
    assert D(it.features["target_raw"]) == D(asked)
    assert it.features["target_clamped"] is True
    lo_hi = "clamped to bounds [25%, 70%]"
    assert lo_hi in it.reason
    assert f"plan asked {D(asked) * 100:.0f}%" in it.reason


def test_target_exactly_on_bound_is_not_clamped(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=make_plan(target="0.70"))))
    assert D(it.features["target_share"]) == D("0.70")
    assert it.features["target_clamped"] is False


def test_rules_table_outside_bounds_is_clamped(cfg: Config, markets: dict[str, MarketInfo]) -> None:
    c = apply_changes(
        cfg,
        {
            "strategies.s2_usdtalloc.target_share_by_regime": {
                "range": "0.10",
                "trend_up": "0.95",
                "trend_down": "0.30",
                "chaos": "0.40",
            }
        },
    )
    up = only(S2USDTAlloc(c.strategies.s2_usdtalloc).on_tick(make_ctx(c, markets, dollar=TREND_UP)))
    assert D(up.features["target_share"]) == D("0.70")
    down = only(S2USDTAlloc(c.strategies.s2_usdtalloc).on_tick(make_ctx(c, markets, share="0.9")))
    assert D(down.features["target_share"]) == D("0.25")
    assert down.side == "SELL"


# ---------------------------------------------------------------- threshold / hysteresis


@pytest.mark.parametrize(
    ("share", "side"),
    [
        ("0.3501", None),  # gap 14.99 pp < 15 pp
        ("0.35", "BUY"),  # gap exactly 15 pp -> trade (>=)
        ("0.3499", "BUY"),
        ("0.6499", None),
        ("0.65", "SELL"),
        ("0.6501", "SELL"),
        ("0.5", None),
    ],
)
def test_rebalance_threshold(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], share: str, side: str | None
) -> None:
    out = s2.on_tick(make_ctx(cfg, markets, share=share))  # rules: range -> target 50 %
    if side is None:
        assert out == []
        assert s2.diagnostics()["status"] == "within_threshold"
        assert s2.last_attempt_ts is None  # nothing happened, nothing to wait for
    else:
        assert only(out).side == side
        assert s2.diagnostics()["status"] == "intent"


# ---------------------------------------------------------------- timing guards


def test_min_interval_after_a_filled_rebalance(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    only(s2.on_tick(make_ctx(cfg, markets, now=T0)))
    done_ts = T0 + 30
    s2.on_order_done(s2_order(filled="4.68"), make_ctx(cfg, markets, now=done_ts))
    assert s2.last_rebalance_ts == done_ts
    assert s2.rebalances == 1
    # the risk cap left the share at 25 % (gap 25 pp): still nothing for 30 minutes
    assert s2.on_tick(make_ctx(cfg, markets, now=done_ts + 1799, share="0.25")) == []
    assert s2.diagnostics()["status"] == "min_interval"
    assert s2.diagnostics()["wait_s"] == 1.0
    it = only(s2.on_tick(make_ctx(cfg, markets, now=done_ts + 1800, share="0.25")))
    assert it.side == "BUY"
    assert "last rebalance 30 min ago (min 30 min)" in it.reason
    assert it.features["initial_allocation"] is False


def test_open_s2_order_blocks(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    open_order = s2_order()
    assert s2.on_tick(make_ctx(cfg, markets, orders=(open_order,))) == []
    assert s2.diagnostics()["status"] == "order_open"
    # another strategy's order elsewhere does not block S2
    other = s2_order(strategy="S1", symbol="BTCTMN", decision="d_s1")
    only(s2.on_tick(make_ctx(cfg, markets, orders=(other,))))


def test_unfilled_attempt_waits_then_retries(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    only(s2.on_tick(make_ctx(cfg, markets, now=T0)))
    done_ts = T0 + 45  # reprices exhausted, given up
    s2.on_order_done(s2_order(filled="0"), make_ctx(cfg, markets, now=done_ts))
    assert s2.last_rebalance_ts is None  # nothing filled: the min interval does not start
    assert s2.last_unfilled_ts == done_ts
    assert s2.on_tick(make_ctx(cfg, markets, now=done_ts + RETRY_AFTER_UNFILLED_S - 1)) == []
    assert s2.diagnostics()["status"] == "retry_wait"
    it = only(s2.on_tick(make_ctx(cfg, markets, now=done_ts + RETRY_AFTER_UNFILLED_S)))
    assert it.features["initial_allocation"] is True


def test_vetoed_intent_is_not_resent_every_tick(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    """An intent that never became an order (risk veto, router drop) waits like an unfilled one."""
    only(s2.on_tick(make_ctx(cfg, markets, now=T0)))
    for k in range(1, int(RETRY_AFTER_UNFILLED_S) // 2):
        assert s2.on_tick(make_ctx(cfg, markets, now=T0 + 2 * k)) == []
    only(s2.on_tick(make_ctx(cfg, markets, now=T0 + RETRY_AFTER_UNFILLED_S)))


def test_partial_fill_on_an_earlier_placement_counts_as_filled(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    """The order handed to on_order_done may be the last re-placement (0 filled) of a logical
    order whose first placement filled partly: the fill seen in on_fill still starts the interval."""
    only(s2.on_tick(make_ctx(cfg, markets, now=T0)))
    s2.on_fill(s2_fill(qty="2", ts=T0 + 10), None, make_ctx(cfg, markets, now=T0 + 10))
    assert s2.last_rebalance_ts == T0 + 10  # clock starts at the fill already
    s2.on_order_done(s2_order(filled="0"), make_ctx(cfg, markets, now=T0 + 45))
    assert s2.last_rebalance_ts == T0 + 45
    assert s2.rebalances == 1
    assert s2.attempt_filled == 0
    assert s2.on_tick(make_ctx(cfg, markets, now=T0 + 45 + 600, share="0.1")) == []
    assert s2.diagnostics()["status"] == "min_interval"


def test_foreign_fills_and_orders_are_ignored(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    ctx = make_ctx(cfg, markets)
    s2.on_fill(s2_fill(strategy="FLATTEN"), None, ctx)  # flatten selling USDT
    s2.on_fill(s2_fill(strategy="S1"), None, ctx)
    s2.on_order_done(s2_order(filled="1", strategy="S1", symbol="BTCTMN"), ctx)
    s2.on_order_done(s2_order(filled="1", strategy="FLATTEN"), ctx)
    assert s2.last_rebalance_ts is None
    assert s2.last_unfilled_ts is None
    assert s2.attempt_filled == 0
    assert s2.rebalances == 0


# ---------------------------------------------------------------- initial allocation / churn


def test_initial_allocation_from_100pct_tmn(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    assert s2.on_tick(make_ctx(cfg, markets, with_book=False)) == []
    assert s2.diagnostics()["status"] == "no_book"
    assert s2.last_attempt_ts is None  # waiting for a book is not an attempt
    it = only(s2.on_tick(make_ctx(cfg, markets, now=T0 + 2)))  # first tick with a book
    assert (it.strategy, it.symbol, it.side, it.order_type, it.purpose) == (
        "S2",
        SYMBOL,
        "BUY",
        "LIMIT",
        "rebalance",
    )
    assert it.taker is False
    assert it.expected_edge_pct is None
    assert it.price == D("266721")
    assert it.qty == D("9.37")  # 50 % x 5,000,000 / 266,721 = 9.3731 -> step 0.01, rounded down
    assert it.notional == D("9.37") * D("266721")
    assert it.features["books_used"] == [SYMBOL]
    assert it.features["initial_allocation"] is True
    assert D(it.features["notional_tmn"]) == D("2500000")
    assert "initial allocation from 100% TMN (no S2 rebalance has filled yet)" in it.reason
    assert "buy 2,500,000 TMN of USDT (9.37 USDT, 50% of equity 5,000,000 TMN)" in it.reason
    assert "caps one order at 25% of equity (1,250,000 TMN)" in it.reason


def test_initial_allocation_passes_risk_capped_to_25pct(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    """End to end with the real RiskManager: approved, MAX_ORDER_PCT shrinks it to 25 % of equity."""
    books = {SYMBOL: book()}
    port = PortfolioState(initial_equity_tmn=D(EQUITY))
    port.set_balances({"TMN": Balance(asset="TMN", free=D(EQUITY))})
    port.refresh(books, T0)
    assert port.usdt_share == 0
    it = only(s2.on_tick(make_ctx(cfg, markets, share=port.usdt_share, equity=str(port.equity_tmn))))
    rctx = RiskContext(
        now=T0,
        portfolio=port,
        books=books,
        markets=markets,
        cfg=cfg,
        flags=EngineFlags(),
        deadline=Deadline(None, None, None, None),
    )
    decision = RiskManager(cfg).evaluate(it, rctx)
    assert decision.approved, decision.veto_reason
    assert decision.adjusted_qty == D("4.68")  # 1,250,000 / 266,721 = 4.686 -> 4.68
    checks = {c.code: c for c in decision.checks}
    assert checks["EDGE_VS_COST"].detail.startswith("n/a")
    assert checks["FEE_BUDGET"].passed
    assert checks["USDT_BOUNDS"].passed


def test_no_churn_over_many_ticks_after_a_fill(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    intents: list[Intent] = []
    intents += s2.on_tick(make_ctx(cfg, markets, now=T0))
    assert len(intents) == 1
    s2.on_fill(s2_fill(qty="9.37", ts=T0 + 4), None, make_ctx(cfg, markets, now=T0 + 4))
    s2.on_order_done(s2_order(filled="9.37"), make_ctx(cfg, markets, now=T0 + 4))
    # 12 hours of 2-second ticks; the dollar moves the share around 50 % by up to +-14 pp
    rng = random.Random(7)
    share = D("0.50")
    for k in range(1, 12 * 1800):
        share = min(max(share + D(rng.randint(-50, 50)) / 100000, D("0.36")), D("0.64"))
        intents += s2.on_tick(make_ctx(cfg, markets, now=T0 + 4 + 2 * k, share=share))
    assert len(intents) == 1
    assert s2.attempts == 1
    assert s2.rebalances == 1


def test_no_churn_while_closing_a_capped_gap(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    """Risk caps the first order at 25 %: the second half follows exactly one interval later."""
    times: list[float] = []
    share = D("0")
    now = T0
    while now < T0 + 3 * 3600:
        out = s2.on_tick(make_ctx(cfg, markets, now=now, share=share))
        if out:
            times.append(now)
            fill_ts = now + 6
            s2.on_order_done(s2_order(filled="4.68"), make_ctx(cfg, markets, now=fill_ts))
            share = min(share + D("0.25"), D("0.50"))
        now += 2
    assert times == [T0, T0 + 6 + 1800]


# ---------------------------------------------------------------- guards


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_never_trades_while_flattening(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], phase: int
) -> None:
    assert s2.on_tick(make_ctx(cfg, markets, share="0.9", flatten_phase=phase)) == []
    assert s2.diagnostics() == {"status": "flatten", "flatten_phase": phase}
    assert s2.last_attempt_ts is None


def test_disabled_by_config_or_plan(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    assert s2.on_tick(make_ctx(cfg, markets, plan=make_plan(enabled=("S1",)))) == []
    assert s2.diagnostics()["status"] == "plan_disabled"
    off = apply_changes(cfg, {"strategies.s2_usdtalloc.enabled": False})
    strat = S2USDTAlloc(off.strategies.s2_usdtalloc)
    assert strat.on_tick(make_ctx(off, markets)) == []
    assert strat.diagnostics()["status"] == "disabled"


def test_stale_plan_is_ignored(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    stale_s = cfg.ai.plan_stale_s
    # stale plan: says trend_up 65 % and does not even enable S2 — all of it ignored
    stale = make_plan(regime="trend_up", target="0.65", ts=T0 - stale_s - 1, enabled=("S1",))
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=stale)))
    assert it.features["regime"] == "range"
    assert it.features["regime_source"] == "rules"
    assert it.features["plan_fresh"] is False
    assert D(it.features["target_share"]) == D("0.50")
    # exactly at the staleness limit the plan still counts
    fresh = make_plan(regime="trend_up", target="0.65", ts=T0 - stale_s)
    it = only(S2USDTAlloc(cfg.strategies.s2_usdtalloc).on_tick(make_ctx(cfg, markets, plan=fresh)))
    assert it.features["regime"] == "trend_up"
    assert D(it.features["target_share"]) == D("0.65")


def test_no_equity_and_tiny_gap_notional(cfg: Config, markets: dict[str, MarketInfo]) -> None:
    s = S2USDTAlloc(cfg.strategies.s2_usdtalloc)
    assert s.on_tick(make_ctx(cfg, markets, equity="0")) == []
    assert s.diagnostics()["status"] == "no_equity"
    # 50 % of 90,000 TMN = 45,000 TMN < USDTTMN min notional 50,000 TMN
    assert s.on_tick(make_ctx(cfg, markets, equity="90000")) == []
    assert s.diagnostics()["status"] == "below_min_notional"
    assert s.last_attempt_ts is None


# ---------------------------------------------------------------- maker prices


@pytest.mark.parametrize(
    ("share", "bid", "ask", "side", "price"),
    [
        ("0", "266720", "266758", "BUY", "266721"),  # bid + 1 tick
        ("0", "266720", "266722", "BUY", "266721"),
        ("0", "266720", "266721", "BUY", "266720"),  # 1-tick spread: bid + tick would cross -> bid
        ("0.9", "266720", "266758", "SELL", "266757"),  # ask - 1 tick
        ("0.9", "266720", "266722", "SELL", "266721"),
        ("0.9", "266720", "266721", "SELL", "266721"),  # 1-tick spread -> ask
    ],
)
def test_maker_first_prices(
    s2: S2USDTAlloc,
    cfg: Config,
    markets: dict[str, MarketInfo],
    share: str,
    bid: str,
    ask: str,
    side: str,
    price: str,
) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, share=share, bid=bid, ask=ask)))
    assert it.side == side
    assert it.price == D(price)
    assert D(bid) <= it.price <= D(ask)
    assert it.taker is False
    # notional = |gap| x equity (risk caps it later): 40 pp or 50 pp of 5,000,000 TMN
    gap = D("0.40") if side == "SELL" else D("0.50")
    assert D(it.features["notional_tmn"]) == gap * D(EQUITY)
    assert it.qty == (gap * D(EQUITY) / D(price)).quantize(D("0.01"), rounding="ROUND_DOWN")


def test_maker_price_respects_tick_decimals(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    fine = {**markets, SYMBOL: markets[SYMBOL].model_copy(update={"tick_decimals": 1})}
    buy = only(s2.on_tick(make_ctx(cfg, fine, bid="266720.5", ask="266720.7")))
    assert buy.price == D("266720.6")
    sell = only(
        S2USDTAlloc(cfg.strategies.s2_usdtalloc).on_tick(
            make_ctx(cfg, fine, share="0.9", bid="266720.5", ask="266720.6")
        )
    )
    assert sell.price == D("266720.6")


@pytest.mark.parametrize(
    ("bid", "ask"), [("266720", "266720"), ("266730", "266720"), (None, "266720"), ("1", None)]
)
def test_crossed_or_one_sided_book_does_nothing(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo], bid: str | None, ask: str | None
) -> None:
    assert s2.on_tick(make_ctx(cfg, markets, bid=bid, ask=ask)) == []
    assert s2.diagnostics()["status"] in ("crossed_book", "no_book")
    assert s2.last_attempt_ts is None


# ---------------------------------------------------------------- reason text


def test_reason_text_has_the_numbers_prd_example(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    """PRD-03 §4: regime trend_up (ema20>ema60 by 0.4 %, 24h +2.4 %), target 65 % (plan) vs 48 % ->
    buy 850,000 TMN of USDT as maker at 266,721."""
    plan = make_plan(regime="trend_up", target="0.65", conf=0.8, evidence="dollar  broke out\nof its range")
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=plan, share="0.48", dollar=TREND_UP)))
    r = it.reason
    for part in (
        "USDTTMN regime=trend_up [plan, confidence 0.80]",
        "analyst: dollar broke out of its range",
        "rules would say trend_up",
        "ema20>ema60 by 0.40%",
        "24h +2.40%",
        "ATR15m 0.120% vs 24h median 0.100%",
        "target USDT share 65% (plan) vs current 48.0%",
        "gap +17.0 pp >= threshold 15 pp",
        "first S2 rebalance (none has filled yet)",  # 48 % USDT already: not "from 100% TMN"
        "buy 850,000 TMN of USDT (3.18 USDT, 17% of equity 5,000,000 TMN) as maker at 266,721",
        "(bid 266,720 / ask 266,758)",
    ):
        assert part in r, (part, r)
    assert "caps one order" not in r  # 17 % < 25 % cap: no cap note
    assert r.endswith(".")
    assert "n/a" not in r
    assert it.qty == D("3.18")


def test_reason_text_sell_and_rules(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, share="0.47", dollar=TREND_DOWN)))
    r = it.reason
    assert it.side == "SELL"
    for part in (
        "USDTTMN regime=trend_down [rules]",
        "ema20<ema60 by 0.30%",
        "24h -1.20%",
        "trend_flag -1 and 24h < -0.80%",
        "target USDT share 30% (rules table for trend_down) vs current 47.0%",
        "gap -17.0 pp >= threshold 15 pp",
        "sell 850,000 TMN of USDT (3.18 USDT, 17% of equity 5,000,000 TMN) as maker at 266,757",
    ):
        assert part in r, (part, r)


def test_reason_text_chaos_rule(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, share="0.6", dollar=CHAOS)))
    assert "regime=chaos [rules]" in it.reason
    assert "ATR15m 0.500% vs 24h median 0.200%; ATR15m > 2.0x its 24h median" in it.reason
    assert "target USDT share 40%" in it.reason


def test_long_plan_evidence_is_trimmed(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    it = only(s2.on_tick(make_ctx(cfg, markets, plan=make_plan(evidence="x" * 1000))))
    assert "x" * 157 + "..." in it.reason
    assert "x" * 158 not in it.reason


# ---------------------------------------------------------------- diagnostics


def test_diagnostics_report_the_evaluation(
    s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]
) -> None:
    s2.on_tick(make_ctx(cfg, markets, share="0.45", dollar=TREND_UP))
    d = s2.diagnostics()
    assert d["status"] == "intent"
    assert d["regime"] == "trend_up"
    assert d["rule_regime"] == "trend_up"
    assert D(d["target"]) == D("0.65")
    assert D(d["gap_pp"]) == D("0.2")
    assert d["side"] == "BUY"
    d["status"] = "mutated"  # a copy: callers cannot change the strategy's view
    assert s2.diagnostics()["status"] == "intent"
    json.dumps(s2.diagnostics())  # health log / TUI need plain JSON


# ---------------------------------------------------------------- state / determinism


def test_state_round_trip(s2: S2USDTAlloc, cfg: Config, markets: dict[str, MarketInfo]) -> None:
    fresh = S2USDTAlloc(cfg.strategies.s2_usdtalloc)
    fresh.load_state(None)
    assert fresh.state() == s2.state()
    only(s2.on_tick(make_ctx(cfg, markets, now=T0)))
    s2.on_fill(s2_fill(qty="2", ts=T0 + 8), None, make_ctx(cfg, markets, now=T0 + 8))
    saved = json.loads(json.dumps(s2.state()))
    assert saved["attempt_filled"] == "2"
    restored = S2USDTAlloc(cfg.strategies.s2_usdtalloc)
    restored.load_state(saved)
    assert restored.state() == s2.state()
    # both continue identically: the partial fill is remembered across the restart
    for strat in (s2, restored):
        strat.on_order_done(s2_order(filled="0"), make_ctx(cfg, markets, now=T0 + 45))
    assert restored.state() == s2.state()
    assert restored.last_rebalance_ts == T0 + 45
    a = s2.on_tick(make_ctx(cfg, markets, now=T0 + 45 + 1800, share="0.25"))
    b = restored.on_tick(make_ctx(cfg, markets, now=T0 + 45 + 1800, share="0.25"))
    assert [i.model_dump(mode="json") for i in a] == [i.model_dump(mode="json") for i in b]
    assert len(a) == 1


def _shuffled(d: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    items = list(d.items())
    rng.shuffle(items)
    return dict(items)


def test_determinism_same_inputs_same_bytes(cfg: Config, markets: dict[str, MarketInfo]) -> None:
    rng = random.Random(42)
    plan = make_plan(regime="trend_up", target="0.62", ts=T0 - 30)
    reference: bytes | None = None
    for _ in range(200):
        strat = S2USDTAlloc(cfg.strategies.s2_usdtalloc)
        ctx = make_ctx(cfg, _shuffled(markets, rng), plan=plan, share="0.31", dollar=_shuffled(TREND_UP, rng))
        out = strat.on_tick(ctx)
        blob = json.dumps(
            {
                "intents": [i.model_dump(mode="json") for i in out],
                "state": strat.state(),
                "diag": strat.diagnostics(),
            },
            sort_keys=True,
        ).encode()
        if reference is None:
            reference = blob
        assert blob == reference
    assert reference is not None and b'"side": "BUY"' in reference
