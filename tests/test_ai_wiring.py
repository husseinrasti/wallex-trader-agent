"""``wta.ai.wiring``: which AI source the engine uses, the routes per role, the cache directory,
the MCP client and the runtime overlay. No network: providers are built but never called."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wta.ai.llm_client import GeminiProvider, OpenRouterProvider, Route
from wta.ai.mcp_client import READ_ONLY_ALLOWLIST, MCPClient
from wta.ai.offline_provider import OFFLINE_MODEL, OFFLINE_PROVIDER, OfflineRulesProvider
from wta.ai.wiring import (
    AI_DISABLED,
    AI_LLM,
    AI_OFFLINE,
    build_llm,
    build_mcp,
    llm_cache_dir,
    llm_routes,
    load_runtime_overlay,
)
from wta.config import Config, Settings, apply_changes
from wta.utils.timeutil import SimClock

from .conftest import T0

OR_KEY = "".join(["sk-or-", "wiring", "-test-", "a" * 10])
GM_KEY = "".join(["gm-", "wiring", "-test-", "b" * 10])
WX_KEY = "".join(["wx-", "wiring", "-test-", "c" * 10])


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {"openrouter_api_key": "", "gemini_api_key": "", "wallex_api_key": "",
                            "openrouter_model_fast": "", "openrouter_model_strong": "", "gemini_model": ""}
    return Settings(_env_file=None, **{**base, **values})


def test_no_key_means_the_offline_rules_provider(cfg: Config, tmp_path: Path) -> None:
    setup = build_llm(cfg, settings(), "shadow", SimClock(T0), tmp_path)
    assert setup.source == AI_OFFLINE
    assert setup.analyst is setup.reviewer and setup.analyst is not None
    assert isinstance(setup.analyst.providers[OFFLINE_PROVIDER], OfflineRulesProvider)
    assert setup.analyst.primary == Route(OFFLINE_PROVIDER, OFFLINE_MODEL) and setup.analyst.fallback is None
    assert setup.cache is not None and not setup.cache.strict


def test_ai_disabled_builds_nothing(cfg: Config, tmp_path: Path) -> None:
    off = apply_changes(cfg, {"ai.enabled": False})
    setup = build_llm(off, settings(openrouter_api_key=OR_KEY), "shadow", SimClock(T0), tmp_path)
    assert setup.source == AI_DISABLED and setup.analyst is None and setup.reviewer is None


def test_openrouter_primary_gemini_fallback_with_daily_budgets(cfg: Config, tmp_path: Path) -> None:
    s = settings(openrouter_api_key=OR_KEY, gemini_api_key=GM_KEY, openrouter_model_fast="vendor/fast",
                 openrouter_model_strong="vendor/strong", gemini_model="gemini-x")
    setup = build_llm(cfg, s, "shadow", SimClock(T0), tmp_path)
    assert setup.source == AI_LLM
    a, r = setup.analyst, setup.reviewer
    assert a is not None and r is not None and a is not r
    assert a.primary == Route("openrouter", "vendor/fast") and a.fallback == Route("gemini", "gemini-x")
    assert r.primary == Route("openrouter", "vendor/strong") and r.fallback == Route("gemini", "gemini-x")
    assert isinstance(a.providers["openrouter"], OpenRouterProvider) and isinstance(a.providers["gemini"], GeminiProvider)
    # the budgets count cycles / reviews (PRD-04: 400 analyst cycles a day); the clients only get a
    # per-request ceiling: cycles x (tool rounds + final + JSON fix) x 2 routes
    assert setup.analyst_cycle_budget == cfg.ai.max_analyst_calls_per_day == 400
    assert setup.reviewer_budget == cfg.ai.max_reviewer_calls_per_day == 30
    assert a.daily_budget == {"analyst": 400 * (cfg.ai.analyst_max_tool_rounds + 2) * 2}
    assert r.daily_budget == {"reviewer": 30 * 2 * 2}
    assert a.providers["openrouter"].timeout == cfg.ai.analyst_timeout_s
    assert r.providers["openrouter"].timeout == cfg.ai.reviewer_timeout_s
    assert setup.routes == {"analyst": "openrouter/vendor/fast", "reviewer": "openrouter/vendor/strong",
                            "fallback": "gemini/gemini-x"}
    assert OR_KEY not in repr(setup.routes) and GM_KEY not in repr(setup.routes)


def test_llm_yaml_models_win_over_the_env_defaults(cfg: Config) -> None:
    tuned = apply_changes(cfg, {"llm.analyst.model": "yaml/analyst", "llm.fallback.model": "gemini-yaml"})
    a, fb, r, warnings = llm_routes(tuned, settings(openrouter_api_key=OR_KEY, gemini_api_key=GM_KEY,
                                                    openrouter_model_fast="env/fast"))
    assert a == Route("openrouter", "yaml/analyst") and fb == Route("gemini", "gemini-yaml")
    # no strong model anywhere: the reviewer falls back to OPENROUTER_MODEL_FAST
    assert r == Route("openrouter", "env/fast") and warnings == []


def test_only_a_gemini_key_makes_gemini_the_primary(cfg: Config) -> None:
    a, fb, r, warnings = llm_routes(cfg, settings(gemini_api_key=GM_KEY, gemini_model="gemini-x"))
    assert a == Route("gemini", "gemini-x") and fb is None and r == a
    assert any("fallback gemini route" in w for w in warnings)


def test_a_key_without_a_model_is_reported_not_hidden(cfg: Config, tmp_path: Path) -> None:
    setup = build_llm(cfg, settings(openrouter_api_key=OR_KEY), "shadow", SimClock(T0), tmp_path)
    assert setup.source == AI_LLM  # still the LLM path: calls fail visibly (llm_ok=false), no offline fallback
    assert any("no model configured" in w for w in setup.warnings)


def test_replay_mode_makes_the_cache_strict(cfg: Config, tmp_path: Path) -> None:
    assert build_llm(cfg, settings(), "replay", SimClock(T0), tmp_path).cache.strict  # type: ignore[union-attr]
    lax = apply_changes(cfg, {"ai.replay_strict": False})
    assert not build_llm(lax, settings(), "replay", SimClock(T0), tmp_path).cache.strict  # type: ignore[union-attr]


def test_cache_dir_follows_the_log_dir_unless_configured(cfg: Config, tmp_path: Path) -> None:
    assert llm_cache_dir(cfg, "logs/shadow") == Path("logs/shadow/llm_cache")
    assert llm_cache_dir(cfg, tmp_path) == tmp_path / "llm_cache"
    moved = apply_changes(cfg, {"ai.cache_dir": str(tmp_path / "shared")})
    assert llm_cache_dir(moved, "logs") == tmp_path / "shared"


def test_mcp_client_only_on_the_live_feed_with_a_key() -> None:
    assert build_mcp(settings(), "live", "shadow") is None
    assert build_mcp(settings(wallex_api_key=WX_KEY), "synthetic", "shadow") is None
    assert build_mcp(settings(wallex_api_key=WX_KEY), "live", "replay") is None
    mcp = build_mcp(settings(wallex_api_key=WX_KEY), "live", "shadow")
    assert isinstance(mcp, MCPClient) and mcp.allowlist == frozenset(READ_ONLY_ALLOWLIST)


@pytest.mark.parametrize("text", ["", "{not: [valid", "- a\n- b\n"])
def test_runtime_overlay_survives_junk(cfg: Config, tmp_path: Path, text: str) -> None:
    path = tmp_path / "latest.yaml"
    path.write_text(text)
    new, info = load_runtime_overlay(cfg, path)
    assert info is not None and info["applied"] == {} and new.config_hash == cfg.config_hash


def test_missing_runtime_overlay_is_none(cfg: Config, tmp_path: Path) -> None:
    assert load_runtime_overlay(cfg, tmp_path / "latest.yaml") == (cfg, None)
