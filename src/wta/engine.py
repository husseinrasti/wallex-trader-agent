"""Trading engine orchestrator — PRD-01 smoke version (PRD-02/03/04 add risk, execution and AI).

What runs today:

* data tasks: the REST book poller and, when given, the WebSocket feed publish into the
  ``MarketDataBus`` (the bus keeps whichever source is fresher);
* the fast loop every ``engine.fast_loop_interval_s``. For now it only refreshes book ages and the
  API error window; strategies, risk and execution plug into ``tick()`` later;
* a heartbeat every ``engine.heartbeat_interval_s`` written to ``logs/health.jsonl``;
* ``start`` / ``stop`` records in ``logs/events.jsonl`` and in the store's ``events`` table;
* periodic store flushing.

The engine keeps running when the exchange is unreachable: the pollers retry with backoff, every
task is supervised (a crash is logged and the task restarted), and the heartbeat reports
``rest_ok=false`` with the reason. It stops on SIGINT/SIGTERM, when ``stop_event`` is set, or after
``seconds``.

Live mode is guarded here as well as in the CLI (``wta.safety``): ``run_engine`` and ``Engine.run``
raise ``LiveGuardError`` before any task starts unless ``i_understand_live`` is set, ``WTA_LIVE=1``
is in the environment, an API key is configured and the git tree is clean (or ``allow_dirty``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from wta.config import Config, Settings
from wta.data.collector import RestPoller, make_rest
from wta.data.store import Store
from wta.exchange.base import Exchange, MarketDataBus
from wta.logging_.decision_log import EventsLog
from wta.logging_.health_log import HealthLog
from wta.models import HealthRecord
from wta.safety import check_live_guard, tree_is_dirty
from wta.utils import gitinfo
from wta.utils.timeutil import Clock, SystemClock, iso

log = logging.getLogger(__name__)

TASK_RESTART_BASE_S = 1.0  # supervised task restart backoff: 1, 2, 4, ... s
TASK_RESTART_MAX_S = 60.0
API_ERROR_WINDOW_S = 60.0  # api_errors_1m
STALE_WARNING_EVERY_S = 60.0
WS_STOP_GRACE_S = 2.0


def tradable_watchlist(cfg: Config) -> list[str]:
    """The configured watchlist without banned symbols (the engine never touches those)."""
    return [s for s in dict.fromkeys(cfg.markets.watchlist) if not cfg.markets.is_banned(s)]


class Engine:
    """Owns the loops; every collaborator is injected so tests and other modes can swap them.

    ``rest`` is the market-data client polled by ``RestPoller`` (``get_all_depth``/``get_depth``);
    ``ws`` is any object with ``async run(stop_event)`` (``WallexWS``); ``exchange`` is the order
    interface used from PRD-02 on (live, paper or backtest) and unused here.
    """

    def __init__(
        self,
        cfg: Config,
        settings: Settings,
        mode: str,
        clock: Clock,
        bus: MarketDataBus,
        store: Store,
        exchange: Exchange | None = None,
        rest: Any = None,
        ws: Any = None,
        log_dir: str | Path = "logs",
        *,
        i_understand_live: bool = False,
        allow_dirty: bool = False,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.mode = mode
        self.clock = clock
        self.bus = bus
        self.store = store
        self.exchange = exchange
        self.rest = rest
        self.ws = ws
        self.log_dir = Path(log_dir)
        self.i_understand_live = i_understand_live
        self.allow_dirty = allow_dirty

        self.watchlist = tradable_watchlist(cfg)
        skipped = sorted(set(cfg.markets.watchlist) - set(self.watchlist))
        if skipped:
            log.warning("engine: banned symbols removed from the watchlist: %s", ", ".join(skipped))
        git = gitinfo.get()
        self.git_commit = str(git["commit"])
        self.git_dirty = tree_is_dirty()  # untracked files count too (gitinfo ignores them)
        self.git_tag = git["tag"]
        self.config_hash = cfg.config_hash

        secrets = settings.secret_values()
        self.health_log = HealthLog(self.log_dir, clock, secrets)
        self.events_log = EventsLog(self.log_dir, clock, secrets)
        self.rest_poller: RestPoller | None = None
        if rest is not None:
            self.rest_poller = RestPoller(
                rest,
                bus,
                self.watchlist,
                interval_s=cfg.data.rest_poll_interval_s,
                use_depth_all=cfg.data.rest_poll_use_depth_all,
                clock=clock,
            )

        # runtime state (read by the heartbeat; the TUI/status read it from health.jsonl)
        self.started_ts: float | None = None
        self.ticks = 0
        self.heartbeats = 0
        self.book_ages: dict[str, float | None] = {}
        self.stale_symbols: list[str] = []
        self.task_crashes: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._signal_name: str | None = None
        self._error_samples: deque[tuple[float, int]] = deque()
        self._last_stale_warning = float("-inf")

    # ------------------------------------------------------------ run

    async def run(
        self,
        stop_event: asyncio.Event | None = None,
        seconds: float | None = None,
        *,
        handle_signals: bool = True,
    ) -> str:
        """Run until ``stop_event``, SIGINT/SIGTERM or ``seconds``. Returns the stop reason.

        In live mode the guards run first; ``LiveGuardError`` is raised before anything starts.
        """
        if self.mode == "live":
            check_live_guard(self.i_understand_live, self.allow_dirty, self.settings)
        self._stop = stop_event or asyncio.Event()
        self.started_ts = self.clock.now()
        self._event("start", f"engine started in {self.mode} mode", self._start_info())
        log.info(
            "engine: %s mode, commit %s%s, config %s, %d symbols",
            self.mode,
            self.git_commit[:12],
            " (dirty)" if self.git_dirty else "",
            self.config_hash,
            len(self.watchlist),
        )
        jobs: dict[str, Callable[[], Awaitable[Any]]] = {
            "fast_loop": self._fast_loop,
            "heartbeat": self._heartbeat_loop,
            "flush": self._flush_loop,
        }
        if self.rest_poller is not None:
            jobs["rest"] = self.rest_poller.run
        if self.ws is not None:
            jobs["ws"] = lambda: self.ws.run(self._stop)
        tasks = {n: asyncio.create_task(self._supervise(n, j), name=f"engine-{n}") for n, j in jobs.items()}
        remove_handlers = self._install_signal_handlers() if handle_signals else (lambda: None)
        reason = "stop requested"
        try:
            if seconds is None:
                await self._stop.wait()
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=seconds)
                except TimeoutError:
                    reason = f"ran for {seconds:g} s"
            if self._signal_name:
                reason = f"signal {self._signal_name}"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            remove_handlers()
            self._stop.set()
            if "ws" in tasks:  # let the WebSocket feed close its session cleanly before cancelling
                await asyncio.wait({tasks["ws"]}, timeout=WS_STOP_GRACE_S)
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            self._shutdown(reason)
        return reason

    def _start_info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "git_tag": self.git_tag,
            "config_hash": self.config_hash,
            "watchlist": self.watchlist,
            "rest": self.rest is not None,
            "ws": self.ws is not None,
            "pid": os.getpid(),
        }

    def _shutdown(self, reason: str) -> None:
        """Final heartbeat, ``stop`` event, store flush. Never raises (we are already stopping)."""
        try:
            self.write_heartbeat(self.clock.now())
        except Exception:
            log.exception("engine: final heartbeat failed")
        uptime = self.clock.now() - (self.started_ts or self.clock.now())
        try:
            self._event(
                "stop", reason, {"uptime_s": round(uptime, 3), "ticks": self.ticks, "heartbeats": self.heartbeats}
            )
        except Exception:
            log.exception("engine: could not write the stop event")
        try:
            self.store.flush()
        except Exception:
            log.exception("engine: final store flush failed")
        self.health_log.close()
        self.events_log.close()
        log.info("engine: stopped (%s)", reason)

    async def _supervise(self, name: str, job: Callable[[], Awaitable[Any]]) -> None:
        """Run ``job`` until stop; restart it after a capped backoff if it returns or crashes."""
        failures = 0
        while not self._stop.is_set():
            try:
                await job()
                if self._stop.is_set():
                    return
                log.warning("engine task %s exited unexpectedly; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("engine task %s crashed; restarting", name)
            failures += 1
            self.task_crashes[name] = self.task_crashes.get(name, 0) + 1
            delay = min(TASK_RESTART_BASE_S * 2 ** (failures - 1), TASK_RESTART_MAX_S)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _install_signal_handlers(self) -> Callable[[], None]:
        """SIGINT/SIGTERM set the stop event; returns a function that removes the handlers."""
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread / Windows: Ctrl-C still cancels asyncio.run()

        def remove() -> None:
            for sig in installed:
                loop.remove_signal_handler(sig)

        return remove

    def _on_signal(self, sig: signal.Signals) -> None:
        self._signal_name = sig.name
        self._stop.set()

    # ------------------------------------------------------------ loops

    async def _fast_loop(self) -> None:
        while True:
            self.tick(self.clock.now())
            await asyncio.sleep(self.cfg.engine.fast_loop_interval_s)

    def tick(self, now: float) -> None:
        """One fast-loop iteration. PRD-01: bookkeeping only (book ages, API error window)."""
        self.ticks += 1
        stale_s = self.cfg.engine.stale_data_s
        self.book_ages = {s: self.bus.age(s, now) for s in self.watchlist}
        self.stale_symbols = [s for s, age in self.book_ages.items() if age is None or age > stale_s]
        self._sample_api_errors(now)
        warmed_up = self.started_ts is not None and now - self.started_ts > stale_s
        if self.stale_symbols and warmed_up and now - self._last_stale_warning >= STALE_WARNING_EVERY_S:
            self._last_stale_warning = now
            log.warning(
                "engine: no book newer than %gs for %s", stale_s, ", ".join(self.stale_symbols)
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.engine.heartbeat_interval_s)
            self.write_heartbeat(self.clock.now())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.data.store_flush_interval_s)
            self.store.maybe_flush(self.clock.now())

    # ------------------------------------------------------------ health

    def _api_error_count(self) -> int:
        """Failed API attempts so far: the REST client's counter, else the poller's."""
        total = getattr(self.rest, "errors_total", None)
        if isinstance(total, int):
            return total
        return self.rest_poller.errors if self.rest_poller is not None else 0

    def _sample_api_errors(self, now: float) -> None:
        self._error_samples.append((now, self._api_error_count()))
        # keep one sample at or before the window start, so the delta covers the whole window
        while len(self._error_samples) > 1 and self._error_samples[1][0] <= now - API_ERROR_WINDOW_S:
            self._error_samples.popleft()

    def api_errors_1m(self, now: float) -> int:
        self._sample_api_errors(now)
        return max(0, self._error_samples[-1][1] - self._error_samples[0][1])

    def health_record(self, now: float) -> HealthRecord:
        stale_s = self.cfg.engine.stale_data_s
        ws_ok = self.bus.source_ok("ws", now, stale_s)
        rest_ok = self.bus.source_ok("rest", now, stale_s)
        rest_health = self.rest.health() if callable(getattr(self.rest, "health", None)) else {}
        reasons: list[str] = []
        if not rest_ok:
            last_error = str(rest_health.get("last_error") or "")[:200]
            reasons.append("rest: no fresh data" + (f" (last error: {last_error})" if last_error else ""))
        fatal_close = getattr(self.ws, "fatal_close", None)
        if fatal_close is not None:
            reasons.append(f"ws: stopped by the server (close code {fatal_close}, not retried)")
        elif self.ws is not None and not ws_ok:
            reasons.append("ws: no fresh data")
        if self.stale_symbols:
            reasons.append(f"stale books (> {stale_s:g}s): {', '.join(self.stale_symbols)}")
        consecutive = int(rest_health.get("consecutive_errors") or 0)
        if consecutive >= self.cfg.risk.consecutive_api_errors_pause:
            reasons.append(f"api: {consecutive} consecutive errors")
        poller = self.rest_poller
        extra: dict[str, Any] = {
            "uptime_s": round(now - (self.started_ts or now), 3),
            "ticks": self.ticks,
            "book_age_s": {s: None if a is None else round(a, 3) for s, a in self.book_ages.items()},
            "rest": rest_health,
            "rest_poller": None
            if poller is None
            else {"cycles": poller.cycles, "errors": poller.errors, "consecutive_failures": poller.consecutive_failures},
            "ws": self.ws.stats() if callable(getattr(self.ws, "stats", None)) else None,
            "task_crashes": dict(self.task_crashes),
            "git_dirty": self.git_dirty,
        }
        return HealthRecord(
            ts=iso(now),
            mode=self.mode,
            git_commit=self.git_commit,
            config_hash=self.config_hash,
            equity_tmn=None,  # PRD-02 (portfolio) fills these
            drawdown_pct=None,
            open_orders=0,
            positions=0,
            ws_ok=ws_ok,
            rest_ok=rest_ok,
            api_errors_1m=self.api_errors_1m(now),
            llm_ok=None,
            paused=False,
            killed=False,
            reasons=reasons,
            extra=extra,
        )

    def write_heartbeat(self, now: float) -> HealthRecord:
        record = self.health_record(now)
        self.health_log.write(record)
        self.heartbeats += 1
        fresh = sum(1 for s in self.watchlist if s not in self.stale_symbols)
        log.info(
            "heartbeat: rest_ok=%s ws_ok=%s fresh_books=%d/%d api_errors_1m=%d",
            record.rest_ok,
            record.ws_ok,
            fresh,
            len(self.watchlist),
            record.api_errors_1m,
        )
        return record

    def _event(self, kind: str, reason: str, data: dict[str, Any]) -> None:
        """``events.jsonl`` + the store's ``events`` table. A failing store never blocks the log."""
        self.events_log.log(kind, reason, data)
        try:
            self.store.add_event(self.clock.now(), kind, {"reason": reason, **data})
        except Exception:
            log.exception("engine: could not store event %s", kind)


async def run_engine(
    cfg: Config,
    settings: Settings,
    mode: str = "shadow",
    *,
    seconds: float | None = None,
    log_dir: str | Path = "logs",
    stop_event: asyncio.Event | None = None,
    handle_signals: bool = True,
    i_understand_live: bool = False,
    allow_dirty: bool = False,
) -> int:
    """Build the live components (REST client, optional WebSocket, store) and run the engine.

    ``mode="live"`` runs the live guards (``wta.safety``) before anything is built or started and
    raises ``LiveGuardError`` when one fails.
    """
    if mode == "live":
        check_live_guard(i_understand_live, allow_dirty, settings)
    clock = SystemClock()
    bus = MarketDataBus()
    store = Store(
        cfg.data.sqlite_path,
        flush_interval_s=cfg.data.store_flush_interval_s,
        flush_rows=cfg.data.store_flush_rows,
        depth_levels=cfg.data.depth_levels_stored,
    )
    rest = make_rest(cfg, settings)
    ws = None
    if cfg.data.ws_enabled:
        from wta.exchange.wallex_ws import WallexWS

        ws = WallexWS(
            settings.wallex_ws_url,
            tradable_watchlist(cfg),
            bus,
            clock,
            levels=cfg.data.depth_levels_stored,
            reconnect_max_s=cfg.data.ws_reconnect_max_s,
            stale_s=cfg.engine.stale_data_s,
        )
    engine = Engine(
        cfg,
        settings,
        mode,
        clock,
        bus,
        store,
        rest=rest,
        ws=ws,
        log_dir=log_dir,
        i_understand_live=i_understand_live,
        allow_dirty=allow_dirty,
    )
    try:
        await engine.run(stop_event, seconds, handle_signals=handle_signals)
    finally:
        await rest.close()
        store.close()
    return 0
