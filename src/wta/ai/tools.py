"""Read-only tools exposed to the Regime Analyst (PRD-04 §4–5).

Internal tools read the engine's own state; ``*_local`` tools mirror the Wallex MCP market-data
tools using our bus/store so the slow loop never depends on MCP uptime (the plan records
``tools_source``). Every tool is read-only, returns JSON-safe dicts, and its output is truncated to
``ai.tool_result_max_bytes`` before it reaches the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from wta.config import Config, canonical_json
from wta.features.engine import FeatureSnapshot, atr, ema, resample
from wta.models import BookSnapshot, Candle

Json = dict[str, Any]


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


# JSON schemas in the OpenAI "function" shape ({name, description, parameters}).
INTERNAL_TOOL_SCHEMAS: list[Json] = [
    {"name": "get_features", "description": "Current computed features (book, returns, ATR, EMA, cross-book edges and costs).",
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
        snap = self.d.features()
        if snap is None:
            return {"error": "features not available yet"}
        return snap.compact(symbols=symbols)

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
