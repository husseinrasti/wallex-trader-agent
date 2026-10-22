"""Market data collection: REST pollers, the recorder and the ``python -m wta record`` entry point.

* ``RestPoller``   — polls order books (``/depth/all`` or per-symbol ``/v1/depth``) and publishes
  watchlist books to the ``MarketDataBus`` as ``source="rest"``. It is the WebSocket's fallback and
  must work on its own.
* ``CandlePoller`` — incremental 1-minute candles into the store (upsert, so overlaps are harmless).
* ``Recorder``     — bus listener that writes books (identical consecutive snapshots deduped) and
  trades to the store, and appends a health line every minute to ``logs/recorder-YYYYMMDD.jsonl``.
* ``run_recorder`` — wires everything together and runs for days: every task is supervised and
  restarted after a backoff, and API outages only cause quiet, capped retries.

Nothing here ever raises out of a poll loop; errors are counted, logged and retried.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from wta.config import Config, Settings
from wta.data.store import Store
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.models import BookSnapshot, Trade
from wta.utils.timeutil import Clock, SystemClock, iso, utc_day

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[Any]]
WSFactory = Callable[[list[str], MarketDataBus], Any]  # returns an object with ``async run()``

REST_SOURCE = "rest"
HEALTH_TABLES = ("books", "trades", "candles")
TASK_RESTART_BASE_S = 1.0  # supervised task restart backoff: 1, 2, 4, ... s
TASK_RESTART_MAX_S = 60.0


def _describe(e: BaseException) -> str:
    """Short, key-free error text for logs (WallexAPIError never carries the key; URLs hold none)."""
    text = str(e)
    return (f"{type(e).__name__}: {text}" if text else type(e).__name__)[:300]


# ---------------------------------------------------------------- pollers


class _Poller:
    """Run ``poll_once`` forever: fixed cadence on success, ``min(interval * 2**k, max_backoff)`` after
    the k-th consecutive failure. Only ``CancelledError`` (or an error raised by ``sleep``) escapes."""

    name = "poller"

    def __init__(self, interval_s: float, max_backoff_s: float, clock: Clock | None, sleep: Sleep) -> None:
        self.interval_s = interval_s
        self.max_backoff_s = max_backoff_s
        self.clock: Clock = clock or SystemClock()
        self.sleep = sleep
        self.cycles = 0  # successful polls
        self.errors = 0  # failed polls (total)
        self.consecutive_failures = 0
        self.last_ok_ts: float | None = None

    async def poll_once(self) -> int:
        raise NotImplementedError

    def _on_success(self, now: float) -> None:
        return None

    def _on_failure(self) -> None:
        return None

    async def run(self) -> None:
        while True:
            started = self.clock.now()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors += 1
                self.consecutive_failures += 1
                self._on_failure()
                delay = min(self.interval_s * 2**self.consecutive_failures, self.max_backoff_s)
                self._log_failure(e, delay)
            else:
                if self.consecutive_failures:
                    log.info("%s recovered after %d failed polls", self.name, self.consecutive_failures)
                self.consecutive_failures = 0
                self.cycles += 1
                self.last_ok_ts = self.clock.now()
                self._on_success(self.last_ok_ts)
                delay = max(0.0, self.interval_s - (self.last_ok_ts - started))
            await self.sleep(delay)

    def _log_failure(self, e: Exception, delay: float) -> None:
        # stay quiet during long outages: the first few failures, then every 20th
        k = self.consecutive_failures
        level = logging.WARNING if k <= 3 or k % 20 == 0 else logging.DEBUG
        log.log(level, "%s failed (%d in a row): %s; retry in %.0fs", self.name, k, _describe(e), delay)


class RestPoller(_Poller):
    """Polls order books and publishes only the watchlist ``symbols`` to the bus (``source="rest"``).

    ``rest`` needs ``async get_all_depth() -> dict[str, BookSnapshot]`` and
    ``async get_depth(symbol) -> BookSnapshot``. If ``get_all_depth`` is not implemented or answers
    404, the poller switches to per-symbol requests for good. Watchlist symbols that a depth/all
    answer leaves out (empty or suspended books, other keys) are fetched one by one in the same poll.
    """

    name = "rest poller"

    def __init__(
        self,
        rest: Any,
        bus: MarketDataBus,
        symbols: Iterable[str],
        interval_s: float = 2,
        use_depth_all: bool = True,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        max_backoff_s: float = 30,
    ) -> None:
        super().__init__(interval_s, max_backoff_s, clock, sleep)
        self.rest = rest
        self.bus = bus
        self.symbols = list(symbols)
        self.use_depth_all = use_depth_all
        self.symbol_errors = 0  # failed per-symbol depth requests that did not fail the whole poll

    async def poll_once(self) -> int:
        """One poll; returns the number of books published. Raises if no watchlist book arrived."""
        books = await self._fetch()
        published = 0
        for symbol in self.symbols:
            book = books.get(symbol)
            if book is None:
                continue
            if book.source != REST_SOURCE:
                book = book.model_copy(update={"source": REST_SOURCE})
            self.bus.publish_book(book)
            published += 1
        if published == 0:
            raise RuntimeError("no watchlist book in the depth response")
        return published

    async def _fetch(self) -> dict[str, BookSnapshot]:
        if self.use_depth_all:
            try:
                books = await self.rest.get_all_depth()
            except NotImplementedError:
                reason = "not implemented"
            except WallexAPIError as e:
                if e.status != 404:
                    raise
                reason = "HTTP 404"
            else:
                return await self._fill_missing(books)
            log.warning("rest poller: depth/all unavailable (%s); using per-symbol depth", reason)
            self.use_depth_all = False
        return await self._fetch_each(self.symbols)

    async def _fill_missing(self, books: dict[str, BookSnapshot]) -> dict[str, BookSnapshot]:
        """Add the watchlist books depth/all left out, via ``/v1/depth``. Never raises: the books
        depth/all did return are published either way."""
        out = dict(books)
        for symbol in self.symbols:
            if symbol in out:
                continue
            try:
                out[symbol] = await self.rest.get_depth(symbol)
            except WallexAPIError as e:
                self.symbol_errors += 1
                log.debug("rest poller: depth %s (missing from depth/all) failed: %s", symbol, _describe(e))
                if e.retryable:  # outage / rate limit: keep what we have, try again next poll
                    break
        return out

    async def _fetch_each(self, symbols: list[str]) -> dict[str, BookSnapshot]:
        books: dict[str, BookSnapshot] = {}
        rejected: list[WallexAPIError] = []
        for symbol in symbols:
            try:
                books[symbol] = await self.rest.get_depth(symbol)
            except WallexAPIError as e:
                if e.retryable:  # outage / rate limit: give up this cycle, the backoff handles it
                    raise
                rejected.append(e)  # a 4xx for one symbol must not stop the others
                self.symbol_errors += 1
                log.debug("rest poller: depth %s rejected: %s", symbol, _describe(e))
        if rejected and not books:
            raise rejected[0]
        return books

    def _on_success(self, now: float) -> None:
        self.bus.mark_source(REST_SOURCE, True, now)

    def _on_failure(self) -> None:
        self.bus.mark_source(REST_SOURCE, False)


class CandlePoller(_Poller):
    """Every ``interval_s``: fetch candles from the last stored bar (or ``now - lookback_s``) to now.

    The last stored bar is re-fetched on purpose — it may have been stored while still forming.
    A single request never spans more than ``max_window_s`` (one UDF day); older gaps are
    ``wta backfill``'s job.
    """

    name = "candle poller"

    def __init__(
        self,
        rest: Any,
        store: Store,
        symbols: Iterable[str],
        resolution: str = "1",
        interval_s: float = 60,
        lookback_s: float = 7200,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        max_backoff_s: float = 300,
        max_window_s: int = 86_400,
    ) -> None:
        super().__init__(interval_s, max_backoff_s, clock, sleep)
        self.rest = rest
        self.store = store
        self.symbols = list(symbols)
        self.resolution = resolution
        self.lookback_s = lookback_s
        self.max_window_s = max_window_s

    def window(self, symbol: str, now: float) -> tuple[int, int]:
        """``(from_ts, to_ts)`` of the next request for ``symbol``."""
        to_ts = int(now)
        last = self.store.latest_candle_ts(symbol, self.resolution)
        from_ts = int(now - self.lookback_s) if last is None else last
        return max(from_ts, to_ts - self.max_window_s), to_ts

    async def poll_once(self) -> int:
        """One round over all symbols; returns candles upserted."""
        now = self.clock.now()
        stored = 0
        rejected: list[WallexAPIError] = []
        for symbol in self.symbols:
            from_ts, to_ts = self.window(symbol, now)
            try:
                candles = await self.rest.get_candles(symbol, self.resolution, from_ts, to_ts)
            except WallexAPIError as e:
                if e.retryable:
                    raise
                rejected.append(e)
                continue
            stored += self.store.upsert_candles(candles)
        if rejected and len(rejected) == len(self.symbols):
            raise rejected[0]
        return stored


# ---------------------------------------------------------------- recorder


class Recorder:
    """Writes bus books (deduped per symbol) and trades to the store; emits health lines."""

    def __init__(
        self,
        store: Store,
        bus: MarketDataBus,
        clock: Clock | None = None,
        health_dir: str | Path = "logs",
        health_interval_s: float = 60,
        stale_s: float = 30,
        symbols: Iterable[str] | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.clock: Clock = clock or SystemClock()
        self.health_dir = Path(health_dir)
        self.health_interval_s = health_interval_s
        self.stale_s = stale_s
        self.symbols = list(symbols) if symbols is not None else None
        self.dedup_skipped = 0
        self.errors = 0
        self._last_book: dict[str, BookSnapshot] = {}
        self._start_counts = self._table_counts()
        self._prev_counts = dict(self._start_counts)

    def attach(self) -> None:
        self.bus.on_book(self.on_book)
        self.bus.on_trade(self.on_trade)

    def on_book(self, book: BookSnapshot) -> None:
        """Bus listener. Never raises (it runs inside the publisher's call)."""
        try:
            if book.same_levels(self._last_book.get(book.symbol)):
                self.dedup_skipped += 1
                return
            self._last_book[book.symbol] = book
            self.store.add_book(book)
        except Exception:
            self.errors += 1
            log.exception("recorder: could not store book %s", book.symbol)

    def on_trade(self, trade: Trade) -> None:
        try:
            self.store.add_trade(trade)
        except Exception:
            self.errors += 1
            log.exception("recorder: could not store trade %s", trade.symbol)

    def _table_counts(self) -> dict[str, int]:
        return {t: self.store.rows_written[t] for t in HEALTH_TABLES}

    def rows_since_start(self) -> dict[str, int]:
        counts = self._table_counts()
        return {t: counts[t] - self._start_counts[t] for t in HEALTH_TABLES}

    def health_line(self, now: float, errors: dict[str, int] | None = None) -> dict[str, Any]:
        """Health snapshot; ``last_min`` counts rows since the previous health line."""
        try:
            self.store.flush()  # so the counts include everything received so far
        except sqlite3.Error:
            self.errors += 1
            log.exception("recorder: flush failed")
        counts = self._table_counts()
        last = {t: counts[t] - self._prev_counts[t] for t in HEALTH_TABLES}
        self._prev_counts = counts
        by_source = {"recorder": self.errors, **(errors or {})}
        line: dict[str, Any] = {
            "ts": iso(now),
            "kind": "recorder_health",
            "rows_written": {"since_start": self.rows_since_start(), "last_min": last},
            "ws_ok": self.bus.source_ok("ws", now, self.stale_s),
            "rest_ok": self.bus.source_ok(REST_SOURCE, now, self.stale_s),
            "errors": sum(by_source.values()),
            "errors_by_source": by_source,
            "symbols_seen": sorted(self._last_book),
            "dedup_skipped": self.dedup_skipped,
            "pending_rows": self.store.pending_rows,
        }
        if self.symbols is not None:
            line["symbols_missing"] = sorted(set(self.symbols) - set(self._last_book))
        return line

    def write_health(self, now: float, errors: dict[str, int] | None = None) -> dict[str, Any]:
        """Append the health line to ``recorder-YYYYMMDD.jsonl`` (UTC day) and print it to stdout."""
        line = self.health_line(now, errors)
        text = json.dumps(line, separators=(",", ":"))
        path = self.health_dir / f"recorder-{utc_day(now)}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            log.exception("recorder: could not write %s", path)
        print(text, flush=True)
        return line


# ---------------------------------------------------------------- entry point


def recordable_symbols(cfg: Config, symbols: Iterable[str] | None = None) -> list[str]:
    """``symbols`` (or the watchlist), upper-cased, de-duplicated, banned markets removed."""
    chosen = [s.upper().replace("/", "") for s in (symbols or cfg.markets.watchlist)]
    banned = [s for s in chosen if cfg.markets.is_banned(s)]
    if banned:
        log.warning("recorder: skipping banned symbols %s", ", ".join(banned))
    result = list(dict.fromkeys(s for s in chosen if s not in banned))
    if not result:
        raise ValueError("no symbols to record (empty or banned watchlist)")
    return result


def make_rest(cfg: Config, settings: Settings) -> Any:
    """The live Wallex REST client (market-data calls only are used by the recorder)."""
    from wta.exchange.wallex_rest import WallexREST  # imported lazily: tests inject a fake

    return WallexREST(
        settings.wallex_base_url,
        settings.wallex_api_key.get_secret_value(),
        rps=cfg.data.rate_limit_rps,
        timeout=cfg.data.http_timeout_s,
        max_retries=cfg.data.max_retries,
        sub_account_client_id=settings.wallex_sub_account_client_id,
    )


def default_ws_factory(cfg: Config, settings: Settings) -> WSFactory:
    """Factory for the Centrifugo WebSocket feed, for ``run_recorder(ws_factory=...)``."""
    from wta.exchange.wallex_ws import WallexWS

    def factory(symbols: list[str], bus: MarketDataBus) -> Any:
        return WallexWS(
            settings.wallex_ws_url,
            symbols,
            bus,
            levels=cfg.data.depth_levels_stored,
            reconnect_max_s=cfg.data.ws_reconnect_max_s,
            stale_s=cfg.engine.stale_data_s,
        )

    return factory


async def _supervise(
    name: str, job: Callable[[], Awaitable[Any]], stop: asyncio.Event, crashes: dict[str, int]
) -> None:
    """Run ``job`` until ``stop``; restart it after a capped exponential backoff if it ends or crashes."""
    failures = 0
    while not stop.is_set():
        try:
            await job()
            log.warning("recorder task %s exited unexpectedly; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("recorder task %s crashed; restarting", name)
        failures += 1
        crashes[name] = crashes.get(name, 0) + 1
        delay = min(TASK_RESTART_BASE_S * 2 ** (failures - 1), TASK_RESTART_MAX_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


def _install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM set ``stop``; returns a function that removes the handlers again."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows / not the main thread: Ctrl-C still cancels asyncio.run()

    def remove() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return remove


async def _close_quietly(rest: Any) -> None:
    close = getattr(rest, "aclose", None) or getattr(rest, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        log.warning("recorder: closing the REST client failed: %s", _describe(e))


async def run_recorder(
    cfg: Config,
    settings: Settings,
    symbols: list[str] | None = None,
    ws_factory: WSFactory | None = None,
    rest: Any = None,
    stop_event: asyncio.Event | None = None,
    *,
    health_dir: str | Path = "logs",
    handle_signals: bool = True,
) -> dict[str, int]:
    """Record market data until ``stop_event`` is set (or SIGINT/SIGTERM). Returns rows written.

    Tasks: REST book poller, candle poller, store flush loop, health loop and — when
    ``cfg.data.ws_enabled`` and ``ws_factory`` is given — the WebSocket feed. Each task is
    supervised: a crash is logged and the task restarted after a backoff (1 s doubling to 60 s).
    """
    clock = SystemClock()
    stop = stop_event or asyncio.Event()
    symbols = recordable_symbols(cfg, symbols)
    own_rest = rest is None
    if rest is None:
        rest = make_rest(cfg, settings)
    store = Store(
        cfg.data.sqlite_path,
        flush_interval_s=cfg.data.store_flush_interval_s,
        flush_rows=cfg.data.store_flush_rows,
        depth_levels=cfg.data.depth_levels_stored,
    )
    bus = MarketDataBus()
    recorder = Recorder(
        store,
        bus,
        clock,
        health_dir=health_dir,
        health_interval_s=cfg.engine.heartbeat_interval_s,
        stale_s=cfg.engine.stale_data_s,
        symbols=symbols,
    )
    recorder.attach()
    rest_poller = RestPoller(
        rest,
        bus,
        symbols,
        interval_s=cfg.data.rest_poll_interval_s,
        use_depth_all=cfg.data.rest_poll_use_depth_all,
        clock=clock,
    )
    candle_poller = CandlePoller(rest, store, symbols, interval_s=cfg.data.candle_poll_interval_s, clock=clock)
    crashes: dict[str, int] = {}

    def errors() -> dict[str, int]:
        ws_state = bus.sources.get("ws")
        return {
            "rest": rest_poller.errors,
            "candles": candle_poller.errors,
            "ws": ws_state.errors if ws_state else 0,
            "task_crashes": sum(crashes.values()),
        }

    async def flush_loop() -> None:
        while True:
            await asyncio.sleep(cfg.data.store_flush_interval_s)
            store.maybe_flush(clock.now())

    async def health_loop() -> None:
        while True:
            await asyncio.sleep(recorder.health_interval_s)
            recorder.write_health(clock.now(), errors())

    jobs: dict[str, Callable[[], Awaitable[Any]]] = {
        "rest": rest_poller.run,
        "candles": candle_poller.run,
        "flush": flush_loop,
        "health": health_loop,
    }
    if cfg.data.ws_enabled and ws_factory is not None:
        jobs["ws"] = lambda: ws_factory(symbols, bus).run()

    start_info = {"symbols": symbols, "tasks": sorted(jobs), "config_hash": cfg.config_hash}
    store.add_event(clock.now(), "recorder_start", start_info)
    log.info("recorder: %d symbols -> %s (tasks: %s)", len(symbols), cfg.data.sqlite_path, ", ".join(jobs))
    tasks = [
        asyncio.create_task(_supervise(name, job, stop, crashes), name=f"recorder-{name}")
        for name, job in jobs.items()
    ]
    remove_handlers = _install_signal_handlers(stop) if handle_signals else (lambda: None)
    try:
        await stop.wait()
        log.info("recorder: stop requested")
    finally:
        remove_handlers()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        now = clock.now()
        recorder.write_health(now, errors())
        written = recorder.rows_since_start()
        store.add_event(now, "recorder_stop", {"rows_written": written})
        store.close()
        if own_rest:
            await _close_quietly(rest)
        log.info("recorder: stopped cleanly (%s)", ", ".join(f"{k}={v}" for k, v in written.items()))
    return written
