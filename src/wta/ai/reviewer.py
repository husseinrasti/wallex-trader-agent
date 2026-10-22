"""Post-trade Reviewer (PRD-04 §7): an hourly journal entry plus bounded, versioned config changes.

Flow of ``PostTradeReviewer.run``::

    build_input(...)            compact, sorted JSON of the last hour: decisions with their merged
                                outcomes, PnL by strategy, fee usage vs budget, vetoes by risk code,
                                plan history, health incidents
    render prompts/reviewer.md  editable paths + bounds + current values, max changes, JSON schema
    LLMClient.structured        strict ``ReviewOutput`` (extra="forbid"), temperature 0, cached and
                                replayable, one "fix the JSON" retry, then None
    ConfigManager.apply         only listed paths, within bounds, <= max_changes_per_review, cooldown;
                                risk.* never passes (the reviewer pre-filters it as well)
    on_event("config_change", "reviewer", {old_hash, new_hash, diff, rejected, overlay_path, source})
    logs/journal.md             one appended section per review: UTC ISO header, config hash
                                before -> after, the model's journal, alerts, applied changes and
                                every rejected/ignored suggestion with the reason

An unavailable or invalid LLM never raises: the journal gets ``reviewer skipped: <reason>`` and the
config is untouched. A strict-replay cache miss (``CacheMissError``) does raise: it is a determinism
failure, not an outage. ``run_final`` writes the end-of-challenge summary and never changes config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from wta.ai.cache import CacheMissError
from wta.ai.config_manager import FORBIDDEN_PREFIXES, ChangeResult, ConfigManager
from wta.ai.llm_client import LLMClient, LLMResponse, Route
from wta.ai.tools import to_jsonable, truncate
from wta.config import Config, canonical_json, config_hash, get_path, mask_secrets
from wta.logging_.decision_log import merge_decisions
from wta.utils.timeutil import Clock, iso, parse_iso, utc_day

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "reviewer.md"
REVIEWER_TEMPERATURE = 0.0
EventSink = Callable[[str, str, dict[str, Any]], None]

MAX_DECISIONS = 40  # most recent decisions shown to the model (aggregates always use all of them)
MAX_PLANS = 12  # one hour of 5-minute plans
MAX_REASONS = 10
MAX_EVENTS = 20
MAX_ALERTS = 10
MAX_JOURNAL_CHARS = 6000
PORTFOLIO_MAX_BYTES = 2048
CLIP = 160

OUTCOME_KEYS = ("status", "done_reason", "exit_reason", "pnl_tmn", "pnl_pct", "pnl_complete", "hold_s",
                "filled_qty", "fee_paid", "fee_asset")

JOURNAL_HEADER = ("# Trading journal\n\nWritten by the Post-trade Reviewer (PRD-04 §7), one section per review, "
                  "newest at the bottom.\n\n")

FINAL_ADDENDUM = """

## Final review (end of the challenge)
This is the last review: the challenge is over and the configuration is frozen. The input covers the
whole challenge instead of the last hour. Write `journal` as the final summary (10–25 lines): the
result in TMN and %, what each strategy contributed, fees paid against the budget, notable vetoes
and incidents, and what you would change next time. `param_suggestions` must be an empty list —
nothing is applied any more.
"""


# ---------------------------------------------------------------- output schema


class ParamSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=200)
    value: bool | int | float | str
    rationale: str = ""


class ReviewOutput(BaseModel):
    """What the model must return. Unknown keys are a validation error (one retry, then None)."""

    model_config = ConfigDict(extra="forbid")

    journal: str
    param_suggestions: list[ParamSuggestion] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)


@dataclass
class ReviewResult:
    journal: str  # the model's journal text, or "reviewer skipped: <reason>"
    suggestions: list[dict[str, Any]] = field(default_factory=list)  # raw model suggestions
    alerts: list[str] = field(default_factory=list)
    change: ChangeResult | None = None  # None when skipped and for the final review
    raw_ok: bool = False  # the model returned valid JSON
    entry: str = ""  # the markdown section appended to the journal file
    skipped_reason: str = ""
    model: str = ""
    cached: bool = False
    final: bool = False


# ---------------------------------------------------------------- helpers


_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render_template(template: str, values: Mapping[str, Any]) -> str:
    """Replace only known ``{name}`` keys. JSON braces and unknown names stay verbatim.

    (Never ``str.format``: the reviewer template contains literal JSON braces.) Replacement text is
    not re-scanned, so a value containing ``{x}`` cannot inject another substitution.
    """

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)

    return _PLACEHOLDER.sub(sub, template)


def reviewer_route(cfg: Config) -> Route:
    """The reviewer's configured provider/model (``configs/llm.yaml -> reviewer``)."""
    return Route(cfg.llm.reviewer.provider, cfg.llm.reviewer.model or "")


def editable_paths(cfg: Config, pinned: Iterable[str] = ()) -> list[str]:
    """Reviewer-editable paths, sorted; forbidden prefixes (risk.* etc.) and the operator's pinned
    keys (``--set`` / ``--overlay``) are never offered."""
    skip = frozenset(pinned)
    return sorted(p for p in cfg.llm.reviewer.editable if not p.startswith(FORBIDDEN_PREFIXES) and p not in skip)


def _num_str(v: Any) -> str:
    d = _dec(v)
    return format(d.normalize(), "f") if d is not None else str(v)


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, Decimal)):
        return _num_str(v)
    return json.dumps(v, sort_keys=True, default=str)


def _is_num(b: Any) -> bool:
    return isinstance(b, (int, float)) and not isinstance(b, bool)


def _fmt_bounds(bounds: list[Any]) -> str:
    if len(bounds) == 2 and all(isinstance(b, bool) for b in bounds):
        return "true or false"
    if len(bounds) == 2 and all(_is_num(b) for b in bounds):
        return f"[{_fmt_value(bounds[0])}, {_fmt_value(bounds[1])}] (inclusive)"
    return "one of " + ", ".join(_fmt_value(b) for b in bounds)


def _value_types(all_bounds: Iterable[list[Any]]) -> list[str]:
    types: set[str] = set()
    for bounds in all_bounds:
        for b in bounds:
            if isinstance(b, bool):
                types.add("boolean")
            elif _is_num(b):
                types.add("number")
            elif isinstance(b, str):
                types.add("string")
    return sorted(types) or ["number"]


def review_schema(paths: list[str], value_types: list[str], max_changes: int) -> dict[str, Any]:
    """JSON schema shown to the model (strict: additionalProperties=false everywhere)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["journal", "param_suggestions", "alerts"],
        "properties": {
            "journal": {"type": "string", "description": "markdown, 5-15 lines: what worked, what didn't, anomalies"},
            "param_suggestions": {
                "type": "array",
                "maxItems": max_changes,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "value", "rationale"],
                    "properties": {
                        "path": {"type": "string", "enum": paths},
                        "value": {"type": value_types},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "alerts": {"type": "array", "items": {"type": "string"}},
        },
    }


def _dec(x: Any) -> Decimal | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _q(d: Decimal | None, exp: str) -> str | None:
    if d is None:
        return None
    return format(d.quantize(Decimal(exp)).normalize(), "f")


def _ts(x: Any) -> float | None:
    """Unix seconds from a float/int or an ISO-8601 string; anything else -> None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str) and x.strip():
        try:
            return parse_iso(x)
        except ValueError:
            try:
                return float(x)
            except ValueError:
                return None
    return None


def _sort_ts(ts: float | None) -> float:
    return ts if ts is not None else float("-inf")


def _iso_str(x: Any) -> str | None:
    ts = _ts(x)
    return iso(ts) if ts is not None else None


def _map(x: Any) -> dict[str, Any]:
    return dict(x) if isinstance(x, Mapping) else {}


def _one_line(text: Any, limit: int = CLIP) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _failed_codes(risk: Mapping[str, Any]) -> list[str]:
    codes = {str(c.get("code")) for c in risk.get("checks") or [] if isinstance(c, Mapping) and c.get("passed") is False}
    codes.update(str(c) for c in risk.get("failed_codes") or [])
    return sorted(c for c in codes if c and c != "None")


def _check_detail(risk: Mapping[str, Any], code: str) -> str:
    for c in risk.get("checks") or []:
        if isinstance(c, Mapping) and str(c.get("code")) == code and c.get("detail"):
            return str(c["detail"])
    return ""


def _outcome_ts(outcome: Mapping[str, Any] | None) -> float | None:
    if not outcome:
        return None
    for key in ("closed_ts", "done_ts", "ts"):
        ts = _ts(outcome.get(key))
        if ts is not None:
            return ts
    return None


def _compact_decision(rec: Mapping[str, Any]) -> dict[str, Any]:
    intent, risk, order, outcome = (_map(rec.get(k)) for k in ("intent", "risk", "order", "outcome"))
    out: dict[str, Any] = {
        "id": str(rec.get("decision_id", "")),
        "ts": _iso_str(rec.get("ts")),
        "kind": str(rec.get("kind") or ""),
        "strategy": str(rec.get("strategy") or intent.get("strategy") or outcome.get("strategy") or ""),
        "symbol": str(rec.get("symbol") or intent.get("symbol") or outcome.get("symbol") or ""),
    }
    for key in ("side", "purpose", "order_type"):
        if intent.get(key):
            out[key] = intent[key]
    reason = intent.get("reason")
    if reason:
        out["reason"] = _one_line(reason)
    if intent.get("expected_edge_pct") is not None:
        out["edge_pct"] = str(intent["expected_edge_pct"])
    if "approved" in risk:
        out["approved"] = bool(risk["approved"])
    failed = _failed_codes(risk)
    if failed:
        out["veto_codes"] = failed
    if "accepted" in order:
        out["accepted"] = bool(order["accepted"])
    status = _map(order.get("response")).get("status")
    if status:
        out["order_status"] = status
    notes = rec.get("notes")
    if notes and notes != reason:
        out["notes"] = _one_line(notes)
    if outcome:
        out["outcome"] = {k: outcome[k] for k in OUTCOME_KEYS if outcome.get(k) is not None}
    return out


def _compact_plan(snap: Mapping[str, Any], ts: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"ts": iso(ts) if ts is not None else None}
    regimes: dict[str, str] = {}
    for sym, call in sorted(_map(snap.get("regimes")).items()):
        if isinstance(call, Mapping):
            conf = _dec(call.get("confidence"))
            regimes[sym] = f"{call.get('regime')}@{_q(conf, '0.01') if conf is not None else '?'}"
        else:
            regimes[sym] = str(call)
    out["regimes"] = regimes
    for key in ("size_multiplier", "usdt_target_share", "s1_margin_min_pct"):
        if snap.get(key) is not None:
            out[key] = _num_str(snap[key])
    for key in ("enabled_strategies", "risk_flags"):
        if snap.get(key):
            out[key] = sorted(str(x) for x in snap[key])
    if snap.get("clamped"):
        out["clamped"] = sorted(str(k) for k in _map(snap["clamped"]))
    if snap.get("tools_source"):
        out["tools_source"] = snap["tools_source"]
    if snap.get("notes"):
        out["notes"] = _one_line(snap["notes"], 240)
    return out


def _same_value(tree: Mapping[str, Any], path: str, value: Any) -> bool:
    """True when ``value`` equals the current config value (numerically for numbers: 300 == 300.0)."""
    try:
        old = get_path(dict(tree), path)
    except (KeyError, TypeError):
        return False
    if isinstance(old, bool) or isinstance(value, bool):
        return isinstance(old, bool) and isinstance(value, bool) and old == value
    a, b = _dec(old), _dec(value)
    if a is not None and b is not None:
        return a == b
    return str(old) == str(value)


def _demote_headings(text: str) -> str:
    """Model markdown lives inside our ``##`` section: demote its ``#``/``##`` headings to ``###``."""
    return re.sub(r"(?m)^#{1,2}(?=\s)", "###", text)


# ---------------------------------------------------------------- reviewer


class PostTradeReviewer:
    """Hourly post-trade review: journal + bounded config suggestions (never touches risk.*)."""

    def __init__(
        self,
        cfg: Config,
        llm: LLMClient | None,
        config_manager: ConfigManager,
        clock: Clock,
        journal_path: Path = Path("logs/journal.md"),
        on_event: EventSink | None = None,
        *,
        route: Route | None = None,
        secrets: list[str] | None = None,
        daily_budget: int | None = None,
    ) -> None:
        """``route=None`` uses the client's primary route with its fallback; pass
        ``reviewer_route(cfg)`` to pin the reviewer model (no fallback then). ``secrets`` are masked
        in everything the reviewer writes or logs (defence in depth; prompts contain none).
        ``daily_budget``: hourly reviews per UTC day that may reach the model
        (``ai.max_reviewer_calls_per_day``; None: unlimited); the final review always runs."""
        self.cfg = cfg
        self.llm = llm
        self.config_manager = config_manager
        self.clock = clock
        self.journal_path = Path(journal_path)
        self.on_event = on_event
        self.route = route
        self.secrets = [s for s in (secrets or []) if s]
        self.template = PROMPT_PATH.read_text(encoding="utf-8")
        self.last_result: ReviewResult | None = None
        self._lock = asyncio.Lock()
        self.daily_budget = daily_budget
        self._reviews_by_day: dict[str, int] = {}

    @property
    def current_cfg(self) -> Config:
        """The live config (the ConfigManager's, which moves on after accepted changes)."""
        return self.config_manager.cfg

    # ------------------------------------------------------------ input

    def build_input(
        self,
        decisions: list[dict[str, Any]],
        plans: list[dict[str, Any]],
        health: list[dict[str, Any]],
        portfolio_summary: dict[str, Any],
        window_s: float = 3600,
    ) -> dict[str, Any]:
        """Compact, deterministic review input for the window ``[now - window_s, now]``.

        ``decisions``: raw ``decisions.jsonl`` lines (outcome updates included; merged here).
        ``plans``: MarketPlan dumps or ``kind="plan"`` decision records (``plan_snapshot``).
        ``health``: HealthRecord dumps and/or event records (``{ts, kind, reason}``).
        Every list in the result is sorted; equal inputs always give byte-identical JSON.
        """
        now = self.clock.now()
        start = now - float(window_s)
        cfg = self.current_cfg

        def inside(ts: float | None) -> bool:
            return ts is not None and start <= ts <= now

        # -- decisions: stable sort by ts (keeps log order for ties), then merge outcomes
        raw = sorted((to_jsonable(d) for d in decisions), key=lambda r: _sort_ts(_ts(_map(r).get("ts"))))
        merged = merge_decisions(r for r in raw if isinstance(r, Mapping))
        rows: list[dict[str, Any]] = list(merged.values())
        for upd in merged.orphans:  # outcome of an entry made before the given records
            outcome = _map(upd.get("outcome"))
            rows.append({"decision_id": upd.get("decision_id"), "kind": "outcome", "ts": upd.get("ts"),
                         "strategy": outcome.get("strategy"), "symbol": outcome.get("symbol"),
                         "outcome": outcome})
        rows.sort(key=lambda r: (_sort_ts(_ts(r.get("ts"))), str(r.get("decision_id")), canonical_json(r)))

        counts: dict[str, int] = {}
        shown: list[dict[str, Any]] = []
        pnl: dict[str, dict[str, Any]] = {}
        fees_window: dict[str, Decimal] = {}
        vetoes: dict[str, dict[str, Any]] = {}
        for rec in rows:
            kind = str(rec.get("kind") or "")
            ts = _ts(rec.get("ts"))
            outcome = _map(rec.get("outcome"))
            out_ts = _outcome_ts(outcome)
            undated = ts is None and out_ts is None
            decided_in = inside(ts) or undated
            if not (decided_in or inside(out_ts)):
                continue
            counts[kind] = counts.get(kind, 0) + 1
            if kind in ("plan", "no_trade"):
                continue
            shown.append(_compact_decision(rec))
            risk = _map(rec.get("risk"))
            if decided_in and (kind == "veto" or risk.get("approved") is False):
                for code in _failed_codes(risk) or ["unspecified"]:
                    v = vetoes.setdefault(code, {"count": 0, "example": ""})
                    v["count"] += 1
                    if not v["example"]:
                        example = _check_detail(risk, code) or risk.get("veto_reason") or rec.get("notes") or ""
                        v["example"] = _one_line(f"{rec.get('strategy', '')} {rec.get('symbol', '')}: {example}", 200)
            if outcome and (inside(out_ts) or out_ts is None):
                pnl_tmn = _dec(outcome.get("pnl_tmn"))
                if pnl_tmn is not None:
                    strat = str(outcome.get("strategy") or rec.get("strategy") or "?")
                    agg = pnl.setdefault(strat, {"trades": 0, "wins": 0, "losses": 0, "pnl_tmn": Decimal(0),
                                                 "pnl_pct_sum": Decimal(0), "incomplete": 0})
                    agg["trades"] += 1
                    if pnl_tmn > 0:
                        agg["wins"] += 1
                    elif pnl_tmn < 0:
                        agg["losses"] += 1
                    agg["pnl_tmn"] += pnl_tmn
                    agg["pnl_pct_sum"] += _dec(outcome.get("pnl_pct")) or Decimal(0)
                    if outcome.get("pnl_complete") is False:
                        agg["incomplete"] += 1
                fee = _dec(outcome.get("fee_paid"))
                if fee is not None and fee > 0:
                    asset = str(outcome.get("fee_asset") or "?")
                    fees_window[asset] = fees_window.get(asset, Decimal(0)) + fee

        pnl_out: dict[str, dict[str, Any]] = {}
        for strat in sorted(pnl):
            a = pnl[strat]
            pnl_out[strat] = {"trades": a["trades"], "wins": a["wins"], "losses": a["losses"],
                              "pnl_tmn": _q(a["pnl_tmn"], "0.01"),
                              "avg_pnl_pct": _q(a["pnl_pct_sum"] / a["trades"], "0.000001")}
            if a["incomplete"]:
                pnl_out[strat]["pnl_incomplete"] = a["incomplete"]
        pnl_total = sum((pnl[s]["pnl_tmn"] for s in pnl), Decimal(0))

        # -- fees vs budget
        ps = _map(to_jsonable(portfolio_summary or {}))
        paid = next((d for d in (_dec(ps.get(k)) for k in ("fees_paid_tmn", "fees_paid_tmn_total")) if d is not None),
                    None)
        initial = _dec(ps.get("initial_equity_tmn")) or cfg.engine.initial_equity_tmn
        budget = cfg.risk.fee_budget_pct * initial
        used = paid / budget if paid is not None and budget > 0 else _dec(ps.get("fee_budget_used_pct"))
        fees = {
            "budget_pct_of_initial": _num_str(cfg.risk.fee_budget_pct),
            "budget_tmn": _q(budget, "0.01"),
            "paid_tmn_total": _q(paid, "0.01"),
            "used_pct_of_budget": _q(used, "0.000001"),
            "window_by_asset": {a: _num_str(fees_window[a]) for a in sorted(fees_window)},
        }

        # -- plan history
        plan_rows: list[tuple[float | None, dict[str, Any]]] = []
        for p in plans:
            pm = _map(to_jsonable(p))
            snap = _map(pm.get("plan_snapshot")) or pm
            ts = _ts(snap.get("ts"))
            if ts is None:
                ts = _ts(pm.get("ts"))
            if ts is not None and not inside(ts):
                continue
            plan_rows.append((ts, _compact_plan(snap, ts)))
        plan_rows.sort(key=lambda x: (_sort_ts(x[0]), canonical_json(x[1])))

        # -- health incidents
        h_rows = sorted((_map(to_jsonable(h)) for h in health),
                        key=lambda h: (_sort_ts(_ts(h.get("ts"))), canonical_json(h)))
        hc = {"records": 0, "paused": 0, "killed": 0, "stale": 0, "api_error_records": 0, "llm_down": 0}
        api_max = 0
        reasons: dict[str, list[Any]] = {}
        events: list[dict[str, Any]] = []
        for h in h_rows:
            ts = _ts(h.get("ts"))
            if ts is not None and not inside(ts):
                continue
            if "kind" in h and not any(k in h for k in ("paused", "killed", "rest_ok", "ws_ok")):
                events.append({"ts": _iso_str(h.get("ts")), "kind": str(h["kind"]),
                               "reason": _one_line(h.get("reason") or "")})
                continue
            hc["records"] += 1
            rs = [str(r) for r in h.get("reasons") or []]
            hc["paused"] += 1 if h.get("paused") else 0
            hc["killed"] += 1 if h.get("killed") else 0
            # on the synthetic feed rest_ok/ws_ok are always False (neither source runs): only the
            # engine's own reasons say whether its data were stale (a real review misread that)
            synthetic = _map(h.get("extra")).get("feed") == "synthetic"
            stale = any("stale" in r.lower() or "no fresh data" in r.lower() for r in rs) or (
                not synthetic and h.get("rest_ok") is False and h.get("ws_ok") is False)
            hc["stale"] += 1 if stale else 0
            errs = _dec(h.get("api_errors_1m")) or Decimal(0)
            if errs > 0:
                hc["api_error_records"] += 1
                api_max = max(api_max, int(errs))
            hc["llm_down"] += 1 if h.get("llm_ok") is False else 0
            for r in rs:
                key = _one_line(r)
                if key not in reasons:
                    reasons[key] = [0, ts]
                reasons[key][0] += 1
        reason_list = sorted(({"reason": r, "count": c, "first": iso(t) if t is not None else None}
                              for r, (c, t) in reasons.items()),
                             key=lambda x: (_sort_ts(_ts(x["first"])), x["reason"]))
        incidents = {**hc, "api_errors_max_1m": api_max, "reasons": reason_list[:MAX_REASONS],
                     "events": events[-MAX_EVENTS:]}

        result = {
            "window": {"from": iso(start), "to": iso(now), "seconds": float(window_s)},
            "config_hash": config_hash(cfg),
            "portfolio": truncate(ps, PORTFOLIO_MAX_BYTES),
            "decision_counts": dict(sorted(counts.items())),
            "decisions": shown[-MAX_DECISIONS:],
            "decisions_omitted": max(0, len(shown) - MAX_DECISIONS),
            "orphan_outcomes": len(merged.orphans),
            "pnl_by_strategy": pnl_out,
            "pnl_tmn_window_total": _q(pnl_total, "0.01"),
            "fees": fees,
            "vetoes_total": sum(v["count"] for v in vetoes.values()),
            "vetoes_by_code": {c: vetoes[c] for c in sorted(vetoes)},
            "plans_total": len(plan_rows),
            "plans": [p for _, p in plan_rows][-MAX_PLANS:],
            "health": incidents,
        }
        return to_jsonable(result)

    # ------------------------------------------------------------ prompt

    def system_prompt(self, final: bool = False) -> str:
        cfg = self.current_cfg
        rc = cfg.llm.reviewer
        if final:
            paths: list[str] = []
            editable_text = ("- (none: the challenge is over and the configuration is frozen — "
                             "return an empty `param_suggestions` list)")
            types, max_changes = ["number"], 0
        else:
            paths = editable_paths(cfg, getattr(self.config_manager, "pinned", ()))
            tree = cfg.model_dump(mode="json")
            lines = []
            for p in paths:
                try:
                    current = _fmt_value(get_path(tree, p))
                except (KeyError, TypeError):
                    current = "unknown"
                lines.append(f"- `{p}`: {_fmt_bounds(rc.editable[p])} (current: {current})")
            editable_text = "\n".join(lines) or "- (none configured: return an empty `param_suggestions` list)"
            types, max_changes = _value_types(rc.editable[p] for p in paths), rc.max_changes_per_review
        schema = json.dumps(review_schema(paths, types, max_changes), indent=2)
        prompt = render_template(self.template, {"editable": editable_text, "max_changes": max_changes,
                                                 "schema": schema})
        return prompt + FINAL_ADDENDUM if final else prompt

    def messages(self, review_input: Mapping[str, Any], final: bool = False) -> list[dict[str, Any]]:
        head = "Final review input (whole challenge, JSON):" if final else "Review input (last hour, JSON):"
        body = canonical_json(to_jsonable(dict(review_input))).decode()
        return [
            {"role": "system", "content": self.system_prompt(final)},
            {"role": "user", "content": f"{head}\n{body}\n\nWrite the review. Return only the JSON object."},
        ]

    # ------------------------------------------------------------ run

    async def run(self, review_input: dict[str, Any]) -> ReviewResult:
        """One hourly review: journal + bounded config changes. Never raises on LLM trouble."""
        async with self._lock:
            over = self._over_budget()
            if over:
                now = self.clock.now()
                self.last_result = self._skip(now, config_hash(self.current_cfg), over, False, review_input)
            else:
                self.last_result = await self._run(review_input, final=False)
            return self.last_result

    def _over_budget(self) -> str:
        """Count one review against today's ``daily_budget``; the reason text when it is used up."""
        if self.daily_budget is None or self.llm is None or getattr(self.llm, "mode", "") == "replay":
            return ""
        day = utc_day(self.clock.now())
        used = self._reviews_by_day.get(day, 0)
        if used >= self.daily_budget:
            return f"daily reviewer budget exhausted ({self.daily_budget} reviews on {day} UTC)"
        self._reviews_by_day = {day: used + 1}
        return ""

    async def run_final(self, review_input: dict[str, Any]) -> ReviewResult:
        """End-of-challenge summary section. Never changes the config (suggestions are ignored)."""
        async with self._lock:
            self.last_result = await self._run(review_input, final=True)
            return self.last_result

    def _unavailable(self) -> str:
        if not self.cfg.ai.enabled:
            return "ai disabled (ai.enabled=false)"
        if self.llm is None:
            return "llm not configured"
        return ""

    async def _run(self, review_input: Mapping[str, Any], final: bool) -> ReviewResult:
        now = self.clock.now()
        hash_before = config_hash(self.current_cfg)
        reason = self._unavailable()
        if reason or self.llm is None:
            return self._skip(now, hash_before, reason or "llm not configured", final, review_input)
        cfg = self.current_cfg
        try:
            obj, resp, err = await self.llm.structured(
                self.messages(review_input, final), ReviewOutput, role="reviewer",
                temperature=REVIEWER_TEMPERATURE, max_tokens=cfg.llm.reviewer.max_tokens,
                timeout=cfg.ai.reviewer_timeout_s, route=self.route)
        except CacheMissError:
            raise
        except Exception as exc:  # noqa: BLE001 - any AI failure degrades to "skipped", never crashes the engine
            obj, resp, err = None, None, f"{type(exc).__name__}: {exc}"
        if obj is None:
            return self._skip(now, hash_before, err or "llm unavailable", final, review_input)

        suggestions = [s.model_dump() for s in obj.param_suggestions]
        alerts = [_one_line(a, 300) for a in obj.alerts if a.strip()][:MAX_ALERTS]
        journal = obj.journal.strip() or "(empty journal)"
        if len(journal) > MAX_JOURNAL_CHARS:
            journal = journal[:MAX_JOURNAL_CHARS].rstrip() + "\n…(truncated)"
        journal = mask_secrets(journal, self.secrets)

        change: ChangeResult | None = None
        if final:
            ignored = [{**s, "why": "final review: configuration is frozen"} for s in suggestions]
            hash_after = hash_before
        else:
            change = self._apply(suggestions, now)
            ignored = change.rejected
            hash_after = change.new_hash or hash_before
        entry = self._format_entry(now=now, final=final, hash_before=hash_before, hash_after=hash_after,
                                   body=_demote_headings(journal), alerts=alerts, change=change, ignored=ignored,
                                   resp=resp, review_input=review_input)
        self._append(entry)
        if change is not None and change.accepted:
            self._emit(change)
        log.info("reviewer%s: %d suggestion(s), %d accepted, %d rejected, config_hash %s -> %s",
                 " (final)" if final else "", len(suggestions), len(change.accepted) if change else 0,
                 len(ignored), hash_before, hash_after)
        return ReviewResult(journal=journal, suggestions=suggestions, alerts=alerts, change=change, raw_ok=True,
                            entry=entry, model=resp.model if resp else "", cached=bool(resp and resp.cached),
                            final=final)

    def _apply(self, suggestions: list[dict[str, Any]], now: float) -> ChangeResult:
        """Reviewer-side guard (forbidden prefixes, numeric no-ops) then ``ConfigManager.apply``."""
        tree = self.current_cfg.model_dump(mode="json")
        guarded: list[dict[str, Any]] = []
        passed: list[dict[str, Any]] = []
        for s in suggestions:
            path = str(s.get("path", ""))
            if path.startswith(FORBIDDEN_PREFIXES):
                guarded.append({**s, "why": "forbidden path (risk/execution/markets/llm are never editable)"})
            elif _same_value(tree, path, s.get("value")):
                guarded.append({**s, "why": "no change"})
            else:
                passed.append(s)
        try:
            change = self.config_manager.apply(passed, now, source="reviewer")
        except Exception as exc:  # noqa: BLE001 - a bad suggestion must not crash the review
            log.exception("reviewer: config apply failed")
            h = config_hash(self.current_cfg)
            change = ChangeResult(old_hash=h, new_hash=h,
                                  rejected=[{**s, "why": f"apply failed: {type(exc).__name__}"} for s in passed])
        change.rejected = guarded + change.rejected
        if change.accepted and change.config is not None:
            self.cfg = change.config
        return change

    def _emit(self, change: ChangeResult) -> None:
        if self.on_event is None:
            return
        data = {"old_hash": change.old_hash, "new_hash": change.new_hash, "diff": change.accepted,
                "rejected": change.rejected, "overlay_path": change.overlay_path, "source": "reviewer"}
        try:
            self.on_event("config_change", "reviewer", to_jsonable(data))
        except Exception:  # noqa: BLE001 - the change is applied; a failing sink must not undo the review
            log.exception("reviewer: on_event(config_change) failed")

    def _skip(self, now: float, hash_before: str, reason: str, final: bool,
              review_input: Mapping[str, Any]) -> ReviewResult:
        reason = _one_line(mask_secrets(str(reason), self.secrets), 300)
        body = f"reviewer skipped: {reason}"
        if final:
            facts = self._facts(review_input)
            if facts:
                body += "\n\n" + facts
        entry = self._format_entry(now=now, final=final, hash_before=hash_before, hash_after=hash_before,
                                   body=body, alerts=[], change=None, ignored=[], resp=None, review_input=review_input)
        self._append(entry)
        log.warning("reviewer skipped: %s", reason)
        return ReviewResult(journal=body, change=None, raw_ok=False, entry=entry, skipped_reason=reason, final=final)

    # ------------------------------------------------------------ journal

    def _format_entry(self, *, now: float, final: bool, hash_before: str, hash_after: str, body: str,
                      alerts: list[str], change: ChangeResult | None, ignored: list[dict[str, Any]],
                      resp: LLMResponse | None, review_input: Mapping[str, Any]) -> str:
        title = "Final summary" if final else "Review"
        unchanged = " (unchanged)" if hash_after == hash_before else ""
        lines = [f"## {title} {iso(now)}", "", f"- config_hash: `{hash_before}` -> `{hash_after}`{unchanged}"]
        window = _map(review_input.get("window"))
        if window.get("from") and window.get("to"):
            lines.append(f"- window: {window['from']} .. {window['to']}")
        if resp is not None:
            lines.append(f"- llm: {resp.provider}/{resp.model}, cache key `{resp.cache_key[:16]}`")
        lines += ["", body.strip(), ""]
        if alerts:
            lines += ["**Alerts**", *(f"- {a}" for a in alerts), ""]
        if change is not None and change.accepted:
            lines.append("**Config changes applied**")
            for a in change.accepted:
                lines.append(f"- `{a.get('path')}`: {_fmt_value(a.get('old'))} -> {_fmt_value(a.get('new'))}"
                             f" — {_one_line(a.get('rationale') or '', 300)}")
            if change.overlay_path:
                lines.append(f"- overlay: `{Path(change.overlay_path).name}`")
            lines.append("")
        if ignored:
            lines.append("**Suggestions rejected/ignored**")
            for r in ignored:
                lines.append(f"- `{r.get('path')}` = {_fmt_value(r.get('value'))}: {r.get('why', '')}"
                             f" — {_one_line(r.get('rationale') or '', 300)}")
            lines.append("")
        return mask_secrets("\n".join(lines).rstrip() + "\n", self.secrets)

    def _append(self, entry: str) -> None:
        """Append one section (never rewrite earlier ones). A failing disk never fails the review."""
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.journal_path.exists() or self.journal_path.stat().st_size == 0
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(JOURNAL_HEADER if fresh else "\n")
                fh.write(entry)
        except OSError:
            log.exception("reviewer: could not append to %s", self.journal_path)

    @staticmethod
    def _facts(review_input: Mapping[str, Any]) -> str:
        """Deterministic facts from a ``build_input`` dict (used when the final review has no LLM)."""
        lines: list[str] = []
        portfolio = _map(review_input.get("portfolio"))
        scalars = [(k, v) for k, v in sorted(portfolio.items()) if isinstance(v, (str, int, float, bool))]
        if scalars:
            lines.append("- portfolio: " + ", ".join(f"{k}={_fmt_value(v)}" for k, v in scalars[:12]))
        for strat, a in sorted(_map(review_input.get("pnl_by_strategy")).items()):
            a = _map(a)
            lines.append(f"- {strat}: {a.get('trades', 0)} trades ({a.get('wins', 0)} won, {a.get('losses', 0)} lost),"
                         f" pnl {a.get('pnl_tmn')} TMN")
        fees = _map(review_input.get("fees"))
        if fees:
            lines.append(f"- fees: paid {fees.get('paid_tmn_total')} TMN of budget {fees.get('budget_tmn')} TMN"
                         f" (used {fees.get('used_pct_of_budget')})")
        if review_input.get("vetoes_total"):
            codes = ", ".join(f"{c}×{_map(v).get('count')}" for c, v in sorted(_map(review_input.get("vetoes_by_code")).items()))
            lines.append(f"- vetoes: {review_input.get('vetoes_total')} ({codes})")
        health = _map(review_input.get("health"))
        if health:
            lines.append(f"- health: paused {health.get('paused', 0)}, killed {health.get('killed', 0)}, "
                         f"stale {health.get('stale', 0)}, api-error records {health.get('api_error_records', 0)}")
        return "\n".join(lines)
