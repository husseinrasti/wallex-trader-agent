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
   the markets, the plan (``plan``: None until the AI layer is wired, always None with
   ``ai.enabled: false``), the config, ``engine.rng_seed`` and the flatten phase they form the
   tick's ``StrategyContext``. One id counter per tick is shared by the PositionManager exits and
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
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any, get_args

try:  # POSIX only; without it (Windows) the one-engine-per-account lock is skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from wta.config import ROOT, Config, DataCfg, Settings, mask_secrets
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
from wta.logging_.health_log import HealthLog
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
)
from wta.risk.context import Deadline, EngineFlags, RecentIntent, RiskContext
from wta.risk.flatten import FlattenManager, cancel_at_flatten_start, flatten_phase_transitions
from wta.risk.manager import RiskEvent, RiskManager
from wta.safety import LiveGuardError, check_live_guard, deadline_guard_reason, tree_is_dirty
from wta.strategies.base import PortfolioView, Strategy, StrategyContext
from wta.strategies.router import StrategyRouter
from wta.strategies.s1_crossbook import S1CrossBook
from wta.strategies.s2_usdtalloc import S2USDTAlloc
from wta.strategies.s3_momentum import S3Momentum
from wta.utils import gitinfo
from wta.utils.decimalutil import D, pct
from wta.utils.ids import OUR_PREFIXES, strategy_prefix
from wta.utils.timeutil import Clock, SystemClock, iso

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

IntentSource = Callable[[RiskContext], list[Intent]]


def state_db_path(cfg: Config, mode: str, feed: str = "live") -> str:
    """The engine's state DB for ``mode`` and ``feed`` (see "State DB per mode" in the module docstring).

    The account label is ``mode`` on the live feed and ``{mode}_{feed}`` otherwise (``shadow`` vs
    ``shadow_synthetic``): synthetic prices never touch the real-feed shadow account, its candles or
    its z-score history. ``data.state_sqlite_path`` with ``{mode}`` replaced by that label; a path
    without ``{mode}`` gets it inserted before the suffix (``x/state.sqlite`` ->
    ``x/state.shadow.sqlite``), so shadow and live can never share a file. Left at its default while
    ``data.sqlite_path`` is overridden, it is derived from the market DB instead:
    ``<dir>/<stem>.state_{mode}.sqlite``. ``:memory:`` stays in memory.
    """
    label = mode if feed == "live" else f"{mode}_{feed}"
    template = cfg.data.state_sqlite_path
    default_state = str(DataCfg.model_fields["state_sqlite_path"].default)
    default_market = str(DataCfg.model_fields["sqlite_path"].default)
    if template == default_state and cfg.data.sqlite_path != default_market:
        if cfg.data.sqlite_path == ":memory:":
            return ":memory:"
        market = Path(cfg.data.sqlite_path)
        template = str(market.parent / f"{market.stem}.state_{{mode}}.sqlite")
    if template == ":memory:":
        return template
    if "{mode}" not in template:
        path = Path(template)
        return str(path.with_name(f"{path.stem}.{label}{path.suffix}"))
    return template.replace("{mode}", label)


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
        """Close the REST client and the store, release the account lock (``build_engine`` callers)."""
        try:
            if self.rest is not None:
                await self.rest.close()
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
        """
        self._bookkeeping(now)
        if not self.trading:
            return
        if not self._restored:  # also after a failed startup: never trade or persist on defaults
            await self.startup()
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

    async def _trading_tick(self, now: float) -> None:
        if self.flags.killed:
            # first, while nothing of this tick is written yet: the read then takes no write lock
            # for the rest of the tick (``wta reset-kill`` writes the same database)
            await self._poll_kill_reset(now)
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
        """The plan in force at ``now`` (default: the clock): ``plan`` (set by the AI layer once it
        is wired) while it is fresh (``now - plan.ts <= ai.plan_stale_s``, the same rule as
        ``StrategyContext.plan_fresh``), never one while ``ai.enabled`` is false. None = rules-only
        defaults, so a decision record's ``plan_snapshot`` is always the envelope the strategies used."""
        if not self.cfg.ai.enabled or self.plan is None:
            return None
        t = self.clock.now() if now is None else now
        return self.plan if t - self.plan.ts <= self.cfg.ai.plan_stale_s else None

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
            llm_ok=None,
            paused=self.flags.paused,
            killed=self.flags.killed,
            reasons=reasons,
            extra=extra,
        )

    def write_heartbeat(self, now: float) -> HealthRecord:
        record = self.health_record(now)
        self.health_log.write(record)
        self.heartbeats += 1
        fresh = sum(1 for s in self.watchlist if s not in self.stale_symbols)
        log.info(
            "heartbeat: feed=%s rest_ok=%s ws_ok=%s fresh_books=%d/%d equity=%s killed=%s paused=%s",
            self.feed,
            record.rest_ok,
            record.ws_ok,
            fresh,
            len(self.watchlist),
            record.equity_tmn,
            record.killed,
            record.paused,
        )
        return record

    def _event(self, kind: str, reason: str, data: dict[str, Any]) -> None:
        """``events.jsonl`` + the store's ``events`` table. A failing store never blocks the log."""
        self.events_log.log(kind, reason, data)
        try:
            self.store.add_event(self.clock.now(), kind, {"reason": reason, **data})
        except Exception:
            log.exception("engine: could not store event %s", kind)


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
) -> Engine:
    """Build a trading engine: markets, state DB, data feed, exchange (paper or Wallex), strategies.

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
    tick_clock = clock if isinstance(clock, TickClock) else TickClock(base_clock)
    state_path = state_db_path(cfg, mode, feed)
    lock = acquire_engine_lock(state_path)
    try:
        return await _build_engine(
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
) -> int:
    """Build the trading engine (``build_engine``) and run it until stopped.

    ``mode="live"`` runs the live guards (``wta.safety``) before anything is built or started and
    raises ``LiveGuardError`` when one fails. ``overlays`` (the ``--overlay`` files already merged
    into ``cfg``) are named in the start event next to the config hash.
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
    )
    try:
        await engine.run(stop_event, seconds, handle_signals=handle_signals)
    finally:
        await engine.aclose()
    return 0
