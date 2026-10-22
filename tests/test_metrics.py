"""PRD-05 §4 metrics: known series -> hand-computed numbers (backtest/metrics.py).

Every expected value below is derived by hand in the comment next to it; ``*_pct`` values are
fractions (0.01 = 1 %).
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.metrics import (
    Metrics,
    ablation_delta,
    compute_metrics,
    drawdown,
    hourly_returns,
    read_csv_rows,
    read_equity_table,
    realized_vs_unrealized,
    trades_from_decisions,
    vetoes_by_code,
)
from wta.data.store import Store
from wta.logging_.decision_log import DecisionLog, merge_decisions
from wta.logging_.jsonl import iter_family
from wta.models import Balance, MarketPlan, RegimeCall, RiskCheck, RiskDecision
from wta.utils.timeutil import SimClock, iso

T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z (hour aligned)
H = 3600.0
MIDNIGHT = T0 + 14 * H  # 2026-10-23T00:00:00Z
ANN = math.sqrt(24 * 365)  # 93.59487165438073


def approx(x: float) -> object:
    return pytest.approx(x, rel=1e-9, abs=1e-12)


# ---------------------------------------------------------------- equity curve


def test_known_equity_series_return_drawdown_and_duration() -> None:
    # hourly: 100 -> 110 (peak) -> 99 (-10 %) -> 105 -> 110 (recovered, 3 h after the peak) -> 120
    points = [(T0 + i * H, e) for i, e in enumerate([100, 110, 99, 105, 110, 120])]
    m = compute_metrics(points, initial_equity=100)
    assert m.n_equity_points == 6 and not m.insufficient_data
    assert m.start_ts == T0 and m.end_ts == T0 + 5 * H
    assert m.start_iso == "2026-10-22T10:00:00.000Z" and m.end_iso == "2026-10-22T15:00:00.000Z"
    assert m.initial_equity_tmn == 100 and m.final_equity_tmn == 120
    assert m.pnl_tmn == 20
    assert m.return_pct == approx(0.2)  # 120 / 100 - 1
    assert m.max_drawdown_pct == approx(0.1)  # (110 - 99) / 110
    assert m.drawdown_duration_max_s == 3 * H  # peak at +1h, back to 110 at +4h
    assert m.daily_returns == {"2026-10-22": approx(0.2)}
    assert m.n_hourly_returns == 5
    assert m.notes == []


def test_drawdown_duration_unrecovered_longest_and_initial_peak() -> None:
    # never recovers: peak 100 at t=0, still below at the end (t=1800)
    assert drawdown([(0, 100), (600, 90), (1800, 95)]) == (approx(0.1), 1800)
    # two underwater periods: 0..200 (200 s, -5 %) and 200..1400 (1200 s, -2 %) -> longest 1200 s
    dd, dur = drawdown([(0, 100), (100, 95), (200, 100), (300, 99), (1300, 98), (1400, 101)])
    assert dd == approx(0.05) and dur == 1200
    # initial equity 100 above the first row (fees of the first fill): peak 100 at the first ts
    m = compute_metrics([(T0, 98), (T0 + 60, 99), (T0 + 120, 100.5)], initial_equity=Decimal("100"))
    assert m.max_drawdown_pct == approx(0.02)  # (100 - 98) / 100
    assert m.drawdown_duration_max_s == 120  # back above 100 at T0 + 120
    assert m.return_pct == approx(0.005)
    # equal to the peak counts as recovered: peak at t=0, back at 100 at t=20 -> 20 s
    assert drawdown([(0, 100), (10, 99), (20, 100), (30, 100)]) == (approx(0.01), 20)


def test_daily_returns_split_at_utc_midnight() -> None:
    points = [
        (MIDNIGHT - H, 100.5),
        (MIDNIGHT - 10, 101.0),  # last row of 2026-10-22
        (MIDNIGHT, 102.0),  # exactly 00:00:00Z belongs to 2026-10-23
        (MIDNIGHT + 12 * H, 99.96),
    ]
    trades = [
        {"strategy": "S1", "symbol": "BTCUSDT", "pnl_tmn": "10", "closed_ts": MIDNIGHT - 1, "fees_tmn": "1"},
        {"strategy": "S1", "symbol": "BTCUSDT", "pnl_tmn": "-4", "closed_ts": MIDNIGHT, "fees_tmn": "2"},
        {"strategy": "S1", "symbol": "BTCUSDT", "pnl_tmn": "6", "closed_ts": MIDNIGHT + 60, "fees_tmn": "2"},
    ]
    m = compute_metrics(points, trades, initial_equity=100)
    # day 1: 101 / 100 - 1; day 2: 99.96 / 101 - 1 = -0.0102970297...
    assert m.daily_returns == {"2026-10-22": approx(0.01), "2026-10-23": approx(-0.010297029702970306)}
    d1, d2 = m.per_day["2026-10-22"], m.per_day["2026-10-23"]
    assert (d1["start_equity_tmn"], d1["end_equity_tmn"]) == (100, 101.0)
    assert (d2["start_equity_tmn"], d2["end_equity_tmn"]) == (101.0, 99.96)
    assert (d1["n_trades"], d1["wins"], d1["pnl_tmn"], d1["fees_tmn"]) == (1, 1, 10.0, 1.0)
    assert (d2["n_trades"], d2["wins"], d2["pnl_tmn"], d2["fees_tmn"]) == (2, 1, 2.0, 4.0)
    assert list(m.per_day) == ["2026-10-22", "2026-10-23"]


def test_sharpe_and_sortino_from_hourly_returns() -> None:
    # hourly returns +2 %, -1 %, +2 %, -1 %: mean 0.005, sample sd sqrt(4 * 0.015^2 / 3) = sqrt(0.0003)
    # downside deviation sqrt((0.01^2 + 0.01^2) / 4) = sqrt(0.00005); annualised by sqrt(8760)
    equity = [100.0, 102.0, 100.98, 102.9996, 101.969604]
    m = compute_metrics([(T0 + i * H, e) for i, e in enumerate(equity)])
    assert m.n_hourly_returns == 4
    assert m.sharpe_annualized == approx(0.005 / math.sqrt(0.0003) * ANN)  # 27.0185121722...
    assert m.sharpe_annualized == pytest.approx(27.018512172212592, rel=1e-9)
    assert m.sortino == pytest.approx(66.18156843109719, rel=1e-9)  # 0.005 / sqrt(0.00005) * sqrt(8760)
    assert m.sharpe_note == "0.2 days of data (4 hourly returns) — do not over-interpret"
    assert "initial equity = first equity row (no initial_equity given)" in m.notes


def test_hourly_sampling_marks_and_partial_last_hour() -> None:
    # 10-minute rows from 10:05 to 12:35: marks 11:00 and 12:00, plus the partial hour ending 12:35
    points = [(T0 + 300 + i * 600, 100.0 + i) for i in range(16)]  # last row at 12:35 -> 115
    rets = hourly_returns(points, start_equity=100.0)
    # value at 11:00 = row at 10:55 (i=5) = 105; at 12:00 = row at 11:55 (i=11) = 111; end = 115
    assert rets == [approx(0.05), approx(111 / 105 - 1), approx(115 / 111 - 1)]
    # constant equity: zero dispersion -> no ratios (never inf / NaN)
    flat = compute_metrics([(T0 + i * H, 100) for i in range(5)])
    assert flat.sharpe_annualized is None and flat.sortino is None and flat.max_drawdown_pct == 0
    # only gains: Sortino undefined (no downside), Sharpe defined
    up = compute_metrics([(T0 + i * H, e) for i, e in enumerate([100, 101, 103, 104])])
    assert up.sortino is None and up.sharpe_annualized is not None
    # a seconds row next to a milliseconds row (unit error): no hourly series instead of a huge loop
    assert hourly_returns([(T0, 100.0), (T0 * 1000, 101.0)]) == []


# ---------------------------------------------------------------- trades


def _four_trades() -> list[dict[str, object]]:
    return [
        {"strategy": "S1", "symbol": "BTCUSDT", "pnl_tmn": "1000", "pnl_pct": "0.002", "hold_s": 60,
         "fees_tmn": "50", "entry_ts": T0, "closed_ts": T0 + 60, "exit_reason": "lockin"},
        {"strategy": "S1", "symbol": "ETHUSDT", "pnl_tmn": Decimal("-400"), "pnl_pct": "-0.0008",
         "hold_s": "120", "fees_tmn": 50, "entry_ts": T0 + 100, "closed_ts": T0 + 220, "exit_reason": "stop"},
        {"strategy": "S3", "symbol": "BTCUSDT", "pnl_tmn": 600.0, "pnl_pct": 0.003, "fees_tmn": "100",
         "entry_ts": iso(T0 + 300), "closed_ts": iso(T0 + 3900), "exit_reason": "target"},  # hold derived: 3600
        {"strategy": "S3", "symbol": "SOLUSDT", "pnl_tmn": "-200", "pnl_pct": "-0.001", "hold_s": 1800,
         "fees_tmn": "100", "closed_ts": T0 + 6000, "exit_reason": "regime_flip"},  # entry derived: T0 + 4200
    ]


def test_trade_metrics_exact() -> None:
    eq = [(T0, 5_000_000), (T0 + 2 * H, 5_001_000)]
    m = compute_metrics(eq, _four_trades(), initial_equity=5_000_000, n_orders=9)
    assert (m.n_trades, m.wins, m.losses, m.n_orders) == (4, 2, 2, 9)
    assert m.win_rate == 0.5
    assert (m.gross_profit_tmn, m.gross_loss_tmn) == (1600.0, 600.0)
    assert m.profit_factor == approx(1600 / 600)  # 2.666...
    assert m.closed_trades_pnl_tmn == 1000.0  # 1000 - 400 + 600 - 200
    assert m.avg_pnl_pct == approx(0.0008)  # (0.002 - 0.0008 + 0.003 - 0.001) / 4
    assert m.avg_hold_s == approx(1395)  # (60 + 120 + 3600 + 1800) / 4
    assert (m.fees_paid_tmn, m.fees_source) == (300.0, "trades")
    assert m.fees_pct_of_equity == approx(6e-5)  # 300 / 5,000,000
    assert m.turnover_tmn is None and m.turnover_source == "none"
    assert m.exit_reasons == {"lockin": 1, "regime_flip": 1, "stop": 1, "target": 1}
    s1, s3 = m.per_strategy["S1"], m.per_strategy["S3"]
    assert (s1["n_trades"], s1["pnl_tmn"], s1["win_rate"], s1["fees_tmn"]) == (2, 600.0, 0.5, 100.0)
    assert s1["profit_factor"] == approx(2.5) and s1["avg_hold_s"] == approx(90)
    assert (s3["n_trades"], s3["pnl_tmn"], s3["fees_tmn"]) == (2, 400.0, 200.0)
    assert s3["profit_factor"] == approx(3.0) and s3["avg_hold_s"] == approx(2700)
    assert m.regime_gated["regime_flip_exits"] == 1


def test_fee_turnover_and_order_count_sources() -> None:
    trades = _four_trades()
    orders = [{"client_id": "a", "fee_tmn": "10", "notional_tmn": "1000"}, {"client_id": "b", "fee_tmn": 5}]
    m = compute_metrics([(T0, 100.0), (T0 + H, 101.0)], trades, orders=orders, initial_equity=100)
    assert (m.n_orders, m.fees_paid_tmn, m.fees_source) == (2, 15.0, "orders")
    assert (m.turnover_tmn, m.turnover_source) == (1000.0, "orders")
    m = compute_metrics(None, trades, orders=orders, fees_paid_tmn=Decimal("42.5"), turnover_tmn="7000",
                        n_orders=11, initial_equity=1000)
    assert (m.n_orders, m.fees_paid_tmn, m.fees_source) == (11, 42.5, "given")
    assert (m.turnover_tmn, m.turnover_source) == (7000.0, "given")
    assert m.fees_pct_of_equity == approx(0.0425)
    m = compute_metrics(None, [{"pnl_tmn": 1, "turnover_tmn": "250"}, {"pnl_tmn": 2, "notional_tmn": 50}])
    assert (m.turnover_tmn, m.turnover_source) == (300.0, "trades")
    assert (m.fees_paid_tmn, m.fees_source, m.fees_pct_of_equity) == (0.0, "none", None)


def test_profit_factor_without_losers_is_none_with_note() -> None:
    m = compute_metrics(None, [{"strategy": "S1", "pnl_tmn": "100"}, {"strategy": "S1", "pnl_tmn": "50"}])
    assert m.profit_factor is None and m.win_rate == 1.0
    assert "no losing trades" in m.profit_factor_note
    assert m.per_strategy["S1"]["profit_factor"] is None
    assert json.loads(json.dumps(m.to_dict(), allow_nan=False))["profit_factor"] is None
    only_losers = compute_metrics(None, [{"pnl_tmn": "-10"}])
    assert only_losers.profit_factor == 0.0 and only_losers.win_rate == 0.0
    breakeven = compute_metrics(None, [{"pnl_tmn": "0"}])
    assert breakeven.profit_factor is None and breakeven.win_rate == 0.0
    assert "no winning or losing" in breakeven.profit_factor_note


# ---------------------------------------------------------------- decisions


def _risk(*failed: str, passed: tuple[str, ...] = ("MIN_NOTIONAL",)) -> RiskDecision:
    checks = [RiskCheck(code=c, passed=True, detail="ok") for c in passed]
    checks += [RiskCheck(code=c, passed=False, detail="failed") for c in failed]
    return RiskDecision(approved=not failed, checks=checks, veto_reason=", ".join(failed) or None)


def _write_sample_log(log_dir: Path) -> list[dict[str, object]]:
    """A small decisions.jsonl over two UTC days (written by the real DecisionLog)."""
    clock = SimClock(T0)
    log = DecisionLog(log_dir, "backtest", "abc1234", "cfg123", clock)
    order_ok = {"client_id": "x", "accepted": True}
    # S1 entry placed, then closed later (position outcome on the entry decision)
    log.record("intent", "S1-1", "S1", "BTCUSDT", risk=_risk())
    log.record("order", "S1-1", "S1", "BTCUSDT", risk=_risk(), order=order_ok)
    # vetoes: two failed codes in one record, a suppressed-repeat veto, a veto naming no check
    log.record("veto", "S1-2", "S1", "ETHUSDT", risk=_risk("MAX_POSITIONS", "COIN_EXPOSURE"))
    log.record("veto", "S3-1", "S3", "BTCUSDT", risk=_risk("MAX_POSITIONS"),
               features={"veto_repeats_suppressed": 4})
    log.record("veto", "S2-1", "S2", "USDTTMN", risk=_risk("RATE_HOUR"))
    log.record("veto", "S3-2", "S3", "ETHUSDT", risk={"approved": False, "checks": []})
    # S2 rebalance placed (order-level outcome: not a trade); S3 placement rejected (not an order)
    log.record("order", "S2-2", "S2", "USDTTMN", risk=_risk(), order=order_ok)
    log.outcome("S2-2", {"kind": "order", "status": "FILLED", "fee_paid": "12", "fee_asset": "TMN"})
    log.record("order", "S3-3", "S3", "SOLUSDT", risk=_risk(), order={"client_id": "y", "accepted": False})
    # S3 held back by the plan regime
    log.record("no_trade", "S3-4", "S3", "BTCUSDT",
               notes="BTCUSDT breakout but plan regime range (conf 0.70); S3 needs trend_up")
    clock.set(T0 + 120)
    log.outcome("S1-1", {"pnl_tmn": "1500", "pnl_pct": "0.003", "hold_s": 120, "closed_ts": T0 + 120,
                         "exit_reason": "lockin", "strategy": "S1", "symbol": "BTCUSDT", "position_id": "p1"})
    clock.set(MIDNIGHT + 60)  # next UTC day, next file
    log.record("veto", "S1-3", "S1", "BTCUSDT", risk=_risk("DAILY_LOSS"))
    log.close()
    return list(iter_family(log_dir, "decisions"))


def test_vetoes_by_code_from_decision_records(tmp_path: Path) -> None:
    raw = _write_sample_log(tmp_path)
    expected = {"MAX_POSITIONS": 2, "COIN_EXPOSURE": 1, "DAILY_LOSS": 1, "RATE_HOUR": 1, "UNSPECIFIED": 1}
    assert vetoes_by_code(raw) == expected
    assert list(vetoes_by_code(raw)) == list(expected)  # most frequent first, ties by code
    assert vetoes_by_code(merge_decisions(raw)) == expected

    m = compute_metrics(decisions=raw)
    assert m.n_vetoes == 5 and m.vetoes_by_code == expected
    assert m.veto_repeats_suppressed == 4
    assert m.n_orders == 2  # S1-1 and S2-2; the rejected S3-3 placement is not an order
    assert m.regime_gated == {"s3_entries_gated": 1, "regime_flip_exits": 0}
    assert {k: (v["n_vetoes"], v["n_orders"]) for k, v in m.per_strategy.items()} == {
        "S1": (2, 1), "S2": (1, 1), "S3": (2, 0),
    }
    assert {d: (v["n_vetoes"], v["n_orders"]) for d, v in m.per_day.items()} == {
        "2026-10-22": (4, 2), "2026-10-23": (1, 0),
    }
    # no equity: the window comes from the decisions, and the report says data is insufficient
    assert m.start_ts == T0 and m.end_ts == MIDNIGHT + 60 and m.insufficient_data
    assert m.return_pct is None and m.max_drawdown_pct is None
    assert compute_metrics(decisions=merge_decisions(raw)).to_dict() == m.to_dict()


def test_trades_from_decision_outcomes(tmp_path: Path) -> None:
    raw = _write_sample_log(tmp_path)
    trades = trades_from_decisions(raw)
    assert len(trades) == 1  # the S2 order-level outcome is not a closed position
    t = trades[0]
    assert (t["strategy"], t["symbol"], t["pnl_tmn"], t["exit_reason"]) == ("S1", "BTCUSDT", "1500", "lockin")
    assert t["entry_ts"] == T0 and t["closed_ts"] == T0 + 120 and t["decision_id"] == "S1-1"
    m = compute_metrics(None, trades, decisions=raw)
    assert (m.n_trades, m.closed_trades_pnl_tmn, m.avg_hold_s, m.avg_pnl_pct) == (1, 1500.0, 120.0, 0.003)
    assert m.per_strategy["S1"]["n_trades"] == 1


# ---------------------------------------------------------------- AI


def _plan_dict(ts: float, btc: str, enabled: list[str], mult: str) -> dict[str, object]:
    return {"ts": ts, "regimes": {"BTCUSDT": {"regime": btc, "confidence": 0.7, "evidence": ""}},
            "enabled_strategies": enabled, "size_multiplier": mult}


def test_ai_plan_metrics_and_regime_gating(tmp_path: Path) -> None:
    first = MarketPlan(
        ts=T0,
        regimes={"BTCUSDT": RegimeCall(regime="range", confidence=0.7),
                 "USDTTMN": RegimeCall(regime="trend_up", confidence=0.8)},
        enabled_strategies=["S1", "S2"],
        size_multiplier=Decimal("1"),
    )
    store_row = {"ts": T0 + 300, "plan": _plan_dict(T0 + 300, "trend_up", ["S1", "S2", "S3"], "0.5"),
                 "prompt_hash": "abc"}
    same_set = _plan_dict(T0 + 600, "trend_up", ["S3", "S2", "S1"], "0.75")  # same set: not a change
    narrowed = _plan_dict(T0 + 900, "trend_up", ["S2"], "1.25")
    plans = [narrowed, store_row, first, same_set]  # out of order on purpose: sorted by ts
    trades = [{"strategy": "S3", "pnl_tmn": "-5", "exit_reason": "regime_flip"}]
    m = compute_metrics(None, trades, plans=plans)
    assert m.n_plans == 4
    assert m.regime_distribution == {"BTCUSDT": {"range": 1, "trend_up": 3}, "USDTTMN": {"trend_up": 1}}
    assert m.avg_size_multiplier == approx(0.875)  # (1 + 0.5 + 0.75 + 1.25) / 4
    assert m.n_plan_changes_of_enabled_strategies == 2  # {S1,S2} -> {S1,S2,S3} -> same -> {S2}
    assert m.regime_gated == {"s3_entries_gated": 0, "regime_flip_exits": 1}

    # without `plans`, plan decision records (kind="plan", plan_snapshot) are used
    log = DecisionLog(tmp_path, "shadow", "abc1234", "cfg123", SimClock(T0))
    log.record("plan", "plan-1", "AI", "*", plan_snapshot=first)
    log.record("plan", "plan-2", "AI", "*", plan_snapshot=MarketPlan(ts=T0 + 300, enabled_strategies=["S2"]))
    log.close()
    from_log = compute_metrics(decisions=list(iter_family(tmp_path, "decisions")))
    assert from_log.n_plans == 2 and from_log.n_plan_changes_of_enabled_strategies == 1
    assert from_log.regime_distribution == {"BTCUSDT": {"range": 1}, "USDTTMN": {"trend_up": 1}}
    assert from_log.avg_size_multiplier == 1.0


def test_ablation_delta() -> None:
    on = Metrics(return_pct=0.012, max_drawdown_pct=0.01, n_trades=5, n_orders=12, fees_paid_tmn=1500.0,
                 n_vetoes=3, vetoes_by_code={"MAX_POSITIONS": 2, "RATE_HOUR": 1})
    off = Metrics(return_pct=0.004, max_drawdown_pct=0.02, n_trades=8, n_orders=20, fees_paid_tmn=2500.0,
                  n_vetoes=1, vetoes_by_code={"MAX_POSITIONS": 1})
    d = ablation_delta(on, off)
    assert d["return_pct"] == {"on": 0.012, "off": 0.004, "delta": approx(0.008)}
    assert d["max_drawdown_pct"] == {"on": 0.01, "off": 0.02, "delta": approx(-0.01)}
    assert d["n_trades"] == {"on": 5, "off": 8, "delta": -3}
    assert d["n_orders"] == {"on": 12, "off": 20, "delta": -8}
    assert d["fees_paid_tmn"] == {"on": 1500.0, "off": 2500.0, "delta": -1000.0}
    assert d["n_vetoes"] == {"on": 3, "off": 1, "delta": 2}
    assert d["vetoes_by_code"] == {
        "MAX_POSITIONS": {"on": 2, "off": 1, "delta": 1},
        "RATE_HOUR": {"on": 1, "off": 0, "delta": 1},
    }
    assert ablation_delta(on.to_dict(), off.to_dict()) == d  # summary.json dicts work the same
    # a side without data: delta None for ratios, counts compare as 0
    empty = ablation_delta(on, Metrics())
    assert empty["return_pct"] == {"on": 0.012, "off": None, "delta": None}
    assert empty["n_trades"]["delta"] == 5


# ---------------------------------------------------------------- end state


def test_realized_vs_unrealized() -> None:
    # balances valued in TMN; DOGE has no price -> listed, left out
    split = realized_vs_unrealized(
        initial_equity=5_000_000,
        final_balances={"TMN": Balance(asset="TMN", free=Decimal("2900000"), locked=Decimal("100000")),
                        "USDT": Decimal("5"), "BTC": "0.0001", "DOGE": "10", "ETH": "0"},
        prices_tmn={"USDT": "266000", "BTC": Decimal("22000000000")},
    )
    assert split["source"] == "balances"
    assert split["tmn"] == 3_000_000  # free 2,900,000 + locked 100,000
    assert split["usdt_tmn_value"] == approx(1_330_000)  # 5 x 266,000
    assert split["coins_tmn_value"] == approx(2_200_000)  # 0.0001 x 22,000,000,000
    assert split["equity_tmn"] == approx(6_530_000)
    assert split["realized_tmn"] == -2_000_000  # 3,000,000 - 5,000,000
    assert split["unrealized_tmn"] == approx(3_530_000)  # 6,530,000 - 3,000,000
    assert split["unrealized_share"] == approx(3_530_000 / 6_530_000) and split["flat"] is False
    assert split["unpriced_assets"] == ["DOGE"]
    # after the flatten: everything in TMN
    flat = realized_vs_unrealized({"equity_tmn": "5010000", "tmn": "5010000", "usdt_tmn_value": "0",
                                   "coins_tmn_value": "0"}, initial_equity=5_000_000)
    assert (flat["realized_tmn"], flat["unrealized_tmn"], flat["flat"]) == (10_000, 0, True)
    assert realized_vs_unrealized()["source"] == "none"


def test_store_equity_rows_csv_and_dataframe_agree(tmp_path: Path) -> None:
    db = tmp_path / "wta.sqlite"
    store = Store(db)
    store.add_equity(T0, Decimal("5000000"), Decimal("5000000"), Decimal("0"), Decimal("0"))
    store.add_equity(T0 + 60, Decimal("4990000"), Decimal("2990000"), Decimal("2000000"), Decimal("0"))
    store.add_equity(T0 + 120, Decimal("5010000"), Decimal("5010000"), Decimal("0"), Decimal("0"))
    store.flush()
    rows = read_equity_table(store)
    assert [r["equity_tmn"] for r in rows] == ["5000000", "4990000", "5010000"]
    assert read_equity_table(db, from_ts=T0 + 60, to_ts=T0 + 60)[0]["tmn"] == "2990000"
    conn = sqlite3.connect(db)
    try:  # raw tuples in the table's column order
        tuples = conn.execute("SELECT * FROM equity ORDER BY ts").fetchall()
    finally:
        conn.close()
    store.close()

    m = compute_metrics(rows, initial_equity=5_000_000)
    assert m.return_pct == approx(0.002)  # 5,010,000 / 5,000,000 - 1
    assert m.max_drawdown_pct == approx(0.002)  # 10,000 / 5,000,000
    assert m.drawdown_duration_max_s == 120
    rvu = m.realized_vs_unrealized
    assert rvu["source"] == "equity_row" and rvu["realized_tmn"] == 10_000 and rvu["flat"] is True

    path = tmp_path / "equity.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "equity_tmn"])
        writer.writerows([[iso(r["ts"]), r["equity_tmn"]] for r in rows])
    frame = pd.DataFrame({"ts": pd.to_datetime([r["ts"] for r in rows], unit="s", utc=True),
                          "equity_tmn": [float(r["equity_tmn"]) for r in rows]})
    for other in (tuples, read_csv_rows(path), frame):
        o = compute_metrics(other, initial_equity=5_000_000)
        assert (o.return_pct, o.max_drawdown_pct, o.drawdown_duration_max_s) == (
            m.return_pct, m.max_drawdown_pct, m.drawdown_duration_max_s)
        assert o.start_ts == T0 and o.end_ts == T0 + 120


# ---------------------------------------------------------------- empty and junk inputs


def test_empty_inputs_never_raise(tmp_path: Path) -> None:
    for m in (compute_metrics(), compute_metrics([], [], decisions=[], plans=[], orders=[])):
        assert m.insufficient_data and m.n_equity_points == 0
        assert (m.n_trades, m.n_orders, m.n_vetoes, m.n_plans) == (0, 0, 0, 0)
        assert m.return_pct is None and m.max_drawdown_pct is None and m.sharpe_annualized is None
        assert m.win_rate is None and m.profit_factor is None and m.avg_pnl_pct is None
        assert m.fees_paid_tmn == 0 and m.fees_pct_of_equity is None
        assert m.vetoes_by_code == {} and m.per_strategy == {} and m.per_day == {} and m.daily_returns == {}
        assert m.start_ts is None and m.days == 0
        assert any(n.startswith("insufficient data: no equity rows") for n in m.notes)
        json.dumps(m.to_dict(), allow_nan=False)  # JSON-ready
    assert vetoes_by_code(None) == {} and trades_from_decisions(None) == []
    assert ablation_delta(None, None)["return_pct"] == {"on": None, "off": None, "delta": None}
    assert read_equity_table(tmp_path / "missing.sqlite") == [] and read_csv_rows(tmp_path / "no.csv") == []
    (tmp_path / "empty.sqlite").touch()  # a file without the equity table
    assert read_equity_table(tmp_path / "empty.sqlite") == []


def test_single_row_and_junk_rows() -> None:
    junk = [("bad", "x"), None, {"ts": None, "equity_tmn": 1}, {"ts": T0, "equity_tmn": "nan"}, (T0,), "row"]
    m = compute_metrics(junk + [(T0 + 60, "101")], [None, 5, {"pnl_tmn": "abc"}], initial_equity=100)
    assert m.n_equity_points == 1 and m.insufficient_data
    assert m.return_pct == approx(0.01) and m.sharpe_annualized is None
    assert m.n_trades == 1 and m.win_rate is None  # the junk trade dict counts, its PnL does not
    assert any("a single equity row" in n for n in m.notes)
    bad_init = compute_metrics([(T0, 100), (T0 + 60, 101)], initial_equity=0)
    assert bad_init.initial_equity_tmn == 100 and any("ignored" in n for n in bad_init.notes)


def test_deterministic_output() -> None:
    points = [(T0 + i * 600, 100 + (i % 7) - (i % 3)) for i in range(400)]
    a = compute_metrics(points, _four_trades(), initial_equity=100).to_dict()
    b = compute_metrics(list(reversed(points)), _four_trades(), initial_equity=100).to_dict()
    assert a == b  # input order of equity rows does not matter (sorted by ts)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
