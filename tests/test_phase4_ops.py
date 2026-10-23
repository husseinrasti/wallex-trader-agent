"""Regression tests for the Phase-4 review findings on the operations side: the ops CLI without
``--log-dir``, which engine owns the Telegram commands, a bot username from ``getMe``, the docker
healthcheck, ``run_forever.sh`` on SIGTERM and its crash-loop alert. No network: Telegram is a
respx mock, the healthcheck and the shell script run against files in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from wta import cli
from wta.config import ROOT, Settings
from wta.logging_.health_log import HealthLog
from wta.models import HealthRecord
from wta.utils.telegram import CONFLICT_LIMIT, TelegramCommands
from wta.utils.timeutil import SimClock, iso

from .conftest import T0
from .test_ai_engine import BOT_TOKEN, CHAT_ID, TelegramServer, ai_cfg, ai_rig, no_keys
from .test_telegram import API, CHAT, TOKEN, UPDATES_URL, FakeEngine, ok, tg_error, update, updates

GETME_URL = f"{API}/bot{TOKEN}/getMe"
SEND_URL = f"{API}/bot{TOKEN}/sendMessage"


# ---------------------------------------------------------------- wta status / tui / plan without --log-dir


@pytest.fixture
def no_env_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = no_keys()
    monkeypatch.setattr(cli, "load_settings", lambda: s)
    return s


def _heartbeat(log_dir: Path, ts: float, mode: str, feed: str = "live") -> None:
    log = HealthLog(log_dir, SimClock(ts))
    log.write(HealthRecord(ts=iso(ts), mode=mode, git_commit="abc1234", config_hash="cafe", extra={"feed": feed}))
    log.close()


def test_status_tui_and_plan_work_without_log_dir(
    tmp_path: Path, no_env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    db = ["--set", f"data.state_sqlite_path={tmp_path / 'state_{mode}.sqlite'}",
          "--set", f"data.sqlite_path={tmp_path / 'market.sqlite'}"]
    assert cli.main(["status", *db]) == 0
    assert "no heartbeat" in capsys.readouterr().out
    assert cli.main(["status", "--json", *db]) == 0
    assert json.loads(capsys.readouterr().out)["sources"]["log_dir"] == "logs/shadow"
    assert cli.main(["tui", "--once", "--width", "200", *db]) == 0
    capsys.readouterr()
    assert cli.main(["plan", "--feed", "synthetic", "--warmup", "2", "--no-runtime-overlay", *db]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"] is not None and out["ai_source"] == "offline-rules"
    assert list((tmp_path / "logs" / "shadow" / "llm_cache").glob("*.json"))  # the default log dir


def test_ops_default_picks_the_newer_heartbeat_of_live_and_shadow(
    tmp_path: Path, no_env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    now = time.time()
    _heartbeat(tmp_path / "logs", now - 20, "live")
    _heartbeat(tmp_path / "logs" / "shadow", now - 600, "shadow")
    db = ["--set", f"data.state_sqlite_path={tmp_path / 'state_{mode}.sqlite'}"]
    assert cli.main(["status", "--json", *db]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["sources"]["log_dir"] == "logs" and st["engine"]["mode"] == "live"
    assert st["sources"]["account"] == {"mode": "live", "feed": "live"}
    _heartbeat(tmp_path / "logs" / "shadow", now - 5, "shadow")
    assert cli.main(["status", "--json", *db]) == 0
    assert json.loads(capsys.readouterr().out)["sources"]["log_dir"] == "logs/shadow"
    assert cli.main(["status", "--json", "--mode", "live", *db]) == 0  # an explicit mode still wins
    assert json.loads(capsys.readouterr().out)["sources"]["log_dir"] == "logs"


# ---------------------------------------------------------------- Telegram: one poller per bot


async def test_a_second_engine_keeps_alerts_but_polls_no_commands(tmp_path: Path) -> None:
    settings = no_keys(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID)
    server = TelegramServer()
    cfg = ai_cfg(tmp_path, **{"alerts.telegram_enabled": True, "alerts.telegram_commands_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, settings=settings, enable=False)
    assert rig.engine.enable_telegram(transport=httpx.MockTransport(server))
    assert rig.engine.notifier is not None and rig.engine.telegram_commands is None
    rig.close()

    cfg2 = ai_cfg(tmp_path / "b", **{"alerts.telegram_enabled": True})
    rig2 = await ai_rig(tmp_path / "b", cfg=cfg2, settings=settings, enable=False)
    assert rig2.engine.enable_telegram(transport=httpx.MockTransport(server), commands_enabled=False)
    assert rig2.engine.telegram_commands is None  # flatten_now passes commands_enabled=False
    assert rig2.engine.enable_telegram(transport=httpx.MockTransport(server))
    assert rig2.engine.telegram_commands is not None
    rig2.close()


def test_flatten_now_builds_its_engine_without_telegram_commands(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("flatten_now_cmds", ROOT / "scripts" / "flatten_now.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    async def fake_build(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Stop

    script.build_engine = fake_build
    with pytest.raises(Stop):
        asyncio.run(script.flatten_now(ai_cfg(tmp_path), no_keys(), live=False, feed="synthetic",
                                       paper_balances=None, phase1_s=1, timeout_s=1, log_dir=str(tmp_path)))
    assert seen["telegram_commands"] is False


async def test_getupdates_conflicts_stop_the_poller_and_show_in_the_heartbeat(tmp_path: Path) -> None:
    import respx

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(UPDATES_URL).mock(return_value=tg_error(409, "Conflict: terminated by other getUpdates request"))
        mock.post(GETME_URL).mock(return_value=ok({"username": "wta_bot"}))
        clock = SimClock(T0)
        cmds = TelegramCommands(TOKEN, CHAT, FakeEngine(clock).on_command, clock, poll_interval_s=0,
                                error_backoff_s=0.001)
        await asyncio.wait_for(cmds.run(asyncio.Event()), timeout=5)  # returns by itself
        assert route.call_count == CONFLICT_LIMIT
        assert "another instance polls this bot" in cmds.stopped_reason
        assert cmds.stats()["stopped"] == cmds.stopped_reason
        assert await cmds.poll_once() == [] and route.call_count == CONFLICT_LIMIT

    settings = no_keys(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID)
    cfg = ai_cfg(tmp_path, **{"alerts.telegram_enabled": True})
    rig = await ai_rig(tmp_path, cfg=cfg, settings=settings, enable=False)
    rig.engine.enable_telegram(transport=httpx.MockTransport(TelegramServer()))
    assert rig.engine.telegram_commands is not None
    rig.engine.telegram_commands.stopped_reason = cmds.stopped_reason
    hb = rig.engine.health_record(rig.clock.now())
    assert any(r.startswith("telegram commands off: another instance polls this bot") for r in hb.reasons)
    rig.close()


async def test_commands_for_another_bot_are_ignored_with_the_username_from_getme() -> None:
    import respx

    clock = SimClock(T0)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(GETME_URL).mock(return_value=ok({"id": 1, "is_bot": True, "username": "WTA_bot"}))
        mock.post(UPDATES_URL).mock(return_value=updates(update(1, "/kill@other_bot"), update(2, "/pause@wta_bot")))
        mock.post(SEND_URL).mock(return_value=ok())
        engine = FakeEngine(clock)
        cmds = TelegramCommands.from_settings(Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_chat_id=CHAT),
                                              type("A", (), {"telegram_enabled": True})(), engine.on_command, clock)
        await cmds.poll_once()
    assert cmds.bot_username == "wta_bot" and engine.calls == [("pause", "")]

    with respx.mock(assert_all_called=False) as mock:  # getMe fails: never guess who @x is
        mock.post(GETME_URL).mock(return_value=tg_error(500, "Internal Server Error"))
        mock.post(UPDATES_URL).mock(return_value=updates(update(1, "/kill@other_bot"), update(2, "/status")))
        mock.post(SEND_URL).mock(return_value=ok())
        engine = FakeEngine(clock)
        cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock)
        await cmds.poll_once()
    assert cmds.bot_username == "" and engine.calls == [("status", "")]


# ---------------------------------------------------------------- docker healthcheck


def _healthcheck(service: str) -> str:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    test = compose["services"][service]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"]
    return test[3]


def _run_check(code: str, cwd: Path, mode: str) -> int:
    env = {**os.environ, "WTA_MODE": mode}
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, check=False).returncode


def _touch(path: Path, age_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_the_healthcheck_reads_only_this_agents_log_dir(tmp_path: Path) -> None:
    code = _healthcheck("agent")
    _touch(tmp_path / "logs" / "shadow" / "health-20261023.jsonl", 10)
    _touch(tmp_path / "logs" / "shadow" / "health-20261022.jsonl", 90000)  # yesterday's file: older
    _touch(tmp_path / "logs" / "soak_noop" / "health-20261022.jsonl", 90000)  # sorts after shadow
    _touch(tmp_path / "logs" / "shadow_synth" / "health-20261023.jsonl", 3600)
    assert _run_check(code, tmp_path, "shadow") == 0  # healthy shadow, whatever else is in logs/
    _touch(tmp_path / "logs" / "live" / "health-20261023.jsonl", 900)  # a dead live agent ...
    assert _run_check(code, tmp_path, "live") == 1  # ... is not hidden by the fresh shadow one
    (tmp_path / "nothing").mkdir()
    assert _run_check(code, tmp_path / "nothing", "shadow") == 1
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    shadow = compose["services"]["shadow"]
    assert "alerts.telegram_commands_enabled=false" in shadow["command"]
    assert shadow["environment"]["WTA_MODE"] == "shadow" and _healthcheck("shadow") == code


def test_compose_pins_wta_live_to_0_in_every_service() -> None:
    """env_file exports .env into each container; a WTA_LIVE=1 there must not arm the live guard."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["x-common"]["env_file"] == ".env"
    for name, service in compose["services"].items():
        assert service["environment"]["WTA_LIVE"] == "0", name
    from wta.config import Settings
    from wta.safety import live_guard_reason

    settings = Settings(_env_file=None, wallex_api_key="k" * 20)
    reason = live_guard_reason(True, True, settings, env={"WTA_LIVE": compose["services"]["agent"]["environment"]["WTA_LIVE"]})
    assert reason is not None and "WTA_LIVE=1" in reason


# ---------------------------------------------------------------- run_forever.sh


FAKE_PY = """#!/usr/bin/env bash
# stands in for "python": "-m wta alert ..." records its arguments; "-m wta run ..." is the agent
shift 2
if [ "$1" = "alert" ]; then
  printf '%s\\n' "$@" > "$OUT/alert.args"
  exit 0
fi
if [ "${FAKE_CRASH:-0}" = "1" ]; then exit 3; fi
trap 'sleep 1.5; date +%s.%N > "$OUT/child.done"; exit 0' TERM
echo started >> "$OUT/child.started"
while true; do
  if [ -n "${FAKE_BEAT:-}" ]; then touch "$FAKE_BEAT"; fi
  sleep 0.1
done
"""


def _fake_python(tmp_path: Path) -> dict[str, str]:
    py = tmp_path / "fakepy"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    return {**os.environ, "PY": str(py), "OUT": str(tmp_path), "RUN_FOREVER_LOG": str(tmp_path / "rf.log"),
            "BACKOFF_S": "0"}


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_run_forever_waits_for_the_child_after_sigterm(tmp_path: Path) -> None:
    env = _fake_python(tmp_path)
    proc = subprocess.Popen(["bash", str(ROOT / "scripts" / "run_forever.sh"), "run", "--mode", "shadow"], env=env)
    try:
        deadline = time.time() + 10
        while not (tmp_path / "child.started").exists() and time.time() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "child.started").exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        # the loop left only after the child had finished its shutdown (1.5 s after the signal)
        assert (tmp_path / "child.done").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
    log = (tmp_path / "rf.log").read_text()
    assert log.rstrip().endswith("stopped") and log.count("starting:") == 1


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_run_forever_passes_the_config_arguments_to_the_crash_loop_alert(tmp_path: Path) -> None:
    env = {**_fake_python(tmp_path), "FAKE_CRASH": "1", "MAX_RESTARTS_PER_HOUR": "0"}
    cmd = ["bash", str(ROOT / "scripts" / "run_forever.sh"), "run", "--mode", "shadow",
           "--set", "alerts.telegram_enabled=true", "--overlay", "configs/live_halfsize.yaml", "--set=engine.rng_seed=7"]
    assert subprocess.run(cmd, env=env, timeout=20, check=False).returncode == 1  # gave up
    args = (tmp_path / "alert.args").read_text().splitlines()
    assert args[:6] == ["alert", "--set", "alerts.telegram_enabled=true", "--overlay", "configs/live_halfsize.yaml",
                        "--set=engine.rng_seed=7"]
    assert args[6] == "--" and "giving up" in args[7]


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_run_forever_watchdog_restarts_a_child_without_heartbeats(tmp_path: Path) -> None:
    """PRD-06 §2: alive but no heartbeat for WATCHDOG_S -> SIGTERM and a restart; the hourly cap still applies."""
    log_dir = tmp_path / "lg"
    _touch(log_dir / "health-20261022.jsonl", 1000)  # an old heartbeat of an earlier process never counts
    env = {**_fake_python(tmp_path), "WATCHDOG_S": "1", "WATCHDOG_GRACE_S": "0", "WATCHDOG_POLL_S": "0.2",
           "WATCHDOG_KILL_S": "5", "MAX_RESTARTS_PER_HOUR": "1"}
    cmd = ["bash", str(ROOT / "scripts" / "run_forever.sh"), "run", "--mode", "shadow", "--log-dir", str(log_dir)]
    assert subprocess.run(cmd, env=env, timeout=40, check=False).returncode == 1  # 2 restarts > cap 1: gave up
    log = (tmp_path / "rf.log").read_text()
    assert log.count("starting:") == 2 and log.count("watchdog: no heartbeat in") == 2
    assert f"{log_dir}/health-*.jsonl" in log and "(limit 1s); SIGTERM to" in log
    assert "giving up" in log and "SIGKILL" not in log  # the child stopped on SIGTERM
    assert (tmp_path / "child.done").exists()  # it got SIGTERM (its trap ran), it was not killed hard


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_run_forever_watchdog_leaves_a_beating_child_alone(tmp_path: Path) -> None:
    log_dir = tmp_path / "lg"
    log_dir.mkdir()
    # 2 s, not 1 s: the watchdog reads heartbeat mtimes with 1-second granularity (stat %m), so a 1 s limit
    # can fire on a beating child when the machine is busy
    env = {**_fake_python(tmp_path), "WATCHDOG_S": "2", "WATCHDOG_GRACE_S": "0", "WATCHDOG_POLL_S": "0.2",
           "FAKE_BEAT": str(log_dir / "health-20261023.jsonl")}
    cmd = ["bash", str(ROOT / "scripts" / "run_forever.sh"), "run", "--mode", "shadow", f"--log-dir={log_dir}"]
    proc = subprocess.Popen(cmd, env=env)
    try:
        time.sleep(5.0)  # > 2 x WATCHDOG_S
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    log = (tmp_path / "rf.log").read_text()
    assert "watchdog:" not in log and log.count("starting:") == 1 and log.rstrip().endswith("stopped")


def test_wta_alert_accepts_the_forwarded_arguments(
    no_env_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from wta.utils import telegram as tg

    sent: list[str] = []
    monkeypatch.setattr(tg, "send_alert_cli", lambda s, text, **kw: sent.append(text) or True)
    monkeypatch.setattr(cli, "load_settings", lambda: no_keys(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID))
    overlay = tmp_path / "on.yaml"
    overlay.write_text("alerts:\n  telegram_enabled: true\n")
    assert cli.main(["alert", "--overlay", str(overlay), "--", "run_forever: giving up"]) == 0
    assert cli.main(["alert", "--set", "alerts.telegram_enabled=true", "--", "--weird text"]) == 0
    assert sent == ["run_forever: giving up", "--weird text"]
