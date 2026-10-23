"""Backtest runner (PRD-05 §3): the SAME engine and strategies over stored books or candles.

``run_backtest(cfg, driver=..., from_ts=..., to_ts=..., out_dir=...)`` (``arun_backtest`` inside an
event loop) builds one ``wta.engine.Engine`` — the class ``wta run`` uses, with the real
``FeatureEngine``, ``StrategyRouter``, S1/S2/S3, ``RiskManager``, ``OrderManager``, positions,
reconcile, flatten and decision log — and swaps only what PRD-05 §3.1 says may differ:

* ``exchange`` = ``BacktestExchange(driver)`` (the PaperExchange with the pessimistic fill model of
  ``backtest/fills.py``; fees from ``configs/fees_snapshot.yaml``);
* ``clock`` = a ``SimClock`` set to each simulated step (the engine wraps it in its ``TickClock``:
  strategies, risk, ids and logs never read the wall clock);
* the ``MarketDataBus`` is fed by this runner from the market database (read-only, default
  ``data.sqlite_path``), step by step, in time order:

  - ``driver="books"``: one tick per ``engine.fast_loop_interval_s`` grid cell that holds at least
    one recorded snapshot (gaps between recording windows are skipped, never invented). At each
    tick every watchlist symbol shows its latest snapshot as of the tick time, and only while it is
    at most ``engine.stale_data_s`` old (an older one is removed from the bus: the engine sees a
    missing book, as it would live). Recorded trades are published the same way. An order still
    open when its symbol's snapshots stop for more than ``engine.stale_data_s`` (a recording gap)
    is never filled on a snapshot after the gap (``BacktestExchange(max_gap_s=...)``): live, the
    OrderManager would have re-priced or canceled it within seconds.
  - ``driver="candles"``: one tick per 1-minute bar close (60 s) that has a candle of at least one
    watchlist symbol. Each symbol's book is ``BacktestExchange.make_synthetic_book`` of its bar
    (mid = close, spread = the median spread of the stored books recorded up to the window's end,
    never after it, else the reference spread of ``configs/markets_snapshot.yaml``; unlimited depth
    at best; ``meta.spreads`` names the source and the sample per symbol) and the bar is also the
    exchange's matching bar. A minute without a candle (no trades) inside a gap of at most
    ``MAX_GAP_FILL_S`` becomes a flat bar at the previous close (the rule the engine's
    ``CandleCache`` applies); after a longer gap the symbol's book goes stale.

  Both drivers feed the engine's candle cache (``Engine.ingest_candles``) with every 1-minute
  candle once it is complete (``ts + 60 <= now``); 15-minute bars are resampled from them by the
  FeatureEngine (one truth). Candles of the 26 h before the window are preloaded, like the live
  engine's startup load.

* AI (``ai``): ``off`` -> ``ai.enabled: false`` (plan None, the ablation); ``offline`` -> the
  deterministic offline rules provider (LLM keys removed from the settings: NOT an LLM, labelled);
  ``on`` -> the configured LLM (OpenRouter/Gemini; offline rules if no key is set, and the report
  says so). The analyst's ``slow_step`` is polled at simulated times (``SLOW_LOOP_POLL_S``; the
  analyst itself keeps the ``engine.slow_loop_interval_s`` cadence and its event/move triggers),
  local tools only (MCP off), at most ``ai_max_calls`` cycles per run (cached or not, so a rerun
  makes exactly the same cycles). Every answer is cached under ``<out_dir>/llm_cache`` (the
  default ``ai.cache_dir`` resolves next to the run's logs), so a rerun is free and reproducible.
  ``cache`` -> re-runs a cached ``ai=on`` run without any key: the analyst's routes are read from
  the cache entries (``cached_routes``), the providers are stand-ins that refuse every call, and
  the cache is strict, so a miss stops the run with ``CacheMissError`` (never a provider call).
  Point ``ai.cache_dir`` at the run's ``llm_cache/`` (``--set ai.cache_dir=...``).
  The Post-trade Reviewer is not run in backtests (no config changes mid-run).

Outputs in ``out_dir``: the engine's own logs (``decisions-*.jsonl`` + ``decisions.jsonl``,
``orders``, ``events``, sparse ``health`` — a heartbeat at start, every ``heartbeat_every_s`` of
simulated time and at the end), the state DB ``state.sqlite`` (orders, fills, positions, equity,
plans and the replay inputs), and through ``backtest.report.write_report``: ``report.md``,
``summary.json``, ``equity.csv``, ``trades.csv`` and the PNGs. ``meta`` is honest about the data:
requested window vs data present, coverage hours (only time with data), data source (recorded or
SYNTHETIC), AI mode and source, strategies, config hash and git commit; ``limitations`` are filled
in automatically (e.g. "S1 not backtested on candles", "book window = N h of recorded data").

``make_synthetic_market_db`` writes an explicitly synthetic market DB (``SyntheticFeed``; a
``synthetic_market_db`` kv marker; every book ``source="synthetic"``) for offline evidence when real
data is missing. Every backtest on it carries the SYNTHETIC banner. It never overwrites a database
without that marker.

Also here, for the ``wta`` CLI: ``report_from_logs`` (a shadow/live run's report from its logs and
state DB, with a paper-vs-live divergence section) and ``export_tables`` (market tables to Parquet,
CSV when pyarrow is missing).

Determinism: no randomness except the seeded synthetic generator, no wall-clock reads in what is
logged (time comes from the simulated clock), symbols and records walked in sorted/time order.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import logging
import math
import random
import sqlite3
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from backtest.metrics import (
    Metrics,
    compute_metrics,
    failed_codes,
    is_veto,
    read_equity_table,
    trades_from_decisions,
)
from backtest.report import (
    DRAWDOWN_PNG,
    EQUITY_CSV,
    EQUITY_PNG,
    REGIMES_PNG,
    SUMMARY_JSON,
    TRADES_CSV,
    VETOES_PNG,
    write_report,
)
from wta.ai.llm_client import ProviderError, Route
from wta.ai.offline_provider import OFFLINE_STATUS
from wta.ai.wiring import AI_LLM, AI_OFFLINE, llm_cache_dir
from wta.config import Config, Settings, apply_changes, load_settings
from wta.data.collector import CANDLE_CACHE_KEEP_S
from wta.data.store import Store
from wta.engine import (
    SLOW_LOOP_POLL_S,
    SYNTHETIC_NOTE,
    Engine,
    TickClock,
    acquire_engine_lock,
    default_strategies,
    release_engine_lock,
)
from wta.exchange.backtest import BacktestExchange, apply_fee_snapshot
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot, snapshot_reference
from wta.execution.positions import valuate
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import family_files, iter_family
from wta.models import Balance, BookSnapshot, Candle, MarketInfo
from wta.utils.decimalutil import D, quantize_down
from wta.utils.timeutil import SimClock, iso, parse_iso

log = logging.getLogger(__name__)

DRIVERS = ("books", "candles")
AI_MODES = ("off", "on", "offline", "cache")
STRATEGY_IDS = ("S1", "S2", "S3")
DEFAULT_STRATEGIES = {"candles": ("S2", "S3"), "books": ("S1", "S2")}
DEFAULT_AI_MAX_CALLS = 150
CANDLE_STEP_S = 60.0
BAR_S = 60
HEARTBEAT_EVERY_S = 3600.0
MAX_GAP_FILL_S = 1800.0  # candle driver: minutes without a candle are filled flat inside such a gap
FLATTEN_AUTO_FACTOR = 4  # flatten_at_end=None: on when the window >= 4 x engine.flatten_start_before_end_s
SPREAD_SAMPLE = 5000  # recorded books per symbol behind the observed median spread
CANDLE_CHUNK_S = 86400
USDTTMN = "USDTTMN"
MODE = "backtest"
STATE_DB = "state.sqlite"
LLM_CACHE = "llm_cache"
LOG_FAMILIES = ("decisions", "orders", "events", "health")
OUT_MARKER = ".wta-backtest"  # an out_dir this runner wrote: its old logs may be replaced
FEED_BACKTEST = "backtest"
FEED_SYNTHETIC = "synthetic"
SYNTHETIC_TAG = "SYNTHETIC — not evidence of performance"
SYNTHETIC_MARKER_KEY = "synthetic_market_db"
SYNTHETIC_GENERATOR = "backtest.runner.make_synthetic_market_db"
SYNTHETIC_VERSION = 2  # 2: candle-only volume clusters (``volume_model="clustered"``)
SYNTHETIC_VOLUME_MODELS = ("clustered", "uniform")
SYNTHETIC_VOLUME_TAU_S = 1800.0  # clustered volume: mean-reversion time of the log-activity process
SYNTHETIC_VOLUME_SIGMA = 0.5  # clustered volume: stationary std of the log-activity process
SYNTHETIC_START_TS = 1_790_812_800.0  # 2026-10-01T00:00:00Z: fixed, so a seed always gives the same DB
SYNTHETIC_SYMBOLS = ("USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT", "SOLTMN", "SOLUSDT")
_LLM_KEYS_OFF = {"openrouter_api_key": SecretStr(""), "gemini_api_key": SecretStr("")}
CACHE_PROVIDERS = ("openrouter", "gemini")  # LLMRoleCfg.provider


class CacheOnlyProvider:
    """``--ai cache``: stands in for a provider and refuses every call (the cache is strict, so a
    call can only come from a bug); ``calls`` counts them."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def chat(self, messages: Any, tools: Any, model: str, temperature: float, max_tokens: int,
                   json_mode: bool) -> Any:
        self.calls += 1
        raise ProviderError(f"backtest --ai cache: provider {self.name} must not be called")


def cached_routes(cache_dir: str | Path) -> list[Route]:
    """The (provider, model) routes of the answers in an LLM cache directory, most answers first
    (at most two: the analyst's route and its fallback). Raises ``ValueError`` when there is none."""
    counts: dict[tuple[str, str], int] = {}
    root = Path(cache_dir)
    for f in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        req = entry.get("request") if isinstance(entry, dict) else None
        if isinstance(req, dict) and req.get("provider") and req.get("model"):
            key = (str(req["provider"]), str(req["model"]))
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        raise ValueError(
            f"--ai cache: no cached LLM answers in {root}; point ai.cache_dir at the llm_cache/ "
            "of an ai=on run, e.g. --set ai.cache_dir=docs/REPORTS/bt_books_synth24h_6h_ai_on/llm_cache"
        )
    routes = [Route(p, m) for (p, m), _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    unknown = [r.provider for r in routes if r.provider not in CACHE_PROVIDERS]
    if unknown:
        raise ValueError(
            f"--ai cache: the cache in {root} holds answers of {', '.join(unknown)} (not an LLM "
            "provider); use --ai offline for the offline rules provider"
        )
    return routes


def cache_only_ai(cfg: Config, cache_dir: str | Path) -> tuple[Config, dict[str, Any], list[Route]]:
    """``--ai cache``: the run's config with the analyst (and fallback) routes of the cache, the
    refusing providers, and the routes."""
    routes = cached_routes(cache_dir)
    changes: dict[str, Any] = {"llm.analyst.provider": routes[0].provider, "llm.analyst.model": routes[0].model}
    if len(routes) > 1:
        changes.update({"llm.fallback.provider": routes[1].provider, "llm.fallback.model": routes[1].model})
    else:
        changes["llm.fallback.model"] = None
    providers: dict[str, Any] = {r.provider: CacheOnlyProvider(r.provider) for r in routes}
    return apply_changes(cfg, changes), providers, routes


# ================================================================= small helpers


def parse_ts(value: float | int | str | None, what: str = "time") -> float:
    """Unix seconds from a number or ISO-8601 text (``2026-09-22``, ``2026-10-22T10:00Z``)."""
    if value is None:
        raise ValueError(f"{what} is required")
    if isinstance(value, bool):
        raise ValueError(f"{what}: not a time: {value!r}")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        ts = parse_iso(text)
    except ValueError:
        raise ValueError(f"{what}: not an ISO-8601 time: {text!r}") from None
    if ts is None:
        raise ValueError(f"{what} is required")
    return ts


def parse_strategies(value: str | Sequence[str] | None, driver: str = "candles") -> tuple[str, ...]:
    """``"S2,S3"`` / ``["s1", "S2"]`` -> ``("S2", "S3")`` (sorted, unique); None -> the driver default."""
    if value is None:
        return DEFAULT_STRATEGIES.get(driver, ("S2",))
    items = value.split(",") if isinstance(value, str) else list(value)
    out = sorted({str(s).strip().upper() for s in items if str(s).strip()})
    bad = [s for s in out if s not in STRATEGY_IDS]
    if bad:
        raise ValueError(f"unknown strategies {', '.join(bad)} (choose from {', '.join(STRATEGY_IDS)})")
    if not out:
        raise ValueError("at least one strategy is needed (S1, S2, S3)")
    return tuple(out)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else D(value)


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def display_path(path: str | Path) -> str:
    """``path`` relative to the working directory when it is inside it, else its name: reports and
    summaries never carry a machine's absolute paths."""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return p.name


def needed_symbols(cfg: Config, strategies: Sequence[str]) -> list[str]:
    """Books a run of ``strategies`` needs: USDTTMN always, S1 = every coin's TMN and USDT book,
    S3 = its symbols plus their TMN pairs (the flatten target and a direct valuation)."""
    syms = {USDTTMN}
    if "S1" in strategies:
        for coin in cfg.strategies.s1_crossbook.coins:
            syms.update({f"{coin}TMN", f"{coin}USDT"})
    if "S3" in strategies:
        for sym in cfg.strategies.s3_momentum.symbols:
            syms.add(sym)
            if sym.endswith("USDT"):
                syms.add(f"{sym[:-4]}TMN")
    return sorted(s for s in syms if not cfg.markets.is_banned(s))


def backtest_config(
    cfg: Config,
    *,
    strategies: Sequence[str],
    ai: str,
    to_ts: float,
    flatten_at_end: bool,
    watchlist: Sequence[str],
    seed: int | None = None,
    initial_equity_tmn: Decimal | None = None,
) -> Config:
    """The run's config: strategies not in ``strategies`` disabled, ``ai.enabled`` per ``ai``, the
    challenge end = window end (``flatten_at_end``) or none, the watchlist of the run (its hash is
    the ``config_hash`` of every decision record)."""
    changes: dict[str, Any] = {
        "strategies.s1_crossbook.enabled": "S1" in strategies,
        "strategies.s2_usdtalloc.enabled": "S2" in strategies,
        "strategies.s3_momentum.enabled": "S3" in strategies,
        "ai.enabled": ai != "off",
        "engine.challenge_start_utc": None,
        "engine.challenge_end_utc": iso(to_ts) if flatten_at_end else None,
        "markets.watchlist": list(watchlist),
    }
    if seed is not None:
        changes["engine.rng_seed"] = int(seed)
    if initial_equity_tmn is not None:
        changes["engine.initial_equity_tmn"] = str(initial_equity_tmn)
    return apply_changes(cfg, changes)


def backtest_settings(settings: Settings, ai: str) -> Settings:
    """Settings for the run: no Wallex/MCP/Telegram use; LLM keys only for ``ai=on``."""
    update: dict[str, Any] = {"telegram_bot_token": SecretStr(""), "telegram_chat_id": ""}
    if ai != "on":
        update.update(_LLM_KEYS_OFF)
    return settings.model_copy(update=update)


@contextlib.contextmanager
def _quiet(enabled: bool) -> Iterator[None]:
    """Per-tick engine logging (INFO lines, the once-a-simulated-minute stale-book warning) is noise
    over thousands of simulated ticks: ERROR and up only. Everything that matters is in the run's
    events / health / decisions logs anyway."""
    if not enabled:
        yield
        return
    logger = logging.getLogger("wta")
    old = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(old)


# ================================================================= the market database (read-only)


class MarketDB:
    """Read-only access to a market database (the recorder's ``data/wta.sqlite`` or a synthetic one)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"market database {self.path} does not exist")
        self.store = Store.open_readonly(self.path)
        self._conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
        with contextlib.suppress(Exception):
            self.store.close()

    def __enter__(self) -> MarketDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def synthetic_info(self) -> dict[str, Any] | None:
        """The generator's kv marker, or ``{"source": "books"}`` when books carry ``source=synthetic``."""
        try:
            marker = self.store.kv_get(SYNTHETIC_MARKER_KEY)
        except sqlite3.Error:
            marker = None
        if isinstance(marker, dict):
            return marker
        try:
            row = self._conn.execute("SELECT 1 FROM books WHERE source = 'synthetic' LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        return {"source": "books with source=synthetic"} if row else None

    def _in(self, symbols: Sequence[str]) -> str:
        return ",".join("?" for _ in symbols)

    def book_times(self, symbols: Sequence[str], from_ts: float, to_ts: float) -> list[float]:
        if not symbols:
            return []
        rows = self._conn.execute(
            f"SELECT ts FROM books WHERE symbol IN ({self._in(symbols)}) AND ts >= ? AND ts <= ? ORDER BY ts",
            [*symbols, from_ts, to_ts],
        ).fetchall()
        return [float(r[0]) for r in rows]

    def candle_open_times(self, symbols: Sequence[str], from_open: float, to_open: float) -> list[int]:
        if not symbols:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT ts FROM candles WHERE resolution = '1' AND symbol IN ({self._in(symbols)})"
            " AND ts >= ? AND ts <= ? ORDER BY ts",
            [*symbols, from_open, to_open],
        ).fetchall()
        return [int(r[0]) for r in rows]

    def symbols_with_books(self, symbols: Sequence[str], from_ts: float, to_ts: float) -> list[str]:
        if not symbols:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT symbol FROM books WHERE symbol IN ({self._in(symbols)}) AND ts >= ? AND ts <= ?",
            [*symbols, from_ts, to_ts],
        ).fetchall()
        return sorted(r[0] for r in rows)

    def symbols_with_candles(self, symbols: Sequence[str], from_open: float, to_open: float) -> list[str]:
        if not symbols:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT symbol FROM candles WHERE resolution = '1' AND symbol IN ({self._in(symbols)})"
            " AND ts >= ? AND ts <= ?",
            [*symbols, from_open, to_open],
        ).fetchall()
        return sorted(r[0] for r in rows)

    def count(self, table: str, symbols: Sequence[str], from_ts: float, to_ts: float) -> int:
        if not symbols:
            return 0
        extra = " AND resolution = '1'" if table == "candles" else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE symbol IN ({self._in(symbols)}) AND ts >= ? AND ts <= ?{extra}",
            [*symbols, from_ts, to_ts],
        ).fetchone()
        return int(row[0])

    def observed_spreads(self, symbols: Sequence[str], to_ts: float | None = None) -> dict[str, dict[str, Any]]:
        """Median spread of the last ``SPREAD_SAMPLE`` two-sided recorded books per symbol stamped at or
        before ``to_ts`` (the backtest window's end: never books recorded after it):
        ``{symbol: {"spread": Decimal, "books": n, "from": first ts, "to": last ts}}``; a symbol
        without such books is left out (the caller falls back to the reference spread)."""
        from backtest.fills import median_spread

        out: dict[str, dict[str, Any]] = {}
        for sym in sorted(set(symbols)):
            rows = self._conn.execute(
                "SELECT ts, symbol, source, bids_json, asks_json FROM books WHERE symbol = ? AND ts <= ?"
                " ORDER BY ts DESC LIMIT ?",
                (sym, math.inf if to_ts is None else float(to_ts), SPREAD_SAMPLE),
            ).fetchall()
            books = [Store._row_to_book(r) for r in rows]
            spread = median_spread(books)
            if spread is not None:
                out[sym] = {"spread": spread, "books": sum(1 for b in books if b.spread_pct is not None),
                            "from": min(b.ts for b in books), "to": max(b.ts for b in books)}
        return out

    def iter_books(self, symbols: Sequence[str], from_ts: float, to_ts: float) -> Iterator[BookSnapshot]:
        return self.store.iter_books_multi(sorted(symbols), from_ts, to_ts)

    def iter_trades(self, symbols: Sequence[str], from_ts: float, to_ts: float) -> Iterator[Any]:
        streams = [self.store.iter_trades(s, from_ts, to_ts) for s in sorted(symbols)]
        return heapq.merge(*streams, key=lambda t: t.ts)


class _CandleFeeder:
    """Complete 1-minute candles of ``symbols`` in close order, loaded a day at a time."""

    def __init__(self, db: MarketDB, symbols: Sequence[str], from_open: float, to_open: float) -> None:
        self.db = db
        self.symbols = sorted(symbols)
        self.next_open = math.floor(from_open)
        self.to_open = to_open
        self.buf: deque[Candle] = deque()
        self.served = 0

    def _load(self) -> None:
        while not self.buf and self.next_open <= self.to_open:
            a = self.next_open
            b = min(a + CANDLE_CHUNK_S - 1, self.to_open)
            self.next_open = a + CANDLE_CHUNK_S
            rows: list[Candle] = []
            for sym in self.symbols:
                rows.extend(self.db.store.load_candles(sym, "1", a, b))
            rows.sort(key=lambda c: (c.ts, c.symbol))
            self.buf.extend(rows)

    def until(self, t: float) -> list[Candle]:
        """Every not yet served candle that is complete at ``t`` (``ts + 60 <= t``)."""
        out: list[Candle] = []
        while True:
            if not self.buf:
                self._load()
                if not self.buf:
                    break
            if self.buf[0].ts + BAR_S > t:
                break
            out.append(self.buf.popleft())
        self.served += len(out)
        return out


def book_steps(times: Sequence[float], step_s: float, to_ts: float) -> list[float]:
    """The books driver's tick times: each ``step_s`` grid cell with a snapshot, ticked at its end
    (``ceil(ts / step_s) * step_s``, capped at ``to_ts``)."""
    out: set[float] = set()
    for ts in times:
        k = math.ceil(round(ts / step_s, 9))
        out.add(min(k * step_s, to_ts))
    return sorted(out)


def candle_steps(open_times: Sequence[int], from_ts: float, to_ts: float) -> list[float]:
    """The candle driver's tick times: every bar close (``open + 60``) inside ``[from_ts, to_ts]``."""
    return sorted({float(t + BAR_S) for t in open_times if from_ts <= t + BAR_S <= to_ts})


@dataclass
class StepPlan:
    """What a run would iterate (also used by the walk-forward split)."""

    driver: str
    steps: list[float]
    watchlist: list[str]
    data_symbols: list[str]
    missing_symbols: list[str]
    step_s: float
    synthetic: dict[str, Any] | None


def plan_steps(
    cfg: Config,
    *,
    driver: str,
    from_ts: float | str,
    to_ts: float | str,
    strategies: Sequence[str] | str | None = None,
    market_db: str | Path | None = None,
    step_s: float | None = None,
) -> StepPlan:
    """Tick times and watchlist of a backtest without running it (``run_backtest`` uses it too)."""
    a, b = parse_ts(from_ts, "from"), parse_ts(to_ts, "to")
    strats = parse_strategies(strategies, driver)
    with MarketDB(market_db or cfg.data.sqlite_path) as db:
        return _plan_steps(cfg, db, driver, a, b, strats, step_s)


def _plan_steps(
    cfg: Config, db: MarketDB, driver: str, a: float, b: float, strats: Sequence[str], step_s: float | None
) -> StepPlan:
    if driver not in DRIVERS:
        raise ValueError(f"unknown driver {driver!r} (books | candles)")
    if b <= a:
        raise ValueError("the window is empty: --to must be after --from")
    needed = needed_symbols(cfg, [s for s in strats if not (driver == "candles" and s == "S1")])
    if driver == "books":
        step = float(step_s or cfg.engine.fast_loop_interval_s)
        have = db.symbols_with_books(needed, a, b)
        steps = book_steps(db.book_times(have, a, b), step, b)
    else:
        step = CANDLE_STEP_S
        have = db.symbols_with_candles(needed, a - CANDLE_CACHE_KEEP_S, b - BAR_S)
        steps = candle_steps(db.candle_open_times(have, a - BAR_S, b - BAR_S), a, b)
    watch = sorted(set(have) | {USDTTMN})
    missing = sorted(set(needed) - set(have))
    return StepPlan(driver, steps, watch, have, missing, step, db.synthetic_info())


# ================================================================= result


@dataclass
class BacktestResult:
    """``metrics`` (``backtest.metrics.Metrics``), ``paths`` (report, summary, csvs, logs, state DB),
    ``meta`` (what ``report.md`` states about the run) and the limitations it lists."""

    metrics: Metrics
    paths: dict[str, Path]
    meta: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    @property
    def out_dir(self) -> Path:
        return self.paths["out_dir"]

    @property
    def return_pct(self) -> float | None:
        return self.metrics.return_pct

    @property
    def max_drawdown_pct(self) -> float | None:
        return self.metrics.max_drawdown_pct


# ================================================================= the run


def _is_backtest_state_db(path: Path) -> bool:
    """A state DB a backtest wrote (its first start event says ``mode: backtest``)."""
    try:
        store = Store.open_readonly(path)
    except sqlite3.Error:
        return False
    try:
        start = next(store.iter_events("start"), None)
    except sqlite3.Error:
        start = None
    finally:
        store.close()
    return bool(start and (start.get("data") or {}).get("mode") == MODE)


def _clear_state_db(path: Path, out: Path) -> None:
    """Remove the previous run's state DB (inside ``out``, or a backtest state DB elsewhere)."""
    inside = path.resolve().parent == out.resolve()
    if path.exists() and not inside and not _is_backtest_state_db(path):
        raise ValueError(f"refusing to replace {path}: it is not a backtest state database")
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(path) + suffix)
        if q.exists() or q.is_symlink():
            q.unlink()


def _prepare_out_dir(out: Path, state_path: Path | None = None) -> None:
    """Create ``out``; a previous run's logs and state DB there are removed — but only when this
    runner wrote them (``OUT_MARKER``), never another engine's logs."""
    out.mkdir(parents=True, exist_ok=True)
    marker = out / OUT_MARKER
    logs = [p for base in LOG_FAMILIES for p in family_files(out, base)]
    if logs and not marker.is_file():
        raise ValueError(
            f"refusing to write a backtest into {out}: it holds logs this runner did not write "
            "(pick an empty or a backtest output directory)"
        )
    for base in LOG_FAMILIES:
        for p in family_files(out, base):
            p.unlink()
        link = out / f"{base}.jsonl"
        if link.is_symlink():
            link.unlink()
    for name in (STATE_DB, f"{STATE_DB}-wal", f"{STATE_DB}-shm", "journal.md"):
        p = out / name
        if p.exists() or p.is_symlink():
            p.unlink()
    if state_path is not None:
        _clear_state_db(state_path, out)
        state_path.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("written by backtest/runner.py; old logs here are replaced on every run\n", encoding="utf-8")


def _initial_balances(initial_balances: Mapping[str, Any] | None, equity: Decimal) -> dict[str, Decimal]:
    if initial_balances is None:
        return {"TMN": equity}
    out = {str(k).upper(): D(v) for k, v in sorted(initial_balances.items())}
    if any(v < 0 for v in out.values()):
        raise ValueError("initial balances must not be negative")
    return out


def build_backtest_engine(
    cfg: Config,
    settings: Settings,
    *,
    exchange: BacktestExchange,
    markets: dict[str, MarketInfo],
    bus: MarketDataBus,
    clock: TickClock,
    store: Store,
    log_dir: Path,
    feed: str,
    ai: str,
    ai_providers: dict[str, Any] | None = None,
) -> Engine:
    """The thin variant of ``wta.engine.build_engine`` for backtests: the same ``Engine`` and
    ``default_strategies``, with the injected exchange/clock/bus/store; no REST, WebSocket, KILL
    file or Telegram; the AI layer with local tools only (MCP off)."""
    engine = Engine(
        cfg,
        settings,
        MODE,
        clock,
        bus,
        store,
        exchange=exchange,
        markets=markets,
        markets_source="snapshot",
        feed=feed,
        log_dir=log_dir,
        kill_file=None,
        strategies=default_strategies(cfg),
    )
    if ai != "off":
        engine.enable_ai(providers=ai_providers, mcp=None, runtime_dir=log_dir / "runtime", runtime_overlay=None,
                         strict_cache=ai == "cache")
    return engine


def run_backtest(cfg: Config, **kwargs: Any) -> BacktestResult:
    """Synchronous ``arun_backtest`` (do not call it from inside a running event loop)."""
    return asyncio.run(arun_backtest(cfg, **kwargs))


async def arun_backtest(
    cfg: Config,
    *,
    driver: str,
    from_ts: float | str,
    to_ts: float | str,
    out_dir: str | Path,
    strategies: Sequence[str] | str | None = None,
    ai: str = "off",
    ai_max_calls: int = DEFAULT_AI_MAX_CALLS,
    market_db: str | Path | None = None,
    seed: int | None = None,
    step_s: float | None = None,
    initial_equity_tmn: Any = None,
    initial_balances: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
    ai_providers: dict[str, Any] | None = None,
    flatten_at_end: bool | None = None,
    heartbeat_every_s: float = HEARTBEAT_EVERY_S,
    title: str | None = None,
    limitations: Sequence[str] = (),
    llm_cache: str | Path | None = None,
    state_db: str | Path | None = None,
    quiet: bool = True,
    write: bool = True,
    report_date: str | None = None,
) -> BacktestResult:
    """One backtest (see the module docstring). ``report_date`` (``YYYY-MM-DD``): the report's
    date (default: the commit date of the code, ``report_date_of``). Raises ``ValueError`` on bad arguments and
    ``FileNotFoundError`` when the market DB is missing; an empty window still writes a report
    (``INSUFFICIENT DATA``). ``state_db``: where the engine's state DB goes (default
    ``<out_dir>/state.sqlite``; a 30-day candle run writes ~130 MB of replay inputs, which do not
    belong in ``docs/REPORTS``)."""
    if driver not in DRIVERS:
        raise ValueError(f"unknown driver {driver!r} (books | candles)")
    if ai not in AI_MODES:
        raise ValueError(f"unknown ai mode {ai!r} (off | on | offline | cache)")
    if ai_max_calls < 0:
        raise ValueError("ai_max_calls must be >= 0")
    a, b = parse_ts(from_ts, "from"), parse_ts(to_ts, "to")
    requested = parse_strategies(strategies, driver)
    notes: list[str] = list(limitations)
    strats = requested
    if driver == "candles" and "S1" in requested:
        strats = tuple(s for s in requested if s != "S1")
        notes.append(
            "S1 was requested but is not backtested on candles (no cross-book information): it is "
            "disabled in this run."
        )
        if not strats:
            raise ValueError("S1 cannot be backtested on candles; use --driver books or add S2/S3")
    equity0 = D(initial_equity_tmn) if initial_equity_tmn is not None else cfg.engine.initial_equity_tmn
    if equity0 <= 0:
        raise ValueError("initial equity must be > 0")
    balances0 = _initial_balances(initial_balances, equity0)
    out = Path(out_dir)
    db_path = Path(market_db or cfg.data.sqlite_path)
    state_path = Path(state_db) if state_db is not None else out / STATE_DB

    with MarketDB(db_path) as db:
        plan = _plan_steps(cfg, db, driver, a, b, strats, step_s)
        flatten = (
            flatten_at_end
            if flatten_at_end is not None
            else (b - a) >= FLATTEN_AUTO_FACTOR * cfg.engine.flatten_start_before_end_s
        )
        run_cfg = backtest_config(
            cfg, strategies=strats, ai=ai, to_ts=b, flatten_at_end=flatten, watchlist=plan.watchlist, seed=seed,
            initial_equity_tmn=equity0 if initial_balances is None else None,
        )
        run_settings = backtest_settings(settings if settings is not None else load_settings(), ai)
        cache_routes: list[Route] = []
        if ai == "cache":
            if ai_providers is not None:
                raise ValueError("--ai cache brings its own refusing providers (ai_providers must be None)")
            run_cfg, ai_providers, cache_routes = cache_only_ai(
                run_cfg, Path(llm_cache) if llm_cache is not None else llm_cache_dir(run_cfg, out)
            )
        _prepare_out_dir(out, state_path)
        if llm_cache is not None and ai != "off":
            link = out / LLM_CACHE
            Path(llm_cache).mkdir(parents=True, exist_ok=True)
            if not link.exists() and not link.is_symlink():
                link.symlink_to(Path(llm_cache).resolve(), target_is_directory=True)
        with _quiet(quiet):
            run = _Run(run_cfg, run_settings, db, plan, out, a, b, balances0, ai, ai_max_calls, ai_providers,
                       heartbeat_every_s, strats, state_path)
            run.cache_routes = [f"{r.provider}/{r.model}" for r in cache_routes]
            await run.execute()
    return _finish(run, cfg=run_cfg, requested=requested, strats=strats, flatten=flatten, notes=notes,
                   title=title, write=write, db_path=db_path, report_date=report_date)


class _Run:
    """State of one backtest while it runs."""

    def __init__(
        self,
        cfg: Config,
        settings: Settings,
        db: MarketDB,
        plan: StepPlan,
        out: Path,
        from_ts: float,
        to_ts: float,
        balances: dict[str, Decimal],
        ai: str,
        ai_max_calls: int,
        ai_providers: dict[str, Any] | None,
        heartbeat_every_s: float,
        strategies: Sequence[str],
        state_path: Path,
    ) -> None:
        self.cfg, self.settings, self.db, self.plan, self.out = cfg, settings, db, plan, out
        self.from_ts, self.to_ts = from_ts, to_ts
        self.balances0 = balances
        self.ai, self.ai_max_calls, self.ai_providers = ai, ai_max_calls, ai_providers
        self.cache_routes: list[str] = []  # --ai cache: the routes read from the cache
        self.heartbeat_every_s = heartbeat_every_s
        self.strategies = tuple(strategies)
        self.driver = plan.driver
        self.feed = FEED_SYNTHETIC if plan.synthetic else FEED_BACKTEST
        self.stale_s = cfg.engine.stale_data_s
        self.markets = apply_fee_snapshot(load_markets_snapshot())
        self.watch = [s for s in plan.watchlist if s in self.markets]
        self.state_path = state_path
        # counters for meta / limitations
        self.ticks = 0
        self.snapshots = 0
        self.trades_published = 0
        self.candles_ingested = 0
        self.warmup_candles = 0
        self.warmup_from: float | None = None
        self.filled_minutes: dict[str, int] = {}
        self.symbol_ticks: dict[str, int] = {}
        self.s1_ticks = 0
        self.ai_capped_at: float | None = None
        self.data_from: float | None = None
        self.data_to: float | None = None
        self.initial_equity: Decimal | None = None
        self.engine: Engine | None = None
        self.exchange: BacktestExchange | None = None
        self.spreads: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ setup

    def _build(self) -> None:
        self.clock = SimClock(self.plan.steps[0] if self.plan.steps else self.from_ts)
        self.tick_clock = TickClock(self.clock)
        self.bus = MarketDataBus()
        observed = self.db.observed_spreads(self.watch, to_ts=self.to_ts) if self.driver == "candles" else {}
        self.exchange = BacktestExchange(
            self.markets,
            self.tick_clock,
            driver=self.driver,  # type: ignore[arg-type]
            books=self.bus.snapshot if self.driver == "books" else None,
            spreads={s: v["spread"] for s, v in observed.items()},
            max_gap_s=self.stale_s if self.driver == "books" else None,
            initial_balances=dict(self.balances0),
            initial_equity_tmn=self.cfg.engine.initial_equity_tmn,
            use_fee_snapshot=False,  # the markets above already carry the fee snapshot
        )
        if self.driver == "candles":
            self.spreads = {}
            for s in self.watch:
                info: dict[str, Any] = {"spread_pct": str(self.exchange.spread_for(s)),
                                        "source": self.exchange.spread_source(s)}
                if info["source"] == "observed":
                    obs = observed[s]
                    info.update(books=obs["books"], **{"from": iso(obs["from"]), "to": iso(obs["to"])})
                self.spreads[s] = info
        self.lock = acquire_engine_lock(str(self.state_path))
        self.store = Store(
            self.state_path,
            flush_interval_s=self.cfg.data.store_flush_interval_s,
            flush_rows=max(self.cfg.data.store_flush_rows, 2000),
            depth_levels=self.cfg.data.depth_levels_stored,
        )
        self.engine = build_backtest_engine(
            self.cfg, self.settings, exchange=self.exchange, markets=self.markets, bus=self.bus,
            clock=self.tick_clock, store=self.store, log_dir=self.out, feed=self.feed, ai=self.ai,
            ai_providers=self.ai_providers,
        )
        self.engine.lock = self.lock
        warm_from = self.from_ts - CANDLE_CACHE_KEEP_S
        self.candles = _CandleFeeder(self.db, self.watch, warm_from, self.to_ts - BAR_S)
        if self.driver == "books":
            self.books_it = self.db.iter_books(self.watch, self.from_ts - self.stale_s, self.to_ts)
            self.next_book = next(self.books_it, None)
            self.trades_it = self.db.iter_trades(self.watch, self.from_ts - self.stale_s, self.to_ts)
            self.next_trade = next(self.trades_it, None)
        self.last_bar: dict[str, Candle] = {}
        self.last_real_close: dict[str, float] = {}

    # ------------------------------------------------------------ data per step

    def _drop_stale(self, t: float) -> None:
        for sym in sorted(self.bus.books):
            if t - self.bus.books[sym].ts > self.stale_s:
                del self.bus.books[sym]

    def _feed_books(self, t: float) -> None:
        while self.next_book is not None and self.next_book.ts <= t:
            self.bus.publish_book(self.next_book)
            if self.next_book.ts >= self.from_ts:
                self.snapshots += 1
                self.data_from = self.next_book.ts if self.data_from is None else self.data_from
                self.data_to = self.next_book.ts
            self.next_book = next(self.books_it, None)
        while self.next_trade is not None and self.next_trade.ts <= t:
            self.bus.publish_trade(self.next_trade)
            self.trades_published += 1
            self.next_trade = next(self.trades_it, None)
        self._drop_stale(t)

    def _feed_bars(self, t: float, closed: list[Candle]) -> None:
        assert self.exchange is not None
        real = {c.symbol: c for c in closed if c.ts + BAR_S == t}
        for sym in self.watch:
            bar = real.get(sym)
            if bar is not None:
                self.last_real_close[sym] = t
                self.data_from = t - BAR_S if self.data_from is None else self.data_from
                self.data_to = t
            else:
                prev = self.last_bar.get(sym)
                last_close = self.last_real_close.get(sym)
                if prev is None or last_close is None or t - last_close > MAX_GAP_FILL_S:
                    continue
                bar = Candle(symbol=sym, resolution="1", ts=int(t - BAR_S), o=prev.c, h=prev.c, l=prev.c, c=prev.c,
                             v=Decimal(0))
                self.filled_minutes[sym] = self.filled_minutes.get(sym, 0) + 1
            self.last_bar[sym] = bar
            self.exchange.set_bar(sym, bar)
        books = self.exchange.synthetic_books()
        for sym in sorted(books):
            if books[sym].ts == t:
                self.bus.publish_book(books[sym])
        self._drop_stale(t)

    def _feed(self, t: float) -> None:
        self.clock.set(t)
        closed = self.candles.until(t)
        if self.driver == "books":
            self._feed_books(t)
        else:
            self._feed_bars(t, closed)
        if closed:
            self.candles_ingested += self.engine.ingest_candles(closed)  # type: ignore[union-attr]
        self.bus.mark_source(self.feed, True, t)
        fresh = sorted(self.bus.books)
        for sym in fresh:
            self.symbol_ticks[sym] = self.symbol_ticks.get(sym, 0) + 1
        if USDTTMN in self.bus.books and any(
            f"{c}TMN" in self.bus.books and f"{c}USDT" in self.bus.books for c in self.cfg.strategies.s1_crossbook.coins
        ):
            self.s1_ticks += 1

    def _warmup(self, first: float) -> None:
        """Candles complete before the first tick into the cache (the live engine's startup load)."""
        warm = self.candles.until(first - 1e-6)
        if warm:
            self.warmup_candles = self.engine.ingest_candles(warm)  # type: ignore[union-attr]
            self.warmup_from = float(min(c.ts for c in warm))
            for c in warm:
                self.last_bar[c.symbol] = c
                self.last_real_close[c.symbol] = float(c.ts + BAR_S)

    def _value_initial(self) -> Decimal:
        bals = {a: Balance(asset=a, free=v) for a, v in self.balances0.items()}
        if set(bals) <= {"TMN"}:
            return self.balances0.get("TMN", D(0))
        val = valuate(bals, self.bus.snapshot())
        if not val.complete:
            raise ValueError(f"cannot value the initial balances: no price for {', '.join(val.missing_prices)}")
        return val.equity_tmn

    # ------------------------------------------------------------ loop

    async def execute(self) -> None:
        try:
            self._build()
        except BaseException:
            store = getattr(self, "store", None)
            if store is not None:
                with contextlib.suppress(Exception):
                    store.close()
            release_engine_lock(getattr(self, "lock", None))
            raise
        engine = self.engine
        assert engine is not None
        steps = self.plan.steps
        try:
            if not steps:
                await engine.startup()
                engine.write_heartbeat(self.clock.now())
                return
            first = steps[0]
            self.clock.set(first)
            self._warmup(first)
            self._feed(first)
            self.initial_equity = self._value_initial() if steps else None
            await engine.startup()
            engine.write_heartbeat(first)
            last_hb = first
            last_ai_poll = float("-inf")
            for i, t in enumerate(steps):
                if i:
                    self._feed(t)
                await engine.safe_tick(t)
                self.ticks += 1
                if engine.analyst is not None and self.ai_capped_at is None and t - last_ai_poll >= SLOW_LOOP_POLL_S:
                    last_ai_poll = t
                    if engine.ai_stats["cycles"] >= self.ai_max_calls:
                        self.ai_capped_at = t
                    else:
                        await engine.slow_step(t)
                if t - last_hb >= self.heartbeat_every_s:
                    engine.write_heartbeat(t)
                    last_hb = t
        finally:
            reason = f"backtest complete: {self.ticks} ticks"
            engine.shutdown(reason)
            await engine.aclose()  # closes the state DB and releases the lock


# ================================================================= results + report


def _decision_records(out: Path) -> list[dict[str, Any]]:
    return list(iter_family(out, "decisions"))


def _ro_rows(db: Path | None, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    """Rows of a read-only query on a state DB; ``[]`` when the file or the table is missing."""
    if db is None or not Path(db).is_file():
        return []
    try:
        conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def add_trade_fees(trades: list[dict[str, Any]], db: Path | None) -> list[dict[str, Any]]:
    """Fill each closed trade's ``fees_tmn`` from its position in the state DB (``meta.fees_tmn``,
    what the PositionManager booked: both legs, TMN-valued at fill time). Outcome lines do not
    carry fees, so without this every per-trade / per-strategy fee column read 0."""
    missing = sorted({str(t.get("position_id")) for t in trades if t.get("fees_tmn") is None and t.get("position_id")})
    if not missing:
        return trades
    fees: dict[str, Any] = {}
    for pid, raw in _ro_rows(db, "SELECT id, json FROM positions"):
        if pid not in missing:
            continue
        try:
            meta = (json.loads(raw) or {}).get("meta") or {}
        except (TypeError, ValueError):
            continue
        if meta.get("fees_tmn") is not None:
            fees[pid] = _f(meta["fees_tmn"])
    for t in trades:
        if t.get("fees_tmn") is None and str(t.get("position_id")) in fees:
            t["fees_tmn"] = fees[str(t.get("position_id"))]
    return trades


def state_fees_tmn(db: Path | None) -> float | None:
    """The engine's fee total (``portfolio_state.fees_paid_tmn_total`` in the state DB kv), or None."""
    for (raw,) in _ro_rows(db, "SELECT json FROM kv WHERE key = 'portfolio_state'"):
        try:
            return _f((json.loads(raw) or {}).get("fees_paid_tmn_total"))
        except (TypeError, ValueError):
            return None
    return None


def _coverage_h(run: _Run) -> float:
    return run.ticks * run.plan.step_s / 3600.0


def _finish(
    run: _Run,
    *,
    cfg: Config,
    requested: Sequence[str],
    strats: Sequence[str],
    flatten: bool,
    notes: list[str],
    title: str | None,
    write: bool,
    db_path: Path,
    report_date: str | None = None,
) -> BacktestResult:
    engine = run.engine
    assert engine is not None
    out = run.out
    raw = _decision_records(out)
    merged = [dict(v) for v in merge_decisions(raw).values()]
    trades = add_trade_fees(trades_from_decisions(merged), run.state_path)
    equity = read_equity_table(run.state_path)
    val = engine.portfolio.last_valuation
    final_valuation = (
        {
            "equity_tmn": _f(val.equity_tmn),
            "tmn": _f(val.tmn),
            "usdt_tmn_value": _f(val.usdt_tmn_value),
            "coins_tmn_value": _f(val.coins_tmn_value),
        }
        if val is not None and val.usable
        else None
    )
    synthetic = run.plan.synthetic is not None
    label = f"backtest {run.driver} {','.join(strats)} ai={run.ai}"
    metrics = compute_metrics(
        equity,
        trades,
        initial_equity=run.initial_equity,
        decisions=raw,
        fees_paid_tmn=engine.portfolio.fees_paid_tmn_total,
        final_valuation=final_valuation,
        label=label + (" SYNTHETIC" if synthetic else ""),
    )
    coverage = _coverage_h(run)
    ai_source = engine.ai_source
    cache_dir = llm_cache_dir(cfg, out) if run.ai != "off" else None
    meta: dict[str, Any] = {
        "mode": MODE,
        "driver": run.driver,
        "window_from": iso(run.from_ts),
        "window_to": iso(run.to_ts),
        "data_from": iso(run.data_from) if run.data_from is not None else None,
        "data_to": iso(run.data_to) if run.data_to is not None else None,
        "data_hours": round(coverage, 4),
        "coverage_hours": round(coverage, 4),
        "data_source": (
            f"SYNTHETIC generated market DB {db_path.name} ({_synthetic_text(run.plan.synthetic)})"
            if synthetic
            else f"recorded Wallex market data ({db_path.name})"
        ),
        "synthetic": synthetic,
        "market_db": display_path(db_path),
        "state_db": display_path(run.state_path),
        "ai": run.ai,
        "ai_source": ai_source,
        "strategies": list(strats),
        "strategies_requested": list(requested),
        "config_hash": engine.config_hash,
        "git_commit": engine.git_commit,
        "git_dirty": engine.git_dirty,
        "seed": cfg.engine.rng_seed,
        "step_s": run.plan.step_s,
        "ticks": run.ticks,
        "watchlist": list(run.watch),
        "symbols_without_data": list(run.plan.missing_symbols),
        "coverage_hours_by_symbol": {
            s: round(n * run.plan.step_s / 3600.0, 4) for s, n in sorted(run.symbol_ticks.items())
        },
        "initial_equity_tmn": _f(run.initial_equity),
        "initial_balances": {k: str(v) for k, v in sorted(run.balances0.items())},
        "flatten_at_end": flatten,
        "tick_crashes": engine.tick_crashes,
        "fee_budget_pct": float(cfg.risk.fee_budget_pct),
        "fees_source": "configs/fees_snapshot.yaml",
        "data_end": iso(run.to_ts),  # the window's end (the data's time, not when the report was made)
        **report_date_of(report_date),
        "candles_ingested": run.candles_ingested,
        "warmup_candles": run.warmup_candles,
    }
    if run.driver == "books":
        meta.update(snapshots=run.snapshots, trades_published=run.trades_published,
                    s1_coverage_hours=round(run.s1_ticks * run.plan.step_s / 3600.0, 4),
                    orders_stopped_by_gaps=engine_gap_orders(run))
    else:
        meta.update(spreads=run.spreads, gap_filled_minutes=dict(sorted(run.filled_minutes.items())))
    if run.ai != "off":
        meta.update(
            ai_max_calls=run.ai_max_calls,
            analyst_cycles=engine.ai_stats.get("cycles", 0),
            analyst_plans=engine.ai_stats.get("plans", 0),
            analyst_failures=engine.ai_stats.get("failures", 0),
            model_calls=engine.ai_stats.get("model_calls", 0),
            cached_calls=engine.ai_stats.get("cached_calls", 0),
            ai_capped_at=iso(run.ai_capped_at) if run.ai_capped_at is not None else None,
            llm_cache=display_path(cache_dir) if cache_dir is not None else None,
            ai_routes=dict(engine.ai_setup.routes) if engine.ai_setup is not None else {},
        )
        if run.ai == "cache":
            meta["ai_cache_routes"] = list(run.cache_routes)
            meta["provider_calls"] = sum(getattr(p, "calls", 0) for p in (run.ai_providers or {}).values())
    if synthetic:
        meta["synthetic_note"] = SYNTHETIC_NOTE
    limits = _limitations(run, cfg, meta, flatten, notes, raw)
    paths: dict[str, Path] = {
        "out_dir": out,
        "state_db": run.state_path,
        "decisions": out / "decisions.jsonl",
        "orders": out / "orders.jsonl",
        "events": out / "events.jsonl",
        "health": out / "health.jsonl",
    }
    if cache_dir is not None:
        paths["llm_cache"] = Path(cache_dir)
    if write:
        heading = title or _title(run, strats, synthetic)
        report = write_report(out, heading, meta, metrics, equity, trades, decisions=merged, plans=None,
                              limitations=limits)
        paths.update(
            report=report,
            summary=out / SUMMARY_JSON,
            equity_csv=out / EQUITY_CSV,
            trades_csv=out / TRADES_CSV,
            equity_png=out / EQUITY_PNG,
            drawdown_png=out / DRAWDOWN_PNG,
            vetoes_png=out / VETOES_PNG,
            regimes_png=out / REGIMES_PNG,
        )
    return BacktestResult(metrics=metrics, paths=paths, meta=meta, limitations=limits)


def engine_gap_orders(run: _Run) -> int:
    """Orders the BacktestExchange kept from filling across a recording gap (books driver)."""
    return int(getattr(run.exchange, "gap_blocked", 0) or 0)


def report_date_of(given: str | None = None) -> dict[str, Any]:
    """``report_date`` (``YYYY-MM-DD``) and ``report_date_source`` for a report's meta: ``given``
    (``--report-date``), else the commit date of the code that made the report (deterministic: a
    rerun of the same commit gives the same report), never the data's end."""
    from wta.utils import gitinfo

    if given:
        try:
            day = parse_iso(str(given).strip())
        except ValueError:
            day = None
        if day is None:
            raise ValueError(f"report date: not a date: {given!r} (YYYY-MM-DD)")
        return {"report_date": iso(day)[:10], "report_date_source": "given with --report-date"}
    day_text = gitinfo.commit_date()
    commit = gitinfo.short_commit()
    dirty = " + uncommitted changes" if gitinfo.get().get("dirty") else ""
    return {"report_date": day_text,
            "report_date_source": f"commit date of {commit}{dirty}" if day_text else "unknown (no git)"}


def _synthetic_text(info: Mapping[str, Any] | None) -> str:
    if not info:
        return "synthetic"
    parts = [f"{k} {info[k]}" for k in ("seed", "hours", "step_s", "volume_model") if k in info]
    return ", ".join(parts) if parts else str(info.get("source") or "synthetic")


def _title(run: _Run, strats: Sequence[str], synthetic: bool) -> str:
    span = f"{iso(run.from_ts)[:16]}Z → {iso(run.to_ts)[:16]}Z"
    text = f"Backtest ({run.driver}) {', '.join(strats)}, ai={run.ai}, {span}"
    return f"{text} — SYNTHETIC" if synthetic else text


def _limitations(run: _Run, cfg: Config, meta: Mapping[str, Any], flatten: bool, notes: list[str],
                 decisions: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    engine = run.engine
    assert engine is not None
    out = list(notes)
    req_h = (run.to_ts - run.from_ts) / 3600.0
    cov = meta["coverage_hours"]
    if run.plan.synthetic is not None:
        out.insert(
            0,
            f"{SYNTHETIC_TAG}: the market data was generated ({_synthetic_text(run.plan.synthetic)}), "
            "not recorded from Wallex.",
        )
    if run.driver == "books":
        kind = "generated (synthetic)" if run.plan.synthetic is not None else "recorded"
        out.append(
            f"Book window = {cov:,.2f} h of {kind} data ({run.ticks:,} ticks of {run.plan.step_s:g} s with a "
            f"snapshot, {run.snapshots:,} snapshots) inside a requested window of {req_h:,.1f} h; time without "
            "snapshots is skipped, not simulated."
        )
        if "S1" in run.strategies:
            out.append(
                f"S1 cross-book coverage (USDTTMN and one coin's TMN and USDT books all fresh at once): "
                f"{meta.get('s1_coverage_hours', 0):,.2f} h."
            )
    else:
        out.append(
            f"Candle window = {cov:,.1f} h with 1-minute bars inside a requested window of {req_h:,.1f} h; a "
            f"minute without a candle inside a gap of at most {MAX_GAP_FILL_S / 60:g} min is a flat bar at the "
            "previous close (the engine's CandleCache rule)."
        )
    if run.driver == "books" and meta.get("orders_stopped_by_gaps"):
        out.append(
            f"{meta['orders_stopped_by_gaps']} order(s) were open across a recording gap (no snapshot of the symbol "
            f"for more than engine.stale_data_s = {run.stale_s:g} s): they were never filled on a snapshot after "
            "the gap (live, the OrderManager would have re-priced or canceled them within seconds); the engine "
            "then re-priced, crossed or gave them up on the post-gap books."
        )
    if run.plan.missing_symbols:
        out.append(f"No data in the window for: {', '.join(run.plan.missing_symbols)} (left out of the watchlist).")
    if run.warmup_candles:
        warm_h = ((run.plan.steps[0] if run.plan.steps else run.from_ts) - (run.warmup_from or run.from_ts)) / 3600
        out.append(
            f"Indicator warm-up: {run.warmup_candles:,} 1-minute candles ({warm_h:,.1f} h) from before the window were "
            "preloaded (the live engine loads up to 26 h at startup)."
        )
    elif "S2" in run.strategies or "S3" in run.strategies:
        out.append(
            "No candles before the window: S2/S3 indicators warm up inside it (EMA60 of 15-minute bars needs 15 h, "
            "S3's breakout 21 bars)."
        )
    if run.ai == "on":
        cap = (
            f"the cap was reached at {meta.get('ai_capped_at')}; afterwards the last plan went stale and the "
            "strategies ran rules-only"
            if meta.get("ai_capped_at")
            else "the cap was not reached"
        )
        out.append(
            f"ai=on: the Regime Analyst ran at simulated times (cadence engine.slow_loop_interval_s = "
            f"{cfg.engine.slow_loop_interval_s:g} s plus its event/move triggers), local tools only (MCP off), at "
            f"most {run.ai_max_calls} cycles per run ({meta.get('analyst_cycles')} run; {cap}). Every answer is cached "
            "in llm_cache/, so a rerun reproduces the plans without network calls."
        )
        if engine.ai_source == AI_OFFLINE:
            out.append("ai=on was requested but no LLM key is configured: plans came from the offline rules provider.")
    elif run.ai == "cache":
        out.append(
            f"ai=cache: every analyst answer came from the recorded LLM cache ({meta.get('llm_cache')}; routes "
            f"{', '.join(run.cache_routes)}). No API key was used and no provider was called (a cache miss would "
            f"have stopped the run), so the plans are the recorded model output, replayed; at most "
            f"{run.ai_max_calls} cycles per run ({meta.get('analyst_cycles')} run)."
        )
    elif run.ai == "offline":
        out.append(f"ai=offline: plans from the deterministic offline rules provider ({OFFLINE_STATUS}), NOT an LLM.")
    if run.ai != "off":
        out.append("The Post-trade Reviewer is not run in backtests: the config stays the same for the whole run.")
    if flatten:
        out.append(
            "The challenge end was set to the window end: the deadline flatten ran in the last "
            f"{cfg.engine.flatten_start_before_end_s / 3600:g} h (no new entries there)."
        )
    else:
        out.append("No end-of-window flatten: holdings at the end are valued at the bid (unrealized).")
    if engine.tick_crashes:
        out.append(f"{engine.tick_crashes} fast tick(s) crashed during the run (events.jsonl tick_error).")
    s3 = cfg.strategies.s3_momentum
    if "S3" in run.strategies and s3.allow_rules_only:
        out.append(
            "S3 ran with allow_rules_only=true: without a fresh plan its regime gate is the rules regime of each "
            "symbol (S2's rule_regime), not an AI call."
        )
    kills = [e for e in iter_family(run.out, "events") if e.get("kind") == "kill"]
    if kills:
        k = kills[0]
        out.append(
            f"The drawdown kill-switch fired at {k.get('ts')} ({k.get('reason')}): the engine flattened to TMN and "
            "only monitored afterwards (manual reset only), so the rest of the window has no new trading."
        )
    fee_vetoes = [r for r in decisions if is_veto(r) and "FEE_BUDGET" in failed_codes(r)]
    if fee_vetoes:
        out.append(
            f"The fee budget ({cfg.risk.fee_budget_pct:.1%} of initial equity, sized for the 72-h challenge) ran out: "
            f"{len(fee_vetoes)} entries were vetoed with FEE_BUDGET from {fee_vetoes[0].get('ts')} on."
        )
    if engine.ai_source == AI_LLM:
        out.append("LLM answers are model output: a different model or prompt version gives different plans.")
    return out


# ================================================================= synthetic market database


def synthetic_marker(path: str | Path) -> dict[str, Any] | None:
    """The kv marker of a database written by ``make_synthetic_market_db`` (None if absent)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        store = Store.open_readonly(p)
    except sqlite3.Error:
        return None
    try:
        marker = store.kv_get(SYNTHETIC_MARKER_KEY)
    except sqlite3.Error:
        marker = None
    finally:
        store.close()
    return marker if isinstance(marker, dict) else None


def make_synthetic_market_db(
    path: str | Path,
    hours: float,
    seed: int,
    step_s: float = 2,
    with_candles: bool = True,
    *,
    with_books: bool = True,
    symbols: Sequence[str] | None = None,
    start_ts: float | None = None,
    depth_levels: int = 5,
    volume_model: str = "clustered",
) -> dict[str, Any]:
    """Write an explicitly SYNTHETIC market DB (books every ``step_s``, 1-minute candles) and return
    its summary (``from_ts``, ``to_ts``, row counts, the marker).

    Uses ``wta.data.synthetic.SyntheticFeed`` seeded with ``seed`` around the reference mids and
    spreads of ``configs/markets_snapshot.yaml``; time starts at ``start_ts`` (default
    ``SYNTHETIC_START_TS``, fixed), so the same arguments always give the same rows. With books,
    candles come from the feed's own aggregation; ``with_books=False`` walks the same random
    processes and aggregates mids into candles itself (fast enough for 30 days at a 10-s step).
    An existing file with the same marker is reused; one with another marker is regenerated; a
    file WITHOUT the marker (e.g. ``data/wta.sqlite``) is never touched (``ValueError``).

    ``volume_model`` (candle-only mode): ``"uniform"`` draws each step's volume from U(0, 5), so a
    15-minute bar's volume stays within a few % of its 20-bar mean and S3's 1.5x volume filter can
    never pass; ``"clustered"`` (default) multiplies that draw by ``exp(x - sigma^2 / 2)``, where
    ``x`` is a per-symbol log-activity Ornstein-Uhlenbeck process (``SYNTHETIC_VOLUME_TAU_S``,
    ``SYNTHETIC_VOLUME_SIGMA``) on its own seeded RNG. Volume clusters as it does in real markets,
    but it carries NO information about the price walk (prices are unchanged): S3's volume filter
    is exercised, and it cannot add an edge. With books the feed's own volumes are used ("feed").
    """
    from wta.data.synthetic import SyntheticFeed, SyntheticParams

    if hours <= 0:
        raise ValueError("hours must be > 0")
    if not 0 < step_s <= 60:
        raise ValueError("step_s must be in (0, 60]")
    if not (with_books or with_candles):
        raise ValueError("nothing to generate: with_books and with_candles are both off")
    if volume_model not in SYNTHETIC_VOLUME_MODELS:
        raise ValueError(f"unknown volume_model {volume_model!r} ({' | '.join(SYNTHETIC_VOLUME_MODELS)})")
    p = Path(path)
    markets = load_markets_snapshot()
    syms = sorted(s for s in (symbols or SYNTHETIC_SYMBOLS) if s in markets)
    start = SYNTHETIC_START_TS if start_ts is None else float(start_ts)
    params: dict[str, Any] = {
        "generator": SYNTHETIC_GENERATOR,
        "version": SYNTHETIC_VERSION,
        "seed": int(seed),
        "hours": float(hours),
        "step_s": float(step_s),
        "start_ts": start,
        "symbols": syms,
        "with_books": bool(with_books),
        "with_candles": bool(with_candles),
        "depth_levels": int(depth_levels),
        "volume_model": "feed" if with_books else volume_model,
    }
    end = start + float(hours) * 3600.0
    if p.exists():
        marker = synthetic_marker(p)
        if marker is None:
            raise ValueError(f"refusing to overwrite {p}: it is not a synthetic market database (no marker)")
        if marker.get("complete") and {k: marker.get(k) for k in params} == params:
            return {**marker, "path": str(p), "reused": True}
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(p) + suffix)
            if q.exists():
                q.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    store = Store(p, flush_interval_s=3600, flush_rows=5000, depth_levels=depth_levels)
    marker = {**params, "synthetic": True, "complete": False, "note": SYNTHETIC_NOTE, "label": SYNTHETIC_TAG,
              "from_ts": start, "to_ts": end, "from": iso(start), "to": iso(end)}
    store.kv_set(SYNTHETIC_MARKER_KEY, marker, start)
    store.flush()
    ref = snapshot_reference()
    bus = MarketDataBus()
    pending: dict[tuple[str, int], Candle] = {}
    counts = {"books": 0, "candles": 0}

    def on_book(book: BookSnapshot) -> None:
        store.add_book(book)
        counts["books"] += 1

    def on_candle(candles: list[Candle]) -> None:
        for c in candles:
            pending[(c.symbol, int(c.ts))] = c

    def flush_closed(t: float, final: bool = False) -> None:
        done = sorted(k for k in pending if k[1] + BAR_S <= t or (final and k[1] + BAR_S <= end))
        if done:
            counts["candles"] += store.upsert_candles([pending.pop(k) for k in done])
        if final:
            pending.clear()

    if with_books:
        bus.on_book(on_book)
    feed = SyntheticFeed(
        markets=markets,
        symbols=syms,
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=SimClock(start),
        params=SyntheticParams(seed=int(seed), step_s=float(step_s)),
        on_candle=on_candle if (with_candles and with_books) else None,
    )
    n = int(round(float(hours) * 3600.0 / float(step_s)))
    vol_rng = random.Random(int(seed) * 7919 + 1)
    act_rng = random.Random(int(seed) * 104729 + 2)  # clustered volume: its own stream (prices untouched)
    act_phi = math.exp(-float(step_s) / SYNTHETIC_VOLUME_TAU_S)
    act_k = SYNTHETIC_VOLUME_SIGMA * math.sqrt(1.0 - act_phi * act_phi)
    act_bias = SYNTHETIC_VOLUME_SIGMA * SYNTHETIC_VOLUME_SIGMA / 2.0
    activity = {sym: SYNTHETIC_VOLUME_SIGMA * act_rng.gauss(0.0, 1.0) for sym in syms}
    clustered = volume_model == "clustered"
    acc: dict[str, list[Any]] = {}  # candle-only mode: symbol -> [ts, o, h, l, c, v]
    for i in range(n + 1):
        t = start + i * float(step_s)
        if i:
            feed.step(float(step_s))
        if with_books:
            feed.publish(t)
        else:
            for sym in syms:
                mid = feed.mid(sym)
                if mid is None:
                    continue
                px = quantize_down(D(mid), markets[sym].tick_decimals)
                bar_ts = int(t // BAR_S * BAR_S)
                draw = vol_rng.random() * 5
                if clustered:
                    if i:
                        activity[sym] = act_phi * activity[sym] + act_k * act_rng.gauss(0.0, 1.0)
                    draw *= math.exp(activity[sym] - act_bias)
                v = D(round(draw, 4))
                cur = acc.get(sym)
                if cur is None or cur[0] != bar_ts:
                    if cur is not None:
                        pending[(sym, cur[0])] = Candle(symbol=sym, resolution="1", ts=cur[0], o=cur[1], h=cur[2],
                                                        l=cur[3], c=cur[4], v=cur[5])
                    acc[sym] = [bar_ts, px, px, px, px, v]
                else:
                    cur[2], cur[3], cur[4], cur[5] = max(cur[2], px), min(cur[3], px), px, cur[5] + v
        if with_candles and pending and i % 30 == 0:
            flush_closed(t)
    if not with_books:
        for sym, cur in sorted(acc.items()):
            pending[(sym, cur[0])] = Candle(symbol=sym, resolution="1", ts=cur[0], o=cur[1], h=cur[2], l=cur[3],
                                            c=cur[4], v=cur[5])
    if with_candles:
        flush_closed(end + 1e-6, final=True)
    marker = {**marker, "complete": True, "books": counts["books"], "candles": counts["candles"]}
    store.kv_set(SYNTHETIC_MARKER_KEY, marker, end)
    store.flush()
    store.close()
    log.info("synthetic market DB %s: %d books, %d candles, %s -> %s (%s)", p, counts["books"], counts["candles"],
             iso(start), iso(end), SYNTHETIC_TAG)
    return {**marker, "path": str(p), "reused": False}


# ================================================================= shadow / live reports (``wta report``)


SIGNAL_KINDS = frozenset({"order", "veto", "no_trade", "intent"})
DIVERGENCE_TOLERANCE_S = 30.0
DIVERGENCE_ROWS = 25
AI_SOURCE_TO_MODE = {"llm": "on", "offline-rules": "offline", "disabled": "off"}


@dataclass
class LogRun:
    """One run's logs as ``report_from_logs`` reads them."""

    log_dir: Path
    raw: list[dict[str, Any]]
    merged: list[dict[str, Any]]
    events: list[dict[str, Any]]
    health: list[dict[str, Any]]
    start: dict[str, Any]
    window: tuple[float | None, float | None]


def _rec_ts(rec: Mapping[str, Any]) -> float | None:
    value = rec.get("ts")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


def load_log_run(log_dir: str | Path, from_ts: float | None = None, to_ts: float | None = None) -> LogRun:
    """Decisions (merged), events and heartbeats of a run's log dir, cut to ``[from_ts, to_ts]``."""
    d = Path(log_dir)
    raw = list(iter_family(d, "decisions"))
    events = list(iter_family(d, "events"))
    health = list(iter_family(d, "health"))
    if not (raw or events or health):
        raise ValueError(f"no log records in {d} (decisions/events/health)")

    def inside(rec: Mapping[str, Any]) -> bool:
        t = _rec_ts(rec)
        return t is None or ((from_ts is None or t >= from_ts) and (to_ts is None or t <= to_ts))

    events = [e for e in events if inside(e)]
    health = [h for h in health if inside(h)]
    merged = [m for m in (dict(v) for v in merge_decisions(raw).values()) if inside(m)]
    starts = [e for e in events if e.get("kind") == "start"]
    start = dict(starts[-1].get("data") or {}) if starts else {}
    start["runs"] = len(starts)
    times = [t for t in (_rec_ts(r) for r in [*merged, *events, *health]) if t is not None]
    lo = from_ts if from_ts is not None else (min(times) if times else None)
    hi = to_ts if to_ts is not None else (max(times) if times else None)
    return LogRun(d, raw, merged, events, health, start, (lo, hi))


def _state_db_for(run: LogRun, given: str | Path | None) -> Path | None:
    if given is not None:
        p = Path(given)
        if not p.is_file():
            raise FileNotFoundError(f"state database {p} does not exist")
        return p
    named = run.start.get("state_db")
    if named and named != ":memory:" and Path(str(named)).is_file():
        return Path(str(named))
    candidate = run.log_dir / STATE_DB
    return candidate if candidate.is_file() else None


def _equity_of(run: LogRun, state_db: Path | None) -> tuple[list[dict[str, Any]], str]:
    lo, hi = run.window
    if state_db is not None:
        rows = read_equity_table(state_db, lo, hi)
        if rows:
            return rows, "state_db"
    rows = [
        {"ts": _rec_ts(h), "equity_tmn": h.get("equity_tmn")}
        for h in run.health
        if h.get("equity_tmn") is not None and _rec_ts(h) is not None
    ]
    return rows, "heartbeats"


def _intent_key(rec: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    intent = rec.get("intent")
    if rec.get("kind") not in SIGNAL_KINDS or not isinstance(intent, Mapping):
        return None
    return (str(rec.get("strategy") or intent.get("strategy") or ""), str(rec.get("symbol") or intent.get("symbol") or ""),
            str(intent.get("side") or ""), str(intent.get("purpose") or ""))


def _fill_of(rec: Mapping[str, Any]) -> dict[str, Any]:
    out = rec.get("outcome") if isinstance(rec.get("outcome"), Mapping) else {}
    order = rec.get("order") if isinstance(rec.get("order"), Mapping) else {}
    return {
        "kind": rec.get("kind"),
        "accepted": order.get("accepted") if order else None,
        "status": out.get("status"),
        "avg_price": out.get("avg_price") or out.get("price"),
        "filled_qty": out.get("filled_qty") or out.get("qty"),
    }


def divergence(
    a: Sequence[Mapping[str, Any]],
    b: Sequence[Mapping[str, Any]],
    *,
    tolerance_s: float = DIVERGENCE_TOLERANCE_S,
    window: tuple[float | None, float | None] = (None, None),
) -> dict[str, Any]:
    """Paper-vs-live divergence (PRD-05 §5): intents of run ``a`` (shadow) and ``b`` (live) matched
    by (strategy, symbol, side, purpose) within ``tolerance_s``, in the overlapping ``window``.

    Returns counts (matched, only in a, only in b), how often the outcome differs (kind: order vs
    veto; fill status; fill price in bps of a's price) and the first ``DIVERGENCE_ROWS`` rows.
    """
    lo, hi = window

    def signals(recs: Sequence[Mapping[str, Any]]) -> list[tuple[float, tuple[str, str, str, str], Mapping[str, Any]]]:
        out = []
        for r in recs:
            key, t = _intent_key(r), _rec_ts(r)
            if key is None or t is None or (lo is not None and t < lo) or (hi is not None and t > hi):
                continue
            out.append((t, key, r))
        out.sort(key=lambda x: (x[0], x[1], str(x[2].get("decision_id"))))
        return out

    sa, sb = signals(a), signals(b)
    used: set[int] = set()
    rows: list[dict[str, Any]] = []
    matched = kind_diff = status_diff = 0
    price_bps: list[float] = []
    only_a: list[dict[str, Any]] = []
    for t, key, rec in sa:
        best, best_dt = None, None
        for j, (tb, kb, _) in enumerate(sb):
            if j in used or kb != key:
                continue
            dt = abs(tb - t)
            if dt <= tolerance_s and (best_dt is None or dt < best_dt):
                best, best_dt = j, dt
        if best is None:
            only_a.append({"ts": iso(t), "key": "/".join(key), "kind": rec.get("kind")})
            continue
        used.add(best)
        matched += 1
        fa, fb = _fill_of(rec), _fill_of(sb[best][2])
        bps = None
        try:
            if fa["avg_price"] and fb["avg_price"]:
                bps = (float(fb["avg_price"]) / float(fa["avg_price"]) - 1.0) * 1e4
                price_bps.append(bps)
        except (TypeError, ValueError, ZeroDivisionError):
            bps = None
        kind_diff += int(fa["kind"] != fb["kind"])
        status_diff += int(fa["status"] != fb["status"])
        if len(rows) < DIVERGENCE_ROWS:
            rows.append({"ts": iso(t), "dt_s": round(best_dt or 0.0, 3), "key": "/".join(key), "a": fa, "b": fb,
                         "price_diff_bps": None if bps is None else round(bps, 2)})
    only_b = [{"ts": iso(tb), "key": "/".join(kb), "kind": rb.get("kind")}
              for j, (tb, kb, rb) in enumerate(sb) if j not in used]
    return {
        "window": [iso(lo) if lo is not None else None, iso(hi) if hi is not None else None],
        "tolerance_s": tolerance_s,
        "signals_a": len(sa),
        "signals_b": len(sb),
        "matched": matched,
        "only_a": len(only_a),
        "only_b": len(only_b),
        "kind_differs": kind_diff,
        "fill_status_differs": status_diff,
        "mean_abs_price_diff_bps": round(sum(abs(x) for x in price_bps) / len(price_bps), 3) if price_bps else None,
        "rows": rows,
        "only_a_rows": only_a[:DIVERGENCE_ROWS],
        "only_b_rows": only_b[:DIVERGENCE_ROWS],
    }


def divergence_markdown(div: Mapping[str, Any], label_a: str, label_b: str) -> list[str]:
    w = div.get("window") or [None, None]
    lines = [
        "## Paper vs live divergence",
        "",
        f"Intents of **{label_a}** (a) and **{label_b}** (b) matched by strategy/symbol/side/purpose within "
        f"{div.get('tolerance_s'):g} s, over the overlapping window {w[0]} → {w[1]}.",
        "",
        "| | count |",
        "|---|---:|",
        f"| signals in a | {div.get('signals_a')} |",
        f"| signals in b | {div.get('signals_b')} |",
        f"| matched | {div.get('matched')} |",
        f"| only in a | {div.get('only_a')} |",
        f"| only in b | {div.get('only_b')} |",
        f"| matched, but order vs veto differs | {div.get('kind_differs')} |",
        f"| matched, fill status differs | {div.get('fill_status_differs')} |",
        f"| mean abs fill price difference (bps) | {div.get('mean_abs_price_diff_bps')} |",
        "",
    ]
    rows = div.get("rows") or []
    if rows:
        lines += ["| time (UTC) | dt s | intent | a | b | price diff bps |", "|---|---:|---|---|---|---:|"]
        for r in rows:
            fa, fb = r["a"], r["b"]
            lines.append(
                f"| {r['ts']} | {r['dt_s']} | {r['key']} | {fa['kind']}/{fa['status']} @ {fa['avg_price']} | "
                f"{fb['kind']}/{fb['status']} @ {fb['avg_price']} | {r['price_diff_bps']} |"
            )
        lines.append("")
    if not div.get("signals_a") and not div.get("signals_b"):
        lines += ["_No intents in the overlapping window: nothing to compare._", ""]
    return lines


def insert_section(report_md: Path, lines: Sequence[str], before: str = "## Limitations") -> None:
    """Insert a markdown section into ``report.md`` before the ``before`` heading (else at the end)."""
    text = report_md.read_text(encoding="utf-8")
    block = "\n".join(lines).rstrip("\n") + "\n\n"
    idx = text.find("\n" + before + "\n")
    text = text + "\n" + block if idx < 0 else text[: idx + 1] + block + text[idx + 1 :]
    report_md.write_text(text, encoding="utf-8")


def update_summary(summary_json: Path, key: str, value: Any) -> None:
    data = json.loads(summary_json.read_text(encoding="utf-8")) if summary_json.is_file() else {}
    data[key] = value
    summary_json.write_text(json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n",
                            encoding="utf-8")


def _per_process(health: Sequence[Mapping[str, Any]], get: Any, restart_key: str) -> list[dict[str, Any]]:
    """The last value ``get(heartbeat)`` of every engine process in ``health`` (counters restart with
    every process: ``restart_key`` going down marks a new one); None values are skipped."""
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for hb in health:
        value = get(hb)
        if not isinstance(value, Mapping):
            continue
        cur = dict(value)
        if prev is not None and int(cur.get(restart_key) or 0) < int(prev.get(restart_key) or 0):
            out.append(prev)
        prev = cur
    if prev is not None:
        out.append(prev)
    return out


def s1_opportunity_ticks(health: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per S1 coin: fast ticks evaluated, opportunity ticks (lock-in edge >= cost + margin in force on
    either side), the best excess (edge - cost - margin) and the last opportunity time, from the
    engine's own counters in the heartbeats (``extra.strategies.S1.coins.<coin>.stats``), summed over
    engine processes; plus the last ``why_no_trade``."""
    def coins(hb: Mapping[str, Any]) -> Mapping[str, Any]:
        return (((hb.get("extra") or {}).get("strategies") or {}).get("S1") or {}).get("coins") or {}

    names = sorted({c for hb in health for c in coins(hb)})
    out: dict[str, dict[str, Any]] = {}
    for coin in names:
        runs = _per_process(health, lambda hb, c=coin: (coins(hb).get(c) or {}).get("stats"), "evaluated")
        if not runs:
            continue
        bests = [D(r["best_excess_pct"]) for r in runs if r.get("best_excess_pct") is not None]
        lasts = [float(r["last_opportunity_ts"]) for r in runs if r.get("last_opportunity_ts") is not None]
        why = next((str((coins(hb).get(coin) or {}).get("why_no_trade")) for hb in reversed(health)
                    if (coins(hb).get(coin) or {}).get("why_no_trade")), None)
        out[coin] = {
            "evaluated": sum(int(r.get("evaluated") or 0) for r in runs),
            "opportunity_ticks": sum(int(r.get("opportunity_ticks") or 0) for r in runs),
            "best_excess_pct": str(max(bests)) if bests else None,
            "last_opportunity": iso(max(lasts)) if lasts else None,
            "why_no_trade": why,
        }
    return out


def s1_opportunity_markdown(totals: Mapping[str, Mapping[str, Any]],
                            merged: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["## S1 opportunity ticks", ""]
    if not totals:
        return [*lines, "_No S1 counters in the heartbeats (S1 not run, or no heartbeat)._", ""]
    lines += [
        "An opportunity tick is a fast tick on which a coin's lock-in edge reached cost + the margin in force "
        "(the plan's `s1_margin_min_pct`, else the config's) on either side, TMN-cheap or USDT-cheap. These are "
        "the engine's own counters, read from the heartbeats; best excess = the largest edge − cost − margin seen "
        "(negative: no tick reached the threshold).",
        "",
        "| coin | ticks evaluated | opportunity ticks | best excess | last opportunity (UTC) | last why-no-trade |",
        "|---|---:|---:|---:|---|---|",
    ]
    for coin, t in sorted(totals.items()):
        best = f"{float(t['best_excess_pct']) * 100:+.3f}%" if t.get("best_excess_pct") is not None else "n/a"
        why = str(t.get("why_no_trade") or "-").split("; ")[0]  # the lock-in clause (the revert one follows)
        why = (why if len(why) <= 160 else why[:159] + "…").replace("|", "\\|")
        lines.append(f"| {coin} | {t['evaluated']:,} | {t['opportunity_ticks']:,} | {best} | "
                     f"{t.get('last_opportunity') or '-'} | {why} |")
    total = sum(int(t["opportunity_ticks"]) for t in totals.values())
    evaluated = sum(int(t["evaluated"]) for t in totals.values())
    entries = [r for r in merged if r.get("strategy") == "S1" and (r.get("intent") or {}).get("purpose") == "entry"]
    placed = [r for r in entries if r.get("kind") == "order" and (r.get("order") or {}).get("accepted")]
    plans = [r for r in merged if r.get("kind") == "plan" and isinstance(r.get("plan_snapshot"), Mapping)]
    off = sum(1 for r in plans if "S1" not in (r["plan_snapshot"].get("enabled_strategies") or []))
    lines += ["", f"Total: {total:,} opportunity ticks in {evaluated:,} coin-ticks evaluated. S1 entries: "
              f"{len(entries)} decided, {len(placed)} placed."
              + (f" The analyst left S1 out of `enabled_strategies` in {off} of {len(plans)} plans (no new S1 "
                 "entries while such a plan is fresh)." if plans else ""), ""]
    return lines


def ai_availability(run: LogRun) -> dict[str, Any] | None:
    """The analyst's cycles, failures and plan freshness over a run, from the heartbeats' AI stats
    (summed over engine processes) and the ``ai_degraded`` / ``ai_recovered`` / ``plan_stale`` events.
    A rules-only stretch runs from a ``plan_stale`` event to the next plan (or the window's end)."""
    stats = _per_process(run.health, lambda hb: (((hb.get("extra") or {}).get("ai") or {}).get("stats")), "cycles")
    events = [e for e in run.events if e.get("kind") in ("ai_degraded", "ai_recovered", "plan_stale")]
    if not stats and not events:
        return None
    totals = {k: sum(int(s.get(k) or 0) for s in stats)
              for k in ("cycles", "plans", "failures", "model_calls", "cached_calls", "reviews", "tokens")}
    if not totals["cycles"] and not events:
        return None  # AI off (or it never ran a cycle)
    streaks = []
    for e in events:
        if e.get("kind") != "ai_degraded":
            continue
        reason = str(e.get("reason") or "")
        cause = ("llm_invalid: an invalid answer" if "llm_invalid" in reason
                 else "llm unavailable: provider unreachable" if "llm unavailable" in reason else "other failure")
        streaks.append({"ts": str(e.get("ts")), "cause": cause})
    plan_ts = sorted(t for t in (_rec_ts(r) for r in run.merged if r.get("kind") == "plan") if t is not None)
    end = run.window[1]
    stretches = []
    for e in events:
        t = _rec_ts(e)
        if e.get("kind") != "plan_stale" or t is None:
            continue
        nxt = next((p for p in plan_ts if p > t), None)
        stop = nxt if nxt is not None else end
        stretches.append({"from": iso(t), "to": iso(stop) if stop is not None else None,
                          "until": "the next plan" if nxt is not None else "the end of the window",
                          "hours": round((stop - t) / 3600.0, 3) if stop is not None else None})
    return {**totals, "failure_streaks": streaks,
            "recovered": [str(e.get("ts")) for e in events if e.get("kind") == "ai_recovered"],
            "rules_only_stretches": stretches}


def ai_availability_text(av: Mapping[str, Any]) -> str | None:
    """One limitation line when the analyst failed or a plan went stale (None: neither happened)."""
    if not av.get("failures") and not av.get("rules_only_stretches"):
        return None
    text = f"The LLM analyst gave no plan in {av['failures']} of {av['cycles']} cycles"
    if av.get("failure_streaks"):
        text += " (" + "; ".join(f"{s['cause']} from {s['ts']}" for s in av["failure_streaks"]) + ")"
    for s in av.get("rules_only_stretches") or []:
        text += (f"; the plan went stale at {s['from']} and the strategies ran rules-only until {s['until']} "
                 f"({s['to']}, {s['hours']:.2f} h)")
    return text + "."


def ai_availability_markdown(av: Mapping[str, Any]) -> list[str]:
    rows = [("analyst cycles", av.get("cycles")), ("plans", av.get("plans")),
            ("cycles without a plan", av.get("failures")), ("model calls", av.get("model_calls")),
            ("model calls served from the cache", av.get("cached_calls")), ("tokens", av.get("tokens")),
            ("post-trade reviews", av.get("reviews"))]
    lines = ["### Analyst availability", "", "| | value |", "|---|---:|"]
    lines += [f"| {k} | {int(v or 0):,} |" for k, v in rows]
    lines.append("")
    for s in av.get("failure_streaks") or []:
        lines.append(f"- failure streak from {s['ts']}: {s['cause']}")
    for t in av.get("recovered") or []:
        lines.append(f"- recovered (new plan) at {t}")
    for s in av.get("rules_only_stretches") or []:
        lines.append(f"- plan stale at {s['from']}: rules-only until {s['until']} ({s['to']}, {s['hours']:.2f} h)")
    lines += ["", "_From the heartbeats' AI counters (last value of each engine process) and the run's "
              "`ai_degraded` / `ai_recovered` / `plan_stale` events. A failure streak is logged once, at its "
              "first failed cycle._", ""]
    return lines


@dataclass
class LogReport:
    report: Path
    metrics: Metrics
    meta: dict[str, Any]
    divergence: dict[str, Any] | None = None


def report_from_logs(
    *,
    source: str | Path,
    out_dir: str | Path,
    state_db: str | Path | None = None,
    compare: str | Path | None = None,
    compare_state_db: str | Path | None = None,
    title: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    report_date: str | None = None,
) -> LogReport:
    """``wta report``: the report of a shadow/live (or backtest) run from its log dir (+ state DB),
    via ``backtest.metrics`` + ``backtest.report``; with ``compare`` (the other run's log dir, e.g.
    live next to shadow) a paper-vs-live divergence section. Reads only files."""
    run = load_log_run(source, from_ts, to_ts)
    db = _state_db_for(run, state_db)
    equity, eq_source = _equity_of(run, db)
    trades = add_trade_fees(trades_from_decisions(run.merged), db)
    start = run.start
    feed = str(start.get("feed") or "")
    synthetic = bool(start.get("synthetic")) or feed == "synthetic" or any(
        e.get("kind") == "start" and (e.get("data") or {}).get("feed") == "synthetic" for e in run.events
    )
    mode = str(start.get("mode") or (run.merged[0].get("mode") if run.merged else "") or "n/a")
    ai_source = str((start.get("ai") or {}).get("source") or "") if isinstance(start.get("ai"), Mapping) else ""
    lo, hi = run.window
    hashes = sorted({str(r.get("config_hash")) for r in run.merged if r.get("config_hash")})
    meta: dict[str, Any] = {
        "mode": mode,
        "feed": feed or "n/a",
        "driver": "n/a",
        "window_from": iso(lo) if lo is not None else None,
        "window_to": iso(hi) if hi is not None else None,
        "data_source": (
            f"{SYNTHETIC_TAG}: the offline synthetic feed (paper fills)" if synthetic
            else ("Wallex live market data, paper fills" if mode == "shadow" else f"{mode} run logs")
        ),
        "synthetic": synthetic,
        "ai": AI_SOURCE_TO_MODE.get(ai_source, ai_source or None),
        "ai_source": ai_source or None,
        "strategies": list(start.get("strategies") or []),
        "config_hash": start.get("config_hash") or (hashes[0] if hashes else None),
        "git_commit": start.get("git_commit") or (run.merged[0].get("git_commit") if run.merged else None),
        "log_dir": display_path(run.log_dir),
        "state_db": display_path(db) if db is not None else None,
        "equity_source": eq_source,
        "engine_starts": start.get("runs", 0),
        "data_end": iso(hi) if hi is not None else None,
        **report_date_of(report_date),
    }
    limits: list[str] = []
    if eq_source == "heartbeats":
        limits.append("Equity curve from the heartbeats (every 60 s), not the engine's 10-s equity table.")
    if start.get("runs", 0) > 1:
        limits.append(f"{start['runs']} engine starts in the window (restarts): the run is not one continuous process.")
    if len(hashes) > 1:
        limits.append(f"The config changed during the run (config hashes {', '.join(hashes)}).")
    if ai_source == "offline-rules":
        limits.append("Plans came from the offline rules provider, NOT an LLM.")
    availability = ai_availability(run)
    if availability is not None and ai_availability_text(availability):
        limits.append(str(ai_availability_text(availability)))
    whole = from_ts is None and to_ts is None
    # the engine's fee total covers the whole run, so it is used only for an uncut window
    fees_total = state_fees_tmn(db) if whole else None
    if not whole:
        limits.append("Window cut with --from/--to: fees are the closed trades' fees only (the engine's fee "
                      "total covers the whole run).")
    metrics = compute_metrics(equity, trades, decisions=run.raw if whole else run.merged,
                              fees_paid_tmn=fees_total, label=f"{mode} {run.log_dir.name}")
    out = Path(out_dir)
    heading = title or f"{mode.capitalize()} run report: {run.log_dir.name}" + (" — SYNTHETIC" if synthetic else "")
    report = write_report(out, heading, meta, metrics, equity, trades, decisions=run.merged, plans=None,
                          limitations=limits)
    if "S1" in (meta["strategies"] or []):
        s1 = s1_opportunity_ticks(run.health)
        insert_section(report, s1_opportunity_markdown(s1, run.merged), before="## AI (Regime Analyst)")
        update_summary(out / SUMMARY_JSON, "s1_opportunities", s1)
    if availability is not None:
        insert_section(report, ai_availability_markdown(availability), before="### Ablation (ai=on vs ai=off)")
        update_summary(out / SUMMARY_JSON, "ai_availability", availability)
    div: dict[str, Any] | None = None
    if compare is not None:
        other = load_log_run(compare, from_ts, to_ts)
        a_lo, a_hi = run.window
        b_lo, b_hi = other.window
        overlap = (max(x for x in (a_lo, b_lo) if x is not None) if a_lo is not None and b_lo is not None else None,
                   min(x for x in (a_hi, b_hi) if x is not None) if a_hi is not None and b_hi is not None else None)
        div = divergence(run.merged, other.merged, window=overlap)
        div["a"], div["b"] = str(run.log_dir), str(other.log_dir)
        if compare_state_db is not None:
            div["b_state_db"] = str(compare_state_db)
        insert_section(report, divergence_markdown(div, f"{run.log_dir} ({mode})",
                                                   f"{other.log_dir} ({other.start.get('mode') or 'n/a'})"))
        (out / "divergence.json").write_text(json.dumps(div, sort_keys=True, indent=2, default=str) + "\n",
                                             encoding="utf-8")
        update_summary(out / SUMMARY_JSON, "divergence", div)
    return LogReport(report=report, metrics=metrics, meta=meta, divergence=div)


# ================================================================= export (``wta export``)


EXPORT_TABLES = ("books", "candles", "trades")


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def export_tables(db_path: str | Path, out_dir: str | Path, *, tables: Sequence[str] = EXPORT_TABLES,
                  fmt: str | None = None) -> dict[str, Any]:
    """Market tables of ``db_path`` to ``out_dir/<table>.parquet`` (CSV when pyarrow is missing:
    ``pandas.to_parquet`` needs it). Writes ``manifest.json`` and returns it."""
    import pandas as pd

    src = Path(db_path)
    if not src.is_file():
        raise FileNotFoundError(f"database {src} does not exist")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    chosen = fmt or ("parquet" if _has_pyarrow() else "csv")
    note = None if chosen == "parquet" else "pyarrow is not installed: wrote CSV instead of Parquet (pip install pyarrow)"
    conn = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    files: dict[str, Any] = {}
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table not in have:
                files[table] = {"rows": 0, "file": None, "note": "table not in the database"}
                continue
            frame = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY rowid", conn)
            target = out / f"{table}.{chosen}"
            if chosen == "parquet":
                frame.to_parquet(target, index=False)
            else:
                frame.to_csv(target, index=False)
            files[table] = {"rows": int(len(frame)), "file": target.name}
    finally:
        conn.close()
    manifest = {"source": str(src), "format": chosen, "note": note, "tables": files}
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return manifest


__all__ = [
    "AI_MODES",
    "DRIVERS",
    "BacktestResult",
    "MarketDB",
    "StepPlan",
    "add_trade_fees",
    "arun_backtest",
    "backtest_config",
    "book_steps",
    "build_backtest_engine",
    "candle_steps",
    "display_path",
    "divergence",
    "export_tables",
    "make_synthetic_market_db",
    "needed_symbols",
    "parse_strategies",
    "parse_ts",
    "plan_steps",
    "report_from_logs",
    "run_backtest",
    "state_fees_tmn",
    "synthetic_marker",
]
