"""Performance metrics (PRD-05 §4): one implementation for backtest, shadow and live runs.

Inputs, all optional (missing or empty inputs give zeros / ``None``; nothing here raises on them):

* ``equity`` — the equity curve, any of: ``(ts, equity_tmn, tmn, usdt_tmn_value, coins_tmn_value,
  ...)`` tuples (raw rows of the store's ``equity`` table, see ``read_equity_table``), dicts with
  ``ts`` and ``equity_tmn`` (``equity.csv`` rows via ``read_csv_rows``), or a DataFrame/Series.
  ``ts`` is unix seconds or ISO-8601; money may be Decimal, str, int or float.
* ``trades`` — closed positions / cycles: dicts with ``strategy, symbol, pnl_tmn, pnl_pct, hold_s,
  fees_tmn, entry_ts, closed_ts, exit_reason`` (``trades.csv`` rows, or ``trades_from_decisions``).
* ``decisions`` — decision records, raw ``decisions.jsonl`` lines or ``merge_decisions`` output:
  vetoes by risk code, order counts, S3 regime gating and (when ``plans`` is not given) the plans.
* ``plans`` — MarketPlans (models, dicts, ``Store.load_plans`` rows, ``kind="plan"`` decisions).

Units and conventions:

* Every ``*_pct`` value is a **fraction** (``0.01`` = 1 %), like ``pnl_pct`` and ``drawdown_pct``
  in the logs. ``max_drawdown_pct`` is positive (``0.05`` = 5 % below the running peak).
* Money is float TMN: metrics are reporting, not fill logic (fills and positions stay Decimal).
* The **initial equity** (``initial_equity``, else the first equity row) is the starting peak of
  the drawdown and the base of the total, daily and hourly returns and of ``fees_pct_of_equity``.
* Drawdown duration: time from a peak until equity is back at (or above) that peak, or until the
  end of the data when it never recovers.
* Days are UTC calendar days (``"YYYY-MM-DD"``). A day's return is its last equity over the previous
  day's last equity (the first day starts from the initial equity) minus one.
* Sharpe / Sortino use 1-hour returns: equity sampled at every UTC hour mark after the first row
  (last row at or before the mark), plus the last row when the data ends between two marks; risk
  free rate 0; annualised by √(24·365) because crypto trades every hour of the year. Sortino's
  downside deviation is √mean(min(r, 0)²) over all hourly returns. Both are ``None`` with fewer
  than two hourly returns or no dispersion. ``sharpe_note`` states the window: a few days of hourly
  returns are far too few for these ratios to mean much.
* ``profit_factor`` = gross profit / gross loss. With no losing trade it is undefined and reported
  as ``None`` (``profit_factor_note`` says why), never ``inf`` (JSON has no infinity).
* ``realized_vs_unrealized``: ``realized_tmn`` = TMN cash − initial equity (what the challenge
  counts, assuming the run started 100 % in TMN); ``unrealized_tmn`` = equity − TMN cash, the value
  still held in USDT/coins (must be ~0 after the flatten). They add up to the total PnL.
* Fees: ``fees_paid_tmn`` argument, else the orders' ``fee_tmn``, else the trades' ``fees_tmn``
  (``fees_source`` says which). Turnover likewise from ``turnover_tmn``, the orders'
  ``notional_tmn`` or the trades' ``turnover_tmn``; ``None`` when no source has it.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from wta.logging_.decision_log import merge_decisions
from wta.utils.timeutil import iso, parse_iso

HOUR_S = 3600
DAY_S = 86400.0
HOURS_PER_YEAR = 24 * 365
ANNUALIZATION = math.sqrt(HOURS_PER_YEAR)
MIN_DISPERSION = 1e-12  # standard deviations below this count as zero (float noise of a flat curve)
MAX_HOURS = 24 * 366 * 5  # a longer equity window is a unit error (ms vs s), not a real run
FLAT_SHARE = 0.001  # unrealized value below 0.1 % of equity counts as flat (all TMN)
FIAT = "TMN"
USDT = "USDT"
UNSPECIFIED_CODE = "UNSPECIFIED"  # a veto record that names no failed check
ABLATION_KEYS: tuple[str, ...] = (
    "return_pct",
    "max_drawdown_pct",
    "n_trades",
    "n_orders",
    "fees_paid_tmn",
    "n_vetoes",
)
COUNT_KEYS = frozenset({"n_trades", "n_orders", "n_vetoes"})
EQUITY_COLUMNS = ("ts", "equity_tmn", "tmn", "usdt_tmn_value", "coins_tmn_value", "peak_tmn", "drawdown_pct")
VALUATION_KEYS = ("tmn", "usdt_tmn_value", "coins_tmn_value")


# ---------------------------------------------------------------- result


@dataclass
class Metrics:
    """All PRD-05 §4 metrics of one run. ``to_dict()`` is JSON-ready (no NaN/inf)."""

    label: str = ""
    # data window (from the equity curve; trades/decisions when there is none)
    start_ts: float | None = None
    end_ts: float | None = None
    start_iso: str | None = None
    end_iso: str | None = None
    days: float = 0.0
    n_equity_points: int = 0
    insufficient_data: bool = True
    # returns and risk
    initial_equity_tmn: float | None = None
    final_equity_tmn: float | None = None
    pnl_tmn: float | None = None
    return_pct: float | None = None
    max_drawdown_pct: float | None = None
    drawdown_duration_max_s: float | None = None
    daily_returns: dict[str, float | None] = field(default_factory=dict)
    n_hourly_returns: int = 0
    sharpe_annualized: float | None = None
    sortino: float | None = None
    sharpe_note: str = ""
    # trading
    n_trades: int = 0
    n_orders: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    profit_factor_note: str = ""
    gross_profit_tmn: float = 0.0
    gross_loss_tmn: float = 0.0
    closed_trades_pnl_tmn: float = 0.0
    avg_pnl_pct: float | None = None
    avg_hold_s: float | None = None
    fees_paid_tmn: float = 0.0
    fees_source: str = "none"
    fees_pct_of_equity: float | None = None
    turnover_tmn: float | None = None
    turnover_source: str = "none"
    exit_reasons: dict[str, int] = field(default_factory=dict)
    per_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_day: dict[str, dict[str, Any]] = field(default_factory=dict)
    # risk
    n_vetoes: int = 0
    vetoes_by_code: dict[str, int] = field(default_factory=dict)
    veto_repeats_suppressed: int = 0
    realized_vs_unrealized: dict[str, Any] = field(default_factory=dict)
    # AI
    n_plans: int = 0
    regime_distribution: dict[str, dict[str, int]] = field(default_factory=dict)
    avg_size_multiplier: float | None = None
    n_plan_changes_of_enabled_strategies: int = 0
    regime_gated: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(dataclasses.asdict(self))


# ---------------------------------------------------------------- parsing helpers


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats by None (JSON has no NaN/inf)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _num(value: Any) -> float | None:
    """Decimal/str/int/float -> finite float, else None (bools, blanks, NaN and junk included)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, InvalidOperation):
        return None
    return out if math.isfinite(out) else None


def _ts(value: Any) -> float | None:
    """Unix seconds from a number, a numeric or ISO-8601 string, or a (pandas) datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if isinstance(value, datetime):
        try:
            dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            out = dt.timestamp()
        except (ValueError, OverflowError, OSError):  # NaT and out-of-range dates
            return None
        return out if math.isfinite(out) else None
    num = _num(value)
    if num is not None:
        return num
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    """str for labels; None and NaN (pandas blanks) become ''."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    return str(value)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _as_dict(obj: Any) -> dict[str, Any] | None:
    """A record as a plain dict: mappings, pydantic models, ``ClosedPosition``-like, dataclasses."""
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    outcome = getattr(obj, "outcome", None)
    if callable(outcome):  # e.g. wta.execution.positions.ClosedPosition
        out = outcome()
        if not isinstance(out, Mapping):
            return None
        d = dict(out)
        pos = getattr(obj, "position", None)
        if pos is not None:
            d.setdefault("entry_ts", getattr(pos, "entry_ts", None))
            meta = getattr(pos, "meta", None)
            if isinstance(meta, Mapping) and "fees_tmn" in meta:
                d.setdefault("fees_tmn", meta["fees_tmn"])
        return d
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    return None


def _items(value: Any) -> list[Any]:
    """Iterable input -> list; None -> []; a single record (mapping/model) -> [record]."""
    if value is None:
        return []
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    if isinstance(value, (Mapping, BaseModel, str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


# ---------------------------------------------------------------- equity


def _equity_rows_of(equity: Any) -> list[Any]:
    if equity is None:
        return []
    if isinstance(equity, pd.Series):
        return [{"ts": t, "equity_tmn": v} for t, v in equity.items()]
    if isinstance(equity, pd.DataFrame):
        df = equity
        if "ts" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
            df = df.rename_axis("ts").reset_index()
        return df.to_dict("records")
    return _items(equity)


def _equity_record(row: Any) -> dict[str, float | None] | None:
    """One equity row -> {ts, equity_tmn, tmn?, usdt_tmn_value?, coins_tmn_value?} or None."""
    if isinstance(row, BaseModel):
        row = row.model_dump(mode="json")
    if isinstance(row, Mapping):
        ts, eq = _ts(row.get("ts")), _num(row.get("equity_tmn", row.get("equity")))
        extra = {k: _num(row.get(k)) for k in VALUATION_KEYS}
    elif isinstance(row, (str, bytes)):
        return None
    else:
        try:  # tuples, lists, sqlite3.Row: the equity table's column order
            values = [row[i] for i in range(min(len(row), len(EQUITY_COLUMNS)))]
        except (TypeError, IndexError, KeyError):
            return None
        if len(values) < 2:
            return None
        ts, eq = _ts(values[0]), _num(values[1])
        positions = {k: EQUITY_COLUMNS.index(k) for k in VALUATION_KEYS}
        extra = {k: _num(values[i]) if i < len(values) else None for k, i in positions.items()}
    if ts is None or eq is None:
        return None
    return {"ts": ts, "equity_tmn": eq, **extra}


def equity_records(equity: Any) -> list[dict[str, float | None]]:
    """Normalised equity rows sorted by ts; unusable rows dropped; one row per ts (the last wins)."""
    rows = [r for r in (_equity_record(x) for x in _equity_rows_of(equity)) if r is not None]
    rows.sort(key=lambda r: r["ts"])  # stable: equal timestamps keep input order
    out: list[dict[str, float | None]] = []
    for r in rows:
        if out and out[-1]["ts"] == r["ts"]:
            out[-1] = r
        else:
            out.append(r)
    return out


def drawdown(points: list[tuple[float, float]], start_equity: float | None = None) -> tuple[float, float]:
    """``(max_drawdown_pct, max_duration_s)`` of a sorted ``(ts, equity)`` series.

    ``start_equity`` (the initial equity) is the peak at the first timestamp when it is higher than
    the first row. Equity back at the peak (>=) ends an underwater period.
    """
    if not points:
        return 0.0, 0.0
    first_ts, first_eq = points[0]
    peak = max(first_eq, start_equity) if start_equity is not None else first_eq
    peak_ts = first_ts
    underwater = False
    max_dd = max_dur = 0.0
    for t, e in points:
        if e >= peak:
            if underwater:
                max_dur = max(max_dur, t - peak_ts)
                underwater = False
            peak, peak_ts = e, t
        elif peak > 0:
            underwater = True
            max_dd = max(max_dd, (peak - e) / peak)
    if underwater:
        max_dur = max(max_dur, points[-1][0] - peak_ts)
    return max_dd, max_dur


def hourly_returns(points: list[tuple[float, float]], start_equity: float | None = None) -> list[float]:
    """1-hour returns: equity at each UTC hour mark after the first row (+ the last row when the data
    ends between marks), starting from ``start_equity`` (default: the first row)."""
    if not points:
        return []
    first_ts, last_ts = points[0][0], points[-1][0]
    if (last_ts - first_ts) / HOUR_S > MAX_HOURS:
        return []  # e.g. milliseconds mixed with seconds: refuse instead of looping for ages
    marks: list[float] = []
    mark = (math.floor(first_ts / HOUR_S) + 1) * HOUR_S
    while mark <= last_ts:
        marks.append(float(mark))
        mark += HOUR_S
    if not marks or marks[-1] < last_ts:
        marks.append(last_ts)
    series = [start_equity if start_equity is not None else points[0][1]]
    i, current = 0, points[0][1]
    for mk in marks:
        while i < len(points) and points[i][0] <= mk:
            current = points[i][1]
            i += 1
        series.append(current)
    return [b / a - 1 for a, b in zip(series, series[1:], strict=False) if a > 0]


def sharpe_sortino(returns: list[float]) -> tuple[float | None, float | None]:
    """Annualised (√8760) Sharpe and Sortino of hourly returns; risk-free rate 0."""
    n = len(returns)
    if n < 2:
        return None, None
    mean = sum(returns) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n)
    sharpe = mean / sd * ANNUALIZATION if sd > MIN_DISPERSION else None
    sortino = mean / downside * ANNUALIZATION if downside > MIN_DISPERSION else None
    return sharpe, sortino


def daily_equity(points: list[tuple[float, float]], start_equity: float | None) -> dict[str, dict[str, Any]]:
    """Per UTC day: start (previous close or initial equity), end (last row) and return."""
    closes: dict[str, float] = {}
    for t, e in points:
        closes[_day(t)] = e
    out: dict[str, dict[str, Any]] = {}
    prev = start_equity if start_equity is not None else (points[0][1] if points else None)
    for day in sorted(closes):
        close = closes[day]
        ret = close / prev - 1 if prev is not None and prev > 0 else None
        out[day] = {"start_equity_tmn": prev, "end_equity_tmn": close, "return_pct": ret}
        prev = close
    return out


# ---------------------------------------------------------------- trades


def normalize_trade(raw: Any) -> dict[str, Any] | None:
    """One closed position as floats; ``hold_s``/``entry_ts`` are derived from each other if missing."""
    d = _as_dict(raw)
    if d is None:
        return None
    closed, entry, hold = _ts(d.get("closed_ts")), _ts(d.get("entry_ts")), _num(d.get("hold_s"))
    if hold is None and closed is not None and entry is not None:
        hold = closed - entry
    if entry is None and closed is not None and hold is not None:
        entry = closed - hold
    fees = d.get("fees_tmn", d.get("fee_tmn"))
    turnover = d.get("turnover_tmn", d.get("notional_tmn"))
    return {
        "strategy": _text(d.get("strategy")) or "unknown",
        "symbol": _text(d.get("symbol")),
        "pnl_tmn": _num(d.get("pnl_tmn")),
        "pnl_pct": _num(d.get("pnl_pct")),
        "hold_s": hold,
        "fees_tmn": _num(fees),
        "entry_ts": entry,
        "closed_ts": closed,
        "exit_reason": _text(d.get("exit_reason")),
        "turnover_tmn": _num(turnover),
    }


def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, win rate, profit factor, averages and fees of normalised trades."""
    pnls = [t["pnl_tmn"] for t in trades if t["pnl_tmn"] is not None]
    gross_profit = sum((p for p in pnls if p > 0), 0.0)
    gross_loss = sum((-p for p in pnls if p < 0), 0.0)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return {
        "n_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(pnls) if pnls else None,
        "pnl_tmn": sum(pnls, 0.0),
        "gross_profit_tmn": gross_profit,
        "gross_loss_tmn": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "avg_pnl_pct": _mean([t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]),
        "avg_hold_s": _mean([t["hold_s"] for t in trades if t["hold_s"] is not None]),
        # None (shown as n/a) when no trade carries its fees: a 0 would read as "paid no fees"
        "fees_tmn": (
            sum((t["fees_tmn"] for t in trades if t["fees_tmn"] is not None), 0.0)
            if any(t["fees_tmn"] is not None for t in trades)
            else None
        ),
    }


def trades_from_decisions(decisions: Any) -> list[dict[str, Any]]:
    """Closed positions from the decision log: outcomes carrying ``pnl_tmn`` (order-level outcomes
    are skipped). ``entry_ts`` = ``closed_ts − hold_s``; ``fees_tmn`` only if the outcome has it."""
    trades: list[dict[str, Any]] = []
    for rec in _decision_list(decisions):
        out = rec.get("outcome")
        if not isinstance(out, Mapping) or out.get("kind") == "order" or "pnl_tmn" not in out:
            continue
        closed, hold = _ts(out.get("closed_ts")), _num(out.get("hold_s"))
        trades.append({
            "strategy": out.get("strategy") or rec.get("strategy"),
            "symbol": out.get("symbol") or rec.get("symbol"),
            "pnl_tmn": out.get("pnl_tmn"),
            "pnl_pct": out.get("pnl_pct"),
            "hold_s": out.get("hold_s"),
            "fees_tmn": out.get("fees_tmn"),
            "entry_ts": closed - hold if closed is not None and hold is not None else None,
            "closed_ts": out.get("closed_ts"),
            "exit_reason": out.get("exit_reason"),
            "position_id": out.get("position_id"),
            "decision_id": rec.get("decision_id"),
        })
    trades.sort(key=lambda t: (_ts(t["closed_ts"]) or 0.0, _text(t["decision_id"])))
    return trades


# ---------------------------------------------------------------- decisions


def _decision_list(decisions: Any) -> list[dict[str, Any]]:
    """Decision records as dicts, merged by ``decision_id`` (outcomes applied) when possible."""
    if decisions is None:
        return []
    if isinstance(decisions, Mapping) and not ("decision_id" in decisions or "kind" in decisions):
        return [dict(v) for v in decisions.values() if isinstance(v, Mapping)]  # merge_decisions output
    recs = [d for d in (_as_dict(x) for x in _items(decisions)) if d is not None]
    if recs and all("decision_id" in r for r in recs):
        return [dict(v) for v in merge_decisions(recs).values()]
    return recs


def is_veto(rec: Mapping[str, Any]) -> bool:
    risk = rec.get("risk")
    return rec.get("kind") == "veto" or (isinstance(risk, Mapping) and risk.get("approved") is False)


def failed_codes(rec: Mapping[str, Any]) -> list[str]:
    """Failed risk check codes of a veto record (each once, log order); ``UNSPECIFIED`` if none."""
    risk = rec.get("risk") if isinstance(rec.get("risk"), Mapping) else {}
    explicit = risk.get("failed_codes")
    if isinstance(explicit, (list, tuple)):
        codes = [str(c) for c in explicit if c]
    else:
        codes = [
            str(c["code"])
            for c in risk.get("checks") or []
            if isinstance(c, Mapping) and c.get("code") and not c.get("passed", True)
        ]
    return list(dict.fromkeys(codes)) or [UNSPECIFIED_CODE]


def vetoes_by_code(decisions: Any) -> dict[str, int]:
    """``{risk code: veto records that failed it}``, most frequent first (ties by code)."""
    counter: Counter[str] = Counter()
    for rec in _decision_list(decisions):
        if is_veto(rec):
            counter.update(failed_codes(rec))
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _is_order(rec: Mapping[str, Any]) -> bool:
    """A decision whose order reached the exchange (accepted placement)."""
    order = rec.get("order")
    if isinstance(order, Mapping):
        return bool(order.get("accepted", rec.get("kind") == "order"))
    return rec.get("kind") == "order"


def _features(rec: Mapping[str, Any]) -> Mapping[str, Any]:
    f = rec.get("features")
    return f if isinstance(f, Mapping) else {}


def _gated_by_regime(rec: Mapping[str, Any]) -> bool:
    """An S3 entry held back by the regime: a ``no_trade`` record naming the regime, or a veto
    whose failed check code names it."""
    if _text(rec.get("strategy")).upper() != "S3":
        return False
    if rec.get("kind") == "no_trade":
        blob = (_text(rec.get("notes")) + json.dumps(_features(rec), sort_keys=True, default=str)).lower()
        return "regime" in blob
    return is_veto(rec) and any("REGIME" in code.upper() for code in failed_codes(rec))


# ---------------------------------------------------------------- plans


def _plan_list(plans: Any, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plans as dicts sorted by ts. Without ``plans``: the ``kind="plan"`` decision records."""
    source = _items(plans) if plans is not None else [r for r in decisions if r.get("kind") == "plan"]
    out: list[dict[str, Any]] = []
    for p in source:
        d = _as_dict(p)
        if d is None:
            continue
        if isinstance(d.get("plan"), Mapping):  # Store.load_plans row
            plan = dict(d["plan"])
            plan.setdefault("ts", d.get("ts"))
        elif d.get("kind") == "plan" and isinstance(d.get("plan_snapshot"), Mapping):
            plan = dict(d["plan_snapshot"])
            plan.setdefault("ts", d.get("ts"))
        else:
            plan = d
        out.append(plan)
    out.sort(key=lambda p: _ts(p.get("ts")) if _ts(p.get("ts")) is not None else -math.inf)
    return out


def plan_metrics(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """n_plans, regime counts per symbol, mean size multiplier, enabled-strategy changes."""
    dist: dict[str, Counter[str]] = defaultdict(Counter)
    for p in plans:
        regimes = p.get("regimes")
        if not isinstance(regimes, Mapping):
            continue
        for sym, call in regimes.items():
            regime = call.get("regime") if isinstance(call, Mapping) else call
            if regime:
                dist[str(sym)][str(regime)] += 1
    mults = [m for m in (_num(p.get("size_multiplier")) for p in plans) if m is not None]
    changes, prev = 0, None
    for p in plans:
        enabled = p.get("enabled_strategies")
        if not isinstance(enabled, (list, tuple)):
            continue
        cur = tuple(sorted(str(s) for s in enabled))
        if prev is not None and cur != prev:
            changes += 1
        prev = cur
    return {
        "n_plans": len(plans),
        "regime_distribution": {s: dict(sorted(c.items())) for s, c in sorted(dist.items())},
        "avg_size_multiplier": _mean(mults),
        "n_plan_changes_of_enabled_strategies": changes,
    }


# ---------------------------------------------------------------- end state


def realized_vs_unrealized(
    final_valuation: Any = None,
    *,
    initial_equity: Any = None,
    final_balances: Mapping[str, Any] | None = None,
    prices_tmn: Mapping[str, Any] | None = None,
    closed_trades_pnl_tmn: float | None = None,
) -> dict[str, Any]:
    """End-of-run split: PnL already in TMN vs value still held outside TMN.

    Source, first available: ``final_balances`` (asset -> amount or ``Balance``) valued with
    ``prices_tmn`` (asset -> TMN price; assets without a price are listed in ``unpriced_assets``
    and left out), else ``final_valuation`` (an equity row: ``equity_tmn, tmn, usdt_tmn_value,
    coins_tmn_value``).
    """
    equity = tmn = usdt = coins = None
    unpriced: list[str] = []
    source = "none"
    if final_balances is not None:
        source = "balances"
        tmn = usdt = coins = 0.0
        for asset, bal in sorted(final_balances.items()):
            amount = _num(getattr(bal, "total", bal))
            if not amount:
                continue
            if asset == FIAT:
                tmn += amount
                continue
            price = _num((prices_tmn or {}).get(asset))
            if price is None:
                unpriced.append(asset)
            elif asset == USDT:
                usdt += amount * price
            else:
                coins += amount * price
        equity = tmn + usdt + coins
    elif final_valuation is not None:
        v = _as_dict(final_valuation) or {}
        source = "valuation"
        equity, tmn = _num(v.get("equity_tmn")), _num(v.get("tmn"))
        usdt, coins = _num(v.get("usdt_tmn_value")), _num(v.get("coins_tmn_value"))
        if equity is None and tmn is not None:
            equity = tmn + (usdt or 0.0) + (coins or 0.0)
    init = _num(initial_equity)
    unrealized = equity - tmn if equity is not None and tmn is not None else None
    share = unrealized / equity if unrealized is not None and equity else None
    return {
        "source": source,
        "equity_tmn": equity,
        "tmn": tmn,
        "usdt_tmn_value": usdt,
        "coins_tmn_value": coins,
        "realized_tmn": tmn - init if tmn is not None and init is not None else None,
        "unrealized_tmn": unrealized,
        "unrealized_share": share,
        "flat": abs(share) <= FLAT_SHARE if share is not None else None,
        "closed_trades_pnl_tmn": closed_trades_pnl_tmn,
        "unpriced_assets": unpriced,
    }


# ---------------------------------------------------------------- main entry point


def compute_metrics(
    equity: Any = None,
    trades: Any = None,
    *,
    initial_equity: Any = None,
    n_orders: int | None = None,
    orders: Any = None,
    decisions: Any = None,
    plans: Any = None,
    fees_paid_tmn: Any = None,
    turnover_tmn: Any = None,
    final_valuation: Any = None,
    final_balances: Mapping[str, Any] | None = None,
    prices_tmn: Mapping[str, Any] | None = None,
    label: str = "",
) -> Metrics:
    """All PRD-05 §4 metrics (see the module docstring for inputs, units and definitions).

    ``n_orders``: explicit count, else ``len(orders)``, else accepted orders in ``decisions``.
    ``final_valuation``/``final_balances``: end state for ``realized_vs_unrealized``; without them
    the last equity row is used when it carries ``tmn``/``usdt_tmn_value``/``coins_tmn_value``.
    """
    m = Metrics(label=label)
    notes = m.notes
    records = equity_records(equity)
    points = [(float(r["ts"] or 0.0), float(r["equity_tmn"] or 0.0)) for r in records]  # never None here
    trade_list = [t for t in (normalize_trade(x) for x in _items(trades)) if t is not None]
    decs = _decision_list(decisions)
    order_list = [d for d in (_as_dict(o) for o in _items(orders)) if d is not None]

    # ---- equity: window, return, drawdown, daily and hourly returns
    init = _num(initial_equity)
    if init is not None and init <= 0:
        notes.append(f"initial_equity {init} ignored (must be > 0)")
        init = None
    start = init if init is not None else (points[0][1] if points else None)
    m.initial_equity_tmn = start
    m.n_equity_points = len(points)
    m.insufficient_data = len(points) < 2
    daily: dict[str, dict[str, Any]] = {}
    if points:
        if init is None:
            notes.append("initial equity = first equity row (no initial_equity given)")
        m.start_ts, m.end_ts = points[0][0], points[-1][0]
        m.final_equity_tmn = points[-1][1]
        if start is not None:
            m.pnl_tmn = m.final_equity_tmn - start
            m.return_pct = m.final_equity_tmn / start - 1 if start > 0 else None
        m.max_drawdown_pct, m.drawdown_duration_max_s = drawdown(points, start)
        daily = daily_equity(points, start)
        m.daily_returns = {day: v["return_pct"] for day, v in daily.items()}
        returns = hourly_returns(points, start) if len(points) >= 2 else []
        m.n_hourly_returns = len(returns)
        m.sharpe_annualized, m.sortino = sharpe_sortino(returns)
    else:
        times = [t for tr in trade_list for t in (tr["entry_ts"], tr["closed_ts"]) if t is not None]
        times += [t for t in (_ts(r.get("ts")) for r in decs) if t is not None]
        if times:
            m.start_ts, m.end_ts = min(times), max(times)
    if m.start_ts is not None and m.end_ts is not None:
        m.start_iso, m.end_iso = iso(m.start_ts), iso(m.end_ts)
        m.days = (m.end_ts - m.start_ts) / DAY_S
    if m.insufficient_data:
        what = "no equity rows" if not points else "a single equity row"
        notes.append(f"insufficient data: {what}; return, drawdown and ratios are not meaningful")
    m.sharpe_note = (
        f"{m.days:.1f} days of data ({m.n_hourly_returns} hourly returns) — do not over-interpret"
        if m.n_hourly_returns >= 2
        else f"fewer than 2 hourly returns ({m.n_hourly_returns}): Sharpe/Sortino not computed"
    )

    # ---- trades
    stats = trade_stats(trade_list)
    m.n_trades, m.wins, m.losses = stats["n_trades"], stats["wins"], stats["losses"]
    m.win_rate, m.profit_factor = stats["win_rate"], stats["profit_factor"]
    m.gross_profit_tmn, m.gross_loss_tmn = stats["gross_profit_tmn"], stats["gross_loss_tmn"]
    m.closed_trades_pnl_tmn = stats["pnl_tmn"]
    m.avg_pnl_pct, m.avg_hold_s = stats["avg_pnl_pct"], stats["avg_hold_s"]
    if m.profit_factor is None and m.gross_profit_tmn > 0:
        m.profit_factor_note = "no losing trades: profit factor undefined (reported as None, not inf)"
    elif m.profit_factor is None and m.n_trades:
        m.profit_factor_note = "no winning or losing PnL: profit factor undefined"
    m.exit_reasons = dict(sorted(Counter(t["exit_reason"] or "unknown" for t in trade_list).items()))

    # ---- orders, fees, turnover
    if n_orders is not None:
        m.n_orders = int(n_orders)
    elif orders is not None:
        m.n_orders = len(order_list)
    else:
        m.n_orders = sum(1 for r in decs if _is_order(r))
    order_fees = [f for f in (_num(o.get("fee_tmn", o.get("fees_tmn"))) for o in order_list) if f is not None]
    fees_given, turnover_given = _num(fees_paid_tmn), _num(turnover_tmn)
    if fees_given is not None:
        m.fees_paid_tmn, m.fees_source = fees_given, "given"
    elif order_fees:
        m.fees_paid_tmn, m.fees_source = sum(order_fees), "orders"
    elif any(t["fees_tmn"] is not None for t in trade_list):
        m.fees_paid_tmn, m.fees_source = stats["fees_tmn"], "trades"
    m.fees_pct_of_equity = m.fees_paid_tmn / start if start else None
    order_notional = [n for n in (_num(o.get("notional_tmn")) for o in order_list) if n is not None]
    trade_turnover = [t["turnover_tmn"] for t in trade_list if t["turnover_tmn"] is not None]
    if turnover_given is not None:
        m.turnover_tmn, m.turnover_source = turnover_given, "given"
    elif order_notional:
        m.turnover_tmn, m.turnover_source = sum(order_notional), "orders"
    elif trade_turnover:
        m.turnover_tmn, m.turnover_source = sum(trade_turnover), "trades"

    # ---- vetoes
    vetoes = [r for r in decs if is_veto(r)]
    m.n_vetoes = len(vetoes)
    m.vetoes_by_code = vetoes_by_code(vetoes)
    m.veto_repeats_suppressed = int(sum(_num(_features(r).get("veto_repeats_suppressed")) or 0 for r in vetoes))

    # ---- per strategy
    strategies = {t["strategy"] for t in trade_list}
    strategies |= {_text(r.get("strategy")) for r in decs if is_veto(r) or _is_order(r)}
    strategies.discard("")
    for sid in sorted(strategies):
        sub = trade_stats([t for t in trade_list if t["strategy"] == sid])
        sub["n_vetoes"] = sum(1 for r in vetoes if _text(r.get("strategy")) == sid)
        sub["n_orders"] = sum(1 for r in decs if _is_order(r) and _text(r.get("strategy")) == sid)
        m.per_strategy[sid] = sub

    # ---- per day (UTC): equity, trades by closed_ts, vetoes and orders by decision ts
    def by_day(recs: list[dict[str, Any]]) -> Counter[str]:
        return Counter(_day(t) for t in (_ts(r.get("ts")) for r in recs) if t is not None)

    day_trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trade_list:
        if t["closed_ts"] is not None:
            day_trades[_day(t["closed_ts"])].append(t)
    day_vetoes, day_orders = by_day(vetoes), by_day([r for r in decs if _is_order(r)])
    for day in sorted(set(daily) | set(day_trades) | set(day_vetoes) | set(day_orders)):
        eq = daily.get(day, {})
        sub = trade_stats(day_trades.get(day, []))
        m.per_day[day] = {
            "start_equity_tmn": eq.get("start_equity_tmn"),
            "end_equity_tmn": eq.get("end_equity_tmn"),
            "return_pct": eq.get("return_pct"),
            "n_trades": sub["n_trades"],
            "wins": sub["wins"],
            "pnl_tmn": sub["pnl_tmn"],
            "fees_tmn": sub["fees_tmn"],
            "n_vetoes": day_vetoes.get(day, 0),
            "n_orders": day_orders.get(day, 0),
        }

    # ---- end state
    valuation = final_valuation
    if valuation is None and final_balances is None and records:
        last = records[-1]
        if any(last.get(k) is not None for k in VALUATION_KEYS):
            valuation = last
    m.realized_vs_unrealized = realized_vs_unrealized(
        valuation,
        initial_equity=start,
        final_balances=final_balances,
        prices_tmn=prices_tmn,
        closed_trades_pnl_tmn=m.closed_trades_pnl_tmn,
    )
    if final_valuation is None and final_balances is None and valuation is not None:
        m.realized_vs_unrealized["source"] = "equity_row"

    # ---- AI
    ai = plan_metrics(_plan_list(plans, decs))
    m.n_plans = ai["n_plans"]
    m.regime_distribution = ai["regime_distribution"]
    m.avg_size_multiplier = ai["avg_size_multiplier"]
    m.n_plan_changes_of_enabled_strategies = ai["n_plan_changes_of_enabled_strategies"]
    m.regime_gated = {
        "s3_entries_gated": sum(1 for r in decs if _gated_by_regime(r)),
        "regime_flip_exits": sum(1 for t in trade_list if t["exit_reason"] == "regime_flip"),
    }
    return m


def ablation_delta(m_on: Metrics | Mapping[str, Any] | None, m_off: Metrics | Mapping[str, Any] | None) -> dict[str, Any]:
    """AI on vs off: ``{key: {"on", "off", "delta" = on − off}}`` for return, max drawdown, trades,
    orders, fees and vetoes, plus ``vetoes_by_code`` per code. ``delta`` is None if a side is None.
    A positive ``max_drawdown_pct`` delta means the AI run drew down more."""
    on = m_on.to_dict() if isinstance(m_on, Metrics) else dict(m_on or {})
    off = m_off.to_dict() if isinstance(m_off, Metrics) else dict(m_off or {})
    out: dict[str, Any] = {}
    for key in ABLATION_KEYS:
        a, b = _num(on.get(key)), _num(off.get(key))
        if key in COUNT_KEYS:
            a, b = int(a or 0), int(b or 0)
        out[key] = {"on": a, "off": b, "delta": a - b if a is not None and b is not None else None}
    codes_on, codes_off = on.get("vetoes_by_code") or {}, off.get("vetoes_by_code") or {}
    out["vetoes_by_code"] = {
        code: {
            "on": int(codes_on.get(code, 0)),
            "off": int(codes_off.get(code, 0)),
            "delta": int(codes_on.get(code, 0)) - int(codes_off.get(code, 0)),
        }
        for code in sorted(set(codes_on) | set(codes_off))
    }
    return out


# ---------------------------------------------------------------- loaders


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    """Rows of ``equity.csv`` / ``trades.csv`` as dicts of strings; ``[]`` if the file is missing."""
    p = Path(path)
    if not p.is_file():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_equity_table(db: Any, from_ts: float | None = None, to_ts: float | None = None) -> list[dict[str, Any]]:
    """Committed rows of the store's ``equity`` table (read-only connection), oldest first.

    ``db`` is a path or anything with a ``path`` attribute (a ``Store``). Returns ``[]`` when the
    file or the table does not exist. Values stay as stored (money as strings).
    """
    p = Path(str(getattr(db, "path", db)))
    if not p.is_file():
        return []
    sql = f"SELECT {', '.join(EQUITY_COLUMNS)} FROM equity WHERE 1=1"
    params: list[float] = []
    if from_ts is not None:
        sql += " AND ts >= ?"
        params.append(from_ts)
    if to_ts is not None:
        sql += " AND ts <= ?"
        params.append(to_ts)
    sql += " ORDER BY ts, rowid"
    try:
        conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(zip(EQUITY_COLUMNS, row, strict=True)) for row in rows]


__all__ = [
    "Metrics",
    "ablation_delta",
    "compute_metrics",
    "daily_equity",
    "drawdown",
    "equity_records",
    "failed_codes",
    "hourly_returns",
    "normalize_trade",
    "plan_metrics",
    "read_csv_rows",
    "read_equity_table",
    "realized_vs_unrealized",
    "sharpe_sortino",
    "trade_stats",
    "trades_from_decisions",
    "vetoes_by_code",
]
