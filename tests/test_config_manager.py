"""Reviewer config changes (PRD-04 §7): only allowlisted paths, only within bounds, never risk.*,
at most ``max_changes_per_review`` per review, a cooldown between reviews, no-op changes rejected,
and every accepted review writes a nested, sorted YAML overlay (+ ``latest.yaml``) that loads back
through ``load_config(runtime_overlay=...)`` to exactly the new ``config_hash``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from wta.ai.config_manager import FORBIDDEN_PREFIXES, ConfigManager
from wta.config import Config, config_hash, load_config

NOW = 1_792_663_200.0  # 2026-10-22T10:00:00Z
STAMP = "20261022T100000Z"
MARGIN = "strategies.s1_crossbook.margin_min_pct"
TAKER = "strategies.s1_crossbook.leg1_taker"
REBAL = "strategies.s2_usdtalloc.rebalance_threshold_pp"
CONF = "strategies.s3_momentum.min_regime_confidence"
SLOW = "engine.slow_loop_interval_s"


def sug(path: str, value: Any, rationale: str = "because") -> dict[str, Any]:
    return {"path": path, "value": value, "rationale": rationale}


def why(res: Any) -> dict[str, str]:
    return {r["path"]: r["why"] for r in res.rejected}


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


@pytest.fixture
def cm(cfg: Config, runtime: Path) -> ConfigManager:
    return ConfigManager(cfg, runtime_dir=runtime)


def custom(editable: dict[str, list[Any]], **reviewer: Any) -> Config:
    """The repo config with a different reviewer allowlist (and optional limits)."""
    overrides: dict[str, Any] = {"llm.reviewer.editable": editable}
    overrides.update({f"llm.reviewer.{k}": v for k, v in reviewer.items()})
    return load_config(env={}, overrides=overrides)


def assert_untouched(cm: ConfigManager, before: Config, res: Any, runtime: Path) -> None:
    assert not res.changed and res.config is None and res.overlay_path is None
    assert res.new_hash == res.old_hash == config_hash(before)
    assert cm.cfg is before
    assert cm.last_apply_ts is None  # a review that changed nothing does not start the cooldown
    assert not runtime.exists()


def test_repo_allowlist_is_the_documented_one(cfg: Config) -> None:
    rc = cfg.llm.reviewer
    assert rc.editable == {MARGIN: [0.0008, 0.005], TAKER: [True, False], REBAL: [0.10, 0.25], CONF: [0.5, 0.9],
                           SLOW: [180, 900]}
    assert (rc.max_changes_per_review, rc.cooldown_s) == (2, 3600)
    assert not any(p.startswith(FORBIDDEN_PREFIXES) for p in rc.editable)


# ---------------------------------------------------------------- bounded apply


def test_in_bounds_change_is_applied(cm: ConfigManager, cfg: Config) -> None:
    res = cm.apply([sug(MARGIN, 0.0012, "fees ate the edge")], now=NOW)
    assert res.changed and res.rejected == []
    assert res.accepted == [{"path": MARGIN, "old": "0.0015", "new": "0.0012", "rationale": "fees ate the edge"}]
    assert res.config is not None and res.config.strategies.s1_crossbook.margin_min_pct == Decimal("0.0012")
    assert res.old_hash == config_hash(cfg) and res.new_hash == config_hash(res.config) != res.old_hash
    assert cm.cfg is res.config and cm.last_apply_ts == NOW
    assert cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.0015")  # the old config is not mutated


@pytest.mark.parametrize("value", [0.0008, "0.005", 0.003])
def test_bounds_are_inclusive_and_strings_are_numbers(cm: ConfigManager, value: Any) -> None:
    res = cm.apply([sug(MARGIN, value)], now=NOW)
    assert res.changed
    assert res.config is not None and res.config.strategies.s1_crossbook.margin_min_pct == Decimal(str(value))


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (0.0007, "outside bounds"),
        (0.02, "outside bounds"),
        (-0.001, "outside bounds"),
        ("abc", "not a number"),
        (None, "not a number"),
        ([0.001], "not a number"),
        (True, "not a number"),
        (float("nan"), "not a finite number"),
        ("NaN", "not a finite number"),
        (float("inf"), "not a finite number"),
    ],
)
def test_out_of_bounds_or_non_numeric_values_are_rejected(cm: ConfigManager, cfg: Config, runtime: Path,
                                                          value: Any, reason: str) -> None:
    res = cm.apply([sug(MARGIN, value)], now=NOW)
    assert why(res)[MARGIN].startswith(reason)
    assert res.rejected[0]["value"] is value and res.rejected[0]["rationale"] == "because"
    assert_untouched(cm, cfg, res, runtime)


# ---------------------------------------------------------------- which paths


@pytest.mark.parametrize(
    "path",
    ["risk.max_order_pct_equity", "risk.daily_loss_limit_pct", "execution.limit_cancel_after_s",
     "llm.reviewer.cooldown_s", "ai.replay_strict", "strategies.s1_crossbook.size_pct_equity", "", "nonsense"],
)
def test_paths_outside_the_allowlist_are_rejected(cm: ConfigManager, cfg: Config, runtime: Path, path: str) -> None:
    res = cm.apply([sug(path, 0.1)], now=NOW)
    assert why(res) == {path: "path not editable"}
    assert_untouched(cm, cfg, res, runtime)


def test_risk_paths_are_rejected_even_when_allowlisted(runtime: Path) -> None:
    cfg = custom({"risk.max_order_pct_equity": [0.1, 0.3], "risk.max_open_orders": [1, 20],
                  "execution.maker_offset_ticks": [0, 3], "markets.watchlist": [["BTCUSDT"]],
                  "engine.initial_equity_tmn": [1, 10_000_000], MARGIN: [0.0008, 0.005]})
    cm = ConfigManager(cfg, runtime_dir=runtime)
    res = cm.apply([sug("risk.max_order_pct_equity", 0.2), sug("risk.max_open_orders", 4),
                    sug("execution.maker_offset_ticks", 2), sug("markets.watchlist", ["BTCUSDT"]),
                    sug("engine.initial_equity_tmn", 9_000_000), sug(MARGIN, 0.002)], now=NOW)
    assert set(why(res).values()) == {"path not editable"} and len(res.rejected) == 5
    assert [a["path"] for a in res.accepted] == [MARGIN]
    assert res.config is not None and res.config.risk == cfg.risk and res.config.execution == cfg.execution


def test_allowlisted_but_unknown_paths_are_rejected(runtime: Path) -> None:
    cfg = custom({"strategies.s1_crossbook.no_such_param": [0, 1], "strategies.s9_ghost.enabled": [True, False],
                  f"{MARGIN}.deeper": [0, 1]})
    cm = ConfigManager(cfg, runtime_dir=runtime)
    res = cm.apply([sug("strategies.s1_crossbook.no_such_param", 0.5), sug("strategies.s9_ghost.enabled", True),
                    sug(f"{MARGIN}.deeper", 0.5)], now=NOW)
    assert set(why(res).values()) == {"unknown path"} and len(res.rejected) == 3
    assert_untouched(cm, cfg, res, runtime)


# ---------------------------------------------------------------- value kinds


def test_bool_bounds_accept_only_booleans(cm: ConfigManager, cfg: Config) -> None:
    for bad in ("false", 0, 1, None, "no"):
        res = cm.apply([sug(TAKER, bad)], now=NOW)
        assert why(res) == {TAKER: "expected a boolean"}
    assert why(cm.apply([sug(TAKER, True)], now=NOW)) == {TAKER: "no change"}
    res = cm.apply([sug(TAKER, False)], now=NOW)
    assert res.changed and res.config is not None
    assert res.config.strategies.s1_crossbook.leg1_taker is False
    assert cfg.strategies.s1_crossbook.leg1_taker is True


def test_explicit_value_lists(runtime: Path) -> None:
    res_path = "strategies.s2_usdtalloc.candle_resolution"
    cfg = custom({res_path: ["1", "15", "60"]})
    cm = ConfigManager(cfg, runtime_dir=runtime)
    res = cm.apply([sug(res_path, "5"), sug(res_path, 60), sug(res_path, "15")], now=NOW)
    assert [r["why"] for r in res.rejected] == ["not one of ['1', '15', '60']", "not one of ['1', '15', '60']",
                                               "no change"]
    assert not res.changed
    res = cm.apply([sug(res_path, "60")], now=NOW)
    assert res.changed and res.config is not None and res.config.strategies.s2_usdtalloc.candle_resolution == "60"


def test_value_must_still_fit_the_field_type(runtime: Path) -> None:
    rounds = "ai.analyst_max_tool_rounds"
    cfg = custom({rounds: [3, 8]})
    cm = ConfigManager(cfg, runtime_dir=runtime)
    res = cm.apply([sug(rounds, 5.5)], now=NOW)  # inside the bounds, but the field is an int
    assert why(res)[rounds].startswith("invalid value")
    assert not res.changed and cm.last_apply_ts is None
    res = cm.apply([sug(rounds, 4)], now=NOW)
    assert res.changed and res.config is not None and res.config.ai.analyst_max_tool_rounds == 4
    assert load_config(env={}, overrides={"llm.reviewer.editable": {rounds: [3, 8]}},
                       runtime_overlay=Path(res.overlay_path or "")).config_hash == res.new_hash


@pytest.mark.parametrize(
    ("path", "value"),
    [(MARGIN, 0.0015), (MARGIN, "0.00150"), (MARGIN, Decimal("1.5E-3")), (REBAL, 0.15), (CONF, "0.6"),
     (SLOW, 300), (SLOW, 300.0), (SLOW, "300")],
)
def test_no_op_changes_are_rejected(cm: ConfigManager, cfg: Config, runtime: Path, path: str, value: Any) -> None:
    res = cm.apply([sug(path, value)], now=NOW)
    assert why(res) == {path: "no change"}
    assert_untouched(cm, cfg, res, runtime)


# ---------------------------------------------------------------- limits per review


def test_max_changes_per_review(cm: ConfigManager) -> None:
    res = cm.apply([sug(MARGIN, 0.002), sug(REBAL, 0.2), sug(CONF, 0.7)], now=NOW)
    assert [a["path"] for a in res.accepted] == [MARGIN, REBAL]
    assert why(res) == {CONF: "max_changes_per_review reached"}
    assert res.config is not None and res.config.strategies.s3_momentum.min_regime_confidence == Decimal("0.6")


def test_rejected_suggestions_do_not_use_up_the_quota(cm: ConfigManager) -> None:
    res = cm.apply([sug("risk.max_open_orders", 3), sug(MARGIN, 0.5), sug(REBAL, 0.15), sug(CONF, 0.7),
                    sug(TAKER, False), sug(SLOW, 600)], now=NOW)
    assert [a["path"] for a in res.accepted] == [CONF, TAKER]
    assert why(res) == {"risk.max_open_orders": "path not editable", MARGIN: "outside bounds [0.0008, 0.005]",
                        REBAL: "no change", SLOW: "max_changes_per_review reached"}


def test_configured_max_changes_is_honoured(runtime: Path) -> None:
    cfg = custom(load_config(env={}).llm.reviewer.editable, max_changes_per_review=1)
    res = ConfigManager(cfg, runtime_dir=runtime).apply([sug(MARGIN, 0.002), sug(REBAL, 0.2)], now=NOW)
    assert [a["path"] for a in res.accepted] == [MARGIN]


def test_a_path_changes_at_most_once_per_review(cm: ConfigManager) -> None:
    res = cm.apply([sug(MARGIN, 0.002), sug(MARGIN, 0.003), sug(REBAL, 0.2)], now=NOW)
    assert [(a["path"], a["new"]) for a in res.accepted] == [(MARGIN, "0.002"), (REBAL, "0.2")]
    assert [(r["value"], r["why"]) for r in res.rejected] == [(0.003, "duplicate path in this review")]
    assert res.config is not None and res.config.strategies.s1_crossbook.margin_min_pct == Decimal("0.002")


def test_cooldown_between_applied_reviews(cm: ConfigManager) -> None:
    assert cm.apply([sug(MARGIN, 0.002)], now=NOW).changed
    blocked = cm.apply([sug(REBAL, 0.2), sug(CONF, 0.7)], now=NOW + 3599)
    assert not blocked.changed
    assert [r["why"] for r in blocked.rejected] == ["cooldown 3600s", "cooldown 3600s"]
    assert blocked.new_hash == blocked.old_hash
    assert cm.last_apply_ts == NOW
    later = cm.apply([sug(REBAL, 0.2)], now=NOW + 3600)
    assert later.changed and cm.last_apply_ts == NOW + 3600


def test_a_review_with_only_rejections_does_not_start_the_cooldown(cm: ConfigManager) -> None:
    assert not cm.apply([sug(MARGIN, 1)], now=NOW).changed
    assert cm.apply([sug(MARGIN, 0.002)], now=NOW + 1).changed


# ---------------------------------------------------------------- overlay + hash


def test_overlay_is_nested_sorted_and_mirrored_to_latest(cm: ConfigManager, runtime: Path) -> None:
    res = cm.apply([sug(SLOW, 600), sug(MARGIN, 0.0012)], now=NOW)
    overlay = runtime / f"config-{STAMP}.yaml"
    assert res.overlay_path == str(overlay)
    text = overlay.read_text()
    assert text.splitlines()[:2] == [f"# runtime overlay written by reviewer at {STAMP}",
                                     f"# old_hash {res.old_hash} -> new_hash {res.new_hash}"]
    assert yaml.safe_load(text) == {"engine": {"slow_loop_interval_s": 600.0},
                                    "strategies": {"s1_crossbook": {"margin_min_pct": 0.0012}}}
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert body == ["engine:", "  slow_loop_interval_s: 600.0", "strategies:", "  s1_crossbook:",
                    "    margin_min_pct: 0.0012"]
    assert (runtime / "latest.yaml").read_text() == text
    assert sorted(p.name for p in runtime.iterdir()) == [f"config-{STAMP}.yaml", "latest.yaml"]


def test_source_is_recorded_in_the_overlay(cm: ConfigManager, runtime: Path) -> None:
    res = cm.apply([sug(TAKER, False)], now=NOW, source="operator")
    assert Path(res.overlay_path or "").read_text().startswith(f"# runtime overlay written by operator at {STAMP}")
    assert yaml.safe_load((runtime / "latest.yaml").read_text()) == {
        "strategies": {"s1_crossbook": {"leg1_taker": False}}}


def test_new_hash_round_trips_through_load_config(cm: ConfigManager, cfg: Config) -> None:
    res = cm.apply([sug(MARGIN, 0.0012), sug(SLOW, 600)], now=NOW)
    assert res.new_hash != res.old_hash == config_hash(cfg)
    reloaded = load_config(env={}, runtime_overlay=Path(res.overlay_path or ""))
    assert reloaded.config_hash == res.new_hash
    assert reloaded == res.config
    assert load_config(env={}, runtime_overlay=Path(res.overlay_path or "").parent / "latest.yaml") == res.config


def test_overlay_accumulates_across_reviews(cm: ConfigManager, cfg: Config, runtime: Path) -> None:
    first = cm.apply([sug(MARGIN, 0.0012)], now=NOW)
    second = cm.apply([sug(REBAL, 0.2), sug(MARGIN, 0.002)], now=NOW + 3600)
    assert second.old_hash == first.new_hash
    assert len({config_hash(cfg), first.new_hash, second.new_hash}) == 3
    latest = runtime / "latest.yaml"
    assert yaml.safe_load(latest.read_text()) == {
        "strategies": {"s1_crossbook": {"margin_min_pct": 0.002}, "s2_usdtalloc": {"rebalance_threshold_pp": 0.2}}}
    assert load_config(env={}, runtime_overlay=latest).config_hash == second.new_hash
    # each review keeps its own versioned file; the first one still reproduces the first hash
    assert load_config(env={}, runtime_overlay=Path(first.overlay_path or "")).config_hash == first.new_hash
    assert Path(second.overlay_path or "").name == "config-20261022T110000Z.yaml"
