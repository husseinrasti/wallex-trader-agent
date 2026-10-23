"""Replay runner (PRD-05 §6): drive the delivered engine over a recorded run and compare decisions.

``run_replay(cfg, log_dir=..., state_db=...)`` (``run_replay_async`` inside an event loop) rebuilds the
engine the recorded run used, feeds it exactly what that engine saw, tick by tick, and compares the
decision log it writes with the recorded one (``wta.replay.compare.compare_decisions``). The result,
``ReplayRun`` (a ``ReplayReport``), gives ``"N/N decisions reproduced"`` and is written to
``<reports_dir>/replay_<YYYYMMDD>.md``. ``scripts/replay_check.py`` (``make replay``) is the CLI.

Inputs, all read-only:

* ``<log_dir>/events*.jsonl``: the ``start`` event (mode, feed, config hash, overlays, AI source and
  routes, state DB, strategies); ``config_change`` events (reviewer); ``kill`` events.
* ``<log_dir>/decisions*.jsonl``: the reference decisions, the ``kind="plan"`` records (which plans
  to re-derive, and when) and the injected test intents (``features.injected``).
* ``<log_dir>/orders*.jsonl``: the recorded exchange answers (``ReplayExchange``).
* the recorded state DB: ``ticks`` (one row per fast tick: its time, the ``ts`` of every book it
  used, its ``candle_seq``), ``engine_books``, ``engine_candles``, ``balances`` and ``fills``.
* the LLM cache of the run (``<log_dir>/llm_cache`` and its ``mcp_tape``).

The procedure (one recorded process segment: by default the latest one that started fresh):

1. Config: ``cfg`` plus the start event's runtime overlay must give the recorded ``config_hash``;
   else the recorded ``data.state_sqlite_path`` pattern is tried, else the whole config taped with
   the first analyst cycle (AI runs tape it). A hash still different is an error in strict mode.
2. A fresh state DB and log dir under ``out_dir`` (mode ``replay``); ``RunnerExchange`` (a
   ``ReplayExchange``) over the recorded orders, balances rows and fills, reading the engine's
   ``TickClock``; the engine's own balances as the exchange's fallback; no REST, WebSocket, MCP or
   Telegram client at all. Two balance rules on top of ``ReplayExchange`` (``RunnerExchange``): the
   startup seed read gets the startup reconcile's row, and a refresh with no exchange-side change
   since the previous answer gets that answer without using up a recorded row.
3. The clock starts at the recorded ``start`` event; the startup reconcile runs at the recorded time
   of its balances row (the first one written before the first tick), with the first tick's books on
   the bus (a startup reconcile the recorded run deferred is deferred again).
4. For every ``ticks`` row: the clock is set to its time, the books it names are put on the bus
   (``engine_books``), the candle arrivals up to its ``candle_seq`` are fed in order
   (``Engine.ingest_candles``), then ``Engine.safe_tick``. Decision ids and client ids embed the
   tick's millisecond, so the replay asks the exchange for exactly the recorded client ids.
5. Plans: the slow loop is not run on a timer. Each recorded plan is re-derived by
   ``RegimeAnalyst.replay_cycle`` (its input from the tape, every model answer from the strict LLM
   cache, providers replaced by sentinels that count and refuse any call) and swapped in before the
   first tick after the recorded swap: the time the cycle's last answer was cached (fresh calls) or
   the cycle time plus its latency, kept within the bounds the recorded decisions give (the last
   tick still showing the previous plan, the first one showing this plan). Reviewer config changes
   are re-applied from their ``config_change`` events the same way (bounds from the decisions'
   ``config_hash``) and checked against the recorded new hash; the reviewer itself is not re-run.
6. ``compare_decisions`` (the PRD-05 fields plus the plan's content) and a strict pass (every field
   but timestamps), the exchange's divergences and the network counters make the verdict.

Zero network is asserted, not assumed: during the replay every ``AF_INET``/``AF_INET6`` connect and
every ``getaddrinfo`` is refused and counted (``network_guard``), and the LLM providers are sentinels.
``ok`` needs both counters at 0.

``strict=True`` (default): an LLM cache miss stops the replay (a determinism failure, surfaced as a
failed replay with the error) and a config hash that cannot be matched is an error. ``strict=False``
keeps going without the missing plan and only notes the config difference.

Limits (also written into every report when they apply): a recorded restart (another ``start``
event) is not reproduced: one segment is replayed and the decisions of the others are excluded and
counted; a run that resumed earlier state (start reason ``restart``) is replayed from an empty state;
heartbeats, Telegram commands and ``wta reset-kill`` are not replayed; markets come from
``configs/markets_snapshot.yaml`` (a run that loaded ``/v1/markets`` is replayed against the
snapshot); REST error counters of a live run are not recorded (the replay sees 0 errors); a run still
being written is replayed up to its last committed tick. The ``ReplayExchange`` approximations are in
its module docstring.
"""

from __future__ import annotations

import asyncio
import bisect
import errno
import json
import logging
import shutil
import socket
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from wta.ai.analyst import RegimeAnalyst, cycle_tape_key
from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, ProviderError
from wta.ai.offline_provider import REVIEWER_MARKER
from wta.ai.replay import cycle_config, cycle_routes, start_routes
from wta.ai.tools import InternalTools, ToolDeps
from wta.ai.wiring import llm_cache_dir
from wta.config import ROOT, Config, Settings, apply_changes, canonical_json
from wta.data.store import Store
from wta.engine import Engine, InjectedIntent, TickClock, default_strategies
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot
from wta.exchange.replay import ReplayCall, ReplayExchange, load_balance_snapshots, load_recorded_fills
from wta.exchange.replay import tick_ms as tick_ms_of
from wta.logging_.jsonl import iter_family
from wta.models import Balance, BookSnapshot, Candle, Fill, MarketInfo, MarketPlan, Order, OrderRequest
from wta.replay.compare import FIELDS, ReplayReport, compare_decisions, report_path
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock, iso, parse_iso, utc_day

log = logging.getLogger(__name__)

REPLAY_MODE = "replay"
# the plan in force is compared by content too, not only by prompt_hash (a changed answer is caught)
PLAN_FIELDS: tuple[str, ...] = (
    "plan.regimes",
    "plan.enabled_strategies",
    "plan.s1_margin_min_pct",
    "plan.size_multiplier",
    "plan.usdt_target_share",
    "plan.risk_flags",
    "plan.notes",
    "plan.model",
)
COMPARE_FIELDS: tuple[str, ...] = FIELDS + PLAN_FIELDS
# strict pass: differences that are true by construction of a replay (kind, field)
BENIGN_STRICT: frozenset[tuple[str, str]] = frozenset({("plan", "features.cached")})
SYNTHETIC_BANNER = "SYNTHETIC — not evidence of performance"
SYNTHETIC_BANNER_LONG = "SYNTHETIC DATA — not evidence of performance"  # what the reports index looks for
INJECT_PREFIX = "[injected for acceptance testing] "
BOOK_CHUNK_S = 600.0  # engine_books are read in windows of this many seconds
CANDLE_CHUNK = 20_000  # engine_candles are read in seq windows of this size
MAX_LISTED = 40  # strict-pass mismatches listed in the report
REVIEW_SPAN_S = 600.0  # a reviewer answer cached this long after its event still belongs to it


class ReplayInputError(ValueError):
    """The recorded run cannot be replayed (no start event, no ticks, no state DB, ...)."""


# ---------------------------------------------------------------- zero network


@contextmanager
def network_guard() -> Iterator[list[str]]:
    """Refuse and count every internet connection attempt (``AF_INET``/``AF_INET6`` connects,
    ``create_connection``, ``getaddrinfo``) while active; local sockets (``AF_UNIX``) pass. Yields
    the list of attempts (empty = zero network)."""
    attempts: list[str] = []
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection
    orig_gai = socket.getaddrinfo
    inet = (socket.AF_INET, socket.AF_INET6)

    def refuse(what: str) -> OSError:
        attempts.append(what)
        return OSError(errno.ENETUNREACH, f"wta replay: network access refused ({what})")

    def connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if self.family in inet:
            raise refuse(f"connect {address!r}")
        return orig_connect(self, address, *args, **kwargs)

    def connect_ex(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if self.family in inet:
            raise refuse(f"connect_ex {address!r}")
        return orig_connect_ex(self, address, *args, **kwargs)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise refuse(f"create_connection {address!r}")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        raise refuse(f"getaddrinfo {host!r}")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_gai  # type: ignore[assignment]


class _NoProvider:
    """Stands in for every LLM provider: counts and refuses each call (a replay never gets here)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls += 1
        raise ProviderError(f"replay: provider {self.name} must not be called")


# ---------------------------------------------------------------- clock


class ReplayClock(SimClock):
    """The replay's base clock: ``set`` per tick. ``jump_when(cond, t)`` moves it to ``t`` at the first
    read once ``cond()`` is true: the startup reconcile reads the clock again after the engine stamped
    its start (``Engine.started_ts``), and must read the recorded reconcile time."""

    def __init__(self, start: float = 0.0) -> None:
        super().__init__(start)
        self._jump: tuple[Callable[[], bool], float] | None = None

    def jump_when(self, cond: Callable[[], bool], t: float) -> None:
        self._jump = (cond, float(t))

    def now(self) -> float:
        if self._jump is not None and self._jump[0]():
            self.set(self._jump[1])
        return super().now()

    def set(self, t: float) -> None:
        self._jump = None
        super().set(t)


class RunnerExchange(ReplayExchange):
    """``ReplayExchange`` with two balance rules of the runner (see ``get_balances``)."""

    def __init__(self, *args: Any, clock: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, clock=clock, **kwargs)
        self.seed: dict[str, Balance] | None = None
        self.unchanged_reads = 0  # refreshes answered by the "nothing changed" rule
        self._seed_clock = clock
        self._changed = True  # an exchange-side change since the last balance answer
        self._last_balances: dict[str, Balance] | None = None
        self._order_state: dict[str, tuple[Any, ...]] = {}
        self._trades_seen: tuple[str, ...] = ()

    def _now_or_none(self) -> float | None:
        return self._seed_clock.now() if self._seed_clock is not None else None

    async def place_order(self, req: OrderRequest) -> Order:
        self._changed = True
        return await super().place_order(req)

    async def cancel_order(self, client_id: str) -> Order | None:
        self._changed = True
        return await super().cancel_order(client_id)

    async def get_order(self, client_id: str) -> Order | None:
        order = await super().get_order(client_id)
        state = (None,) if order is None else (order.status, order.filled_qty, order.fee_paid)
        if self._order_state.get(client_id) != state:
            self._order_state[client_id] = state
            self._changed = True
        return order

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        trades = await super().get_my_trades(symbol, since_ts)
        seen = tuple(sorted(t.trade_id for t in trades))
        if seen != self._trades_seen:
            self._trades_seen = seen
            self._changed = True
        return trades

    async def get_balances(self) -> dict[str, Balance]:
        """Two rules on top of ``ReplayExchange.get_balances``:

        * Startup seed. On a fresh store the engine seeds its balances at the start event's time,
          then reconciles; the reconcile wrote the row. ``ReplayExchange`` serves the seed only from
          a row of the read's own millisecond, but a real run reconciles a few hundred ms after the
          start event (it waits for books). The runner arms ``seed`` with that row right before
          ``Engine.startup()``; the first read gets it without using it up.
        * Nothing changed. A refresh (not the Reconciler's read) with no exchange-side change since
          the previous balance answer (no placement, no cancel, no new state of a polled order, no
          new trade) got the previous answer; when that is also the engine's balances the recorded
          engine wrote no row, so no row of the tick may be used up (``ReplayExchange`` would hand
          it the row a later read of the same tick wrote, e.g. after the kill-switch's flatten SELL).
        """
        seed, self.seed = self.seed, None
        if seed is not None:
            self.calls.append(ReplayCall("get_balances", self._now_or_none(), None, None, "recorded"))
            answer = dict(seed)
        else:
            last = self.calls[-1] if self.calls else None
            now = self._now_or_none()
            reconciler = (last is not None and last.method == "get_my_trades" and last.ts is not None
                          and now is not None and tick_ms_of(last.ts) == tick_ms_of(now))
            current = self._fallback() if self._fallback is not None else None
            if (not self._changed and not reconciler and current and self._last_balances is not None
                    and dict(current) == self._last_balances):
                self.unchanged_reads += 1
                self.calls.append(ReplayCall("get_balances", now, None, None, "fallback"))
                answer = {a: current[a] for a in sorted(current)}
            else:
                answer = await super().get_balances()
        self._changed = False
        self._last_balances = dict(answer)
        return answer


# ---------------------------------------------------------------- the recorded run


@dataclass
class PlanSlot:
    """A recorded plan and the tick before which the replay swaps it in."""

    decision_id: str
    prompt_hash: str
    plan_ts: float
    swap_ts: float
    swap_source: str  # "cache" (time its last answer was cached) or "latency" (cycle time + latency)
    lower: int  # last tick index that still showed an earlier plan (-1: none)
    upper: int  # first tick index that showed this or a later plan (n: none)
    index: int = 0  # the activation tick (n = after the last tick)
    conflict: bool = False
    record: dict[str, Any] = field(default_factory=dict)
    reproduced: bool | None = None
    error: str = ""


@dataclass
class ConfigSlot:
    """A recorded reviewer config change and the tick before which the replay applies it."""

    ts: float
    old_hash: str
    new_hash: str
    diff: list[dict[str, Any]]
    done_ts: float
    lower: int
    upper: int
    index: int = 0
    conflict: bool = False
    applied_hash: str = ""
    error: str = ""


@dataclass
class RecordedRun:
    """What the replay needs from the recorded logs and state DB (see the module docstring)."""

    log_dir: Path
    state_db: Path
    start: dict[str, Any]
    start_ts: float
    mode: str
    feed: str
    config_hash: str
    segment: int
    segments: int
    segment_end: float | None
    ticks: list[dict[str, Any]]
    tick_ts: list[float]
    decisions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    events: list[dict[str, Any]]
    excluded: dict[str, int]
    in_progress: bool
    balances: list[tuple[float, Any]]
    fills: list[Any]

    @property
    def data(self) -> dict[str, Any]:
        return dict(self.start.get("data") or {})

    @property
    def synthetic(self) -> bool:
        return self.feed == "synthetic" or bool(self.data.get("synthetic"))

    @property
    def first_ts(self) -> float:
        return self.tick_ts[0]

    @property
    def last_ts(self) -> float:
        return self.tick_ts[-1]

    def tick_index(self, ts: float) -> int:
        """Index of the tick a record stamped ``ts`` belongs to (lines print the tick's ms, truncated):
        the last tick at or before ``ts`` + 1 ms; -1 before the first tick."""
        return bisect.bisect_right(self.tick_ts, ts + 0.001) - 1

    def first_after(self, ts: float) -> int:
        """Index of the first tick that started after ``ts`` (len(ticks) when none)."""
        return bisect.bisect_right(self.tick_ts, ts)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    alt = ROOT / p
    return alt if alt.exists() else p


def _ts_of(rec: Mapping[str, Any]) -> float | None:
    value = rec.get("ts")
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def load_recorded_run(
    log_dir: str | Path, state_db: str | Path | None = None, segment: int | None = None
) -> RecordedRun:
    """Read one recorded process segment (from a ``start`` event to the next), up to its last
    committed tick. ``segment`` (1-based) picks it; default: the latest segment that started fresh
    (a reused log directory holds older runs; a restart's segment resumed state the replay cannot
    rebuild), else the first."""
    log_path = Path(log_dir)
    events = list(iter_family(log_path, "events"))
    starts = [e for e in events if e.get("kind") == "start" and parse_iso(str(e.get("ts") or "")) is not None]
    if not starts:
        raise ReplayInputError(f"no start event in {log_path}/events*.jsonl: not an engine log directory")
    if segment is None:
        fresh = [i for i, e in enumerate(starts) if (e.get("data") or {}).get("reason") == "fresh"]
        k = fresh[-1] if fresh else 0
    else:
        if not 1 <= segment <= len(starts):
            raise ReplayInputError(f"segment {segment} does not exist ({len(starts)} start events)")
        k = segment - 1
    start = starts[k]
    data = dict(start.get("data") or {})
    start_ts = float(parse_iso(str(start.get("ts"))) or 0.0)
    segment_end = parse_iso(str(starts[k + 1].get("ts"))) if k + 1 < len(starts) else None
    mode = str(data.get("mode") or "")
    if not mode:
        raise ReplayInputError("the start event names no mode")
    db = _resolve(state_db) if state_db is not None else _resolve(str(data.get("state_db") or ""))
    if not str(db) or not db.is_file():
        raise ReplayInputError(f"recorded state DB not found: {db} (pass state_db)")
    store = Store.open_readonly(db)
    try:
        ticks = [
            row for row in store.iter_ticks(mode, from_ts=start_ts)
            if segment_end is None or float(row["ts"]) < segment_end
        ]
    finally:
        store.close()
    if not ticks:
        raise ReplayInputError(f"no '{mode}' ticks after {iso(start_ts)} in {db}: nothing to replay")
    tick_ts = [float(r["ts"]) for r in ticks]
    cutoff = tick_ts[-1]
    stops = [e for e in events if e.get("kind") == "stop" and start_ts <= (_ts_of(e) or 0.0)
             and (segment_end is None or (_ts_of(e) or 0.0) < segment_end)]
    in_progress = segment_end is None and not stops

    def split(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
        """(records of this segment up to the last tick, lines of other runs/segments, lines after it)"""
        kept: list[dict[str, Any]] = []
        other = after = 0
        for rec in records:
            t = _ts_of(rec)
            if t is None or t < start_ts - 0.001 or (segment_end is not None and t >= segment_end):
                other += 1
            elif t > cutoff + 0.001:
                after += 1
            else:
                kept.append(rec)
        return kept, other, after

    decisions, d_other, d_after = split(iter_family(log_path, "decisions"))
    orders, o_other, o_after = split(iter_family(log_path, "orders"))
    excluded = {"decision lines of other segments": d_other, "decision lines after the last tick": d_after,
                "order lines of other segments": o_other, "order lines after the last tick": o_after}
    balances = [(ts, snap) for ts, snap in load_balance_snapshots(db) if start_ts - 0.001 <= ts <= cutoff + 0.001]
    fills = [f for f in load_recorded_fills(db) if start_ts - 0.001 <= f.ts <= cutoff + 0.001]
    seg_events = [e for e in events if start_ts - 0.001 <= (_ts_of(e) or 0.0) <= cutoff + 0.001
                  and (segment_end is None or (_ts_of(e) or 0.0) < segment_end)]
    return RecordedRun(
        log_dir=log_path,
        state_db=db,
        start=start,
        start_ts=start_ts,
        mode=mode,
        feed=str(data.get("feed") or "live"),
        config_hash=str(data.get("config_hash") or ""),
        segment=k + 1,
        segments=len(starts),
        segment_end=segment_end,
        ticks=ticks,
        tick_ts=tick_ts,
        decisions=decisions,
        orders=orders,
        events=seg_events,
        excluded=excluded,
        in_progress=in_progress,
        balances=balances,
        fills=fills,
    )


# ---------------------------------------------------------------- the report


@dataclass
class ReplayRun(ReplayReport):
    """``compare_decisions``' report plus how the replay ran (inputs, AI, exchange, network)."""

    errors: list[str] = field(default_factory=list)  # fatal: the replay is not a reproduction
    notes: list[str] = field(default_factory=list)  # approximations and limits that applied
    run: dict[str, Any] = field(default_factory=dict)
    strict_check: dict[str, Any] = field(default_factory=dict)
    plans: list[dict[str, Any]] = field(default_factory=list)
    config_changes: list[dict[str, Any]] = field(default_factory=list)
    report_file: str | None = None

    @property
    def network_calls(self) -> int:
        net = self.run.get("network") or {}
        return int(net.get("socket_attempts", 0)) + int(net.get("provider_calls", 0))

    @property
    def ok(self) -> bool:
        return super().ok and not self.errors and self.network_calls == 0

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.update(
            ok=self.ok,
            errors=list(self.errors),
            notes=list(self.notes),
            run=self.run,
            strict_check=self.strict_check,
            plans=self.plans,
            config_changes=self.config_changes,
            report_file=self.report_file,
        )
        return out

    def to_markdown(self, date: str | None = None, title: str | None = None) -> str:
        r = self.run
        rec = r.get("recorded") or {}
        head = title or f"Replay report {date or ''}".strip()
        if rec.get("synthetic"):
            head += " — SYNTHETIC"
        base = super().to_markdown(date=date, title=head).split("\n")
        intro: list[str] = [""]
        if rec.get("synthetic"):
            intro += [
                f"> **{SYNTHETIC_BANNER}.** The recorded run traded on the offline synthetic feed "
                f"({SYNTHETIC_BANNER_LONG}). This report proves reproducibility of the code, nothing about "
                "strategy performance.",
                "",
            ]
        window = r.get("window") or {}
        kinds = rec.get("kinds") or {}
        intro += [
            "Recorded decisions by kind: " + (", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "none")
            + " (outcome updates are joined to their decisions).",
            "",
            f"Recorded run `{rec.get('log_dir')}` (mode `{rec.get('mode')}`, feed `{rec.get('feed')}`), "
            f"replayed over {window.get('ticks', 0)} ticks from {window.get('first', '?')} to "
            f"{window.get('last', '?')} ({window.get('hours', 0):.2f} h"
            + (", run still being written: replayed up to its last committed tick" if rec.get("in_progress") else "")
            + ").",
            "",
        ]
        if self.errors:
            intro += ["**Replay errors:**", ""] + [f"- {e}" for e in self.errors] + [""]
        net = r.get("network") or {}
        ai = r.get("ai") or {}
        intro += [
            f"Network: {net.get('socket_attempts', 0)} socket attempts, {net.get('provider_calls', 0)} LLM "
            f"provider calls (both must be 0). LLM cache: {ai.get('cache_hits', 0)} hits, "
            f"{ai.get('cache_misses', 0)} misses. Strict pass (every field but time stamps): "
            f"{self.strict_check.get('summary', 'n/a')}.",
            "",
            f"Reproduce: `python scripts/replay_check.py --log-dir {rec.get('log_dir')}` (or `make replay`; "
            "`python -m wta replay`).",
        ]
        lines = base[:3] + intro + base[3:]
        lines += self._inputs_md() + self._plans_md() + self._strict_md() + self._limits_md()
        return "\n".join(lines).rstrip() + "\n"

    def _inputs_md(self) -> list[str]:
        r = self.run
        rec, window, cfg, ex = r.get("recorded") or {}, r.get("window") or {}, r.get("config") or {}, r.get("exchange") or {}
        inputs = r.get("inputs") or {}
        rows = [
            ("recorded log dir", f"`{rec.get('log_dir')}`"),
            ("recorded state DB", f"`{rec.get('state_db')}`"),
            ("recorded start", f"{rec.get('start')} (reason `{rec.get('start_reason')}`; segment "
                               f"{rec.get('segment')} of {rec.get('segments')} replayed)"),
            ("git commit (recorded)", f"`{rec.get('git_commit')}`" + (" (dirty)" if rec.get("git_dirty") else "")),
            ("data window", f"{window.get('first')} → {window.get('last')} = {window.get('hours', 0):.2f} h, "
                            f"{window.get('ticks')} ticks"),
            ("books put on the bus", str(inputs.get("books_published"))),
            ("candle arrivals fed", str(inputs.get("candles_fed"))),
            ("plans re-derived", f"{(r.get('ai') or {}).get('plans_replayed', 0)} of {len(self.plans)}"),
            ("reviewer config changes", str(len(self.config_changes))),
            ("injected test intents", str(inputs.get("injected", 0))),
            ("config", f"`{cfg.get('replay_hash')}` from {cfg.get('source')} (recorded `{cfg.get('recorded_hash')}`"
                       f", {'equal' if cfg.get('equal') else 'DIFFERENT'})"),
            ("markets", f"recorded: {rec.get('markets_source') or '?'}; replay: {r.get('markets_source')}"),
            ("exchange answers", ", ".join(f"{k} {v}" for k, v in sorted((ex.get("answers") or {}).items())) or "-"),
            ("recorded records left out", ", ".join(f"{k} {v}" for k, v in sorted((rec.get("excluded") or {}).items()))),
            ("replay output", _where(r.get("out_dir"))),
        ]
        out = ["", "## Inputs and replay run", "", "| | |", "|---|---|"]
        out += [f"| {k} | {_md(v)} |" for k, v in rows]
        return out

    def _plans_md(self) -> list[str]:
        out = ["", "## Plans (slow loop) and config changes", ""]
        if not self.plans:
            out.append("No analyst plan was recorded.")
        else:
            out += [
                "| decision_id | prompt_hash | cycle | swapped in before tick | evidence | reproduced |",
                "|---|---|---|---|---|---|",
            ]
            for p in self.plans:
                out.append(
                    f"| `{p['decision_id']}` | `{p['prompt_hash'][:12]}` | {p['plan_utc']} | {p['activation_utc']} "
                    f"({p['swap_source']}) | {_bounds(p)}"
                    + f" | {_yes(p.get('reproduced'))}{(' — ' + _md(p['error'])) if p.get('error') else ''} |"
                )
        if self.config_changes:
            out += ["", "| event | old → new hash | applied before tick | applied hash | |", "|---|---|---|---|---|"]
            for c in self.config_changes:
                out.append(
                    f"| {c['event_utc']} | `{c['old_hash']}` → `{c['new_hash']}` | {c['activation_utc']} | "
                    f"`{c['applied_hash']}` | {_md(c.get('error') or 'ok')} |"
                )
        return out

    def _strict_md(self) -> list[str]:
        s = self.strict_check
        out = ["", "## Strict pass", "", f"{s.get('summary', 'n/a')} — every field of the merged records except "
               "time stamps and durations; known differences by construction are not counted: "
               + ", ".join(f"`{k}.{f}`" for k, f in sorted(BENIGN_STRICT)) + "."]
        rows = s.get("mismatches") or []
        if rows:
            out += ["", "| decision_id | field | reference | replayed |", "|---|---|---|---|"]
            for m in rows[:MAX_LISTED]:
                out.append(f"| `{m['decision_id']}` | `{m['field']}` | {_md(_show(m['reference']))} | "
                           f"{_md(_show(m['replayed']))} |")
            if len(rows) > MAX_LISTED:
                out.append(f"| … | {len(rows) - MAX_LISTED} more | | |")
        return out

    def _limits_md(self) -> list[str]:
        out = ["", "## Limitations", ""]
        out += [f"- {n}" for n in self.notes] or ["- None beyond the ReplayExchange approximations."]
        return out + [""]


def _bounds(p: Mapping[str, Any]) -> str:
    """The recorded decisions' bounds on a plan's swap tick, in words."""
    lo, hi, n = p.get("lower", -1), p.get("upper"), p.get("ticks")
    parts = []
    if isinstance(lo, int) and lo >= 0:
        parts.append(f"after tick #{lo}")
    if isinstance(hi, int) and hi != n:
        parts.append(f"by tick #{hi}")
    text = ", ".join(parts) or "no recorded decision bounds it"
    return text + (" (CONFLICT)" if p.get("conflict") else "")


def _where(path: Any) -> str:
    """A path for the report: relative to the repo when inside it, else just 'outside the repo'."""
    try:
        return f"`{Path(str(path)).resolve().relative_to(ROOT.resolve())}`"
    except ValueError:
        return f"`{Path(str(path)).name}/` (temporary directory outside the repo)"


def _yes(value: Any) -> str:
    return "yes" if value is True else ("NO" if value is False else "not replayed")


def _show(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return canonical_json(value).decode("utf-8")


def _md(text: Any) -> str:
    s = str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= 300 else s[:299] + "…"


# ---------------------------------------------------------------- inputs from the state DB


class _BookFeed:
    """``engine_books`` read in time windows; ``get(symbol, ts)`` returns the book a tick used."""

    def __init__(self, db: Path, mode: str, start: float, end: float) -> None:
        self.db, self.mode, self.end = db, mode, end
        self.loaded_until = start - 1e-6
        self.pending: dict[str, dict[float, BookSnapshot]] = {}
        self.missing: list[tuple[str, float]] = []
        self.loaded = 0

    def _load(self, lo: float, hi: float) -> None:
        store = Store.open_readonly(self.db)
        try:
            books = list(store.iter_engine_books(self.mode, from_ts=lo, to_ts=hi))
        finally:
            store.close()
        for b in books:
            self.pending.setdefault(b.symbol, {})[b.ts] = b
        self.loaded += len(books)

    def get(self, symbol: str, ts: float) -> BookSnapshot | None:
        while ts > self.loaded_until and self.loaded_until < self.end + 1:
            hi = self.loaded_until + BOOK_CHUNK_S
            self._load(self.loaded_until, hi)
            self.loaded_until = hi
        book = self.pending.get(symbol, {}).get(ts)
        if book is None:  # out of order (should not happen): one direct lookup
            self._load(ts, ts)
            book = self.pending.get(symbol, {}).get(ts)
        if book is None:
            self.missing.append((symbol, ts))
            return None
        older = [t for t in self.pending[symbol] if t < ts]
        for t in older:
            del self.pending[symbol][t]
        return book


class _CandleFeed:
    """``engine_candles`` arrivals in seq order, read in seq windows."""

    def __init__(self, db: Path, mode: str) -> None:
        self.db, self.mode = db, mode
        self.applied = 0
        self.buffer: list[tuple[int, Candle]] = []
        self.buffer_until = 0

    def _load(self, lo: int, hi: int) -> None:
        uri = Path(self.db).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "SELECT seq, symbol, resolution, ts, o, h, l, c, v FROM engine_candles"
                " WHERE mode = ? AND seq > ? AND seq <= ? ORDER BY seq, rowid",
                (self.mode, lo, hi),
            ).fetchall()
        finally:
            conn.close()
        self.buffer += [
            (int(seq), Candle(symbol=sym, resolution=res, ts=ts, o=Decimal(o), h=Decimal(h), l=Decimal(lo_),
                              c=Decimal(c), v=Decimal(v)))
            for seq, sym, res, ts, o, h, lo_, c, v in rows
        ]
        self.buffer_until = hi

    def take(self, to_seq: int) -> list[Candle]:
        """The arrivals after the last call, up to ``to_seq`` (inclusive)."""
        while self.buffer_until < to_seq:
            self._load(self.buffer_until, max(to_seq, self.buffer_until + CANDLE_CHUNK))
        out = [c for s, c in self.buffer if self.applied < s <= to_seq]
        self.buffer = [(s, c) for s, c in self.buffer if s > to_seq]
        self.applied = max(self.applied, to_seq)
        return out


# ---------------------------------------------------------------- schedule: plans, config, kills


def _evidence(run: RecordedRun, key: Callable[[Mapping[str, Any]], str | None]) -> dict[str, list[int]]:
    """``value -> [tick indices]`` of the recorded non-plan decisions where ``key`` gives a value."""
    out: dict[str, list[int]] = {}
    for rec in run.decisions:
        if rec.get("kind") in ("plan", "outcome"):
            continue
        value = key(rec)
        t = _ts_of(rec)
        if value is None or t is None:
            continue
        idx = run.tick_index(t)
        if idx >= 0:
            out.setdefault(value, []).append(idx)
    return out


def _plan_hash(rec: Mapping[str, Any]) -> str | None:
    snap = rec.get("plan_snapshot")
    if not isinstance(snap, Mapping):
        return None
    value = snap.get("prompt_hash")
    return str(value) if value else None


def _cache_entry(cache_dir: Path, key: str) -> dict[str, Any] | None:
    path = cache_dir / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def schedule_plans(run: RecordedRun, cache_dir: Path) -> list[PlanSlot]:
    """When each recorded plan was swapped in (module docstring, step 5)."""
    n = len(run.ticks)
    evidence = _evidence(run, _plan_hash)
    slots: list[PlanSlot] = []
    for rec in run.decisions:
        if rec.get("kind") != "plan":
            continue
        snap = rec.get("plan_snapshot") or {}
        feats = rec.get("features") or {}
        ph = str(snap.get("prompt_hash") or feats.get("prompt_hash") or "")
        plan_ts = float(snap.get("ts") or _ts_of(rec) or 0.0)
        swap, source = plan_ts + float(snap.get("latency_ms") or feats.get("latency_ms") or 0) / 1000.0, "latency"
        if not feats.get("cached"):
            stamps = []
            for call in feats.get("calls") or []:
                entry = _cache_entry(cache_dir, str((call or {}).get("cache_key") or "")) if isinstance(call, dict) else None
                t = entry.get("ts") if entry else None
                if isinstance(t, int | float) and t >= plan_ts - 1e-6:
                    stamps.append(float(t))
            if stamps:
                swap, source = max(stamps), "cache"
        slots.append(PlanSlot(decision_id=str(rec.get("decision_id")), prompt_hash=ph, plan_ts=plan_ts,
                              swap_ts=swap, swap_source=source, lower=-1, upper=n, record=rec))
    hashes = [s.prompt_hash for s in slots]
    prev_index = 0
    for i, s in enumerate(slots):
        earlier = set(hashes[:i])
        later = set(hashes[i:])
        s.lower = max((x for h in earlier for x in evidence.get(h, [])), default=-1)
        s.upper = min((x for h in later for x in evidence.get(h, [])), default=n)
        est = run.first_after(s.swap_ts)
        s.conflict = s.lower + 1 > s.upper
        s.index = max(prev_index, min(max(est, s.lower + 1), s.upper))
        prev_index = s.index
    return slots


def _reviewer_done(cache_dir: Path, start: float) -> float | None:
    """The latest time a reviewer answer was cached within ``REVIEW_SPAN_S`` after ``start``."""
    best: float | None = None
    if not cache_dir.is_dir():
        return None
    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(t, int | float) or not start <= t <= start + REVIEW_SPAN_S:
            continue
        msgs = ((data.get("request") or {}).get("messages") or [])
        if msgs and REVIEWER_MARKER in str((msgs[0] or {}).get("content", "")):
            best = float(t) if best is None else max(best, float(t))
    return best


def schedule_config_changes(run: RecordedRun, cache_dir: Path) -> list[ConfigSlot]:
    """When each recorded reviewer change took effect (module docstring, step 5)."""
    changes = [e for e in run.events if e.get("kind") == "config_change"
               and (e.get("data") or {}).get("diff") and (e.get("data") or {}).get("new_hash")]
    if not changes:
        return []
    n = len(run.ticks)
    evidence = _evidence(run, lambda r: str(r.get("config_hash")) if r.get("config_hash") else None)
    out: list[ConfigSlot] = []
    prev = 0
    for ev in changes:
        data = ev.get("data") or {}
        ts = _ts_of(ev) or 0.0
        done = _reviewer_done(cache_dir, ts) or ts
        old, new = str(data.get("old_hash") or ""), str(data.get("new_hash"))
        slot = ConfigSlot(ts=ts, old_hash=old, new_hash=new, diff=list(data.get("diff") or []), done_ts=done,
                          lower=max(evidence.get(old, [-1])), upper=min(evidence.get(new, [n])))
        slot.conflict = slot.lower + 1 > slot.upper
        slot.index = max(prev, min(max(run.first_after(done), slot.lower + 1), slot.upper))
        prev = slot.index
        out.append(slot)
    return out


def _change_value(text: Any) -> Any:
    """A ``config_change`` diff's ``new`` (``str`` of what the ConfigManager applied) back to the
    value it applied: numbers as floats (``float(Decimal)``), booleans and strings as YAML reads them."""
    if isinstance(text, bool | int | float) or not isinstance(text, str):
        return text
    try:
        value = Decimal(text)
        if value.is_finite():
            return float(value)
    except InvalidOperation:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def injected_from_decisions(run: RecordedRun) -> list[InjectedIntent]:
    """The ``--inject-intents`` lines the recorded run fired, rebuilt from their decision records
    (``features.injected``), due at the same tick (``at_s`` from the replay's start; intents of
    one tick keep their recorded order)."""
    seen: set[str] = set()
    per_tick: dict[int, list[dict[str, Any]]] = {}
    for rec in run.decisions:
        intent = rec.get("intent") or {}
        feats = intent.get("features") or {}
        did = str(rec.get("decision_id") or "")
        if rec.get("kind") == "outcome" or not feats.get("injected") or did in seen:
            continue
        seen.add(did)
        t = float(intent.get("ts") or _ts_of(rec) or 0.0)
        per_tick.setdefault(run.tick_index(t), []).append(intent)
    out: list[InjectedIntent] = []
    for intents in per_tick.values():
        for k, intent in enumerate(intents):
            feats = dict(intent.get("features") or {})
            line = int(feats.pop("inject_line", 0) or 0)
            feats.pop("injected", None)
            reason = str(intent.get("reason") or "")
            out.append(InjectedIntent(
                at_s=float(intent["ts"]) - run.start_ts - (len(intents) - k) * 1e-6,
                line=line,
                strategy=str(intent["strategy"]),
                symbol=str(intent["symbol"]),
                side=intent["side"],
                order_type=intent["order_type"],
                purpose=intent["purpose"],
                reason=reason[len(INJECT_PREFIX):] if reason.startswith(INJECT_PREFIX) else reason,
                qty=_dec(intent.get("qty")),
                notional=_dec(intent.get("notional")),
                price=_dec(intent.get("price")),
                stop_price=_dec(intent.get("stop_price")),
                taker=bool(intent.get("taker")),
                expected_edge_pct=_dec(intent.get("expected_edge_pct")),
                features=feats,
            ))
    return out


def _dec(value: Any) -> Decimal | None:
    return None if value is None or value == "" else D(value)


# ---------------------------------------------------------------- config


def resolve_config(cfg: Config, run: RecordedRun, cache_dir: Path) -> tuple[Config, str]:
    """The recorded run's config (module docstring, step 1): ``(config, source)``."""
    want = run.config_hash
    overlay = run.data.get("runtime_overlay") or {}
    applied = overlay.get("applied") if isinstance(overlay, dict) and not overlay.get("skipped") else None
    candidates: list[tuple[str, Callable[[], Config]]] = [("the given config", lambda: cfg)]
    if applied:
        candidates.append(("the given config + the recorded runtime overlay", lambda: apply_changes(cfg, dict(applied))))
    state_db = str(run.data.get("state_db") or "")
    label = run.mode if run.feed == "live" else f"{run.mode}_{run.feed}"
    if label and label in state_db:
        pattern = state_db.replace(label, "{mode}")
        for name, build in list(candidates):
            candidates.append((f"{name} + data.state_sqlite_path={pattern}",
                               lambda b=build: apply_changes(b(), {"data.state_sqlite_path": pattern})))
    for name, build in candidates:
        try:
            got = build()
        except ValueError:
            continue
        if not want or got.config_hash == want:
            return got, name
    tape = LLMCache(cache_dir / "mcp_tape", strict=False)
    for rec in run.decisions:
        if rec.get("kind") != "plan":
            continue
        entry = tape.get(cycle_tape_key(str((rec.get("plan_snapshot") or {}).get("prompt_hash") or ""))) or {}
        if entry.get("config_hash") == want:
            taped, source = cycle_config(cfg, entry)
            if source == "tape" and taped.config_hash == want:
                return taped, "the config taped with the first analyst cycle"
    return cfg, "the given config (recorded hash NOT matched)"


# ---------------------------------------------------------------- the runner


def _tools(cfg: Config, clock: SimClock) -> InternalTools:
    """Tools over nothing: every tool result of a replayed cycle comes from the tape; a call that
    reaches these answers differently, so the next cache key misses (reported, never hidden)."""
    return InternalTools(ToolDeps(
        cfg=cfg, now=clock.now, features=lambda: None, books=dict, candles_1m=lambda s, a, b: [],
        trades=lambda s, a, b: [], pnl_summary=lambda: {"error": "replay: no engine state"},
        recent_decisions=lambda n: [], health=lambda: {"error": "replay: no engine state"},
    ))


class _Runner:
    def __init__(self, cfg: Config, run: RecordedRun, out_dir: Path, *, strict: bool, settings: Settings,
                 markets: Mapping[str, MarketInfo] | None, cache_dir: Path) -> None:
        self.given_cfg = cfg
        self.run = run
        self.out_dir = out_dir
        self.strict = strict
        self.settings = settings
        self.cache_dir = cache_dir
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.markets_source = "given"
        if markets is None:
            markets = load_markets_snapshot()
            self.markets_source = "configs/markets_snapshot.yaml (offline snapshot)"
            if run.data.get("markets_source") == "api":
                self.notes.append("The recorded run loaded /v1/markets; the replay uses configs/markets_snapshot.yaml "
                                  "(no network): fee or step differences would show as mismatches.")
        self.markets = dict(sorted(markets.items()))
        self.sentinels: dict[str, _NoProvider] = {}
        self.cache = LLMCache(cache_dir, strict=True)
        self.tape = LLMCache(cache_dir / "mcp_tape", strict=False)
        self.analyst_clock = SimClock(0.0)
        self.fatal = False
        self.counts: dict[str, int] = {"books_published": 0, "candles_fed": 0, "candle_drift": 0, "ticks": 0,
                                       "tick_crashes": 0, "injected": 0}

    # ---- setup

    def build(self) -> Engine:
        run = self.run
        self.cfg, self.cfg_source = resolve_config(self.given_cfg, run, self.cache_dir)
        if run.config_hash and self.cfg.config_hash != run.config_hash:
            msg = (f"config mismatch: recorded {run.config_hash}, replay {self.cfg.config_hash} "
                   "(pass the recorded run's --set / --overlay options)")
            (self.errors if self.strict else self.notes).append(msg)
        if run.data.get("reason") == "restart":
            self.notes.append("The recorded run resumed earlier state (start reason 'restart'); the replay starts "
                              "from an empty state, so restored positions/orders/peak are not reproduced.")
        if run.segments > 1:
            self.notes.append(f"The log directory holds {run.segments} process segments (start events); segment "
                              f"{run.segment} is replayed, "
                              f"{run.excluded['decision lines of other segments']} decision lines of the others are "
                              "left out (a restart resumes state the replay cannot rebuild).")
        if run.in_progress:
            self.notes.append(f"The recorded run was still being written (no stop event): replayed up to its last "
                              f"committed tick {iso(run.last_ts)}; "
                              f"{run.excluded['decision lines after the last tick']} later decision lines are left out.")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.kill_path = self.out_dir / "KILL"
        self.base = ReplayClock(run.start_ts)
        self.clock = TickClock(self.base)
        self.bus = MarketDataBus()
        cfg = self.cfg
        self.store = Store(self.out_dir / "replay_state.sqlite", flush_interval_s=cfg.data.store_flush_interval_s,
                           flush_rows=cfg.data.store_flush_rows, depth_levels=cfg.data.depth_levels_stored)
        self.exchange = RunnerExchange(run.orders, clock=self.clock, markets=self.markets, balances=run.balances,
                                       my_trades=run.fills)
        injected = injected_from_decisions(run)
        self.counts["injected"] = len(injected)
        recorded_n = run.data.get("injected_intents")
        if isinstance(recorded_n, int) and recorded_n != len(injected):
            self.notes.append(f"The recorded run was given {recorded_n} injected intents; {len(injected)} fired "
                              "within the replayed window and are replayed.")
        strategies = default_strategies(cfg)
        recorded_strats = run.data.get("strategies")
        if isinstance(recorded_strats, list) and recorded_strats != [s.id for s in strategies]:
            self.errors.append(f"strategies differ: recorded {recorded_strats}, replay {[s.id for s in strategies]}")
        engine = Engine(cfg, self.settings, REPLAY_MODE, self.clock, self.bus, self.store, exchange=self.exchange,
                        log_dir=self.out_dir / "logs", markets=self.markets, markets_source="replay snapshot",
                        feed=run.feed, injected=injected, kill_file=self.kill_path, strategies=strategies,
                        overlays=list(run.data.get("overlays") or []))
        self.exchange.set_balances_fallback(lambda: engine.portfolio.balances)
        ai = run.data.get("ai") or {}
        engine.ai_source = str(ai.get("source") or engine.ai_source)
        engine.runtime_overlay = run.data.get("runtime_overlay")
        self.engine = engine
        self.plans = schedule_plans(run, self.cache_dir)
        self.config_slots = schedule_config_changes(run, self.cache_dir)
        self.kills = sorted({run.tick_index(_ts_of(e) or 0.0) for e in run.events
                             if e.get("kind") == "kill" and (e.get("data") or {}).get("source") == "kill_file"})
        for kind, what in (("reset_kill_applied", "a manual kill reset (wta reset-kill)"),
                           ("telegram", "Telegram commands")):
            hits = [e for e in run.events if e.get("kind") == kind
                    or (kind == "telegram" and (e.get("data") or {}).get("source") == "telegram")]
            if hits:
                self.notes.append(f"The recorded run applied {what} ({len(hits)}x); these are not replayed.")
        if run.mode == "live":
            self.notes.append("REST error counters of the live run are not recorded: the replay sees 0 consecutive "
                              "API errors.")
        self.books = _BookFeed(run.state_db, run.mode, min(run.ticks[0]["book_ts"].values(), default=run.start_ts),
                               run.last_ts)
        self.candles = _CandleFeed(run.state_db, run.mode)
        return engine

    def _publish(self, row: Mapping[str, Any]) -> None:
        for symbol, ts in sorted((row.get("book_ts") or {}).items()):
            cur = self.bus.latest(symbol)
            if cur is not None and cur.ts == ts:
                continue
            book = self.books.get(symbol, float(ts))
            if book is None:
                continue
            if not self.bus.publish_book(book):
                self.bus.books[symbol] = book  # an older book than the bus holds: the tick used it
            self.counts["books_published"] += 1

    def _feed_candles(self, row: Mapping[str, Any]) -> None:
        seq = row.get("candle_seq")
        if not isinstance(seq, int) or seq <= self.candles.applied:
            return
        batch = self.candles.take(seq)
        if not batch:
            return
        changed = self.engine.ingest_candles(batch)
        self.counts["candles_fed"] += len(batch)
        if changed != len(batch):
            self.counts["candle_drift"] += len(batch) - changed

    async def startup(self) -> None:
        run, engine = self.run, self.engine
        first = run.ticks[0]
        before = [ts for ts, _ in run.balances if ts < run.first_ts - 1e-6]
        if before:
            reconcile_ts = before[0]
            self._publish(first)  # the first tick's books: the closest record of the startup's bus
            late = sorted(s for s, t in (first.get("book_ts") or {}).items() if t > reconcile_ts)
            if late:
                self.notes.append("Startup reconcile: the bus at startup is not recorded; the first tick's books were "
                                  f"used, {len(late)} of them newer than the reconcile ({', '.join(late[:5])}"
                                  f"{', ...' if len(late) > 5 else ''}).")
            self.base.jump_when(lambda: engine.started_ts is not None, reconcile_ts)
        else:
            reconcile_ts = None  # the recorded startup reconcile waited for books: an empty bus defers it again
        if run.balances and tick_ms_of(run.balances[0][0]) != tick_ms_of(run.start_ts):
            self.exchange.seed = dict(run.balances[0][1])  # what the seed read saw (see RunnerExchange)
        await engine.startup()
        self.exchange.seed = None
        if reconcile_ts is not None and getattr(engine, "_last_reconcile_ts", reconcile_ts) != reconcile_ts:
            self.notes.append(f"The startup reconcile ran at {iso(self.base.now())}, not at the recorded "
                              f"{iso(reconcile_ts)}.")

    # ---- AI

    async def replay_plan(self, slot: PlanSlot) -> None:
        if not slot.prompt_hash:
            slot.reproduced, slot.error = False, "the plan record has no prompt_hash"
            self._plan_failed(slot)
            return
        rec = slot.record
        routes = cycle_routes(self.cache_dir, rec, slot.prompt_hash, start_routes(self.run.log_dir))[:2]
        try:
            if not routes:
                raise CacheMissError(f"replay: the first call {slot.prompt_hash} of {slot.decision_id} is not in the "
                                     f"LLM cache {self.cache_dir}")
            providers = {r.provider: self.sentinels.setdefault(r.provider, _NoProvider(r.provider)) for r in routes}
            entry = self.tape.get(cycle_tape_key(slot.prompt_hash)) or {}
            cycle_cfg, _ = cycle_config(self.engine.cfg, entry)
            llm = LLMClient(providers, routes[0], routes[1] if len(routes) > 1 else None, self.cache,
                            mode=REPLAY_MODE, clock=self.analyst_clock)
            analyst = RegimeAnalyst(cycle_cfg, llm, _tools(cycle_cfg, self.analyst_clock), None, self.analyst_clock,
                                    on_plan=self.engine._on_plan, tape=self.tape)  # noqa: SLF001 - the engine's own hook
            plan = await analyst.replay_cycle(slot.prompt_hash)
        except CacheMissError as exc:
            slot.reproduced, slot.error = False, f"LLM cache miss: {exc}"
            self._plan_failed(slot)
            return
        if plan is None:
            slot.reproduced, slot.error = False, analyst.last_error or "no plan"
            self._plan_failed(slot)
            return
        slot.reproduced, slot.error = self._same_plan(rec.get("plan_snapshot") or {}, plan)

    @staticmethod
    def _same_plan(recorded: Mapping[str, Any], plan: MarketPlan) -> tuple[bool, str]:
        replayed = plan.model_dump(mode="json")
        keys = sorted((set(recorded) | set(replayed)) - {"cached", "latency_ms"})
        diff = [k for k in keys if canonical_json(recorded.get(k)) != canonical_json(replayed.get(k))]
        return (not diff, f"differs in {', '.join(diff)}" if diff else "")

    def _plan_failed(self, slot: PlanSlot) -> None:
        msg = f"plan {slot.decision_id} not reproduced: {slot.error}"
        if self.strict:
            self.errors.append(msg + " (strict replay stopped here)")
            self.fatal = True
        else:
            self.notes.append(msg + " (replay continued without it)")

    def apply_config(self, slot: ConfigSlot) -> None:
        engine = self.engine
        try:
            changes = {str(d.get("path")): _change_value(d.get("new")) for d in slot.diff}
            new_cfg = apply_changes(engine.cfg, changes)
            if new_cfg.config_hash != slot.new_hash:  # an int field written as a float, or similar
                alt = {k: (int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in changes.items()}
                new_cfg = apply_changes(engine.cfg, alt)
            engine.reload_config(new_cfg)
            slot.applied_hash = new_cfg.config_hash
            if new_cfg.config_hash != slot.new_hash:
                slot.error = f"applied hash {new_cfg.config_hash} != recorded {slot.new_hash}"
        except ValueError as exc:
            slot.error = f"not applied: {exc}"
        if slot.error:
            (self.errors if self.strict else self.notes).append(f"reviewer change at {iso(slot.ts)}: {slot.error}")

    # ---- the loop

    async def drive(self) -> None:
        engine, run = self.engine, self.run
        n = len(run.ticks)
        plans = sorted(self.plans, key=lambda s: s.index)
        configs = sorted(self.config_slots, key=lambda s: s.index)
        pi = ci = 0
        recorded_crashes = sum(1 for e in run.events if e.get("kind") == "tick_error")
        for idx, row in enumerate(run.ticks):
            ts = float(row["ts"])
            self.base.set(ts)
            self._publish(row)
            self._feed_candles(row)
            while ci < len(configs) and configs[ci].index <= idx:
                self.apply_config(configs[ci])
                ci += 1
            while pi < len(plans) and plans[pi].index <= idx and not self.fatal:
                await self.replay_plan(plans[pi])
                pi += 1
            if self.fatal:
                break
            kill = idx in self.kills
            if kill:
                self.kill_path.write_text("replay: recorded kill file\n")
            try:
                if not await engine.safe_tick(ts):
                    self.counts["tick_crashes"] += 1
            finally:
                if kill:
                    self.kill_path.unlink(missing_ok=True)
            self.counts["ticks"] += 1
        while pi < len(plans) and not self.fatal:  # plans swapped in after the last tick
            await self.replay_plan(plans[pi])
            pi += 1
        while ci < len(configs):
            self.apply_config(configs[ci])
            ci += 1
        if self.counts["ticks"] < n and not self.fatal:
            self.errors.append(f"only {self.counts['ticks']} of {n} ticks replayed")
        if self.counts["tick_crashes"] != recorded_crashes:
            self.notes.append(f"{self.counts['tick_crashes']} replayed ticks crashed; the recorded run logged "
                              f"{recorded_crashes} tick_error event(s).")
        if self.books.missing:
            self.errors.append(f"{len(self.books.missing)} books named by the ticks rows are missing from "
                               f"engine_books (first: {self.books.missing[0]})")
        if self.counts["candle_drift"]:
            self.notes.append(f"{self.counts['candle_drift']} recorded candle arrivals did not change the replay's "
                              "candle cache (its engine_candles numbering drifts from the recorded one).")

    def close(self, reason: str) -> None:
        engine, store = getattr(self, "engine", None), getattr(self, "store", None)
        try:
            if engine is not None:
                engine.shutdown(reason)
        finally:
            if store is not None:
                store.close()
            if hasattr(self, "kill_path"):
                self.kill_path.unlink(missing_ok=True)


def _strict_check(reference: list[dict[str, Any]], replayed: Path) -> dict[str, Any]:
    rep = compare_decisions(reference, replayed, strict=True)
    kinds = {}
    for rec in reference:
        kinds.setdefault(str(rec.get("decision_id")), str(rec.get("kind")))
    rows = [m for m in rep.mismatches if (kinds.get(m.decision_id, ""), m.field) not in BENIGN_STRICT]
    bad = {m.decision_id for m in rows}
    matched = rep.n_reference - len(rep.missing) - len(bad)
    ok = not rows and not rep.missing and not rep.extra and rep.n_reference == rep.n_replayed
    return {
        "ok": ok,
        "summary": f"{matched}/{rep.n_reference} decisions identical",
        "n_matched": matched,
        "mismatches": [m.to_dict() for m in rows],
        "benign": len(rep.mismatches) - len(rows),
        "missing": len(rep.missing),
        "extra": len(rep.extra),
    }


async def run_replay_async(
    cfg: Config,
    *,
    log_dir: str | Path,
    state_db: str | Path | None = None,
    out_dir: str | Path | None = None,
    reports_dir: str | Path | None = "docs/REPORTS",
    strict: bool = True,
    settings: Settings | None = None,
    markets: Mapping[str, MarketInfo] | None = None,
    cache_dir: str | Path | None = None,
    guard_network: bool = True,
    segment: int | None = None,
) -> ReplayRun:
    """Replay the recorded run in ``log_dir`` (see the module docstring); ``run_replay`` outside a loop.

    ``state_db``: the recorded engine's state DB (default: the one its start event names).
    ``out_dir``: a new or empty directory for the replay's logs and state DB (default: a temporary
    directory). ``reports_dir``: where ``replay_<YYYYMMDD>.md`` is written (None: not written).
    ``cache_dir``: the run's LLM cache (default ``<log_dir>/llm_cache``, else the start event's).
    ``segment``: which process segment of the log directory (1-based; see ``load_recorded_run``).
    Raises ``ReplayInputError`` when the recorded run cannot be read; everything else, a cache miss
    included, is reported in the returned ``ReplayRun``.
    """
    run = load_recorded_run(log_dir, state_db, segment)
    out = Path(out_dir) if out_dir is not None else Path(tempfile.mkdtemp(prefix="wta-replay-"))
    if out.exists() and any(out.iterdir()):
        raise ReplayInputError(f"replay output directory {out} is not empty: the replay needs a fresh state DB and logs")
    cache = _cache_dir(cfg, run, cache_dir)
    runner = _Runner(cfg, run, out, strict=strict, settings=settings or Settings(), markets=markets, cache_dir=cache)
    with network_guard() if guard_network else _no_guard() as attempts:
        reason = "replay complete"
        try:
            runner.build()
            await runner.startup()
            await runner.drive()
            if runner.fatal:
                reason = "replay stopped: determinism failure"
        finally:
            runner.close(reason)
    rep_logs = out / "logs"
    report = compare_decisions(run.decisions, rep_logs, fields=COMPARE_FIELDS,
                               divergences=runner.exchange.all_divergences())
    result = ReplayRun(**{f: getattr(report, f) for f in ReplayReport.__dataclass_fields__})
    result.errors, result.notes = runner.errors, runner.notes
    result.strict_check = _strict_check(run.decisions, rep_logs)
    result.plans = [_plan_info(run, s) for s in runner.plans]
    result.config_changes = [_config_info(run, s) for s in runner.config_slots]
    for s in runner.plans:
        if s.conflict:
            result.notes.append(f"plan {s.decision_id}: the recorded decisions give no consistent swap tick "
                                f"(after {s.lower}, by {s.upper}); tick {s.index} was used.")
    result.notes.append("The slow loop is not re-run on a timer: each recorded plan is re-derived from the LLM cache "
                        "and tape and swapped in before the first tick after its recorded swap; heartbeats are not "
                        "replayed.")
    if runner.config_slots:
        result.notes.append("The reviewer is not re-run: its accepted config changes are re-applied from the recorded "
                            "config_change events and checked against the recorded config hash.")
    result.notes.append(
        "Exchange answers come from the record (ReplayExchange: same client ids, recorded fills and balance rows; "
        "its documented approximations apply). Two runner rules on top: the startup seed read gets the startup "
        "reconcile's balances row, and a balance refresh with no exchange-side change since the previous answer "
        f"gets that answer without using up a recorded row ({runner.exchange.unchanged_reads} such reads)."
    )
    if not run.decisions:
        result.notes.insert(0, "The recorded window holds no decision at all: the replay only shows that the engine "
                               "made none either (0/0 is not evidence of reproducing trades).")
    if run.synthetic:
        result.notes.insert(0, f"{SYNTHETIC_BANNER}: the recorded run used the offline synthetic feed.")
    result.run = {
        "recorded": {
            "log_dir": str(run.log_dir),
            "state_db": str(run.state_db),
            "mode": run.mode,
            "feed": run.feed,
            "synthetic": run.synthetic,
            "start": iso(run.start_ts),
            "start_reason": run.data.get("reason"),
            "segment": run.segment,
            "segments": run.segments,
            "in_progress": run.in_progress,
            "git_commit": run.data.get("git_commit"),
            "git_dirty": run.data.get("git_dirty"),
            "markets_source": run.data.get("markets_source"),
            "kinds": _kinds(run.decisions),
            "excluded": dict(run.excluded),
        },
        "window": {
            "first": iso(run.first_ts),
            "last": iso(run.last_ts),
            "hours": round((run.last_ts - run.first_ts) / 3600.0, 4),
            "ticks": len(run.ticks),
        },
        "config": {
            "recorded_hash": run.config_hash,
            "replay_hash": runner.cfg.config_hash,
            "equal": runner.cfg.config_hash == run.config_hash,
            "source": runner.cfg_source,
        },
        "inputs": {**runner.counts, "books_loaded": runner.books.loaded},
        "ai": {
            "source": runner.engine.ai_source,
            "cache_dir": str(cache),
            "plans_recorded": len(runner.plans),
            "plans_replayed": sum(1 for s in runner.plans if s.reproduced is not None),
            "plans_reproduced": sum(1 for s in runner.plans if s.reproduced),
            "cache_hits": runner.cache.hits,
            "cache_misses": runner.cache.misses,
        },
        "exchange": {**runner.exchange.summary(), "unchanged_reads": runner.exchange.unchanged_reads},
        "network": {"socket_attempts": len(attempts), "provider_calls": sum(s.calls for s in runner.sentinels.values()),
                    "attempts": list(attempts)[:10], "guarded": guard_network},
        "markets_source": runner.markets_source,
        "out_dir": str(out),
        "strict": strict,
    }
    (out / "replay_report.json").write_text(canonical_json(result.to_dict()).decode("utf-8") + "\n", encoding="utf-8")
    date = utc_day(run.first_ts)
    (out / "replay_report.md").write_text(result.to_markdown(date=date), encoding="utf-8")
    if reports_dir is not None:
        path = report_path(reports_dir, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out / "replay_report.md", path)
        result.report_file = str(path)
    return result


def run_replay(
    cfg: Config,
    *,
    log_dir: str | Path,
    state_db: str | Path | None = None,
    out_dir: str | Path | None = None,
    reports_dir: str | Path | None = "docs/REPORTS",
    strict: bool = True,
    **options: Any,
) -> ReplayRun:
    """``run_replay_async`` for callers outside an event loop (``wta replay``, ``scripts/replay_check.py``)."""
    return asyncio.run(run_replay_async(cfg, log_dir=log_dir, state_db=state_db, out_dir=out_dir,
                                        reports_dir=reports_dir, strict=strict, **options))


@contextmanager
def _no_guard() -> Iterator[list[str]]:
    yield []


def _cache_dir(cfg: Config, run: RecordedRun, given: str | Path | None) -> Path:
    if given is not None:
        return Path(given)
    default = llm_cache_dir(cfg, run.log_dir)
    if default.is_dir():
        return default
    recorded = (run.data.get("ai") or {}).get("cache_dir")
    if recorded:
        alt = _resolve(str(recorded))
        if alt.is_dir():
            return alt
    return default


def _kinds(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Distinct decisions per kind (outcome lines counted apart)."""
    seen: dict[str, str] = {}
    outcomes = 0
    for rec in records:
        if rec.get("kind") == "outcome":
            outcomes += 1
        else:
            seen.setdefault(str(rec.get("decision_id")), str(rec.get("kind")))
    out: dict[str, int] = {}
    for kind in seen.values():
        out[kind] = out.get(kind, 0) + 1
    if outcomes:
        out["outcome lines"] = outcomes
    return out


def _plan_info(run: RecordedRun, s: PlanSlot) -> dict[str, Any]:
    n = len(run.ticks)
    return {
        "decision_id": s.decision_id,
        "prompt_hash": s.prompt_hash,
        "plan_utc": iso(s.plan_ts),
        "swap_utc": iso(s.swap_ts),
        "swap_source": s.swap_source,
        "activation_index": s.index,
        "activation_utc": iso(run.tick_ts[s.index]) if s.index < n else "after the last tick",
        "lower": s.lower,
        "upper": s.upper,
        "ticks": n,
        "conflict": s.conflict,
        "reproduced": s.reproduced,
        "error": s.error,
    }


def _config_info(run: RecordedRun, s: ConfigSlot) -> dict[str, Any]:
    n = len(run.ticks)
    return {
        **{k: v for k, v in asdict(s).items() if k not in ("ts", "done_ts")},
        "event_utc": iso(s.ts),
        "done_utc": iso(s.done_ts),
        "activation_utc": iso(run.tick_ts[s.index]) if s.index < n else "after the last tick",
    }


def find_run_dirs(root: str | Path) -> list[dict[str, Any]]:
    """Engine log directories under ``root`` (``root`` itself and its subdirectories, two levels):
    ``[{path, mode, feed, start_ts}]`` of their latest start event, newest first; replay outputs
    (mode ``replay``) are skipped."""
    base = Path(root)
    if not base.is_dir():
        return []
    dirs = [base] + sorted(p for p in base.iterdir() if p.is_dir())
    dirs += sorted(q for p in dirs[1:] for q in p.iterdir() if q.is_dir() and q.name not in ("llm_cache", "mcp_tape"))
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not any(d.glob("decisions*.jsonl")):
            continue
        starts = [e for e in _iter_events(d) if e.get("kind") == "start"]
        if not starts:
            continue
        data = starts[-1].get("data") or {}
        mode = str(data.get("mode") or "")
        if mode == REPLAY_MODE or not mode:
            continue
        out.append({"path": d, "mode": mode, "feed": str(data.get("feed") or "live"),
                    "start_ts": parse_iso(str(starts[-1].get("ts"))) or 0.0})
    out.sort(key=lambda r: (r["start_ts"], str(r["path"])), reverse=True)
    return out


def _iter_events(d: Path) -> Iterable[dict[str, Any]]:
    try:
        return list(iter_family(d, "events"))
    except (OSError, ValueError):
        return []


def pick_run_dir(root: str | Path) -> dict[str, Any] | None:
    """The newest live run under ``root`` if there is one, else the newest shadow run."""
    runs = find_run_dirs(root)
    live = [r for r in runs if r["mode"] == "live"]
    return (live or runs or [None])[0]
