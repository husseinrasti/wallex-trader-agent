"""The Regime Analyst's output contract (PRD-04 §6): strict parsing, then deterministic clamping.

* ``PlanOutput`` is what the model must return. It is strict (``extra="forbid"`` everywhere, every key
  required, finite numbers only, booleans are not numbers), so a malformed answer fails validation
  and gets one "fix the JSON" retry.
* ``plan_json_schema`` / ``PLAN_JSON_SCHEMA`` and ``PLAN_EXAMPLE`` are pasted into the system prompt.
* ``clamp_plan`` turns a valid ``PlanOutput`` into a ``MarketPlan``. The model's numbers are untrusted:
  ``s1_margin_min_pct`` in [0.0008, 0.01], ``size_multiplier`` in [0, 1], ``usdt_target_share`` in the
  S2 config bounds, confidences in [0, 1], ``enabled_strategies`` a subset of the strategies enabled in
  config, regimes exactly the four required symbols (missing ones become ``range`` / 0.0). Every value
  that had to change is recorded in ``MarketPlan.clamped`` as ``{field: original}``. Money-like fields
  become ``Decimal`` (built from the float's shortest repr, so the result is reproducible).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat, field_validator

from wta.config import Config
from wta.models import MarketPlan, RegimeCall

REQUIRED_SYMBOLS: tuple[str, ...] = ("USDTTMN", "BTCUSDT", "ETHUSDT", "SOLUSDT")
REGIMES: tuple[str, ...] = ("range", "trend_up", "trend_down", "chaos")
STRATEGY_IDS: tuple[str, ...] = ("S1", "S2", "S3")

S1_MARGIN_BOUNDS: tuple[Decimal, Decimal] = (Decimal("0.0008"), Decimal("0.01"))
SIZE_MULTIPLIER_BOUNDS: tuple[Decimal, Decimal] = (Decimal("0"), Decimal("1"))
CONFIDENCE_BOUNDS: tuple[float, float] = (0.0, 1.0)

MISSING_EVIDENCE = "missing in model output"
MAX_RISK_FLAGS = 12
MAX_RISK_FLAG_CHARS = 64
MAX_NOTES_CHARS = 2000
MAX_EVIDENCE_CHARS = 500


# ---------------------------------------------------------------- what the model returns


def _not_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    return value


class RegimeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regime: Literal["range", "trend_up", "trend_down", "chaos"]
    confidence: FiniteFloat
    evidence: str

    _no_bool = field_validator("confidence", mode="before")(_not_bool)


class PlanOutput(BaseModel):
    """The JSON object the analyst model must return (``additionalProperties: false``)."""

    model_config = ConfigDict(extra="forbid")

    regimes: dict[str, RegimeOut]
    enabled_strategies: list[str]
    s1_margin_min_pct: FiniteFloat
    size_multiplier: FiniteFloat
    usdt_target_share: FiniteFloat
    risk_flags: list[str]
    notes: str

    _no_bool = field_validator("s1_margin_min_pct", "size_multiplier", "usdt_target_share", mode="before")(
        _not_bool
    )


# ---------------------------------------------------------------- schema + example for the prompt


def _num(d: Decimal) -> float | int:
    """A bound as a plain JSON number (schemas only; never used for money arithmetic)."""
    return int(d) if d == d.to_integral_value() else float(d)


def plan_json_schema(cfg: Config | None = None) -> dict[str, Any]:
    """JSON schema of ``PlanOutput`` with the bounds of ``cfg`` (defaults when ``cfg`` is None)."""
    s2_lo, s2_hi = cfg.strategies.s2_usdtalloc.bounds if cfg is not None else (Decimal("0.25"), Decimal("0.70"))
    regime = {
        "type": "object",
        "properties": {
            "regime": {"type": "string", "enum": list(REGIMES)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string"},
        },
        "required": ["regime", "confidence", "evidence"],
        "additionalProperties": False,
    }
    return {
        "$defs": {"regime_call": regime},
        "type": "object",
        "properties": {
            "regimes": {
                "type": "object",
                "properties": {sym: {"$ref": "#/$defs/regime_call"} for sym in REQUIRED_SYMBOLS},
                "required": list(REQUIRED_SYMBOLS),
                "additionalProperties": False,
            },
            "enabled_strategies": {
                "type": "array",
                "items": {"type": "string", "enum": list(STRATEGY_IDS)},
                "uniqueItems": True,
            },
            "s1_margin_min_pct": {"type": "number", "minimum": _num(S1_MARGIN_BOUNDS[0]),
                                  "maximum": _num(S1_MARGIN_BOUNDS[1])},
            "size_multiplier": {"type": "number", "minimum": _num(SIZE_MULTIPLIER_BOUNDS[0]),
                                "maximum": _num(SIZE_MULTIPLIER_BOUNDS[1])},
            "usdt_target_share": {"type": "number", "minimum": _num(s2_lo), "maximum": _num(s2_hi)},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
        },
        "required": ["regimes", "enabled_strategies", "s1_margin_min_pct", "size_multiplier",
                     "usdt_target_share", "risk_flags", "notes"],
        "additionalProperties": False,
    }


PLAN_JSON_SCHEMA: dict[str, Any] = plan_json_schema()

PLAN_EXAMPLE: dict[str, Any] = {
    "regimes": {
        "USDTTMN": {"regime": "range", "confidence": 0.6,
                    "evidence": "ret_1h +0.1%, ema_gap +0.02%, atr_15m 0.15% (median 0.14%)"},
        "BTCUSDT": {"regime": "trend_up", "confidence": 0.7,
                    "evidence": "ret_1h +0.9%, ema20 > ema60 by 0.4%, volume ratio 1.8"},
        "ETHUSDT": {"regime": "range", "confidence": 0.5, "evidence": "ret_1h -0.1%, flat EMAs"},
        "SOLUSDT": {"regime": "chaos", "confidence": 0.6, "evidence": "atr_15m 2.1% vs median 0.8%"},
    },
    "enabled_strategies": ["S1", "S2"],
    "s1_margin_min_pct": 0.0015,
    "size_multiplier": 0.8,
    "usdt_target_share": 0.5,
    "risk_flags": ["sol_volatility"],
    "notes": "The dollar is flat and books are fresh, so S1 and S2 keep normal settings. "
             "BTC is trending up on rising volume. SOL is erratic, so size is cut to 0.8.",
}


def example_text(example: Mapping[str, Any] | None = None) -> str:
    """``PLAN_EXAMPLE`` as short JSON for the prompt: one line per top-level key and per regime."""
    ex = dict(example if example is not None else PLAN_EXAMPLE)
    keys = list(ex)
    lines = ["{"]
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        value = ex[key]
        if key == "regimes" and isinstance(value, Mapping):
            lines.append('  "regimes": {')
            syms = list(value)
            for j, sym in enumerate(syms):
                inner = "," if j < len(syms) - 1 else ""
                lines.append(f"    {json.dumps(sym)}: {json.dumps(value[sym], ensure_ascii=False)}{inner}")
            lines.append("  }" + comma)
        else:
            lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}{comma}")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------- clamping


def configured_enabled(cfg: Config) -> list[str]:
    """Strategy ids whose ``enabled`` flag is true in config, in id order."""
    st = cfg.strategies
    flags = {"S1": st.s1_crossbook.enabled, "S2": st.s2_usdtalloc.enabled, "S3": st.s3_momentum.enabled}
    return [sid for sid in STRATEGY_IDS if flags[sid]]


def to_decimal(x: float) -> Decimal:
    """Float from JSON -> Decimal via its shortest repr (0.0015 -> Decimal('0.0015'), 1.0 -> Decimal('1'))."""
    d = Decimal(repr(float(x)))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize()


def _clamp_dec(value: float, lo: Decimal, hi: Decimal) -> tuple[Decimal, bool]:
    d = to_decimal(value)
    if d < lo:
        return lo, True
    if d > hi:
        return hi, True
    return d, False


def normalize_symbol(sym: str) -> str:
    """'btc/usdt' / 'BTC-USDT' / ' BTCUSDT ' -> 'BTCUSDT'."""
    return "".join(ch for ch in str(sym).upper() if ch.isalnum())


def _clamp_regimes(regimes: Mapping[str, RegimeOut], clamped: dict[str, Any]) -> dict[str, RegimeCall]:
    by_symbol: dict[str, RegimeOut] = {}
    dropped: list[str] = []
    for raw in sorted(regimes):
        sym = normalize_symbol(raw)
        if sym in REQUIRED_SYMBOLS and sym not in by_symbol:
            by_symbol[sym] = regimes[raw]
        else:
            dropped.append(raw)
    if dropped:
        clamped["regimes.dropped"] = dropped
    missing = [s for s in REQUIRED_SYMBOLS if s not in by_symbol]
    if missing:
        clamped["regimes.missing"] = missing
    out: dict[str, RegimeCall] = {}
    lo, hi = CONFIDENCE_BOUNDS
    for sym in REQUIRED_SYMBOLS:
        r = by_symbol.get(sym)
        if r is None:
            out[sym] = RegimeCall(regime="range", confidence=0.0, evidence=MISSING_EVIDENCE)
            continue
        conf = float(r.confidence)
        if not lo <= conf <= hi:
            clamped[f"regimes.{sym}.confidence"] = r.confidence
            conf = min(max(conf, lo), hi)
        out[sym] = RegimeCall(regime=r.regime, confidence=conf, evidence=r.evidence.strip()[:MAX_EVIDENCE_CHARS])
    return out


def _clamp_strategies(requested: Iterable[str], allowed: Iterable[str], clamped: dict[str, Any]) -> list[str]:
    original = list(requested)
    allowed_set = {a.upper() for a in allowed}
    wanted = {str(s).strip().upper() for s in original}
    out = [sid for sid in STRATEGY_IDS if sid in wanted and sid in allowed_set]
    # Recorded only when an id was removed (unknown, or not enabled in config); a change of
    # order, case or duplicates keeps the same set and is not a clamp.
    if sorted(wanted) != out:
        clamped["enabled_strategies"] = original
    return out


def _clean_flags(flags: Iterable[str], clamped: dict[str, Any]) -> list[str]:
    original = list(flags)
    out: list[str] = []
    for f in original:
        tag = str(f).strip()[:MAX_RISK_FLAG_CHARS]
        if tag and tag not in out:
            out.append(tag)
    out = out[:MAX_RISK_FLAGS]
    if out != original:
        clamped["risk_flags"] = original
    return out


def clamp_plan(
    out: PlanOutput,
    cfg: Config,
    configured: Iterable[str] | None,
    now: float,
    meta: Mapping[str, Any] | None = None,
) -> MarketPlan:
    """Validated model output -> bounded ``MarketPlan`` (deterministic; never raises on bad values).

    ``configured`` = strategy ids enabled in config (``configured_enabled(cfg)`` when None).
    ``meta`` carries the audit fields: ``model``, ``prompt_hash``, ``cached``, ``tools_used``,
    ``tools_source``, ``latency_ms``.
    """
    meta = dict(meta or {})
    clamped: dict[str, Any] = {}
    allowed = list(configured) if configured is not None else configured_enabled(cfg)

    margin, c = _clamp_dec(out.s1_margin_min_pct, *S1_MARGIN_BOUNDS)
    if c:
        clamped["s1_margin_min_pct"] = out.s1_margin_min_pct
    size, c = _clamp_dec(out.size_multiplier, *SIZE_MULTIPLIER_BOUNDS)
    if c:
        clamped["size_multiplier"] = out.size_multiplier
    s2_lo, s2_hi = cfg.strategies.s2_usdtalloc.bounds
    share, c = _clamp_dec(out.usdt_target_share, Decimal(s2_lo), Decimal(s2_hi))
    if c:
        clamped["usdt_target_share"] = out.usdt_target_share

    regimes = _clamp_regimes(out.regimes, clamped)
    enabled = _clamp_strategies(out.enabled_strategies, allowed, clamped)
    flags = _clean_flags(out.risk_flags, clamped)
    notes = out.notes.strip()
    if len(notes) > MAX_NOTES_CHARS:
        clamped["notes"] = {"truncated_from_chars": len(notes)}
        notes = notes[:MAX_NOTES_CHARS]

    latency = meta.get("latency_ms")
    return MarketPlan(
        ts=float(now),
        regimes=regimes,
        enabled_strategies=enabled,
        s1_margin_min_pct=margin,
        size_multiplier=size,
        usdt_target_share=share,
        risk_flags=flags,
        notes=notes,
        model=str(meta.get("model") or ""),
        prompt_hash=str(meta.get("prompt_hash") or ""),
        cached=bool(meta.get("cached", False)),
        tools_used=sorted({str(t) for t in meta.get("tools_used") or []}),
        tools_source=meta.get("tools_source") or "none",
        latency_ms=int(latency) if latency is not None and math.isfinite(float(latency)) else None,
        clamped=clamped,
    )
