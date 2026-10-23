"""Judge-facing reports (PRD-05 §7): one folder per run, an index, and the assembled final report.

* ``write_report(out_dir, ...)`` writes ``report.md`` plus ``summary.json``, ``trades.csv``,
  ``equity.csv`` and four PNGs (equity, drawdown, veto histogram, regime timeline) into ``out_dir``.
* ``update_index(reports_dir)`` regenerates ``reports_dir/README.md`` (``docs/REPORTS/README.md``),
  one row per report folder or standalone ``.md`` file, with its title and date.
* ``assemble_final_report(reports_dir, out_path)`` concatenates survey -> backtests (candles 30 d,
  books window, ablation) -> shadow -> live -> replay proof into ``deliverables/REPORT.md``,
  rewriting links so PNGs resolve relative to the new file; missing sections say "not available yet".

Inputs:

* ``metrics`` — ``backtest.metrics.Metrics`` or its ``to_dict()`` (any mapping, an object with
  ``to_dict()`` or a dataclass is accepted). Every ``*_pct`` / ``*_rate`` / ``*_share`` value is a
  **fraction** (``0.012`` = 1.2 %) and is multiplied by 100 for display only; ``max_drawdown_pct``
  is positive; ``*_tmn`` are TMN amounts; ``*_s`` are seconds.
* ``equity`` — ``(ts, equity_tmn[, tmn, usdt_tmn_value, coins_tmn_value, ...])`` rows (the store's
  ``equity`` table order), dicts with ``ts``/``equity_tmn`` (``read_equity_table`` rows,
  ``equity.csv`` rows), a DataFrame or a Series. ``ts``: unix seconds or ms, ISO-8601, datetime.
* ``trades`` — closed positions (``trades.csv`` rows, ``trades_from_decisions`` output, pydantic
  models or dataclasses); every column is kept in ``trades.csv``.
* ``decisions`` (``decisions.jsonl`` records, outcome-merged or not) and ``plans`` (``MarketPlan``
  dumps, ``Store.load_plans`` rows or ``kind="plan"`` decision records).
* ``ablation`` — ``backtest.metrics.ablation_delta`` output (``{key: {"on", "off", "delta"}}``) or
  ``{"ai_on": metrics, "ai_off": metrics}``.

Honesty rules: the requested window and the data actually present are both stated; a synthetic run
(``meta["synthetic"]``, or a ``data_source`` that says "synthetic") gets a loud banner before any
number and a watermark on every chart; an empty store yields an "insufficient data" report instead
of an error; limitations are filled in automatically from meta and metrics. Output is
deterministic: no wall-clock reads (the report date comes from ``meta["generated_at"]`` or the
window end), sorted keys, fixed file names, Agg-rendered PNGs without version metadata, and no
absolute paths in the assembled report.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import math
import numbers
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import matplotlib
import pandas as pd
from matplotlib import dates as mdates
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator

from backtest.metrics import vetoes_by_code as _vetoes_by_code_from_decisions

# ---------------------------------------------------------------- file names (fixed)

REPORT_MD = "report.md"
SUMMARY_JSON = "summary.json"
TRADES_CSV = "trades.csv"
EQUITY_CSV = "equity.csv"
EQUITY_PNG = "equity.png"
DRAWDOWN_PNG = "drawdown.png"
VETOES_PNG = "vetoes.png"
REGIMES_PNG = "regimes.png"
REPORT_FILES: tuple[str, ...] = (
    REPORT_MD,
    SUMMARY_JSON,
    TRADES_CSV,
    EQUITY_CSV,
    EQUITY_PNG,
    DRAWDOWN_PNG,
    VETOES_PNG,
    REGIMES_PNG,
)
INDEX_MD = "README.md"
DEFAULT_FINAL_REPORT = "deliverables/REPORT.md"

SYNTHETIC_BANNER = "SYNTHETIC DATA — not evidence of performance"
INSUFFICIENT_BANNER = "INSUFFICIENT DATA"
NOT_AVAILABLE = "not available yet"
TRADES_TABLE_ROWS = 50
TRADES_TABLE_MAX_COLS = 12
DEFAULT_FEE_BUDGET_PCT = 0.02  # risk.fee_budget_pct (configs/default.yaml), over the challenge
CHALLENGE_HOURS = 72.0
OFFLINE_MODEL_MARKER = "offline"  # wta.ai.offline_provider.OFFLINE_MODEL = "offline-rules-v1"

VALUATION_KEYS: tuple[str, ...] = ("tmn", "usdt_tmn_value", "coins_tmn_value")
EQUITY_CSV_COLUMNS: tuple[str, ...] = ("ts", "ts_unix", "equity_tmn", *VALUATION_KEYS, "drawdown_pct")
# trades.csv header when there are no trades: the schema backtest.metrics reads back.
DEFAULT_TRADE_COLUMNS: tuple[str, ...] = (
    "entry_ts",
    "closed_ts",
    "strategy",
    "symbol",
    "pnl_tmn",
    "pnl_pct",
    "hold_s",
    "fees_tmn",
    "exit_reason",
)

# 1080p-readable charts: 12 in wide at 120 dpi = 1440 px, 12-pt text.
_DPI = 120
_FIG_W = 12.0
_PNG_METADATA: dict[str, Any] = {"Software": None}  # no matplotlib version in the bytes
_RC = {
    "font.size": 12,
    "axes.titlesize": 14,
    "axes.labelsize": 12,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 11,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "timezone": "UTC",
}
_REGIME_COLORS = {
    "trend_up": "#2e7d32",
    "range": "#9e9e9e",
    "trend_down": "#c62828",
    "chaos": "#ef6c00",
}
_UNKNOWN_REGIME_COLOR = "#5e35b1"

# Main metrics table, in this order (key, label, kind). Aliases share a label; the first one wins.
_METRIC_SPECS: tuple[tuple[str, str, str], ...] = (
    ("initial_equity_tmn", "Initial equity (TMN)", "tmn"),
    ("final_equity_tmn", "Final equity (TMN)", "tmn"),
    ("pnl_tmn", "PnL (TMN)", "tmn"),
    ("return_pct", "Return", "pct"),
    ("max_drawdown_pct", "Max drawdown (below running peak)", "pct"),
    ("drawdown_duration_max_s", "Longest drawdown", "dur"),
    ("drawdown_duration_max", "Longest drawdown", "dur"),
    ("sharpe_annualized", "Sharpe (annualized, from 1-h returns)", "num"),
    ("sortino", "Sortino (annualized, from 1-h returns)", "num"),
    ("n_trades", "Closed trades / cycles", "int"),
    ("wins", "Winning trades", "int"),
    ("losses", "Losing trades", "int"),
    ("win_rate", "Win rate", "pct"),
    ("profit_factor", "Profit factor", "num"),
    ("avg_pnl_pct", "Average PnL per trade", "pct"),
    ("avg_hold_s", "Average hold", "dur"),
    ("gross_profit_tmn", "Gross profit (TMN)", "tmn"),
    ("gross_loss_tmn", "Gross loss (TMN)", "tmn"),
    ("closed_trades_pnl_tmn", "Closed trades PnL (TMN)", "tmn"),
    ("n_orders", "Orders placed", "int"),
    ("n_vetoes", "Risk vetoes", "int"),
    ("veto_repeats_suppressed", "Repeated vetoes suppressed in the log", "int"),
    ("fees_paid_tmn", "Fees paid (TMN)", "tmn"),
    ("fees_pct_of_equity", "Fees / initial equity", "pct"),
    ("turnover_tmn", "Turnover (TMN)", "tmn"),
    ("days", "Days of data", "num"),
    ("n_equity_points", "Equity points", "int"),
    ("n_hourly_returns", "Hourly returns (Sharpe/Sortino sample)", "int"),
)
# Keys shown elsewhere (header, own sections, notes) and never in the generic metrics rows.
_HIDDEN_METRIC_KEYS = frozenset(
    {
        "label",
        "start_ts",
        "end_ts",
        "start_iso",
        "end_iso",
        "insufficient_data",
        "fees_source",
        "turnover_source",
        "notes",
        "per_strategy",
        "per_day",
        "daily_returns",
        "exit_reasons",
        "vetoes_by_code",
        "vetoes_by_risk_code",
        "vetoes",
        "realized_vs_unrealized",
        "regime_distribution",
        "regime_gated",
        "n_plans",
        "avg_size_multiplier",
        "n_plan_changes_of_enabled_strategies",
        "ablation",
        "ai",
        "equity",
        "trades",
    }
)
_PER_STRATEGY_SPECS: tuple[tuple[str, str, str], ...] = (
    ("n_trades", "trades", "int"),
    ("wins", "wins", "int"),
    ("losses", "losses", "int"),
    ("win_rate", "win rate", "pct"),
    ("pnl_tmn", "PnL (TMN)", "tmn"),
    ("profit_factor", "profit factor", "num"),
    ("avg_pnl_pct", "avg PnL", "pct"),
    ("avg_hold_s", "avg hold", "dur"),
    ("fees_tmn", "fees (TMN)", "tmn"),
    ("n_orders", "orders", "int"),
    ("n_vetoes", "vetoes", "int"),
)
_PER_DAY_SPECS: tuple[tuple[str, str, str], ...] = (
    ("start_equity_tmn", "start equity (TMN)", "tmn"),
    ("end_equity_tmn", "end equity (TMN)", "tmn"),
    ("return_pct", "return", "pct"),
    ("n_trades", "trades", "int"),
    ("wins", "wins", "int"),
    ("pnl_tmn", "trades PnL (TMN)", "tmn"),
    ("fees_tmn", "fees (TMN)", "tmn"),
    ("n_orders", "orders", "int"),
    ("n_vetoes", "vetoes", "int"),
)
_BREAKDOWN_SKIP = frozenset({"gross_profit_tmn", "gross_loss_tmn"})  # in summary.json, not the table
_END_STATE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("tmn", "TMN cash", "tmn"),
    ("usdt_tmn_value", "USDT held (TMN value)", "tmn"),
    ("coins_tmn_value", "Coins held (TMN value)", "tmn"),
    ("equity_tmn", "Equity (TMN)", "tmn"),
    ("realized_tmn", "Realized PnL (TMN cash − initial equity)", "tmn"),
    ("unrealized_tmn", "Unrealized at the end (equity − TMN cash)", "tmn"),
    ("unrealized_share", "Unrealized share of equity", "pct"),
    ("flat", "Flat (all TMN)", "bool"),
    ("source", "Source", "str"),
)
_ABLATION_ORDER: tuple[str, ...] = (
    "return_pct",
    "max_drawdown_pct",
    "n_trades",
    "n_orders",
    "win_rate",
    "profit_factor",
    "avg_pnl_pct",
    "sharpe_annualized",
    "fees_paid_tmn",
    "n_vetoes",
    "n_plans",
)
_ABLATION_ON = ("ai_on", "on", "ai=on", "with_ai")
_ABLATION_OFF = ("ai_off", "off", "ai=off", "rules_only", "without_ai")
_TRADE_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "id",
    "open_ts",
    "entry_ts",
    "close_ts",
    "closed_ts",
    "exit_ts",
    "ts",
    "strategy",
    "symbol",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "price",
    "notional_tmn",
    "turnover_tmn",
    "pnl_tmn",
    "pnl_pct",
    "fee_tmn",
    "fees_tmn",
    "hold_s",
    "exit_reason",
    "reason",
    "position_id",
    "decision_id",
)
# Columns preferred for the markdown trades table (the CSV has every column).
_TRADE_DISPLAY: tuple[str, ...] = (
    "open_ts",
    "entry_ts",
    "close_ts",
    "closed_ts",
    "exit_ts",
    "ts",
    "strategy",
    "symbol",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "price",
    "pnl_tmn",
    "pnl_pct",
    "fee_tmn",
    "fees_tmn",
    "hold_s",
    "exit_reason",
)

_DATE_RE = re.compile(r"(20\d\d)-?(\d\d)-?(\d\d)")
_HEADING_RE = re.compile(r"^(#{1,6})(\s.*)$")
_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


# ================================================================= small value helpers


def _num(value: Any) -> float | None:
    """Finite float for numbers (Decimal, numpy included) and numeric strings; else None (bools too)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            out = float(Decimal(value.strip()))
        elif isinstance(value, numbers.Number):
            out = float(value)  # type: ignore[arg-type]
        else:
            return None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _flag(value: Any) -> bool:
    """Truthiness that reads ``"false"``/``"off"``/``"0"`` strings as False."""
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    return bool(value)


def _to_epoch(value: Any) -> float | None:
    """Unix seconds from seconds/ms numbers, ISO-8601 strings or datetimes (naive = UTC)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        try:
            out = (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
        except (ValueError, OverflowError, OSError):  # pandas NaT, out-of-range dates
            return None
        return out if math.isfinite(out) else None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
    n = _num(value)
    if n is not None:
        return n / 1000.0 if abs(n) > 1e11 else n  # > year 5138 in seconds => milliseconds
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    return None


def _iso(epoch: float | None) -> str:
    """``2026-10-22T10:00:00Z`` (milliseconds only when present)."""
    if epoch is None:
        return "n/a"
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _fmt_duration(seconds: float) -> str:
    s = abs(seconds)
    sign = "-" if seconds < 0 else ""
    if s < 60:
        return f"{sign}{s:.0f} s"
    if s < 3600:
        return f"{sign}{int(s // 60)}m {int(s % 60):02d}s"
    if s < 86400:
        return f"{sign}{int(s // 3600)}h {int(s % 3600 // 60):02d}m"
    return f"{sign}{int(s // 86400)}d {int(s % 86400 // 3600):02d}h"


def _guess_kind(key: str, value: Any) -> str:
    k = key.lower()
    if isinstance(value, bool):
        return "bool"
    if k.endswith(("_pct", "_rate", "_share")) or k in {"win_rate", "return", "drawdown"}:
        return "pct"
    if k.endswith("_tmn"):
        return "tmn"
    if k.endswith(("_s", "_sec", "_seconds")):
        return "dur"
    if k == "qty" or k.endswith(("_qty", "price")):
        return "raw"  # exchange precision: never round quantities or prices for display
    if k.endswith("ts") and (_num(value) or 0) > 1e9:
        return "ts"
    if isinstance(value, int):
        return "int"
    if _num(value) is not None and not isinstance(value, str):
        return "num"
    return "str"


def _fmt(kind: str, value: Any) -> str:
    """Human-readable cell value; ``n/a`` for missing/non-finite numbers. Fractions -> percent."""
    if value is None:
        return "n/a"
    if kind == "bool" or isinstance(value, bool):
        return "yes" if _flag(value) else "no"
    n = _num(value)
    if kind == "str" or (n is None and kind != "ts"):
        if isinstance(value, float) and not math.isfinite(value):
            return "n/a"
        if isinstance(value, list | tuple):
            return ", ".join(str(v) for v in value) or "—"
        return str(value)
    if kind == "ts":
        epoch = _to_epoch(value)
        return _iso(epoch) if epoch is not None else str(value)
    if kind == "raw":
        return format(value, "f") if isinstance(value, Decimal) else str(value)
    assert n is not None
    if kind == "pct":
        p = n * 100
        return f"{p:.4f}%" if 0 < abs(p) < 0.01 else f"{p:.2f}%"
    if kind in ("tmn", "int"):
        return f"{n:,.0f}"
    if kind == "dur":
        return _fmt_duration(n)
    return f"{n:,.2f}" if abs(n) >= 0.01 or n == 0 else f"{n:.4g}"


def _fmt_delta(kind: str, value: float) -> str:
    if kind == "pct":
        return f"{value * 100:+.2f} pp"
    if kind in ("tmn", "int"):
        return f"{value:+,.0f}"
    if kind == "dur":
        return ("+" if value >= 0 else "") + _fmt_duration(value)
    return f"{value:+,.2f}"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _md_table(
    headers: Sequence[str], rows: Iterable[Sequence[Any]], right: Sequence[bool] | None = None
) -> str:
    align = ["---:" if (right and i < len(right) and right[i]) else "---" for i in range(len(headers))]
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    """JSON-safe copy: Decimals as strings (project convention), NaN/inf as null, sets sorted."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else None
    if isinstance(value, datetime):
        epoch = _to_epoch(value)
        return _iso(epoch) if epoch is not None else None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return [_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    n = _num(value)
    return n if n is not None else str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    """Metrics/meta/ablation as a dict: accepts dicts, objects with ``to_dict()``, dataclasses."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"expected a mapping, got {type(value).__name__}")


def _record(value: Any) -> dict[str, Any] | None:
    """One trade/decision/plan as a dict: mappings, pydantic models, ``ClosedPosition``, dataclasses."""
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    outcome = getattr(value, "outcome", None)
    if callable(outcome):  # wta.execution.positions.ClosedPosition
        out = outcome()
        if isinstance(out, Mapping):
            d = dict(out)
            pos = getattr(value, "position", None)
            if pos is not None and getattr(pos, "entry_ts", None) is not None:
                d.setdefault("entry_ts", pos.entry_ts)
            return d
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    return None


def _records(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, pd.DataFrame):
        return [dict(r) for r in values.to_dict("records")]
    if isinstance(values, Mapping):
        values = [values]
    return [r for r in (_record(v) for v in values) if r is not None]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else ""
    if isinstance(value, float):
        return repr(float(value)) if math.isfinite(value) else ""
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False)
    if isinstance(value, datetime):
        epoch = _to_epoch(value)
        return _iso(epoch) if epoch is not None else ""
    return str(value)


def _strategies(meta: Mapping[str, Any]) -> list[str]:
    raw = meta.get("strategies")
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [str(s).strip() for s in items if str(s).strip()]


def _ai_on(meta: Mapping[str, Any]) -> bool | None:
    raw = meta.get("ai")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"on", "true", "1", "yes", "enabled"}:
        return True
    if text in {"off", "false", "0", "no", "disabled"}:
        return False
    return None


def _is_synthetic(meta: Mapping[str, Any]) -> bool:
    """``meta["synthetic"]``, or a data source that names synthetic data (err on the honest side)."""
    return _flag(meta.get("synthetic")) or "synthetic" in str(meta.get("data_source") or "").lower()


def _date_text(epoch: float | None) -> str | None:
    return None if epoch is None else _dt(epoch).strftime("%Y-%m-%d")


def _rel(path: Path, start: Path) -> str:
    """``path`` relative to ``start`` (posix), so generated markdown never embeds absolute paths."""
    return Path(os.path.relpath(path.resolve(), start.resolve())).as_posix()


# ================================================================= input normalisation


@dataclass(frozen=True)
class _EqRow:
    ts: float
    equity: float
    raw: Any  # the input value, so equity.csv keeps Decimal precision
    valuation: tuple[Any, ...]  # raw tmn, usdt_tmn_value, coins_tmn_value (None when absent)


def _equity_rows(equity: Any) -> list[_EqRow]:
    """Equity rows sorted by time, one per timestamp (the last wins); unusable rows dropped.

    Accepts ``(ts, equity[, tmn, usdt_tmn_value, coins_tmn_value, ...])`` sequences (the store's
    ``equity`` table order), mappings with ``ts``/``equity_tmn`` (or ``time``/``equity``), pydantic
    models, a DataFrame (``ts`` column or a DatetimeIndex) or a Series indexed by time.
    """
    if equity is None:
        return []
    items: Iterable[Any]
    if isinstance(equity, pd.DataFrame):
        df = equity
        if not ({"ts", "time"} & set(df.columns)) and isinstance(df.index, pd.DatetimeIndex):
            df = df.rename_axis("ts").reset_index()
        items = df.to_dict("records")
    elif isinstance(equity, pd.Series):
        items = [{"ts": t, "equity_tmn": v} for t, v in equity.items()]
    else:
        items = equity
    rows: list[_EqRow] = []
    for item in items:
        rec = _record(item) if not isinstance(item, Mapping | str | bytes) else item
        if isinstance(rec, Mapping):
            ts = rec.get("ts", rec.get("time"))
            eq = rec.get("equity_tmn", rec.get("equity"))
            valuation = tuple(rec.get(k) for k in VALUATION_KEYS)
        elif isinstance(item, str | bytes):
            continue
        else:
            try:
                values = list(item)
            except TypeError:
                continue
            if len(values) < 2:
                continue
            ts, eq = values[0], values[1]
            valuation = tuple(values[i] if i < len(values) else None for i in (2, 3, 4))
        t, v = _to_epoch(ts), _num(eq)
        if t is not None and v is not None:
            rows.append(_EqRow(t, v, eq, valuation))
    rows.sort(key=lambda r: r.ts)  # stable: equal timestamps keep input order
    out: list[_EqRow] = []
    for r in rows:
        if out and out[-1].ts == r.ts:
            out[-1] = r
        else:
            out.append(r)
    return out


def _drawdowns(points: Sequence[tuple[float, float]], start_peak: float | None = None) -> list[float]:
    """Drawdown below the running peak, ``(peak - equity) / peak`` (>= 0), for every equity point.

    Same convention as the engine's ``drawdown_pct`` and ``Metrics.max_drawdown_pct`` (positive);
    ``start_peak`` (the initial equity) is the first peak when it is above the first point.
    """
    out: list[float] = []
    peak = start_peak if start_peak is not None else -math.inf
    for _, v in points:
        peak = max(peak, v)
        out.append(max(0.0, (peak - v) / peak) if peak > 0 else 0.0)
    return out


def _plan_records(plans: Any, decisions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalised plans ``{ts, regimes{sym: (regime, confidence)}, enabled, size_mult, cached, clamped, model}``.

    Accepts ``MarketPlan`` models/dumps, ``Store.load_plans`` rows (``{"ts", "plan"}``) and
    ``kind="plan"`` decision records (``plan_snapshot``). Without ``plans``: the plan decisions.
    """
    source = _records(plans)
    if not source:
        source = [d for d in decisions if d.get("kind") == "plan"]
    out: list[dict[str, Any]] = []
    for rec in source:
        inner: Mapping[str, Any] = rec
        for key in ("plan_snapshot", "plan"):
            if isinstance(rec.get(key), Mapping):
                inner = rec[key]
                break
        ts = _to_epoch(inner.get("ts"))
        if ts is None:
            ts = _to_epoch(rec.get("ts"))
        regimes: dict[str, tuple[str, float | None]] = {}
        raw_regimes = inner.get("regimes")
        if isinstance(raw_regimes, Mapping):
            for sym, call in raw_regimes.items():
                if isinstance(call, Mapping):
                    if call.get("regime"):
                        regimes[str(sym)] = (str(call["regime"]), _num(call.get("confidence")))
                elif call:
                    regimes[str(sym)] = (str(call), None)
        enabled = inner.get("enabled_strategies")
        out.append(
            {
                "ts": ts,
                "regimes": regimes,
                "enabled": sorted(str(s) for s in enabled) if isinstance(enabled, list | tuple) else None,
                "size_mult": _num(inner.get("size_multiplier")),
                "cached": _flag(inner.get("cached")),
                "clamped": bool(inner.get("clamped")),
                "model": str(inner.get("model") or ""),
            }
        )
    out.sort(key=lambda p: (p["ts"] is None, p["ts"] or 0.0))
    return out


def _veto_counts(metrics: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Vetoes by risk code: from metrics when present, else counted from the decisions."""
    for key in ("vetoes_by_code", "vetoes_by_risk_code", "vetoes"):
        raw = metrics.get(key)
        if isinstance(raw, Mapping) and raw:
            return {str(k): int(_num(v) or 0) for k, v in raw.items()}
    return dict(_vetoes_by_code_from_decisions(list(decisions))) if decisions else {}


def _sorted_counts(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _trade_columns(trades: Sequence[Mapping[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for t in trades:
        keys.update(str(k) for k in t)
    if not keys:
        return list(DEFAULT_TRADE_COLUMNS)
    preferred = [c for c in _TRADE_COLUMNS if c in keys]
    return preferred + sorted(keys - set(preferred))


def _display_columns(columns: Sequence[str]) -> list[str]:
    """Up to ``TRADES_TABLE_MAX_COLS`` columns: the preferred ones present, then the rest in order."""
    preferred = [c for c in _TRADE_DISPLAY if c in columns]
    rest = [c for c in columns if c not in preferred]
    return (preferred + rest)[:TRADES_TABLE_MAX_COLS]


def _trade_pnl(trade: Mapping[str, Any]) -> float | None:
    return _num(trade.get("pnl_tmn", trade.get("pnl")))


def _trade_fee(trade: Mapping[str, Any]) -> float | None:
    for key in ("fees_tmn", "fee_tmn", "fees_paid_tmn"):
        if key in trade:
            return _num(trade[key])
    return None


# ================================================================= charts (Agg, no pyplot state)


def _figure(height: float = 5.5) -> tuple[Figure, Any]:
    fig = Figure(figsize=(_FIG_W, height), dpi=_DPI, layout="constrained")
    FigureCanvasAgg(fig)
    return fig, fig.add_subplot()


def _stamp(fig: Figure, synthetic: bool) -> None:
    """Diagonal "SYNTHETIC DATA" watermark, sized to the figure so it never dominates short charts."""
    if synthetic:
        fig.text(
            0.5,
            0.5,
            "SYNTHETIC DATA",
            ha="center",
            va="center",
            rotation=20,
            fontsize=min(64.0, 11.0 * fig.get_figheight()),
            color="#c62828",
            alpha=0.12,
            weight="bold",
        )


def _save(fig: Figure, path: Path) -> None:
    fig.savefig(path, format="png", dpi=_DPI, metadata=_PNG_METADATA)


def _suffix(synthetic: bool) -> str:
    return " — SYNTHETIC DATA" if synthetic else ""


def _placeholder(path: Path, title: str, message: str, synthetic: bool) -> None:
    with matplotlib.rc_context(_RC):
        fig, ax = _figure(height=3.0)
        ax.axis("off")
        ax.set_title(title + _suffix(synthetic))
        ax.text(
            0.5, 0.5, message, ha="center", va="center", fontsize=14, color="#555555", transform=ax.transAxes
        )
        _stamp(fig, synthetic)
        _save(fig, path)


def _time_axis(ax: Any) -> None:
    locator = mdates.AutoDateLocator(tz=UTC)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=UTC))
    ax.set_xlabel("time (UTC)")


def _plot_equity(
    path: Path, points: Sequence[tuple[float, float]], start: float | None, title: str, synthetic: bool
) -> None:
    if len(points) < 2:
        _placeholder(path, f"Equity (TMN) — {title}", "No equity data in the window", synthetic)
        return
    with matplotlib.rc_context(_RC):
        fig, ax = _figure()
        xs = [_dt(t) for t, _ in points]
        ys = [v for _, v in points]
        base = start if start is not None else ys[0]
        ax.plot(xs, ys, color="#1565c0", linewidth=1.6, label="equity")
        ax.axhline(base, color="#616161", linewidth=1.0, linestyle="--", label=f"initial {base:,.0f}")
        ax.set_title(f"Equity (TMN) — {title}{_suffix(synthetic)}")
        ax.set_ylabel("equity (TMN)")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:,.0f}"))
        ax.legend(loc="best")
        _time_axis(ax)
        _stamp(fig, synthetic)
        _save(fig, path)


def _plot_drawdown(
    path: Path, points: Sequence[tuple[float, float]], drawdowns: Sequence[float], title: str, synthetic: bool
) -> None:
    if len(points) < 2:
        _placeholder(path, f"Drawdown — {title}", "No equity data in the window", synthetic)
        return
    with matplotlib.rc_context(_RC):
        fig, ax = _figure(height=4.5)
        xs = [_dt(t) for t, _ in points]
        dd = [-d * 100 for d in drawdowns]  # plotted below zero; the value itself is positive
        ax.fill_between(xs, dd, 0, color="#c62828", alpha=0.35, linewidth=0)
        ax.plot(xs, dd, color="#c62828", linewidth=1.2)
        worst = min(dd)
        ax.axhline(worst, color="#616161", linewidth=1.0, linestyle="--", label=f"max drawdown {-worst:.2f}%")
        ax.set_title(f"Drawdown below running peak (%) — {title}{_suffix(synthetic)}")
        ax.set_ylabel("drawdown (%)")
        ax.set_ylim(bottom=min(worst * 1.1, -0.05), top=max(0.05, -worst * 0.05))
        ax.legend(loc="lower left")
        _time_axis(ax)
        _stamp(fig, synthetic)
        _save(fig, path)


def _plot_vetoes(path: Path, counts: Mapping[str, int], title: str, synthetic: bool) -> None:
    items = _sorted_counts(counts)
    if not items:
        _placeholder(path, f"Risk vetoes by code — {title}", "No vetoes recorded", synthetic)
        return
    with matplotlib.rc_context(_RC):
        fig, ax = _figure(height=max(3.0, 0.45 * len(items) + 1.6))
        labels = [k for k, _ in items][::-1]
        values = [v for _, v in items][::-1]
        bars = ax.barh(labels, values, color="#6a1b9a")
        ax.bar_label(bars, labels=[f"{v:,}" for v in values], padding=4)
        ax.set_xlabel("vetoes (a veto with several failed checks counts once per code)")
        ax.set_title(f"Risk vetoes by code — {title}{_suffix(synthetic)}")
        ax.grid(axis="y", visible=False)
        ax.set_xlim(right=max(values) * 1.15 + 1)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        _stamp(fig, synthetic)
        _save(fig, path)


def _plot_regimes(
    path: Path, plans: Sequence[Mapping[str, Any]], end_ts: float | None, title: str, synthetic: bool
) -> None:
    timed = [p for p in plans if p["ts"] is not None and p["regimes"]]
    if not timed:
        _placeholder(path, f"AI regime timeline — {title}", "No AI plans in this run", synthetic)
        return
    symbols = sorted({s for p in timed for s in p["regimes"]})
    gaps = [b["ts"] - a["ts"] for a, b in zip(timed, timed[1:], strict=False) if b["ts"] > a["ts"]]
    default_w = sorted(gaps)[len(gaps) // 2] if gaps else 300.0
    last_end = max(end_ts or 0.0, timed[-1]["ts"] + default_w)
    with matplotlib.rc_context(_RC):
        fig, ax = _figure(height=max(3.0, 0.55 * len(symbols) + 1.8))
        seen: set[str] = set()
        for row, sym in enumerate(symbols):
            for i, plan in enumerate(timed):
                call = plan["regimes"].get(sym)
                if call is None:
                    continue
                start = plan["ts"]
                stop = timed[i + 1]["ts"] if i + 1 < len(timed) else last_end
                if stop <= start:
                    continue
                regime = call[0]
                seen.add(regime)
                x0 = mdates.date2num(_dt(start))
                width = mdates.date2num(_dt(stop)) - x0
                ax.broken_barh(
                    [(x0, width)],
                    (row - 0.4, 0.8),
                    facecolors=_REGIME_COLORS.get(regime, _UNKNOWN_REGIME_COLOR),
                )
        ax.set_yticks(range(len(symbols)), labels=symbols)
        ax.set_ylim(-0.6, len(symbols) - 0.4)
        ax.xaxis_date(tz=UTC)
        _time_axis(ax)
        ax.grid(axis="y", visible=False)
        order = [r for r in _REGIME_COLORS if r in seen] + sorted(seen - set(_REGIME_COLORS))
        ax.legend(
            handles=[Patch(color=_REGIME_COLORS.get(r, _UNKNOWN_REGIME_COLOR), label=r) for r in order],
            loc="upper left",
            bbox_to_anchor=(1.0, 1.0),
        )
        ax.set_title(f"AI regime timeline — {title}{_suffix(synthetic)}")
        _stamp(fig, synthetic)
        _save(fig, path)


# ================================================================= report sections


def _window(
    meta: Mapping[str, Any], points: Sequence[tuple[float, float]], metrics: Mapping[str, Any]
) -> dict[str, Any]:
    """Requested window (meta) and the data actually present (meta, else equity, else metrics)."""
    req_from, req_to = _to_epoch(meta.get("window_from")), _to_epoch(meta.get("window_to"))
    data_from, data_to = _to_epoch(meta.get("data_from")), _to_epoch(meta.get("data_to"))
    if data_from is None:
        data_from = points[0][0] if points else _to_epoch(metrics.get("start_ts"))
    if data_to is None:
        data_to = points[-1][0] if points else _to_epoch(metrics.get("end_ts"))

    def hours(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else max(0.0, (b - a) / 3600)

    data_hours = _num(meta.get("data_hours"))
    return {
        "requested_from": req_from,
        "requested_to": req_to,
        "requested_hours": hours(req_from, req_to),
        "data_from": data_from,
        "data_to": data_to,
        "data_hours": data_hours if data_hours is not None else hours(data_from, data_to),
    }


def _report_date(meta: Mapping[str, Any], window: Mapping[str, Any]) -> str | None:
    for epoch in (_to_epoch(meta.get("generated_at")), window["requested_to"], window["data_to"]):
        if epoch is not None:
            return _date_text(epoch)
    return None


def _span_text(a: float | None, b: float | None, hours: float | None) -> str:
    if a is None and b is None:
        return "none"
    span = f"{_iso(a)} → {_iso(b)}"
    return f"{span} ({hours:,.1f} h)" if hours is not None else span


def _fees(
    metrics: Mapping[str, Any],
    meta: Mapping[str, Any],
    trades: Sequence[Mapping[str, Any]],
    points: Sequence[tuple[float, float]],
    data_hours: float | None,
) -> dict[str, Any]:
    paid = _num(metrics.get("fees_paid_tmn"))
    source = str(metrics.get("fees_source") or "metrics")
    if paid is None or (paid == 0 and source == "none"):
        fees = [f for f in (_trade_fee(t) for t in trades) if f is not None]
        if fees:
            paid, source = sum(fees), "trades"
        elif paid is None:
            source = "none"
    initial = _num(metrics.get("initial_equity_tmn"))
    if initial is None:
        initial = _num(meta.get("initial_equity_tmn"))
    if initial is None and points:
        initial = points[0][1]
    budget_pct = _num(meta.get("fee_budget_pct"))
    if budget_pct is None:
        budget_pct = DEFAULT_FEE_BUDGET_PCT
    budget = initial * budget_pct if initial is not None else None
    used = paid / budget if paid is not None and budget else None
    per_72h = paid * CHALLENGE_HOURS / data_hours if paid is not None and data_hours else None
    return {
        "fees_paid_tmn": paid,
        "source": source,
        "initial_equity_tmn": initial,
        "fee_budget_pct": budget_pct,
        "fee_budget_tmn": budget,
        "budget_used_pct": used,
        "data_hours": data_hours,
        "fees_per_72h_tmn": per_72h,
        "budget_used_per_72h_pct": per_72h / budget if per_72h is not None and budget else None,
    }


def _regime_table(dist: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """``{symbol: {regime: count}}`` (the metrics shape); a flat ``{regime: count}`` becomes ``{"all": ...}``."""
    if dist and all(not isinstance(v, Mapping) for v in dist.values()):
        dist = {"all": dist}
    out: dict[str, dict[str, int]] = {}
    for sym, counts in dist.items():
        if isinstance(counts, Mapping):
            out[str(sym)] = {str(r): int(_num(c) or 0) for r, c in counts.items()}
    return out


def _ai_stats(metrics: Mapping[str, Any], plans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """AI section numbers: from the plans when given (so they match the timeline), else from metrics."""
    nested = metrics.get("ai") if isinstance(metrics.get("ai"), Mapping) else {}
    src: dict[str, Any] = {**metrics, **nested}
    if plans:
        changes = sum(
            1
            for a, b in zip(plans, plans[1:], strict=False)
            if a["enabled"] is not None and b["enabled"] is not None and a["enabled"] != b["enabled"]
        )
        mults = [p["size_mult"] for p in plans if p["size_mult"] is not None]
        counts: dict[str, Counter[str]] = {}
        for p in plans:
            for sym, call in p["regimes"].items():
                counts.setdefault(sym, Counter())[call[0]] += 1
        stats: dict[str, Any] = {
            "source": "plans",
            "n_plans": len(plans),
            "avg_size_multiplier": sum(mults) / len(mults) if mults else None,
            "n_plan_changes_of_enabled_strategies": changes,
            "regime_distribution": {s: dict(sorted(c.items())) for s, c in sorted(counts.items())},
            "plans_cached": sum(1 for p in plans if p["cached"]),
            "plans_clamped": sum(1 for p in plans if p["clamped"]),
            "models": sorted({p["model"] for p in plans if p["model"]}),
        }
    else:
        dist = src.get("regime_distribution")
        n_plans = _num(src.get("n_plans"))
        stats = {
            "source": "metrics" if n_plans else "none",
            "n_plans": int(n_plans or 0),
            "avg_size_multiplier": _num(src.get("avg_size_multiplier")),
            "n_plan_changes_of_enabled_strategies": int(
                _num(src.get("n_plan_changes_of_enabled_strategies")) or 0
            ),
            "regime_distribution": _regime_table(dist) if isinstance(dist, Mapping) else {},
            "plans_cached": None,
            "plans_clamped": None,
            "models": [],
        }
    gated = src.get("regime_gated") if isinstance(src.get("regime_gated"), Mapping) else {}
    stats["s3_entries_gated"] = _num(gated.get("s3_entries_gated", src.get("trades_gated_by_regime")))
    stats["regime_flip_exits"] = _num(gated.get("regime_flip_exits"))
    return stats


def _ablation_items(ablation: Mapping[str, Any]) -> list[tuple[str, Any, Any, Any]]:
    """``[(metric, off, on, delta)]`` from either accepted ablation shape (see ``_ablation_table``)."""
    on = next((ablation[k] for k in _ABLATION_ON if isinstance(ablation.get(k), Mapping)), None)
    off = next((ablation[k] for k in _ABLATION_OFF if isinstance(ablation.get(k), Mapping)), None)
    items: list[tuple[str, Any, Any, Any]] = []
    if on is not None or off is not None:
        on, off = _as_dict(on), _as_dict(off)
        delta = ablation.get("delta") if isinstance(ablation.get("delta"), Mapping) else {}
        numeric = {k for src in (on, off, delta) for k, v in src.items() if _num(v) is not None}
        keys = [k for k in _ABLATION_ORDER if k in numeric]
        if not keys:  # not metrics dicts: compare every numeric, non-timestamp field
            keys = sorted(k for k in numeric if _guess_kind(k, on.get(k, off.get(k))) != "ts")
        items += [(k, off.get(k), on.get(k), delta.get(k)) for k in keys]
        codes_on, codes_off = on.get("vetoes_by_code"), off.get("vetoes_by_code")
        if isinstance(codes_on, Mapping) or isinstance(codes_off, Mapping):
            codes_on, codes_off = codes_on or {}, codes_off or {}
            for code in sorted(set(codes_on) | set(codes_off)):
                items.append((f"vetoes_by_code.{code}", codes_off.get(code, 0), codes_on.get(code, 0), None))
        return items

    def walk(prefix: str, mapping: Mapping[str, Any]) -> None:
        for key, val in mapping.items():
            name = f"{prefix}{key}"
            if isinstance(val, Mapping):
                if any(k in val for k in (*_ABLATION_ON, *_ABLATION_OFF, "delta")):
                    vo = next((val[k] for k in _ABLATION_OFF if k in val), None)
                    vn = next((val[k] for k in _ABLATION_ON if k in val), None)
                    items.append((name, vo, vn, val.get("delta")))
                else:
                    walk(name + ".", val)
            elif _num(val) is not None:
                items.append((name, None, None, val))

    walk("", ablation)
    return items


def _ablation_table(ablation: Mapping[str, Any]) -> str:
    """Table ``metric | ai=off | ai=on | delta (on − off)``.

    Accepted shapes: per metric ``{key: {"on", "off", "delta"}}`` as ``backtest.metrics.ablation_delta``
    returns it (nested groups such as ``vetoes_by_code`` included), or ``{"ai_on": metrics,
    "ai_off": metrics[, "delta": {...}]}`` (``on``/``off`` aliases too). A ``note`` string is shown.
    """
    items = _ablation_items(ablation)
    if not items:
        return "_Ablation data was given but contained no comparable metrics._"
    rank = {k: i for i, k in enumerate(_ABLATION_ORDER)}
    items.sort(key=lambda it: (rank.get(it[0], len(rank)), "." in it[0], it[0]))
    rows = []
    for name, off, on, delta in items:
        sample = on if on is not None else (off if off is not None else delta)
        kind = _guess_kind(name.rsplit(".", 1)[-1], sample)
        d, a, b = _num(delta), _num(off), _num(on)
        if d is None and a is not None and b is not None:
            d = b - a
        rows.append([name, _fmt(kind, off), _fmt(kind, on), _fmt_delta(kind, d) if d is not None else "n/a"])
    parts = []
    note = ablation.get("note") or ablation.get("notes")
    if isinstance(note, str) and note.strip():
        parts += [f"_{note.strip()}_", ""]
    parts.append(
        _md_table(["metric", "ai=off", "ai=on", "delta (on − off)"], rows, right=[False, True, True, True])
    )
    if any(it[0] == "max_drawdown_pct" for it in items):
        parts += [
            "",
            "_Drawdowns are positive fractions: a positive max_drawdown_pct delta means the AI run drew down more._",
        ]
    return "\n".join(parts)


def _breakdown_table(
    data: Any, first: str, specs: Sequence[tuple[str, str, str]] = (), value_kind: str | None = None
) -> str | None:
    """``{name: {col: v}}`` / ``{name: v}`` / ``[{first: name, ...}]`` -> markdown table.

    ``specs`` order and label the known columns; other scalar columns follow (``_BREAKDOWN_SKIP``
    excepted). ``value_kind`` formats plain ``{name: v}`` values (e.g. ``"pct"`` for daily returns).
    """
    if isinstance(data, list):
        mapping: dict[str, Any] = {}
        for i, row in enumerate(data):
            if isinstance(row, Mapping):
                name = row.get(first, row.get("name", row.get("date", i)))
                mapping[str(name)] = {k: v for k, v in row.items() if k not in (first, "name")}
        data = mapping
    if not isinstance(data, Mapping) or not data:
        return None
    present: list[str] = []
    for val in data.values():
        if isinstance(val, Mapping):
            for k in val:
                if str(k) not in present and not isinstance(val[k], Mapping | list):
                    present.append(str(k))
    if not present:
        rows_v = [
            [str(k), _fmt(value_kind or _guess_kind(str(k), v), v)]
            for k, v in sorted(data.items(), key=lambda kv: str(kv[0]))
        ]
        return _md_table([first, "value"], rows_v, right=[False, True])
    known = {k for k, _, _ in specs}
    cols = [(k, label, kind) for k, label, kind in specs if k in present]
    cols += [(k, k, "") for k in present if k not in known and k not in _BREAKDOWN_SKIP]
    rows = []
    for name in sorted(data, key=str):
        val = data[name] if isinstance(data[name], Mapping) else {}
        rows.append(
            [str(name)] + [_fmt(kind or _guess_kind(k, val.get(k)), val.get(k)) for k, _, kind in cols]
        )
    return _md_table([first, *(label for _, label, _ in cols)], rows, right=[False] + [True] * len(cols))


def _per_strategy_from_trades(trades: Sequence[Mapping[str, Any]]) -> str | None:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(t.get("strategy") or "unknown"), []).append(t)
    if not groups:
        return None
    rows = []
    for name in sorted(groups):
        items = groups[name]
        pnls = [p for p in (_trade_pnl(t) for t in items) if p is not None]
        fees = [f for f in (_trade_fee(t) for t in items) if f is not None]
        holds = [h for h in (_num(t.get("hold_s")) for t in items) if h is not None]
        wins = sum(1 for p in pnls if p > 0)
        rows.append(
            [
                name,
                f"{len(items):,}",
                _fmt("pct", wins / len(pnls)) if pnls else "n/a",
                _fmt("tmn", sum(pnls)) if pnls else "n/a",
                _fmt("tmn", sum(fees)) if fees else "n/a",
                _fmt("dur", sum(holds) / len(holds)) if holds else "n/a",
            ]
        )
    return _md_table(
        ["strategy", "trades", "win rate", "PnL (TMN)", "fees (TMN)", "avg hold"],
        rows,
        right=[False, True, True, True, True, True],
    )


def _metric_rows(metrics: Mapping[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    done_keys: set[str] = set()
    done_labels: set[str] = set()
    for key, label, kind in _METRIC_SPECS:
        if key in metrics and not isinstance(metrics[key], Mapping | list) and label not in done_labels:
            rows.append([label, _fmt(kind, metrics[key])])
            done_labels.add(label)
        done_keys.add(key)
    for key in sorted(metrics, key=str):
        name = str(key)
        if key in done_keys or name in _HIDDEN_METRIC_KEYS or name.endswith("_note"):
            continue
        val = metrics[key]
        if isinstance(val, Mapping):
            if 0 < len(val) <= 8 and all(not isinstance(v, Mapping | list) for v in val.values()):
                for sub in sorted(val, key=str):
                    rows.append([f"{name}.{sub}", _fmt(_guess_kind(str(sub), val[sub]), val[sub])])
            continue
        if isinstance(val, list):
            continue
        rows.append([name, _fmt(_guess_kind(name, val), val)])
    return rows


def _metric_notes(metrics: Mapping[str, Any]) -> list[str]:
    """The metrics' own caveats (``*_note``, ``notes``) and where fees/turnover came from."""
    out: list[str] = []
    for key in sorted(k for k in metrics if str(k).endswith("_note")):
        text = str(metrics[key] or "").strip()
        if text:
            out.append(text)
    notes = metrics.get("notes")
    if isinstance(notes, str):
        notes = [notes]
    if isinstance(notes, list | tuple):
        out += [str(n).strip() for n in notes if str(n).strip()]
    sources = [
        f"{what} from {metrics[key]}"
        for what, key in (("fees", "fees_source"), ("turnover", "turnover_source"))
        if metrics.get(key) and metrics.get(key) != "none"
    ]
    if sources:
        out.append("Sources: " + "; ".join(sources) + ".")
    return list(dict.fromkeys(out))


def _end_state(metrics: Mapping[str, Any]) -> list[list[str]] | None:
    rvu = metrics.get("realized_vs_unrealized")
    if not isinstance(rvu, Mapping) or not any(_num(rvu.get(k)) is not None for k, _, _ in _END_STATE_SPECS):
        return None
    rows = [[label, _fmt(kind, rvu.get(key))] for key, label, kind in _END_STATE_SPECS if key in rvu]
    unpriced = rvu.get("unpriced_assets")
    if isinstance(unpriced, list | tuple) and unpriced:
        rows.append(["Assets without a TMN price (left out)", ", ".join(str(a) for a in unpriced)])
    return rows


def _limitations(
    meta: Mapping[str, Any],
    metrics: Mapping[str, Any],
    window: Mapping[str, Any],
    points: Sequence[tuple[float, float]],
    trades: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    extra: Sequence[str],
    insufficient: bool,
) -> list[str]:
    """Caller-provided limitations first, then the ones derived from meta/metrics (deduplicated)."""
    synthetic = _is_synthetic(meta)
    driver = str(meta.get("driver") or "").lower()
    mode = str(meta.get("mode") or "").lower()
    strategies = {s.upper() for s in _strategies(meta)}
    ai = _ai_on(meta)
    data_h, req_h = window["data_hours"], window["requested_hours"]
    auto: list[str] = []
    if synthetic:
        auto.append(
            "The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run "
            "exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence "
            "of strategy performance."
        )
    if insufficient:
        auto.append(
            f"Insufficient data: {len(points)} equity point(s) in the window, so performance metrics are "
            "undefined. Record market data first (`python -m wta record`, `python -m wta backfill`) and rerun."
        )
    if req_h is not None and data_h is not None and data_h + 1e-9 < req_h * 0.95:
        auto.append(
            f"The requested window is {req_h:,.1f} h but the data present covers only {data_h:,.1f} h."
        )
    if driver == "books" and "S1" in strategies:
        source = "generated (synthetic)" if synthetic else "recorded"
        if data_h is not None:
            auto.append(f"S1 backtest window limited to {source} {data_h:,.1f} h of order books.")
        else:
            auto.append(f"S1 backtest window limited to the {source} order-book hours.")
    if driver == "candles":
        auto.append(
            "S1 not backtested on candles (candles carry no cross-book information); S1 evidence comes from "
            "the survey, the book-driven window and the live logs."
        )
        auto.append(
            "Candle-driven fills use a synthetic book (mid = close, the market's median observed spread, "
            "unlimited depth at the best level); queue position and depth are not modelled."
        )
    if mode == "backtest" or driver in {"books", "candles"}:
        auto.append(
            "Fills come from the pessimistic fill model (orders act one step later, at most 80% of the "
            "best-level quantity, taker fee when crossing); real queue position is not modelled."
        )
    if mode == "shadow":
        auto.append(
            "Shadow fills are paper fills against the live book (PaperExchange): they consume no liquidity "
            "and ignore queue position."
        )
    if ai is False:
        auto.append(
            "AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation."
        )
    elif ai is True and not plans:
        auto.append("ai=on but no AI plans were recorded in this run.")
    offline = sorted({p["model"] for p in plans if OFFLINE_MODEL_MARKER in p["model"].lower()})
    if offline:
        auto.append(
            f"AI plans came from the deterministic offline rules provider ({', '.join(offline)}), not an LLM: "
            "the AI section shows the plumbing and the rules fallback, not LLM judgement."
        )
    has_ratio = _num(metrics.get("sharpe_annualized")) is not None or _num(metrics.get("sortino")) is not None
    if has_ratio and data_h is not None and data_h < 30 * 24:
        n_hourly = _num(metrics.get("n_hourly_returns"))
        sample = f"{int(n_hourly):,} hourly returns over " if n_hourly is not None else "hourly returns over "
        hours = f"{data_h:,.1f}" if data_h < 10 else f"{data_h:,.0f}"
        auto.append(
            f"Sharpe and Sortino are computed from {sample}{hours} h of data: reported, but not "
            "meaningful over so short a window."
        )
    n_trades = _num(metrics.get("n_trades"))
    if not insufficient and (n_trades == 0 or (n_trades is None and not trades)):
        auto.append("No closed trades in the window: win rate, profit factor and average PnL are undefined.")
    rvu = metrics.get("realized_vs_unrealized")
    unreal = _num(rvu.get("unrealized_tmn")) if isinstance(rvu, Mapping) else None
    if unreal is not None and abs(unreal) >= 1:
        if _flag(meta.get("flatten_at_end")):
            auto.append(
                f"{unreal:,.0f} TMN was still held outside TMN (unrealized) at the end of the window although "
                "the deadline flatten ran (dust below a market's minimum order, or an exit still open); the "
                "challenge counts realized TMN only."
            )
        else:
            auto.append(
                f"{unreal:,.0f} TMN was still held outside TMN (unrealized) at the end of the window; the "
                "challenge counts realized TMN only, so this run was not flattened."
            )
    if isinstance(rvu, Mapping) and rvu.get("unpriced_assets"):
        assets = ", ".join(str(a) for a in rvu["unpriced_assets"])
        auto.append(f"End-state valuation left out assets without a TMN price: {assets}.")
    out: list[str] = []
    for item in [*extra, *auto]:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


# ================================================================= write_report


def write_report(
    out_dir: Path | str,
    title: str,
    meta: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | Any | None,
    equity: Any,
    trades: Any,
    decisions: Any = None,
    plans: Any = None,
    ablation: Mapping[str, Any] | Any | None = None,
    limitations: Sequence[str] | None = None,
) -> Path:
    """Write ``report.md``, ``summary.json``, ``trades.csv``, ``equity.csv`` and the PNGs; return the md path.

    ``meta`` keys used: ``config_hash, git_commit, mode, driver, window_from, window_to, data_source,
    synthetic, ai, strategies`` and optionally ``data_from, data_to, data_hours, generated_at,
    initial_equity_tmn, fee_budget_pct``; any other scalar keys are listed in the header table.
    ``limitations`` are shown first, before the automatic ones. Never raises on empty inputs: an
    empty store produces an "insufficient data" report.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta_d = _as_dict(meta)
    metrics_d = _as_dict(metrics)
    ablation_d = _as_dict(ablation) if ablation is not None else None
    trade_rows = _records(trades)
    decision_rows = _records(decisions)
    equity_rows = _equity_rows(equity)
    points = [(r.ts, r.equity) for r in equity_rows]
    plan_rows = _plan_records(plans, decision_rows)
    synthetic = _is_synthetic(meta_d)
    insufficient = _flag(meta_d.get("insufficient_data")) or metrics_d.get("insufficient_data") is True
    insufficient = insufficient or len(points) < 2
    window = _window(meta_d, points, metrics_d)
    report_date = _report_date(meta_d, window)
    vetoes = _veto_counts(metrics_d, decision_rows)
    fees = _fees(metrics_d, meta_d, trade_rows, points, window["data_hours"])
    ai_stats = _ai_stats(metrics_d, plan_rows)
    limits = _limitations(
        meta_d, metrics_d, window, points, trade_rows, plan_rows, list(limitations or []), insufficient
    )
    start_equity = _num(metrics_d.get("initial_equity_tmn"))
    if start_equity is None:
        start_equity = _num(meta_d.get("initial_equity_tmn"))
    drawdowns = _drawdowns(points, start_equity)

    # --- data files
    columns = _trade_columns(trade_rows)
    with (out / TRADES_CSV).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for t in trade_rows:
            writer.writerow([_csv_value(t.get(c)) for c in columns])
    with (out / EQUITY_CSV).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(EQUITY_CSV_COLUMNS)
        for row, dd in zip(equity_rows, drawdowns, strict=True):
            valuation = [_csv_value(v) for v in row.valuation]
            writer.writerow([_iso(row.ts), repr(row.ts), _csv_value(row.raw), *valuation, f"{dd:.8f}"])

    # --- charts
    chart_title = title if len(title) <= 60 else title[:57] + "..."
    _plot_equity(out / EQUITY_PNG, points, start_equity, chart_title, synthetic)
    _plot_drawdown(out / DRAWDOWN_PNG, points, drawdowns, chart_title, synthetic)
    _plot_vetoes(out / VETOES_PNG, vetoes, chart_title, synthetic)
    _plot_regimes(
        out / REGIMES_PNG, plan_rows, window["data_to"] or window["requested_to"], chart_title, synthetic
    )

    # --- report.md
    run = _Run(
        out=out,
        title=title,
        meta=meta_d,
        metrics=metrics_d,
        window=window,
        report_date=report_date,
        points=points,
        trades=trade_rows,
        columns=columns,
        decisions=decision_rows,
        plans=plan_rows,
        vetoes=vetoes,
        fees=fees,
        ai=ai_stats,
        ablation=ablation_d,
        limitations=limits,
        synthetic=synthetic,
        insufficient=insufficient,
    )
    (out / REPORT_MD).write_text(_render_markdown(run), encoding="utf-8")

    # --- summary.json
    summary = {
        "title": title,
        "report_date": report_date,
        "synthetic": synthetic,
        "insufficient_data": insufficient,
        "meta": meta_d,
        "window": {
            k: (_iso(v) if k.endswith(("_from", "_to")) and v is not None else v) for k, v in window.items()
        },
        "metrics": metrics_d,
        "counts": {
            "equity_points": len(points),
            "trades": len(trade_rows),
            "decisions": len(decision_rows),
            "plans": len(plan_rows),
            "decisions_by_kind": dict(sorted(Counter(str(d.get("kind")) for d in decision_rows).items())),
        },
        "vetoes_by_code": dict(_sorted_counts(vetoes)),
        "fees": fees,
        "ai": ai_stats,
        "ablation": ablation_d,
        "limitations": limits,
        "files": list(REPORT_FILES),
    }
    (out / SUMMARY_JSON).write_text(
        json.dumps(_jsonable(summary), sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out / REPORT_MD


@dataclass(frozen=True)
class _Run:
    """Everything ``report.md`` is rendered from (inputs already normalised)."""

    out: Path
    title: str
    meta: Mapping[str, Any]
    metrics: Mapping[str, Any]
    window: Mapping[str, Any]
    report_date: str | None
    points: Sequence[tuple[float, float]]
    trades: Sequence[Mapping[str, Any]]
    columns: Sequence[str]
    decisions: Sequence[Mapping[str, Any]]
    plans: Sequence[Mapping[str, Any]]
    vetoes: Mapping[str, int]
    fees: Mapping[str, Any]
    ai: Mapping[str, Any]
    ablation: Mapping[str, Any] | None
    limitations: Sequence[str]
    synthetic: bool
    insufficient: bool


_HEADER_META_KEYS = frozenset(
    {
        "config_hash",
        "git_commit",
        "mode",
        "driver",
        "window_from",
        "window_to",
        "data_source",
        "synthetic",
        "ai",
        "strategies",
        "data_from",
        "data_to",
        "data_hours",
        "generated_at",
        "insufficient_data",
    }
)


def _header(run: _Run) -> list[str]:
    meta, window = run.meta, run.window
    ai_flag = _ai_on(meta)
    rows = [
        ["Config hash", f"`{meta.get('config_hash') or 'unknown'}`"],
        ["Git commit", f"`{meta.get('git_commit') or 'unknown'}`"],
        ["Mode", str(meta.get("mode") or "n/a")],
        ["Driver", str(meta.get("driver") or "n/a")],
        [
            "Requested window (UTC)",
            _span_text(window["requested_from"], window["requested_to"], window["requested_hours"]),
        ],
        ["Data present (UTC)", _span_text(window["data_from"], window["data_to"], window["data_hours"])],
        ["Data source", str(meta.get("data_source") or "n/a") + (" (SYNTHETIC)" if run.synthetic else "")],
        ["AI", "on" if ai_flag else ("off" if ai_flag is False else str(meta.get("ai") or "n/a"))],
        ["Strategies", ", ".join(_strategies(meta)) or "n/a"],
        ["Report date", run.report_date or "n/a"],
    ]
    for key in sorted(meta, key=str):
        if key in _HEADER_META_KEYS or isinstance(meta[key], Mapping | list | tuple):
            continue
        rows.append([str(key), _fmt(_guess_kind(str(key), meta[key]), meta[key])])
    return [_md_table(["run", "value"], rows), ""]


def _trades_section(run: _Run) -> list[str]:
    trades = run.trades
    parts = ["## Trades", ""]
    if not trades:
        return parts + [f"_No trades in the window._ ([{TRADES_CSV}]({TRADES_CSV}) has the header only.)", ""]
    shown_cols = _display_columns(run.columns)
    rows = []
    for t in trades[:TRADES_TABLE_ROWS]:
        cells = []
        for c in shown_cols:
            text = _fmt(_guess_kind(c, t.get(c)), t.get(c)) if t.get(c) is not None else ""
            cells.append(text if len(text) <= 60 else text[:57] + "...")
        rows.append(cells)
    more = (
        f"first {TRADES_TABLE_ROWS} of {len(trades):,}"
        if len(trades) > TRADES_TABLE_ROWS
        else f"all {len(trades):,}"
    )
    parts += [
        f"Showing {more} trades; full list (all columns): [{TRADES_CSV}]({TRADES_CSV}).",
        "",
        _md_table(shown_cols, rows),
        "",
    ]
    reasons = run.metrics.get("exit_reasons")
    if not isinstance(reasons, Mapping) or not reasons:
        reasons = Counter(str(t.get("exit_reason") or "unknown") for t in trades if "exit_reason" in t)
    if reasons:
        listed = ", ".join(f"{k} {int(_num(v) or 0):,}" for k, v in _sorted_counts(dict(reasons)))
        parts += [f"Exit reasons: {listed}.", ""]
    return parts


def _ai_section(run: _Run) -> list[str]:
    ai, plans = run.ai, run.plans
    ai_flag = _ai_on(run.meta)
    parts = ["## AI (Regime Analyst)", ""]
    if ai_flag is False and not plans:
        parts += ["AI was **off** in this run (rules-only defaults); no plans were produced.", ""]
    rows = [
        ["Plans produced", _fmt("int", ai.get("n_plans"))],
        ["Average size multiplier", _fmt("num", ai.get("avg_size_multiplier"))],
        [
            "Plans that changed the enabled strategies",
            _fmt("int", ai.get("n_plan_changes_of_enabled_strategies")),
        ],
    ]
    if ai.get("plans_cached") is not None:
        rows.append(["Plans served from the LLM cache", _fmt("int", ai["plans_cached"])])
    if ai.get("plans_clamped") is not None:
        rows.append(["Plans with clamped fields", _fmt("int", ai["plans_clamped"])])
    if ai.get("s3_entries_gated") is not None:
        rows.append(["S3 entries gated by regime", _fmt("int", ai["s3_entries_gated"])])
    if ai.get("regime_flip_exits") is not None:
        rows.append(["Exits on a regime flip", _fmt("int", ai["regime_flip_exits"])])
    if ai.get("models"):
        rows.append(["Models", ", ".join(ai["models"])])
    parts += [_md_table(["AI metric", "value"], rows, right=[False, True]), ""]
    dist = _regime_table(ai.get("regime_distribution") or {})
    if dist:
        seen = {r for counts in dist.values() for r in counts}
        regimes = [r for r in _REGIME_COLORS if r in seen] + sorted(seen - set(_REGIME_COLORS))
        rows_d = []
        for sym in sorted(dist):
            counts = dist[sym]
            total = sum(counts.values())
            rows_d.append(
                [sym]
                + [
                    f"{counts.get(r, 0):,} ({_fmt('pct', counts.get(r, 0) / total if total else 0)})"
                    for r in regimes
                ]
                + [f"{total:,}"]
            )
        parts += [
            "Regime calls per symbol (count and share of that symbol's calls):",
            "",
            _md_table(["symbol", *regimes, "calls"], rows_d, right=[False] + [True] * (len(regimes) + 1)),
            "",
        ]
    if plans:
        parts += [f"![Regime timeline]({REGIMES_PNG})", ""]
    parts += ["### Ablation (ai=on vs ai=off)", ""]
    parts += [
        _ablation_table(run.ablation) if run.ablation else f"_Ablation {NOT_AVAILABLE} for this run._",
        "",
    ]
    return parts


def _fees_section(run: _Run) -> list[str]:
    fees = run.fees
    source = fees["source"]
    paid = _fmt("tmn", fees["fees_paid_tmn"]) + (
        " (no fee data recorded)"
        if source == "none"
        else (f" (from {source})" if source not in ("metrics", "given") else "")
    )
    rows = [
        ["Fees paid (TMN)", paid],
        ["Initial equity (TMN)", _fmt("tmn", fees["initial_equity_tmn"])],
        [
            f"Fee budget ({_fmt('pct', fees['fee_budget_pct'])} of initial equity over the 72-h challenge, TMN)",
            _fmt("tmn", fees["fee_budget_tmn"]),
        ],
        ["Budget used", _budget_text(fees["budget_used_pct"])],
    ]
    if fees["fees_per_72h_tmn"] is not None and (fees["data_hours"] or 0) >= 1:  # no extrapolating minutes
        rows += [
            [
                f"Fees per 72 h at this run's pace ({_fmt('num', fees['data_hours'])} h of data, TMN)",
                _fmt("tmn", fees["fees_per_72h_tmn"]),
            ],
            ["Budget used per 72 h at this pace", _budget_text(fees["budget_used_per_72h_pct"])],
        ]
    return ["## Fees vs budget", "", _md_table(["fees", "value"], rows, right=[False, True]), ""]


def _budget_text(used: float | None) -> str:
    text = _fmt("pct", used)
    return f"**{text} (over budget)**" if used is not None and used > 1 else text


def _render_markdown(run: _Run) -> str:
    metrics, points, vetoes, decisions = run.metrics, run.points, run.vetoes, run.decisions
    parts: list[str] = [f"# {run.title}", ""]
    if run.synthetic:
        parts += [
            f"> **{SYNTHETIC_BANNER}.** This run used generated market data (offline SyntheticFeed), not "
            "data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.",
            "",
        ]
    if run.insufficient:
        parts += [
            f"> **{INSUFFICIENT_BANNER}.** The store held too little data for the requested window "
            f"({len(points)} equity point(s)); metrics below are empty or undefined. See *Limitations*.",
            "",
        ]
    parts += _header(run)

    # metrics
    parts += ["## Metrics", ""]
    rows = _metric_rows(metrics)
    if rows:
        parts += [_md_table(["metric", "value"], rows, right=[False, True]), ""]
        parts += [
            f"_Percentages are shown ×100; [{SUMMARY_JSON}]({SUMMARY_JSON}) has every metric, including the "
            "nested breakdowns, with percentages as fractions (0.012 = 1.2%)._",
            "",
        ]
    else:
        parts += ["_No metrics computed (insufficient data)._", ""]
    notes = _metric_notes(metrics)
    if notes:
        parts += ["Notes on the metrics:", ""] + [f"- {n}" for n in notes] + [""]
    end_state = _end_state(metrics)
    if end_state:
        parts += [
            "### End state: realized vs unrealized",
            "",
            _md_table(["end of window", "value"], end_state, right=[False, True]),
            "",
        ]

    # equity + drawdown
    parts += [
        "## Equity curve (TMN)",
        "",
        f"![Equity curve]({EQUITY_PNG})",
        "",
        f"Data: [{EQUITY_CSV}]({EQUITY_CSV}) ({len(points):,} points).",
        "",
        "## Drawdown",
        "",
        f"![Drawdown]({DRAWDOWN_PNG})",
        "",
        "_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._",
        "",
    ]

    parts += _trades_section(run)

    # per strategy / per day
    parts += ["## Per strategy", ""]
    per_strategy = _breakdown_table(metrics.get("per_strategy"), "strategy", _PER_STRATEGY_SPECS)
    parts += [per_strategy or _per_strategy_from_trades(run.trades) or "_No per-strategy data._", ""]
    per_day = _breakdown_table(metrics.get("per_day"), "date (UTC)", _PER_DAY_SPECS) or _breakdown_table(
        metrics.get("daily_returns"), "date (UTC)", value_kind="pct"
    )
    if per_day:
        parts += ["## Per day", "", per_day, ""]

    # vetoes
    parts += ["## Risk vetoes", "", f"![Veto histogram]({VETOES_PNG})", ""]
    if vetoes:
        total = sum(vetoes.values())
        parts += [
            _md_table(
                ["risk code", "vetoes", "share"],
                [[k, f"{v:,}", _fmt("pct", v / total if total else 0)] for k, v in _sorted_counts(vetoes)],
                right=[False, True, True],
            ),
            "",
        ]
    else:
        parts += ["_No vetoes recorded._", ""]
    if decisions:
        kinds = Counter(str(d.get("kind")) for d in decisions)
        link = " ([decisions.jsonl](decisions.jsonl))" if (run.out / "decisions.jsonl").exists() else ""
        parts += [
            f"Decision records{link}: " + ", ".join(f"{k} {v:,}" for k, v in sorted(kinds.items())) + ".",
            "",
        ]

    parts += _ai_section(run)
    parts += _fees_section(run)

    # limitations
    parts += ["## Limitations", ""]
    if run.limitations:
        parts += ["Read these before reading any number above:", ""]
        parts += [f"- {item}" for item in run.limitations]
    else:
        parts += ["_None recorded._"]
    files = ", ".join(f"[{f}]({f})" for f in (SUMMARY_JSON, TRADES_CSV, EQUITY_CSV))
    parts += ["", "---", "", f"_Generated by `backtest/report.py`. Files: {files}._", ""]
    return "\n".join(parts)


# ================================================================= index


@dataclass(frozen=True)
class ReportInfo:
    path: Path  # the report's markdown file
    name: str  # folder name (with trailing /) or file name, relative to reports_dir
    title: str
    date: str | None
    synthetic: bool | None


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if m:
            return m.group(1).strip()
    return None


def _date_from(text: str) -> str | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _report_info(md_path: Path, name: str) -> ReportInfo:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    summary: dict[str, Any] = {}
    summary_path = md_path.parent / SUMMARY_JSON
    if md_path.name == REPORT_MD and summary_path.exists():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            summary = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            summary = {}
    title = str(summary.get("title") or _first_heading(text) or name)
    when = summary.get("report_date") or _date_from(name) or _date_from("\n".join(text.splitlines()[:30]))
    synthetic = (
        summary.get("synthetic")
        if isinstance(summary.get("synthetic"), bool)
        else (SYNTHETIC_BANNER in text or None)
    )
    return ReportInfo(
        path=md_path, name=name, title=title, date=str(when) if when else None, synthetic=synthetic
    )


def _collect_reports(reports_dir: Path) -> list[ReportInfo]:
    found: list[ReportInfo] = []
    if not reports_dir.is_dir():
        return found
    for child in sorted(reports_dir.iterdir(), key=lambda p: p.name):
        if child.name.startswith((".", "_")):
            continue
        if child.is_file() and child.suffix.lower() == ".md" and child.name != INDEX_MD:
            found.append(_report_info(child, child.name))
        elif child.is_dir():
            md = child / REPORT_MD
            if not md.exists():
                mds = sorted(p for p in child.glob("*.md") if p.name != INDEX_MD)
                md = mds[0] if mds else md
            if md.exists():
                found.append(_report_info(md, child.name + "/"))
    return found


def update_index(reports_dir: Path | str) -> Path:
    """Regenerate ``reports_dir/README.md`` listing every report folder and standalone ``.md`` file.

    Newest first (by report date, then name); undated entries last. Deterministic: it reads only the
    reports themselves (title from ``summary.json`` or the first ``# `` heading; date from
    ``summary.json``, the name, or the first date in the file's first 30 lines), never mtimes.
    """
    root = Path(reports_dir)
    root.mkdir(parents=True, exist_ok=True)
    infos = _collect_reports(root)
    infos.sort(key=lambda i: i.name)
    infos.sort(key=lambda i: i.date or "", reverse=True)  # stable: equal dates keep name order
    infos.sort(key=lambda i: i.date is None)
    lines = [
        "# Reports index",
        "",
        "Regenerated by `backtest/report.py` (`update_index`) — do not edit by hand. Rows marked "
        "*synthetic* were produced on generated data and are **not evidence of performance**.",
        "",
    ]
    if infos:
        rows = []
        for info in infos:
            rel = info.path.relative_to(root).as_posix()
            data = "synthetic" if info.synthetic else "—"
            rows.append([f"[{info.title}]({rel})", info.date or "n/a", data, f"`{info.name}`"])
        lines += [_md_table(["report", "date", "data", "path"], rows), ""]
    else:
        lines += ["_No reports yet._", ""]
    out = root / INDEX_MD
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ================================================================= final report


@dataclass(frozen=True)
class FinalSection:
    """One section of ``deliverables/REPORT.md``; ``patterns`` are globs relative to ``reports_dir``."""

    key: str
    title: str
    patterns: tuple[str, ...]


DEFAULT_FINAL_SECTIONS: tuple[FinalSection, ...] = (
    FinalSection(
        "survey",
        "Market survey (spreads, S1 opportunities, dollar regime)",
        ("survey_*.md", "survey_*/report.md"),
    ),
    FinalSection("bt_candles", "Backtest: 1-min candles, 30 days", ("bt_candles*/report.md",)),
    FinalSection("bt_books", "Backtest: recorded order-book window", ("bt_books*/report.md",)),
    FinalSection(
        "ablation",
        "AI ablation (ai=on vs ai=off)",
        ("ablation*/report.md", "ablation*.md", "bt_*ablation*/report.md"),
    ),
    FinalSection("shadow", "Shadow (paper) run", ("shadow_*/report.md", "shadow_*.md")),
    FinalSection("live", "Live challenge run", ("live_*/report.md", "live_*.md")),
    FinalSection("replay", "Replay proof", ("replay_*/report.md", "replay_*.md")),
)
_DEFAULT_BY_KEY = {s.key: s for s in DEFAULT_FINAL_SECTIONS}


def _resolve_sections(
    sections: Sequence[str | FinalSection | tuple[str, str | Sequence[str]]] | None,
) -> list[FinalSection]:
    if sections is None:
        return list(DEFAULT_FINAL_SECTIONS)
    out: list[FinalSection] = []
    for item in sections:
        if isinstance(item, FinalSection):
            out.append(item)
        elif isinstance(item, str):
            if item not in _DEFAULT_BY_KEY:
                raise ValueError(f"unknown section {item!r}; known: {', '.join(_DEFAULT_BY_KEY)}")
            out.append(_DEFAULT_BY_KEY[item])
        else:
            title, patterns = item
            pats = (patterns,) if isinstance(patterns, str) else tuple(patterns)
            key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or f"section{len(out) + 1}"
            out.append(FinalSection(key, title, pats))
    return out


def _assign(reports_dir: Path, sections: Sequence[FinalSection]) -> dict[str, list[Path]]:
    """Files per section; a file matched by several sections goes to the most specific pattern
    (most literal characters), ties to the earlier section."""
    best: dict[Path, tuple[int, int, str]] = {}  # path -> (specificity, -section order, key)
    for order, sec in enumerate(sections):
        for pattern in sec.patterns:
            score = len(pattern.replace("*", "").replace("?", ""))
            for path in sorted(reports_dir.glob(pattern)):
                if not path.is_file() or path.name == INDEX_MD:
                    continue
                cand = (score, -order, sec.key)
                if path not in best or cand > best[path]:
                    best[path] = cand
    out: dict[str, list[Path]] = {s.key: [] for s in sections}
    for path, (_, _, key) in sorted(best.items()):
        out[key].append(path)
    return out


def _rewrite_link(target: str, src_dir: Path, out_dir: Path) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith(("#", "/")):
        return target
    path_part, sep, anchor = target.partition("#")
    if not path_part:
        return target
    return _rel(src_dir / path_part, out_dir) + (sep + anchor if sep else "")


def _embed(md_path: Path, out_dir: Path, demote: int = 1) -> str:
    """The report's markdown minus its H1, headings demoted, relative links rebased onto ``out_dir``."""
    lines_out: list[str] = []
    in_fence = False
    dropped_h1 = False
    for line in md_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            lines_out.append(line)
            continue
        if in_fence:
            lines_out.append(line)
            continue
        m = _HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 1 and not dropped_h1:
                dropped_h1 = True
                continue
            line = "#" * min(6, len(m.group(1)) + demote) + m.group(2)
        line = _LINK_RE.sub(
            lambda mm: mm.group(1) + _rewrite_link(mm.group(2), md_path.parent, out_dir) + mm.group(3), line
        )
        lines_out.append(line)
    while lines_out and not lines_out[0].strip():
        lines_out.pop(0)
    return "\n".join(lines_out).rstrip()


def assemble_final_report(
    reports_dir: Path | str,
    out_path: Path | str = DEFAULT_FINAL_REPORT,
    sections: Sequence[str | FinalSection | tuple[str, str | Sequence[str]]] | None = None,
    *,
    title: str = "Wallex Trader Agent — final report",
) -> Path:
    """Concatenate the latest report of each section into ``out_path`` (default ``deliverables/REPORT.md``).

    ``sections`` defaults to survey -> bt_candles -> bt_books -> ablation -> shadow -> live -> replay;
    items may be those keys, ``FinalSection`` objects or ``(title, glob | [globs])`` tuples. The latest
    report per section wins (report date, then name). Links and images are rewritten relative to the
    output file, so PNGs keep working; no absolute path is written. Missing sections are listed as
    "not available yet".
    """
    root = Path(reports_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    secs = _resolve_sections(sections)
    assigned = _assign(root, secs) if root.is_dir() else {s.key: [] for s in secs}
    chosen: dict[str, ReportInfo | None] = {}
    for sec in secs:
        infos = []
        for path in assigned[sec.key]:
            rel = path.relative_to(root)
            name = rel.parts[0] + "/" if len(rel.parts) > 1 else rel.name
            infos.append(_report_info(path, name))
        infos.sort(key=lambda i: (i.date or "", i.path.relative_to(root).as_posix()))
        chosen[sec.key] = infos[-1] if infos else None
    root_rel = _rel(root, out.parent)

    def src_link(info: ReportInfo) -> str:
        return f"[{info.path.relative_to(root).as_posix()}]({_rel(info.path, out.parent)})"

    missing = [s.title for s in secs if chosen[s.key] is None]
    any_synthetic = any(i is not None and i.synthetic for i in chosen.values())
    parts: list[str] = [
        f"# {title}",
        "",
        f"Assembled by `backtest/report.py` (`assemble_final_report`) from `{root_rel}`; edit the source "
        "reports, not this file. Order: survey → backtests (candles 30 d, books window, ablation) → shadow → "
        "live → replay proof.",
        "",
    ]
    if any_synthetic:
        parts += [
            f"> **Some sections below are built on {SYNTHETIC_BANNER}.** They are marked in the table "
            "and carry their own banner.",
            "",
        ]
    toc = []
    for n, sec in enumerate(secs, 1):
        info = chosen[sec.key]
        if info is None:
            toc.append([str(n), sec.title, NOT_AVAILABLE, "—"])
        else:
            status = (
                "included"
                + (f" ({info.date})" if info.date else "")
                + (" — SYNTHETIC" if info.synthetic else "")
            )
            toc.append([str(n), sec.title, status, src_link(info)])
    parts += [_md_table(["#", "section", "status", "source"], toc), ""]
    if missing:
        parts += [f"**{NOT_AVAILABLE.capitalize()}:** " + "; ".join(missing) + ".", ""]
    for n, sec in enumerate(secs, 1):
        info = chosen[sec.key]
        parts += ["---", "", f"## {n}. {sec.title}", ""]
        if info is None:
            pats = ", ".join(f"`{p}`" for p in sec.patterns)
            parts += [f"_{NOT_AVAILABLE.capitalize()}: no report matching {pats} in `{root_rel}`._", ""]
            continue
        parts += [
            f"_Source: {src_link(info)} — {info.title}"
            + (f", {info.date}" if info.date and info.date not in info.title else "")
            + "._",
            "",
        ]
        body = _embed(info.path, out.parent)
        if body:
            parts += [body, ""]
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


__all__ = [
    "DEFAULT_FINAL_REPORT",
    "DEFAULT_FINAL_SECTIONS",
    "DRAWDOWN_PNG",
    "EQUITY_CSV",
    "EQUITY_CSV_COLUMNS",
    "EQUITY_PNG",
    "INDEX_MD",
    "INSUFFICIENT_BANNER",
    "NOT_AVAILABLE",
    "REGIMES_PNG",
    "REPORT_FILES",
    "REPORT_MD",
    "SUMMARY_JSON",
    "SYNTHETIC_BANNER",
    "TRADES_CSV",
    "VETOES_PNG",
    "FinalSection",
    "ReportInfo",
    "assemble_final_report",
    "update_index",
    "write_report",
]
