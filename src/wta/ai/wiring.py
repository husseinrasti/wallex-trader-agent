"""Engine-side construction of the AI layer (PRD-04 §8): LLM clients, cache, MCP, runtime overlay.

``build_llm`` picks the AI source once, at engine start:

* ``ai.enabled: false`` -> ``disabled``: no client, no analyst plan ever (ablation, PRD-04 §9).
* an OpenRouter or Gemini key in the environment -> ``llm``: one ``LLMClient`` per role. The analyst
  route is ``configs/llm.yaml -> analyst`` (model: ``analyst.model``, which ``llm.yaml`` fills from
  ``OPENROUTER_MODEL_FAST``; else ``OPENROUTER_MODEL_FAST`` / ``GEMINI_MODEL`` from the settings),
  the reviewer route ``reviewer`` (``OPENROUTER_MODEL_STRONG``), and the fallback route
  ``fallback`` (Gemini, ``GEMINI_MODEL``) when its key is set. Without a key for the primary
  provider the fallback becomes the primary. Per-role daily budgets: ``ai.max_*_calls_per_day``
  count analyst CYCLES and reviews (PRD-04: "400 analyst calls/day (every 5 min = 288)"), checked
  by the analyst / reviewer (``LLMSetup.analyst_cycle_budget`` / ``reviewer_budget``); the
  ``LLMClient`` per-request budget is only a hard ceiling of that many cycles times the requests
  one cycle can make (tool rounds + final answer + JSON fix, each on both routes).
  A provider error, a timeout or an invalid answer gives ``None`` (rules-only / last plan): the
  offline provider is never used while a key is configured — a broken key must show as
  ``llm_ok=false``, not be papered over by rules that look like a plan.
* ``ai.enabled`` and no key -> ``offline-rules``: ``offline_provider.make_offline_llm`` for both
  roles — deterministic rules, NOT an LLM, labelled ``offline rules (no LLM key)`` everywhere.

Every client shares one ``LLMCache`` (``llm_cache_dir``): ``ai.cache_dir`` at its default
(``logs/llm_cache``) means ``<log dir>/llm_cache`` of the run, so a shadow run (``logs/shadow``)
and live (``logs``) keep their own caches next to their own logs; any other value is used as given
(relative to the working directory). The cache is strict only in ``replay`` mode with
``ai.replay_strict``.

``build_mcp``: the read-only Wallex MCP client (``READ_ONLY_ALLOWLIST``) on the live feed only. On
the synthetic feed the analyst gets the ``*_local`` tools: real Wallex prices next to synthetic
books would make a plan about a market the strategies do not see.

``load_runtime_overlay``: the reviewer's ``latest.yaml`` re-applied at startup, so accepted changes
survive restarts — but only reviewer-editable paths within their bounds (the file is an input like
any other; ``risk.*`` and friends can never come back through it), only when ``ai.enabled`` (the
ablation runs on the plain config) and only for the account that wrote it. The default directory is
per account (``runtime_dir_for``: ``configs/runtime/<mode>`` on the live feed,
``configs/runtime/<mode>_<feed>`` otherwise), like the state DB: a reviewer reasoning over
synthetic or paper fills never tunes the live engine.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml

from wta.ai.cache import LLMCache
from wta.ai.config_manager import FORBIDDEN_PREFIXES, overlay_meta
from wta.ai.config_manager import _within as within_bounds
from wta.ai.llm_client import GeminiProvider, LLMClient, OpenRouterProvider, Provider, Route
from wta.ai.mcp_client import READ_ONLY_ALLOWLIST, MCPClient
from wta.ai.offline_provider import has_llm_key, make_offline_llm
from wta.config import CONFIG_DIR, AICfg, Config, Settings, account_label, apply_changes
from wta.utils.timeutil import Clock

log = logging.getLogger(__name__)

AISource = Literal["llm", "offline-rules", "disabled"]
AI_LLM: AISource = "llm"
AI_OFFLINE: AISource = "offline-rules"
AI_DISABLED: AISource = "disabled"
MCP_TIMEOUT_S = 10.0
RUNTIME_LATEST = "latest.yaml"
REVIEW_ATTEMPTS = 2  # LLMClient.structured: the answer + one "fix the JSON" retry


def runtime_dir_for(mode: str, feed: str = "live") -> Path:
    """The default reviewer overlay directory of an account: ``configs/runtime/<account_label>``."""
    return CONFIG_DIR / "runtime" / account_label(mode, feed)


def analyst_requests_per_cycle(cfg: Config) -> int:
    """Model requests one analyst cycle can make on one route: the tool rounds, the final answer
    and one JSON fix."""
    rounds = cfg.ai.analyst_max_tool_rounds if cfg.ai.analyst_tools_enabled else 0
    return max(0, rounds) + 2


def llm_cache_dir(cfg: Config, log_dir: str | Path) -> Path:
    """``ai.cache_dir`` at its default -> ``<log_dir>/llm_cache``; any other value as configured."""
    configured = cfg.ai.cache_dir
    if configured == AICfg.model_fields["cache_dir"].default:
        return Path(log_dir) / "llm_cache"
    return Path(configured)


@dataclass
class LLMSetup:
    """What ``build_llm`` decided (``routes`` holds provider/model names only, never a key)."""

    source: AISource
    analyst: LLMClient | None = None
    reviewer: LLMClient | None = None
    reviewer_route: Route | None = None  # pinned reviewer route (None: the client's own routes)
    cache: LLMCache | None = None
    routes: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    analyst_cycle_budget: int | None = None  # analyst cycles per UTC day (None: unlimited, offline)
    reviewer_budget: int | None = None  # reviews per UTC day


def _key(settings: Settings, provider: str) -> str:
    if provider == "openrouter":
        return settings.openrouter_api_key.get_secret_value()
    if provider == "gemini":
        return settings.gemini_api_key.get_secret_value()
    return ""


def _default_model(settings: Settings, provider: str, strong: bool = False) -> str:
    if provider == "gemini":
        return settings.gemini_model
    return (settings.openrouter_model_strong if strong else "") or settings.openrouter_model_fast


def _providers(cfg: Config, settings: Settings, timeout: float) -> dict[str, Provider]:
    out: dict[str, Provider] = {}
    key = _key(settings, "openrouter")
    if key:
        out["openrouter"] = OpenRouterProvider(key, base_url=cfg.llm.openrouter.base_url, timeout=timeout)
    key = _key(settings, "gemini")
    if key:
        out["gemini"] = GeminiProvider(key, base_url=cfg.llm.fallback.base_url, timeout=timeout)
    return out


def llm_routes(cfg: Config, settings: Settings) -> tuple[Route, Route | None, Route, list[str]]:
    """(analyst primary, fallback, reviewer primary, warnings) for the keys that are set."""
    warnings: list[str] = []
    a_prov = cfg.llm.analyst.provider
    analyst = Route(a_prov, cfg.llm.analyst.model or _default_model(settings, a_prov))
    f_prov = cfg.llm.fallback.provider
    fallback: Route | None = Route(f_prov, cfg.llm.fallback.model or _default_model(settings, f_prov))
    r_prov = cfg.llm.reviewer.provider
    reviewer = Route(r_prov, cfg.llm.reviewer.model or _default_model(settings, r_prov, strong=True)
                     or analyst.model)
    if fallback is not None and (not _key(settings, f_prov) or fallback == analyst):
        fallback = None
    if not _key(settings, a_prov) and fallback is not None:
        warnings.append(f"no {a_prov} key: the analyst uses the fallback {fallback.provider} route")
        analyst, fallback = fallback, None
    if not _key(settings, r_prov):
        reviewer = analyst
    for role, route in (("analyst", analyst), ("reviewer", reviewer), ("fallback", fallback)):
        if route is not None and not route.model:
            warnings.append(f"{role}: no model configured for {route.provider} (calls fail until one is set)")
    return analyst, fallback, reviewer, warnings


def build_llm(
    cfg: Config,
    settings: Settings,
    mode: str,
    clock: Clock,
    cache_dir: str | Path,
    *,
    providers: dict[str, Provider] | None = None,
    on_call: Any = None,
) -> LLMSetup:
    """The AI source and its clients (see the module docstring). ``providers`` replaces the HTTP
    providers (tests: a fake provider under the configured provider name)."""
    if not cfg.ai.enabled:
        return LLMSetup(source=AI_DISABLED)
    strict = mode == "replay" and cfg.ai.replay_strict
    cache = LLMCache(cache_dir, strict=strict)
    if providers is None and not has_llm_key(settings):
        llm = make_offline_llm(cfg, cache, mode, clock, on_call=on_call)
        return LLMSetup(source=AI_OFFLINE, analyst=llm, reviewer=llm, cache=cache,
                        routes={"analyst": "offline/offline-rules-v1", "reviewer": "offline/offline-rules-v1"})
    analyst, fallback, reviewer, warnings = llm_routes(cfg, settings)
    if providers is not None:  # injected (tests): every configured route counts as keyed
        analyst = Route(cfg.llm.analyst.provider, cfg.llm.analyst.model or analyst.model)
        reviewer = Route(cfg.llm.reviewer.provider, cfg.llm.reviewer.model or reviewer.model)
        fb = cfg.llm.fallback
        fallback = Route(fb.provider, fb.model) if fb.model and fb.provider in providers else None
        warnings = []
    a_providers = providers if providers is not None else _providers(cfg, settings, cfg.ai.analyst_timeout_s)
    r_providers = providers if providers is not None else _providers(cfg, settings, cfg.ai.reviewer_timeout_s)
    r_fallback = fallback if fallback != reviewer else None
    # the budgets count cycles / reviews (checked by the analyst and the reviewer); the client's
    # per-request count is only a ceiling: that many cycles x the requests one can make x routes
    a_ceiling = cfg.ai.max_analyst_calls_per_day * analyst_requests_per_cycle(cfg) * (2 if fallback else 1)
    r_ceiling = cfg.ai.max_reviewer_calls_per_day * REVIEW_ATTEMPTS * (2 if r_fallback else 1)
    a_llm = LLMClient(a_providers, analyst, fallback, cache, mode=mode, clock=clock,
                      daily_budget={"analyst": a_ceiling}, on_call=on_call)
    r_llm = LLMClient(r_providers, reviewer, r_fallback, cache, mode=mode,
                      clock=clock, daily_budget={"reviewer": r_ceiling}, on_call=on_call)
    for w in warnings:
        log.warning("ai: %s", w)
    routes = {
        "analyst": f"{analyst.provider}/{analyst.model or '?'}",
        "reviewer": f"{reviewer.provider}/{reviewer.model or '?'}",
        "fallback": None if fallback is None else f"{fallback.provider}/{fallback.model or '?'}",
    }
    return LLMSetup(source=AI_LLM, analyst=a_llm, reviewer=r_llm, cache=cache, routes=routes, warnings=warnings,
                    analyst_cycle_budget=cfg.ai.max_analyst_calls_per_day,
                    reviewer_budget=cfg.ai.max_reviewer_calls_per_day)


def build_mcp(settings: Settings, feed: str, mode: str) -> MCPClient | None:
    """The read-only Wallex MCP client, or None (synthetic feed, replay, no key or URL)."""
    if feed != "live" or mode == "replay":
        return None
    key = settings.wallex_api_key.get_secret_value()
    if not key or not settings.wallex_mcp_url:
        return None
    return MCPClient(settings.wallex_mcp_url, key, settings.wallex_mcp_auth_header, timeout=MCP_TIMEOUT_S,
                     allowlist=READ_ONLY_ALLOWLIST)


# ---------------------------------------------------------------- runtime overlay


def _flatten(tree: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(tree, dict):
        return {prefix: tree} if prefix else {}
    out: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def load_runtime_overlay(
    cfg: Config,
    path: str | Path,
    skip_paths: set[str] | frozenset[str] = frozenset(),
    *,
    account: str | None = None,
) -> tuple[Config, dict[str, Any] | None]:
    """Apply the reviewer's runtime overlay (``<runtime_dir>/latest.yaml``) to ``cfg``.

    Returns ``(config, info)``; ``info`` is None when the file does not exist, else
    ``{path, sha256, account, applied_ts, applied, pinned, rejected, config_hash_before,
    config_hash_after}`` for the start event (``skipped`` when nothing was applied on purpose).

    * Only ``reviewer.editable`` paths within their bounds are applied (never a forbidden prefix).
    * ``skip_paths`` (the operator's ``--set`` / ``--overlay`` keys) win over the overlay: a valid
      entry for one of them is not applied but returned in ``pinned``, so the ConfigManager keeps it
      in ``latest.yaml`` (a later review must not erase it; the next run without the pin gets it).
    * ``account`` (``config.account_label``): an overlay whose ``# meta`` header names another
      account is refused as a whole. A file without the header (older versions) is accepted.
    * ``applied_ts``: when the overlay was written (``# meta``; else the file's mtime), so the
      reviewer's cooldown survives a restart.
    """
    p = Path(path)
    if not p.is_file():
        return cfg, None
    text = p.read_text(encoding="utf-8")
    meta = overlay_meta(text)
    applied_ts = meta.get("applied_ts")
    if not isinstance(applied_ts, int | float) or isinstance(applied_ts, bool):
        try:
            applied_ts = p.stat().st_mtime
        except OSError:
            applied_ts = None
    info: dict[str, Any] = {
        "path": str(p),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "account": meta.get("account"),
        "applied_ts": applied_ts,
        "applied": {},
        "pinned": {},
        "rejected": [],
        "config_hash_before": cfg.config_hash,
        "config_hash_after": cfg.config_hash,
    }
    if account is not None and meta.get("account") not in (None, account):
        why = f"written by another account ({meta.get('account')}; this engine is {account})"
        info["rejected"].append({"path": "*", "why": why})
        info["skipped"] = why
        return cfg, info
    try:
        tree = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        info["rejected"].append({"path": "*", "why": f"not valid YAML: {str(exc).splitlines()[0][:200]}"})
        return cfg, info
    editable = cfg.llm.reviewer.editable
    accepted: dict[str, Any] = {}
    for key, value in sorted(_flatten(tree).items()):
        if key.startswith(FORBIDDEN_PREFIXES) or key not in editable:
            info["rejected"].append({"path": key, "value": value, "why": "not reviewer-editable"})
            continue
        ok, val, why = within_bounds(value, editable[key])
        if not ok:
            info["rejected"].append({"path": key, "value": value, "why": why})
            continue
        plain = float(val) if isinstance(val, Decimal) else val
        if key in skip_paths:
            info["rejected"].append({"path": key, "value": value, "why": "overridden by --set"})
            info["pinned"][key] = plain
            continue
        accepted[key] = plain
    if not accepted:
        return cfg, info
    try:
        new_cfg = apply_changes(cfg, accepted)
    except ValueError as exc:
        info["rejected"].extend({"path": k, "value": v, "why": f"invalid: {str(exc)[:200]}"}
                                for k, v in sorted(accepted.items()))
        return cfg, info
    info["applied"] = accepted
    info["config_hash_after"] = new_cfg.config_hash
    return new_cfg, info
