"""Phase-4 CLI commands: ``wta status``, ``wta tui``, ``wta plan``, ``wta alert`` and the ``run``
options of the AI layer (``--no-runtime-overlay``, pinned ``--set`` keys).

Settings never come from the developer's ``.env`` (no LLM key, no Telegram); ``wta plan`` runs the
offline rules provider on the synthetic feed (a few seconds of real time, no network).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wta import cli
from wta.ai.offline_provider import OFFLINE_MODEL
from wta.data.store import Store
from wta.utils import telegram as tg

from .test_ai_engine import BOT_TOKEN, CHAT_ID, ai_cfg, ai_rig, no_keys


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(**values: Any) -> Any:
        s = no_keys(**values)
        monkeypatch.setattr(cli, "load_settings", lambda: s)
        return s

    install()
    return install


def _a_run(tmp_path: Path) -> Path:
    """Logs + state DB of a short engine run with two offline plans and a heartbeat."""

    async def go() -> Path:
        rig = await ai_rig(tmp_path, cfg=ai_cfg(tmp_path))
        await rig.tick()
        await rig.engine.slow_step(rig.clock.now())
        rig.engine.write_heartbeat(rig.clock.now())
        rig.close()
        return rig.logs

    return asyncio.run(go())


def test_status_reads_the_runs_logs_and_state_db(tmp_path: Path, settings: Any, capsys: Any) -> None:
    logs = _a_run(tmp_path)
    db_set = f"data.state_sqlite_path={tmp_path / 'state_{mode}.sqlite'}"
    assert cli.main(["status", "--log-dir", str(logs), "--mode", "shadow", "--set", db_set, "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert "WTA status" in out and "offline rules (no LLM key)" in out
    assert "AI " in out and "tools local" in out
    assert cli.main(["status", "--log-dir", str(logs), "--set", db_set, "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["sources"]["state_db"] == str(tmp_path / "state_shadow.sqlite")
    assert st["engine"]["ai_source"] == "offline-rules" and st["plan"]["model"] == OFFLINE_MODEL


def test_status_without_any_run_still_answers(tmp_path: Path, settings: Any, capsys: Any) -> None:
    assert cli.main(["status", "--log-dir", str(tmp_path / "nothing")]) == 0
    assert "no heartbeat" in capsys.readouterr().out


def test_tui_once_draws_one_frame(tmp_path: Path, settings: Any, capsys: Any) -> None:
    logs = _a_run(tmp_path)
    db_set = f"data.state_sqlite_path={tmp_path / 'state_{mode}.sqlite'}"
    assert cli.main(["tui", "--once", "--width", "200", "--log-dir", str(logs), "--mode", "shadow",
                     "--set", db_set]) == 0
    out = capsys.readouterr().out
    assert "Current plan" in out and "offline rules (no LLM key)" in out


def test_alert_is_a_noop_without_config(settings: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tg, "send_alert_cli", lambda s, text, **kw: sent.append(text) or True)
    assert cli.main(["alert", "agent", "restarted"]) == 0
    assert sent == [] and "not configured" in capsys.readouterr().err
    settings(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID)
    assert cli.main(["alert", "agent", "restarted"]) == 0  # alerts.telegram_enabled is still false
    assert sent == []
    assert cli.main(["alert", "agent", "restarted", "--set", "alerts.telegram_enabled=true"]) == 0
    assert sent == ["agent restarted"]
    monkeypatch.setattr(tg, "send_alert_cli", lambda s, text, **kw: False)
    assert cli.main(["alert", "x", "--set", "alerts.telegram_enabled=true"]) == 1


def test_plan_prints_one_offline_plan_and_stores_nothing(tmp_path: Path, settings: Any, capsys: Any) -> None:
    args = ["plan", "--feed", "synthetic", "--log-dir", str(tmp_path / "logs"), "--warmup", "5",
            "--no-runtime-overlay",
            "--set", f"data.state_sqlite_path={tmp_path / 'state_{mode}.sqlite'}",
            "--set", f"data.sqlite_path={tmp_path / 'market.sqlite'}"]
    assert cli.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ai_source"] == "offline-rules" and "NOT an LLM" in out["label"] and out["error"] is None
    assert out["plan"]["model"] == OFFLINE_MODEL and out["plan"]["notes"].startswith("[offline rules provider")
    assert out["analyst"]["reason"] == "manual" and out["analyst"]["tools_source"] == "local"
    assert set(out["plan"]["regimes"]) == {"USDTTMN", "BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert "synthetic" in out["note"]
    # a dry run: no decision log, no plans row, no candles stored; the LLM cache recorded the calls
    assert not list((tmp_path / "logs").glob("decisions*.jsonl"))
    store = Store.open_readonly(tmp_path / "state_shadow_synthetic.sqlite")
    try:
        assert store.load_plans() == [] and store.counts()["candles"] == 0
    finally:
        store.close()
    assert list((tmp_path / "logs" / "llm_cache").glob("*.json"))


def test_run_passes_the_overlay_options(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run_engine(cfg: Any, settings: Any, mode: str, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_engine", fake_run_engine)
    assert cli.main(["run", "--seconds", "1", "--no-runtime-overlay", "--runtime-dir", "/tmp/rt",
                     "--set", "engine.slow_loop_interval_s=60"]) == 0
    assert calls[-1]["runtime_overlay"] is False and calls[-1]["runtime_dir"] == "/tmp/rt"
    assert calls[-1]["pinned_keys"] == ["engine.slow_loop_interval_s"]
    assert cli.main(["run", "--seconds", "1"]) == 0
    assert "runtime_overlay" not in calls[-1] and "pinned_keys" not in calls[-1]  # plain run: unchanged


def test_placeholders_are_gone_for_the_new_commands() -> None:
    assert {"status", "tui"}.isdisjoint(cli.PLACEHOLDERS)
    assert {"replay", "backtest"} <= set(cli.PLACEHOLDERS)
