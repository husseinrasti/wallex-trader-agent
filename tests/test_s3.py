"""S3 RegimeMomentum (PRD-03 §5): 15-minute breakout + regime gate + risk sizing + exits.

Every context is built the way the engine builds it: 1-minute candle fixtures go through the real
``FeatureEngine`` (which resamples them into 15-minute bars), never hand-written feature values.

Base fixture (BTCUSDT): 24 range bars o=c=84,000, high 84,210, low 83,790 (true range 420), volume 10;
then the breakout bar (open 09:45, closes at T0 = 10:00 UTC) o=84,000 h=84,420 l=84,000 c=84,400,
volume 30. So ATR(14) = 420 exactly, hh20 = 84,210, volume ratio = 3.00, ATR% = 0.498 %.
Book 84,400.00 / 84,420.00 -> maker price 84,400.01. Equity 5,000,000 TMN at USDTTMN bid 250,000
= 20 USDT exactly; free USDT 10.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

import pytest

from wta.config import Config, S3Cfg
from wta.execution.positions import PortfolioState, PositionManager
from wta.features.engine import FeatureEngine
from wta.models import (
    Balance,
    BookSnapshot,
    Candle,
    Fill,
    Intent,
    Level,
    MarketInfo,
    MarketPlan,
    Order,
    OrderRequest,
    Position,
    RegimeCall,
)
from wta.risk.context import Deadline, EngineFlags, RiskContext
from wta.risk.manager import RiskManager
from wta.strategies.base import PortfolioView, StrategyContext
from wta.strategies.router import StrategyRouter
from wta.strategies.s3_momentum import S3Momentum
from wta.utils.decimalutil import D
from wta.utils.ids import make_client_id

from .conftest import T0

BAR = 900
BAR_TS = int(T0) - BAR  # the breakout bar: 09:45 -> 10:00 UTC
NOW = T0 + 5  # 5 s after the bar closed


# ---------------------------------------------------------------- candle fixtures


@dataclass(frozen=True)
class Bar:
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


def bar(o: object, h: object, l: object, c: object, v: object) -> Bar:  # noqa: E741
    return Bar(D(o), D(h), D(l), D(c), D(v))


def range_bars(price: object, rng: object, vol: object = 10, n: int = 24) -> list[Bar]:
    p, r = D(price), D(rng)
    return [bar(p, p + r / 2, p - r / 2, p, vol) for _ in range(n)]


def breakout_bar(price: object, rng: object, close_up: object, vol: object = 30) -> Bar:
    """Opens at ``price``, trades up ``rng`` (true range = rng after a close at ``price``)."""
    p = D(price)
    return bar(p, p + D(rng), p, p + D(close_up), vol)


def minutes(symbol: str, bar_ts: int, b: Bar) -> list[Candle]:
    """15 one-minute candles whose 15-minute resample is exactly ``b``."""
    out: list[Candle] = []
    prev = b.o
    per_v = (b.v / 15).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    for k in range(15):
        c = b.c if k == 14 else b.o + (b.c - b.o) * k / 14
        o = prev
        h = b.h if k == 7 else max(o, c)
        lo = b.l if k == 8 else min(o, c)
        v = per_v if k < 14 else b.v - per_v * 14
        out.append(Candle(symbol=symbol, resolution="1", ts=bar_ts + 60 * k, o=o, h=h, l=lo, c=c, v=v))
        prev = c
    return out


def candles_for(symbol: str, bars: list[Bar], last_bar_ts: int = BAR_TS, next_minute: bool = True) -> list[Candle]:
    """1-minute candles for ``bars`` (the last one opening at ``last_bar_ts``).

    ``next_minute`` adds the first minute of the following (still open) bar: proof that the bar's last
    minute was re-read after it closed, i.e. the bar is settled.
    """
    start = last_bar_ts - (len(bars) - 1) * BAR
    out: list[Candle] = []
    for i, b in enumerate(bars):
        out += minutes(symbol, start + i * BAR, b)
    if next_minute:
        c = bars[-1].c
        out.append(Candle(symbol=symbol, resolution="1", ts=last_bar_ts + BAR, o=c, h=c, l=c, c=c, v=D(1)))
    return out


def btc_bars(rng: object = 420, close_up: object = 400, vol: object = 30) -> list[Bar]:
    return [*range_bars(84000, rng), breakout_bar(84000, rng, close_up, vol)]


def eth_bars() -> list[Bar]:
    """ETH at 3,000: range 30 (1 %), breakout close 3,020 vs hh20 3,015 -> 0.17 ATR (weaker than BTC)."""
    return [*range_bars(3000, 30, vol=100), breakout_bar(3000, 30, 20, vol=300)]


def uptrend_bars(n: int = 100) -> list[Bar]:
    """n trending bars (+20 per bar, true range 420) and a breakout bar: trend_flag 1, 24h > +0.8 %."""
    out = []
    p0 = D(84000)
    for i in range(n):
        o = p0 + 20 * i
        out.append(bar(o, o + 220, o - 200, o + 20, 10))
    o = p0 + 20 * n
    out.append(bar(o, o + 420, o, o + 400, 30))
    return out


# ---------------------------------------------------------------- world


def book(symbol: str, bid: object, ask: object, ts: float = NOW, qty: object = "5") -> BookSnapshot:
    return BookSnapshot(
        symbol=symbol, ts=ts, source="rest", bids=[Level(price=D(bid), qty=D(qty))], asks=[Level(price=D(ask), qty=D(qty))]
    )


def default_books(now: float = NOW) -> dict[str, BookSnapshot]:
    return {
        "BTCUSDT": book("BTCUSDT", "84400.00", "84420.00", now),
        "ETHUSDT": book("ETHUSDT", "3020.00", "3021.00", now),
        "SOLUSDT": book("SOLUSDT", "150.00", "150.05", now),
        "USDTTMN": book("USDTTMN", "250000", "250010", now, qty="1000"),
    }


@pytest.fixture
def mk(markets: dict[str, MarketInfo]) -> dict[str, MarketInfo]:
    sol = MarketInfo(
        symbol="SOLUSDT", base="SOL", quote="USDT", tick_decimals=2, step_decimals=2, min_notional=D(1),
        maker_fee=D("0.001"), taker_fee=D("0.0013"),
    )
    return {**markets, "SOLUSDT": sol}


def view(
    positions: list[Position] | None = None,
    orders: list[Order] | None = None,
    free_usdt: object = "10",
    equity: object = "5000000",
    u_bid: object | None = "250000",
) -> PortfolioView:
    return PortfolioView(
        equity_tmn=D(equity),
        free={"TMN": D("2500000"), "USDT": D(free_usdt)},
        total={"TMN": D("2500000"), "USDT": D(free_usdt)},
        positions=tuple(positions or []),
        open_orders=tuple(orders or []),
        usdt_share=D("0.5"),
        coin_exposure_pct=D(0),
        u_bid=None if u_bid is None else D(u_bid),
    )


def plan(
    now: float = NOW,
    regimes: dict[str, tuple[str, float]] | None = None,
    enabled: tuple[str, ...] = ("S1", "S2", "S3"),
    mult: object = "1",
    age: float = 60,
) -> MarketPlan:
    regimes = {"BTCUSDT": ("trend_up", 0.7), "ETHUSDT": ("trend_up", 0.7)} if regimes is None else regimes
    return MarketPlan(
        ts=now - age,
        regimes={s: RegimeCall(regime=r, confidence=c) for s, (r, c) in regimes.items()},  # type: ignore[arg-type]
        enabled_strategies=list(enabled),
        size_multiplier=D(mult),
    )


def make_ctx(
    cfg: Config,
    mk: dict[str, MarketInfo],
    candles: dict[str, list[Candle]],
    now: float = NOW,
    the_plan: MarketPlan | None | str = "default",
    port: PortfolioView | None = None,
    books: dict[str, BookSnapshot] | None = None,
    pass_1m: bool = True,
    flatten_phase: int = 0,
) -> StrategyContext:
    books = default_books(now) if books is None else books
    feats = FeatureEngine(cfg, mk).compute(now, books, candles, sorted(candles))
    return StrategyContext(
        now=now,
        books=books,
        features=feats,
        candles={(s, "1"): c for s, c in candles.items()} if pass_1m else {},
        markets=mk,
        portfolio=port or view(),
        plan=plan(now) if the_plan == "default" else the_plan,  # type: ignore[arg-type]
        cfg=cfg,
        flatten_phase=flatten_phase,
    )


def s3(cfg: Config, ai_enabled: bool = True, **over: Any) -> S3Momentum:
    s3cfg = cfg.strategies.s3_momentum.model_copy(update={"enabled": True, **over})
    return S3Momentum(s3cfg, ai_enabled=ai_enabled)


def s3_position(
    symbol: str = "BTCUSDT", strategy: str = "S3", meta: dict[str, Any] | None = None, time_stop_ts: float | None = None
) -> Position:
    return Position(
        id=f"p_{strategy}_{symbol}_1", strategy=strategy, symbol=symbol, base=symbol.removesuffix("USDT"),
        quote="USDT", qty=D("0.000047"), entry_price=D("84400.01"), entry_ts=T0 - 3600,
        stop_price=D("83770.01"), target_price=D("85450.01"),
        time_stop_ts=T0 + 10_000 if time_stop_ts is None else time_stop_ts, meta=meta or {},
    )


def open_order(strategy: str, symbol: str, purpose: str = "entry", side: str = "BUY") -> Order:
    decision = f"d_{strategy}_{symbol}_open"
    req = OrderRequest(
        client_id=make_client_id(strategy, symbol, T0, decision), symbol=symbol, side=side, type="LIMIT",  # type: ignore[arg-type]
        qty=D("0.0001"), price=D("84000"), strategy=strategy, purpose=purpose, decision_id=decision,  # type: ignore[arg-type]
    )
    return Order(req=req, created_ts=T0 - 5, updated_ts=T0 - 5)


@pytest.fixture
def base(cfg: Config, mk: dict[str, MarketInfo]) -> StrategyContext:
    return make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())})


# ---------------------------------------------------------------- fixture sanity


def test_fixture_features_are_what_the_docstring_says(base: StrategyContext) -> None:
    f = base.features.symbols["BTCUSDT"]
    assert f["last_bar_ts_15m"] == BAR_TS
    assert f["last_close_15m"] == D("84400")
    assert f["hh20_15m"] == D("84210")
    assert f["vol_ratio_15m"] == D(3)
    assert f["atr_15m"] == D(420)
    assert f["atr_15m_pct"] == D(420) / D(84400)


# ---------------------------------------------------------------- breakout detection + sizing


def test_breakout_entry_exact_size_stop_target(cfg: Config, base: StrategyContext) -> None:
    out = s3(cfg).on_tick(base)
    assert len(out) == 1
    it = out[0]
    assert (it.strategy, it.symbol, it.side, it.order_type, it.purpose, it.taker) == (
        "S3", "BTCUSDT", "BUY", "LIMIT", "entry", False,
    )
    # maker-first: best bid 84,400.00 + 1 tick
    assert it.price == D("84400.01")
    # equity 20 USDT; risk 0.5 % x 20 / (1.5 x 420) = 0.000158 BTC; cap 20 % x 20 / 84,400.01 = 0.0000473..
    assert D(it.features["qty_risk"]) == D("0.005") * 20 / D(630)
    assert D(it.features["qty_cap"]) == D("0.20") * 20 / D("84400.01")
    assert it.features["size_binding"] == "max_size"
    assert it.qty == D("0.000047")
    assert it.notional == D("0.000047") * D("84400.01")
    # stop = entry - 1.5 ATR, target = entry + 2.5 ATR
    assert it.stop_price == D("83770.01")
    assert it.features["target_price"] == "85450.01"
    assert it.features["stop_price"] == "83770.01"
    assert it.features["time_stop_s"] == 14400
    assert it.expected_edge_pct == D(1050) / D("84400.01")
    assert it.features["cost_model"] == "roundtrip"
    assert it.features["margin_min_pct"] == "0"
    assert it.features["books_used"] == ["BTCUSDT", "USDTTMN"]
    assert it.features["regime"] == "trend_up"
    assert it.features["atr_pct"] == it.features["atr_15m_pct"]
    assert it.linked_position_id is None
    json.dumps(it.model_dump(mode="json"))  # decision-log safe


def test_reason_has_every_number_a_judge_needs(cfg: Config, base: StrategyContext) -> None:
    reason = s3(cfg).on_tick(base)[0].reason
    for part in (
        "BTCUSDT",
        "2026-10-22 09:45 UTC",
        "closed at 84,400.00",
        "high 84,210.00",
        "3.00x their average volume (>= 1.5x)",
        "ATR(14) 420.00 = 0.498% of the close inside [0.3%, 3.0%]",
        "plan regime trend_up with confidence 0.70 (>= min 0.60)",
        "Buy 0.000047 BTC at 84,400.01 as maker",
        "risk 0.50% of 20.00 USDT equity",
        "stop distance 630.00 (1.5 ATR) = 0.000158",
        "sized by the 20%-of-equity cap",
        "server STOP_LIMIT stop 83,770.01 (entry - 1.5 ATR)",
        "software target 85,450.01 (entry + 2.5 ATR = +1.24% expected edge)",
        "time stop 4 h",
    ):
        assert part in reason, part
    assert reason.endswith(".")


def test_sizing_risk_budget_binds(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}, port=view(free_usdt="15"))
    it = s3(cfg, max_size_pct_equity=D(1)).on_tick(ctx)[0]
    # 0.1 USDT risk / 630 USDT stop distance = 0.00015873.. -> rounded down to the 6-decimal step
    assert it.qty == D("0.000158")
    assert it.features["size_binding"] == "risk"
    assert "sized by the risk budget" in it.reason
    # the stop really risks <= 0.5 % of equity
    assert it.qty * (it.price - it.stop_price) <= D("0.1")  # type: ignore[operator]


def test_sizing_capped_by_free_usdt(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}, port=view(free_usdt="2"))
    it = s3(cfg).on_tick(ctx)[0]
    usable = D(2) * (1 - cfg.risk.balance_buffer_pct)
    assert D(it.features["qty_free"]) == usable / (D("84400.01") * D("1.001"))
    assert it.qty == D("0.000023")
    assert it.features["size_binding"] == "free_usdt"


def test_sizing_scaled_by_plan_size_multiplier(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}, the_plan=plan(mult="0.5"))
    it = s3(cfg).on_tick(ctx)[0]
    assert it.qty == D("0.000023")  # 0.0000473.. x 0.5 rounded down
    assert it.features["plan_size_multiplier"] == "0.5"
    assert "plan size mult 0.50" in it.reason


def test_size_below_min_notional_no_entry(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}, port=view(free_usdt="0.5"))
    strat = s3(cfg)
    assert strat.on_tick(ctx) == []
    assert "below the minimum" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


@pytest.mark.parametrize(("vol", "expect"), [("14", False), ("15", True), ("30", True)])
def test_volume_ratio_threshold(cfg: Config, mk: dict[str, MarketInfo], vol: str, expect: bool) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars(vol=vol))})
    strat = s3(cfg)
    assert bool(strat.on_tick(ctx)) is expect
    if not expect:
        assert "volume ratio 1.40 < 1.5" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


@pytest.mark.parametrize(
    ("rng", "close_up", "why"),
    [
        (200, 190, "outside [0.3%, 3.0%]"),  # ATR 200 / 84,190 = 0.24 %
        (3000, 2900, "outside [0.3%, 3.0%]"),  # ATR 3,000 / 86,900 = 3.45 %
    ],
)
def test_atr_out_of_band_no_entry(
    cfg: Config, mk: dict[str, MarketInfo], rng: int, close_up: int, why: str
) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars(rng=rng, close_up=close_up))})
    f = ctx.features.symbols["BTCUSDT"]
    assert f["atr_15m"] == D(rng) and f["last_close_15m"] > f["hh20_15m"] and f["vol_ratio_15m"] == 3
    strat = s3(cfg)
    assert strat.on_tick(ctx) == []
    assert why in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


@pytest.mark.parametrize("close_up", [210, 100])  # close == hh20 (84,210) and close below it
def test_close_not_above_hh20_no_entry(cfg: Config, mk: dict[str, MarketInfo], close_up: int) -> None:
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars(close_up=close_up))})
    strat = s3(cfg)
    assert strat.on_tick(ctx) == []
    assert "<= hh20 84210" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def test_warming_up_consumes_the_bar(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    short = [*range_bars(84000, 420, n=10), breakout_bar(84000, 420, 400)]
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", short)})
    strat = s3(cfg)
    assert strat.on_tick(ctx) == []
    assert strat.diagnostics()["symbols"]["BTCUSDT"]["status"].startswith("warming up")
    assert strat.state() == {"last_bar_seen": {"BTCUSDT": BAR_TS}}


# ---------------------------------------------------------------- regime gate


def _btc(cfg: Config, mk: dict[str, MarketInfo], **kw: Any) -> StrategyContext:
    return make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}, **kw)


def test_no_plan_and_rules_only_off_no_entry(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    assert strat.on_tick(_btc(cfg, mk, the_plan=None)) == []
    status = strat.diagnostics()["symbols"]["BTCUSDT"]["status"]
    assert "regime gate closed: no fresh plan and rules-only mode is off" in status


def test_stale_plan_counts_as_no_plan(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    stale = plan(age=cfg.ai.plan_stale_s + 1)
    assert s3(cfg).on_tick(_btc(cfg, mk, the_plan=stale)) == []
    assert len(s3(cfg).on_tick(_btc(cfg, mk, the_plan=plan(age=cfg.ai.plan_stale_s)))) == 1


@pytest.mark.parametrize(("conf", "expect"), [(0.59, False), (0.6, True), (0.95, True)])
def test_plan_confidence_threshold(cfg: Config, mk: dict[str, MarketInfo], conf: float, expect: bool) -> None:
    p = plan(regimes={"BTCUSDT": ("trend_up", conf)})
    strat = s3(cfg)
    assert bool(strat.on_tick(_btc(cfg, mk, the_plan=p))) is expect
    if not expect:
        assert "confidence 0.59 (< min 0.60)" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


@pytest.mark.parametrize("regime", ["range", "trend_down", "chaos"])
def test_plan_regime_other_than_trend_up_no_entry(cfg: Config, mk: dict[str, MarketInfo], regime: str) -> None:
    assert s3(cfg).on_tick(_btc(cfg, mk, the_plan=plan(regimes={"BTCUSDT": (regime, 0.9)}))) == []


def test_plan_without_call_for_symbol_no_entry(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    assert strat.on_tick(_btc(cfg, mk, the_plan=plan(regimes={"ETHUSDT": ("trend_up", 0.9)}))) == []
    assert "no regime call for BTCUSDT" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def test_plan_disabling_s3_or_zero_size_blocks_entries(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    assert strat.on_tick(_btc(cfg, mk, the_plan=plan(enabled=("S1", "S2")))) == []
    assert strat.diagnostics()["status"].startswith("disabled by the plan")
    strat = s3(cfg)
    assert strat.on_tick(_btc(cfg, mk, the_plan=plan(mult="0"))) == []
    assert "size multiplier is 0" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def test_ai_disabled_ignores_the_plan_regime(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    assert s3(cfg, ai_enabled=False).on_tick(_btc(cfg, mk)) == []


def _uptrend(cfg: Config, mk: dict[str, MarketInfo], **kw: Any) -> StrategyContext:
    up = uptrend_bars()
    books = default_books()
    close = up[-1].c
    books["BTCUSDT"] = book("BTCUSDT", close, close + 20)
    ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", up)}, books=books, **kw)
    f = ctx.features.symbols["BTCUSDT"]
    assert f["trend_flag"] == 1 and f["ret_24h"] > D("0.008")  # fixture sanity
    assert f["last_close_15m"] > f["hh20_15m"] and f["vol_ratio_15m"] >= D("1.5")
    return ctx


def test_rules_only_mode_trades_a_rules_trend_up(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    out = s3(cfg, allow_rules_only=True).on_tick(_uptrend(cfg, mk, the_plan=None))
    assert len(out) == 1
    assert "rules-only regime trend_up (no fresh plan;" in out[0].reason
    assert out[0].features["regime_source"] == "rules"
    assert out[0].features["regime_confidence"] is None


def test_rules_only_flag_off_or_no_trend_no_entry(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    assert s3(cfg).on_tick(_uptrend(cfg, mk, the_plan=None)) == []
    strat = s3(cfg, allow_rules_only=True)
    assert strat.on_tick(_btc(cfg, mk, the_plan=None)) == []  # flat history: trend flag 0
    assert "rules-only regime not_trend_up" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def test_plan_call_overrides_rules_only(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    p = plan(regimes={"BTCUSDT": ("trend_down", 0.8)})
    assert s3(cfg, allow_rules_only=True).on_tick(_uptrend(cfg, mk, the_plan=p)) == []


# ---------------------------------------------------------------- once per completed bar


def test_once_per_bar(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    bars = btc_bars()
    assert len(strat.on_tick(make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", bars)}))) == 1
    # same bar, later ticks, still no position: nothing new
    for dt in (2, 30, 300):
        ctx = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", bars)}, now=NOW + dt)
        assert strat.on_tick(ctx) == []
    # the next bar breaks out again (above the first breakout's high 84,420): evaluated once more
    bars2 = [*bars, breakout_bar(84400, 420, 400)]
    now2 = NOW + BAR
    ctx2 = make_ctx(cfg, mk, {"BTCUSDT": candles_for("BTCUSDT", bars2, last_bar_ts=BAR_TS + BAR)}, now=now2)
    ctx2.books["BTCUSDT"] = book("BTCUSDT", "84800.00", "84820.00", now2)
    out = strat.on_tick(ctx2)
    assert len(out) == 1 and out[0].features["bar_ts_15m"] == BAR_TS + BAR
    assert strat.on_tick(ctx2) == []


def test_bar_waits_until_settled(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    raw = {"BTCUSDT": candles_for("BTCUSDT", btc_bars(), next_minute=False)}
    assert strat.on_tick(make_ctx(cfg, mk, raw, now=T0 + 5)) == []
    assert "settle" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]
    assert strat.state() == {"last_bar_seen": {}}  # not consumed
    settle = 2 * cfg.data.candle_poll_interval_s
    assert strat.on_tick(make_ctx(cfg, mk, raw, now=T0 + settle - 1)) == []
    assert len(strat.on_tick(make_ctx(cfg, mk, raw, now=T0 + settle))) == 1


def test_next_minute_candle_settles_the_bar_at_once(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    raw = {"BTCUSDT": candles_for("BTCUSDT", btc_bars(), next_minute=True)}
    assert len(s3(cfg).on_tick(make_ctx(cfg, mk, raw, now=T0 + 2))) == 1
    # without 1-minute candles in the context only the time rule settles it
    assert s3(cfg).on_tick(make_ctx(cfg, mk, raw, now=T0 + 2, pass_1m=False)) == []
    assert len(s3(cfg).on_tick(make_ctx(cfg, mk, raw, now=T0 + 120, pass_1m=False))) == 1


def test_stale_bar_is_skipped(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    raw = {"BTCUSDT": candles_for("BTCUSDT", btc_bars(), next_minute=False)}
    strat = s3(cfg)
    ctx = make_ctx(cfg, mk, raw, now=T0 + BAR + 10)  # no candle of the next bar arrived for 15 min
    assert ctx.features.symbols["BTCUSDT"]["last_bar_ts_15m"] == BAR_TS
    assert strat.on_tick(ctx) == []
    assert strat.diagnostics()["symbols"]["BTCUSDT"]["status"].startswith("stale bar")
    assert strat.state() == {"last_bar_seen": {"BTCUSDT": BAR_TS}}


def test_missing_book_does_not_consume_the_bar(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    books = default_books()
    del books["BTCUSDT"]
    assert strat.on_tick(_btc(cfg, mk, books=books)) == []
    assert strat.state() == {"last_bar_seen": {}}
    assert len(strat.on_tick(_btc(cfg, mk, now=NOW + 2))) == 1


# ---------------------------------------------------------------- one S3 position globally


def test_open_s3_position_blocks_and_consumes_the_bar(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    pos = s3_position("ETHUSDT")
    assert strat.on_tick(_btc(cfg, mk, port=view(positions=[pos]))) == []
    assert "S3 position p_S3_ETHUSDT_1 is open" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]
    # the position closed a minute later: the old breakout is not acted on late
    assert strat.on_tick(_btc(cfg, mk, now=NOW + 60)) == []


def test_open_s3_order_blocks_entries(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    assert strat.on_tick(_btc(cfg, mk, port=view(orders=[open_order("S3", "SOLUSDT")]))) == []
    assert "S3 order S3_SOLUSDT_" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def _two(cfg: Config, mk: dict[str, MarketInfo], **kw: Any) -> StrategyContext:
    candles = {"BTCUSDT": candles_for("BTCUSDT", btc_bars()), "ETHUSDT": candles_for("ETHUSDT", eth_bars())}
    return make_ctx(cfg, mk, candles, **kw)


def test_two_breakouts_take_the_stronger_one(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    out = strat.on_tick(_two(cfg, mk))
    assert [i.symbol for i in out] == ["BTCUSDT"]  # 0.45 ATR beats ETH's 0.17 ATR
    assert out[0].features["other_breakouts"] == ["ETHUSDT"]
    assert "Also breaking out on this bar (weaker in ATR units, one S3 position at a time): ETHUSDT." in out[0].reason
    assert "BTCUSDT is stronger" in strat.diagnostics()["symbols"]["ETHUSDT"]["status"]
    assert strat.on_tick(_two(cfg, mk, now=NOW + 2)) == []  # both bars consumed


def test_equal_strength_tie_breaks_by_symbol(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    # ETH at 4,200 is the BTC fixture scaled by 1/20: identical breakout strength 19/42 ATR
    eth = [*range_bars(4200, 21, vol=10), breakout_bar(4200, 21, 20, vol=30)]
    books = default_books()
    books["ETHUSDT"] = book("ETHUSDT", "4220.00", "4221.00")
    candles = {"ETHUSDT": candles_for("ETHUSDT", eth), "BTCUSDT": candles_for("BTCUSDT", btc_bars())}
    out = s3(cfg).on_tick(make_ctx(cfg, mk, candles, books=books))
    assert [i.symbol for i in out] == ["BTCUSDT"]
    assert out[0].features["breakout_atr"] == str(D(190) / D(420))


def test_symbol_owned_by_another_strategy_is_skipped(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    out = strat.on_tick(_two(cfg, mk, port=view(positions=[s3_position("BTCUSDT", strategy="S1")])))
    assert [i.symbol for i in out] == ["ETHUSDT"]
    assert "held or worked by S1" in strat.diagnostics()["symbols"]["BTCUSDT"]["status"]


def test_entry_passes_the_strategy_router(cfg: Config, base: StrategyContext) -> None:
    router = StrategyRouter([s3(cfg)], cfg)
    out = router.tick(base)
    assert [i.symbol for i in out] == ["BTCUSDT"] and router.dropped == []


# ---------------------------------------------------------------- exits: software target, time stop


def _open_position(cfg: Config, mk: dict[str, MarketInfo], it: Intent) -> tuple[PositionManager, Position]:
    port = PortfolioState(initial_equity_tmn=D("5000000"))
    pm = PositionManager(port, mk)
    req = OrderRequest(
        client_id=make_client_id("S3", it.symbol, it.ts, it.id), symbol=it.symbol, side="BUY", type="LIMIT",
        qty=it.qty, price=it.price, strategy="S3", purpose="entry", decision_id=it.id,  # type: ignore[arg-type]
    )
    fill = Fill(order_client_id=req.client_id, symbol=it.symbol, side="BUY", price=it.price, qty=it.qty, ts=NOW + 30)  # type: ignore[arg-type]
    pos = pm.on_fill(fill, req, default_books(), intent=it)
    assert isinstance(pos, Position)
    return pm, pos


def test_position_gets_stop_target_and_time_stop(cfg: Config, mk: dict[str, MarketInfo], base: StrategyContext) -> None:
    it = s3(cfg).on_tick(base)[0]
    _, pos = _open_position(cfg, mk, it)
    assert pos.stop_price == D("83770.01")
    assert pos.target_price == D("85450.01")
    assert pos.time_stop_ts == NOW + 30 + 4 * 3600
    assert pos.meta["regime"] == "trend_up" and pos.meta["atr_pct"] == it.features["atr_15m_pct"]


@pytest.mark.parametrize(
    ("bid", "dt", "reason"),
    [
        ("84500.00", 4 * 3600, "time_stop"),  # between stop and target, 4 h after the fill
        ("85450.01", 60, "target"),  # software target (no resting target SELL)
        ("84500.00", 4 * 3600 - 1, None),
    ],
)
def test_position_manager_exits(
    cfg: Config, mk: dict[str, MarketInfo], base: StrategyContext, bid: str, dt: float, reason: str | None
) -> None:
    pm, pos = _open_position(cfg, mk, s3(cfg).on_tick(base)[0])
    now = NOW + 30 + dt
    books = default_books(now)
    books["BTCUSDT"] = book("BTCUSDT", bid, D(bid) + D("20"), now)
    exits = pm.check_exits(books, now, software_stops=False)
    assert [e.features["exit_reason"] for e in exits] == ([reason] if reason else [])


def test_expected_edge_feeds_edge_vs_cost(cfg: Config, mk: dict[str, MarketInfo], base: StrategyContext) -> None:
    port = PortfolioState(initial_equity_tmn=D("5000000"))
    port.set_balances({"TMN": Balance(asset="TMN", free=D("2500000")), "USDT": Balance(asset="USDT", free=D("10"))})
    books = default_books()
    port.refresh(books, NOW)
    rctx = RiskContext(
        now=NOW, portfolio=port, books=books, markets=mk, cfg=cfg, flags=EngineFlags(),
        deadline=Deadline(None, None, None, None),
    )

    def edge_check(it: Intent) -> Any:
        return next(c for c in RiskManager(cfg).evaluate(it, rctx).checks if c.code == "EDGE_VS_COST")

    wide = s3(cfg).on_tick(base)[0]  # target +1.24 % vs round trip 0.10 % + 0.13 % + spread 0.024 %
    assert edge_check(wide).passed
    narrow = s3(cfg, target_atr_mult=D("0.1")).on_tick(base)[0]  # target +0.05 %
    assert narrow.expected_edge_pct == D(42) / D("84400.01")
    check = edge_check(narrow)
    assert not check.passed and "edge too small" in check.detail


# ---------------------------------------------------------------- regime flip


def _flip_ctx(
    cfg: Config, mk: dict[str, MarketInfo], regime: str = "trend_down", pos: Position | None = None, **kw: Any
) -> StrategyContext:
    p = kw.pop("the_plan", plan(regimes={"BTCUSDT": (regime, 0.7)}))
    return _btc(cfg, mk, the_plan=p, port=view(positions=[pos or s3_position()]), **kw)


@pytest.mark.parametrize("regime", ["trend_down", "chaos"])
def test_regime_flip_exit(cfg: Config, mk: dict[str, MarketInfo], regime: str) -> None:
    pos = s3_position()
    out = s3(cfg).on_tick(_flip_ctx(cfg, mk, regime, pos))
    assert len(out) == 1
    it = out[0]
    assert (it.side, it.order_type, it.purpose, it.taker, it.price) == ("SELL", "LIMIT", "exit", True, None)
    assert it.qty == pos.qty and it.linked_position_id == pos.id
    assert it.features["exit_reason"] == "regime_flip" and it.features["regime"] == regime
    for part in (f"now calls {regime} (confidence 0.70)", "bought at 84,400.01", "bid now 84,400.00",
                 "stop 83,770.01", "target 85,450.01", "regime flip"):
        assert part in it.reason, part


@pytest.mark.parametrize("regime", ["trend_up", "range"])
def test_no_regime_flip_while_regime_holds(cfg: Config, mk: dict[str, MarketInfo], regime: str) -> None:
    assert s3(cfg).on_tick(_flip_ctx(cfg, mk, regime)) == []


def test_regime_flip_fires_even_when_s3_is_disabled(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    disabled_by_plan = plan(regimes={"BTCUSDT": ("chaos", 0.9)}, enabled=("S2",), mult="0")
    assert len(s3(cfg).on_tick(_flip_ctx(cfg, mk, the_plan=disabled_by_plan))) == 1
    off = S3Momentum(cfg.strategies.s3_momentum)  # enabled: false
    assert [i.features["exit_reason"] for i in off.on_tick(_flip_ctx(cfg, mk))] == ["regime_flip"]


def test_regime_flip_suppressed_cases(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    strat = s3(cfg)
    # a closing SELL is already working
    assert strat.on_tick(_flip_ctx(cfg, mk, pos=s3_position(meta={"exit_pending": True}))) == []
    # stale plan: nothing to act on (stop/target/time stop still protect the position)
    stale = plan(regimes={"BTCUSDT": ("trend_down", 0.9)}, age=cfg.ai.plan_stale_s + 1)
    assert strat.on_tick(_flip_ctx(cfg, mk, the_plan=stale)) == []
    # the PositionManager's own time stop fires this tick: no second SELL
    assert strat.on_tick(_flip_ctx(cfg, mk, pos=s3_position(time_stop_ts=NOW))) == []
    # another strategy's position on the symbol is not S3's business
    assert strat.on_tick(_flip_ctx(cfg, mk, pos=s3_position(strategy="S1"))) == []
    # flatten phase: the flatten routine owns every position
    assert strat.on_tick(_flip_ctx(cfg, mk, flatten_phase=1)) == []


def test_regime_flip_skipped_when_target_already_hit(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    books = default_books()
    books["BTCUSDT"] = book("BTCUSDT", "85500.00", "85520.00")
    assert s3(cfg).on_tick(_flip_ctx(cfg, mk, books=books)) == []


# ---------------------------------------------------------------- config, determinism, state


def test_disabled_by_default_config(cfg: Config, base: StrategyContext) -> None:
    assert cfg.strategies.s3_momentum.enabled is False
    strat = S3Momentum(cfg.strategies.s3_momentum)
    assert strat.on_tick(base) == []
    assert strat.diagnostics()["status"] == "disabled by config"
    assert strat.state() == {"last_bar_seen": {}}


def test_flatten_phase_no_entries(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    assert s3(cfg).on_tick(_btc(cfg, mk, flatten_phase=1)) == []


def _shuffled(d: dict[Any, Any], rng: random.Random) -> dict[Any, Any]:
    items = list(d.items())
    rng.shuffle(items)
    return dict(items)


def _dump(intents: list[Intent]) -> str:
    return json.dumps([i.model_dump(mode="json") for i in intents], sort_keys=True)


def test_deterministic_under_shuffled_dict_orders(cfg: Config, mk: dict[str, MarketInfo]) -> None:
    candles = {"BTCUSDT": candles_for("BTCUSDT", btc_bars()), "ETHUSDT": candles_for("ETHUSDT", eth_bars())}
    held = s3_position("SOLUSDT").model_copy(
        update={"qty": D("0.03"), "entry_price": D("151.00"), "stop_price": D("148.00"), "target_price": D("156.00")}
    )
    p = plan(regimes={"BTCUSDT": ("trend_up", 0.7), "ETHUSDT": ("trend_up", 0.8), "SOLUSDT": ("chaos", 0.6)})

    def run(rng: random.Random | None) -> str:
        out: list[Intent] = []
        states = []
        for positions in ([], [held]):  # entries (two breakouts), then the regime-flip exit of a held position
            books, markets, cs = default_books(), dict(mk), dict(candles)
            regimes = dict(p.regimes)
            if rng is not None:
                books, markets, cs, regimes = (_shuffled(x, rng) for x in (books, markets, cs, regimes))
            the_plan = p.model_copy(update={"regimes": regimes})
            syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
            if rng is not None:
                rng.shuffle(syms)
            strat = s3(cfg, symbols=syms)
            ctx = make_ctx(cfg, markets, cs, the_plan=the_plan, books=books, port=view(positions=positions))
            out += strat.on_tick(ctx)
            states.append(strat.state())
        return _dump(out) + json.dumps(states, sort_keys=True)

    baseline = run(None)
    assert '"regime_flip"' in baseline and '"entry"' in baseline
    rng = random.Random(1234)
    for _ in range(25):
        assert run(rng) == baseline


def test_state_round_trip(cfg: Config, base: StrategyContext) -> None:
    strat = s3(cfg)
    assert len(strat.on_tick(base)) == 1
    state = json.loads(json.dumps(strat.state()))
    assert state == {"last_bar_seen": {"BTCUSDT": BAR_TS}}
    restored = s3(cfg)
    restored.load_state(state)
    assert restored.state() == strat.state()
    assert restored.on_tick(base) == []  # a restart does not re-enter on the same bar
    fresh = s3(cfg)
    fresh.load_state(None)
    assert fresh.state() == {"last_bar_seen": {}}
    fresh.load_state({"last_bar_seen": {"BTCUSDT": str(BAR_TS)}})
    assert fresh.state() == {"last_bar_seen": {"BTCUSDT": BAR_TS}}


def test_cfg_type(cfg: Config) -> None:
    assert isinstance(cfg.strategies.s3_momentum, S3Cfg)
