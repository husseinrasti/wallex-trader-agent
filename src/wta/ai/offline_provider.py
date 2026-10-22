"""Deterministic OFFLINE RULES PROVIDER — this is NOT an LLM.

When no OpenRouter/Gemini key is configured (and ``ai.enabled`` is true) the engine can still run the
whole AI plumbing — prompt -> tool calls -> JSON -> validation -> clamp -> cache -> replay — with this
stand-in. It implements the ``Provider`` protocol of ``llm_client.py`` but decides with fixed rules:

* **Analyst prompt, tools offered, no tool results yet** -> asks for ``get_features`` (the four
  regime symbols) and ``get_pnl_summary``.
* **Tool results present, some regime symbol still without features** (``get_features`` output is
  often cut by the 4 kB tool cap; the complete flat objects are salvaged from the cut JSON) and
  ``get_candle_stats`` offered and not asked yet -> asks for ``get_candle_stats`` (15-min bars,
  n=97 so the window return spans exactly 24 h) for the missing symbols.
* **Otherwise** -> returns a MarketPlan-shaped JSON (the analyst's PlanOutput keys, nothing else):
  each symbol's regime is S2's own ``rule_regime`` on that symbol's features (trend_flag, ret_24h,
  ATR vs its 24 h median) with a confidence from the EMA-gap magnitude; ``enabled_strategies``
  = S1, S2 (those enabled in config); ``size_multiplier`` 1.0, 0.5 when drawdown > 2 %, 0 when any
  regime is chaos; ``usdt_target_share`` from the S2 regime table for the dollar's regime;
  ``s1_margin_min_pct`` = config margin; ``risk_flags``; ``notes`` start with ``OFFLINE_LABEL``.
* **Reviewer prompt** (system prompt contains "Post-trade Reviewer") -> ``{journal, param_suggestions:
  [], alerts: []}`` where the journal (starting with ``OFFLINE_LABEL``) only counts what it was given.

Model name ``offline-rules-v1``, latency 0, usage ``{}``. Output depends only on the messages (sorted
iteration, no clock, no randomness), so identical requests give identical responses and the LLM cache
replays them exactly. Every plan it produces carries ``model="offline-rules-v1"`` so logs, health and
the TUI can show ``OFFLINE_STATUS``.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from wta.ai.cache import LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, Route, ToolCall
from wta.config import Config, S2Cfg, Settings
from wta.strategies.s2_usdtalloc import rule_regime
from wta.utils.decimalutil import pct
from wta.utils.timeutil import Clock

log = logging.getLogger(__name__)

OFFLINE_PROVIDER = "offline"
OFFLINE_MODEL = "offline-rules-v1"
OFFLINE_LABEL = "[offline rules provider — not an LLM]"
OFFLINE_STATUS = "AI: offline rules (no LLM key)"

DOLLAR = "USDTTMN"
REGIME_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "USDTTMN")  # sorted
REVIEWER_MARKER = "Post-trade Reviewer"
DRAWDOWN_HALF_SIZE = Decimal("0.02")  # drawdown (fraction of peak) above which size is halved
CANDLE_STATS_BARS = 97  # 97 completed 15-min closes -> ret_all spans exactly 24 h
MAX_TOOL_MESSAGES = 16  # never ask for more tools once this many results are in the conversation
_FEATURE_KEYS = frozenset({"trend_flag", "ema_gap_pct", "ret_24h", "atr_15m_pct", "atr_15m_pct_median_24h"})
_DECISION_KINDS = frozenset({"intent", "veto", "order", "plan", "outcome", "no_trade", "event"})
_OFFLINE_TOOLS = ("get_candle_stats", "get_features", "get_pnl_summary")  # tools this provider asks for
_FLAT_OBJECT = re.compile(r'"([A-Za-z0-9_]+)":(\{[^{}]*\})')
_DRAWDOWN_IN_TEXT = re.compile(r'"drawdown_pct":\s*"?(-?[0-9][0-9.eE+-]*)"?')
_CENT = Decimal("0.01")


# ---------------------------------------------------------------- engine-facing helpers


def has_llm_key(settings: Settings) -> bool:
    """True when an OpenRouter or Gemini API key is configured (the value itself is never returned)."""
    return bool(settings.openrouter_api_key.get_secret_value() or settings.gemini_api_key.get_secret_value())


def use_offline_provider(cfg: Config, settings: Settings) -> bool:
    """The engine's switch: offline rules ONLY when the AI layer is on and no LLM key exists."""
    return cfg.ai.enabled and not has_llm_key(settings)


def is_offline_model(model: str | None) -> bool:
    return model == OFFLINE_MODEL


def ai_status_label(model: str | None) -> str:
    """Health/TUI label for the model that produced the current plan."""
    if is_offline_model(model):
        return OFFLINE_STATUS
    return f"AI: {model}" if model else "AI: no plan"


# ---------------------------------------------------------------- message parsing


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI "parts"
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if isinstance(content, dict):
        return json.dumps(content, sort_keys=True)
    return str(content)


def _json_objects(text: str) -> list[Any]:
    """Every top-level JSON object embedded in ``text`` (prose and ``` fences tolerated), in order."""
    out: list[Any] = []
    decoder = json.JSONDecoder()
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return out
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        out.append(obj)
        i = end


def _tool_content(content: Any) -> list[Any]:
    """A tool message's content as JSON values (normally exactly one)."""
    if isinstance(content, (dict, list)):
        return [content]
    text = _text(content).strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        return _json_objects(text)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _int(value: Any) -> int:
    d = _dec(value)
    return int(d) if d is not None else 0


def _num(value: Decimal) -> float:
    """Decimal -> JSON number (shortest float repr, e.g. 0.0015)."""
    return float(value)


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _is_reviewer(messages: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "system" and REVIEWER_MARKER in _text(m.get("content")) for m in messages)


@dataclass
class _ToolActivity:
    tool_messages: int = 0
    requested: set[str] = field(default_factory=set)  # tool names already asked for / answered


def _tool_activity(messages: list[dict[str, Any]]) -> _ToolActivity:
    act = _ToolActivity()
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                if isinstance(fn.get("name"), str):
                    act.requested.add(fn["name"])
        elif role == "tool":
            act.tool_messages += 1
            if isinstance(m.get("name"), str):
                act.requested.add(m["name"])
            call_id = m.get("tool_call_id")
            if isinstance(call_id, str) and call_id.startswith("offline_"):
                rest = call_id.removeprefix("offline_")
                act.requested.update(n for n in _OFFLINE_TOOLS if rest.startswith(n))
    return act


# ---------------------------------------------------------------- evidence harvesting


@dataclass
class _Evidence:
    features: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> feature dict
    candle_stats: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> get_candle_stats
    pnl: dict[str, Any] | None = None
    tool_results: int = 0
    tool_errors: int = 0
    salvaged: bool = False


def _looks_like_features(d: dict[str, Any]) -> bool:
    return any(k in d for k in _FEATURE_KEYS)


def _salvage(prefix: str, ev: _Evidence) -> None:
    """Recover complete flat objects from a hard-cut tool result (``{"truncated", "json_prefix"}``)."""
    ev.salvaged = True
    for m in _FLAT_OBJECT.finditer(prefix):
        name, body = m.group(1), m.group(2)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if _looks_like_features(obj):
            ev.features.setdefault(DOLLAR if name == "dollar" else name.upper(), obj)
        elif ev.pnl is None and "drawdown_pct" in obj:
            ev.pnl = obj
    if ev.pnl is None:
        dd = _DRAWDOWN_IN_TEXT.search(prefix)
        if dd:
            ev.pnl = {"drawdown_pct": dd.group(1)}


def _harvest(node: Any, ev: _Evidence, depth: int = 0) -> None:
    """Walk a JSON value; the first source of each item wins (callers feed the freshest data first)."""
    if depth > 8:
        return
    if isinstance(node, list):
        for x in node:
            _harvest(x, ev, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if node.get("truncated") is True and isinstance(node.get("json_prefix"), str):
        _salvage(node["json_prefix"], ev)
        return
    symbols = node.get("symbols")
    if isinstance(symbols, dict):
        for sym in sorted(symbols):
            f = symbols[sym]
            if isinstance(f, dict) and _looks_like_features(f):
                ev.features.setdefault(str(sym).upper(), f)
    dollar = node.get("dollar")
    if isinstance(dollar, dict) and _looks_like_features(dollar):
        ev.features.setdefault(DOLLAR, dollar)
    sym = node.get("symbol")
    if isinstance(sym, str) and "atr14_pct" in node and "ema_gap_pct" in node and "error" not in node:
        ev.candle_stats.setdefault(sym.upper(), node)
    if ev.pnl is None and _dec(node.get("drawdown_pct", node.get("drawdown"))) is not None:
        ev.pnl = node
    for key in sorted(node):
        if key in ("symbols", "dollar", "cross"):
            continue
        value = node[key]
        if isinstance(value, (dict, list)):
            _harvest(value, ev, depth + 1)


def _evidence(messages: list[dict[str, Any]]) -> _Evidence:
    """Tool results (newest first), then JSON embedded in user messages (newest first)."""
    ev = _Evidence()
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        for value in _tool_content(m.get("content")):
            ev.tool_results += 1
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                ev.tool_errors += 1
            _harvest(value, ev)
    for m in reversed(messages):
        if m.get("role") == "user":
            for value in _json_objects(_text(m.get("content"))):
                _harvest(value, ev)
    return ev


# ---------------------------------------------------------------- rules


def _rule_inputs_from_features(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "trend_flag": _int(f.get("trend_flag")),
        "ret_24h": _dec(f.get("ret_24h")),
        "ema_gap_pct": _dec(f.get("ema_gap_pct")),
        "atr_15m_pct": _dec(f.get("atr_15m_pct")),
        "atr_15m_pct_median_24h": _dec(f.get("atr_15m_pct_median_24h")),
        "book_age_s": _dec(f.get("book_age_s")),
    }


def _rule_inputs_from_candle_stats(cs: dict[str, Any], s2: S2Cfg) -> dict[str, Any]:
    """Approximate the feature inputs from ``get_candle_stats`` (no 24 h ATR median -> no chaos call)."""
    gap = _dec(cs.get("ema_gap_pct"))
    trend = (1 if gap > 0 else -1) if gap is not None and abs(gap) > s2.trend_flag_min_gap_pct else 0
    full_day = _int(cs.get("bars")) >= CANDLE_STATS_BARS and str(cs.get("resolution", "15")) == "15"
    return {
        "trend_flag": trend,
        "ret_24h": _dec(cs.get("ret_all")) if full_day else None,
        "ema_gap_pct": gap,
        "atr_15m_pct": _dec(cs.get("atr14_pct")),
        "atr_15m_pct_median_24h": None,
        "book_age_s": None,
    }


def _confidence(regime: str, inputs: dict[str, Any], s2: S2Cfg) -> Decimal:
    """Trend: 0.5..0.9 rising with |EMA gap| (full strength at 5x the trend-flag gap); range: 0.6..0.3
    falling with |EMA gap| (0.3 without an EMA gap); chaos: 0.5..0.9 rising with ATR / (chaos_mult x
    24 h median)."""
    gap = inputs.get("ema_gap_pct")
    if gap is None and regime == "range":
        return Decimal("0.30")
    full = s2.trend_flag_min_gap_pct * 5
    strength = min(abs(gap) / full, Decimal(1)) if gap is not None and full > 0 else Decimal(0)
    if regime in ("trend_up", "trend_down"):
        c = Decimal("0.5") + Decimal("0.4") * strength
    elif regime == "chaos":
        atr_pct, med = inputs.get("atr_15m_pct"), inputs.get("atr_15m_pct_median_24h")
        excess = atr_pct / (s2.chaos_atr_mult * med) - 1 if atr_pct is not None and med else Decimal(0)
        c = Decimal("0.5") + Decimal("0.4") * min(max(excess, Decimal(0)), Decimal(1))
    else:
        c = Decimal("0.6") - Decimal("0.3") * strength
    return c.quantize(_CENT, rounding=ROUND_HALF_EVEN)


class OfflineRulesProvider:
    """Deterministic rules dressed as a ``Provider`` — NOT an LLM (see the module docstring)."""

    name = OFFLINE_PROVIDER

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        return self.respond(messages, tools)

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        if _is_reviewer(messages):
            return self._final(self.review(messages))
        calls = self.tool_requests(messages, tools)
        if calls:
            names = ", ".join(sorted({c.name for c in calls}))
            return LLMResponse(text=f"{OFFLINE_LABEL} requesting tools: {names}", tool_calls=calls, usage={},
                               model=OFFLINE_MODEL, provider=self.name, latency_ms=0)
        return self._final(self.plan(_evidence(messages)))

    def _final(self, body: dict[str, Any]) -> LLMResponse:
        text = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return LLMResponse(text=text, tool_calls=[], usage={}, model=OFFLINE_MODEL, provider=self.name, latency_ms=0)

    # ------------------------------------------------------------ analyst: tool rounds

    def tool_requests(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> list[ToolCall]:
        offered = _tool_names(tools)
        if not offered:
            return []
        act = _tool_activity(messages)
        if act.tool_messages == 0 and not act.requested:
            calls: list[ToolCall] = []
            if "get_features" in offered:
                calls.append(ToolCall("offline_get_features", "get_features", {"symbols": list(REGIME_SYMBOLS)}))
            if "get_pnl_summary" in offered:
                calls.append(ToolCall("offline_get_pnl_summary", "get_pnl_summary", {}))
            return calls
        if act.tool_messages >= MAX_TOOL_MESSAGES or "get_candle_stats" not in offered:
            return []
        if "get_candle_stats" in act.requested:
            return []
        ev = _evidence(messages)
        missing = [s for s in REGIME_SYMBOLS if s not in ev.features and s not in ev.candle_stats]
        return [ToolCall(f"offline_get_candle_stats_{s}", "get_candle_stats",
                         {"symbol": s, "resolution": "15", "n": CANDLE_STATS_BARS}) for s in missing]

    # ------------------------------------------------------------ analyst: the plan

    def plan(self, ev: _Evidence) -> dict[str, Any]:
        cfg = self.cfg
        s2 = cfg.strategies.s2_usdtalloc
        stale_s = _dec(cfg.engine.stale_data_s)
        regimes: dict[str, dict[str, Any]] = {}
        sources: set[str] = set()
        flags: set[str] = set()
        summary: list[str] = []
        for sym in REGIME_SYMBOLS:
            if sym in ev.features:
                inputs, source = _rule_inputs_from_features(ev.features[sym]), "features"
            elif sym in ev.candle_stats:
                inputs, source = _rule_inputs_from_candle_stats(ev.candle_stats[sym], s2), "candle stats"
            else:
                regimes[sym] = {"regime": "range", "confidence": 0.0,
                                "evidence": "no features available; conservative range default"}
                flags.add("missing_features")
                summary.append(f"{sym} range (no data)")
                continue
            regime, evidence = rule_regime(inputs, s2)
            conf = _confidence(regime, inputs, s2)
            regimes[sym] = {"regime": regime, "confidence": _num(conf), "evidence": f"{evidence} [{source}]"}
            sources.add(source)
            summary.append(f"{sym} {regime} (conf {conf})")
            age = inputs.get("book_age_s")
            if age is not None and stale_s is not None and age > stale_s:
                flags.add("stale_books")
            if regime == "chaos":
                flags.add("chaos")
                if sym == DOLLAR:
                    flags.add("high_dollar_volatility")

        dd = _dec((ev.pnl or {}).get("drawdown_pct", (ev.pnl or {}).get("drawdown")))
        if dd is not None and dd > DRAWDOWN_HALF_SIZE:
            flags.add("drawdown")
        if "chaos" in flags:
            size, size_why = Decimal(0), "a chaos regime was called"
        elif "drawdown" in flags:
            size, size_why = Decimal("0.5"), f"drawdown {pct(dd)} > {pct(DRAWDOWN_HALF_SIZE, 0)}"
        else:
            size, size_why = Decimal(1), "normal conditions"
        if ev.tool_errors:
            flags.add("tool_errors")

        dollar_regime = regimes[DOLLAR]["regime"]
        table = s2.target_share_by_regime
        lo, hi = s2.bounds
        share = min(max(table.get(dollar_regime, table["range"]), lo), hi)
        margin = cfg.strategies.s1_crossbook.margin_min_pct
        enabled = [sid for sid, on in (("S1", cfg.strategies.s1_crossbook.enabled), ("S2", s2.enabled)) if on]

        source_txt = " + ".join(sorted(sources)) if sources else "no market data"
        if ev.salvaged:
            source_txt += " (salvaged from a size-capped tool result)"
        notes = (
            f"{OFFLINE_LABEL} Rules-only regimes from {source_txt}: {', '.join(summary)}. "
            f"Size multiplier {size} because {size_why}; USDT target {pct(share, 0)} from the S2 table for a "
            f"{dollar_regime} dollar; S1 margin kept at the config {pct(margin, 2)}. "
            f"Risk flags: {', '.join(sorted(flags)) or 'none'}; drawdown "
            f"{pct(dd) if dd is not None else 'unknown'}; {ev.tool_errors} tool error(s)."
        )
        return {
            "regimes": regimes,
            "enabled_strategies": enabled,
            "s1_margin_min_pct": _num(margin),
            "size_multiplier": _num(size),
            "usdt_target_share": _num(share),
            "risk_flags": sorted(flags),
            "notes": notes,
        }

    # ------------------------------------------------------------ reviewer

    def review(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        values: list[Any] = []
        n_lines = 0
        for m in messages:
            if m.get("role") != "user":
                continue
            text = _text(m.get("content"))
            n_lines += len(text.splitlines())
            values.extend(_json_objects(text))
        lines = [f"{OFFLINE_LABEL} Deterministic count of the review input; no model judged this hour."]
        if not values:
            lines.append(f"- Input: {n_lines} line(s) of text, no structured data found.")
        kinds: Counter[str] = Counter()
        _count_kinds(values, kinds)
        if kinds:
            detail = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
            lines.append(f"- Decision records: {sum(kinds.values())} ({detail}).")
        facts: list[str] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            for key in sorted(value):
                v = value[key]
                if isinstance(v, list):
                    facts.append(f"- `{key}`: {len(v)} item(s).")
                elif isinstance(v, dict):
                    facts.append(f"- `{key}`: {len(v)} entr{'y' if len(v) == 1 else 'ies'}.")
                elif (isinstance(v, (int, float)) and not isinstance(v, bool)) or (isinstance(v, str) and len(v) <= 40):
                    facts.append(f"- `{key}`: {v}.")
        lines.extend(facts[:10])
        if len(facts) > 10:
            lines.append(f"- ... and {len(facts) - 10} more field(s).")
        lines.append("- No parameter changes suggested: the offline provider never tunes parameters.")
        lines.append("- Configure an OpenRouter or Gemini key for a real LLM review.")
        return {"journal": "\n".join(lines), "param_suggestions": [], "alerts": []}


def _count_kinds(node: Any, kinds: Counter[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(node, list):
        for x in node:
            _count_kinds(x, kinds, depth + 1)
    elif isinstance(node, dict):
        kind = node.get("kind")
        if isinstance(kind, str) and kind in _DECISION_KINDS:
            kinds[kind] += 1
            return
        for key in sorted(node):
            _count_kinds(node[key], kinds, depth + 1)


# ---------------------------------------------------------------- factory


def make_offline_llm(cfg: Config, cache: LLMCache, mode: str, clock: Clock, *,
                     on_call: Callable[[dict[str, Any]], None] | None = None) -> LLMClient:
    """An ``LLMClient`` whose only route is the offline rules provider (no fallback, no call budget —
    it costs nothing). Responses are cached like any model's, so ``replay`` reproduces them exactly.
    Use it ONLY when ``use_offline_provider(cfg, settings)`` is true."""
    log.warning("%s: AI plans come from %s (deterministic rules, NOT an LLM)", OFFLINE_STATUS, OFFLINE_MODEL)
    return LLMClient({OFFLINE_PROVIDER: OfflineRulesProvider(cfg)}, Route(OFFLINE_PROVIDER, OFFLINE_MODEL), None,
                     cache, mode=mode, clock=clock, daily_budget=None, on_call=on_call)
