"""`wta run --ai on|off|offline` and `wta plan --ai on|offline`; demo.sh --feed/--ai parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from wta import cli
from wta.ai.wiring import has_llm_key
from wta.config import Settings, load_config

ROOT = Path(__file__).resolve().parents[1]


def _keyed() -> Settings:
    return Settings(_env_file=None, openrouter_api_key=SecretStr("dummy-openrouter-key-123"),
                    gemini_api_key=SecretStr("dummy-gemini-key-456"))


def test_on_keeps_config_and_keys() -> None:
    cfg, s = load_config(env={}), _keyed()
    c2, s2 = cli.apply_ai_choice("on", cfg, s)
    assert c2 is cfg and s2 is s and has_llm_key(s2)


def test_off_disables_the_analyst_and_changes_the_hash() -> None:
    cfg, s = load_config(env={}), _keyed()
    c2, s2 = cli.apply_ai_choice("off", cfg, s)
    assert c2.ai.enabled is False and c2.config_hash != cfg.config_hash and s2 is s


def test_offline_blanks_only_the_llm_keys_for_this_run() -> None:
    cfg, s = load_config(env={}), _keyed()
    c2, s2 = cli.apply_ai_choice("offline", cfg, s)
    assert c2 is cfg and not has_llm_key(s2)
    assert has_llm_key(s)  # the caller's settings are untouched
    assert s2.wallex_api_key == s.wallex_api_key


def test_unknown_choice_is_refused() -> None:
    with pytest.raises(ValueError):
        cli.apply_ai_choice("maybe", load_config(env={}), _keyed())


def test_parser_accepts_the_choices() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["run", "--ai", "offline"]).ai == "offline"
    assert parser.parse_args(["run"]).ai == "on"
    assert parser.parse_args(["plan", "--ai", "offline"]).ai == "offline"
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "--ai", "off"])


def _demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(ROOT / "scripts" / "demo.sh"), *args], capture_output=True, text=True,
                          timeout=60, cwd=ROOT)


def test_demo_feed_live_changes_only_the_dashboard_run() -> None:
    out = _demo("--feed", "live", "--ai", "offline", "--dry-run", "--scene", "0").stdout
    runs = [ln for ln in out.splitlines() if " -m wta run " in ln]
    assert any("--feed live" in ln and "--ai offline" in ln for ln in runs)  # the dashboard run
    assert any("--feed synthetic" in ln and "--synthetic-shock-pct" in ln for ln in runs)  # the drill
    assert not any("--mode live" in ln for ln in runs)


def test_demo_rejects_bad_feed_and_ai() -> None:
    assert _demo("--feed", "testnet", "--list").returncode == 2
    assert _demo("--ai", "maybe", "--list").returncode == 2
