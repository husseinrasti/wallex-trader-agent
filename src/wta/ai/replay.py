"""Analyst replay (PRD-04 §3, §11): every recorded plan reproduced from the LLM cache, zero network.

``replay_recorded_plans(cfg, log_dir)`` reads the ``kind="plan"`` records of ``<log_dir>/decisions``
and, for each, re-runs the analyst cycle through ``RegimeAnalyst.replay_cycle``:

* the cycle's input (``now``, trigger ``reason``, the JSON state the engine passed) and every tool
  result come from the tape ``<cache_dir>/mcp_tape`` the run recorded;
* every model answer comes from the LLM cache ``<cache_dir>`` with ``strict=True`` in ``replay``
  mode: a miss is a ``CacheMissError``, never a provider call;
* the routes (provider + model) of each cycle are read from the cached requests of its calls
  (the plan record's ``calls``; older records: the first call ``<cache_dir>/<prompt_hash>.json``
  plus the analyst and fallback routes of the run's start event). A cycle that failed over
  part-way (primary timeout on one call, fallback answer) is replayed with both routes, like the
  live client: a primary-route miss then tries the fallback route's cache entry (such a miss is
  counted in ``cache_misses`` although the plan reproduces). The client gets **no provider at
  all** — only a sentinel per provider that counts (and refuses) any call, so
  ``provider_calls == 0`` is asserted, not assumed;
* each cycle is replayed under ITS config: the tape holds the cycle's whole config (and its
  ``config_hash``), so a run whose reviewer changed a value the system prompt shows replays with
  the right value before and after the change. For an older tape without the config the CLI
  config is used, and a miss under a different ``config_hash`` is reported as
  ``config mismatch: recorded X, replay Y`` (not as an opaque cache miss);
* no MCP client (recorded MCP schemas and results come from the tape).

A plan is reproduced when the replayed ``MarketPlan`` equals the recorded ``plan_snapshot`` in every
field but ``cached`` (True in a replay by definition). ``cache_dir`` defaults to where the run wrote
it (``wiring.llm_cache_dir``: ``<log_dir>/llm_cache`` for the default ``ai.cache_dir``).

This is the analyst half of ``wta replay``; re-running the recorded ticks through the engine (books,
candles, decisions) is the Phase-5 runner (PRD-05). Command line::

    python -m wta.ai.replay --log-dir logs/shadow [--cache-dir DIR] [--set key=value ...] [--json]

``--set`` matters only for tapes recorded before the cycle config was taped (it must then repeat
the run's overrides that change a value the system prompt shows) and for locating the cache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wta.ai.analyst import RegimeAnalyst, cycle_tape_key
from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, ProviderError, Route
from wta.ai.tools import InternalTools, ToolDeps
from wta.ai.wiring import llm_cache_dir
from wta.config import Config, canonical_json, load_config, parse_overrides
from wta.logging_.jsonl import iter_family
from wta.utils.timeutil import SimClock

IGNORED_FIELDS = frozenset({"cached"})  # a replayed plan is cached by definition


class _NoNetwork:
    """Stands in for every provider: counts and refuses each call (a replay must never get here)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls += 1
        raise ProviderError(f"replay: provider {self.name} must not be called")


@dataclass
class PlanReplay:
    decision_id: str
    prompt_hash: str
    route: str = ""
    reproduced: bool = False
    replayed_prompt_hash: str = ""
    differences: list[str] = field(default_factory=list)
    config_hash_recorded: str = ""
    config_hash_replayed: str = ""
    config_source: str = ""  # "tape" (the cycle's own config) or "cli" (older tape)
    error: str = ""


@dataclass
class ReplayReport:
    log_dir: str
    cache_dir: str
    config_hash: str
    plans: int = 0
    reproduced: int = 0
    provider_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    results: list[PlanReplay] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.plans > 0 and self.reproduced == self.plans and self.provider_calls == 0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ok"] = self.ok
        return out

    def summary(self) -> str:
        lines = [
            f"analyst replay of {self.log_dir} (cache {self.cache_dir}, config {self.config_hash}):",
            f"  plans recorded {self.plans}, reproduced {self.reproduced}, provider calls {self.provider_calls}, "
            f"cache hits {self.cache_hits}, cache misses {self.cache_misses}",
        ]
        for r in self.results:
            state = "identical" if r.reproduced else f"DIFFERENT ({r.error or '; '.join(r.differences)})"
            lines.append(f"  {r.decision_id} prompt_hash {r.prompt_hash[:16]} via {r.route or '?'}: {state}")
        lines.append("OK: every plan reproduced with zero network calls" if self.ok else "FAILED")
        return "\n".join(lines)


def recorded_plans(log_dir: str | Path) -> list[dict[str, Any]]:
    """``kind="plan"`` records of ``<log_dir>/decisions*.jsonl`` in log order."""
    return [r for r in iter_family(Path(log_dir), "decisions") if r.get("kind") == "plan"]


def _empty_tools(cfg: Config, clock: SimClock) -> InternalTools:
    """Tools over an empty engine: a replay reads every tool result from the tape; a call that
    reaches these answers differently, so the next cache key misses (reported, never hidden)."""
    return InternalTools(ToolDeps(
        cfg=cfg, now=clock.now, features=lambda: None, books=dict, candles_1m=lambda s, a, b: [],
        trades=lambda s, a, b: [], pnl_summary=lambda: {"error": "replay: no engine state"},
        recent_decisions=lambda n: [], health=lambda: {"error": "replay: no engine state"},
    ))


def _differences(recorded: dict[str, Any], replayed: dict[str, Any]) -> list[str]:
    keys = sorted((set(recorded) | set(replayed)) - IGNORED_FIELDS)
    return [k for k in keys if canonical_json(recorded.get(k)) != canonical_json(replayed.get(k))]


def _cached_route(cache_path: Path, key: str) -> Route | None:
    """The route (provider, model) a cached call was made on (its stored request), or None."""
    path = cache_path / f"{key}.json"
    if not key or not path.is_file():
        return None
    try:
        request = json.loads(path.read_text(encoding="utf-8")).get("request") or {}
    except (OSError, ValueError, AttributeError):
        return None
    provider, model = str(request.get("provider") or ""), str(request.get("model") or "")
    return Route(provider, model) if provider else None


def _split_route(text: Any) -> Route | None:
    """``"provider/model"`` (the start event's ``ai.routes``; a model may contain ``/``) -> Route."""
    if not isinstance(text, str) or "/" not in text:
        return None
    provider, model = text.split("/", 1)
    return Route(provider, model) if provider and model and model != "?" else None


def start_routes(log_dir: str | Path) -> list[Route]:
    """The analyst and fallback routes of every start event of the run, in order, distinct."""
    out: list[Route] = []
    for rec in iter_family(Path(log_dir), "events"):
        if rec.get("kind") != "start":
            continue
        routes = ((rec.get("data") or {}).get("ai") or {}).get("routes") or {}
        for name in ("analyst", "fallback"):
            route = _split_route(routes.get(name))
            if route is not None and route not in out:
                out.append(route)
    return out


def cycle_routes(cache_path: Path, record: dict[str, Any], prompt_hash: str, fallbacks: list[Route]) -> list[Route]:
    """Every route this cycle's calls were answered on, first call first (distinct). Older records
    without ``calls``: the first call's route, then the run's analyst/fallback routes."""
    out: list[Route] = []
    calls = (record.get("features") or {}).get("calls")
    keys = [str(c.get("cache_key") or "") for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
    for key in [prompt_hash, *keys]:
        route = _cached_route(cache_path, key)
        if route is not None and route not in out:
            out.append(route)
    if not isinstance(calls, list):
        out.extend(r for r in fallbacks if r not in out)
    return out


def cycle_config(cfg: Config, entry: dict[str, Any]) -> tuple[Config, str]:
    """The config a recorded cycle ran under: the taped one (``"tape"``) when present and valid,
    else ``cfg`` (``"cli"``)."""
    taped = entry.get("config")
    if isinstance(taped, dict):
        try:
            return Config.model_validate(taped), "tape"
        except ValidationError:
            pass
    return cfg, "cli"


async def replay_recorded_plans(
    cfg: Config, log_dir: str | Path, *, cache_dir: str | Path | None = None
) -> ReplayReport:
    """Replay every recorded plan of ``log_dir`` (see the module docstring)."""
    cache_path = Path(cache_dir) if cache_dir is not None else llm_cache_dir(cfg, log_dir)
    report = ReplayReport(log_dir=str(log_dir), cache_dir=str(cache_path), config_hash=cfg.config_hash)
    cache = LLMCache(cache_path, strict=True)
    tape = LLMCache(cache_path / "mcp_tape", strict=False)
    sentinels: dict[str, _NoNetwork] = {}
    clock = SimClock(0.0)
    fallbacks = start_routes(log_dir)
    for rec in recorded_plans(log_dir):
        snap = rec.get("plan_snapshot") or {}
        prompt_hash = str(snap.get("prompt_hash") or (rec.get("features") or {}).get("prompt_hash") or "")
        result = PlanReplay(decision_id=str(rec.get("decision_id")), prompt_hash=prompt_hash)
        report.results.append(result)
        report.plans += 1
        first = cache_path / f"{prompt_hash}.json"
        if not prompt_hash or not first.is_file():
            result.error = "the first call of this cycle is not in the cache"
            continue
        routes = cycle_routes(cache_path, rec, prompt_hash, fallbacks)[:2]  # the client has primary + fallback
        result.route = " + ".join(f"{r.provider}/{r.model}" for r in routes)
        providers = {r.provider: sentinels.setdefault(r.provider, _NoNetwork(r.provider)) for r in routes}
        entry = tape.get(cycle_tape_key(prompt_hash)) or {}
        result.config_hash_recorded = str(entry.get("config_hash") or rec.get("config_hash") or "")
        cycle_cfg, result.config_source = cycle_config(cfg, entry)
        result.config_hash_replayed = cycle_cfg.config_hash
        llm = LLMClient(providers, routes[0], routes[1] if len(routes) > 1 else None, cache, mode="replay",
                        clock=clock)
        analyst = RegimeAnalyst(cycle_cfg, llm, _empty_tools(cycle_cfg, clock), None, clock, tape=tape)
        try:
            plan = await analyst.replay_cycle(prompt_hash)
        except CacheMissError as exc:
            recorded, replayed = result.config_hash_recorded, result.config_hash_replayed
            if recorded and recorded != replayed:
                result.error = f"config mismatch: recorded {recorded}, replay {replayed} (cache miss: {exc})"
            else:
                result.error = f"cache miss: {exc}"
            continue
        if plan is None:
            result.error = analyst.last_error or "no plan"
            continue
        replayed = plan.model_dump(mode="json")
        result.replayed_prompt_hash = plan.prompt_hash
        result.differences = _differences(snap, replayed)
        result.reproduced = not result.differences and plan.prompt_hash == prompt_hash
        if not result.reproduced and result.config_hash_recorded and \
                result.config_hash_recorded != result.config_hash_replayed:
            result.error = (f"config mismatch: recorded {result.config_hash_recorded}, "
                            f"replay {result.config_hash_replayed}")
        report.reproduced += int(result.reproduced)
    report.provider_calls = sum(s.calls for s in sentinels.values())
    report.cache_hits, report.cache_misses = cache.hits, cache.misses
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wta.ai.replay",
                                 description="replay every recorded analyst plan from the LLM cache (zero network)")
    ap.add_argument("--log-dir", required=True, help="log directory of the run (its decisions*.jsonl)")
    ap.add_argument("--cache-dir", default=None, help="LLM cache of the run (default <log-dir>/llm_cache)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="the run's config overrides that change the system prompt (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    cfg = load_config(parse_overrides(args.set))
    report = asyncio.run(replay_recorded_plans(cfg, args.log_dir, cache_dir=args.cache_dir))
    if args.json:
        print(canonical_json(report.as_dict()).decode())
    else:
        print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
