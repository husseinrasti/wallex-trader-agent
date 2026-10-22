"""Read-only tools exposed to the Regime Analyst (PRD-04 §4–5).

Internal tools read the engine's own state; ``*_local`` tools mirror the Wallex MCP market-data
tools using our bus/store so the slow loop never depends on MCP uptime (the plan records
``tools_source``). Every tool is read-only, returns JSON-safe dicts, and its output is truncated to
``ai.tool_result_max_bytes`` before it reaches the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from wta.ai.plan_schema import REQUIRED_SYMBOLS
from wta.config import Config, canonical_json
from wta.features.engine import FeatureSnapshot, atr, ema, resample
from wta.models import BookSnapshot, Candle
from wta.utils.timeutil import iso

Json = dict[str, Any]
DOLLAR_SYMBOL = "USDTTMN"


def to_jsonable(obj: Any) -> Any:
    return json.loads(canonical_json(obj))


def truncate(obj: Any, max_bytes: int = 4096) -> Any:
    """Shrink a tool result to <= max_bytes of JSON: trim long lists first, then hard-cut a string."""
    data = to_jsonable(obj)
    if len(canonical_json(data)) <= max_bytes:
        return data

    def trim(node: Any, keep: int) -> Any:
        if isinstance(node, list):
            return [trim(x, keep) for x in node[-keep:]]
        if isinstance(node, dict):
            return {k: trim(v, keep) for k, v in node.items()}
        return node

    for keep in (50, 20, 10, 5, 2):
        cand = trim(data, keep)
        if len(canonical_json(cand)) <= max_bytes:
            return cand
    # The prefix is re-encoded as a JSON string (quotes/backslashes escaped, multi-byte UTF-8), so
    # shrink it until the wrapper itself fits; each dropped char frees >= 1 byte, so this converges.
    text = canonical_json(data).decode()
    cut = max(0, max_bytes - 40)
    while True:
        wrapper = {"truncated": True, "json_prefix": text[:cut]}
        excess = len(canonical_json(wrapper)) - max_bytes
        if excess <= 0 or cut == 0:
            return wrapper
        cut = max(0, cut - excess)


# ---------------------------------------------------------------- curated feature view (get_features)

FEATURE_VIEW = "features"  # value of the view's "view" key (how the analyst and the offline provider spot it)
# per symbol: short key -> FeatureSnapshot field; None values are dropped from every row
SYMBOL_KEYS: dict[str, str] = {
    "mid": "mid",
    "spread": "spread_pct",
    "ret_15m": "ret_15m",
    "ret_1h": "ret_1h",
    "ret_24h": "ret_24h",
    "atr": "atr_15m_pct",
    "atr_med": "atr_15m_pct_median_24h",
    "ema_gap": "ema_gap_pct",
    "trend": "trend_flag",
    "vol_x": "vol_ratio_15m",
    "age_s": "book_age_s",
}
BRIEF_KEYS: tuple[str, ...] = ("mid", "spread", "ret_1h", "ret_24h")
# per S1 coin (its TMN book vs its USDT book x USDTTMN): short key -> cross-feature field; the two
# lock-in edges are shown net of their fees (edge - cost), the fees once under "lockin_cost"
CROSS_KEYS: dict[str, str] = {"dev": "dev_mid_pct", "z": "dev_zscore"}
CROSS_NET: dict[str, tuple[str, str]] = {
    "net_tmn_cheap": ("edge_tmn_cheap_pct", "cost_lockin_tmn_cheap"),
    "net_usdt_cheap": ("edge_usdt_cheap_pct", "cost_lockin_usdt_cheap"),
}
FEATURE_LEGEND: dict[str, str] = {
    "mid": "mid price in the quote currency",
    "spread": "(ask - bid) / mid",
    "ret_15m": "return over 15 min",
    "ret_1h": "return over 1 h",
    "ret_24h": "return over 24 h",
    "atr": "ATR14 of 15-min bars / price",
    "atr_med": "24 h median of atr",
    "ema_gap": "EMA20 / EMA60 - 1 on 15-min closes",
    "trend": "+1 up, -1 down, 0 flat (EMA gap beyond the S2 threshold)",
    "vol_x": "last 15-min volume / mean of the 20 bars before",
    "age_s": "seconds since the book was updated",
    "dev": "coin TMN mid / USDTTMN mid / coin USDT mid - 1",
    "z": "z-score of dev over the last hour",
    "net_tmn_cheap": "lock-in edge buying the TMN book and selling the USDT book, after the fees of both legs "
                     "(S1 trades when it exceeds s1_margin_min_pct)",
    "net_usdt_cheap": "the same lock-in the other way round, after fees",
    "lockin_cost": "the fees of both lock-in legs (tmn_cheap / usdt_cheap), per coin when they differ",
}
FEATURE_UNITS = "fractions (0.01 = 1%) except mid, age_s (seconds), z, trend and vol_x"
FEATURE_DIGITS: tuple[int, ...] = (6, 5, 4)  # significant digits; fewer only when the view would not fit


def round_sig(value: Any, digits: int = 6) -> int | float | None:
    """A number rounded to ``digits`` significant digits as a plain JSON number (int when integral
    and >= 1, else float); None for None, booleans, text that is no number and non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    if d == 0:
        return 0
    q = Decimal(format(d, f".{digits}g"))
    if abs(q) >= 1 and q == q.to_integral_value():
        return int(q)
    return float(q)


def _coin_books(coin: str) -> tuple[str, str]:
    return f"{coin}TMN", f"{coin}USDT"


def _row(src: Mapping[str, Any], keys: Mapping[str, str] | Iterable[str], digits: int) -> dict[str, Any]:
    """``{short key: rounded value}`` without the None values (``keys``: short -> source, or short keys
    of ``SYMBOL_KEYS``)."""
    pairs = keys.items() if isinstance(keys, Mapping) else ((k, SYMBOL_KEYS[k]) for k in keys)
    return {k: v for k, field_name in pairs if (v := round_sig(src.get(field_name), digits)) is not None}


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _cross_row(src: Mapping[str, Any], digits: int) -> dict[str, Any]:
    row = _row(src, CROSS_KEYS, digits)
    for key, (edge_field, cost_field) in CROSS_NET.items():
        edge, cost = _dec_or_none(src.get(edge_field)), _dec_or_none(src.get(cost_field))
        if edge is not None and cost is not None:
            row[key] = round_sig(edge - cost, digits)
    return row


def _lockin_costs(snap: FeatureSnapshot, coins: list[str], digits: int) -> dict[str, Any]:
    """``{"tmn_cheap": fee, "usdt_cheap": fee}`` when every listed coin has the same lock-in fees
    (the usual case: one fee table per quote currency), else the pair per coin."""
    per: dict[str, dict[str, Any]] = {}
    for coin in coins:
        src = snap.cross.get(coin) or {}
        pair = {side: v for side, (_edge, cost_field) in (("tmn_cheap", CROSS_NET["net_tmn_cheap"]),
                                                          ("usdt_cheap", CROSS_NET["net_usdt_cheap"]))
                if (v := round_sig(src.get(cost_field), digits)) is not None}
        if pair:
            per[coin] = pair
    distinct = {canonical_json(p) for p in per.values()}
    if len(distinct) == 1:
        return next(iter(per.values()))
    return per


def feature_view(
    snap: FeatureSnapshot,
    symbols: Iterable[str] | None = None,
    max_bytes: int = 4096,
    *,
    full_symbols: Iterable[str] = REQUIRED_SYMBOLS,
    legend: bool = False,
) -> dict[str, Any]:
    """The curated, size-capped view of a ``FeatureSnapshot`` the analyst sees (``get_features``,
    and the features block of its first message with ``legend=True``)::

        {"view": "features", "ts": "<UTC ISO>", "units": "...",
         "symbols": {"BTCUSDT": {"mid": 82877.1, "spread": 0.000774, "ret_1h": 0.00123, ...}},
         "cross": {"BTC": {"dev": -0.00123, "z": 1.2, "net_tmn_cheap": -0.0051, "net_usdt_cheap": -0.0035}},
         "lockin_cost": {"tmn_cheap": 0.0043, "usdt_cheap": 0.0043},
         "brief_rows": [...], "no_data": [...], "unknown_symbols": [...], "omitted_symbols": [...],
         "significant_digits": 5, "keys": {short key: meaning}}

    Key fields only, under short names (``SYMBOL_KEYS``, ``CROSS_KEYS``; the meanings are in the
    ``get_features`` description and, with ``legend``, under ``keys``), JSON numbers rounded to 6
    significant digits, None values dropped (a missing key = not available), canonical JSON within
    ``max_bytes``. Without ``symbols`` the regime symbols (``full_symbols``) get every key and the
    other books the brief set ``BRIEF_KEYS`` (named in ``brief_rows``): the 21-symbol watchlist
    with every key would not fit the 4 kB tool cap. ``symbols`` filters the rows (case-insensitive;
    USDTTMN, the dollar every TMN valuation uses, is always included) and the S1 coins (a coin is
    kept when it or one of its books is asked for); asked rows get every key. Only when that does
    not fit: brief rows for the non-regime symbols, then fewer digits (``significant_digits``), then
    rows dropped from the end (``omitted_symbols``) — never a cut JSON. The S1 lock-in edges are
    given net of their fees (``net_*`` = edge - cost); the fees themselves once, under
    ``lockin_cost``.
    """
    full = {str(s).upper() for s in full_symbols}
    wanted: set[str] | None = None if symbols is None else {str(s).strip().upper() for s in symbols}
    known = set(snap.symbols)
    if wanted is None:
        rows_for = sorted(known)
        unknown: list[str] = []
    else:
        rows_for = sorted((wanted | {DOLLAR_SYMBOL}) & known)
        unknown = sorted(s for s in wanted - known if s not in snap.cross)
    coins = sorted(
        c for c in snap.cross
        if wanted is None or c in wanted or any(b in wanted for b in _coin_books(c))
    )

    def build(digits: int, brief_all: bool = False, drop: int = 0) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {}
        brief: list[str] = []
        no_data: list[str] = []
        for sym in rows_for:
            src = snap.symbols.get(sym) or {}
            short = sym not in full and (brief_all or wanted is None)
            row = _row(src, BRIEF_KEYS if short else SYMBOL_KEYS, digits)
            if not row:
                no_data.append(sym)
                continue
            rows[sym] = row
            if short:
                brief.append(sym)
        omitted: list[str] = []
        if drop:
            droppable = [s for s in sorted(rows) if s not in full]
            omitted = droppable[max(0, len(droppable) - drop):]
            rows = {s: r for s, r in rows.items() if s not in omitted}
            brief = [s for s in brief if s not in omitted]
        cross = {c: r for c in coins if (r := _cross_row(snap.cross.get(c) or {}, digits))}
        out: dict[str, Any] = {
            "view": FEATURE_VIEW,
            "ts": iso(snap.ts),
            "units": FEATURE_UNITS,
            "symbols": rows,
            "cross": cross,
        }
        costs = _lockin_costs(snap, coins, digits)
        if costs:
            out["lockin_cost"] = costs
        if brief:
            out["brief_rows"] = brief
        for key, items in (("no_data", no_data), ("unknown_symbols", unknown), ("omitted_symbols", omitted)):
            if items:
                out[key] = items
        if digits != FEATURE_DIGITS[0]:
            out["significant_digits"] = digits
        if legend:
            out["keys"] = dict(FEATURE_LEGEND)
        return out

    def fits(view: dict[str, Any]) -> bool:
        return len(canonical_json(view)) <= max_bytes

    view = build(FEATURE_DIGITS[0])
    if fits(view):
        return view
    for digits in FEATURE_DIGITS:  # many asked rows: the brief key set first, then fewer digits
        view = build(digits, brief_all=True)
        if fits(view):
            return view
    last = FEATURE_DIGITS[-1]
    droppable = sum(1 for s in view["symbols"] if s not in full)
    for drop in range(1, droppable + 1):
        view = build(last, brief_all=True, drop=drop)
        if fits(view):
            return view
    return view  # still too big (an absurd cap): InternalTools.call truncates it


def is_feature_view(obj: Any) -> bool:
    """True for a ``feature_view`` dict."""
    return isinstance(obj, Mapping) and obj.get("view") == FEATURE_VIEW and isinstance(obj.get("symbols"), Mapping)


def feature_rows(view: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``feature_view`` rows under the FeatureSnapshot field names (``ret_24h``, ``ema_gap_pct``,
    ``trend_flag`` ...), for code that reasons on features (the offline provider, triggers)."""
    out: dict[str, dict[str, Any]] = {}
    for sym, row in (view.get("symbols") or {}).items():
        if isinstance(row, Mapping):
            out[str(sym)] = {SYMBOL_KEYS[k]: v for k, v in row.items() if k in SYMBOL_KEYS and v is not None}
    return out


GET_FEATURES_DESCRIPTION = (
    "Current computed features, rounded to 6 significant digits; a missing key = not available. "
    f"Units: {FEATURE_UNITS}. Per symbol: "
    + "; ".join(f"{k} = {FEATURE_LEGEND[k]}" for k in SYMBOL_KEYS)
    + ". Without 'symbols' only USDTTMN, BTCUSDT, ETHUSDT and SOLUSDT carry every key; the other books "
    "(listed in brief_rows) carry " + ", ".join(BRIEF_KEYS) + ". Per S1 coin under 'cross': "
    + "; ".join(f"{k} = {FEATURE_LEGEND[k]}" for k in (*CROSS_KEYS, *CROSS_NET, "lockin_cost"))
    + ". 'symbols' (optional) selects rows with every key (USDTTMN is always included) and the coins of "
    "those books."
)


# JSON schemas in the OpenAI "function" shape ({name, description, parameters}).
INTERNAL_TOOL_SCHEMAS: list[Json] = [
    {"name": "get_features", "description": GET_FEATURES_DESCRIPTION,
     "parameters": {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}},
                    "additionalProperties": False}},
    {"name": "get_pnl_summary", "description": "Equity in TMN, drawdown, daily PnL, realized PnL by strategy, fees vs budget, open positions.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_recent_decisions", "description": "Last n compact decision records (reason, risk result, outcome).",
     "parameters": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 50}},
                    "additionalProperties": False}},
    {"name": "get_health", "description": "Data freshness, API errors, paused/killed flags, flatten phase, time to deadline.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_candle_stats", "description": "Computed stats for a symbol: returns, ATR%, EMA20/60, volume ratio.",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                     "resolution": {"type": "string", "enum": ["1", "15", "60"]},
                                                     "n": {"type": "integer", "minimum": 5, "maximum": 200}},
                    "required": ["symbol"], "additionalProperties": False}},
]

LOCAL_TOOL_SCHEMAS: list[Json] = [
    {"name": "wallex_basic_get_market_depth_local", "description": "Order book (top 10) for a symbol from our feed.",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
                    "additionalProperties": False}},
    {"name": "wallex_basic_candle_history_local", "description": "Last n OHLCV candles from our store.",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                     "resolution": {"type": "string", "enum": ["1", "15", "60"]},
                                                     "n": {"type": "integer", "minimum": 1, "maximum": 50}},
                    "required": ["symbol"], "additionalProperties": False}},
    {"name": "wallex_basic_get_latest_trades_local", "description": "Recent trades for a symbol from our store.",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
                    "additionalProperties": False}},
]


@dataclass
class ToolDeps:
    cfg: Config
    now: Callable[[], float]
    features: Callable[[], FeatureSnapshot | None]
    books: Callable[[], dict[str, BookSnapshot]]
    candles_1m: Callable[[str, float, float], list[Candle]]  # (symbol, from_ts, to_ts)
    trades: Callable[[str, float, float], list[Any]]
    pnl_summary: Callable[[], Json]
    recent_decisions: Callable[[int], list[Json]]
    health: Callable[[], Json]


class InternalTools:
    """Dispatches tool calls by name. Unknown names return an error dict (never raise into the loop)."""

    def __init__(self, deps: ToolDeps) -> None:
        self.d = deps
        self.max_bytes = deps.cfg.ai.tool_result_max_bytes

    @property
    def schemas(self) -> list[Json]:
        return INTERNAL_TOOL_SCHEMAS + LOCAL_TOOL_SCHEMAS

    @property
    def names(self) -> set[str]:
        return {s["name"] for s in self.schemas}

    def call(self, name: str, args: Json | None = None) -> Any:
        args = args or {}
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in self.names:
            return {"error": f"unknown tool {name}"}
        try:
            return truncate(fn(**args), self.max_bytes)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - tools must never crash the analyst loop
            return {"error": f"{name} failed: {type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------ internal

    def t_get_features(self, symbols: list[str] | None = None) -> Any:
        """The curated ``feature_view`` (always within ``ai.tool_result_max_bytes``: the full
        ``FeatureSnapshot.compact()`` of the watchlist is several times the cap)."""
        snap = self.d.features()
        if snap is None:
            return {"error": "features not available yet"}
        if symbols is not None and not isinstance(symbols, list):
            symbols = [str(symbols)]
        return feature_view(snap, symbols, max_bytes=self.max_bytes)

    def t_get_pnl_summary(self) -> Any:
        return self.d.pnl_summary()

    def t_get_recent_decisions(self, n: int = 20) -> Any:
        return {"decisions": self.d.recent_decisions(max(1, min(int(n), 50)))}

    def t_get_health(self) -> Any:
        return self.d.health()

    def t_get_candle_stats(self, symbol: str, resolution: str = "15", n: int = 60) -> Any:
        now = self.d.now()
        one = self.d.candles_1m(symbol, now - 26 * 3600, now)
        if not one:
            return {"symbol": symbol, "error": "no candles"}
        minutes = int(resolution)
        bars = one if minutes == 1 else resample(one, minutes, now=now)
        bars = bars[-max(5, min(int(n), 200)):]
        closes = [b.c for b in bars]

        def r(k: int) -> str | None:
            return str(closes[-1] / closes[-1 - k] - 1) if len(closes) > k and closes[-1 - k] else None

        a = atr(bars, 14)
        vols = [b.v for b in bars]
        vr = (vols[-1] / (sum(vols[-21:-1], Decimal(0)) / 20)) if len(vols) > 21 and sum(vols[-21:-1]) > 0 else None
        e20, e60 = ema(closes, 20), ema(closes, 60)
        return {
            "symbol": symbol, "resolution": resolution, "bars": len(bars), "last_close": str(closes[-1]),
            "ret_1_bar": r(1), "ret_4_bars": r(4), "ret_all": str(closes[-1] / closes[0] - 1) if closes[0] else None,
            "atr14_pct": str(a / closes[-1]) if a is not None and closes[-1] else None,
            "ema20": str(e20) if e20 is not None else None, "ema60": str(e60) if e60 is not None else None,
            "ema_gap_pct": str((e20 - e60) / e60) if e20 is not None and e60 else None,
            "volume_ratio_last": str(vr) if vr is not None else None,
            "high": str(max(b.h for b in bars)), "low": str(min(b.l for b in bars)),
        }

    # ------------------------------------------------------------ local mirrors of Wallex MCP tools

    def t_wallex_basic_get_market_depth_local(self, symbol: str) -> Any:
        b = self.d.books().get(symbol)
        if b is None:
            return {"symbol": symbol, "error": "no book"}
        return {"symbol": symbol, "ts": b.ts, "source": b.source,
                "bid": [[str(lv.price), str(lv.qty)] for lv in b.bids[:10]],
                "ask": [[str(lv.price), str(lv.qty)] for lv in b.asks[:10]]}

    def t_wallex_basic_candle_history_local(self, symbol: str, resolution: str = "15", n: int = 20) -> Any:
        now = self.d.now()
        one = self.d.candles_1m(symbol, now - 26 * 3600, now)
        minutes = int(resolution)
        bars = one if minutes == 1 else resample(one, minutes, now=now)
        bars = bars[-max(1, min(int(n), 50)):]
        return {"symbol": symbol, "resolution": resolution,
                "t": [b.ts for b in bars], "o": [str(b.o) for b in bars], "h": [str(b.h) for b in bars],
                "l": [str(b.l) for b in bars], "c": [str(b.c) for b in bars], "v": [str(b.v) for b in bars]}

    def t_wallex_basic_get_latest_trades_local(self, symbol: str) -> Any:
        now = self.d.now()
        rows = self.d.trades(symbol, now - 900, now)[-30:]
        return {"symbol": symbol, "trades": [to_jsonable(t) for t in rows]}
