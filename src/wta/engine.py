"""Trading engine orchestrator: data -> risk -> execution, plus the guardian loops (PRD-01, PRD-02 §5-§8).

Two shapes, one class:

* **Monitoring only** (no ``exchange``; PRD-01): data tasks, heartbeats, events, store flushing.
* **Trading** (an ``exchange`` and its ``markets``): everything below. ``shadow`` mode trades on a
  ``PaperExchange`` that fills against the live (or synthetic) books; ``live`` mode trades on the
  Wallex REST client and is reachable only through the live guard (``wta.safety``), which runs in
  the CLI, in ``run_engine``/``build_engine`` and again in ``Engine.run``.

Startup (PRD-02 §5), in ``startup()``: the ``start`` event (``reason`` = ``restart`` when the store
holds earlier engine state, else ``fresh``) -> ``startup_restore`` (peak equity, day-start equity,
fees, order counters, realized PnL, kill/pause/flatten flags, open positions and live orders; a
restart resets none of them) -> a pending manual kill reset is applied -> the startup reconcile
against the exchange -> a ``restart`` event with both summaries. Then the loops start. If the
restore raises (e.g. ``database is locked``), nothing is ever persisted from that process state —
not by the ticks, not by ``shutdown()`` — because defaults (``killed=False``, no peak, empty strategy
and feature state) would overwrite the saved rows; the next tick runs the whole startup again.
``run()`` starts the market-data tasks *before* ``startup()`` and waits (at most
``engine.stale_data_s``) for the books the reconcile needs (USDTTMN and the symbols of our
positions and orders): every fill or close it books is valued in TMN, never at 0. If they do not
arrive in time the startup reconcile runs at the first tick that has them, and strategies wait.

Fast tick (every ``engine.fast_loop_interval_s``, PRD-02 §8), in ``tick(now)``:

1. ``books`` = the bus snapshot of the watchlist plus every book the flatten routine, positions and
   open orders need; they are recorded first (replay inputs, also for a tick that crashes later).
   The paper exchange matches its open orders against exactly these books (shadow mode).
2. ``portfolio.refresh`` (equity, peak, UTC day start) -> ``risk.update_rolling_state`` -> kill /
   pause / resume events are applied to ``EngineFlags``. The ``KILL`` file in the repo root kills too.
   While killed, a reset written by ``wta reset-kill`` is picked up from the store (at the very
   start of the tick).
3. Deadline phases by clock: ``flatten_start`` (T-2h) cancels entry/rebalance orders and starts the
   maker-first flatten, ``flatten_final`` (T-30min) sells leftovers with MARKET orders,
   ``final_state`` is logged at T-5min, and at the challenge end every open order is canceled and
   no order is placed any more.
4. ``order_manager.poll`` (fills, re-prices, crosses, protective stops). Balances are then re-read
   from the exchange whenever fills, placements or cancels happened: the exchange is the truth,
   the OrderManager's optimistic update only bridges the gap. (Re-reading *before* the poll would
   count paper fills twice: the exchange has them, the OrderManager applies them again.)
5. Intents, each through ``risk.evaluate`` -> ``order_manager.submit``: exits of positions
   (software stops, targets, time stops), flatten SELLs (deadline phases or kill-switch),
   strategy intents (below), intents from ``intent_sources`` (tests/tools) and acceptance-test
   intents injected from a JSONL file.
6. Scheduler: reconcile every ``reconcile_interval_s`` and after an API error burst; an equity row
   every ``equity_row_interval_s``; kv state (portfolio, flags, orders, paper exchange, strategy
   and feature state) whenever it changed; the books and tick used (replay inputs,
   ``engine_books`` / ``ticks`` tables).

Strategies (PRD-03; ``strategies`` = S1 + S2 always, S3 when ``strategies.s3_momentum.enabled``),
inside step 5 of every tick, after the order poll and the balance refresh:

a. ``FeatureEngine.compute(now, books, candles, watchlist)`` (every tick, so the cross-book
   deviation history behind ``dev_zscore`` is sampled evenly) and a ``PortfolioView``; together with
   the markets, the plan in force (``current_plan``: the slow loop's latest plan while it is fresh,
   taken once at the tick's start; always None with ``ai.enabled: false``), the config,
   ``engine.rng_seed`` and the flatten phase they form the tick's ``StrategyContext``. One id counter per tick is shared by the PositionManager exits and
   every strategy intent, so decision ids never collide.
b. Hooks, with that context, before ``on_tick``: every ``FillEvent`` of the poll goes to the
   strategy that owns the order (``on_fill(fill, position, ctx)``; ``position`` is the position
   after the fill, the closed position's last state when the fill closed it, or None for fills
   without a position such as S2 rebalances), every finished order to ``on_order_done`` with its
   *logical* fill (``ManagedOrder.logical_filled`` across all re-placements). The router notes
   every fill (symbol cooldown). Hook intents (S1 leg 2 or its degrade stop) go through risk ->
   submit at once, in the same tick. After positions close, ``prune`` drops their strategy state.
c. ``router.tick(ctx, exits)`` (the PositionManager exits first, then S1 -> S2 -> S3, filtered for
   one strategy per symbol, one open order per (strategy, symbol) and the symbol cooldown) ->
   ``router.apply_cooldown`` -> risk -> submit. The portfolio view is rebuilt after the hook
   intents, so ``on_tick`` sees the orders they placed. Every decision record of a strategy batch
   carries ``FeatureSnapshot.compact`` of the symbols the intent used (``features.books_used``) and
   the plan snapshot. An intent the router drops gets a ``no_trade`` record (repeats suppressed
   like vetoes).
d. Not asked while killed, in a flatten phase, during a manual flatten (``flatten_now``) or before a
   deferred startup reconcile (Phase-2 semantics): exits and flatten orders only. Hooks are still
   delivered (bookkeeping); intents they return are dropped then (S1 returns none in a flatten
   phase anyway and retries leg 2 on its next tick).
e. Each heartbeat puts compact diagnostics into ``health.extra.strategies``: S1 per coin
   edge / cost / need / why-no-trade plus the engine's opportunity-tick counters, S2 target /
   current / status, S3 status.

Candles: ``candles`` (``CandleCache``) holds the last 26 h of 1-minute candles per watchlist
symbol in memory (missing minutes read as flat bars); the fast loop never queries SQLite for
candles. It is loaded from the state DB at startup (per symbol the bars within 26 h of its newest
stored bar: the cache's own trim rule). Feed ``live``: a supervised ``CandleRefresher`` task
backfills the window through ``rest.get_candles`` in requests of at most one day and refreshes
every ``data.candle_poll_interval_s``. Feed ``synthetic``: ``SyntheticFeed.on_candle`` ->
``ingest_candles``. Either way every new or changed bar goes into the state DB twice: ``candles``
(latest version, for the next startup) and the append-only ``engine_candles`` with a sequence
number. Each tick takes its candle snapshot together with its book snapshot, at the start of the
tick, and its ``ticks`` row records ``candle_seq``: a replay applies the ``engine_candles`` rows
with ``seq <= candle_seq`` to an empty ``CandleCache`` and gets exactly the candles the tick's
features were computed from (a refresh that lands during the tick's awaits is in the next tick).

State DB per mode and feed: the engine's store is ``state_db_path(cfg, mode, feed)`` —
``data.state_sqlite_path`` with ``{mode}`` replaced (default ``data/state_shadow.sqlite`` /
``data/state_live.sqlite``; ``data/state_shadow_synthetic.sqlite`` on the synthetic feed; a path
without ``{mode}`` gets ``.<mode>`` inserted before its suffix), so a shadow run, a synthetic
exercise and the live account never share kv state, positions, orders, equity or candles. The
recorder keeps ``data.sqlite_path``. When only ``data.sqlite_path`` is overridden (``--set``, e.g.
``make shadow-synth``) and ``data.state_sqlite_path`` is left at its default, the state DB is
derived from it: ``<dir>/<stem>.state_{mode}.sqlite`` (``data/shadow_synth.sqlite`` ->
``data/shadow_synth.state_shadow.sqlite``), so a separate market DB still means a separate
account, per mode, and never the default ``data/state_shadow.sqlite``. kv keys ``strategy_state`` (``router.state()``, every tick it changed) and
``feature_state`` (``FeatureEngine.state()``, at most every ``FEATURE_STATE_EVERY_S`` and at
shutdown: it holds an hour of deviation samples) are restored at startup.

Guardian rules the engine adds on top of the RiskManager:

* **Kill** (drawdown >= 5 %, ``KILL`` file, or >= ``exception_storm_count`` fast-loop crashes
  within ``exception_storm_window_s``): ``killed`` is persisted, entry/rebalance orders are
  canceled, the account is flattened to TMN, and the process keeps running in monitoring mode
  (heartbeats; only exit/stop/flatten orders). Only ``wta reset-kill --reason ...`` clears it; the
  reset also moves the peak to the current equity, so the old drawdown does not kill again at once.
* **Closing SELL of a position with a server stop.** The stop locks the coins on the exchange. The
  OrderManager cancels it right before the SELL (and refuses the SELL if that cancel fails), so the
  risk check sees the base balance as it will be at that moment: the stop's locked quantity counts
  as free for that one evaluation.
* **Repeated vetoes.** An intent vetoed for the same checks as the same intent less than
  ``VETO_REPEAT_S`` ago is not logged again (a software stop vetoed every 2 s would flood the log);
  the next logged veto carries ``veto_repeats_suppressed``.
* **Pause** (daily loss, anomaly): entry/rebalance orders still working are canceled, so nothing
  keeps re-pricing an entry without a risk check while paused.
* **Kill reset**: when no deadline or manual flatten is running, the kill-switch's flatten orders
  are canceled with it (the operator decided to resume, not to keep selling).

Persistence: one fast tick is **one SQLite commit** (``Store.transaction``): orders, fills,
positions and the kv state (portfolio, flags, orders, paper exchange, strategy state; the feature
state when it is due) always move together, so a crash restores the state as of the end of a tick
(the z-score history may miss up to ``FEATURE_STATE_EVERY_S`` of samples, which barely moves an
hour-long mean). A strategy that raises crashes the tick like any engine bug (``safe_tick``
counts it; a storm kills and flattens): a broken strategy must not keep trading, and unhedged S1
coins must not be left alone. Exceptions: right before an order is sent the
OrderManager's write-ahead record is committed together with the kv state of that moment
(``_checkpoint``), and the kill-switch commits at once. A failing commit is logged, its rows stay
pending and the next tick retries them: storage trouble never counts as a tick crash.

Stopping: a stop (signal, ``--seconds``, flatten complete) never interrupts a tick — an order in
flight would be lost. The fast loop ends between two ticks and ``run()`` waits for it before the
other tasks are cancelled.

One process per account: ``build_engine`` takes an exclusive lock next to the state DB
(``<state db>.engine.lock``), so ``wta run`` and ``scripts/flatten_now.py`` can never trade the
same account (or paper account) at once. Live mode also refuses to start without
``engine.challenge_end_utc`` (``CHALLENGE_END_UTC``): without it the mandatory flatten to 100 % TMN
before the end would never run.

Determinism: time comes only from the injected clock (``tick(now)`` in tests, replays and
backtests). While a tick runs, the clock every component reads (OrderManager, PaperExchange,
Reconciler, logs) is frozen at the tick's ``now`` (``TickClock``), so client ids, placement times
and timers derive from the recorded tick time alone. The market-data feeds keep the real clock.
Dicts are walked in sorted order. Every task is supervised (a crash is logged and the task
restarted) and a crashing tick never stops the loop (``safe_tick``).

Synthetic data (``feed="synthetic"``) is an offline exercise of the whole engine, never evidence of
performance: the ``start`` event and every heartbeat say so.

AI layer (PRD-04 §8; ``enable_ai``, attached by ``build_engine``): the slow loop and the reviewer are
supervised tasks of their own; the fast loop never awaits them. ``wta.ai.wiring.build_llm`` picks the
source once — ``llm`` (OpenRouter primary, Gemini fallback, per ``configs/llm.yaml`` and the env
models, daily call budgets) when a key is set, ``offline-rules`` (deterministic rules, NOT an LLM,
labelled so everywhere) when none is, ``disabled`` with ``ai.enabled: false`` (no analyst, plan always
None: the ablation). Every ``SLOW_LOOP_POLL_S`` the slow loop asks ``analyst.should_run`` (startup,
``engine.slow_loop_interval_s``, kill/pause/flatten-start events, fast dollar/BTC moves) and runs one
cycle with a timeout on a view captured at the cycle start (features, books, candles, trades, PnL,
the last 50 decisions, health). A plan is persisted (``plans`` + a ``decisions.jsonl`` record
``kind="plan"`` stamped with the cycle time) and swapped into ``plan``; the next tick uses it. A failed
cycle keeps the last plan until it is stale, counts ``llm_consecutive_failures`` and shows
``llm_ok=false``. Every ``engine.reviewer_interval_s`` the reviewer reads the last hour of this run's
logs, writes ``<log_dir>/journal.md`` and may apply <= 2 bounded changes (``ConfigManager``) →
``reload_config`` at the next tick boundary (strategy configs, router, FeatureEngine, analyst; risk
and execution never; a running analyst cycle keeps its own config) → the decision log stamps the new
``config_hash``. At startup ``build_engine`` re-applies the reviewer's ``latest.yaml`` of the
account's own runtime directory (``configs/runtime/<mode>[_<feed>]``; editable paths within bounds
only; the operator's ``--set`` / ``--overlay`` keys win and are never offered to the reviewer; not at
all with ``ai.enabled: false``).
Telegram (``enable_telegram``, optional) only sends alerts and takes /status /pause /resume /kill, which
are applied at the start of the next tick and logged with ``source: "telegram"``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import os
import signal
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any, get_args

try:  # POSIX only; without it (Windows) the one-engine-per-account lock is skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from wta.ai.analyst import MAX_CALLS_PER_ROUND, RegimeAnalyst
from wta.ai.cache import CacheMissError
from wta.ai.config_manager import ConfigManager
from wta.ai.mcp_client import MCPClient
from wta.ai.offline_provider import OFFLINE_STATUS
from wta.ai.reviewer import PostTradeReviewer
from wta.ai.reviewer import _compact_decision as compact_decision
from wta.ai.tools import InternalTools, ToolDeps, feature_view, to_jsonable
from wta.ai.wiring import (
    AI_DISABLED,
    AI_LLM,
    AI_OFFLINE,
    MCP_TIMEOUT_S,
    RUNTIME_LATEST,
    LLMSetup,
    analyst_requests_per_cycle,
    build_llm,
    build_mcp,
    llm_cache_dir,
    load_runtime_overlay,
    runtime_dir_for,
)
from wta.config import ROOT, Config, Settings, account_label, mask_secrets, state_db_path
from wta.data.collector import CANDLE_CACHE_KEEP_S, CandleCache, CandleRefresher, RestPoller, make_rest
from wta.data.store import Store
from wta.exchange.base import Exchange, MarketDataBus, WallexAPIError
from wta.exchange.paper import PaperExchange
from wta.execution.order_manager import OrderManager, PollResult
from wta.execution.positions import (
    CLOSING_PURPOSES,
    FIAT,
    USDTTMN,
    ClosedPosition,
    PortfolioState,
    PositionManager,
)
from wta.execution.reconcile import (
    KV_FLAGS,
    KV_ORDER_MANAGER,
    KV_PORTFOLIO,
    Reconciler,
    ReconcileReport,
    startup_restore,
)
from wta.features.engine import FeatureEngine, FeatureSnapshot
from wta.logging_.decision_log import DecisionLog, EventsLog, OrdersLog
from wta.logging_.health_log import HealthLog, _reverse_lines
from wta.logging_.jsonl import family_files
from wta.models import (
    D0,
    Balance,
    BookSnapshot,
    Candle,
    HealthRecord,
    Intent,
    MarketInfo,
    MarketPlan,
    OrderType,
    Purpose,
    Side,
    Trade,
)
from wta.risk.context import Deadline, EngineFlags, RecentIntent, RiskContext
from wta.risk.flatten import FlattenManager, cancel_at_flatten_start, flatten_phase_transitions
from wta.risk.manager import RiskEvent, RiskManager
from wta.safety import LiveGuardError, check_live_guard, deadline_guard_reason, tree_is_dirty
from wta.status import format_status, status_for_config
from wta.strategies.base import PortfolioView, Strategy, StrategyContext
from wta.strategies.router import StrategyRouter
from wta.strategies.s1_crossbook import S1CrossBook
from wta.strategies.s2_usdtalloc import S2USDTAlloc
from wta.strategies.s3_momentum import S3Momentum
from wta.utils import gitinfo
from wta.utils import telegram as tg
from wta.utils.decimalutil import D, pct
from wta.utils.ids import OUR_PREFIXES, make_decision_id, strategy_prefix
from wta.utils.timeutil import Clock, SimClock, SystemClock, iso, parse_iso

log = logging.getLogger(__name__)

TASK_RESTART_BASE_S = 1.0  # supervised task restart backoff: 1, 2, 4, ... s
TASK_RESTART_MAX_S = 60.0
API_ERROR_WINDOW_S = 60.0  # api_errors_1m
STALE_WARNING_EVERY_S = 60.0
WS_STOP_GRACE_S = 2.0
FAST_LOOP_STOP_GRACE_S = 60.0  # a stop waits this long for the running tick to finish
STARTUP_BOOKS_POLL_S = 0.2

KV_PAPER = "paper_state"  # PaperExchange.state() of a shadow run
KV_KILL_RESET = "kill_reset"  # the last manual kill reset (written by ``wta reset-kill``)
KILL_FILE = ROOT / "KILL"  # create it to kill the running engine
VETO_REPEAT_S = 60.0  # identical vetoes of the same intent are logged at most this often
FEEDS = ("live", "synthetic")
SYNTHETIC_NOTE = "synthetic market data: an offline exercise of the engine, never evidence of performance"
KV_STRATEGY = "strategy_state"  # StrategyRouter.state(): router + every strategy (replay / restart)
KV_FEATURES = "feature_state"  # FeatureEngine.state(): the rolling cross-book deviation history
FEATURE_STATE_EVERY_S = 60.0  # the feature state is ~one hour of samples: saved at most this often
CANDLE_RESOLUTION = "1"
ROUTER_DROPS_SHOWN = 10  # router drops listed in a heartbeat
S1_COIN_KEYS = (  # S1 diagnostics per coin that a heartbeat carries
    "edge_tmn_cheap_pct",
    "cost_tmn_cheap_pct",
    "need_tmn_cheap_pct",
    "edge_usdt_cheap_pct",
    "cost_usdt_cheap_pct",
    "need_usdt_cheap_pct",
    "dev_mid_pct",
    "dev_zscore",
    "dev_samples",
    "why_no_trade",
    "decision",
)

# ---- AI layer (PRD-04 §8) and alerts (PRD-06 §2)
SLOW_LOOP_POLL_S = 5.0  # the slow loop asks ``analyst.should_run`` this often (it enforces the cadence)
SLOW_LOOP_FIRST_WAIT_S = 60.0  # the first cycle waits this long at most for the first features
REVIEW_POLL_S = 5.0
REVIEW_WINDOW_S = 3600.0  # the reviewer reads the last hour (PRD-04 §7)
REVIEW_LOG_MAX_LINES = 50_000  # per log family and review
RECENT_DECISIONS = 50  # ring buffer behind the get_recent_decisions tool
RECENT_EVENTS = 10  # events shown in the analyst's first message
TRADES_KEPT = 200  # market trades per symbol kept in memory for the *_latest_trades_local tool
AI_TRIGGER_EVENTS = frozenset({"kill", "pause", "flatten"})  # analyst.should_run decides which count
LLM_FAILURE_ALERT = 3  # consecutive failed analyst cycles before a Telegram alert
STALE_ALERT_S = 120.0  # Telegram: a watchlist book older than this (after warm-up)
API_ERRORS_ALERT_1M = 10  # Telegram: failed API attempts per minute in a heartbeat
EQUITY_ALERT_EVERY_S = 3600.0
ALERT_DRAIN_S = 5.0
MANUAL_PAUSE_PREFIX = "telegram:"  # a /pause; the RiskManager treats it as "other" (never auto-resumed)
STRATEGY_CFG = {"S1": "s1_crossbook", "S2": "s2_usdtalloc", "S3": "s3_momentum"}
REQUIRED_PLAN_BOOKS = ("USDTTMN", "BTCUSDT", "ETHUSDT", "SOLUSDT")  # the analyst's regime symbols

IntentSource = Callable[[RiskContext], list[Intent]]


@dataclass
class _AIView:
    """What the analyst's tools read during one cycle: captured at the cycle start, so every tool
    answer of a cycle comes from the same moment (ticks keep running meanwhile)."""

    now: float
    features: FeatureSnapshot | None
    books: dict[str, BookSnapshot]
    candles: dict[str, list[Candle]]
    trades: dict[str, list[Trade]]
    pnl: dict[str, Any]
    decisions: list[dict[str, Any]]
    health: dict[str, Any]


def _record_ts(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


def read_log_window(
    log_dir: Path,
    base: str,
    since: float,
    max_lines: int = REVIEW_LOG_MAX_LINES,
    until: float | None = None,
) -> list[dict[str, Any]]:
    """Records of a JSONL log family with ``since <= ts <= until``, oldest first (read backwards from
    the end, so a long run costs only the window). Lines that do not parse are skipped.

    ``until`` (the review's ``now``): the files are read while the engine keeps appending, so
    anything stamped later — and an undated line written after the last record inside the window —
    is left out; the same ``now`` then always reads the same records."""
    out: list[dict[str, Any]] = []
    seen = 0
    inside = until is None  # reading backwards: True once a dated record <= until was seen
    for path in reversed(family_files(log_dir, base)):
        for line in _reverse_lines(path):
            seen += 1
            if seen > max_lines:
                return list(reversed(out))
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            t = _record_ts(rec.get("ts"))
            if t is not None and t < since:
                return list(reversed(out))
            if until is not None:
                if t is not None and t > until:
                    continue
                if t is not None:
                    inside = True
                elif not inside:
                    continue
            out.append(rec)
    return list(reversed(out))


def default_strategies(cfg: Config) -> list[Strategy]:
    """S1 and S2 always (each checks its own ``enabled`` flag every tick, so its diagnostics still
    report why it is idle); S3 only when ``strategies.s3_momentum.enabled``."""
    out: list[Strategy] = [
        S1CrossBook(cfg.strategies.s1_crossbook),
        S2USDTAlloc(cfg.strategies.s2_usdtalloc),
    ]
    if cfg.strategies.s3_momentum.enabled:
        out.append(S3Momentum(cfg.strategies.s3_momentum, ai_enabled=cfg.ai.enabled))
    return out


class EngineLockError(RuntimeError):
    """Another engine (or flatten_now) already runs on this store/account; nothing was started."""


class TickClock:
    """A clock that is frozen at the current tick's ``now`` while a tick runs.

    Every component that reads the clock during a tick (OrderManager, PaperExchange, Reconciler,
    logs) then sees exactly the tick's timestamp, so a replay that sets each tick's ``now`` gets the
    same client ids, placement times and timers. Outside a tick it reads the wrapped clock.
    """

    def __init__(self, base: Clock) -> None:
        self.base = base
        self._frozen: float | None = None

    def now(self) -> float:
        return self._frozen if self._frozen is not None else self.base.now()

    @contextmanager
    def frozen(self, now: float) -> Iterator[None]:
        previous, self._frozen = self._frozen, now
        try:
            yield
        finally:
            self._frozen = previous


def acquire_engine_lock(sqlite_path: str) -> IO[str] | None:
    """Exclusive, non-blocking lock ``<sqlite_path>.engine.lock`` (released when the file closes).

    Raises ``EngineLockError`` when another process holds it. None for an in-memory store or a
    platform without ``fcntl``.
    """
    if fcntl is None or sqlite_path == ":memory:":
        return None
    path = Path(f"{sqlite_path}.engine.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "?"
        handle.close()
        raise EngineLockError(
            f"another engine or flatten_now (pid {holder}) is running on {sqlite_path}: two processes "
            "must never trade the same account; stop it first"
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release_engine_lock(handle: IO[str] | None) -> None:
    if handle is None or handle.closed:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def tradable_watchlist(cfg: Config) -> list[str]:
    """The configured watchlist without banned symbols (the engine never touches those)."""
    return [s for s in dict.fromkeys(cfg.markets.watchlist) if not cfg.markets.is_banned(s)]


def _s(value: Decimal | None, places: str | None = None) -> str | None:
    """Decimal -> plain string for logs (optionally quantized, e.g. ``"0.000001"``)."""
    if value is None:
        return None
    if places is not None:
        value = value.quantize(Decimal(places))
    return format(value, "f")


def _flat(tree: Any, prefix: str = "") -> dict[str, Any]:
    """A nested config dump as ``{"a.b.c": value}`` (lists are values)."""
    if not isinstance(tree, dict):
        return {prefix: tree}
    out: dict[str, Any] = {}
    for key, value in tree.items():
        out.update(_flat(value, f"{prefix}.{key}" if prefix else str(key)))
    return out


# ---------------------------------------------------------------- acceptance-test intents


@dataclass(frozen=True)
class InjectedIntent:
    """One line of an ``--inject-intents`` JSONL file: an intent sent ``at_s`` seconds after start.

    Acceptance testing only (shadow mode): it goes through the normal risk -> order path and its
    decision record says it was injected.
    """

    at_s: float
    line: int
    strategy: str
    symbol: str
    side: Side
    order_type: OrderType
    purpose: Purpose
    reason: str
    qty: Decimal | None = None
    notional: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    taker: bool = False
    expected_edge_pct: Decimal | None = None
    features: dict[str, Any] = field(default_factory=dict)

    def to_intent(self, now: float) -> Intent:
        return Intent(
            id=f"d_{int(round(now * 1000))}_{self.strategy}_{self.symbol}_inj{self.line}",
            ts=now,
            strategy=self.strategy,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            qty=self.qty,
            notional=self.notional,
            price=self.price,
            stop_price=self.stop_price,
            purpose=self.purpose,
            reason=f"[injected for acceptance testing] {self.reason}",
            features={**self.features, "injected": True, "inject_line": self.line},
            expected_edge_pct=self.expected_edge_pct,
            taker=self.taker,
        )


_INJECT_KEYS = frozenset(
    {
        "at_s",
        "strategy",
        "symbol",
        "side",
        "order_type",
        "qty",
        "notional",
        "price",
        "stop_price",
        "purpose",
        "taker",
        "expected_edge_pct",
        "features",
        "reason",
    }
)


def _dec_field(obj: dict[str, Any], key: str, where: str) -> Decimal | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be a number")
    try:
        return D(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{where}: {key} is not a number") from None


def parse_injected_line(obj: dict[str, Any], line: int) -> InjectedIntent:
    """Validate one injected-intent object; ``ValueError`` names the line and the field."""
    where = f"line {line}"
    unknown = sorted(set(obj) - _INJECT_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}")
    for key in ("at_s", "strategy", "symbol", "side", "order_type", "purpose", "reason"):
        if obj.get(key) in (None, ""):
            raise ValueError(f"{where}: {key} is required")
    strategy = str(obj["strategy"])
    if strategy_prefix(strategy) not in OUR_PREFIXES:
        raise ValueError(f"{where}: strategy {strategy!r} must start with one of {', '.join(OUR_PREFIXES)}")
    side, otype, purpose = str(obj["side"]).upper(), str(obj["order_type"]).upper(), str(obj["purpose"])
    if side not in get_args(Side):
        raise ValueError(f"{where}: side must be BUY or SELL")
    if otype not in get_args(OrderType):
        raise ValueError(f"{where}: order_type must be one of {', '.join(get_args(OrderType))}")
    if purpose not in get_args(Purpose):
        raise ValueError(f"{where}: purpose must be one of {', '.join(get_args(Purpose))}")
    qty, notional = _dec_field(obj, "qty", where), _dec_field(obj, "notional", where)
    if qty is None and notional is None:
        raise ValueError(f"{where}: give qty or notional")
    features = obj.get("features") or {}
    if not isinstance(features, dict):
        raise ValueError(f"{where}: features must be an object")
    try:
        at_s = float(obj["at_s"])
    except (TypeError, ValueError):
        raise ValueError(f"{where}: at_s must be a number of seconds") from None
    if at_s < 0:
        raise ValueError(f"{where}: at_s must be >= 0")
    return InjectedIntent(
        at_s=at_s,
        line=line,
        strategy=strategy,
        symbol=str(obj["symbol"]).upper(),
        side=side,  # type: ignore[arg-type]  # checked against the Literal above
        order_type=otype,  # type: ignore[arg-type]
        purpose=purpose,  # type: ignore[arg-type]
        reason=str(obj["reason"]),
        qty=qty,
        notional=notional,
        price=_dec_field(obj, "price", where),
        stop_price=_dec_field(obj, "stop_price", where),
        taker=bool(obj.get("taker", False)),
        expected_edge_pct=_dec_field(obj, "expected_edge_pct", where),
        features=dict(features),
    )


def load_injected_intents(path: str | Path) -> list[InjectedIntent]:
    """Read an ``--inject-intents`` JSONL file (blank lines and ``#`` comments are skipped)."""
    out: list[InjectedIntent] = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(f"line {n}: not valid JSON") from None
        if not isinstance(obj, dict):
            raise ValueError(f"line {n}: not a JSON object")
        out.append(parse_injected_line(obj, n))
    return sorted(out, key=lambda x: (x.at_s, x.line))


def parse_paper_balances(text: str) -> dict[str, Decimal]:
    """``--paper-balances``: inline JSON (``{"TMN": 1000000, "USDT": "5"}``) or a JSON file path."""
    source = text.strip()
    if not source.startswith("{"):
        source = Path(source).read_text()
    try:
        obj = json.loads(source)
    except json.JSONDecodeError:
        raise ValueError("paper balances: not valid JSON") from None
    if not isinstance(obj, dict) or not obj:
        raise ValueError("paper balances: expected a non-empty object {asset: amount}")
    out: dict[str, Decimal] = {}
    for asset, amount in sorted(obj.items()):
        try:
            value = D(amount)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f"paper balances: {asset} is not a number") from None
        if value < 0:
            raise ValueError(f"paper balances: {asset} is negative")
        out[str(asset).upper()] = value
    return out


# ---------------------------------------------------------------- manual kill reset (CLI side)


def request_kill_reset(store: Store, events_log: EventsLog, reason: str, now: float) -> dict[str, Any] | None:
    """``wta reset-kill``: clear ``killed`` in the store and record who reset it and why.

    Returns None when the kill-switch is not active. Otherwise it writes, at ``now``:

    * kv ``engine_flags`` with ``killed=false`` (an engine started later reads it);
    * kv ``portfolio_state`` with the peak moved to the latest equity row, so the drawdown is
      measured from the reset, not from the pre-kill peak;
    * kv ``kill_reset`` = the request; a *running* engine applies it on its next tick (it keeps the
      flags in memory) and marks it ``applied``;
    * a ``reset_kill`` event in ``events.jsonl`` and in the store's ``events`` table.
    """
    flags = store.kv_get(KV_FLAGS) or {}
    if not flags.get("killed"):
        return None
    latest = store.latest_equity()
    peak = latest["equity_tmn"] if latest else None
    store.kv_set(KV_FLAGS, {**flags, "killed": False, "kill_reason": ""}, now)
    if peak is not None:
        portfolio = store.kv_get(KV_PORTFOLIO) or {}
        store.kv_set(KV_PORTFOLIO, {**portfolio, "peak_equity_tmn": str(peak)}, now)
    record = {
        "ts": now,
        "reason": reason,
        "previous_kill_reason": str(flags.get("kill_reason", "")),
        "peak_equity_tmn": _s(peak),
        "applied": False,
    }
    store.kv_set(KV_KILL_RESET, record, now)
    data = {k: v for k, v in record.items() if k != "reason"}
    store.add_event(now, "reset_kill", {"reason": reason, **data})
    store.flush()
    events_log.log("reset_kill", reason, data)
    return record


# ---------------------------------------------------------------- the engine


class Engine:
    """Owns the loops; every collaborator is injected so tests and other modes can swap them.

    ``rest`` is the market-data client polled by ``RestPoller`` (``get_all_depth``/``get_depth``);
    ``ws`` and ``synthetic`` are feeds with ``async run(stop_event)``. ``exchange`` (with
    ``markets``) turns trading on; without it the engine only monitors data (PRD-01).
    ``strategies`` (PRD-03; ``build_engine`` passes ``default_strategies(cfg)``) run through a
    ``StrategyRouter`` every tick; without them (the default here, e.g. Phase-2 tests) no strategy
    runs. ``intent_sources`` are callables ``(RiskContext) -> list[Intent]`` asked every tick
    (tests, tools); ``injected`` are acceptance-test intents. ``overlays`` only label the start
    event (the config is already merged). ``clock`` is wrapped in a ``TickClock`` (unless it is one
    already) for everything but the market-data feeds.
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
        markets: dict[str, MarketInfo] | None = None,
        markets_source: str = "",
        feed: str = "live",
        synthetic: Any = None,
        intent_sources: list[IntentSource] | None = None,
        injected: list[InjectedIntent] | None = None,
        kill_file: Path | None = KILL_FILE,
        strategies: list[Strategy] | None = None,
        overlays: list[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.mode = mode
        self.clock = clock if isinstance(clock, TickClock) else TickClock(clock)
        clock = self.clock
        self.bus = bus
        self.store = store
        self.exchange = exchange
        self.rest = rest
        self.ws = ws
        self.synthetic = synthetic
        self.feed = feed
        self.log_dir = Path(log_dir)
        self.i_understand_live = i_understand_live
        self.allow_dirty = allow_dirty
        self.markets = dict(sorted((markets or {}).items()))
        self.markets_source = markets_source
        self.intent_sources = list(intent_sources or [])
        self.kill_file = kill_file
        if exchange is not None and not self.markets:
            raise ValueError("a trading engine needs the exchange's markets")

        self.watchlist = tradable_watchlist(cfg)
        skipped = sorted(set(cfg.markets.watchlist) - set(self.watchlist))
        if skipped:
            log.warning("engine: banned symbols removed from the watchlist: %s", ", ".join(skipped))
        git = gitinfo.get()
        self.git_commit = str(git["commit"])
        self.git_dirty = tree_is_dirty()  # untracked files count too (gitinfo ignores them)
        self.git_tag = git["tag"]
        self.config_hash = cfg.config_hash

        self._secrets = settings.secret_values()
        self.health_log = HealthLog(self.log_dir, clock, self._secrets)
        self.events_log = EventsLog(self.log_dir, clock, self._secrets)
        self.rest_poller: RestPoller | None = None
        if rest is not None:
            self.rest_poller = RestPoller(
                rest,
                bus,
                self.watchlist,
                interval_s=cfg.data.rest_poll_interval_s,
                use_depth_all=cfg.data.rest_poll_use_depth_all,
                clock=self.clock.base,  # books are stamped with the real receive time
            )

        # runtime state (read by the heartbeat; the TUI/status read it from health.jsonl)
        self.started_ts: float | None = None
        self.ticks = 0
        self.heartbeats = 0
        self.book_ages: dict[str, float | None] = {}
        self.stale_symbols: list[str] = []
        self.task_crashes: dict[str, int] = {}
        self.tick_crashes = 0
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._signal_name: str | None = None
        self._error_samples: deque[tuple[float, int]] = deque()
        self._last_stale_warning = float("-inf")
        self._started = False
        self._restored = False  # startup_restore + strategy/feature state loaded (see ``startup``)
        self._shut_down = False

        # trading components (None in monitoring-only mode)
        self.trading = exchange is not None
        self.paper: PaperExchange | None = exchange if isinstance(exchange, PaperExchange) else None
        self.flags = EngineFlags()
        self.deadline = Deadline.from_config(cfg)
        self.portfolio = PortfolioState(initial_equity_tmn=cfg.engine.initial_equity_tmn)
        self.recent_intents: list[RecentIntent] = []
        self.last_anomalies: list[str] = []
        self.last_reconcile: dict[str, Any] | None = None
        self.flatten_report: dict[str, Any] = {"before": None, "after": None}
        self.manual_flatten: dict[str, Any] | None = None  # set by enable_manual_flatten (flatten_now)
        self._pending_injected = sorted(injected or [], key=lambda x: (x.at_s, x.line))
        self._veto_memo: dict[tuple[Any, ...], tuple[float, int]] = {}
        self._crash_times: deque[float] = deque()
        self._persisted: dict[str, str] = {}
        self._stored_book_ts: dict[str, float] = {}
        self._balances_stale = False
        self._last_reconcile_ts = float("-inf")
        self._last_equity_ts = float("-inf")
        self._last_day_start_ts: float | None = None
        self._final_state_logged = False
        self._flatten_complete_logged: set[str] = set()
        self._error_streak = 0
        self._api_burst = False
        self._reconcile_reason: str | None = None  # an unscheduled reconcile is due (API error burst)
        self._startup_reconcile_due = False  # deferred: the books it needs were not there at startup
        self.lock: IO[str] | None = None  # one engine per account (build_engine); released by aclose()
        self.overlays = list(overlays or [])

        # strategies (PRD-03): None without strategies (monitoring, Phase-2 style engines)
        self.router: StrategyRouter | None = None
        self.features: FeatureEngine | None = None
        if exchange is not None and strategies:
            self.router = StrategyRouter(list(strategies), cfg)
            self.features = FeatureEngine(cfg, self.markets)
        self.plan: MarketPlan | None = None  # latest validated plan (AI layer, a later phase)
        self.strategies_enabled = True  # False during a manual flatten (flatten_now)
        self.strategy_status = "not started"
        self.last_features: FeatureSnapshot | None = None
        self.s1_stats: dict[str, dict[str, Any]] = {}  # per coin: evaluated / opportunity ticks
        self._feature_state_ts = float("-inf")
        self.candles = CandleCache(self.watchlist, keep_s=CANDLE_CACHE_KEEP_S, resolution=CANDLE_RESOLUTION)
        self._candle_seq = 0  # arrivals in ``candles`` (engine_candles.seq), monotonic per state DB
        self._tick_candles: dict[str, list[Candle]] | None = None  # this tick's candle snapshot
        self._tick_candle_seq: int | None = None
        self.candle_refresher: CandleRefresher | None = None
        if exchange is not None and feed == "live" and callable(getattr(rest, "get_candles", None)):
            self.candle_refresher = CandleRefresher(
                rest,
                self.candles,
                self.watchlist,
                sink=self.ingest_candles,
                interval_s=cfg.data.candle_poll_interval_s,
                clock=self.clock.base,  # real time, like the other market-data tasks
            )
        if exchange is not None:
            self.decision_log = DecisionLog(
                self.log_dir, mode, self.git_commit, self.config_hash, clock, self._secrets
            )
            self.orders_log = OrdersLog(self.log_dir, clock, self._secrets)
            self.positions = PositionManager(self.portfolio, self.markets)
            self.risk = RiskManager(cfg)
            self.order_manager = OrderManager(
                exchange,
                self.portfolio,
                self.positions,
                self.markets,
                cfg,
                clock,
                self.decision_log,
                self.orders_log,
                store,
                mode,
            )
            # the write-ahead record of a placement is committed together with the kv state
            self.order_manager.checkpoint = lambda: self._checkpoint(self.clock.now())
            self.reconciler = Reconciler(
                exchange,
                self.portfolio,
                self.positions,
                self.order_manager,
                store,
                self.events_log,
                clock,
                cfg,
            )
            self.flatten = FlattenManager(cfg, self.markets)

        # AI layer (PRD-04 §8): attached by ``enable_ai`` (build_engine does it); None = no slow loop
        self.ai_source: str = AI_DISABLED if not cfg.ai.enabled else "off"
        self.ai_setup: LLMSetup | None = None
        self.analyst: RegimeAnalyst | None = None
        self.reviewer: PostTradeReviewer | None = None
        self.config_manager: ConfigManager | None = None
        self.mcp: MCPClient | None = None
        self.runtime_overlay: dict[str, Any] | None = None  # the reviewer overlay applied at startup
        self.analyst_clock = SimClock(0.0)  # set to each cycle's start: never the TickClock (see slow_step)
        self.reviewer_clock = SimClock(0.0)
        self.llm_consecutive_failures = 0
        self.ai_stats: dict[str, Any] = {"cycles": 0, "plans": 0, "failures": 0, "reviews": 0, "model_calls": 0,
                                         "cached_calls": 0, "tokens": 0}
        self.ai_last_error = ""
        self.ai_last_reason = ""
        self.last_review: dict[str, Any] | None = None
        self._ai_view: _AIView | None = None
        self._ai_triggers: list[dict[str, Any]] = []  # kill/pause/flatten events since the last cycle
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENTS)
        self._recent_decisions: dict[str, dict[str, Any]] = {}  # decision_id -> merged record (ordered)
        self._trades: dict[str, deque[Trade]] = {}
        self._plan_seq = 0
        self._tick_plan: tuple[MarketPlan | None] | None = None  # the plan in force while a tick runs
        self._stale_plan_logged: float | None = None
        self._analyst_busy = False
        self._reviewer_busy = False
        self._next_review_ts: float | None = None
        self._last_health: HealthRecord | None = None
        self.runtime_dir = runtime_dir_for(mode, feed)  # per account, like the state DB
        self.pinned_keys: frozenset[str] = frozenset()  # the operator's --set/--overlay keys
        self._pending_cfg: Config | None = None  # a reviewer change waiting for the next tick boundary
        self._in_tick = False
        if exchange is not None:
            self.decision_log.listeners.append(self._note_decision)
        bus.on_trade(self._note_trade)

        # Telegram (PRD-06 §2, optional): attached by ``enable_telegram``
        self.notifier: tg.TelegramNotifier | None = None
        self.telegram_commands: tg.TelegramCommands | None = None
        self._operator_requests: list[tuple[str, str, float]] = []
        self._stale_alerted = False
        self._last_equity_alert_ts: float | None = None
        self._llm_failure_alerted = False

    # ------------------------------------------------------------ run

    async def run(
        self,
        stop_event: asyncio.Event | None = None,
        seconds: float | None = None,
        *,
        handle_signals: bool = True,
    ) -> str:
        """Run until ``stop_event``, SIGINT/SIGTERM, ``seconds`` or an internal stop (flatten_now).

        Returns the stop reason. In live mode the guards run first; ``LiveGuardError`` is raised
        before anything starts. The market-data tasks start before ``startup()`` (its reconcile
        needs prices); a stop lets the running tick finish before the tasks are cancelled.
        """
        if self.mode == "live":
            check_live_guard(self.i_understand_live, self.allow_dirty, self.settings)
            if self.trading and self.manual_flatten is None:
                why = deadline_guard_reason(self.cfg.engine.challenge_end_utc, self.clock.now())
                if why is not None:
                    raise LiveGuardError(f"refusing to start live mode: {why}")
        self._stop = stop_event or asyncio.Event()
        data_jobs: dict[str, Callable[[], Awaitable[Any]]] = {}
        if self.rest_poller is not None:
            data_jobs["rest"] = self.rest_poller.run
        if self.ws is not None:
            data_jobs["ws"] = lambda: self.ws.run(self._stop)
        if self.synthetic is not None:
            data_jobs["synthetic"] = lambda: self.synthetic.run(self._stop)
        if self.candle_refresher is not None:
            data_jobs["candles"] = self.candle_refresher.run
        loop_jobs: dict[str, Callable[[], Awaitable[Any]]] = {
            "fast_loop": self._fast_loop,
            "heartbeat": self._heartbeat_loop,
            "flush": self._flush_loop,
        }
        if self.analyst is not None:  # the slow loop: its own task, the fast loop never awaits it
            loop_jobs["slow_loop"] = self._slow_loop
        if self.reviewer is not None:
            loop_jobs["reviewer"] = self._reviewer_loop
        if self.telegram_commands is not None and self.telegram_commands.enabled:
            commands = self.telegram_commands
            loop_jobs["telegram"] = lambda: commands.run(self._stop)
        tasks: dict[str, asyncio.Task[None]] = {}

        def start(jobs: dict[str, Callable[[], Awaitable[Any]]]) -> None:
            for n, j in jobs.items():
                tasks[n] = asyncio.create_task(self._supervise(n, j), name=f"engine-{n}")

        remove_handlers: Callable[[], None] = lambda: None  # noqa: E731
        reason = "stop requested"
        loop = asyncio.get_running_loop()
        ends_at = None if seconds is None else loop.time() + seconds  # ``seconds`` include startup
        try:
            start(data_jobs)
            remove_handlers = self._install_signal_handlers() if handle_signals else (lambda: None)
            books_wait = self.cfg.engine.stale_data_s if data_jobs else 0.0
            if ends_at is not None:
                books_wait = max(0.0, min(books_wait, ends_at - loop.time()))
            await self.startup(wait_for_books=bool(data_jobs), books_wait_s=books_wait)
            start(loop_jobs)
            if ends_at is None:
                await self._stop.wait()
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, ends_at - loop.time()))
                except TimeoutError:
                    reason = f"ran for {seconds:g} s"
            if self._signal_name:
                reason = f"signal {self._signal_name}"
            elif self._stop_reason:
                reason = self._stop_reason
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            remove_handlers()
            self._stop.set()
            if "fast_loop" in tasks:  # never cut a tick in half: an order may be in flight
                done, _ = await asyncio.wait({tasks["fast_loop"]}, timeout=FAST_LOOP_STOP_GRACE_S)
                if not done:
                    log.error("engine: the running tick did not finish within %gs; cancelling it", FAST_LOOP_STOP_GRACE_S)
            for name in ("ws", "synthetic"):  # let feeds close their sessions cleanly before cancelling
                if name in tasks:
                    await asyncio.wait({tasks[name]}, timeout=WS_STOP_GRACE_S)
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            self.shutdown(reason)
            await self._drain_alerts()
        return reason

    def request_stop(self, reason: str) -> None:
        """Stop ``run`` from inside the engine (e.g. flatten_now when the account is flat)."""
        self._stop_reason = reason
        self._stop.set()

    async def startup(self, wait_for_books: bool = False, books_wait_s: float | None = None) -> dict[str, Any]:
        """Start event, state restore and the startup reconcile (PRD-02 §5). Runs once.

        ``wait_for_books``: wait (at most ``books_wait_s``, default ``engine.stale_data_s``) for the
        books the reconcile needs (``run()`` does; callers that publish books first need not).
        Without them the reconcile is deferred to the first tick that has them.
        """
        if self._started:
            return {}
        self._started = True
        now = self.clock.now()
        self.started_ts = now
        prior = self._has_prior_state()
        with self.clock.frozen(now):
            self._event("start", f"engine started in {self.mode} mode", self._start_info(prior))
        log.info(
            "engine: %s mode, feed %s, commit %s%s, config %s, %d symbols",
            self.mode,
            self.feed,
            self.git_commit[:12],
            " (dirty)" if self.git_dirty else "",
            self.config_hash,
            len(self.watchlist),
        )
        if not self.trading:
            return {"restored": False}
        try:
            with self.clock.frozen(now):
                restore = await startup_restore(
                    self.store, self.portfolio, self.flags, self.positions, self.order_manager, now
                )
                restore.update(self._restore_strategies(now))
                await self._restore_after_kill_reset(now)
                self._last_day_start_ts = self.portfolio.day_start_ts
                await self._seed_balances(now)
        except BaseException:
            # nothing is persisted and nothing trades on half-restored defaults: the next tick
            # (or run) tries the whole startup again
            self._started = False
            log.exception("engine: state restore failed; nothing is persisted until it succeeds")
            raise
        self._restored = True
        wait_s = self.cfg.engine.stale_data_s if books_wait_s is None else books_wait_s
        books = await self._startup_books(wait_s) if wait_for_books else self._snapshot()
        now = self.clock.now()
        with self.clock.frozen(now):
            missing = self._missing_books(books)
            if missing:
                self._startup_reconcile_due = True
                reconcile: dict[str, Any] = {
                    "deferred": True,
                    "missing_books": missing,
                    "note": "the startup reconcile runs at the first tick with these books; "
                    "strategies wait for it",
                }
                log.warning("engine: startup reconcile deferred: no book yet for %s", ", ".join(missing))
            else:
                reconcile = (await self.reconcile(books, now, reason="startup")).summary()
            summary = {"restore": restore, "reconcile": reconcile}
            if prior:
                self._event(
                    "restart",
                    "state restored from the store (peak, day start, fees, counters, flags, positions, orders)",
                    summary,
                )
            self._persist(now, force=True)
        return summary

    def _restore_strategies(self, now: float) -> dict[str, Any]:
        """Startup: candles of the last 26 h from the state DB into the cache, then the strategy and
        feature state saved in kv (``strategy_state`` / ``feature_state``)."""
        out: dict[str, Any] = {}
        bars = 0
        for symbol in self.watchlist:
            latest = self.store.latest_candle_ts(symbol, CANDLE_RESOLUTION)
            if latest is None:
                continue
            got = self.store.load_candles(symbol, CANDLE_RESOLUTION, from_ts=latest - CANDLE_CACHE_KEEP_S)
            bars += self.candles.add(got)
        out["candles_loaded"] = bars
        self._candle_seq = self.store.max_engine_candle_seq(self.mode)
        out["candle_seq"] = self._candle_seq
        restored = self._restore_plan(now)
        if restored is not None:
            out["plan_restored"] = restored
        if self.router is None or self.features is None:
            return out
        strategy_state = self.store.kv_get(KV_STRATEGY)
        feature_state = self.store.kv_get(KV_FEATURES)
        self.router.load_state(strategy_state)
        self.features.load_state(feature_state)
        out["strategy_state"] = strategy_state is not None
        out["feature_state"] = feature_state is not None
        return out

    def _missing_books(self, books: dict[str, BookSnapshot]) -> list[str]:
        """Books the reconcile needs to value fills in TMN: USDTTMN and the symbols of our
        positions and open orders."""
        needed = {USDTTMN}
        needed.update(p.symbol for p in self.portfolio.positions.values())
        needed.update(o.req.symbol for o in self.portfolio.open_orders.values())
        return sorted(s for s in needed if s not in books)

    async def _startup_books(self, wait_s: float) -> dict[str, BookSnapshot]:
        """The bus snapshot once it has the reconcile's books, or after ``wait_s`` (or a stop)."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + wait_s
        books = self._snapshot()
        while self._missing_books(books) and loop.time() < give_up and not self._stop.is_set():
            await asyncio.sleep(STARTUP_BOOKS_POLL_S)
            books = self._snapshot()
        return books

    def _has_prior_state(self) -> bool:
        try:
            return self.store.kv_get(KV_PORTFOLIO) is not None or self.store.kv_get(KV_FLAGS) is not None
        except Exception:  # noqa: BLE001 - the start event must not depend on the store
            log.exception("engine: could not read the store's kv table")
            return False

    def _start_info(self, prior: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": self.mode,
            "feed": self.feed,
            "reason": "restart" if prior else "fresh",
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "git_tag": self.git_tag,
            "config_hash": self.config_hash,
            "overlays": list(self.overlays),
            "state_db": self.store.path,
            "strategies": [s.id for s in self.router.strategies] if self.router is not None else [],
            "rules_only": self.current_plan(self.clock.now()) is None,  # no fresh plan: rules-only defaults
            "ai": self._ai_start_info(),
            "runtime_overlay": self.runtime_overlay,
            "markets_source": self.markets_source or None,
            "trading": self.trading,
            "exchange": getattr(self.exchange, "name", None),
            "watchlist": self.watchlist,
            "rest": self.rest is not None,
            "ws": self.ws is not None,
            "pid": os.getpid(),
        }
        if self.feed == "synthetic":
            info.update(synthetic=True, note=SYNTHETIC_NOTE)
        if self._pending_injected:
            info["injected_intents"] = len(self._pending_injected)
        if self.trading:
            d = self.deadline
            info["challenge_end_utc"] = iso(d.end_ts) if d.end_ts is not None else None
            info["flatten_start_utc"] = iso(d.flatten_start_ts) if d.flatten_start_ts is not None else None
            info["flatten_final_utc"] = iso(d.flatten_final_ts) if d.flatten_final_ts is not None else None
        return info

    def shutdown(self, reason: str) -> None:
        """Final heartbeat, state save, ``stop`` event, store flush. Never raises (we are stopping)."""
        if self._shut_down:
            return
        self._shut_down = True
        now = self.clock.now()
        try:
            self.write_heartbeat(now)
        except Exception:
            log.exception("engine: final heartbeat failed")
        if self.trading and not self._restored:
            log.error("engine: the state was never restored from %s: nothing persisted at shutdown", self.store.path)
        elif self.trading:
            try:
                with self.store.transaction():
                    self._persist(now, force=True)
                    for _, pos in sorted(self.portfolio.positions.items()):
                        self.store.upsert_position(pos, open=True, ts=now)
            except Exception:
                log.exception("engine: final state save failed")
            if self.manual_flatten is not None:
                self.flatten_report["after"] = self._balance_report()
        uptime = now - (self.started_ts or now)
        try:
            self._event(
                "stop",
                reason,
                {"uptime_s": round(uptime, 3), "ticks": self.ticks, "heartbeats": self.heartbeats},
            )
        except Exception:
            log.exception("engine: could not write the stop event")
        try:
            self.store.flush()
        except Exception:
            log.exception("engine: final store flush failed")
        self.health_log.close()
        self.events_log.close()
        if self.trading:
            self.decision_log.close()
            self.orders_log.close()
        log.info("engine: stopped (%s)", reason)

    async def aclose(self) -> None:
        """Close the REST and MCP clients and the store, release the account lock (``build_engine``
        callers)."""
        try:
            if self.rest is not None:
                await self.rest.close()
            if self.mcp is not None:
                try:
                    await self.mcp.close()
                except Exception:  # noqa: BLE001 - closing must not fail the shutdown
                    log.exception("engine: closing the MCP client failed")
            self.store.close()
        finally:
            release_engine_lock(self.lock)
            self.lock = None

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
        """One tick after another until stop. A stop never interrupts a tick: the loop ends between
        two ticks (``run()`` waits for that)."""
        while not self._stop.is_set():
            await self.safe_tick(self.clock.now())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.engine.fast_loop_interval_s)
            except TimeoutError:
                pass

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.engine.heartbeat_interval_s)
            self.write_heartbeat(self.clock.now())
            if self.notifier is not None:
                await self.notifier.flush_digest()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.data.store_flush_interval_s)
            self.store.maybe_flush(self.clock.now())

    async def safe_tick(self, now: float) -> bool:
        """``tick`` that never raises: a crash is logged and counted; a storm of them kills.

        ``engine.exception_storm_count`` crashes within ``engine.exception_storm_window_s`` trigger
        the kill-switch (reason ``exception storm``). Returns True when the tick completed.
        """
        try:
            await self.tick(now)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad tick must not stop the engine
            log.exception("engine: fast tick crashed")
            self.tick_crashes += 1
            window, limit = self.cfg.engine.exception_storm_window_s, self.cfg.engine.exception_storm_count
            self._crash_times.append(now)
            while self._crash_times and self._crash_times[0] <= now - window:
                self._crash_times.popleft()
            text = mask_secrets(f"{type(exc).__name__}: {exc}".splitlines()[0][:300], self._secrets)
            n = len(self._crash_times)
            if n <= limit:  # log the first crashes of a window; a persistent bug would flood events
                self._event("tick_error", text, {"crashes_in_window": n, "window_s": window})
            if self.trading and n >= limit and not self.flags.killed:
                reason = f"exception storm: {n} fast-loop crashes within {window:g}s (last: {text})"
                try:
                    await self._kill(reason, now, source="exception_storm")
                except Exception:  # noqa: BLE001 - at least the flag must stick
                    log.exception("engine: kill after the exception storm failed; setting the flag only")
                    self.flags.killed, self.flags.kill_reason = True, reason
                    try:
                        self._persist_flags(now)
                    except Exception:  # noqa: BLE001 - the next successful persist writes it
                        log.exception("engine: could not persist the kill flag; it is set in memory")
            return False

    async def tick(self, now: float) -> None:
        """One fast-loop iteration (see the module docstring). Raises on a bug; use ``safe_tick``.

        The clock is frozen at ``now`` and the whole tick is one store transaction. The kv state is
        saved at its end also when the tick raised, so what is committed always matches memory.
        A reviewer config change accepted while the previous tick was running is swapped in here,
        first: like the plan, the config never changes half-way through a tick.
        """
        self._in_tick = True
        try:
            self._apply_pending_config()
            await self._tick(now)
        finally:
            self._in_tick = False

    def _apply_pending_config(self) -> None:
        if self._pending_cfg is None:
            return
        cfg, self._pending_cfg = self._pending_cfg, None
        try:
            self.reload_config(cfg)
        except ValueError:
            log.exception("reviewer: change refused by reload_config; config unchanged")

    async def _tick(self, now: float) -> None:
        self._bookkeeping(now)
        if not self.trading:
            return
        if not self._restored:  # also after a failed startup: never trade or persist on defaults
            await self.startup()
        # the plan in force for the whole tick: a plan the slow loop swaps in meanwhile (it runs
        # during the tick's awaits) is used from the next tick on, never half-way through this one
        self._tick_plan = (self.plan,)
        try:
            with self.clock.frozen(now), self.store.transaction(raise_errors=False):
                try:
                    await self._trading_tick(now)
                except BaseException:
                    try:
                        self._persist(now)
                    except Exception:  # noqa: BLE001 - the tick's own error is the one to report
                        log.exception("engine: could not save the state of a crashed tick")
                    raise
                self._persist(now)
        finally:
            self._tick_plan = None

    async def _trading_tick(self, now: float) -> None:
        if self.flags.killed:
            # first, while nothing of this tick is written yet: the read then takes no write lock
            # for the rest of the tick (``wta reset-kill`` writes the same database)
            await self._poll_kill_reset(now)
        if self._operator_requests:  # Telegram /pause /resume /kill (operational, never a trade)
            await self._apply_operator_requests(now)
        books = self._snapshot()
        if self.router is not None:  # candles are snapshotted with the books (replay: candle_seq)
            self._tick_candles = self.candles.snapshot(self.watchlist)
            self._tick_candle_seq = self._candle_seq
        self._record_inputs(books, now)  # first: a tick that crashes later is still replayable
        if self.paper is not None:
            self.paper.on_tick(now, books)
        if self._startup_reconcile_due and not self._missing_books(books):
            self._startup_reconcile_due = False
            report = await self.reconcile(books, now, reason="startup")
            log.info("engine: deferred startup reconcile done: %s", report.summary())
        self.portfolio.refresh(books, now)
        self._note_day_change(now)
        ctx = self._context(now, books)
        await self._rolling_state(ctx)
        await self._check_kill_file(now)
        await self._deadline(ctx)
        result = await self.order_manager.poll(books, now)
        self._after_poll(result, now)
        await self._refresh_balances(now, books)
        self._note_flatten_before()
        await self._intents(ctx, result)
        await self._scheduled(ctx)

    def _bookkeeping(self, now: float) -> None:
        """PRD-01 part of the tick: book ages, stale symbols, API error window."""
        self.ticks += 1
        stale_s = self.cfg.engine.stale_data_s
        self.book_ages = {s: self.bus.age(s, now) for s in self.watchlist}
        self.stale_symbols = [s for s, age in self.book_ages.items() if age is None or age > stale_s]
        self._sample_api_errors(now)
        warmed_up = self.started_ts is not None and now - self.started_ts > stale_s
        if self.stale_symbols and warmed_up and now - self._last_stale_warning >= STALE_WARNING_EVERY_S:
            self._last_stale_warning = now
            log.warning("engine: no book newer than %gs for %s", stale_s, ", ".join(self.stale_symbols))
        if self.notifier is not None and self.started_ts is not None and now - self.started_ts > STALE_ALERT_S:
            old = sorted(s for s, a in self.book_ages.items() if a is None or a > STALE_ALERT_S)
            if old and not self._stale_alerted:
                self._stale_alerted = True
                ages = [a for s, a in self.book_ages.items() if s in old and a is not None]
                self._alert(tg.fmt_stale(old, max(ages) if ages else None), "stale")
            elif not old:
                self._stale_alerted = False

    # ------------------------------------------------------------ tick steps

    def _needed_symbols(self) -> list[str]:
        """Watchlist + flatten targets of held assets + symbols of positions and open orders."""
        needed = set(self.watchlist)
        if self.trading:
            needed.update(self.flatten.markets_needed(self.portfolio.balances))
            needed.update(p.symbol for p in self.portfolio.positions.values())
            needed.update(o.req.symbol for o in self.portfolio.open_orders.values())
        symbols = sorted(needed)
        if self.rest_poller is not None:
            missing = [s for s in symbols if s not in self.rest_poller.symbols]
            if missing:
                self.rest_poller.symbols = sorted({*self.rest_poller.symbols, *missing})
        return symbols

    def _snapshot(self) -> dict[str, BookSnapshot]:
        return self.bus.snapshot(self._needed_symbols())

    def _context(self, now: float, books: dict[str, BookSnapshot]) -> RiskContext:
        horizon = max(self.cfg.risk.duplicate_cooldown_s, self.cfg.engine.symbol_cooldown_s)
        self.recent_intents = [r for r in self.recent_intents if now - r.ts < horizon]
        return RiskContext(
            now=now,
            portfolio=self.portfolio,
            books=books,
            markets=self.markets,
            cfg=self.cfg,
            flags=self.flags,
            deadline=self.deadline,
            plan=self.current_plan(now),
            recent_intents=self.recent_intents,
            api_consecutive_errors=self._api_consecutive_errors(),
        )

    def _api_consecutive_errors(self) -> int:
        value = getattr(self.rest, "consecutive_errors", 0)
        return value if isinstance(value, int) else 0

    def _note_day_change(self, now: float) -> None:
        """A ``day_start`` event when the UTC day (and with it the day-start equity) rolls over."""
        day = self.portfolio.day_start_ts
        if day is not None and self._last_day_start_ts is not None and day != self._last_day_start_ts:
            self._event(
                "day_start",
                f"new UTC day {iso(day)[:10]}: daily PnL and the daily-loss limit restart",
                {"day_start_equity_tmn": _s(self.portfolio.day_start_equity_tmn)},
            )
        if day is not None:
            self._last_day_start_ts = day

    async def _rolling_state(self, ctx: RiskContext) -> None:
        """Kill / pause / resume from the RiskManager (skipped while the first books arrive)."""
        since_start = ctx.now - (self.started_ts if self.started_ts is not None else ctx.now)
        if USDTTMN not in ctx.books and since_start < self.cfg.engine.stale_data_s:
            return
        anomalies = self._anomalies(ctx)
        self.last_anomalies = self.risk.anomaly_reasons(ctx, anomalies)
        for event in self.risk.update_rolling_state(ctx, anomalies):
            await self._apply_risk_event(event, ctx.now)

    def _anomalies(self, ctx: RiskContext) -> list[str]:
        """Engine-observed anomalies: TMN and USDT books disagree, or a held asset has no price."""
        out: list[str] = []
        books, stale_s = ctx.books, self.cfg.engine.stale_data_s
        limit = self.cfg.risk.price_disagreement_pause_pct

        def fresh_mid(symbol: str) -> Decimal | None:
            b = books.get(symbol)
            return b.mid if b is not None and b.age(ctx.now) <= stale_s else None

        dollar = fresh_mid(USDTTMN)
        for symbol in sorted(books):
            m = self.markets.get(symbol)
            if dollar is None or m is None or m.quote != FIAT or m.base == "USDT":
                continue
            tmn_mid, usdt_mid = fresh_mid(symbol), fresh_mid(f"{m.base}USDT")
            if not tmn_mid or not usdt_mid:
                continue
            dev = tmn_mid / dollar / usdt_mid - 1
            if abs(dev) >= limit:
                out.append(
                    f"price disagreement {m.base}: {symbol}/USDTTMN vs {m.base}USDT differ by {pct(dev)}"
                )
        val = self.portfolio.last_valuation
        if val is not None and val.missing_prices:
            out.append("no price for held asset(s): " + ", ".join(val.missing_prices))
        return out

    async def _apply_risk_event(self, event: RiskEvent, now: float) -> None:
        if event.kind == "kill":
            await self._kill(event.reason, now, source="drawdown")
            return
        data: dict[str, Any] = {**event.to_dict(), **self._risk_snapshot()}
        if event.kind == "pause":
            self.flags.paused = True
            self.flags.pause_reason = event.reason
            self.flags.paused_until_ts = event.until_ts
            # nothing may keep re-pricing an entry without a risk check while paused
            data["entries_canceled"] = await self.order_manager.cancel_where(cancel_at_flatten_start, "pause")
            self._balances_stale = True
        else:
            self.flags.paused = False
            self.flags.pause_reason = ""
            self.flags.paused_until_ts = None
        self._event(event.kind, event.reason, data)
        self._persist_flags(now)

    def _risk_snapshot(self) -> dict[str, Any]:
        p = self.portfolio
        return {
            "equity_tmn": _s(p.equity_tmn),
            "peak_equity_tmn": _s(p.peak_equity_tmn),
            "drawdown_pct": _s(p.drawdown_pct, "0.000001"),
            "daily_pnl_pct": _s(p.daily_pnl_pct, "0.000001"),
        }

    async def _kill(self, reason: str, now: float, source: str) -> None:
        """Kill-switch (PRD-02 §6): persist, cancel entries, start flattening, keep monitoring."""
        if self.flags.killed:
            return
        self.flags.killed = True
        self.flags.kill_reason = reason
        self._flatten_complete_logged.discard("kill")
        self._checkpoint(now)  # the kill flag is committed at once, with the state of this moment
        self._event(
            "kill",
            reason,
            {
                "source": source,
                **self._risk_snapshot(),
                "positions": sorted(self.portfolio.positions),
                "open_orders": sorted(self.portfolio.open_orders),
            },
        )
        canceled = await self.order_manager.cancel_where(cancel_at_flatten_start, "kill")
        self._balances_stale = True
        self._event(
            "flatten",
            "kill-switch flatten started: every non-TMN balance is sold to TMN, then the engine only monitors",
            {
                "trigger": "kill",
                "kind": "kill_flatten_start",
                "phase": self._flatten_phase(now),
                "entries_canceled": canceled,
            },
        )

    async def _check_kill_file(self, now: float) -> None:
        if self.kill_file is not None and not self.flags.killed and self.kill_file.exists():
            await self._kill("kill file", now, source="kill_file")

    async def _poll_kill_reset(self, now: float) -> None:
        """While killed: apply a reset that ``wta reset-kill`` wrote to the store."""
        try:
            record = self.store.kv_get(KV_KILL_RESET)
        except Exception:  # noqa: BLE001 - try again next tick
            log.exception("engine: could not read the kill reset request")
            return
        if record and not record.get("applied"):
            await self._apply_kill_reset(record, now)

    async def _restore_after_kill_reset(self, now: float) -> None:
        """At startup: after a manual reset, the peak is the one kept since the reset.

        ``startup_restore`` takes the highest equity row ever written as a peak floor; rows from
        before a reset would bring back the drawdown that caused the kill. The kv peak is kept up
        to date after the reset, so it is the right one. A reset not applied yet is applied now.
        """
        record = self.store.kv_get(KV_KILL_RESET)
        if not record:
            return
        state = self.store.kv_get(KV_PORTFOLIO) or {}
        if state.get("peak_equity_tmn") is not None:
            self.portfolio.peak_equity_tmn = D(state["peak_equity_tmn"])
        if not record.get("applied"):
            await self._apply_kill_reset(record, now)

    async def _apply_kill_reset(self, record: dict[str, Any], now: float) -> None:
        """Clear the kill. Its flatten orders are canceled too unless a deadline phase or a manual
        flatten still wants them: the operator decided to resume, not to keep selling."""
        previous = self.flags.kill_reason or str(record.get("previous_kill_reason", ""))
        self.flags.killed = False
        self.flags.kill_reason = ""
        canceled = 0
        if self.flags.flatten_phase == 0 and self.manual_flatten is None:
            canceled = await self.order_manager.cancel_where(lambda m: m.purpose == "flatten", "kill_reset")
            self._balances_stale = True
        val = self.portfolio.last_valuation
        if val is not None and val.usable:
            self.portfolio.peak_equity_tmn = val.equity_tmn
        elif record.get("peak_equity_tmn") is not None:
            self.portfolio.peak_equity_tmn = D(record["peak_equity_tmn"])
        self._flatten_complete_logged.discard("kill")
        self.store.kv_set(KV_KILL_RESET, {**record, "applied": True, "applied_ts": now}, now)
        self._event(
            "reset_kill_applied",
            f"kill-switch reset applied: {record.get('reason', '')}",
            {
                "request_ts": record.get("ts"),
                "previous_kill_reason": previous,
                "peak_equity_tmn": _s(self.portfolio.peak_equity_tmn),
                "flatten_orders_canceled": canceled,
            },
        )
        self._persist(now, force=True)

    async def _deadline(self, ctx: RiskContext) -> None:
        """Flatten phases by clock, the T-5min ``final_state`` event and the challenge end."""
        now = ctx.now
        texts = {
            "flatten_start": "flatten_start (T-2h): no new exposure; entry/rebalance orders canceled; "
            "selling everything to TMN with maker-first limits",
            "flatten_final": "flatten_final (T-30min): only flatten orders; leftovers sold with MARKET orders",
            "challenge_end": "challenge end: every open order canceled; no orders from now on",
        }
        for phase, kind in flatten_phase_transitions(self.deadline, now, self.flags.flatten_phase):
            self.flags.flatten_phase = phase
            data: dict[str, Any] = {
                "phase": phase,
                "kind": kind,
                "trigger": "deadline",
                "end_utc": iso(self.deadline.end_ts) if self.deadline.end_ts is not None else None,
            }
            if kind == "flatten_start":
                data["orders_canceled"] = await self.order_manager.cancel_where(cancel_at_flatten_start, kind)
            elif kind == "challenge_end":
                data["orders_canceled"] = await self.order_manager.cancel_where(lambda _m: True, kind)
            self._balances_stale = True
            self._event("flatten", texts[kind], data)
            self._persist_flags(now)
        due = self.deadline.final_state_ts
        if due is not None and now >= due and not self._final_state_logged:
            self._final_state_logged = True
            state = self.flatten.final_state(ctx)
            left = ", ".join(e["asset"] for e in state["non_tmn_over_min"])
            text = (
                "final state: 100% TMN (dust only)" if state["complete"] else f"ALERT: not flat yet: {left}"
            )
            self._event("final_state", text, state)

    def _after_poll(self, result: PollResult, now: float) -> None:
        """Track API trouble from the poll (an error burst triggers a reconcile once it ends)."""
        for err in result.errors:
            log.warning("engine: order poll: %s", mask_secrets(err, self._secrets))
        self._error_streak = self._error_streak + 1 if result.errors else 0
        burst = (
            max(self._error_streak, self._api_consecutive_errors())
            >= self.cfg.risk.consecutive_api_errors_pause
        )
        if burst and not self._api_burst:
            self._alert(tg.fmt_api_burst(max(self._error_streak, self._api_consecutive_errors())), "api",
                        dedupe_key="api_burst")
        if self.notifier is not None:
            for ev in result.fills:
                f = ev.fill
                closed = ev.result if isinstance(ev.result, ClosedPosition) else None
                self._alert(
                    tg.fmt_fill(
                        ev.managed.intent.strategy, f.symbol, f.side, f.price * f.qty,
                        pnl=closed.pnl_tmn if closed is not None else None,
                        pnl_pct=closed.pnl_pct if closed is not None else None,
                        qty=f.qty, price=f.price, is_maker=f.is_maker,
                    ),
                    "fill",
                )
        if burst:
            self._api_burst = True
        elif self._api_burst and not result.errors and self._api_consecutive_errors() == 0:
            self._api_burst = False
            self._reconcile_reason = "api_error_burst"
        if result.fills or result.done:
            self._balances_stale = True

    async def _refresh_balances(self, now: float, books: dict[str, BookSnapshot]) -> None:
        """Re-read balances from the exchange after fills / placements / cancels (exchange = truth)."""
        if not (self.order_manager.balances_dirty or self._balances_stale):
            return
        try:
            balances = await self.exchange.get_balances()  # type: ignore[union-attr]
        except WallexAPIError as err:
            log.warning("engine: balance refresh failed (kept the optimistic balances): %s", err)
            return
        self.order_manager.balances_dirty = False
        self._balances_stale = False
        if balances != self.portfolio.balances:
            self.store.add_balances(now, balances)
        self.portfolio.set_balances(balances)
        self.portfolio.refresh(books, now)

    async def _seed_balances(self, now: float) -> None:
        """Before the startup reconcile: the balances we last knew (so drift is measured against
        them), or the exchange's on a fresh store (nothing to compare with)."""
        stored = self.store.latest_balances()
        if stored:
            self.portfolio.set_balances(stored)
            return
        try:
            self.portfolio.set_balances(await self.exchange.get_balances())  # type: ignore[union-attr]
        except WallexAPIError as err:
            log.warning("engine: could not read balances at startup: %s", err)

    # ------------------------------------------------------------ intents

    def _flatten_phase(self, now: float) -> int:
        """Flatten phase to plan with: 1 maker-first, 2 final; 0 = no flattening (3 = over)."""
        deadline_phase = self.flags.flatten_phase
        if deadline_phase >= 3:
            return 0
        phase = deadline_phase
        if self.flags.killed:
            phase = max(phase, 1)
        if self.manual_flatten is not None:
            start = self.manual_flatten.get("start_ts")
            start = now if start is None else start
            manual = 1 if now - start < self.manual_flatten["phase1_s"] else 2
            phase = max(phase, manual)
        return phase

    async def _intents(self, ctx: RiskContext, result: PollResult | None = None) -> None:
        """Exits (+ strategies), flatten, intent sources and injected intents -> risk -> orders.

        Without strategies the exits go out first exactly as in Phase 2; with them the exits are
        part of the strategy step (hook intents, then ``router.tick(ctx, exits)``).
        """
        now, books = ctx.now, ctx.books
        # forget vetoes not repeated for a while (kept 2 windows, so a due repeat still finds its count)
        self._veto_memo = {k: v for k, v in self._veto_memo.items() if now - v[0] < 2 * VETO_REPEAT_S}
        over = self.flags.flatten_phase >= 3
        exits: list[Intent] = []
        if not over and self.flags.flatten_phase < 2:
            exits = self.positions.check_exits(
                books, now, software_stops=not self.cfg.execution.use_server_side_stops
            )
        phase = self._flatten_phase(now)
        if self.router is None:
            await self._process(exits, ctx)
        else:
            await self._strategy_step(ctx, result or PollResult(), exits, phase)
        if phase:
            if self.manual_flatten is not None and self.manual_flatten.get("start_ts") is None:
                self.manual_flatten["start_ts"] = now
            trigger = "kill" if self.flags.killed else "deadline"
            open_orders = [o for _, o in sorted(self.portfolio.open_orders.items())]
            await self._process(self.flatten.plan(ctx, open_orders, phase, books, trigger=trigger), ctx)
            self._check_flatten_complete(ctx, "manual" if self.manual_flatten is not None else trigger)
        if not over and not self.flags.killed and self.flags.flatten_phase < 2 and not self._startup_reconcile_due:
            for source in self.intent_sources:
                await self._process(list(source(ctx)), ctx)
        await self._process(self._due_injected(now), ctx, suppress_repeats=False)

    def _due_injected(self, now: float) -> list[Intent]:
        if not self._pending_injected:
            return []
        base = self.started_ts if self.started_ts is not None else now
        due = [x for x in self._pending_injected if now - base >= x.at_s]
        self._pending_injected = [x for x in self._pending_injected if now - base < x.at_s]
        return [x.to_intent(now) for x in due]

    async def _process(
        self,
        intents: list[Intent],
        ctx: RiskContext,
        *,
        suppress_repeats: bool = True,
        snapshot: FeatureSnapshot | None = None,
    ) -> None:
        """risk.evaluate -> order_manager.submit for each intent; balances re-read after placements.

        ``snapshot`` (strategy batches): the decision record's ``features`` become the compact
        feature snapshot of the symbols the intent used, and the plan snapshot is attached.
        """
        plan = self.current_plan(ctx.now) if snapshot is not None else None
        plan_snapshot = plan.model_dump(mode="json") if plan is not None else None
        for intent in intents:
            decision = self.risk.evaluate(intent, self._risk_view(ctx, intent))
            features = self._decision_features(intent, snapshot) if snapshot is not None else None
            if not decision.approved and suppress_repeats:
                key = (
                    intent.strategy,
                    intent.symbol,
                    intent.side,
                    intent.purpose,
                    intent.linked_position_id,
                    tuple(decision.failed_codes),
                )
                memo = self._veto_memo.get(key)
                if memo is not None and ctx.now - memo[0] < VETO_REPEAT_S:
                    self._veto_memo[key] = (memo[0], memo[1] + 1)
                    continue
                if memo is not None and memo[1]:
                    features = {**(features if features is not None else intent.features),
                                "veto_repeats_suppressed": memo[1]}
                self._veto_memo[key] = (ctx.now, 0)
            placed = await self.order_manager.submit(
                intent, decision, ctx.books, features=features, plan_snapshot=plan_snapshot
            )
            if decision.approved:
                self.recent_intents.append(
                    RecentIntent(ctx.now, intent.strategy, intent.symbol, intent.side, intent.purpose)
                )
            if placed is not None:
                self._balances_stale = True
            await self._refresh_balances(ctx.now, ctx.books)

    def _risk_view(self, ctx: RiskContext, intent: Intent) -> RiskContext:
        """The context ``intent`` is evaluated in (see "Closing SELL ..." in the module docstring).

        For a closing SELL of a position whose server stop is open, the stop's locked coins count
        as free: the OrderManager cancels that stop right before placing the SELL.
        """
        if intent.side != "SELL" or intent.purpose not in CLOSING_PURPOSES or not intent.linked_position_id:
            return ctx
        port = ctx.portfolio
        pos = port.positions.get(intent.linked_position_id)
        stop = port.open_orders.get(pos.stop_order_client_id or "") if pos is not None else None
        if pos is None or stop is None or stop.is_terminal or stop.req.side != "SELL":
            return ctx
        bal = port.balances.get(pos.base)
        release = min(stop.remaining_qty, bal.locked) if bal is not None else Decimal(0)
        if bal is None or release <= 0:
            return ctx
        freed = Balance(asset=pos.base, free=bal.free + release, locked=bal.locked - release)
        view = dataclasses.replace(port, balances={**port.balances, pos.base: freed})
        return dataclasses.replace(ctx, portfolio=view)

    # ------------------------------------------------------------ strategies (PRD-03)

    def current_plan(self, now: float | None = None) -> MarketPlan | None:
        """The plan in force at ``now`` (default: the clock): ``plan`` (swapped in by the slow loop;
        during a tick the one taken at the tick's start) while it is fresh (``now - plan.ts <= ai.plan_stale_s``, the same rule as
        ``StrategyContext.plan_fresh``), never one while ``ai.enabled`` is false. None = rules-only
        defaults, so a decision record's ``plan_snapshot`` is always the envelope the strategies used."""
        plan = self._tick_plan[0] if self._tick_plan is not None else self.plan
        if not self.cfg.ai.enabled or plan is None:
            return None
        t = self.clock.now() if now is None else now
        return plan if t - plan.ts <= self.cfg.ai.plan_stale_s else None

    def _strategy_block(self) -> str | None:
        """Why the strategies are not asked this tick (None: they are)."""
        if not self.strategies_enabled:
            return "manual flatten: no strategy runs"
        if self.flags.flatten_phase >= 3:
            return "challenge over: no orders"
        if self.flags.killed:
            return f"killed ({self.flags.kill_reason}): exits and flatten only"
        if self.flags.flatten_phase:
            return f"flatten phase {self.flags.flatten_phase}: exits and flatten only"
        if self._startup_reconcile_due:
            return "waiting for the deferred startup reconcile"
        return None

    async def _strategy_step(
        self, ctx: RiskContext, result: PollResult, exits: list[Intent], phase: int
    ) -> None:
        """Steps a-d of "Strategies" in the module docstring."""
        assert self.router is not None
        now = ctx.now
        block = self._strategy_block()
        self.strategy_status = block or "active"
        for ev in result.fills:
            self.router.note_fill(ev.fill.symbol, ev.fill.ts)
        seq = [len(exits)]  # check_exits numbered its intents 1..n: strategies continue from there
        sctx = self._strategy_context(ctx, phase, seq)
        hook_intents = self._deliver_hooks(result, sctx)
        if result.closed:
            self._prune_strategies()
        if block is not None:
            if hook_intents:
                log.info(
                    "engine: %d hook intent(s) not sent (%s): %s",
                    len(hook_intents), block, ", ".join(i.id for i in hook_intents),
                )
            await self._process(exits, ctx)
            return
        if hook_intents:
            kept, dropped = self.router.apply_cooldown(hook_intents, now)
            self._log_router_drops(
                [(i, d["why"]) for i in hook_intents for d in dropped if d["intent"] == i.id], sctx.features, now
            )
            await self._process(kept, ctx, snapshot=sctx.features)
            sctx.portfolio = self._portfolio_view(ctx.books)  # on_tick sees what the hooks placed
        intents = self.router.tick(sctx, exits)
        kept, dropped = self.router.apply_cooldown(intents, now)
        drops = list(self.router.dropped_intents)
        drops += [(i, d["why"]) for i in intents for d in dropped if d["intent"] == i.id]
        self._log_router_drops(drops, sctx.features, now)
        self._note_s1_stats(now)
        await self._process(kept, ctx, snapshot=sctx.features)

    def _strategy_context(self, ctx: RiskContext, phase: int, seq: list[int]) -> StrategyContext:
        """The tick's ``StrategyContext`` (features are computed here, once per tick)."""
        assert self.features is not None
        candles = self._tick_candles if self._tick_candles is not None else self.candles.snapshot(self.watchlist)
        snap = self.features.compute(ctx.now, ctx.books, candles, self.watchlist)
        self.last_features = snap
        return StrategyContext(
            now=ctx.now,
            books=ctx.books,
            features=snap,
            candles={(s, CANDLE_RESOLUTION): c for s, c in candles.items()},
            markets=self.markets,
            portfolio=self._portfolio_view(ctx.books),
            plan=self.current_plan(ctx.now),
            cfg=self.cfg,
            rng_seed=self.cfg.engine.rng_seed,
            flatten_phase=phase,
            seq=seq,
        )

    def _portfolio_view(self, books: dict[str, BookSnapshot]) -> PortfolioView:
        """Read-only portfolio numbers for the strategies.

        Equity is the latest usable valuation (else the last usable one): a book missing for a
        moment must not halve the sizes. The RiskManager pauses entries on such an anomaly anyway.
        """
        p = self.portfolio
        val = p.last_valuation
        if val is not None and val.usable:
            equity = val.equity_tmn
        elif p.last_complete_equity_tmn is not None:
            equity = p.last_complete_equity_tmn
        else:
            equity = val.equity_tmn if val is not None else D0
        dollar = books.get(USDTTMN)
        return PortfolioView(
            equity_tmn=equity,
            free={a: b.free for a, b in sorted(p.balances.items())},
            total={a: b.total for a, b in sorted(p.balances.items())},
            positions=tuple(p.positions[k] for k in sorted(p.positions)),
            open_orders=tuple(o for _, o in sorted(p.open_orders.items()) if not o.is_terminal),
            usdt_share=p.usdt_share,
            coin_exposure_pct=p.coin_exposure_pct,
            u_bid=dollar.best_bid if dollar is not None else None,
        )

    def _deliver_hooks(self, result: PollResult, sctx: StrategyContext) -> list[Intent]:
        """``on_fill`` for every fill and ``on_order_done`` for every finished order of the poll,
        each to the strategy that owns the order (by the intent's strategy id)."""
        assert self.router is not None
        out: list[Intent] = []
        for ev in result.fills:
            strat = self.router.by_id(ev.managed.intent.strategy)
            if strat is None:
                continue
            res = ev.result
            position = res.position if isinstance(res, ClosedPosition) else res
            out.extend(strat.on_fill(ev.fill, position, sctx))
        for mo in result.done:
            strat = self.router.by_id(mo.intent.strategy)
            if strat is None:
                continue
            # the logical order: every placement's fills (a re-placed order's current placement
            # alone may show none although an earlier one filled)
            order = mo.order.model_copy(
                update={"filled_qty": mo.logical_filled, "avg_price": mo.avg_price, "fee_paid": mo.logical_fee}
            )
            out.extend(strat.on_order_done(order, sctx))
        return out

    def _prune_strategies(self) -> None:
        """After positions closed: strategies with a ``prune`` forget the cycles of closed positions."""
        if self.router is None:
            return
        for strat in self.router.strategies:
            prune = getattr(strat, "prune", None)
            if callable(prune):
                prune(
                    {
                        str(p.meta.get("entry_decision_id") or p.id)
                        for p in self.portfolio.positions.values()
                        if p.strategy == strat.id
                    }
                )

    def _decision_features(self, intent: Intent, snap: FeatureSnapshot) -> dict[str, Any]:
        """``FeatureSnapshot.compact`` for the intent's symbol and ``features.books_used`` (and the
        cross-book features of their coins)."""
        used = intent.features.get("books_used")
        symbols = {intent.symbol}
        if isinstance(used, (list, tuple, set)):
            symbols.update(str(s) for s in used)
        elif isinstance(used, str) and used:
            symbols.add(used)
        coins = {self.markets[s].base for s in symbols if s in self.markets} & set(snap.cross)
        return snap.compact(symbols=sorted(symbols), coins=sorted(coins))

    def _log_router_drops(self, drops: list[tuple[Intent, str]], snap: FeatureSnapshot, now: float) -> None:
        """A ``no_trade`` record per intent the router dropped (same intent and reason again within
        ``VETO_REPEAT_S``: counted, not logged)."""
        plan = self.current_plan(now)
        for intent, why in drops:
            key = ("router", intent.strategy, intent.symbol, intent.side, intent.purpose, why)
            memo = self._veto_memo.get(key)
            if memo is not None and now - memo[0] < VETO_REPEAT_S:
                self._veto_memo[key] = (memo[0], memo[1] + 1)
                continue
            features = self._decision_features(intent, snap)
            if memo is not None and memo[1]:
                features["veto_repeats_suppressed"] = memo[1]
            self._veto_memo[key] = (now, 0)
            self.decision_log.record(
                kind="no_trade",
                decision_id=intent.id,
                strategy=intent.strategy,
                symbol=intent.symbol,
                features=features,
                plan_snapshot=plan.model_dump(mode="json") if plan is not None else None,
                intent=intent,
                notes=f"dropped by the strategy router: {why}; not sent to the RiskManager",
            )

    def _note_s1_stats(self, now: float) -> None:
        """Per coin: ticks S1 evaluated and ticks a lock-in edge reached its need (edge >= cost +
        margin on either side), plus the best excess seen (health and the shadow report)."""
        s1 = self.router.by_id(S1CrossBook.id) if self.router is not None else None
        if s1 is None:
            return
        coins = s1.diagnostics().get("coins") or {}
        for coin in sorted(coins):
            d = coins[coin] or {}
            excess: Decimal | None = None
            for side in ("tmn_cheap", "usdt_cheap"):
                edge, need = d.get(f"edge_{side}_pct"), d.get(f"need_{side}_pct")
                if edge is None or need is None:
                    continue
                try:
                    x = D(edge) - D(need)
                except (InvalidOperation, ValueError, TypeError):
                    continue
                excess = x if excess is None else max(excess, x)
            if excess is None:
                continue
            st = self.s1_stats.setdefault(
                coin, {"evaluated": 0, "opportunity_ticks": 0, "best_excess_pct": None, "last_opportunity_ts": None}
            )
            st["evaluated"] += 1
            if excess >= 0:
                st["opportunity_ticks"] += 1
                st["last_opportunity_ts"] = now
            best = st["best_excess_pct"]
            if best is None or excess > D(best):
                st["best_excess_pct"] = str(excess.quantize(Decimal("0.0000001")))

    def ingest_candles(self, candles: list[Candle]) -> int:
        """Candles from the candle task or the synthetic feed: into the in-memory cache and, the
        ones that changed, into the state DB (replay input). Never raises: a feed must not die on
        a storage hiccup (the cache is updated either way)."""
        changed = self.candles.update(candles)
        if changed:
            try:
                self.store.upsert_candles(changed)
                for c in changed:
                    self._candle_seq += 1
                    self.store.add_engine_candle(c, self.mode, self._candle_seq)
            except (sqlite3.Error, RuntimeError):
                log.exception("engine: could not store %d candles (kept in memory)", len(changed))
        return len(changed)

    def strategy_health(self, now: float | None = None) -> dict[str, Any] | None:
        """Compact strategy diagnostics for ``health.extra.strategies`` (None without strategies).

        ``plan`` is the plan in force (fresh, ``current_plan``); a plan that exists but is stale
        shows as ``plan_stale`` (its ts) with ``plan`` None: the strategies run rules-only then."""
        if self.router is None:
            return None
        t = self.clock.now() if now is None else now
        plan = self.current_plan(t)
        out: dict[str, Any] = {
            "status": self.strategy_status,
            "plan": None
            if plan is None
            else {
                "ts": plan.ts,
                "model": plan.model,
                "enabled_strategies": list(plan.enabled_strategies),
                "size_multiplier": str(plan.size_multiplier),
            },
            "router_dropped": [dict(d) for d in self.router.dropped[:ROUTER_DROPS_SHOWN]],
        }
        if plan is None and self.cfg.ai.enabled and self.plan is not None:
            out["plan_stale"] = {"ts": self.plan.ts, "age_s": round(t - self.plan.ts, 3)}
        for strat in self.router.strategies:
            diag = copy.deepcopy(strat.diagnostics() or {})
            if strat.id == S1CrossBook.id:
                coins = diag.get("coins") or {}
                compact_coins: dict[str, Any] = {}
                for coin in sorted(coins):
                    d = coins[coin] or {}
                    compact_coins[coin] = {k: d[k] for k in S1_COIN_KEYS if k in d}
                    if coin in self.s1_stats:
                        compact_coins[coin]["stats"] = dict(self.s1_stats[coin])
                diag["coins"] = compact_coins
            elif strat.id == S3Momentum.id:
                symbols = diag.get("symbols") or {}
                diag["symbols"] = {s: (symbols[s] or {}).get("status") for s in sorted(symbols)}
            out[strat.id] = diag
        return out

    def _check_flatten_complete(self, ctx: RiskContext, trigger: str) -> None:
        """Log ``flatten`` / ``complete`` once per episode; flatten_now stops the engine then."""
        if trigger in self._flatten_complete_logged:
            return
        working = any(m.purpose == "flatten" for m in self.order_manager.live())
        if working or not self.flatten.is_complete(ctx):
            return
        self._flatten_complete_logged.add(trigger)
        state = self.flatten.final_state(ctx)
        self._event(
            "flatten",
            f"{trigger} flatten complete: only TMN and dust left",
            {"trigger": trigger, "kind": "complete", **state},
        )
        if self.manual_flatten is not None:
            self.request_stop("flatten complete")

    # ------------------------------------------------------------ scheduler

    async def _scheduled(self, ctx: RiskContext) -> None:
        now = ctx.now
        reason = self._reconcile_reason
        due = reason or now - self._last_reconcile_ts >= self.cfg.engine.reconcile_interval_s
        if due and not self._startup_reconcile_due:  # the deferred startup reconcile comes first
            self._reconcile_reason = None
            await self.reconcile(ctx.books, now, reason=reason or "scheduled")
        if now - self._last_equity_ts >= self.cfg.engine.equity_row_interval_s:
            self._write_equity(now)

    async def reconcile(
        self, books: dict[str, BookSnapshot], now: float, reason: str = "scheduled"
    ) -> ReconcileReport:
        """One Reconciler pass (same task as the poll); closed positions get their outcomes."""
        report = await self.reconciler.run(books, now, reason=reason)
        report.log_outcomes(self.decision_log)
        self._last_reconcile_ts = now
        self.last_reconcile = report.summary()
        self.portfolio.refresh(books, now)
        self._prune_strategies()  # positions the reconcile closed
        if report.errors:
            log.warning("engine: reconcile (%s) errors: %s", reason, "; ".join(report.errors))
        return report

    def _write_equity(self, now: float) -> None:
        val = self.portfolio.last_valuation
        if val is None or not val.usable:
            return  # a partial valuation would read as a crash
        self._last_equity_ts = now
        self.store.add_equity(
            now,
            val.equity_tmn,
            val.tmn,
            val.usdt_tmn_value,
            val.coins_tmn_value,
            self.portfolio.peak_equity_tmn,
            self.portfolio.drawdown_pct,
        )

    def _record_inputs(self, books: dict[str, BookSnapshot], now: float) -> None:
        """Replay inputs: each book used (new (symbol, ts) only) and one row for this tick (with
        ``candle_seq``, the candle arrivals in this tick's candle snapshot, when strategies run)."""
        for symbol, book in sorted(books.items()):
            if self._stored_book_ts.get(symbol) != book.ts:
                self.store.add_engine_book(book, self.mode)
                self._stored_book_ts[symbol] = book.ts
        row: dict[str, Any] = {"ts": now, "book_ts": {s: b.ts for s, b in sorted(books.items())}}
        if self.router is not None:
            row["candle_seq"] = self._tick_candle_seq
        self.store.add_tick(now, self.mode, row)

    def _persist(self, now: float, force: bool = False) -> None:
        """kv state that changed since the last write (portfolio, flags, orders, paper exchange,
        strategies; the feature state at most every ``FEATURE_STATE_EVERY_S``). A no-op until the
        state was restored: defaults must never overwrite the saved state."""
        if not self._restored:
            return
        states: dict[str, Any] = {
            KV_PORTFOLIO: self.portfolio.to_state(),
            KV_FLAGS: self.flags.to_state(),
            KV_ORDER_MANAGER: self.order_manager.to_state(),
        }
        if self.router is not None:
            states[KV_STRATEGY] = self.router.state()
        for key, value in states.items():
            self._kv_if_changed(key, value, value, now, force)
        if self.paper is not None:
            state = self.paper.state()
            comparable = {k: v for k, v in state.items() if k != "tick"}  # the tick counter always moves
            self._kv_if_changed(KV_PAPER, state, comparable, now, force)
        if self.features is not None and (force or now - self._feature_state_ts >= FEATURE_STATE_EVERY_S):
            self._feature_state_ts = now
            state = self.features.state()
            self._kv_if_changed(KV_FEATURES, state, state, now, force)

    def _persist_flags(self, now: float) -> None:
        if self._restored:
            self._kv_if_changed(KV_FLAGS, self.flags.to_state(), self.flags.to_state(), now, force=True)

    def _checkpoint(self, now: float) -> None:
        """Commit now, also inside the tick's transaction, with the kv state of this moment (the
        OrderManager's write-ahead record before an order is sent; the kill-switch). A storage
        error is logged, never raised: its rows stay pending and the end of the tick retries them."""
        try:
            self._persist(now)
            self.store.checkpoint()
        except sqlite3.Error:
            log.exception("engine: checkpoint commit failed; retried at the end of the tick")

    def _kv_if_changed(self, key: str, value: Any, comparable: Any, now: float, force: bool) -> None:
        text = json.dumps(comparable, sort_keys=True, default=str)
        if force or self._persisted.get(key) != text:
            self.store.kv_set(key, value, now)
            self._persisted[key] = text

    # ------------------------------------------------------------ manual flatten (scripts/flatten_now.py)

    def enable_manual_flatten(self, phase1_s: float) -> None:
        """Flatten now: maker-first (phase 1) for ``phase1_s`` seconds, then MARKET (phase 2).

        No strategy runs; ``run`` stops with reason ``flatten complete`` once only TMN and dust
        are left. ``flatten_report`` holds the balances and equity before and after.
        """
        self.intent_sources = []
        self.strategies_enabled = False
        self.manual_flatten = {"phase1_s": float(phase1_s), "start_ts": None}

    def _note_flatten_before(self) -> None:
        """Manual flatten: the account as it was at the first fully priced tick, before selling."""
        if self.manual_flatten is None or self.flatten_report["before"] is not None:
            return
        val = self.portfolio.last_valuation
        if val is not None and val.usable:
            self.flatten_report["before"] = self._balance_report()

    def _balance_report(self) -> dict[str, Any]:
        val = self.portfolio.last_valuation
        return {
            "ts": self.clock.now(),
            "balances": {a: _s(b.total) for a, b in sorted(self.portfolio.balances.items()) if b.total},
            "equity_tmn": _s(val.equity_tmn) if val is not None else None,
            "tmn": _s(val.tmn) if val is not None else None,
            "missing_prices": list(val.missing_prices) if val is not None else [],
        }

    # ------------------------------------------------------------ AI layer (PRD-04 §8)

    def enable_ai(
        self,
        *,
        providers: dict[str, Any] | None = None,
        mcp: Any = "auto",
        runtime_dir: str | Path | None = None,
        runtime_overlay: dict[str, Any] | None = None,
        pinned_keys: Iterable[str] = (),
    ) -> str:
        """Attach the Regime Analyst and the Post-trade Reviewer (``wta.ai.wiring.build_llm``).

        ``providers`` replaces the HTTP providers (tests); ``mcp="auto"`` builds the Wallex MCP
        client from the settings (live feed only), None disables it, anything else is used as the
        client. ``runtime_dir`` is where the reviewer writes its overlays (default
        ``configs/runtime/<account>``, ``wiring.runtime_dir_for``); ``runtime_overlay`` is the
        ``load_runtime_overlay`` info of the overlay already merged into the config (named in the
        start event; the ConfigManager keeps building on it, including the entries the operator
        pinned, and restarts the cooldown from its ``applied_ts``). ``pinned_keys`` (the operator's
        ``--set`` / ``--overlay`` keys) are never offered to or changed by the reviewer. Returns
        the AI source: ``llm``, ``offline-rules`` or ``disabled``.
        """
        if runtime_dir is not None:
            self.runtime_dir = Path(runtime_dir)
        self.runtime_overlay = runtime_overlay
        self.pinned_keys = frozenset(pinned_keys)
        if not self.cfg.ai.enabled:  # ablation: no analyst, ``plan`` stays None
            self.ai_source = AI_DISABLED
            return self.ai_source
        setup = build_llm(
            self.cfg, self.settings, self.mode, self.clock.base, llm_cache_dir(self.cfg, self.log_dir),
            providers=providers, on_call=self._note_llm_call,
        )
        self.ai_setup = setup
        self.ai_source = setup.source
        self.mcp = build_mcp(self.settings, self.feed, self.mode) if mcp == "auto" else mcp
        view = self._view
        tools = InternalTools(
            ToolDeps(
                cfg=self.cfg,
                now=self.analyst_clock.now,
                features=lambda: view().features,
                books=lambda: view().books,
                candles_1m=lambda sym, a, b: [c for c in view().candles.get(sym, []) if a <= c.ts <= b],
                trades=lambda sym, a, b: [t for t in view().trades.get(sym, []) if a <= t.ts <= b],
                pnl_summary=lambda: view().pnl,
                recent_decisions=lambda n: view().decisions[-n:],
                health=lambda: view().health,
            )
        )
        self.analyst = RegimeAnalyst(self.cfg, setup.analyst, tools, self.mcp, self.analyst_clock, on_plan=self._on_plan,
                                     secrets=self._secrets, daily_cycle_budget=setup.analyst_cycle_budget)
        self.config_manager = ConfigManager(self.cfg, self.runtime_dir, pinned=self.pinned_keys,
                                            account=account_label(self.mode, self.feed), mode=self.mode,
                                            feed=self.feed, git_commit=self.git_commit)
        if runtime_overlay:
            # the pinned entries stay in latest.yaml (not applied now; a later review must not erase them)
            self.config_manager.overlay = {**dict(runtime_overlay.get("pinned") or {}),
                                           **dict(runtime_overlay.get("applied") or {})}
            applied_ts = runtime_overlay.get("applied_ts")
            if not runtime_overlay.get("skipped") and isinstance(applied_ts, int | float):
                # the cooldown survives a restart (an apply time "in the future" counts from now)
                self.config_manager.last_apply_ts = min(float(applied_ts), self.clock.base.now())
        self.reviewer = PostTradeReviewer(
            self.cfg,
            setup.reviewer,
            self.config_manager,
            self.reviewer_clock,
            journal_path=self.log_dir / "journal.md",
            on_event=lambda kind, reason, data: self._event_at(self.reviewer_clock.now(), kind, reason, data),
            route=setup.reviewer_route,
            secrets=self._secrets,
            daily_budget=setup.reviewer_budget,
        )
        if setup.source == AI_OFFLINE:
            log.warning("engine: %s: plans come from deterministic rules, NOT an LLM", OFFLINE_STATUS)
        return self.ai_source

    def _ai_start_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"source": self.ai_source}
        if self.ai_setup is not None:
            info["routes"] = dict(self.ai_setup.routes)
            info["cache_dir"] = str(self.ai_setup.cache.dir) if self.ai_setup.cache is not None else None
            if self.ai_setup.warnings:
                info["warnings"] = list(self.ai_setup.warnings)
        if self.ai_source == AI_OFFLINE:
            info["note"] = "offline rules provider (no LLM key): deterministic rules, NOT an LLM"
        if self.analyst is not None:
            info["mcp"] = getattr(self.mcp, "url", None) if self.mcp is not None else None
            info["slow_loop_interval_s"] = self.cfg.engine.slow_loop_interval_s
            info["reviewer_interval_s"] = self.cfg.engine.reviewer_interval_s
        return info

    def _restore_plan(self, now: float) -> dict[str, Any] | None:
        """Startup: the newest stored plan, if still fresh, is in force again (a restart must not
        drop a valid envelope for up to one slow-loop interval)."""
        if self.analyst is None or not self.cfg.ai.enabled:
            return None
        try:
            rows = self.store.load_plans(from_ts=now - self.cfg.ai.plan_stale_s)
        except Exception:  # noqa: BLE001 - no plan is a valid state
            log.exception("engine: could not read the plans table")
            return None
        if not rows:
            return None
        try:
            plan = MarketPlan.model_validate(rows[-1]["plan"])
        except ValueError:
            return None
        if now - plan.ts > self.cfg.ai.plan_stale_s:
            return None
        self.plan = plan
        self.analyst.last_plan = plan
        return {"ts": plan.ts, "model": plan.model, "prompt_hash": plan.prompt_hash}

    def _view(self) -> _AIView:
        """The analyst cycle's captured view (a fresh one outside a cycle)."""
        if self._ai_view is None:
            self._ai_view = self._capture_ai_view(self.analyst_clock.now() or self.clock.now())
        return self._ai_view

    def _capture_ai_view(self, now: float) -> _AIView:
        return _AIView(
            now=now,
            features=self.last_features,
            books=self.bus.snapshot(self.watchlist),
            candles=self.candles.snapshot(self.watchlist),
            trades={s: list(d) for s, d in sorted(self._trades.items())},
            pnl=self.pnl_summary(now),
            decisions=self.recent_decisions(RECENT_DECISIONS),
            health=self.ai_health_view(now),
        )

    def pnl_summary(self, now: float) -> dict[str, Any]:
        """``get_pnl_summary``: equity, drawdown, daily PnL, realized by strategy, fees vs budget,
        exposure and open positions (Decimals as strings)."""
        if not self.trading:
            return {"error": "monitoring only: no account"}
        p = self.portfolio
        equity, day0 = p.equity_tmn, p.day_start_equity_tmn
        budget = self.cfg.risk.fee_budget_pct * p.initial_equity_tmn
        positions = [
            {
                "id": pos.id,
                "strategy": pos.strategy,
                "symbol": pos.symbol,
                "qty": _s(pos.qty),
                "entry_price": _s(pos.entry_price),
                "entry_utc": iso(pos.entry_ts),
                "age_s": round(now - pos.entry_ts),
                "stop_price": _s(pos.stop_price),
                "target_price": _s(pos.target_price),
            }
            for _, pos in sorted(p.positions.items())
        ]
        out: dict[str, Any] = {
            "equity_tmn": _s(equity, "0.01"),
            "initial_equity_tmn": _s(p.initial_equity_tmn),
            "return_pct": _s(p.total_return_pct, "0.000001"),
            "peak_equity_tmn": _s(p.peak_equity_tmn, "0.01"),
            "drawdown_pct": _s(p.drawdown_pct, "0.000001"),
            "daily_pnl_pct": _s(p.daily_pnl_pct, "0.000001"),
            "daily_pnl_tmn": _s(equity - day0, "0.01") if equity is not None and day0 is not None else None,
            "realized_pnl_tmn": _s(p.realized_total_tmn, "0.01"),
            "realized_by_strategy": {k: _s(v, "0.01") for k, v in sorted(p.realized_pnl_tmn.items())},
            "fees_paid_tmn": _s(p.fees_paid_tmn_total, "0.01"),
            "fee_budget_tmn": _s(budget, "0.01"),
            "fee_budget_used_pct": _s(p.fees_paid_tmn_total / budget if budget else D0, "0.000001"),
            "usdt_share": _s(p.usdt_share, "0.000001"),
            "coin_exposure_pct": _s(p.coin_exposure_pct, "0.000001"),
            "orders_today": p.orders_today(now),
            "orders_last_hour": p.orders_last_hour(now),
            "open_orders": sum(1 for o in p.open_orders.values() if not o.is_terminal),
            "open_positions": positions,
            "balances": {a: _s(b.total) for a, b in sorted(p.balances.items()) if b.total},
        }
        val = p.last_valuation
        if val is not None and val.missing_prices:
            out["valuation_missing_prices"] = list(val.missing_prices)
        return out

    def ai_health_view(self, now: float) -> dict[str, Any]:
        """``get_health``: data freshness, API errors, paused/killed, flatten phase, time to deadline
        (compact; the full heartbeat is far over the tool cap)."""
        stale_s = self.cfg.engine.stale_data_s
        ages = {s: self.bus.age(s, now) for s in self.watchlist}
        known = [a for a in ages.values() if a is not None]
        stale = sorted(s for s, a in ages.items() if a is None or a > stale_s)
        f = self.flags
        out: dict[str, Any] = {
            "ts": iso(now),
            "feed": self.feed,
            "fresh_books": len(self.watchlist) - len(stale),
            "watchlist": len(self.watchlist),
            "stale_books": stale,
            "max_book_age_s": round(max(known), 1) if known else None,
            "api_consecutive_errors": self._api_consecutive_errors(),
            "api_error_burst": self._api_burst,
            "paused": f.paused,
            "pause_reason": f.pause_reason,
            "killed": f.killed,
            "kill_reason": f.kill_reason,
            "flatten_phase": f.flatten_phase,
            "time_to_deadline_s": None if self.deadline.end_ts is None else int(self.deadline.end_ts - now),
            "anomalies": list(self.last_anomalies)[:5],
            "strategy_status": self.strategy_status,
            "tick_crashes": self.tick_crashes,
        }
        if self.feed == "synthetic":
            out["synthetic"] = True
        hb = self._last_health
        if hb is not None:
            out["last_heartbeat"] = {
                "ts": hb.ts,
                "rest_ok": hb.rest_ok,
                "ws_ok": hb.ws_ok,
                "api_errors_1m": hb.api_errors_1m,
                "reasons": [r[:160] for r in hb.reasons[:6]],
            }
        return out

    def recent_decisions(self, n: int = RECENT_DECISIONS) -> list[dict[str, Any]]:
        """The last ``n`` decisions (outcomes merged), compact (``get_recent_decisions``)."""
        rows = list(self._recent_decisions.values())[-max(0, n):]
        return [compact_decision(r) for r in rows]

    def _note_decision(self, rec: dict[str, Any]) -> None:
        """DecisionLog hook: the in-memory ring buffer behind ``get_recent_decisions``."""
        kind, did = rec.get("kind"), str(rec.get("decision_id"))
        if kind == "outcome":
            target = self._recent_decisions.get(did)
            if target is not None:
                target["outcome"] = rec.get("outcome")
            return
        if kind == "plan":
            return
        if did in self._recent_decisions:
            self._recent_decisions[did].update({k: v for k, v in rec.items() if v not in (None, "", {}, [])})
            return
        self._recent_decisions[did] = dict(rec)
        while len(self._recent_decisions) > RECENT_DECISIONS:
            self._recent_decisions.pop(next(iter(self._recent_decisions)))

    def _note_trade(self, trade: Trade) -> None:
        self._trades.setdefault(trade.symbol, deque(maxlen=TRADES_KEPT)).append(trade)

    def _note_llm_call(self, info: dict[str, Any]) -> None:
        """LLMClient ``on_call``: counters for the heartbeat (token usage is logged, PRD-04 §2)."""
        self.ai_stats["model_calls"] += 1
        if info.get("cached"):
            self.ai_stats["cached_calls"] += 1
        usage = info.get("usage") or {}
        tokens = usage.get("total_tokens")
        if not isinstance(tokens, int):
            tokens = sum(v for k in ("prompt_tokens", "completion_tokens") if isinstance(v := usage.get(k), int))
        self.ai_stats["tokens"] += tokens
        log.info(
            "llm call: role=%s %s/%s cached=%s latency_ms=%s tokens=%s fallback=%s",
            info.get("role"), info.get("provider"), info.get("model"), info.get("cached"),
            info.get("latency_ms"), tokens, info.get("fallback"),
        )

    def _analyst_state(self, now: float) -> dict[str, Any]:
        """The analyst's input (JSON only, so it is taped and replays byte for byte)."""
        view = self._view()
        features: Any = {"error": "features not available yet"}
        if view.features is not None:
            features = (
                self.analyst.first_features(view.features)
                if self.analyst is not None
                else feature_view(view.features, self.watchlist, max_bytes=4 * self.cfg.ai.tool_result_max_bytes,
                                  legend=True)
            )
        state = {
            "features": features,
            "pnl_summary": view.pnl,
            "health": view.health,
            "last_plan": self.plan.model_dump(mode="json") if self.plan is not None else None,
            "events": list(self._recent_events),
        }
        return to_jsonable(state)

    def _analyst_timeout_s(self) -> float:
        """Whole-cycle cap. Every model call has ``ai.analyst_timeout_s`` on the primary and again on
        the fallback; a cycle makes at most the tool rounds + the final answer + one JSON fix
        (``analyst_requests_per_cycle``). With MCP: up to ``MAX_CALLS_PER_ROUND`` tool calls of
        ``MCP_TIMEOUT_S`` per round, plus 30 s for the handshake. A primary that hangs on every call
        must not cancel a cycle the fallback answers."""
        ai = self.cfg.ai
        llm = self.analyst.llm if self.analyst is not None else None
        routes = 2 if llm is not None and getattr(llm, "fallback", None) is not None else 1
        cap = ai.analyst_timeout_s * analyst_requests_per_cycle(self.cfg) * routes
        if self.mcp is not None:
            cap += MCP_TIMEOUT_S * MAX_CALLS_PER_ROUND * max(0, ai.analyst_max_tool_rounds) + 30.0
        return cap

    async def slow_step(self, now: float) -> MarketPlan | None:
        """One poll of the slow loop: stale-plan check, then ``analyst.should_run`` (startup, every
        ``engine.slow_loop_interval_s``, kill/pause/flatten events, fast dollar/BTC moves; never more
        often than every 60 s) and, when due, one analyst cycle.

        ``now`` comes from the real clock (``clock.base``), never the TickClock: a tick freezes that
        clock for its whole length, and the slow loop runs during the tick's awaits. The analyst
        reads its own ``analyst_clock``, set to ``now`` for the cycle.
        """
        if self.analyst is None:
            return None
        self._check_plan_stale(now)
        if self._analyst_busy:
            return None
        due, reason = self.analyst.should_run(now, self.analyst.last_run_ts, self.last_features, list(self._ai_triggers))
        if not due:
            return None
        return await self.analyst_cycle(now, reason)

    async def analyst_cycle(self, now: float, reason: str = "manual") -> MarketPlan | None:
        """One analyst cycle at ``now``; a plan is persisted and swapped in by ``_on_plan``.

        Never raises on AI trouble (timeouts, provider errors, invalid JSON: ``None``, the last plan
        stays until it is stale, failures are counted); a strict-replay cache miss does raise.
        """
        if self.analyst is None:
            return None
        self._analyst_busy = True
        try:
            self.analyst_clock.set(now)
            self._ai_view = self._capture_ai_view(now)
            state = self._analyst_state(now)
            self._ai_triggers.clear()
            self.ai_last_reason = reason
            self.ai_stats["cycles"] += 1
            timeout = self._analyst_timeout_s()
            try:
                plan = await asyncio.wait_for(self.analyst.run(state, reason), timeout=timeout)
                error = self.analyst.last_error
            except TimeoutError:
                plan, error = None, f"analyst cycle timed out after {timeout:g}s"
                self.analyst.last_error = error
        finally:
            self._analyst_busy = False
            self._ai_view = None  # the next cycle (or a tool call outside one) captures a fresh view
        if plan is None:
            self._analyst_failed(now, reason, error)
        else:
            self._analyst_succeeded(now, plan)
        return plan

    def _on_plan(self, plan: MarketPlan, record: dict[str, Any]) -> None:
        """Analyst ``on_plan``: persist (state DB ``plans`` + ``decisions.jsonl`` kind ``plan``), then
        swap ``self.plan`` (atomic: the next tick's StrategyContext gets it)."""
        self._plan_seq += 1
        decision_id = make_decision_id(plan.ts, "AI", "PLAN", self._plan_seq)
        features = {k: v for k, v in record.items() if k not in ("notes", "config_hash")}
        features["ai_source"] = self.ai_source
        if self.ai_source == AI_OFFLINE:
            features["ai_label"] = OFFLINE_STATUS
        try:
            # stamped with the cycle's time (no await in here: a tick that is waiting on the
            # exchange meanwhile gets its own frozen time back unchanged)
            with self.clock.frozen(plan.ts):
                if self.trading:
                    self.decision_log.record(
                        kind="plan",
                        decision_id=decision_id,
                        strategy="AI",
                        symbol="PLAN",
                        features=features,
                        plan_snapshot=plan,
                        notes=plan.notes,
                        config_hash=record.get("config_hash"),  # the config the plan was made under
                    )
                self.store.add_plan(plan.ts, plan.model_dump(mode="json"), plan.prompt_hash)
        finally:
            self.plan = plan
            self._stale_plan_logged = None

    def _event_at(self, now: float, kind: str, reason: str, data: dict[str, Any]) -> None:
        """An event of the slow loop or the reviewer, stamped with that task's time (synchronous:
        it never interleaves with a tick)."""
        with self.clock.frozen(now):
            self._event(kind, reason, data)

    def _analyst_succeeded(self, now: float, plan: MarketPlan) -> None:
        self.ai_stats["plans"] += 1
        if self.llm_consecutive_failures:
            self._event_at(
                now,
                "ai_recovered",
                f"analyst plan again after {self.llm_consecutive_failures} failed cycle(s)",
                {"model": plan.model, "prompt_hash": plan.prompt_hash, "ai_source": self.ai_source},
            )
        self.llm_consecutive_failures = 0
        self._llm_failure_alerted = False
        self.ai_last_error = ""
        log.info(
            "analyst: plan %s (%s, cached=%s, tools %s via %s, %s ms): %s",
            plan.prompt_hash[:12], plan.model, plan.cached, ",".join(plan.tools_used) or "-", plan.tools_source,
            plan.latency_ms, " ".join(plan.notes.split())[:200],
        )

    def _analyst_failed(self, now: float, reason: str, error: str) -> None:
        self.ai_stats["failures"] += 1
        self.llm_consecutive_failures += 1
        self.ai_last_error = mask_secrets(error or "no plan", self._secrets)[:300]
        keep = (
            f"the plan from {iso(self.plan.ts)} stays in force until it is {self.cfg.ai.plan_stale_s:g}s old"
            if self.current_plan(now) is not None
            else "strategies run rules-only"
        )
        log.warning("analyst: no plan (%s, trigger %s): %s; %s", self.ai_source, reason, self.ai_last_error, keep)
        if self.llm_consecutive_failures == 1:
            self._event_at(
                now,
                "ai_degraded",
                f"analyst cycle failed: {self.ai_last_error}; {keep}",
                {"trigger": reason, "ai_source": self.ai_source, "consecutive_failures": 1},
            )
        if self.llm_consecutive_failures >= LLM_FAILURE_ALERT and not self._llm_failure_alerted:
            self._llm_failure_alerted = True
            self._alert(tg.fmt_llm_failures(self.llm_consecutive_failures, self.ai_last_error), "llm")

    def _check_plan_stale(self, now: float) -> None:
        """A plan older than ``ai.plan_stale_s`` is ignored by the strategies; say so once per plan."""
        plan = self.plan
        if plan is None or not self.cfg.ai.enabled:
            return
        age = now - plan.ts
        if age <= self.cfg.ai.plan_stale_s or self._stale_plan_logged == plan.ts:
            return
        self._stale_plan_logged = plan.ts
        text = (
            f"plan from {iso(plan.ts)} is {age:.0f}s old (> ai.plan_stale_s {self.cfg.ai.plan_stale_s:g}s): "
            "ignored; strategies run rules-only until a new plan arrives"
        )
        log.warning("engine: %s", text)
        self._event_at(now, "plan_stale", text, {"plan_ts": plan.ts, "age_s": round(age, 3), "model": plan.model,
                                                 "prompt_hash": plan.prompt_hash})

    async def _slow_loop(self) -> None:
        """The analyst task (supervised, never awaited by the fast loop): first once a tick has
        computed features (at most ``SLOW_LOOP_FIRST_WAIT_S`` after the loop starts), then a
        ``slow_step`` every ``SLOW_LOOP_POLL_S``. A strict-replay cache miss stops the engine
        (determinism failure)."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + SLOW_LOOP_FIRST_WAIT_S
        while not self._stop.is_set() and loop.time() < give_up and (
            self.ticks == 0 or (self.router is not None and self.last_features is None)
        ):
            await asyncio.sleep(0.2)
        while not self._stop.is_set():
            try:
                await self.slow_step(self.clock.base.now())
            except CacheMissError as exc:
                log.error("engine: replay cache miss in the slow loop: %s", exc)
                self.request_stop(f"replay determinism failure: {exc}")
                return
            await asyncio.sleep(SLOW_LOOP_POLL_S)

    # ------------------------------------------------------------ reviewer + config reload (PRD-04 §7)

    async def review_step(self, now: float) -> Any:
        """One poll of the reviewer task: a review every ``engine.reviewer_interval_s`` (the first one
        that long after the start)."""
        if self.reviewer is None or self._reviewer_busy:
            return None
        if self._next_review_ts is None:
            start = self.started_ts if self.started_ts is not None else now
            self._next_review_ts = start + self.cfg.engine.reviewer_interval_s
        if now < self._next_review_ts:
            return None
        self._next_review_ts = now + self.cfg.engine.reviewer_interval_s
        return await self.review_cycle(now)

    def read_review_logs(self, now: float) -> dict[str, list[dict[str, Any]]]:
        """The hour up to ``now`` of this run's ``decisions`` (outcome updates included), ``health``
        and ``events`` lines (files only: safe in a worker thread; nothing stamped after ``now``)."""
        since = now - REVIEW_WINDOW_S
        return {base: read_log_window(self.log_dir, base, since, until=now) for base in ("decisions", "health", "events")}

    def review_input(self, now: float, logs: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        """The reviewer's input: the last hour of decisions (outcome updates included), the plans
        among them, heartbeats and events from this run's log files, plus the PnL summary."""
        assert self.reviewer is not None
        logs = self.read_review_logs(now) if logs is None else logs
        decisions = logs.get("decisions", [])
        plans = [d for d in decisions if d.get("kind") == "plan"]
        health = logs.get("health", []) + logs.get("events", [])
        return self.reviewer.build_input(decisions, plans, health, self.pnl_summary(now), window_s=REVIEW_WINDOW_S)

    async def review_cycle(self, now: float) -> Any:
        """One review at ``now``: journal entry (``<log_dir>/journal.md``), bounded changes through
        the ConfigManager, ``reload_config`` when one was accepted. Never raises on AI trouble; a
        strict-replay cache miss does."""
        if self.reviewer is None:
            return None
        self._reviewer_busy = True
        try:
            self.reviewer_clock.set(now)
            logs = await asyncio.to_thread(self.read_review_logs, now)  # file reads off the event loop
            review_input = self.review_input(now, logs)
            timeout = self.cfg.ai.reviewer_timeout_s * 4  # two attempts, each with one failover
            try:
                result = await asyncio.wait_for(self.reviewer.run(review_input), timeout=timeout)
            except TimeoutError:
                log.warning("reviewer: timed out after %gs; config unchanged", timeout)
                self.last_review = {"ts": iso(now), "skipped": f"timed out after {timeout:g}s"}
                return None
        finally:
            self._reviewer_busy = False
        self.ai_stats["reviews"] += 1
        change = result.change
        new_hash = self.config_hash
        if change is not None and change.accepted and change.config is not None:
            new_hash = change.new_hash or change.config.config_hash
            if self._in_tick:
                # a tick is suspended on an exchange await: its intents were computed under the old
                # config, so the swap waits for the next tick boundary (like the plan, ``_tick_plan``)
                self._pending_cfg = change.config
                log.info("reviewer: config %s takes effect at the next tick", new_hash)
            else:
                try:
                    self.reload_config(change.config)
                except ValueError:
                    log.exception("reviewer: change refused by reload_config; config unchanged")
                    new_hash = self.config_hash
        self.last_review = {
            "ts": iso(now),
            "model": result.model or None,
            "skipped": result.skipped_reason or None,
            "accepted": len(change.accepted) if change is not None else 0,
            "rejected": len(change.rejected) if change is not None else 0,
            "config_hash": new_hash,
        }
        return result

    async def _reviewer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.review_step(self.clock.base.now())
            except CacheMissError as exc:
                log.error("engine: replay cache miss in the reviewer: %s", exc)
                self.request_stop(f"replay determinism failure: {exc}")
                return
            await asyncio.sleep(REVIEW_POLL_S)

    def reload_config(self, new_cfg: Config) -> str:
        """Swap in a reviewer-changed config (PRD-04 §8): strategies (their S1Cfg/S2Cfg/S3Cfg),
        router, FeatureEngine, analyst; the decision log stamps the new ``config_hash``.

        Only reviewer-editable paths may differ: ``risk.*``, ``execution.*`` and the rest are
        refused (``ValueError``), so the RiskManager and the OrderManager keep their parameters
        and their authority. Call it between ticks (``review_cycle`` defers it to the next tick
        boundary while a tick runs). A running analyst cycle keeps the config it started with
        (``RegimeAnalyst.cycle_cfg``).
        """
        old_tree = _flat(self.cfg.model_dump(mode="json"))
        new_tree = _flat(new_cfg.model_dump(mode="json"))
        changed = sorted(k for k in set(old_tree) | set(new_tree) if old_tree.get(k) != new_tree.get(k))
        editable = set(self.cfg.llm.reviewer.editable)
        refused = [k for k in changed if k not in editable]
        if refused:
            raise ValueError(f"reload_config: not reviewer-editable: {', '.join(refused)}")
        old_hash = self.config_hash
        self.cfg = new_cfg
        self.config_hash = new_cfg.config_hash
        if self.router is not None:
            self.router.cfg = new_cfg
            for strat in self.router.strategies:
                section = STRATEGY_CFG.get(strat.id)
                if section is not None and hasattr(strat, "cfg"):
                    strat.cfg = getattr(new_cfg.strategies, section)  # type: ignore[attr-defined]
        if self.features is not None:
            self.features.cfg = new_cfg
        if self.analyst is not None:
            self.analyst.cfg = new_cfg
        if self.trading:
            self.decision_log.set_config_hash(self.config_hash)
        log.info("engine: config reloaded %s -> %s (%s)", old_hash, self.config_hash, ", ".join(changed) or "no change")
        return self.config_hash

    # ------------------------------------------------------------ Telegram (PRD-06 §2)

    def enable_telegram(
        self,
        notifier: tg.TelegramNotifier | None = None,
        commands: tg.TelegramCommands | None = None,
        *,
        transport: Any = None,
        commands_enabled: bool = True,
    ) -> bool:
        """Alerts (and the /status /pause /resume /kill commands) when ``TELEGRAM_BOT_TOKEN`` and
        ``TELEGRAM_CHAT_ID`` are set and ``alerts.telegram_enabled`` is true. Returns True if on.

        The inbound commands also need ``alerts.telegram_commands_enabled`` and ``commands_enabled``
        (flatten_now passes False): only one engine per bot may poll ``getUpdates``, else a /kill
        meant for the live engine can land in another one. A poller that gets HTTP 409 (another
        instance owns the bot) stops and says so in the heartbeat reasons."""
        prefix = f"[wta {self.mode}{' synthetic' if self.feed == 'synthetic' else ''}]"
        if notifier is None:
            candidate = tg.TelegramNotifier.from_settings(
                self.settings, self.cfg.alerts, self.clock.base, prefix=prefix, transport=transport
            )
            notifier = candidate if candidate.enabled else None
        wants_commands = commands_enabled and self.cfg.alerts.telegram_commands_enabled
        if commands is None and notifier is not None and wants_commands:
            cmd = tg.TelegramCommands.from_settings(
                self.settings, self.cfg.alerts, self._telegram_command, self.clock.base, prefix=prefix,
                transport=transport,
            )
            commands = cmd if cmd.enabled else None
        self.notifier, self.telegram_commands = notifier, commands
        return notifier is not None

    def _alert(self, text: str, kind: str, dedupe_key: str | None = None) -> None:
        """Fire-and-forget: a slow Telegram never delays a tick (``send_nowait``)."""
        if self.notifier is not None:
            self.notifier.send_nowait(mask_secrets(text, self._secrets), kind, dedupe_key=dedupe_key)

    async def _drain_alerts(self) -> None:
        if self.notifier is None:
            return
        try:
            await asyncio.wait_for(self.notifier.drain(), timeout=ALERT_DRAIN_S)
            await asyncio.wait_for(self.notifier.flush_digest(force=True), timeout=ALERT_DRAIN_S)
        except (TimeoutError, Exception):  # noqa: BLE001 - alerts never block a shutdown
            log.warning("engine: telegram alerts not fully delivered at shutdown")

    def _event_alert(self, kind: str, reason: str, data: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        text: str | None = None
        if kind in ("start", "restart", "stop"):
            equity = self.portfolio.equity_tmn if self.trading else None
            text = tg.fmt_start_stop(kind, self.mode, git_commit=self.git_commit, config_hash=self.config_hash,
                                     reason=reason, equity=equity)
        elif kind in ("pause", "resume", "kill", "reset_kill_applied"):
            text = tg.fmt_risk("reset_kill" if kind == "reset_kill_applied" else kind, reason,
                               source=str(data.get("source") or ""))
        elif kind == "flatten":
            sub = str(data.get("kind") or data.get("phase") or "")
            text = tg.fmt_flatten("kill" if sub == "kill_flatten_start" else sub, reason)
        elif kind == "config_change":
            diff = "; ".join(f"{d.get('path')}: {d.get('old')} -> {d.get('new')}" for d in data.get("diff") or [])
            text = f"CONFIG CHANGE ({data.get('source', '?')}): {data.get('old_hash')} -> {data.get('new_hash')}\n{diff}"
        elif kind == "final_state":
            text = f"FINAL STATE: {reason}"
        elif kind == "tick_error":
            text = f"TICK ERROR: {reason}"
        if text is not None:
            self._alert(text, kind)

    async def _telegram_command(self, command: str, args: str) -> str:
        """``TelegramCommands.on_command``: operational controls only (no trading). /status reads;
        /pause, /resume, /kill are queued and applied at the start of the next tick."""
        now = self.clock.base.now()
        self._event(
            "operator_command",
            f"/{command} via Telegram" + (f": {args}" if args else ""),
            {"command": command, "args": args, "source": "telegram"},
        )
        if command == "status":  # file and read-only DB reads: off the event loop
            return await asyncio.to_thread(self.status_text, now)
        if command not in ("pause", "resume", "kill"):
            return f"/{command}: not supported"
        self._operator_requests.append((command, args, now))
        return {
            "pause": "pause requested: no new entries from the next tick (entry orders are canceled)",
            "resume": "resume requested: lifts a /pause at the next tick (risk pauses and a kill stay)",
            "kill": "kill-switch requested: at the next tick entries are canceled and everything is "
            "flattened to TMN; only `wta reset-kill` clears it",
        }[command]

    def status_text(self, now: float, width: int = 60) -> str:
        """``wta status`` text for this engine's logs and state DB (Telegram /status)."""
        try:
            st = status_for_config(self.cfg, now, log_dir=self.log_dir, mode=self.mode, feed=self.feed)
            return format_status(st, width=width)
        except Exception as exc:  # noqa: BLE001 - a status reply must never break the poller
            return f"status unavailable: {type(exc).__name__}"

    async def _apply_operator_requests(self, now: float) -> None:
        """Telegram /pause /resume /kill, inside the tick (clock frozen, one transaction)."""
        requests, self._operator_requests = self._operator_requests, []
        for command, args, _ts in requests:
            why = args or "operator request"
            base = {"command": command, "args": args, "source": "telegram"}
            if command == "kill":
                if self.flags.killed:
                    self._event("operator_command_ignored", "/kill: the kill-switch is already active", base)
                    continue
                await self._kill(f"telegram /kill: {why}", now, source="telegram")
            elif command == "pause":
                if self.flags.killed or self.flags.paused:
                    state = f"killed ({self.flags.kill_reason})" if self.flags.killed else f"paused ({self.flags.pause_reason})"
                    self._event("operator_command_ignored", f"/pause: already {state}", base)
                    continue
                self.flags.paused, self.flags.paused_until_ts = True, None
                self.flags.pause_reason = f"{MANUAL_PAUSE_PREFIX} {why}"
                canceled = await self.order_manager.cancel_where(cancel_at_flatten_start, "pause")
                self._balances_stale = True
                self._event("pause", self.flags.pause_reason,
                            {"source": "telegram", "entries_canceled": canceled, **self._risk_snapshot()})
                self._persist_flags(now)
            elif command == "resume":
                if not (self.flags.paused and self.flags.pause_reason.startswith(MANUAL_PAUSE_PREFIX)):
                    self._event(
                        "operator_command_ignored",
                        "/resume lifts only a /pause: risk pauses clear by their own rule, a kill needs wta reset-kill",
                        {**base, "paused": self.flags.paused, "pause_reason": self.flags.pause_reason,
                         "killed": self.flags.killed},
                    )
                    continue
                previous = self.flags.pause_reason
                self.flags.paused, self.flags.pause_reason, self.flags.paused_until_ts = False, "", None
                self._event("resume", f"telegram /resume: {why}", {"source": "telegram", "previous_reason": previous})
                self._persist_flags(now)

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
        if self.feed == "synthetic":
            if not self.bus.source_ok("synthetic", now, stale_s):
                reasons.append("synthetic feed: no fresh data")
        elif not rest_ok:
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
            "feed": self.feed,
            "uptime_s": round(now - (self.started_ts or now), 3),
            "ticks": self.ticks,
            "book_age_s": {s: None if a is None else round(a, 3) for s, a in self.book_ages.items()},
            "rest": rest_health,
            "rest_poller": None
            if poller is None
            else {
                "cycles": poller.cycles,
                "errors": poller.errors,
                "consecutive_failures": poller.consecutive_failures,
            },
            "ws": self.ws.stats() if callable(getattr(self.ws, "stats", None)) else None,
            "task_crashes": dict(self.task_crashes),
            "tick_crashes": self.tick_crashes,
            "git_dirty": self.git_dirty,
        }
        if self.feed == "synthetic":
            extra["note"] = SYNTHETIC_NOTE
        equity = drawdown = None
        open_orders = positions = 0
        if self.trading:
            p, f = self.portfolio, self.flags
            val = p.last_valuation
            equity, drawdown = p.equity_tmn, p.drawdown_pct
            open_orders = sum(1 for o in p.open_orders.values() if not o.is_terminal)
            positions = len(p.positions)
            if f.killed:
                reasons.append(f"killed: {f.kill_reason}")
            if f.paused:
                reasons.append(f"paused: {f.pause_reason}")
            if f.flatten_phase:
                reasons.append(f"flatten phase {f.flatten_phase}")
            if val is not None and val.missing_prices:
                reasons.append("valuation incomplete: no price for " + ", ".join(val.missing_prices))
            budget = self.cfg.risk.fee_budget_pct * p.initial_equity_tmn
            extra.update(
                markets_source=self.markets_source,
                flatten_phase=f.flatten_phase,
                daily_pnl_pct=_s(p.daily_pnl_pct, "0.000001"),
                fees_paid_tmn=_s(p.fees_paid_tmn_total, "0.01"),
                fee_budget_used_pct=_s(p.fees_paid_tmn_total / budget if budget else Decimal(0), "0.000001"),
                usdt_share=_s(p.usdt_share, "0.000001"),
                coin_exposure_pct=_s(p.coin_exposure_pct, "0.000001"),
                orders_today=p.orders_today(now),
                orders_last_hour=p.orders_last_hour(now),
                peak_equity_tmn=_s(p.peak_equity_tmn),
                realized_pnl_tmn=_s(p.realized_total_tmn, "0.01"),
                positions=sorted(p.positions),
                anomalies=list(self.last_anomalies),
                last_reconcile=None if self.last_reconcile is None else self.last_reconcile["ts"],
            )
            if self.paper is not None:
                extra["paper_tick"] = self.paper.tick_count
            strategies = self.strategy_health(now)
            if strategies is not None:
                extra["strategies"] = strategies
            extra["candles"] = {
                "bars": {s: self.candles.count(s) for s in self.watchlist},
                "refresher": self.candle_refresher.stats() if self.candle_refresher is not None else None,
            }
        ai = self.ai_health(now)
        extra["ai_source"] = ai["ai_source"]
        extra["ai"] = ai
        if ai.get("mcp_ok") is not None:
            extra["mcp_ok"] = ai["mcp_ok"]
        if ai["llm_ok"] is False:
            reasons.append(
                f"llm: {ai['llm_consecutive_failures']} failed analyst cycle(s) in a row"
                f" ({ai.get('last_error') or 'no plan'}); "
                + ("the last plan stays until it is stale" if ai.get("plan_fresh") else "strategies run rules-only")
            )
        if ai.get("plan_stale"):
            reasons.append(f"plan stale ({ai['plan_age_s']:.0f}s > {self.cfg.ai.plan_stale_s:g}s): rules-only")
        if self.notifier is not None:
            extra["telegram"] = self.notifier.stats()
            if self.telegram_commands is not None:
                extra["telegram_commands"] = self.telegram_commands.stats()
        if self.telegram_commands is not None and self.telegram_commands.stopped_reason:
            reasons.append(f"telegram commands off: {self.telegram_commands.stopped_reason}")
        return HealthRecord(
            ts=iso(now),
            mode=self.mode,
            git_commit=self.git_commit,
            config_hash=self.config_hash,
            equity_tmn=equity,
            drawdown_pct=drawdown,
            open_orders=open_orders,
            positions=positions,
            ws_ok=ws_ok,
            rest_ok=rest_ok,
            api_errors_1m=self.api_errors_1m(now),
            llm_ok=ai["llm_ok"],
            paused=self.flags.paused,
            killed=self.flags.killed,
            reasons=reasons,
            extra=extra,
        )

    def ai_health(self, now: float) -> dict[str, Any]:
        """``health.extra.ai`` (and ``HealthRecord.llm_ok``).

        ``llm_ok``: None while no LLM is in use (``ai.enabled`` false, the offline rules provider —
        it is not an LLM — or no cycle yet), False after a failed analyst cycle (provider error,
        timeout, invalid JSON twice, budget), True after a plan from the LLM. ``ai_source``:
        ``llm``, ``offline-rules`` or ``disabled``.
        """
        plan = self.plan
        fresh = self.current_plan(now)
        out: dict[str, Any] = {
            "ai_source": self.ai_source,
            "llm_ok": None,
            "plan_age_s": None if plan is None else round(now - plan.ts, 3),
            "plan_model": None if plan is None else plan.model,
            "plan_fresh": fresh is not None,
            "plan_stale": plan is not None and fresh is None and self.cfg.ai.enabled,
            "tools_source": None if plan is None else plan.tools_source,
            "llm_consecutive_failures": self.llm_consecutive_failures,
        }
        if self.ai_source == AI_OFFLINE:
            out["label"] = OFFLINE_STATUS
        if self.analyst is None:
            return out
        if self.ai_source == AI_LLM and self.ai_stats["cycles"]:
            out["llm_ok"] = self.llm_consecutive_failures == 0
        mcp = self.mcp
        out["mcp_ok"] = None if mcp is None else (not self.analyst.mcp_error and self.analyst._mcp_schemas is not None)
        if self.analyst.mcp_error:
            out["mcp_error"] = mask_secrets(self.analyst.mcp_error, self._secrets)[:200]
        out.update(
            last_cycle_ts=None if self.analyst.last_run_ts is None else iso(self.analyst.last_run_ts),
            last_reason=self.ai_last_reason or None,
            stats=dict(self.ai_stats),
        )
        if self.ai_last_error:
            out["last_error"] = self.ai_last_error
        if self.ai_setup is not None and self.ai_setup.routes:
            out["routes"] = dict(self.ai_setup.routes)
        if self.last_review is not None:
            out["last_review"] = dict(self.last_review)
        return out

    def write_heartbeat(self, now: float) -> HealthRecord:
        record = self.health_record(now)
        self.health_log.write(record)
        self.heartbeats += 1
        self._last_health = record
        fresh = sum(1 for s in self.watchlist if s not in self.stale_symbols)
        ai = record.extra.get("ai") or {}
        log.info(
            "heartbeat: feed=%s rest_ok=%s ws_ok=%s fresh_books=%d/%d equity=%s killed=%s paused=%s ai=%s llm_ok=%s "
            "plan_age_s=%s",
            self.feed,
            record.rest_ok,
            record.ws_ok,
            fresh,
            len(self.watchlist),
            record.equity_tmn,
            record.killed,
            record.paused,
            ai.get("ai_source"),
            record.llm_ok,
            ai.get("plan_age_s"),
        )
        self._heartbeat_alerts(record, now)
        return record

    def _heartbeat_alerts(self, record: HealthRecord, now: float) -> None:
        """Telegram: API error bursts seen by the heartbeat and the hourly equity line."""
        if self.notifier is None:
            return
        if record.api_errors_1m >= API_ERRORS_ALERT_1M:
            self._alert(tg.fmt_api_burst(record.api_errors_1m), "api", dedupe_key="api_errors_1m")
        if not self.trading or record.equity_tmn is None:
            return
        if self._last_equity_alert_ts is None:
            self._last_equity_alert_ts = now  # the start alert already carries the equity
            return
        if now - self._last_equity_alert_ts >= EQUITY_ALERT_EVERY_S:
            self._last_equity_alert_ts = now
            p = self.portfolio
            self._alert(
                tg.fmt_equity_hourly(record.equity_tmn, p.total_return_pct, p.drawdown_pct,
                                     peak=p.peak_equity_tmn, daily_pnl=p.daily_pnl_pct),
                "equity",
            )

    def _event(self, kind: str, reason: str, data: dict[str, Any]) -> None:
        """``events.jsonl`` + the store's ``events`` table. A failing store never blocks the log.
        Then: the analyst's recent events and triggers (kill/pause/flatten) and the Telegram alert."""
        self.events_log.log(kind, reason, data)
        try:
            self.store.add_event(self.clock.now(), kind, {"reason": reason, **data})
        except Exception:
            log.exception("engine: could not store event %s", kind)
        try:
            self._recent_events.append(
                {"ts": iso(self.clock.now()), "kind": kind, "reason": " ".join(str(reason).split())[:200]}
            )
            if kind in AI_TRIGGER_EVENTS:
                self._ai_triggers.append({"kind": kind, "data": {k: data.get(k) for k in ("kind", "trigger")}})
            self._event_alert(kind, reason, data)
        except Exception:  # noqa: BLE001 - bookkeeping must never break the event log
            log.exception("engine: event hooks failed for %s", kind)


# ---------------------------------------------------------------- wiring


async def build_engine(
    cfg: Config,
    settings: Settings,
    mode: str = "shadow",
    *,
    feed: str = "live",
    log_dir: str | Path = "logs",
    clock: Clock | None = None,
    injected: list[InjectedIntent] | None = None,
    paper_balances: dict[str, Decimal] | None = None,
    synthetic_shock: tuple[float, float] | None = None,
    i_understand_live: bool = False,
    allow_dirty: bool = False,
    kill_file: Path | None = KILL_FILE,
    require_deadline: bool = True,
    strategies: list[Strategy] | None = None,
    overlays: list[str] | None = None,
    ai: bool = True,
    ai_providers: dict[str, Any] | None = None,
    mcp: Any = "auto",
    runtime_overlay: bool = True,
    runtime_dir: str | Path | None = None,
    pinned_keys: Iterable[str] = (),
    telegram: bool = True,
    telegram_commands: bool = True,
    telegram_transport: Any = None,
) -> Engine:
    """Build a trading engine: markets, state DB, data feed, exchange (paper or Wallex), strategies,
    the AI layer and the Telegram alerts.

    * markets: ``load_markets(rest)`` (``/v1/markets`` + fees, else the offline snapshot). Live mode
      refuses the snapshot.
    * store: the state DB of ``mode`` and ``feed`` (``state_db_path``); shadow, synthetic and live
      never share one.
    * feed ``live``: REST poller (+ WebSocket when enabled) and the candle refresher;
      ``synthetic``: the deterministic offline feed seeded with the snapshot's reference
      mids/spreads, its candles into the engine's cache and the state DB.
    * shadow: ``PaperExchange`` on the bus books; its state is restored from kv ``paper_state``
      (``paper_balances`` only seed a store without one). live: the Wallex REST client, only after
      the live guard (``wta.safety``) passed — and, with ``require_deadline`` (everything but
      ``flatten_now``), only with a challenge end time in the future.
    * strategies: ``default_strategies(cfg)`` unless given (``[]``: none).
    * one engine per account: the lock ``<state db>.engine.lock`` (``EngineLockError`` when
      another engine or flatten_now holds it); ``Engine.aclose()`` releases it.
    * clock: wrapped in a ``TickClock`` for the engine and the paper exchange; the data feeds keep
      the real one.
    * reviewer overlay (``runtime_overlay``): ``<runtime_dir>/latest.yaml`` is merged first —
      reviewer-editable paths within bounds only, the ``pinned_keys`` (the operator's ``--set`` /
      ``--overlay`` keys) win and are never offered to the reviewer — and named with its hash in the
      start event. The default ``runtime_dir`` is the account's own (``configs/runtime/<mode>`` on
      the live feed, ``configs/runtime/<mode>_<feed>`` otherwise, like the state DB), and an
      overlay written by another account is refused. With ``ai.enabled: false`` (the ablation) the
      overlay is NOT applied (``runtime_overlay.skipped`` in the start event): the rules-only run
      uses the plain configs/*.yaml values, never strategy parameters chosen by an LLM review.
    * AI (``ai``): ``Engine.enable_ai`` — LLM clients (OpenRouter primary, Gemini fallback) when a
      key is set, the offline rules provider when none is, nothing with ``ai.enabled: false``;
      ``ai_providers`` replaces the HTTP providers (tests), ``mcp`` the Wallex MCP client.
    * Telegram (``telegram``): ``Engine.enable_telegram`` (a no-op without ``TELEGRAM_*`` and
      ``alerts.telegram_enabled``). ``telegram_commands=False`` (flatten_now, a second engine) keeps
      the alerts but polls no inbound commands: exactly one engine may own the bot's getUpdates.
    """
    if mode not in ("shadow", "live"):
        raise ValueError(f"unknown mode {mode!r} (shadow or live)")
    if feed not in FEEDS:
        raise ValueError(f"unknown feed {feed!r} ({' or '.join(FEEDS)})")
    base_clock = clock.base if isinstance(clock, TickClock) else (clock or SystemClock())
    if mode == "live":
        check_live_guard(i_understand_live, allow_dirty, settings)
        if feed != "live":
            raise LiveGuardError("refusing to start live mode: live trading needs --feed live")
        if injected or paper_balances is not None:
            raise LiveGuardError(
                "refusing to start live mode: injected intents and paper balances are shadow-only"
            )
        if require_deadline:
            why = deadline_guard_reason(cfg.engine.challenge_end_utc, base_clock.now())
            if why is not None:
                raise LiveGuardError(f"refusing to start live mode: {why}")
    if synthetic_shock is not None and feed != "synthetic":
        raise ValueError("a synthetic shock needs --feed synthetic")
    rt_dir = Path(runtime_dir) if runtime_dir is not None else runtime_dir_for(mode, feed)
    pinned = frozenset(pinned_keys)
    overlay_info: dict[str, Any] | None = None
    latest_overlay = rt_dir / RUNTIME_LATEST
    if runtime_overlay and not cfg.ai.enabled:
        if latest_overlay.is_file():
            overlay_info = {"path": str(latest_overlay), "skipped": "ai.enabled is false", "applied": {}}
            log.info("engine: reviewer overlay %s not applied: ai.enabled is false (rules-only ablation)",
                     latest_overlay)
    elif runtime_overlay:
        cfg, overlay_info = load_runtime_overlay(cfg, latest_overlay, pinned, account=account_label(mode, feed))
        if overlay_info is not None:
            log.info(
                "engine: reviewer overlay %s (sha256 %s): applied %s, rejected %d; config %s -> %s%s",
                overlay_info["path"], overlay_info["sha256"], ", ".join(sorted(overlay_info["applied"])) or "nothing",
                len(overlay_info["rejected"]), overlay_info["config_hash_before"], overlay_info["config_hash_after"],
                f" (skipped: {overlay_info['skipped']})" if overlay_info.get("skipped") else "",
            )
    tick_clock = clock if isinstance(clock, TickClock) else TickClock(base_clock)
    state_path = state_db_path(cfg, mode, feed)
    lock = acquire_engine_lock(state_path)
    try:
        engine = await _build_engine(
            cfg,
            settings,
            mode,
            feed=feed,
            log_dir=log_dir,
            clock=tick_clock,
            injected=injected,
            paper_balances=paper_balances,
            synthetic_shock=synthetic_shock,
            i_understand_live=i_understand_live,
            allow_dirty=allow_dirty,
            kill_file=kill_file,
            lock=lock,
            state_path=state_path,
            strategies=default_strategies(cfg) if strategies is None else strategies,
            overlays=overlays,
        )
    except BaseException:
        release_engine_lock(lock)
        raise
    try:
        if ai:
            engine.enable_ai(providers=ai_providers, mcp=mcp, runtime_dir=rt_dir, runtime_overlay=overlay_info,
                             pinned_keys=pinned)
        else:
            engine.runtime_overlay = overlay_info
        if telegram:
            engine.enable_telegram(transport=telegram_transport, commands_enabled=telegram_commands)
    except BaseException:
        await engine.aclose()
        raise
    return engine


async def _build_engine(
    cfg: Config,
    settings: Settings,
    mode: str,
    *,
    feed: str,
    log_dir: str | Path,
    clock: TickClock,
    injected: list[InjectedIntent] | None,
    paper_balances: dict[str, Decimal] | None,
    synthetic_shock: tuple[float, float] | None,
    i_understand_live: bool,
    allow_dirty: bool,
    kill_file: Path | None,
    lock: IO[str] | None,
    state_path: str,
    strategies: list[Strategy],
    overlays: list[str] | None,
) -> Engine:
    """``build_engine`` after its guards, holding the account lock."""
    from wta.data.synthetic import SyntheticFeed, SyntheticParams
    from wta.exchange.markets import load_markets, snapshot_reference

    bus = MarketDataBus()
    rest = make_rest(cfg, settings) if feed == "live" else None
    markets, source = await load_markets(rest)
    if mode == "live" and source != "api":
        await rest.close()  # type: ignore[union-attr]
        raise LiveGuardError(
            "refusing to start live mode: /v1/markets is unreachable (only the offline snapshot)"
        )
    store = Store(
        state_path,
        flush_interval_s=cfg.data.store_flush_interval_s,
        flush_rows=cfg.data.store_flush_rows,
        depth_levels=cfg.data.depth_levels_stored,
    )
    watchlist = tradable_watchlist(cfg)
    ws = synthetic = None
    if feed == "live" and cfg.data.ws_enabled:
        from wta.exchange.wallex_ws import WallexWS

        ws = WallexWS(
            settings.wallex_ws_url,
            watchlist,
            bus,
            clock.base,
            levels=cfg.data.depth_levels_stored,
            reconnect_max_s=cfg.data.ws_reconnect_max_s,
            stale_s=cfg.engine.stale_data_s,
        )
    if feed == "synthetic":
        ref = snapshot_reference()
        params = SyntheticParams(seed=cfg.engine.rng_seed)
        if synthetic_shock is not None:
            params = dataclasses.replace(params, shock_at_s=synthetic_shock[0], shock_pct=synthetic_shock[1])
        synthetic = SyntheticFeed(
            markets=markets,
            symbols=[s for s in watchlist if s in markets],
            bus=bus,
            reference_mids=ref["mids"],
            reference_spreads=ref["spreads"],
            clock=clock.base,
            params=params,
            on_candle=None,  # set to Engine.ingest_candles below (cache + state DB)
        )
    exchange: Exchange
    if mode == "shadow":
        state = store.kv_get(KV_PAPER)
        if state is not None and paper_balances is not None:
            log.warning(
                "engine: --paper-balances ignored: the store already holds a paper account (kv paper_state)"
            )
        exchange = PaperExchange(
            markets,
            bus.snapshot,
            clock,
            initial_balances=paper_balances if state is None else None,
            initial_equity_tmn=cfg.engine.initial_equity_tmn,
            state=state,
        )
    else:
        exchange = rest  # type: ignore[assignment]  # WallexREST implements Exchange
    engine = Engine(
        cfg,
        settings,
        mode,
        clock,
        bus,
        store,
        exchange=exchange,
        rest=rest,
        ws=ws,
        log_dir=log_dir,
        i_understand_live=i_understand_live,
        allow_dirty=allow_dirty,
        markets=markets,
        markets_source=source,
        feed=feed,
        synthetic=synthetic,
        injected=injected,
        kill_file=kill_file,
        strategies=strategies,
        overlays=overlays,
    )
    if synthetic is not None:
        synthetic.on_candle = engine.ingest_candles
    engine.lock = lock
    return engine


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
    feed: str = "live",
    injected: list[InjectedIntent] | None = None,
    paper_balances: dict[str, Decimal] | None = None,
    synthetic_shock: tuple[float, float] | None = None,
    overlays: list[str] | None = None,
    **build_options: Any,
) -> int:
    """Build the trading engine (``build_engine``) and run it until stopped.

    ``mode="live"`` runs the live guards (``wta.safety``) before anything is built or started and
    raises ``LiveGuardError`` when one fails. ``overlays`` (the ``--overlay`` files already merged
    into ``cfg``) are named in the start event next to the config hash. ``build_options`` go to
    ``build_engine`` (``runtime_overlay``, ``pinned_keys``, ``ai_providers``, ...).
    """
    if mode == "live":
        check_live_guard(i_understand_live, allow_dirty, settings)
    engine = await build_engine(
        cfg,
        settings,
        mode,
        feed=feed,
        log_dir=log_dir,
        injected=injected,
        paper_balances=paper_balances,
        synthetic_shock=synthetic_shock,
        i_understand_live=i_understand_live,
        allow_dirty=allow_dirty,
        overlays=overlays,
        **build_options,
    )
    try:
        await engine.run(stop_event, seconds, handle_signals=handle_signals)
    finally:
        await engine.aclose()
    return 0


PLAN_WARMUP_S = 15.0  # ``wta plan``: wait at most this long for the books of the regime symbols


async def plan_once(
    cfg: Config,
    settings: Settings,
    *,
    feed: str = "live",
    log_dir: str | Path = "logs/shadow",
    warmup_s: float = PLAN_WARMUP_S,
    clock: Clock | None = None,
    **build_options: Any,
) -> dict[str, Any]:
    """``wta plan``: ONE analyst cycle on the shadow account's data, nothing traded or persisted.

    Builds the shadow engine of ``feed`` (its state DB, so the candle history and the cross-book
    z-score state of earlier runs are used; the account lock is taken, so this refuses to run next
    to a shadow engine on the same account), restores candles and feature state read-only, runs
    only the market-data task(s) until the books of the regime symbols arrive (at most
    ``warmup_s``), computes one FeatureSnapshot and runs one analyst cycle with reason ``manual``.
    No tick runs (no order, no exit, no kv write); the plan is returned, not stored (the LLM cache
    and tape still record the call, so it replays). Returns ``{"plan", "analyst", "ai_source",
    "error", ...}`` (JSON-safe).
    """
    build_options.setdefault("telegram", False)
    engine = await build_engine(cfg, settings, "shadow", feed=feed, log_dir=log_dir, clock=clock, **build_options)
    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    try:
        now = engine.clock.now()
        engine._restore_strategies(now)  # candles + feature/strategy state: reads only
        if engine.rest_poller is not None:
            tasks.append(asyncio.create_task(engine.rest_poller.run(), name="plan-rest"))
        if engine.synthetic is not None:
            engine.synthetic.on_candle = engine.candles.update  # in memory only: no state-DB write
            tasks.append(asyncio.create_task(engine.synthetic.run(stop), name="plan-synthetic"))
        loop = asyncio.get_running_loop()
        give_up = loop.time() + max(0.0, warmup_s)
        wanted = [s for s in REQUIRED_PLAN_BOOKS if s in engine.watchlist]
        while loop.time() < give_up and any(engine.bus.latest(s) is None for s in wanted):
            await asyncio.sleep(STARTUP_BOOKS_POLL_S)
        now = engine.clock.now()
        books = engine.bus.snapshot(engine.watchlist)
        if engine.features is not None:
            engine.last_features = engine.features.compute(now, books, engine.candles.snapshot(engine.watchlist),
                                                           engine.watchlist)
        out: dict[str, Any] = {
            "ai_source": engine.ai_source,
            "mode": "shadow",
            "feed": feed,
            "books": sorted(books),
            "ts": iso(now),
            "config_hash": engine.config_hash,
        }
        if engine.ai_source == AI_OFFLINE:
            out["label"] = OFFLINE_STATUS + ": deterministic rules, NOT an LLM"
        if feed == "synthetic":
            out["note"] = SYNTHETIC_NOTE
        if engine.analyst is None:
            out.update(plan=None, analyst=None, error="ai.enabled is false: no analyst (rules-only)")
            return out
        engine.analyst.on_plan = None  # a dry run: nothing goes to decisions.jsonl or the plans table
        plan = await engine.analyst_cycle(now, "manual")
        out["plan"] = plan.model_dump(mode="json") if plan is not None else None
        out["analyst"] = to_jsonable(engine.analyst.last_record) if plan is not None else None
        out["error"] = None if plan is not None else engine.analyst.last_error or "no plan"
        return out
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (engine.health_log, engine.events_log, engine.decision_log, engine.orders_log):
            writer.close()
        await engine.aclose()
