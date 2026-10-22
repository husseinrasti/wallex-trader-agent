"""Engine smoke tests (PRD-01): short real runs with tiny intervals, fake data sources, real logs.

Each run lasts about a second on the system clock. Nothing touches the network except one test
that points the real REST client at a closed port on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta import safety
from wta.config import Config, Settings, load_config
from wta.data.store import Store
from wta.engine import Engine, run_engine
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.logging_.checker import check_logs
from wta.logging_.jsonl import iter_family
from wta.models import BookSnapshot, Level
from wta.safety import LiveGuardError
from wta.utils.timeutil import SystemClock

SYMBOLS = ["USDTTMN", "BTCUSDT"]


def _cfg(tmp_path: Path, **extra: Any) -> Config:
    overrides = {
        "engine.fast_loop_interval_s": 0.05,
        "engine.heartbeat_interval_s": 0.2,
        "data.rest_poll_interval_s": 0.05,
        "data.store_flush_interval_s": 0.1,
        "data.ws_enabled": False,
        "data.sqlite_path": str(tmp_path / "wta.sqlite"),
        "markets.watchlist": list(SYMBOLS),
        **extra,
    }
    return load_config(overrides=overrides, env={})


def _book(symbol: str, source: str = "rest") -> BookSnapshot:
    return BookSnapshot(
        symbol=symbol,
        ts=time.time(),
        source=source,
        bids=[Level(price=Decimal("100"), qty=Decimal("1"))],
        asks=[Level(price=Decimal("101"), qty=Decimal("2"))],
    )


class FakeRest:
    """Market-data client: fresh books, or (``down=True``) a transport error on every call."""

    def __init__(self, down: bool = False) -> None:
        self.down = down
        self.calls = 0
        self.errors_total = 0
        self.consecutive_errors = 0

    async def get_all_depth(self) -> dict[str, BookSnapshot]:
        self.calls += 1
        if self.down:
            self.errors_total += 1
            self.consecutive_errors += 1
            raise WallexAPIError(None, "transport", "GET /v2/depth/all: ConnectError")
        return {s: _book(s) for s in SYMBOLS}

    def health(self) -> dict[str, Any]:
        last_error = "GET /v2/depth/all: ConnectError" if self.down else ""
        return {"ok": not self.down, "consecutive_errors": self.consecutive_errors, "last_error": last_error}


class FakeWS:
    """Publishes one ws book per symbol, then waits for the stop event like WallexWS.run."""

    def __init__(self, bus: MarketDataBus, crash: bool = False) -> None:
        self.bus = bus
        self.crash = crash
        self.runs = 0
        self.stopped = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self.runs += 1
        if self.crash:
            raise RuntimeError("ws bug")
        for s in SYMBOLS:
            self.bus.publish_book(_book(s, source="ws"))
        await stop_event.wait()
        self.stopped = True

    def stats(self) -> dict[str, Any]:
        return {"connected": True, "runs": self.runs}


def _engine(tmp_path: Path, cfg: Config, rest: Any = None, ws: Any = None, bus: MarketDataBus | None = None):
    bus = bus or MarketDataBus()
    store = Store(cfg.data.sqlite_path, flush_interval_s=cfg.data.store_flush_interval_s)
    engine = Engine(
        cfg, Settings(_env_file=None), "shadow", SystemClock(), bus, store, rest=rest, ws=ws, log_dir=tmp_path / "logs"
    )
    return engine, store


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    return list(iter_family(tmp_path / "logs", "events", strict=True))


def _health(tmp_path: Path) -> list[dict[str, Any]]:
    return list(iter_family(tmp_path / "logs", "health", strict=True))


async def test_short_run_writes_start_stop_events_and_heartbeats(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rest = FakeRest()
    engine, store = _engine(tmp_path, cfg, rest=rest)
    reason = await engine.run(seconds=1.0, handle_signals=False)
    store.close()

    assert reason == "ran for 1 s"
    events = _events(tmp_path)
    assert [e["kind"] for e in events] == ["start", "stop"]
    assert events[0]["data"]["mode"] == "shadow" and events[0]["data"]["config_hash"] == cfg.config_hash
    assert events[1]["reason"] == "ran for 1 s" and events[1]["data"]["ticks"] >= 5

    health = _health(tmp_path)
    assert len(health) >= 2  # periodic heartbeats + the final one
    last = health[-1]
    assert last["rest_ok"] is True and last["ws_ok"] is False and last["reasons"] == []
    assert last["mode"] == "shadow" and last["config_hash"] == cfg.config_hash and last["git_commit"]
    assert last["equity_tmn"] is None and last["extra"]["book_age_s"]["BTCUSDT"] < 1
    assert rest.calls >= 5
    assert check_logs(tmp_path / "logs").ok

    stored = Store.open_readonly(cfg.data.sqlite_path)
    assert [e["kind"] for e in stored.iter_events()] == ["start", "stop"]
    stored.close()


async def test_keeps_running_when_the_api_is_unreachable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, **{"risk.consecutive_api_errors_pause": 2})
    engine, store = _engine(tmp_path, cfg, rest=FakeRest(down=True))
    reason = await engine.run(seconds=1.0, handle_signals=False)
    store.close()

    assert reason == "ran for 1 s"
    assert [e["kind"] for e in _events(tmp_path)] == ["start", "stop"]
    last = _health(tmp_path)[-1]
    assert last["rest_ok"] is False
    assert any(r.startswith("rest: no fresh data") and "ConnectError" in r for r in last["reasons"])
    assert any("api: " in r and "consecutive errors" in r for r in last["reasons"])
    assert any(r.startswith("stale books") for r in last["reasons"])
    assert last["api_errors_1m"] >= 2
    assert last["extra"]["rest_poller"]["errors"] >= 2
    assert engine.task_crashes == {}  # the poller handled the errors itself
    assert check_logs(tmp_path / "logs").ok


async def test_stop_event_ends_the_run_and_stops_the_ws_feed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    bus = MarketDataBus()
    ws = FakeWS(bus)
    engine, store = _engine(tmp_path, cfg, ws=ws, bus=bus)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)
    reason = await asyncio.wait_for(engine.run(stop_event=stop, handle_signals=False), timeout=5)
    store.close()

    assert reason == "stop requested"
    assert ws.stopped and ws.runs == 1
    last = _health(tmp_path)[-1]
    assert last["ws_ok"] is True and last["rest_ok"] is False and last["extra"]["ws"]["connected"] is True


async def test_sigterm_stops_the_engine_gracefully(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    engine, store = _engine(tmp_path, cfg, rest=FakeRest())
    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
    reason = await asyncio.wait_for(engine.run(), timeout=5)
    store.close()
    assert reason == "signal SIGTERM"
    assert _events(tmp_path)[-1]["reason"] == "signal SIGTERM"


async def test_a_crashing_task_is_restarted_and_the_engine_keeps_going(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    bus = MarketDataBus()
    ws = FakeWS(bus, crash=True)
    engine, store = _engine(tmp_path, cfg, rest=FakeRest(), ws=ws, bus=bus)
    reason = await engine.run(seconds=1.3, handle_signals=False)
    store.close()
    assert reason == "ran for 1.3 s"
    assert ws.runs == 2 and engine.task_crashes == {"ws": 2}  # restarted after 1 s
    assert _health(tmp_path)[-1]["rest_ok"] is True


async def test_banned_watchlist_symbols_are_dropped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, **{"markets.watchlist": ["BTCUSDT", "ATMN"]})
    engine, store = _engine(tmp_path, cfg)
    store.close()
    assert engine.watchlist == ["BTCUSDT"]


async def test_run_engine_wires_the_real_rest_client_and_survives_a_closed_port(tmp_path: Path) -> None:
    with socket.socket() as s:  # a free port; nothing listens on it once closed
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = _cfg(tmp_path, **{"data.max_retries": 0})
    settings = Settings(_env_file=None, wallex_base_url=f"http://127.0.0.1:{port}")
    code = await run_engine(cfg, settings, "shadow", seconds=0.6, log_dir=tmp_path / "logs", handle_signals=False)
    assert code == 0
    last = _health(tmp_path)[-1]
    assert last["rest_ok"] is False and last["extra"]["rest"]["errors_total"] >= 1
    assert [e["kind"] for e in _events(tmp_path)] == ["start", "stop"]


# ---------------------------------------------------------------- live guard (also enforced here, not only in the CLI)


async def test_run_engine_refuses_live_mode_before_building_anything(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: True)
    cfg = _cfg(tmp_path)
    settings = Settings(_env_file=None, wallex_api_key="engine-test-key-0000", wallex_base_url="http://127.0.0.1:9")
    with pytest.raises(LiveGuardError, match="i-understand-live"):
        await run_engine(cfg, settings, "live", seconds=0.1, log_dir=tmp_path / "logs", handle_signals=False)
    with pytest.raises(LiveGuardError, match="dirty"):
        await run_engine(
            cfg, settings, "live", seconds=0.1, log_dir=tmp_path / "logs", handle_signals=False, i_understand_live=True
        )
    assert not Path(cfg.data.sqlite_path).exists() and not (tmp_path / "logs").exists()  # nothing started


async def test_engine_run_checks_the_live_guard_itself(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("WTA_LIVE", raising=False)
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    cfg = _cfg(tmp_path)
    settings = Settings(_env_file=None, wallex_api_key="engine-test-key-0000")
    store = Store(cfg.data.sqlite_path)
    engine = Engine(cfg, settings, "live", SystemClock(), MarketDataBus(), store, rest=FakeRest(),
                    log_dir=tmp_path / "logs", i_understand_live=True)
    with pytest.raises(LiveGuardError, match="WTA_LIVE=1"):
        await engine.run(seconds=0.1, handle_signals=False)
    assert _events(tmp_path) == [] and engine.ticks == 0  # refused before any task or event

    monkeypatch.setenv("WTA_LIVE", "1")  # every guard passes now
    assert await engine.run(seconds=0.3, handle_signals=False) == "ran for 0.3 s"
    store.close()
    assert [e["kind"] for e in _events(tmp_path)] == ["start", "stop"]
    assert _events(tmp_path)[0]["data"]["mode"] == "live"


async def test_a_fatal_ws_close_is_named_in_the_heartbeat(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    bus = MarketDataBus()
    ws = FakeWS(bus)
    ws.fatal_close = 3501  # type: ignore[attr-defined]  # as WallexWS sets it
    engine, store = _engine(tmp_path, cfg, ws=ws, bus=bus)
    record = engine.health_record(time.time())
    store.close()
    assert "ws: stopped by the server (close code 3501, not retried)" in record.reasons
