"""Config loading: the three yaml files, the ban list, ``--set`` overrides, env interpolation,
``config_hash``, ``apply_changes`` and secret masking."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from wta.config import (
    ROOT,
    Config,
    Settings,
    apply_changes,
    config_hash,
    load_config,
    mask_secrets,
    parse_overrides,
)

BANNED_TXT = ROOT / "docs" / "BANNED_MARKETS.txt"
SECRET = "dummy-secret-123"
SECRET_ENV_VARS = ("WALLEX_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN")


def _write_config_dir(tmp_path: Path, default_yaml: str) -> Path:
    """A config dir with only ``default.yaml`` (markets.yaml / llm.yaml are optional)."""
    (tmp_path / "default.yaml").write_text(default_yaml)
    return tmp_path


@pytest.fixture
def clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no real secret from the shell can reach a ``Settings`` instance."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- loading the three files


def test_default_yaml_is_loaded(cfg: Config) -> None:
    assert cfg.engine.fast_loop_interval_s == 2
    assert cfg.risk.max_order_pct_equity == Decimal("0.25")
    assert isinstance(cfg.risk.max_order_pct_equity, Decimal)
    # default.yaml lists 10 S1 coins; the model default has only 5 -> proves the file was read
    assert cfg.strategies.s1_crossbook.coins[-1] == "AVAX"
    assert len(cfg.strategies.s1_crossbook.coins) == 10
    assert cfg.strategies.s2_usdtalloc.bounds == (Decimal("0.25"), Decimal("0.70"))


def test_markets_yaml_is_loaded(cfg: Config) -> None:
    assert cfg.markets.quote_assets == ["TMN", "USDT"]
    assert cfg.markets.watchlist[:3] == ["USDTTMN", "BTCTMN", "BTCUSDT"]
    assert len(cfg.markets.watchlist) == 21  # 21 x 3 WS channels = 63 <= the 64-channel cap


def test_llm_yaml_is_loaded(cfg: Config) -> None:
    assert cfg.llm.openrouter.base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm.reviewer.max_tokens == 2000  # model default is 1500
    assert cfg.llm.reviewer.editable["strategies.s1_crossbook.margin_min_pct"] == [0.0008, 0.005]
    assert cfg.llm.fallback.provider == "gemini"


def test_config_dir_with_only_default_yaml_uses_model_defaults(tmp_path: Path) -> None:
    config_dir = _write_config_dir(tmp_path, "engine:\n  fast_loop_interval_s: 3\n")
    cfg = load_config(config_dir=config_dir, env={})
    assert cfg.engine.fast_loop_interval_s == 3
    assert cfg.markets.banned == []
    assert cfg.llm.reviewer.max_tokens == 1500


# ---------------------------------------------------------------- ban list


def _banned_txt_symbols() -> list[str]:
    lines = (line.strip() for line in BANNED_TXT.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_banned_list_is_complete(cfg: Config) -> None:
    banned = cfg.markets.banned
    assert len(banned) == 345
    assert len(set(banned)) == 345
    assert "ATMN" in banned
    assert "1BBABYDOGEUSDT" in banned
    assert set(banned) == set(_banned_txt_symbols())  # same as the organizer list in docs/


def test_no_watchlist_symbol_is_banned(cfg: Config) -> None:
    assert not set(cfg.markets.watchlist) & set(cfg.markets.banned)
    assert not any(cfg.markets.is_banned(s) for s in cfg.markets.watchlist)


@pytest.mark.parametrize("symbol", ["ATMN", "A/TMN", "a/tmn", "atmn", "1BBABYDOGE/USDT"])
def test_is_banned_normalises(cfg: Config, symbol: str) -> None:
    assert cfg.markets.is_banned(symbol)


@pytest.mark.parametrize("symbol", ["BTCTMN", "BTC/USDT", "USDTTMN", "SOLTMN"])
def test_is_banned_allows_majors(cfg: Config, symbol: str) -> None:
    assert not cfg.markets.is_banned(symbol)


# ---------------------------------------------------------------- --set overrides


def test_parse_overrides_uses_yaml_typing() -> None:
    parsed = parse_overrides(["a.b=0.2", "c=true", "d=3", "e=hello", "f=x=y"])
    assert parsed == {"a.b": 0.2, "c": True, "d": 3, "e": "hello", "f": "x=y"}
    assert parse_overrides(None) == {}


def test_parse_overrides_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="key=value"):
        parse_overrides(["risk.max_order_pct_equity"])


def test_set_overrides_are_typed_by_the_model() -> None:
    cfg = load_config(
        overrides=[
            "risk.max_order_pct_equity=0.2",
            "strategies.s3_momentum.enabled=true",
            "ai.enabled=false",
        ],
        env={},
    )
    assert cfg.risk.max_order_pct_equity == Decimal("0.2")
    assert str(cfg.risk.max_order_pct_equity) == "0.2"  # no binary float artefacts
    assert cfg.strategies.s3_momentum.enabled is True
    assert cfg.ai.enabled is False


def test_dict_overrides() -> None:
    cfg = load_config(overrides={"engine.stale_data_s": 10, "markets.watchlist": ["BTCUSDT"]}, env={})
    assert cfg.engine.stale_data_s == 10
    assert cfg.markets.watchlist == ["BTCUSDT"]


def test_override_with_invalid_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load_config(overrides=["risk.max_open_orders=many"], env={})


# ---------------------------------------------------------------- config hash


def test_hash_is_stable_across_loads() -> None:
    a, b = load_config(env={}), load_config(env={})
    assert a == b
    assert a.config_hash == b.config_hash
    assert re.fullmatch(r"[0-9a-f]{12}", a.config_hash)
    assert a.config_hash == config_hash(a)


def test_hash_changes_with_an_override(cfg: Config) -> None:
    changed = load_config(overrides=["risk.max_order_pct_equity=0.2"], env={})
    assert changed.config_hash != cfg.config_hash


def test_hash_unchanged_when_override_repeats_the_yaml_value(cfg: Config) -> None:
    same = load_config(overrides=["risk.max_order_pct_equity=0.25"], env={})
    assert same.config_hash == cfg.config_hash


# ---------------------------------------------------------------- env interpolation


def test_env_placeholders_are_resolved() -> None:
    env = {"CHALLENGE_END_UTC": "2026-10-25T12:00:00Z", "OPENROUTER_MODEL_FAST": "vendor/fast-model"}
    cfg = load_config(env=env)
    assert cfg.engine.challenge_end_utc == "2026-10-25T12:00:00Z"
    assert cfg.llm.analyst.model == "vendor/fast-model"


def test_missing_or_empty_env_resolves_to_none(cfg: Config) -> None:
    assert cfg.engine.challenge_start_utc is None
    assert cfg.engine.challenge_end_utc is None
    assert cfg.llm.analyst.model is None
    assert load_config(env={"CHALLENGE_END_UTC": ""}).engine.challenge_end_utc is None


def test_env_value_is_part_of_the_hash(cfg: Config) -> None:
    with_end = load_config(env={"CHALLENGE_END_UTC": "2026-10-25T12:00:00Z"})
    assert with_end.config_hash != cfg.config_hash


def test_env_placeholder_inside_a_longer_string(tmp_path: Path) -> None:
    config_dir = _write_config_dir(tmp_path, 'data:\n  sqlite_path: "data/${RUN_ID}/wta.sqlite"\n')
    assert load_config(config_dir=config_dir, env={"RUN_ID": "r1"}).data.sqlite_path == "data/r1/wta.sqlite"


# ---------------------------------------------------------------- unknown keys (extra=forbid)


def test_unknown_key_in_yaml_is_rejected(tmp_path: Path) -> None:
    config_dir = _write_config_dir(tmp_path, "risk:\n  max_order_pct_equtiy: 0.2\n")  # typo on purpose
    with pytest.raises(ValidationError, match="max_order_pct_equtiy"):
        load_config(config_dir=config_dir, env={})


def test_unknown_key_in_override_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load_config(overrides=["risk.not_a_rule=1"], env={})


def test_unknown_top_level_section_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"bogus": {}})


def test_config_is_frozen(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        cfg.risk.max_open_orders = 99  # type: ignore[misc]


# ---------------------------------------------------------------- runtime overlay + apply_changes


def test_runtime_overlay_is_deep_merged(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("strategies:\n  s1_crossbook:\n    margin_min_pct: 0.002\n")
    cfg = load_config(runtime_overlay=overlay, env={})
    assert cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.002")
    assert cfg.strategies.s1_crossbook.z_min == Decimal("2.0")  # siblings untouched
    assert load_config(runtime_overlay=tmp_path / "missing.yaml", env={}) == load_config(env={})


def test_apply_changes_returns_a_new_validated_config(cfg: Config) -> None:
    changed = apply_changes(
        cfg,
        {"strategies.s1_crossbook.margin_min_pct": "0.002", "strategies.s1_crossbook.leg1_taker": False},
    )
    assert changed.strategies.s1_crossbook.margin_min_pct == Decimal("0.002")
    assert changed.strategies.s1_crossbook.leg1_taker is False
    assert cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.0015")  # original untouched
    assert changed.config_hash != cfg.config_hash
    assert changed.risk == cfg.risk


def test_apply_no_changes_keeps_the_hash(cfg: Config) -> None:
    same = apply_changes(cfg, {})
    assert same == cfg
    assert same.config_hash == cfg.config_hash


@pytest.mark.parametrize(
    "changes",
    [{"strategies.s1_crossbook.margin_min_pct": "lots"}, {"strategies.s1_crossbook.no_such_param": 1}],
)
def test_apply_changes_rejects_invalid_changes(cfg: Config, changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        apply_changes(cfg, changes)


# ---------------------------------------------------------------- secrets


def test_settings_masked_never_contains_a_secret(clean_secret_env: None) -> None:
    settings = Settings(wallex_api_key=SECRET, _env_file=None)
    masked = settings.masked()
    assert masked["wallex_api_key"] == "<set>"
    assert masked["openrouter_api_key"] == "<empty>"
    assert SECRET not in json.dumps(masked)
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert settings.secret_values() == [SECRET]


def test_settings_masked_hides_every_secret_field(clean_secret_env: None) -> None:
    secrets = {
        "wallex_api_key": "wallex-secret-1",
        "openrouter_api_key": "openrouter-secret-2",
        "gemini_api_key": "gemini-secret-3",
        "telegram_bot_token": "telegram-secret-4",
    }
    settings = Settings(**secrets, _env_file=None)
    dumped = json.dumps(settings.masked())
    for name, value in secrets.items():
        assert settings.masked()[name] == "<set>"
        assert value not in dumped
    assert sorted(settings.secret_values()) == sorted(secrets.values())


def test_mask_secrets_replaces_every_occurrence() -> None:
    text = f"GET /v1/account/balances key={SECRET} retry key={SECRET}"
    assert mask_secrets(text, [SECRET]) == "GET /v1/account/balances key=*** retry key=***"


def test_mask_secrets_handles_several_short_and_empty_secrets() -> None:
    text = "a=abcd b=xyz c=longer-secret"
    assert mask_secrets(text, ["abcd", "xyz", "", "longer-secret"]) == "a=*** b=xyz c=***"
    assert mask_secrets(text, []) == text


def test_a_field_at_its_legacy_value_does_not_change_the_hash() -> None:
    """Configs recorded before ai.repair_plan_nesting existed keep their exact hash (replay stays exact)."""
    from wta.config import HASH_LEGACY_DEFAULTS, apply_changes, load_config

    cfg = load_config(env={})
    on = apply_changes(cfg, {"ai.repair_plan_nesting": True})
    off = apply_changes(cfg, {"ai.repair_plan_nesting": False})
    assert on.config_hash != off.config_hash
    tree = off.model_dump(mode="json")
    tree["ai"].pop("repair_plan_nesting")  # what a config recorded before the field looked like
    import hashlib

    from wta.config import canonical_json
    assert off.config_hash == hashlib.sha256(canonical_json(tree)).hexdigest()[:12]
    assert ("ai", "repair_plan_nesting") in HASH_LEGACY_DEFAULTS
