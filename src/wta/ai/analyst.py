"""Regime Analyst (PRD-04 §6): the slow-loop LLM agent that writes the ``MarketPlan``.

One ``run`` = one plan (or ``None``):

1. System prompt = ``prompts/analyst.md`` rendered with the current config values (only known
   ``{name}`` keys are replaced, so literal JSON braces survive) + the exact output schema.
2. User message = canonical JSON (sorted keys) of a compact state: watchlist features (the curated
   ``tools.feature_view`` with its key legend), PnL summary, health, the last plan, recent events
   and the trigger reason. Same state + same clock => the same
   bytes => the same cache key.
3. Hand-written tool loop, at most ``ai.analyst_max_tool_rounds`` rounds. Tools offered = internal
   tools + the Wallex MCP read-only tools (when an MCP client is set and ``initialize`` worked), else
   the ``*_local`` mirrors. Tool calls and results are appended in OpenAI format (assistant
   ``tool_calls`` + ``role: tool`` messages), so every intermediate step is part of the next cache
   key and the whole trajectory replays. At the cap the model is asked for the final answer without
   tools. An MCP failure never aborts the cycle: the model gets an error result and the ``_local``
   tools from the next round on (``tools_source = "local"``).
4. The answer is parsed with ``PlanOutput`` (one "fix the JSON" retry), then clamped by
   ``clamp_plan``. The plan carries ``prompt_hash`` (cache key of the first call), ``cached`` (every
   call came from the cache), ``tools_used``, ``tools_source`` and the summed ``latency_ms``.

Failures (no LLM, provider down, invalid JSON twice, anything unexpected) return ``None`` and leave
the reason in ``last_error``; the engine keeps rules-only defaults. A strict-replay cache miss
(``CacheMissError``) is re-raised: replay must prove determinism, not degrade silently.

Replay with MCP: in shadow/live every MCP tool list and MCP result the model saw is recorded in a
small tape next to the LLM cache (``<cache_dir>/mcp_tape``). When the LLM client runs in ``replay``
mode the analyst never touches the MCP server: it reads the same schemas and results from the tape,
so a trajectory that used Wallex MCP tools replays byte-identically with zero network.

The same tape holds every internal tool result (``tool-call-*``: the engine state changes between
cycles, so a replay without the engine reads the recorded answers; when there is none, e.g. a full
engine replay that rebuilds the state, the tool runs) and, per plan, the cycle's input
(``analyst-cycle-<prompt_hash>``: ``now``, ``reason``, the JSON state passed to ``run`` and the
``config_hash``), so ``replay_cycle`` re-runs a recorded cycle from the cache and the tape alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, ToolCall, extract_json
from wta.ai.mcp_client import MCPClient, MCPToolNotAllowed, tool_payload, truncate_result
from wta.ai.plan_schema import (
    REGIMES,
    REQUIRED_SYMBOLS,
    PlanOutput,
    clamp_plan,
    configured_enabled,
    example_text,
    plan_json_schema,
)
from wta.ai.tools import (
    INTERNAL_TOOL_SCHEMAS,
    LOCAL_TOOL_SCHEMAS,
    InternalTools,
    feature_rows,
    feature_view,
    is_feature_view,
    to_jsonable,
    truncate,
)
from wta.config import Config, canonical_json, mask_secrets
from wta.features.engine import FeatureSnapshot
from wta.models import MarketPlan
from wta.utils.timeutil import Clock, iso, parse_iso, utc_day

log = logging.getLogger(__name__)
Json = dict[str, Any]
ToolsSource = Literal["mcp", "local", "none"]

PROMPT_PATH = Path(__file__).with_name("prompts") / "analyst.md"

# ---- triggers
MIN_INTERVAL_S = 60.0  # never more often than this, whatever triggers
MOVE_WINDOW_S = 900.0  # a sustained 15-minute move re-triggers at most once per window
MOVE_TRIGGERS: tuple[tuple[str, Decimal], ...] = (("USDTTMN", Decimal("0.01")), ("BTCUSDT", Decimal("0.02")))
TRIGGER_EVENT_KINDS = frozenset({"kill", "pause", "flatten_start"})
FLATTEN_NOT_A_START = frozenset({"flatten_final", "challenge_end", "complete"})

# ---- tool loop
MCP_RETRY_S = 600.0  # after an MCP failure, try to initialize again after this long
MAX_CALLS_PER_ROUND = 8  # tool calls executed per model turn; extra calls get an error result
MAX_EVENTS_IN_PROMPT = 10

SYMBOL_FEATURE_KEYS: tuple[str, ...] = (
    "mid", "spread_pct", "book_age_s", "ret_15m", "ret_1h", "ret_24h", "realized_vol_1h_pct",
    "atr_15m_pct", "atr_15m_pct_median_24h", "ema_gap_pct", "trend_flag", "vol_ratio_15m",
)
CROSS_FEATURE_KEYS: tuple[str, ...] = (
    "dev_mid_pct", "dev_zscore", "edge_tmn_cheap_pct", "edge_usdt_cheap_pct",
    "cost_lockin_tmn_cheap", "cost_lockin_usdt_cheap", "tmn_book_age_s", "usdt_book_age_s",
)

FORCE_FINAL = ("Tool-call limit reached ({rounds} rounds). Do not call any more tools. "
               "Return only the final JSON plan now, based on the data above.")
FIX_JSON = ("Your JSON was invalid: {err}\nFix the JSON. Return only the corrected JSON object: exactly the "
            "top-level keys {keys}; `regimes` holds only {symbols} and is closed before `enabled_strategies`.")
PLAN_KEYS: tuple[str, ...] = tuple(PlanOutput.model_fields)
USER_TAIL = ("Decide the plan. Use the tools for anything you need that is not above, "
             "then return only the JSON object.")

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


# ---------------------------------------------------------------- small helpers


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Replace ``{name}`` for the known names only; everything else (JSON braces) stays as is."""
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def _dumps(obj: Any) -> str:
    return canonical_json(obj).decode("utf-8")


def _plain_numbers(obj: Any) -> Any:
    """MCP schemas are decoded with Decimal floats; providers need plain JSON numbers."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, Mapping):
        return {str(k): _plain_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain_numbers(v) for v in obj]
    return obj


def mcp_tool_schema(tool: Mapping[str, Any]) -> Json:
    """An MCP ``tools/list`` entry -> the OpenAI function shape ``{name, description, parameters}``."""
    params = tool.get("inputSchema") or {"type": "object", "properties": {}}
    return {
        "name": str(tool["name"]),
        "description": str(tool.get("description") or "")[:500],
        "parameters": _plain_numbers(params),
    }


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def feature_value(features: Any, symbol: str, key: str) -> Decimal | None:
    """``features.symbols[symbol][key]`` from a FeatureSnapshot, its ``compact()`` dict or a
    ``feature_view`` (keys under their FeatureSnapshot names, e.g. ``ret_15m``)."""
    if features is None:
        return None
    if is_feature_view(features):
        row = feature_rows(features).get(symbol)
        return _to_decimal(row.get(key)) if row else None
    rows = getattr(features, "symbols", None)
    if rows is None and isinstance(features, Mapping):
        rows = features.get("symbols", features)
    if not isinstance(rows, Mapping):
        return None
    row = rows.get(symbol)
    return _to_decimal(row.get(key)) if isinstance(row, Mapping) else None


def _event_kind(event: Any) -> str | None:
    """The trigger name of an event (``kill``, ``pause``, ``flatten_start``) or None."""
    if isinstance(event, str):
        kind, data = event, {}
    elif isinstance(event, Mapping):
        kind, data = event.get("kind"), event.get("data") or {}
    else:
        kind, data = getattr(event, "kind", None), getattr(event, "data", None) or {}
    kind = str(kind or "")
    if kind == "flatten":
        sub = data.get("kind") if isinstance(data, Mapping) else None
        return None if sub in FLATTEN_NOT_A_START else "flatten_start"
    return kind if kind in TRIGGER_EVENT_KINDS else None


def should_run(
    cfg: Config,
    now: float,
    last_run_ts: float | None,
    features: Any,
    events: Iterable[Any] = (),
    move_trigger_ts: Mapping[str, float] | None = None,
) -> tuple[bool, str]:
    """Is an analyst cycle due? Returns ``(run, reason)``.

    * never within ``MIN_INTERVAL_S`` (60 s) of the last run (``min_interval``);
    * first run ever -> ``startup``;
    * a kill / pause / flatten-start event -> ``event:<kind>``;
    * |USDTTMN ret_15m| > 1 % or |BTCUSDT ret_15m| > 2 % -> ``move:<symbol>`` (a sustained move
      re-triggers at most once per ``MOVE_WINDOW_S``, see ``move_trigger_ts``);
    * ``engine.slow_loop_interval_s`` since the last run -> ``scheduled``; else ``not_due``.

    ``events`` are the events since the last run (strings, dicts or ``EventRecord``s); the caller keeps
    passing them until a run happens.
    """
    if last_run_ts is None:
        return True, "startup"
    elapsed = now - last_run_ts
    if elapsed < MIN_INTERVAL_S:
        return False, "min_interval"
    for ev in events:
        kind = _event_kind(ev)
        if kind is not None:
            return True, f"event:{kind}"
    recent = move_trigger_ts or {}
    for symbol, limit in MOVE_TRIGGERS:
        move = feature_value(features, symbol, "ret_15m")
        if move is None or abs(move) <= limit:
            continue
        last = recent.get(symbol)
        if last is not None and now - last < MOVE_WINDOW_S:
            continue
        return True, f"move:{symbol}"
    if elapsed >= cfg.engine.slow_loop_interval_s:
        return True, "scheduled"
    return False, "not_due"


def plan_is_fresh(plan: MarketPlan | None, now: float, cfg: Config) -> bool:
    """True while ``plan`` is at most ``ai.plan_stale_s`` old (same rule as ``StrategyContext``)."""
    return plan is not None and now - plan.ts <= cfg.ai.plan_stale_s


def time_to_deadline_s(cfg: Config, now: float, state: Mapping[str, Any] | None = None) -> float | None:
    if state and state.get("time_to_deadline_s") is not None:
        return float(state["time_to_deadline_s"])
    try:
        end = parse_iso(cfg.engine.challenge_end_utc)
    except ValueError:
        return None
    return None if end is None else end - now


def deadline_text(cfg: Config, now: float, state: Mapping[str, Any] | None = None) -> str:
    left = time_to_deadline_s(cfg, now, state)
    if left is None:
        return "unknown (challenge end not configured)"
    if left <= 0:
        return "0 (the challenge has ended)"
    days, rem = divmod(int(left), 86400)
    hours, rem = divmod(rem, 3600)
    text = f"{days}d {hours:02d}h {rem // 60:02d}m"
    try:
        end = parse_iso(cfg.engine.challenge_end_utc)
    except ValueError:
        end = None
    return f"{text} (ends {iso(end)[:16]}Z)" if end is not None else text


def compact_features(raw: Any) -> Any:
    """The handful of feature numbers the model needs per symbol / cross-book coin (None dropped).

    A ``feature_view`` (``get_features``; what the engine passes) is already curated: unchanged."""
    if not isinstance(raw, Mapping) or "error" in raw or is_feature_view(raw):
        return raw
    out: Json = {"ts": raw.get("ts")}
    symbols = raw.get("symbols") or {}
    out["symbols"] = {
        sym: {k: row[k] for k in SYMBOL_FEATURE_KEYS if row.get(k) is not None}
        for sym, row in sorted(symbols.items()) if isinstance(row, Mapping)
    }
    cross = raw.get("cross") or {}
    out["cross"] = {
        coin: {k: row[k] for k in CROSS_FEATURE_KEYS if row.get(k) is not None}
        for coin, row in sorted(cross.items()) if isinstance(row, Mapping)
    }
    return out


def cycle_tape_key(prompt_hash: str) -> str:
    """Tape key of a recorded cycle input (``now``, ``reason``, ``state``) for the plan with this hash."""
    return f"analyst-cycle-{prompt_hash}"


def _tape_key(prefix: str, *parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x00")
    return f"{prefix}-{h.hexdigest()}"


def _usage_total(responses: list[LLMResponse]) -> Json:
    total: Json = {}
    for r in responses:
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = (r.usage or {}).get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total


@dataclass
class _Run:
    """Mutable state of one analyst cycle."""

    messages: list[Json]
    state: Json = field(default_factory=dict)
    reason: str = ""
    now: float = 0.0
    offered: list[Json] | None = None
    source: ToolsSource = "none"
    mcp_names: set[str] = field(default_factory=set)
    responses: list[LLMResponse] = field(default_factory=list)
    tools_used: set[str] = field(default_factory=set)
    rounds: int = 0
    retried: bool = False

    @property
    def offered_names(self) -> set[str]:
        return {t["name"] for t in self.offered or []}


# ---------------------------------------------------------------- the analyst


class RegimeAnalyst:
    """Slow-loop agent: state -> tools -> LLM -> validated, clamped ``MarketPlan``."""

    def __init__(
        self,
        cfg: Config,
        llm: LLMClient | None,
        tools: InternalTools,
        mcp: MCPClient | None,
        clock: Clock,
        on_plan: Callable[[MarketPlan, Json], None] | None = None,
        *,
        tape: LLMCache | None = None,
        prompt_template: str | None = None,
        secrets: Iterable[str] = (),
        daily_cycle_budget: int | None = None,
    ) -> None:
        """``secrets`` (the engine's ``settings.secret_values()``) are masked out of every MCP result
        before it reaches the model, the LLM cache or the tape (the MCP client masks its own key
        already; this is the second layer). ``daily_cycle_budget``: at most that many cycles reach
        the model per UTC day (``ai.max_analyst_calls_per_day``; None: unlimited, e.g. the offline
        rules provider); a cycle over it returns None with ``last_error`` "daily analyst cycle
        budget exhausted"."""
        self.cfg = cfg
        self.secrets: list[str] = [s for s in secrets if s]
        self.daily_cycle_budget = daily_cycle_budget
        self._cycle_cfg: Config | None = None  # the config of the running cycle (see ``cycle_cfg``)
        self._cycles_by_day: dict[str, int] = {}
        self.llm = llm
        self.tools = tools
        self.mcp = mcp
        self.clock = clock
        self.on_plan = on_plan
        if tape is None and llm is not None and getattr(llm, "cache", None) is not None:
            tape = LLMCache(Path(llm.cache.dir) / "mcp_tape", strict=False)
        self.tape = tape
        self.template = prompt_template if prompt_template is not None else PROMPT_PATH.read_text(encoding="utf-8")
        self.watchlist: list[str] = sorted(set(cfg.markets.watchlist) | set(REQUIRED_SYMBOLS))
        self.last_plan: MarketPlan | None = None
        self.last_record: Json | None = None
        self.last_error = ""
        self.last_run_ts: float | None = None
        self.last_reason = ""
        self.last_messages: list[Json] = []
        self.mcp_error = ""
        self.move_trigger_ts: dict[str, float] = {}
        self._mcp_schemas: list[Json] | None = None
        self._mcp_retry_at: float | None = None
        self._stale_logged_for: float | None = None

    @property
    def cycle_cfg(self) -> Config:
        """The config of the running cycle: ``self.cfg`` as it was when ``run`` started (the engine
        may swap ``cfg`` while a cycle awaits the model; prompt, clamp and recorded config_hash
        must all come from one config). Outside a cycle: ``self.cfg``."""
        return self._cycle_cfg if self._cycle_cfg is not None else self.cfg

    # ------------------------------------------------------------ cadence + staleness

    def should_run(self, now: float, last_run_ts: float | None, features: Any,
                   events: Iterable[Any] = ()) -> tuple[bool, str]:
        """See module-level ``should_run``; remembers which symbols' moves already triggered a run."""
        return should_run(self.cfg, now, last_run_ts, features, events, self.move_trigger_ts)

    def fresh_plan(self, now: float) -> MarketPlan | None:
        """The last plan while fresh, else None (logged once per stale plan)."""
        plan = self.last_plan
        if plan is None or plan_is_fresh(plan, now, self.cfg):
            return plan
        if self._stale_logged_for != plan.ts:
            self._stale_logged_for = plan.ts
            log.warning("analyst: plan from %s is older than %.0fs; strategies use rules-only defaults",
                        iso(plan.ts), self.cfg.ai.plan_stale_s)
        return None

    # ------------------------------------------------------------ prompts

    def prompt_values(self, now: float, state: Mapping[str, Any] | None = None) -> dict[str, str]:
        st = self.cycle_cfg.strategies
        s1, s2, s3 = st.s1_crossbook, st.s2_usdtalloc, st.s3_momentum
        enabled = configured_enabled(self.cycle_cfg)
        # the template wraps {schema} in a ```json fence; the example gets a fence of its own
        schema = (json.dumps(plan_json_schema(self.cycle_cfg), ensure_ascii=False)
                  + "\n```\nExample (illustrative values only; use the numbers you see):\n```json\n"
                  + example_text())
        return {
            "time_to_deadline": deadline_text(self.cycle_cfg, now, state),
            "s1_coins": ", ".join(s1.coins),
            "s1_margin": str(s1.margin_min_pct),
            "s2_lo": str(s2.bounds[0]),
            "s2_hi": str(s2.bounds[1]),
            "s2_threshold": str(s2.rebalance_threshold_pp),
            "s2_defaults": ", ".join(f"{r} {s2.target_share_by_regime[r]}" for r in REGIMES
                                     if r in s2.target_share_by_regime),
            "s3_status": "enabled" if s3.enabled else "currently disabled in config; your calls are still logged",
            "s3_conf": str(s3.min_regime_confidence),
            "enabled": json.dumps(enabled) if enabled else "[] (no strategy is enabled in config)",
            "schema": schema,
        }

    def system_prompt(self, now: float, state: Mapping[str, Any] | None = None) -> str:
        return render_template(self.template, self.prompt_values(now, state))

    def user_message(self, state: Mapping[str, Any] | None, reason: str, now: float) -> str:
        """Compact, canonical (sorted-key) JSON of the state: deterministic for a given state + clock."""
        state = state or {}
        max_bytes = self.cycle_cfg.ai.tool_result_max_bytes
        left = time_to_deadline_s(self.cycle_cfg, now, state)
        payload: Json = {
            "reason": reason,
            "now_utc": iso(now),
            "time_to_deadline_s": int(left) if left is not None else None,
            "features": self._block(lambda: truncate(compact_features(self._features(state)), 4 * max_bytes)),
            "pnl_summary": self._block(lambda: truncate(
                state["pnl_summary"] if "pnl_summary" in state else self.tools.t_get_pnl_summary(), max_bytes)),
            "health": self._block(lambda: truncate(
                state["health"] if "health" in state else self.tools.t_get_health(), max_bytes)),
            "last_plan": self._block(lambda: self._last_plan_block(state, now)),
        }
        events = state.get("events")
        if events:
            payload["recent_events"] = self._block(lambda: truncate(
                [self._event_json(e) for e in list(events)[-MAX_EVENTS_IN_PROMPT:]], max_bytes))
        return "Current state (canonical JSON):\n" + _dumps(payload) + "\n\n" + USER_TAIL

    @staticmethod
    def _block(fn: Callable[[], Any]) -> Any:
        try:
            return to_jsonable(fn())
        except Exception as exc:  # noqa: BLE001 - a broken state source must not abort the cycle
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def first_features(self, snap: FeatureSnapshot) -> Json:
        """The features block of the first user message: the curated ``feature_view`` of the whole
        watchlist with every key and the key legend, within 4 x ``ai.tool_result_max_bytes`` (the
        engine passes exactly this in ``state["features"]``, as JSON, so it is taped)."""
        return feature_view(snap, self.watchlist, max_bytes=4 * self.cycle_cfg.ai.tool_result_max_bytes, legend=True)

    def _features(self, state: Mapping[str, Any]) -> Any:
        raw = state.get("features")
        if raw is None:
            raw = self.tools.d.features()
            if raw is None:
                return {"error": "features not available yet"}
        if isinstance(raw, FeatureSnapshot):
            return self.first_features(raw)
        return raw

    def _last_plan_block(self, state: Mapping[str, Any], now: float) -> Any:
        plan = state["last_plan"] if "last_plan" in state else self.last_plan
        if plan is None:
            return None
        if isinstance(plan, Mapping):
            try:
                plan = MarketPlan.model_validate(plan)
            except ValidationError:
                return truncate(dict(plan), self.cycle_cfg.ai.tool_result_max_bytes)
        return {
            "age_s": int(now - plan.ts),
            "regimes": {s: {"regime": c.regime, "confidence": c.confidence} for s, c in sorted(plan.regimes.items())},
            "enabled_strategies": list(plan.enabled_strategies),
            "s1_margin_min_pct": str(plan.s1_margin_min_pct),
            "size_multiplier": str(plan.size_multiplier),
            "usdt_target_share": str(plan.usdt_target_share),
            "risk_flags": list(plan.risk_flags),
            "notes": plan.notes,
        }

    @staticmethod
    def _event_json(event: Any) -> Any:
        if isinstance(event, BaseModel):
            return event.model_dump(mode="json")
        if isinstance(event, Mapping):
            return dict(event)
        return str(event)

    # ------------------------------------------------------------ run

    @property
    def _replaying(self) -> bool:
        return getattr(self.llm, "mode", "") == "replay"

    async def run(self, state: Mapping[str, Any] | None = None, reason: str = "scheduled") -> MarketPlan | None:
        """One analyst cycle. Returns the clamped plan, or None (reason in ``last_error``)."""
        now = self.clock.now()
        self.last_run_ts = now
        self.last_reason = reason
        self.last_error = ""
        if reason.startswith("move:"):
            self.move_trigger_ts[reason.split(":", 1)[1]] = now
        cfg = self.cfg  # one config for the whole cycle (see ``cycle_cfg``)
        if not cfg.ai.enabled:
            self.last_error = "ai.enabled is false"
            return None
        if self.llm is None:
            self.last_error = "llm not configured"
            return None
        if not self._take_budget(now):
            return None
        self._cycle_cfg = cfg
        try:
            return await self._run(dict(state or {}), reason, now)
        except CacheMissError:
            raise
        except Exception as exc:  # noqa: BLE001 - the slow loop degrades to rules-only, never crashes
            self.last_error = f"analyst failed: {type(exc).__name__}: {str(exc)[:200]}"
            log.warning("analyst: %s", self.last_error)
            return None
        finally:
            self._cycle_cfg = None

    def _take_budget(self, now: float) -> bool:
        """Count this cycle against the UTC day's ``daily_cycle_budget`` (never in replay: nothing
        reaches a provider). False (``last_error`` set) when the day's budget is used up."""
        limit = self.daily_cycle_budget
        if limit is None or self._replaying:
            return True
        day = utc_day(now)
        used = self._cycles_by_day.get(day, 0)
        if used >= limit:
            self.last_error = f"daily analyst cycle budget exhausted ({limit} cycles on {day} UTC)"
            log.warning("analyst: %s", self.last_error)
            return False
        self._cycles_by_day = {day: used + 1}  # only today's count is kept
        return True

    async def _run(self, state: Json, reason: str, now: float) -> MarketPlan | None:
        assert self.llm is not None
        run = _Run(messages=[
            {"role": "system", "content": self.system_prompt(now, state)},
            {"role": "user", "content": self.user_message(state, reason, now)},
        ], state=state, reason=reason, now=now)
        self.last_messages = run.messages
        await self._offer_tools(run, now)
        max_rounds = self.cycle_cfg.ai.analyst_max_tool_rounds

        while True:
            tools_now = run.offered if run.offered and run.rounds < max_rounds else None
            if run.offered and tools_now is None:
                run.messages.append({"role": "user", "content": FORCE_FINAL.format(rounds=run.rounds)})
            resp = await self._chat(run, tools_now)
            if resp is None:
                return None
            if tools_now and resp.tool_calls:
                run.rounds += 1
                await self._tool_round(run, resp, now)
                continue
            break

        out, err = self._parse(run.responses[-1].text)
        if out is None:
            run.retried = True
            run.messages.extend([
                {"role": "assistant", "content": run.responses[-1].text},
                {"role": "user", "content": FIX_JSON.format(err=err, keys=", ".join(PLAN_KEYS),
                                                            symbols=", ".join(REQUIRED_SYMBOLS))},
            ])
            resp = await self._chat(run, None)
            if resp is None:
                return None
            out, err = self._parse(resp.text)
            if out is None:
                self.last_error = f"llm_invalid: {err}"
                log.warning("analyst: invalid plan JSON twice: %s", err[:200])
                return None
        return self._finish(run, out, reason)

    async def _chat(self, run: _Run, tools: list[Json] | None) -> LLMResponse | None:
        assert self.llm is not None
        resp = await self.llm.chat(
            list(run.messages), tools, role="analyst", temperature=self.cycle_cfg.ai.temperature,
            max_tokens=self.cycle_cfg.llm.analyst.max_tokens, json_mode=True, timeout=self.cycle_cfg.ai.analyst_timeout_s,
        )
        if resp is None:
            self.last_error = f"llm unavailable: {self.llm.last_error or 'no provider answered'}"
            return None
        run.responses.append(resp)
        return resp

    @staticmethod
    def _parse(text: str) -> tuple[PlanOutput | None, str]:
        """Strict ``PlanOutput`` validation (nothing is repaired). The error text names the usual
        structural slip — top-level keys written inside ``regimes`` — so the one retry can fix it."""
        try:
            data = extract_json(text)
        except ValueError as exc:
            return None, f"not one valid JSON object ({str(exc)[:300]}); check that every {{ is closed"
        try:
            return PlanOutput.model_validate(data), ""
        except ValidationError as exc:
            hint = ""
            regimes = data.get("regimes")
            if isinstance(regimes, Mapping):
                misplaced = sorted(k for k in regimes if k in PLAN_KEYS)
                if misplaced:
                    hint = (f"the keys {', '.join(misplaced)} are inside `regimes`: close `regimes` after "
                            f"{REQUIRED_SYMBOLS[-1]} and put them at the top level. ")
            return None, (hint + str(exc))[:800]

    def _finish(self, run: _Run, out: PlanOutput, reason: str) -> MarketPlan:
        final = run.responses[-1]
        meta = {
            "model": final.model,
            "prompt_hash": run.responses[0].cache_key,
            "cached": all(r.cached for r in run.responses),
            "tools_used": sorted(run.tools_used),
            "tools_source": run.source,
            "latency_ms": sum(int(r.latency_ms or 0) for r in run.responses),
        }
        cfg = self.cycle_cfg
        plan = clamp_plan(out, cfg, configured_enabled(cfg), self.clock.now(), meta)
        if plan.clamped:
            log.info("analyst: plan values clamped: %s", _dumps(plan.clamped))
        record: Json = {
            "prompt_hash": plan.prompt_hash,
            "model": plan.model,
            "cached": plan.cached,
            "tools_used": list(plan.tools_used),
            "tools_source": plan.tools_source,
            "latency_ms": plan.latency_ms,
            "notes": plan.notes,
            "clamped": dict(plan.clamped),
            "reason": reason,
            "tool_rounds": run.rounds,
            "llm_calls": len(run.responses),
            # which route answered each call (a cycle may fail over part-way): replay needs every one
            "calls": [{"provider": r.provider, "model": r.model, "cache_key": r.cache_key} for r in run.responses],
            "json_retry": run.retried,
            "usage": _usage_total(run.responses),
            "config_hash": cfg.config_hash,  # the cycle's config, whatever the engine holds by now
        }
        self.last_plan = plan
        self.last_record = record
        if not self._replaying and plan.prompt_hash:
            self._tape_put(cycle_tape_key(plan.prompt_hash),
                           {"now": run.now, "reason": run.reason, "state": self._jsonable_state(run.state),
                            "config_hash": cfg.config_hash, "config": cfg.model_dump(mode="json")})
        if self.on_plan is not None:
            try:
                self.on_plan(plan, record)
            except Exception:  # the plan is valid; a persistence problem must not drop it
                log.exception("analyst: on_plan callback failed")
        return plan

    def recorded_cycle(self, prompt_hash: str) -> Json | None:
        """The taped input of the cycle whose plan has ``prompt_hash``: ``{now, reason, state,
        config_hash, config}`` (``config``: the cycle's whole config, JSON; older tapes lack it;
        None when the cycle was never recorded)."""
        return self.tape.get(cycle_tape_key(prompt_hash)) if self.tape is not None else None

    async def replay_cycle(self, prompt_hash: str) -> MarketPlan | None:
        """Re-run the recorded cycle whose plan has ``prompt_hash``: its input (``now``, ``reason``,
        ``state``) comes from the tape, every model answer from the cache and every tool result from
        the tape, so with an LLM client in ``replay`` mode (strict cache, no provider needed) it
        makes zero network calls. The clock must be settable (``SimClock``): it is set to the
        recorded ``now``. ``CacheMissError`` when the cycle or one of its answers was never
        recorded — e.g. because ``self.cfg`` differs from the recorded config in a value the system
        prompt shows (compare ``recorded_cycle(...)["config_hash"]``)."""
        entry = self.recorded_cycle(prompt_hash)
        if entry is None:
            raise CacheMissError(f"replay: no recorded analyst cycle for prompt_hash {prompt_hash}")
        set_clock = getattr(self.clock, "set", None)
        if not callable(set_clock):
            raise TypeError("replay_cycle needs a settable clock (SimClock)")
        set_clock(float(entry["now"]))
        return await self.run(entry.get("state") or {}, str(entry.get("reason") or "replay"))

    # ------------------------------------------------------------ tools

    async def _offer_tools(self, run: _Run, now: float) -> None:
        """Pick the tool set: none / internal + MCP read-only / internal + ``*_local``."""
        if not self.cycle_cfg.ai.analyst_tools_enabled or self.cycle_cfg.ai.analyst_max_tool_rounds <= 0:
            return
        local = list(INTERNAL_TOOL_SCHEMAS) + list(LOCAL_TOOL_SCHEMAS)
        key = _tape_key("mcp-tools", canonical_json(run.messages[:2]))
        if self._replaying:  # never touch the MCP server: offer what the recorded run offered
            hit = self.tape.get(key) if self.tape is not None else None
            if hit is not None and hit.get("tools_source") == "mcp":
                run.offered, run.source = list(hit["schemas"]), "mcp"
                run.mcp_names = set(hit.get("mcp_tools") or [])
            else:
                run.offered, run.source = local, "local"
            return
        mcp_schemas = await self._mcp_tool_schemas(now)
        if mcp_schemas:
            run.offered = list(INTERNAL_TOOL_SCHEMAS) + mcp_schemas
            run.source = "mcp"
            run.mcp_names = {s["name"] for s in mcp_schemas}
        else:
            run.offered, run.source = local, "local"
        if self.mcp is not None:  # the choice depended on MCP uptime: record it for replay
            self._tape_put(key, {"tools_source": run.source, "schemas": run.offered,
                                 "mcp_tools": sorted(run.mcp_names)})

    async def _mcp_tool_schemas(self, now: float) -> list[Json] | None:
        if self.mcp is None:
            return None
        if self._mcp_schemas is not None:
            return self._mcp_schemas
        if self._mcp_retry_at is not None and now < self._mcp_retry_at:
            return None
        try:
            await self.mcp.initialize()
            await self.mcp.list_tools()
            schemas = [mcp_tool_schema(t) for t in self.mcp.allowed_tools()]
        except Exception as exc:  # noqa: BLE001 - MCP is optional; local tools cover it
            self._mcp_down(now, f"initialize failed: {type(exc).__name__}: {str(exc)[:200]}")
            return None
        if not schemas:
            self._mcp_down(now, "the server lists none of the allowed read-only tools")
            return None
        self.mcp_error = ""
        self._mcp_schemas = schemas
        return schemas

    def _mcp_down(self, now: float, why: str) -> None:
        self.mcp_error = why
        self._mcp_schemas = None
        self._mcp_retry_at = now + MCP_RETRY_S
        log.warning("analyst: Wallex MCP unavailable (%s); using *_local tools", why)

    async def _tool_round(self, run: _Run, resp: LLMResponse, now: float) -> None:
        calls = list(resp.tool_calls)
        run.messages.append({
            "role": "assistant",
            "content": resp.text or None,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.name, "arguments": _dumps(tc.arguments)}} for tc in calls],
        })
        mcp_failed = False
        for i, tc in enumerate(calls):
            if i >= MAX_CALLS_PER_ROUND:
                content = _dumps({"error": f"at most {MAX_CALLS_PER_ROUND} tool calls per turn; call skipped"})
            else:
                content, failed = await self._execute(run, resp, tc)
                mcp_failed = mcp_failed or failed
            run.messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content})
        if mcp_failed:
            run.offered = list(INTERNAL_TOOL_SCHEMAS) + list(LOCAL_TOOL_SCHEMAS)
            run.source = "local"
            run.mcp_names = set()
            if not self._replaying:
                self._mcp_down(now, self.mcp_error or "tool call failed")

    async def _execute(self, run: _Run, resp: LLMResponse, tc: ToolCall) -> tuple[str, bool]:
        """Run one tool call. Returns (result as canonical JSON text, MCP failed)."""
        if tc.name not in run.offered_names:
            return _dumps({"error": f"unknown tool {tc.name!r}"}), False
        run.tools_used.add(tc.name)
        if tc.name in run.mcp_names:
            return await self._call_mcp(resp, tc)
        return self._call_internal(resp, tc), False

    def _call_internal(self, resp: LLMResponse, tc: ToolCall) -> str:
        """An internal / ``*_local`` tool: recorded on the tape (shadow/live), read back in replay."""
        key = _tape_key("tool-call", resp.cache_key, tc.id, tc.name, canonical_json(tc.arguments))
        if self._replaying:
            hit = self.tape.get(key) if self.tape is not None else None
            if hit is not None:
                return str(hit["content"])
        text = _dumps(self.tools.call(tc.name, dict(tc.arguments)))
        if not self._replaying:
            self._tape_put(key, {"tool": tc.name, "content": text})
        return text

    @staticmethod
    def _jsonable_state(state: Mapping[str, Any]) -> Any:
        try:
            return to_jsonable(dict(state))
        except TypeError:  # e.g. a FeatureSnapshot object (tests): not replayable from the tape
            return {"unrecorded": sorted(str(k) for k in state)}

    async def _call_mcp(self, resp: LLMResponse, tc: ToolCall) -> tuple[str, bool]:
        key = _tape_key("mcp-call", resp.cache_key, tc.id, tc.name, canonical_json(tc.arguments))
        if self._replaying:
            hit = self.tape.get(key) if self.tape is not None else None
            if hit is None:
                return _dumps({"error": "no recorded Wallex MCP result for this call (replay)"}), False
            return str(hit["content"]), bool(hit.get("mcp_failed"))
        failed = False
        content: Any
        if self.mcp is None:
            failed, content = True, {"error": "Wallex MCP not configured; use the *_local tools instead"}
        else:
            try:
                result = await self.mcp.call_tool(tc.name, dict(tc.arguments))
                payload = tool_payload(result)
                if result.get("isError"):
                    payload = {"error": payload}
                content = truncate_result(payload, self.cycle_cfg.ai.tool_result_max_bytes)
            except MCPToolNotAllowed as exc:
                content = {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - an MCP outage must not abort the cycle
                failed = True
                self.mcp_error = mask_secrets(f"{tc.name}: {type(exc).__name__}: {str(exc)[:200]}", self.secrets)
                content = {"error": f"Wallex MCP unavailable ({type(exc).__name__}); use the *_local tools instead"}
        text = mask_secrets(_dumps(content), self.secrets)  # before the tape, the cache and the model see it
        self._tape_put(key, {"tool": tc.name, "content": text, "mcp_failed": failed})
        return text, failed

    def _tape_put(self, key: str, entry: Json) -> None:
        if self.tape is None:
            return
        try:
            self.tape.put(key, entry)
        except OSError as exc:
            log.warning("analyst: could not record MCP tape entry: %s", exc)
