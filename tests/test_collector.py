"""Collector: REST/candle pollers, the recorder (dedupe + health) and run_recorder end to end.

All exchange access goes through in-memory fakes; sleeps are faked or tiny. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta.config import Config, Settings
from wta.data import collector
from wta.data.collector import CandlePoller, Recorder, RestPoller, recordable_symbols, run_recorder
from wta.data.store import Store
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.models import BookSnapshot, Candle, Level, Trade
from wta.utils.timeutil import SimClock

T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z (a whole minute)


def _book(symbol: str, bid: int | str = 100, ask: int | str = 101, ts: float = T0, source: str = "rest"):
    return BookSnapshot(
        symbol=symbol,
        ts=ts,
        source=source,
        bids=[Level(price=Decimal(str(bid)), qty=Decimal("1.5"))],
        asks=[Level(price=Decimal(str(ask)), qty=Decimal("2"))],
    )


def _candles(symbol: str, from_ts: int, to_ts: int) -> list[Candle]:
    first = from_ts - from_ts % 60
    return [
        Candle(symbol=symbol, resolution="1", ts=t, o=Decimal(1), h=Decimal(2), l=Decimal(1), c=Decimal(2), v=Decimal(3))
        for t in range(first, to_ts + 1, 60)
    ]


class StopLoop(Exception):
    """Raised by FakeSleep to end a poller's endless run()."""


class FakeSleep:
    def __init__(self, stop_after: int) -> None:
        self.delays: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            raise StopLoop


# ---------------------------------------------------------------- RestPoller


class FlakyDepthRest:
    """get_all_depth fails ``fail_first`` times (outage), then returns books incl. a non-watchlist one.

    ``get_depth`` knows ``depth_only`` symbols (left out of depth/all) and refuses everything else.
    """

    def __init__(self, fail_first: int = 0, depth_only: tuple[str, ...] = (), depth_error: Exception | None = None) -> None:
        self.fail_first = fail_first
        self.calls = 0
        self.depth_only = depth_only
        self.depth_error = depth_error
        self.depth_calls: list[str] = []

    async def get_all_depth(self) -> dict[str, BookSnapshot]:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise WallexAPIError(None, None, "connection reset")
        return {s: _book(s, source="ws") for s in ("BTCUSDT", "ETHUSDT", "XRPTMN")}

    async def get_depth(self, symbol: str) -> BookSnapshot:
        self.depth_calls.append(symbol)
        if self.depth_error is not None:
            raise self.depth_error
        if symbol not in self.depth_only:
            raise WallexAPIError(400, "invalid_symbol", "unknown market")
        return _book(symbol)


async def test_rest_poller_publishes_watchlist_only_and_survives_errors() -> None:
    bus = MarketDataBus()
    rest = FlakyDepthRest(fail_first=2)
    sleep = FakeSleep(stop_after=3)
    poller = RestPoller(rest, bus, ["BTCUSDT", "ETHUSDT", "DOGETMN"], interval_s=2, clock=SimClock(T0), sleep=sleep)
    with pytest.raises(StopLoop):
        await poller.run()
    assert sleep.delays == [4.0, 8.0, 2.0]  # backoff 2*2**k after failures, then the normal cadence
    assert set(bus.books) == {"BTCUSDT", "ETHUSDT"}  # XRPTMN is not in the watchlist
    assert {b.source for b in bus.books.values()} == {"rest"}  # relabelled
    assert poller.errors == 2 and poller.consecutive_failures == 0 and poller.cycles == 1
    assert bus.sources["rest"].ok and bus.sources["rest"].errors == 2
    assert bus.source_ok("rest", T0, stale_s=30)
    assert rest.depth_calls == ["DOGETMN"] and poller.symbol_errors == 1  # tried once, refused (400)


async def test_rest_poller_fetches_watchlist_symbols_missing_from_depth_all() -> None:
    bus = MarketDataBus()
    rest = FlakyDepthRest(depth_only=("ETHTMN",))
    poller = RestPoller(rest, bus, ["BTCUSDT", "ETHTMN", "ETHUSDT"], clock=SimClock(T0))
    assert await poller.poll_once() == 3
    assert rest.depth_calls == ["ETHTMN"]  # only the symbol depth/all left out
    assert set(bus.books) == {"BTCUSDT", "ETHTMN", "ETHUSDT"} and bus.books["ETHTMN"].source == "rest"
    assert poller.use_depth_all  # depth/all itself works, so the mode stays


async def test_rest_poller_keeps_depth_all_books_when_a_missing_symbol_times_out() -> None:
    bus = MarketDataBus()
    rest = FlakyDepthRest(depth_error=WallexAPIError(None, "timeout", "GET /v1/depth: ReadTimeout"))
    poller = RestPoller(rest, bus, ["DOGETMN", "SHIBTMN", "BTCUSDT"], clock=SimClock(T0))
    assert await poller.poll_once() == 1  # BTCUSDT from depth/all is still published
    assert rest.depth_calls == ["DOGETMN"]  # an outage ends the fill for this poll
    assert poller.symbol_errors == 1 and set(bus.books) == {"BTCUSDT"}


async def test_rest_poller_backoff_is_capped() -> None:
    bus = MarketDataBus()
    sleep = FakeSleep(stop_after=6)
    poller = RestPoller(FlakyDepthRest(fail_first=100), bus, ["BTCUSDT"], interval_s=2, sleep=sleep)
    with pytest.raises(StopLoop):
        await poller.run()
    assert sleep.delays == [4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert not bus.sources["rest"].ok and bus.books == {}


class PerSymbolRest:
    def __init__(self, all_depth_error: Exception, symbol_error: Exception | None = None) -> None:
        self.all_depth_error = all_depth_error
        self.symbol_error = symbol_error
        self.all_depth_calls = 0
        self.depth_calls: list[str] = []

    async def get_all_depth(self) -> dict[str, BookSnapshot]:
        self.all_depth_calls += 1
        raise self.all_depth_error

    async def get_depth(self, symbol: str) -> BookSnapshot:
        self.depth_calls.append(symbol)
        if symbol == "BADTMN":
            raise self.symbol_error or WallexAPIError(400, "invalid_symbol", "unknown market")
        return _book(symbol)


@pytest.mark.parametrize("error", [NotImplementedError(), WallexAPIError(404, None, "not found")])
async def test_rest_poller_falls_back_to_per_symbol_depth(error: Exception) -> None:
    bus = MarketDataBus()
    rest = PerSymbolRest(error)
    poller = RestPoller(rest, bus, ["BTCUSDT", "BADTMN", "ETHUSDT"])
    assert await poller.poll_once() == 2
    assert await poller.poll_once() == 2
    assert rest.all_depth_calls == 1 and not poller.use_depth_all  # switched for good
    assert rest.depth_calls == ["BTCUSDT", "BADTMN", "ETHUSDT"] * 2
    assert poller.symbol_errors == 2  # one rejected symbol never stops the others
    assert set(bus.books) == {"BTCUSDT", "ETHUSDT"}


async def test_rest_poller_outage_errors_propagate_to_the_backoff() -> None:
    server_error = PerSymbolRest(WallexAPIError(500, None, "boom"))
    with pytest.raises(WallexAPIError):
        await RestPoller(server_error, MarketDataBus(), ["BTCUSDT"]).poll_once()
    assert server_error.depth_calls == []  # a 500 on depth/all is not a reason to switch modes

    outage = PerSymbolRest(NotImplementedError(), symbol_error=WallexAPIError(503, None, "unavailable"))
    with pytest.raises(WallexAPIError):
        await RestPoller(outage, MarketDataBus(), ["BADTMN", "BTCUSDT"]).poll_once()
    assert outage.depth_calls == ["BADTMN"]  # a retryable error ends the cycle at once


# ---------------------------------------------------------------- CandlePoller


class CandleRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    async def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> list[Candle]:
        self.calls.append((symbol, resolution, from_ts, to_ts))
        if symbol == "BADTMN":
            raise WallexAPIError(400, None, "unknown market")
        return _candles(symbol, from_ts, to_ts)


async def test_candle_poller_incremental_windows(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    clock = SimClock(T0)
    rest = CandleRest()
    poller = CandlePoller(rest, store, ["BTCUSDT", "BADTMN"], lookback_s=7200, clock=clock)
    first = await poller.poll_once()
    assert first == 121  # two hours of 1-minute bars, both ends included
    assert rest.calls == [("BTCUSDT", "1", int(T0) - 7200, int(T0)), ("BADTMN", "1", int(T0) - 7200, int(T0))]

    clock.advance(65)
    rest.calls.clear()
    assert await poller.poll_once() == 2  # the last stored bar is re-fetched (it may have been forming)
    assert rest.calls[0] == ("BTCUSDT", "1", int(T0), int(T0) + 65)
    assert store.latest_candle_ts("BTCUSDT", "1") == int(T0) + 60

    clock.advance(3 * 86_400)  # after a long outage a single request still spans at most one day
    assert poller.window("BTCUSDT", clock.now()) == (int(clock.now()) - 86_400, int(clock.now()))
    store.close()


async def test_candle_poller_run_never_crashes(tmp_path: Path) -> None:
    class DownRest:
        async def get_candles(self, *args: Any) -> list[Candle]:
            raise ConnectionError("api.wallex.ir unreachable")

    store = Store(tmp_path / "db.sqlite")
    sleep = FakeSleep(stop_after=4)
    poller = CandlePoller(DownRest(), store, ["BTCUSDT"], interval_s=60, clock=SimClock(T0), sleep=sleep)
    with pytest.raises(StopLoop):
        await poller.run()
    assert sleep.delays == [120.0, 240.0, 300.0, 300.0]
    assert poller.errors == 4
    store.close()


# ---------------------------------------------------------------- Recorder


async def test_recorder_dedupes_identical_consecutive_books(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    bus = MarketDataBus()
    recorder = Recorder(store, bus, SimClock(T0), health_dir=tmp_path / "logs", symbols=["BTCUSDT", "ETHUSDT", "SOLTMN"])
    recorder.attach()
    bus.publish_book(_book("BTCUSDT", 100, 101, ts=T0))
    bus.publish_book(_book("BTCUSDT", 100, 101, ts=T0 + 1))  # same levels -> skipped
    bus.publish_book(_book("BTCUSDT", 100, 101, ts=T0 + 2, source="ws"))  # same levels, other source -> skipped
    bus.publish_book(_book("BTCUSDT", 100, 102, ts=T0 + 3))  # changed -> stored
    bus.publish_book(_book("BTCUSDT", 100, 101, ts=T0 + 4))  # changed back -> stored
    bus.publish_book(_book("ETHUSDT", 100, 101, ts=T0 + 4))  # dedupe is per symbol
    bus.publish_trade(Trade(symbol="BTCUSDT", ts=T0 + 4, price=Decimal("100.5"), qty=Decimal("0.1"), side="BUY"))
    assert recorder.dedup_skipped == 2
    assert [b.ts for b in store.iter_books("BTCUSDT")] == [T0, T0 + 3, T0 + 4]
    assert store.book_counts_by_symbol() == {"BTCUSDT": 3, "ETHUSDT": 1}
    assert [t.price for t in store.iter_trades("BTCUSDT")] == [Decimal("100.5")]
    store.close()


async def test_recorder_health_line_rotates_daily_and_prints(tmp_path: Path, capsys) -> None:
    store = Store(tmp_path / "db.sqlite")
    bus = MarketDataBus()
    logs = tmp_path / "logs"
    recorder = Recorder(store, bus, health_dir=logs, symbols=["BTCUSDT", "ETHUSDT"])
    recorder.attach()
    bus.publish_book(_book("BTCUSDT", ts=T0))
    store.upsert_candles(_candles("BTCUSDT", int(T0), int(T0) + 60))
    line = recorder.write_health(T0 + 1, errors={"rest": 3})
    assert line["ts"] == "2026-10-22T10:00:01.000Z"
    assert line["rows_written"] == {
        "since_start": {"books": 1, "trades": 0, "candles": 2},
        "last_min": {"books": 1, "trades": 0, "candles": 2},
    }
    assert line["rest_ok"] is True and line["ws_ok"] is False
    assert line["errors"] == 3 and line["errors_by_source"] == {"recorder": 0, "rest": 3}
    assert line["symbols_seen"] == ["BTCUSDT"] and line["symbols_missing"] == ["ETHUSDT"]
    assert json.loads(capsys.readouterr().out.strip()) == line

    nxt = recorder.write_health(T0 + 86_400)  # next UTC day -> new file
    assert nxt["rows_written"]["last_min"] == {"books": 0, "trades": 0, "candles": 0}
    assert nxt["rows_written"]["since_start"]["books"] == 1
    assert nxt["rest_ok"] is False  # stale
    day1 = (logs / "recorder-20261022.jsonl").read_text().splitlines()
    day2 = (logs / "recorder-20261023.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in day1] == [line] and [json.loads(x) for x in day2] == [nxt]
    store.close()


def test_recorder_listener_never_raises(tmp_path: Path) -> None:
    class BrokenStore(Store):
        def add_book(self, book: BookSnapshot) -> None:
            raise OSError("disk full")

    store = BrokenStore(tmp_path / "db.sqlite")
    bus = MarketDataBus()
    recorder = Recorder(store, bus, health_dir=tmp_path)
    recorder.attach()
    assert bus.publish_book(_book("BTCUSDT"))  # the publisher is not disturbed
    assert recorder.errors == 1
    store.close()


def test_recordable_symbols_drops_banned_and_duplicates() -> None:
    cfg = Config.model_validate({"markets": {"watchlist": ["BTCTMN", "ATMN", "btctmn", "ETH/TMN"], "banned": ["ATMN"]}})
    assert recordable_symbols(cfg) == ["BTCTMN", "ETHTMN"]
    assert recordable_symbols(cfg, ["XRPTMN"]) == ["XRPTMN"]
    with pytest.raises(ValueError):
        recordable_symbols(cfg, ["ATMN"])


# ---------------------------------------------------------------- run_recorder


class FakeMarketRest:
    """Books whose bid moves on every poll (so nothing is deduped), plus candles."""

    def __init__(self) -> None:
        self.polls = 0
        self.candle_calls = 0
        self.closed = False

    async def get_all_depth(self) -> dict[str, BookSnapshot]:
        self.polls += 1
        now = time.time()
        return {s: _book(s, 100 + self.polls, 1000, ts=now) for s in ("BTCUSDT", "ETHUSDT", "XRPTMN")}

    async def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> list[Candle]:
        self.candle_calls += 1
        return _candles(symbol, to_ts - 120, to_ts)

    async def close(self) -> None:
        self.closed = True


def _cfg(tmp_path: Path, ws_enabled: bool = False) -> Config:
    return Config.model_validate(
        {
            "engine": {"heartbeat_interval_s": 0.1, "stale_data_s": 30},
            "data": {
                "sqlite_path": str(tmp_path / "data" / "wta.sqlite"),
                "rest_poll_interval_s": 0.02,
                "candle_poll_interval_s": 0.05,
                "store_flush_interval_s": 0.02,
                "ws_enabled": ws_enabled,
            },
            "markets": {"watchlist": ["BTCUSDT", "ETHUSDT", "ATMN"], "banned": ["ATMN"]},
        }
    )


async def test_run_recorder_writes_rows_and_stops_cleanly(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rest = FakeMarketRest()
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)
    written = await run_recorder(
        cfg,
        Settings(_env_file=None),
        rest=rest,
        stop_event=stop,
        health_dir=tmp_path / "logs",
        handle_signals=False,
    )
    assert written["books"] >= 4 and written["candles"] > 0
    assert rest.polls >= 2 and not rest.closed  # an injected client belongs to the caller

    store = Store.open_readonly(cfg.data.sqlite_path)
    assert set(store.book_counts_by_symbol()) == {"BTCUSDT", "ETHUSDT"}  # watchlist only, banned dropped
    assert store.counts()["books"] == written["books"]
    assert {c.ts % 60 for c in store.load_candles("BTCUSDT", "1")} == {0}
    events = list(store.iter_events())
    assert [e["kind"] for e in events] == ["recorder_start", "recorder_stop"]
    assert events[0]["data"]["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert events[1]["data"]["rows_written"] == written
    store.close()

    health_files = list((tmp_path / "logs").glob("recorder-*.jsonl"))
    assert len(health_files) == 1
    lines = [json.loads(x) for x in health_files[0].read_text().splitlines()]
    assert len(lines) >= 2  # periodic lines + the final one on stop
    assert lines[-1]["rest_ok"] is True and lines[-1]["errors"] == 0
    assert lines[-1]["rows_written"]["since_start"] == written


async def test_run_recorder_restarts_a_crashed_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(collector, "TASK_RESTART_BASE_S", 0.01)
    made: list[FakeWS] = []

    class FakeWS:
        def __init__(self, symbols: list[str], bus: MarketDataBus) -> None:
            self.symbols, self.bus = symbols, bus
            made.append(self)

        async def run(self) -> None:
            if len(made) == 1:
                raise RuntimeError("ws handshake failed")
            self.bus.publish_book(_book("BTCUSDT", 1, 2, ts=time.time(), source="ws"))
            await asyncio.Event().wait()

    cfg = _cfg(tmp_path, ws_enabled=True)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, stop.set)
    await run_recorder(
        cfg,
        Settings(_env_file=None),
        ws_factory=FakeWS,
        rest=FakeMarketRest(),
        stop_event=stop,
        health_dir=tmp_path / "logs",
        handle_signals=False,
    )
    assert len(made) == 2 and made[1].symbols == ["BTCUSDT", "ETHUSDT"]
    store = Store.open_readonly(cfg.data.sqlite_path)
    assert any(b.source == "ws" for b in store.iter_books("BTCUSDT"))
    store.close()
    last = json.loads(next((tmp_path / "logs").glob("recorder-*.jsonl")).read_text().splitlines()[-1])
    assert last["errors_by_source"]["task_crashes"] == 1


class DownRest:
    """api.wallex.ir unreachable: every call fails."""

    async def get_all_depth(self) -> dict[str, BookSnapshot]:
        raise WallexAPIError(None, None, "TLS handshake dropped")

    async def get_candles(self, *args: Any) -> list[Candle]:
        raise WallexAPIError(None, None, "TLS handshake dropped")


async def test_run_recorder_survives_outage_and_stops_on_sigterm(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
    written = await asyncio.wait_for(
        run_recorder(cfg, Settings(_env_file=None), rest=DownRest(), health_dir=tmp_path / "logs"), timeout=5
    )
    assert written == {"books": 0, "trades": 0, "candles": 0}
    last = json.loads(next((tmp_path / "logs").glob("recorder-*.jsonl")).read_text().splitlines()[-1])
    assert last["rest_ok"] is False and last["errors_by_source"]["rest"] >= 1
    assert last["errors_by_source"]["task_crashes"] == 0  # pollers absorb the errors themselves
