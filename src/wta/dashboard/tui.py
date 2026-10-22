"""Terminal dashboard (PRD-06 §3): ``python -m wta tui`` — a separate, read-only process.

Every refresh builds a ``DashboardSnapshot`` (``build_snapshot``) from

* the log dir: the newest heartbeat (``health-*.jsonl`` via ``health_log.latest``), the tail of
  ``decisions-*.jsonl`` (merged with their outcome updates) and of ``events-*.jsonl``;
* the engine's SQLite store opened with ``Store.open_readonly``: the latest equity row, the
  engine flags (kv ``engine_flags``, fresher than the heartbeat), open positions and orders, the
  latest book of every watchlist symbol and the latest stored plan;

and draws it with ``rich`` (``render``): header, equity, markets (+ S1 cross-book edges vs cost),
health, current plan, positions & open orders, last 10 decisions. ``run`` refreshes it in a
``rich.live.Live`` screen (or prints one frame with ``once=True``).

Nothing here writes a file or a row, and nothing can place or cancel an order. Every source is
optional: a missing log dir, DB, table, file or field shows as "—" plus a note in the health
panel, never as an exception. Data from the offline synthetic feed is marked with a banner on
every frame: it exercises the engine and is never evidence of performance.

Layout fits a 200 x 55 terminal (1080p screen recording, PRD-06 §5).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import orjson
from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from wta.config import Config
from wta.data.store import Store
from wta.execution.reconcile import KV_FLAGS
from wta.logging_.decision_log import merge_decisions
from wta.logging_.health_log import latest as latest_health
from wta.logging_.jsonl import family_files
from wta.models import BookSnapshot, Order, Position
from wta.utils.timeutil import parse_iso

OFFLINE_PLAN_MODEL = "offline-rules-v1"  # the Analyst's rules fallback when no LLM key is set
SYNTHETIC_BANNER = "SYNTHETIC FEED — not real market data"
SYNTHETIC_DETAIL = "an offline exercise of the engine; nothing on this screen is evidence of performance"
HEARTBEAT_STALE_S = 180.0  # the watchdog restarts the agent after 3 min without a heartbeat
DECISIONS_SHOWN = 10
MAX_POSITIONS_SHOWN = 4  # risk allows 3 concurrent positions; one spare row before "+N more"
MAX_ORDERS_SHOWN = 4  # newest open orders shown (the cap is 8); keeps the frame within 55 lines
MAX_REASONS_SHOWN = 3  # heartbeat reasons in the health panel
MAX_NOTES_CHARS = 600  # plan notes, wrapped (about 7 lines in the plan panel)
TRADE_KINDS = frozenset({"intent", "veto", "order"})  # plan/no_trade/event records are not trades
NOTABLE_EVENTS = frozenset(
    {
        "start", "stop", "restart", "pause", "resume", "kill", "reset_kill", "reset_kill_applied",
        "flatten", "final_state", "tick_error", "day_start", "config_change",
    }
)
TAIL_MAX_LINES = 20_000  # never read more than this many lines from the end of a log family
ENGINE_BOOK_WINDOW_S = 300.0  # engine_books fallback: only books stored in the last 5 min
EST_TAKER_FEE = {"TMN": Decimal("0.003"), "USDT": Decimal("0.0013")}  # Overview §2 (S1 estimate only)
PHASE_NAMES = {0: "none", 1: "1 · maker limits (T-2h)", 2: "2 · market (T-30m)", 3: "3 · challenge over"}
DASH = "—"


# ---------------------------------------------------------------- settings & snapshot


@dataclass(frozen=True)
class DashboardSettings:
    """What the logs do not carry: limits for colouring and the defaults of ``configs/*.yaml``.

    ``from_config`` takes them from the loaded config; values in the heartbeat (e.g. an
    ``initial_equity_tmn`` in ``extra``) win over these when present.
    """

    initial_equity_tmn: Decimal = Decimal("5000000")
    fee_budget_pct: Decimal = Decimal("0.02")
    max_drawdown_kill_pct: Decimal = Decimal("0.05")
    daily_loss_limit_pct: Decimal = Decimal("0.025")
    max_coin_exposure_pct: Decimal = Decimal("0.40")
    max_orders_per_day: int = 60
    s1_margin_min_pct: Decimal = Decimal("0.0015")
    challenge_end_utc: str | None = None
    watchlist: tuple[str, ...] = ()
    s1_coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "DOGE")

    @classmethod
    def from_config(cls, cfg: Config) -> DashboardSettings:
        s1 = cfg.strategies.s1_crossbook
        watch = tuple(s for s in dict.fromkeys(cfg.markets.watchlist) if not cfg.markets.is_banned(s))
        return cls(
            initial_equity_tmn=cfg.engine.initial_equity_tmn,
            fee_budget_pct=cfg.risk.fee_budget_pct,
            max_drawdown_kill_pct=cfg.risk.max_drawdown_kill_pct,
            daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
            max_coin_exposure_pct=cfg.risk.max_coin_exposure_pct,
            max_orders_per_day=cfg.risk.max_orders_per_day,
            s1_margin_min_pct=s1.margin_min_pct,
            challenge_end_utc=cfg.engine.challenge_end_utc or None,
            watchlist=watch,
            s1_coins=tuple(s1.coins),
        )


@dataclass
class DashboardSnapshot:
    """Everything one frame shows, read once; ``render`` never touches a file."""

    log_dir: str
    state_db: str
    now: float
    settings: DashboardSettings = field(default_factory=DashboardSettings)
    health: dict[str, Any] | None = None  # newest HealthRecord line
    health_ts: float | None = None
    flags: dict[str, Any] | None = None  # kv engine_flags (paused/killed/flatten_phase), from the DB
    start_event: dict[str, Any] | None = None  # newest "start" event line
    lifecycle_event: dict[str, Any] | None = None  # newest "start" or "stop" event line
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    restarts: int = 0
    equity: dict[str, Any] | None = None  # Store.latest_equity()
    watchlist: list[str] = field(default_factory=list)
    books: dict[str, BookSnapshot] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    open_orders: list[Order] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    plan_ts: float | None = None
    plan_source: str = ""  # "db" or "decisions"
    decisions: list[dict[str, Any]] = field(default_factory=list)  # merged, oldest first
    db_found: bool = False
    problems: list[str] = field(default_factory=list)  # sources that could not be read

    # ------------------------------------------------------------ derived fields

    @property
    def extra(self) -> dict[str, Any]:
        return _dict((self.health or {}).get("extra"))

    @property
    def start_data(self) -> dict[str, Any]:
        return _dict((self.start_event or {}).get("data"))

    @property
    def mode(self) -> str:
        return str((self.health or {}).get("mode") or self.start_data.get("mode") or "?")

    @property
    def git_commit(self) -> str:
        return str((self.health or {}).get("git_commit") or self.start_data.get("git_commit") or "")

    @property
    def git_tag(self) -> str:
        return str(self.extra.get("git_tag") or self.start_data.get("git_tag") or "")

    @property
    def config_hash(self) -> str:
        return str((self.health or {}).get("config_hash") or self.start_data.get("config_hash") or "")

    @property
    def feed(self) -> str:
        feed = self.extra.get("feed") or self.start_data.get("feed")
        if feed:
            return str(feed)
        if any(b.source == "synthetic" for b in self.books.values()):
            return "synthetic"
        return "unknown"

    @property
    def synthetic(self) -> bool:
        return self.feed == "synthetic" or any(b.source == "synthetic" for b in self.books.values())

    def _flag(self, key: str) -> Any:
        """Engine flag: the DB's kv state (written the moment it changes) beats the heartbeat."""
        if self.flags is not None and key in self.flags:
            return self.flags[key]
        if key == "flatten_phase":
            return self.extra.get("flatten_phase")
        return (self.health or {}).get(key)

    @property
    def killed(self) -> bool:
        return bool(self._flag("killed"))

    @property
    def paused(self) -> bool:
        return bool(self._flag("paused"))

    @property
    def kill_reason(self) -> str:
        return str((self.flags or {}).get("kill_reason") or _reason_with(self, "killed:") or "")

    @property
    def pause_reason(self) -> str:
        return str((self.flags or {}).get("pause_reason") or _reason_with(self, "paused:") or "")

    @property
    def flatten_phase(self) -> int:
        try:
            return int(self._flag("flatten_phase") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def initial_equity_tmn(self) -> Decimal:
        return _dec(self.extra.get("initial_equity_tmn")) or self.settings.initial_equity_tmn

    @property
    def deadline_ts(self) -> float | None:
        for text in (
            self.settings.challenge_end_utc,
            self.extra.get("challenge_end_utc"),
            self.start_data.get("challenge_end_utc"),
        ):
            ts = _ts(text)
            if ts is not None:
                return ts
        return None

    def heartbeat_age(self, now: float) -> float | None:
        return None if self.health_ts is None else max(0.0, now - self.health_ts)


# ---------------------------------------------------------------- small helpers


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dec(value: Any) -> Decimal | None:
    """Tolerant Decimal: str/int/float/Decimal -> Decimal; None, bools and garbage -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _ts(value: Any) -> float | None:
    """Unix seconds from a number or an ISO-8601 string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return parse_iso(str(value))
    except ValueError:
        return None


def _reason_with(snap: DashboardSnapshot, prefix: str) -> str:
    for r in (snap.health or {}).get("reasons") or []:
        if isinstance(r, str) and r.startswith(prefix):
            return r[len(prefix):].strip()
    return ""


def _pct(d: Decimal | None, places: int = 2, signed: bool = False) -> str:
    if d is None:
        return DASH
    v = d * 100
    return f"{v:+.{places}f}%" if signed else f"{v:.{places}f}%"


def _money(d: Decimal | None, places: int = 0, signed: bool = False) -> str:
    if d is None:
        return DASH
    return f"{d:+,.{places}f}" if signed else f"{d:,.{places}f}"


def _quote(symbol: str) -> str:
    return "TMN" if symbol.endswith("TMN") else "USDT" if symbol.endswith("USDT") else ""


def _price(d: Decimal | None, quote: str = "") -> str:
    """TMN prices as whole toman (Wallex tick), USDT prices with cents; tiny prices keep 8 decimals."""
    if d is None:
        return DASH
    if quote == "TMN" and d >= 100:
        return f"{d:,.0f}"
    if d >= 1000:
        return f"{d:,.2f}"
    if d >= 1:
        return f"{d:,.4f}"
    return f"{d:.8f}".rstrip("0").rstrip(".") or "0"


def _qty(d: Decimal | None) -> str:
    if d is None:
        return DASH
    text = f"{d:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return DASH
    s = int(abs(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        out = f"{d}d{h:02d}h"
    elif h:
        out = f"{h}h{m:02d}m"
    elif m:
        out = f"{m}m{sec:02d}s"
    else:
        out = f"{sec}s"
    return f"-{out}" if seconds < 0 else out


def _hms(ts: float | None) -> str:
    if ts is None:
        return DASH
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def _short(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _age_style(age: float | None) -> str:
    if age is None:
        return "red"
    return "green" if age < 5 else "yellow" if age < 30 else "red"


def _signed_style(d: Decimal | None) -> str:
    if d is None or d == 0:
        return ""
    return "green" if d > 0 else "red"


# ---------------------------------------------------------------- reading the logs


def _reverse_lines(path: Path, block: int = 65536) -> Iterator[bytes]:
    """Non-blank lines of a file, last first, reading ``block`` bytes at a time from the end."""
    with path.open("rb") as fh:
        pos = fh.seek(0, 2)
        tail = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + tail).split(b"\n")
            tail = lines.pop(0)
            for line in reversed(lines):
                if line.strip():
                    yield line
        if tail.strip():
            yield tail


def tail_records(
    log_dir: Path,
    base: str,
    max_lines: int = TAIL_MAX_LINES,
    until: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """The last records of a log family, oldest first, without reading whole files.

    Reads newest file first, last line first; stops after ``max_lines`` lines or as soon as
    ``until(record)`` is true (that record included). Undecodable lines are skipped.
    """
    out: list[dict[str, Any]] = []
    seen = 0
    for path in reversed(family_files(log_dir, base)):
        for raw in _reverse_lines(path):
            seen += 1
            if seen > max_lines:
                return out[::-1]
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            out.append(obj)
            if until is not None and until(obj):
                return out[::-1]
    return out[::-1]


def _read_health(snap: DashboardSnapshot, log_dir: Path) -> None:
    rec = latest_health(log_dir)
    if rec is not None:
        snap.health = rec
        snap.health_ts = _ts(rec.get("ts"))


def _read_events(snap: DashboardSnapshot, log_dir: Path) -> None:
    recent: list[dict[str, Any]] = []
    restart_starts = restart_events = 0
    for rec in tail_records(log_dir, "events", max_lines=5000):
        kind = rec.get("kind")
        if kind == "start":
            snap.start_event = rec
            restart_starts += _dict(rec.get("data")).get("reason") == "restart"
        restart_events += kind == "restart"
        if kind in ("start", "stop"):
            snap.lifecycle_event = rec
        if kind in NOTABLE_EVENTS:
            recent.append(rec)
    # a restart writes a "start" (reason restart) and, when trading, a "restart" event: count once
    snap.restarts = max(restart_starts, restart_events)
    snap.recent_events = recent[-4:]


def _read_decisions(snap: DashboardSnapshot, log_dir: Path, limit: int = DECISIONS_SHOWN) -> None:
    ids: set[str] = set()

    def enough(rec: dict[str, Any]) -> bool:
        if rec.get("kind") in TRADE_KINDS:
            ids.add(str(rec.get("decision_id")))
        return len(ids) >= limit

    records = tail_records(log_dir, "decisions", until=enough)
    merged = merge_decisions(records)
    snap.decisions = [r for r in merged.values() if r.get("kind") in TRADE_KINDS][-limit:]
    # plan fallback (the DB's plans table wins when it is newer): the newest plan snapshot logged
    for rec in reversed(records):
        plan = rec.get("plan_snapshot")
        if isinstance(plan, dict) and plan:
            snap.plan, snap.plan_source = plan, "decisions"
            snap.plan_ts = _ts(plan.get("ts")) or _ts(rec.get("ts"))
            break


# ---------------------------------------------------------------- reading the store


def _db_equity(snap: DashboardSnapshot, store: Store) -> None:
    snap.equity = store.latest_equity()


def _db_flags(snap: DashboardSnapshot, store: Store) -> None:
    flags = store.kv_get(KV_FLAGS)
    snap.flags = flags if isinstance(flags, dict) else None


def _db_positions(snap: DashboardSnapshot, store: Store) -> None:
    snap.positions = store.load_open_positions()


def _db_orders(snap: DashboardSnapshot, store: Store) -> None:
    snap.open_orders = store.load_orders(non_terminal_only=True)


def _db_plan(snap: DashboardSnapshot, store: Store) -> None:
    _lo, hi = store.time_range("plans")
    if hi is None:
        return
    plans = store.load_plans(from_ts=hi)
    if not plans:
        return
    plan = plans[-1].get("plan")
    ts = _ts(plans[-1].get("ts"))
    if isinstance(plan, dict) and (snap.plan_ts is None or ts is None or ts >= snap.plan_ts):
        snap.plan, snap.plan_ts, snap.plan_source = plan, ts, "db"


def _db_books(snap: DashboardSnapshot, store: Store) -> None:
    books: dict[str, BookSnapshot] = {}
    for sym in snap.watchlist:
        b = store.latest_book(sym)
        if b is not None:
            books[sym] = b
    missing = {s for s in snap.watchlist if s not in books}
    if missing and snap.mode != "?":
        # no recorder rows: the books the engine itself used (replay inputs), recent ones only
        for b in store.iter_engine_books(snap.mode, from_ts=snap.now - ENGINE_BOOK_WINDOW_S):
            cur = books.get(b.symbol)
            if b.symbol in missing and (cur is None or b.ts >= cur.ts):
                books[b.symbol] = b
    snap.books = books


def _guard(snap: DashboardSnapshot, label: str, fn: Callable[..., None], *args: Any) -> None:
    """Run one reader; a failure becomes a note in the health panel, never an exception."""
    try:
        fn(snap, *args)
    except Exception as exc:  # noqa: BLE001 - the dashboard must keep drawing whatever breaks
        snap.problems.append(_short(f"{label}: {type(exc).__name__}: {exc}", 160))


def _read_db(snap: DashboardSnapshot, state_db: str | Path | None) -> None:
    if not state_db:
        snap.problems.append("no state DB given")
        return
    path = Path(state_db)
    if not path.is_file():
        snap.problems.append(f"state DB not found: {path}")
        return
    try:
        store = Store.open_readonly(path)
    except Exception as exc:  # noqa: BLE001
        snap.problems.append(_short(f"state DB: {type(exc).__name__}: {exc}", 160))
        return
    snap.db_found = True
    try:
        for label, reader in (
            ("equity", _db_equity),
            ("flags", _db_flags),
            ("positions", _db_positions),
            ("orders", _db_orders),
            ("plan", _db_plan),
            ("books", _db_books),
        ):
            _guard(snap, label, reader, store)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def _watchlist(snap: DashboardSnapshot) -> list[str]:
    """Settings -> the engine's start event -> heartbeat book ages -> derived from the S1 coins."""
    candidates: list[Any] = [
        list(snap.settings.watchlist),
        snap.start_data.get("watchlist"),
        sorted(_dict(snap.extra.get("book_age_s"))),
    ]
    for cand in candidates:
        if isinstance(cand, list) and cand:
            return [str(s) for s in dict.fromkeys(cand)]
    out = ["USDTTMN"]
    for coin in snap.settings.s1_coins:
        out += [f"{coin}TMN", f"{coin}USDT"]
    return out


def build_snapshot(
    log_dir: str | Path,
    state_db: str | Path | None,
    *,
    now: float | None = None,
    cfg: Config | None = None,
    settings: DashboardSettings | None = None,
) -> DashboardSnapshot:
    """Read every source once (read-only). Never raises because a file, table or field is missing."""
    if settings is None:
        settings = DashboardSettings.from_config(cfg) if cfg is not None else DashboardSettings()
    snap = DashboardSnapshot(
        log_dir=str(log_dir),
        state_db=str(state_db or ""),
        now=time.time() if now is None else float(now),
        settings=settings,
    )
    log_path = Path(log_dir)
    if log_path.is_dir():
        _guard(snap, "health log", _read_health, log_path)
        _guard(snap, "events log", _read_events, log_path)
        _guard(snap, "decisions log", _read_decisions, log_path)
    else:
        snap.problems.append(f"log dir not found: {log_path}")
    snap.watchlist = _watchlist(snap)
    _read_db(snap, state_db)
    return snap


# ---------------------------------------------------------------- S1 edges


@dataclass(frozen=True)
class EdgeRow:
    """One S1 coin: both lock-in directions, edge vs need (= cost + margin), as fractions."""

    coin: str
    edge_tmn_cheap: Decimal | None
    need_tmn_cheap: Decimal | None
    edge_usdt_cheap: Decimal | None
    need_usdt_cheap: Decimal | None
    status: str = ""
    estimated: bool = False

    @property
    def hit_tmn_cheap(self) -> bool:
        return _hit(self.edge_tmn_cheap, self.need_tmn_cheap)

    @property
    def hit_usdt_cheap(self) -> bool:
        return _hit(self.edge_usdt_cheap, self.need_usdt_cheap)


def _hit(edge: Decimal | None, need: Decimal | None) -> bool:
    return edge is not None and need is not None and edge >= need


def s1_diagnostics(extra: Mapping[str, Any]) -> dict[str, Any] | None:
    """``extra.strategies.S1`` of a heartbeat (also accepts ``{"diagnostics": {...}}`` nesting)."""
    s1 = _dict(extra.get("strategies")).get("S1")
    if isinstance(s1, dict) and isinstance(s1.get("diagnostics"), dict):
        s1 = s1["diagnostics"]
    return s1 if isinstance(s1, dict) else None


def s1_edges(snap: DashboardSnapshot) -> list[EdgeRow]:
    """From the S1 diagnostics in the heartbeat; else estimated from the latest books."""
    diag = s1_diagnostics(snap.extra) or {}
    coins = _dict(diag.get("coins"))
    if not coins:
        return _estimated_edges(snap)
    margin = _dec(diag.get("margin_min_pct"))
    margin = margin if margin is not None else snap.settings.s1_margin_min_pct
    rows = []
    for coin in sorted(coins):
        d = _dict(coins[coin])
        c_t, c_u = _dec(d.get("cost_tmn_cheap")), _dec(d.get("cost_usdt_cheap"))
        lockin, revert = str(d.get("lockin") or ""), str(d.get("revert") or "")
        if lockin.startswith("ENTER"):
            status = "▶ lock-in"  # the highlighted edge cell shows which book is cheap
        elif revert.startswith("ENTER"):
            status = "▶ revert"
        else:
            status = str(d.get("skip") or "")
        rows.append(
            EdgeRow(
                coin=coin,
                edge_tmn_cheap=_dec(d.get("edge_tmn_cheap")),
                need_tmn_cheap=None if c_t is None else c_t + margin,
                edge_usdt_cheap=_dec(d.get("edge_usdt_cheap")),
                need_usdt_cheap=None if c_u is None else c_u + margin,
                status=status,
            )
        )
    return rows


def _estimated_edges(snap: DashboardSnapshot) -> list[EdgeRow]:
    """S1 lock-in edges from the books (PRD-03 §3 formulas) with default taker fees."""
    u = snap.books.get("USDTTMN")
    if u is None or u.best_bid is None or u.best_ask is None:
        return []
    margin = snap.settings.s1_margin_min_pct
    cost = EST_TAKER_FEE["TMN"] + EST_TAKER_FEE["USDT"]
    rows = []
    for coin in snap.settings.s1_coins:
        t, x = snap.books.get(f"{coin}TMN"), snap.books.get(f"{coin}USDT")
        if t is None or x is None or None in (t.best_bid, t.best_ask, x.best_bid, x.best_ask):
            continue
        if not t.best_ask or not x.best_ask:
            continue
        e_t = x.best_bid * u.best_bid / t.best_ask - 1  # type: ignore[operator]
        e_u = t.best_bid / (x.best_ask * u.best_ask) - 1  # type: ignore[operator]
        rows.append(EdgeRow(coin, e_t, cost + margin, e_u, cost + margin, "", estimated=True))
    return rows


# ---------------------------------------------------------------- layout helper


class _Row:
    """Panels side by side with equal heights (the tallest content decides), fixed width split."""

    def __init__(self, cells: list[tuple[str, RenderableType, str]], weights: list[int]) -> None:
        self.cells = cells
        self.weights = weights

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        total = options.max_width
        widths = [total * w // sum(self.weights) for w in self.weights]
        widths[-1] = total - sum(widths[:-1])
        measure = options.update(height=None)
        height = 0
        for (_title, body, _style), w in zip(self.cells, widths, strict=True):
            lines = console.render_lines(body, measure.update(width=max(w - 4, 1)), pad=False)
            height = max(height, len(lines))
        panels = [
            Panel(body, title=f"[bold]{title}[/]", title_align="left", border_style=style, height=height + 2)
            for title, body, style in self.cells
        ]
        rendered = [
            console.render_lines(p, options.update(width=w, height=height + 2))
            for p, w in zip(panels, widths, strict=True)
        ]
        for parts in zip(*rendered, strict=True):
            for line in parts:
                yield from line
            yield Segment.line()


def _kv_grid() -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(no_wrap=False)
    return grid


# ---------------------------------------------------------------- panels


def _banner(snap: DashboardSnapshot) -> Panel:
    text = Text(justify="center")
    text.append(f"⚠  {SYNTHETIC_BANNER}  ⚠", style="bold")
    text.append(f"\n{SYNTHETIC_DETAIL}")
    return Panel(text, style="bold white on red", border_style="white on red", box=box.HEAVY)


def _status_badges(snap: DashboardSnapshot, now: float) -> Text:
    t = Text()
    age = snap.heartbeat_age(now)
    if snap.health is None and snap.flags is None:
        t.append(" NO HEARTBEAT ", style="bold white on red")
        return t
    if snap.killed:
        t.append(" KILLED ", style="bold white on red")
        if snap.kill_reason:
            t.append(f" {_short(snap.kill_reason, 60)}", style="red")
    elif snap.paused:
        t.append(" PAUSED ", style="bold black on yellow")
        if snap.pause_reason:
            t.append(f" {_short(snap.pause_reason, 60)}", style="yellow")
    elif snap.flatten_phase:
        t.append(" FLATTENING ", style="bold black on magenta")
    else:
        t.append(" RUNNING ", style="bold black on green")
    if age is not None and age > HEARTBEAT_STALE_S:
        t.append("  ")
        t.append(f" STALE: no heartbeat for {_dur(age)} ", style="bold white on red")
    return t


def _header(snap: DashboardSnapshot, now: float) -> Panel:
    sep = ("  │  ", "dim")
    mode = snap.mode.upper()
    line1 = Text()
    line1.append("WALLEX TRADER AGENT", style="bold")
    line1.append(*sep)
    line1.append("mode ", style="dim")
    line1.append(mode, style="bold red" if mode == "LIVE" else "bold cyan")
    line1.append(*sep)
    line1.append("feed ", style="dim")
    line1.append(snap.feed, style="bold red" if snap.synthetic else "bold")
    line1.append(*sep)
    line1.append("commit ", style="dim")
    line1.append(snap.git_commit[:12] or DASH)
    if snap.git_tag:
        line1.append(f" ({snap.git_tag})", style="bold")
    if snap.extra.get("git_dirty") or snap.start_data.get("git_dirty"):
        line1.append(" dirty", style="yellow")
    line1.append(*sep)
    line1.append("config ", style="dim")
    line1.append(snap.config_hash or DASH)
    markets_source = snap.extra.get("markets_source") or snap.start_data.get("markets_source")
    if markets_source:
        line1.append(*sep)
        line1.append("markets ", style="dim")
        line1.append(str(markets_source), style="yellow" if markets_source != "api" else "")

    line2 = Text()
    line2.append("uptime ", style="dim")
    line2.append(_uptime(snap, now))
    line2.append(*sep)
    line2.append("deadline ", style="dim")
    dl = snap.deadline_ts
    if dl is None:
        line2.append("not set", style="yellow")
    else:
        left = dl - now
        line2.append(datetime.fromtimestamp(dl, tz=UTC).strftime("%Y-%m-%d %H:%MZ"))
        line2.append(f" ({'in ' + _dur(left) if left > 0 else 'ended'})", style="bold" if left > 7200 else "bold red")
    line2.append(*sep)
    line2.append("flatten ", style="dim")
    phase = snap.flatten_phase
    line2.append(PHASE_NAMES.get(phase, str(phase)), style="bold magenta" if phase else "")
    line2.append(*sep)
    line2.append_text(_status_badges(snap, now))
    line2.append(*sep)
    line2.append("heartbeat ", style="dim")
    age = snap.heartbeat_age(now)
    line2.append(DASH if age is None else f"{_dur(age)} ago", style="red" if age is None or age > 90 else "")
    line2.append(*sep)
    line2.append(datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ"), style="dim")
    return Panel(Group(line1, line2), border_style="red" if snap.killed else "bright_blue", padding=(0, 1))


def _uptime(snap: DashboardSnapshot, now: float) -> str:
    life = snap.lifecycle_event
    if life is not None and life.get("kind") == "stop":
        ts = _ts(life.get("ts"))
        return f"stopped {_dur(now - ts)} ago" if ts is not None else "stopped"
    if life is not None:
        ts = _ts(life.get("ts"))
        if ts is not None:
            return _dur(now - ts)
    up = snap.extra.get("uptime_s")
    if isinstance(up, (int, float)) and snap.health_ts is not None:
        return _dur(up + max(0.0, now - snap.health_ts))
    return DASH


def _equity_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    eq_row = snap.equity or {}
    extra = snap.extra
    s = snap.settings
    equity = _dec(eq_row.get("equity_tmn")) or _dec((snap.health or {}).get("equity_tmn"))
    peak = _dec(eq_row.get("peak_tmn")) or _dec(extra.get("peak_equity_tmn"))
    dd = _dec(eq_row.get("drawdown_pct"))
    if dd is None:
        dd = _dec((snap.health or {}).get("drawdown_pct"))
    if dd is None and equity is not None and peak:
        dd = max((peak - equity) / peak, Decimal(0))
    initial = snap.initial_equity_tmn
    ret = equity / initial - 1 if equity is not None and initial else None
    daily = _dec(extra.get("daily_pnl_pct"))
    realized = _dec(extra.get("realized_pnl_tmn"))
    fees = _dec(extra.get("fees_paid_tmn"))
    used = _dec(extra.get("fee_budget_used_pct"))
    budget = fees / used if fees is not None and used else initial * s.fee_budget_pct
    if used is None and fees is not None and budget:
        used = fees / budget
    usdt_share = coin_exp = None
    if equity:
        u_val, c_val = _dec(eq_row.get("usdt_tmn_value")), _dec(eq_row.get("coins_tmn_value"))
        usdt_share = None if u_val is None else u_val / equity
        coin_exp = None if c_val is None else c_val / equity
    usdt_share = usdt_share if usdt_share is not None else _dec(extra.get("usdt_share"))
    coin_exp = coin_exp if coin_exp is not None else _dec(extra.get("coin_exposure_pct"))
    target = _dec(_dict(snap.plan).get("usdt_target_share"))

    g = _kv_grid()
    if equity is None:
        g.add_row("Equity", Text("no equity yet", style="yellow"))
    else:
        g.add_row("Equity", Text(f"{_money(equity)} TMN", style="bold"))
    g.add_row("Return", Text.assemble((_pct(ret, 2, signed=True), _signed_style(ret)), (f"  vs {_money(initial)}", "dim")))
    g.add_row("Peak", _money(peak))
    dd_style = "" if dd is None else "red" if dd >= s.max_drawdown_kill_pct * Decimal("0.6") else "yellow" if dd >= s.max_drawdown_kill_pct * Decimal("0.3") else "green"
    g.add_row("Drawdown", Text.assemble((_pct(dd), dd_style), (f"  kill at {_pct(s.max_drawdown_kill_pct, 1)}", "dim")))
    daily_style = _signed_style(daily)
    if daily is not None and daily <= -s.daily_loss_limit_pct * Decimal("0.6"):
        daily_style = "bold red"
    g.add_row("Daily PnL", Text.assemble((_pct(daily, 2, signed=True), daily_style), (f"  pause at -{_pct(s.daily_loss_limit_pct, 1)}", "dim")))
    g.add_row("Realized", Text(f"{_money(realized, signed=True)} TMN" if realized is not None else DASH, style=_signed_style(realized)))
    fee_style = "" if used is None else "red" if used >= Decimal("0.8") else "yellow" if used >= Decimal("0.5") else ""
    g.add_row("Fees", Text.assemble((f"{_money(fees)} / {_money(budget)}", fee_style), (f"  ({_pct(used, 1)})", "dim")))
    usdt_txt = Text(_pct(usdt_share, 1))
    if target is not None:
        usdt_txt.append(f"  target {_pct(target, 1)}", style="dim")
    g.add_row("USDT share", usdt_txt)
    exp_style = "red" if coin_exp is not None and coin_exp > s.max_coin_exposure_pct else ""
    g.add_row("Coin exposure", Text.assemble((_pct(coin_exp, 1), exp_style), (f"  cap {_pct(s.max_coin_exposure_pct, 0)}", "dim")))
    orders_today = extra.get("orders_today")
    g.add_row("Orders today", f"{orders_today if isinstance(orders_today, int) else DASH} / {s.max_orders_per_day}")
    as_of = _ts(eq_row.get("ts")) if eq_row else snap.health_ts
    src = "DB equity row" if eq_row else "heartbeat"
    g.add_row("As of", Text(f"{_dur(now - as_of)} ago ({src})" if as_of is not None else DASH, style="dim"))
    return g


def _markets_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    watch = Table(box=box.SIMPLE_HEAD, padding=(0, 1), show_edge=False, expand=False)
    watch.add_column("Market", no_wrap=True, style="bold")
    watch.add_column("Bid", justify="right", no_wrap=True)
    watch.add_column("Ask", justify="right", no_wrap=True)
    watch.add_column("Spread", justify="right", no_wrap=True)
    watch.add_column("Age", justify="right", no_wrap=True)
    for sym in snap.watchlist:
        b = snap.books.get(sym)
        if b is None:
            watch.add_row(sym, DASH, DASH, DASH, Text("no book", style="red"))
            continue
        age = now - b.ts
        watch.add_row(
            sym,
            Text(_price(b.best_bid, _quote(sym)), style="green"),
            Text(_price(b.best_ask, _quote(sym)), style="red"),
            _pct(b.spread_pct, 3),
            Text(_dur(age), style=_age_style(age)),
        )
    rows = s1_edges(snap)
    edges = Table(box=box.SIMPLE_HEAD, padding=(0, 1), show_edge=False, pad_edge=False, expand=False)
    edges.add_column("S1", no_wrap=True, style="bold", min_width=4)
    edges.add_column("TMN cheap", justify="right", no_wrap=True, min_width=12)
    edges.add_column("USDT cheap", justify="right", no_wrap=True, min_width=12)
    edges.add_column("State", overflow="ellipsis", max_width=16)  # wrapable: shrinks first when tight
    for r in rows:
        edges.add_row(
            r.coin,
            _edge_text(r.edge_tmn_cheap, r.need_tmn_cheap, r.hit_tmn_cheap),
            _edge_text(r.edge_usdt_cheap, r.need_usdt_cheap, r.hit_usdt_cheap),
            Text(_short(r.status, 16), style="bold green" if "▶" in r.status else "dim", no_wrap=True),
        )
    legend = "edge/need in %, need = cost + margin, green = edge ≥ need"
    if not rows:
        caption = Text("no S1 diagnostics and no books to estimate edges", style="dim")
    elif rows[0].estimated:
        caption = Text(f"{legend}\nestimated from the books, default taker fees (no S1 diagnostics yet)", style="dim")
    else:
        caption = Text(f"{legend}\nfrom the S1 diagnostics of the last heartbeat", style="dim")
    s1_status = (s1_diagnostics(snap.extra) or {}).get("status")
    if s1_status:
        caption.append(f"\nS1 status: {s1_status}", style="yellow")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()
    grid.add_row(watch, Group(edges, caption))
    return grid


def _edge_text(edge: Decimal | None, need: Decimal | None, hit: bool) -> Text:
    if edge is None:
        return Text(DASH, style="dim")
    body = f"{edge * 100:+.3f}/{need * 100:.3f}" if need is not None else f"{edge * 100:+.3f}"
    return Text(body, style="bold black on green" if hit else ("" if edge > 0 else "dim"))


def _ok_text(value: Any, off_when_none: str = "n/a") -> Text:
    if value is None:
        return Text(off_when_none, style="dim")
    return Text("✓ ok", style="green") if value else Text("✗ down", style="bold red")


def _health_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    h = snap.health or {}
    extra = snap.extra
    g = _kv_grid()
    age = snap.heartbeat_age(now)
    if not h:
        g.add_row("Heartbeat", Text("none yet", style="bold red"))
    else:
        g.add_row("Heartbeat", Text(f"{_dur(age)} ago", style="bold red" if age is not None and age > HEARTBEAT_STALE_S else ""))
    g.add_row("REST", _ok_text(h.get("rest_ok")) if h else Text(DASH, style="dim"))
    ws_configured = extra.get("ws") is not None or bool(h.get("ws_ok"))
    g.add_row("WebSocket", _ok_text(h.get("ws_ok")) if h and ws_configured else Text("off", style="dim"))
    ai = _dict(extra.get("ai"))
    source = extra.get("ai_source") or ai.get("ai_source")
    if source == "offline-rules":
        g.add_row("LLM", Text("offline rules (no LLM key)", style="yellow"))
    elif source == "disabled":
        g.add_row("LLM", Text("AI off (rules-only)", style="dim"))
    else:
        llm = _ok_text(h.get("llm_ok"))
        failures = ai.get("llm_consecutive_failures")
        if h.get("llm_ok") is False and isinstance(failures, int) and failures:
            llm.append(f" ×{failures}", style="red")
        g.add_row("LLM", llm)
    mcp = extra.get("mcp_ok")
    if mcp is None and isinstance(extra.get("mcp"), dict):
        mcp = extra["mcp"].get("ok")
    g.add_row("MCP", _ok_text(mcp))
    errors = h.get("api_errors_1m")
    g.add_row("API errors 1m", Text(str(errors) if isinstance(errors, int) else DASH, style="red" if isinstance(errors, int) and errors else ""))
    restarts = extra.get("restarts")
    n_restarts = restarts if isinstance(restarts, int) else snap.restarts
    g.add_row("Restarts", Text(str(n_restarts), style="yellow" if n_restarts else ""))
    crashes = extra.get("tick_crashes")
    task = sum(v for v in _dict(extra.get("task_crashes")).values() if isinstance(v, int))
    if isinstance(crashes, int) or task:
        g.add_row("Crashes", Text(f"tick {crashes if isinstance(crashes, int) else 0} · task {task}", style="red" if crashes or task else ""))
    parts: list[RenderableType] = [g]
    # one line per item (ellipsis), so the panel keeps its height whatever the engine reports
    reasons = [r for r in h.get("reasons") or [] if isinstance(r, str)]
    for r in reasons[:MAX_REASONS_SHOWN]:
        style = "red" if r.startswith(("killed", "api", "rest", "ws")) else "yellow"
        parts.append(_line(f"• {r}", style))
    if len(reasons) > MAX_REASONS_SHOWN:
        parts.append(_line(f"  +{len(reasons) - MAX_REASONS_SHOWN} more reasons", "dim"))
    if h and not reasons:
        parts.append(_line("no warnings", "green"))
    for ev in snap.recent_events[-2:]:
        parts.append(_line(f"{_hms(_ts(ev.get('ts')))} {ev.get('kind')}: {ev.get('reason') or ''}", "dim"))
    for p in snap.problems[:3]:
        parts.append(Text(f"! {p}", style="yellow"))
    return Group(*parts)


def _line(text: str, style: str = "") -> Text:
    """A single line cut with an ellipsis at the panel edge."""
    return Text(" ".join(text.split()), style=style, no_wrap=True, overflow="ellipsis")


_REGIME_STYLE = {"trend_up": "green", "trend_down": "red", "range": "cyan", "chaos": "bold magenta"}


def _plan_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    plan = snap.plan
    parts: list[RenderableType] = []
    if not plan:
        parts.append(Text("rules-only", style="bold yellow"))
        parts.append(Text("no plan stored yet: every strategy runs on its rules-only defaults (size ×1)", style="dim"))
        llm_ok = (snap.health or {}).get("llm_ok")
        if llm_ok is False:
            parts.append(Text("LLM down", style="red"))
        return Group(*parts)
    model = str(plan.get("model") or "")
    head = Text()
    if model == OFFLINE_PLAN_MODEL:
        head.append("AI: offline rules (no LLM key)", style="bold yellow")
    else:
        head.append(f"AI: {model or 'unknown model'}", style="bold green")
        if plan.get("cached"):
            head.append("  cached", style="cyan")
        if plan.get("tools_source") and plan.get("tools_source") != "none":
            head.append(f"  tools {plan.get('tools_source')}", style="dim")
    age = None if snap.plan_ts is None else now - snap.plan_ts
    head.append(f"   age {_dur(age)}", style="red" if age is not None and age > 900 else "dim")
    parts.append(head)
    enabled = [str(s) for s in plan.get("enabled_strategies") or []]
    line = Text()
    line.append("Enabled ", style="dim")
    line.append(" ".join(enabled) if enabled else "none", style="bold" if enabled else "bold red")
    line.append("   Size ", style="dim")
    line.append(f"×{_qty(_dec(plan.get('size_multiplier')))}")
    line.append("   USDT target ", style="dim")
    line.append(_pct(_dec(plan.get("usdt_target_share")), 1))
    line.append("   S1 margin ", style="dim")
    line.append(f"≥ {_pct(_dec(plan.get('s1_margin_min_pct')), 2)}")
    parts.append(line)
    flags = [str(f) for f in plan.get("risk_flags") or []]
    if flags:
        parts.append(Text("Risk flags: " + ", ".join(flags), style="bold red"))
    clamped = _dict(plan.get("clamped"))
    if clamped:
        parts.append(Text("Clamped: " + ", ".join(sorted(clamped)), style="yellow"))
    regimes = _dict(plan.get("regimes"))
    if regimes:
        grid = Table.grid(padding=(0, 1))
        for group in range(2):
            if group:
                grid.add_column(width=2)  # gap between the two column groups
            grid.add_column(no_wrap=True, style="bold")
            grid.add_column(no_wrap=True)
            grid.add_column(no_wrap=True, justify="right")
        cells: list[list[RenderableType]] = []
        for sym in sorted(regimes):
            call = _dict(regimes[sym])
            regime = str(call.get("regime") or "?")
            conf = call.get("confidence")
            conf_text = f"{conf:.2f}" if isinstance(conf, (int, float)) and not isinstance(conf, bool) else DASH
            cells.append([sym, Text(regime, style=_REGIME_STYLE.get(regime, "")), conf_text])
        half = (len(cells) + 1) // 2  # left group top to bottom, then the right group
        for i in range(half):
            right = cells[half + i] if half + i < len(cells) else ["", "", ""]
            grid.add_row(*cells[i], "", *right)
        parts.append(Text("Regimes (confidence)", style="dim"))
        parts.append(grid)
    notes = str(plan.get("notes") or "").strip()
    if notes:
        parts.append(Text(_short(notes, MAX_NOTES_CHARS), style="italic"))
    return Group(*parts)


def _positions_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    parts: list[RenderableType] = []
    if not snap.db_found:
        h = snap.health or {}
        parts.append(Text("state DB not available", style="yellow"))
        parts.append(Text(f"heartbeat: {h.get('positions', DASH)} positions, {h.get('open_orders', DASH)} open orders", style="dim"))
        return Group(*parts)
    if snap.positions:
        t = _table(
            f"Positions ({len(snap.positions)})",
            [("Strat", "l"), ("Symbol", "l"), ("Qty", "r"), ("Entry", "r"), ("Now", "r"), ("uPnL", "r"),
             ("Stop", "r"), ("Target", "r"), ("Held", "r"), ("Mode", "w")],
        )
        for p in snap.positions[:MAX_POSITIONS_SHOWN]:
            b = snap.books.get(p.symbol)
            bid = b.best_bid if b is not None else None
            upnl = bid / p.entry_price - 1 if bid is not None and p.entry_price else None
            q = _quote(p.symbol)
            t.add_row(
                p.strategy, p.symbol, _qty(p.qty), _price(p.entry_price, q), _price(bid, q),
                Text(_pct(upnl, 2, signed=True), style=_signed_style(upnl)),
                _price(p.stop_price, q), _price(p.target_price, q), _dur(now - p.entry_ts),
                Text(str(p.meta.get("mode") or ""), no_wrap=True),
            )
        parts.append(t)
        if len(snap.positions) > MAX_POSITIONS_SHOWN:
            parts.append(Text(f"+{len(snap.positions) - MAX_POSITIONS_SHOWN} more", style="dim"))
    else:
        parts.append(Text("No open positions", style="dim"))
    if snap.open_orders:
        t = _table(
            f"Open orders ({len(snap.open_orders)})",
            [("Strat", "l"), ("Symbol", "l"), ("Side", "l"), ("Type", "l"), ("Purpose", "l"),
             ("Filled/Qty", "r"), ("Price", "r"), ("Status", "l"), ("Age", "r"), ("Client id", "w")],
        )
        newest = sorted(snap.open_orders, key=lambda o: (o.created_ts, o.client_id))[-MAX_ORDERS_SHOWN:]
        for o in reversed(newest):
            r = o.req
            status = "PARTIAL" if o.status == "PARTIALLY_FILLED" else o.status
            t.add_row(
                r.strategy, r.symbol, Text(r.side, style="green" if r.side == "BUY" else "red"), r.type, r.purpose,
                f"{_qty(o.filled_qty)}/{_qty(r.qty)}",
                _price(r.price if r.price is not None else r.stop_price, _quote(r.symbol)),
                status, _dur(now - o.created_ts), Text(r.client_id, no_wrap=True),
            )
        parts.append(t)
        if len(snap.open_orders) > MAX_ORDERS_SHOWN:
            parts.append(Text(f"+{len(snap.open_orders) - MAX_ORDERS_SHOWN} older open orders", style="dim"))
    else:
        parts.append(Text("No open orders", style="dim"))
    return Group(*parts)


def _table(title: str, columns: list[tuple[str, str]]) -> Table:
    """Compact table; justify ``l``/``r``, or ``w`` for the one text column that may shrink (ellipsis)."""
    t = Table(
        box=box.SIMPLE_HEAD, padding=(0, 1), show_edge=False, pad_edge=False, title=title, title_justify="left",
        title_style="bold",
    )
    for name, kind in columns:
        if kind == "w":
            t.add_column(name, overflow="ellipsis")
        else:
            t.add_column(name, justify="right" if kind == "r" else "left", no_wrap=True, min_width=len(name))
    return t


def _risk_text(rec: Mapping[str, Any]) -> Text:
    risk = _dict(rec.get("risk"))
    approved = risk.get("approved")
    if approved is True:
        return Text("✓", style="bold green")
    if approved is False:
        failed = [
            str(c.get("code")) for c in risk.get("checks") or [] if isinstance(c, dict) and c.get("passed") is False
        ]
        detail = ",".join(failed) or _short(risk.get("veto_reason"), 24) or "veto"
        return Text(f"✗ {detail}", style="bold red")
    return Text(DASH, style="dim")


def _outcome_text(rec: Mapping[str, Any]) -> Text:
    out = rec.get("outcome")
    order = _dict(rec.get("order"))
    if isinstance(out, dict) and out:
        pnl = _dec(out.get("pnl_tmn"))
        if pnl is not None:
            pnl_pct = _dec(out.get("pnl_pct"))
            text = f"{_money(pnl, signed=True)} TMN"
            if pnl_pct is not None:
                text += f" ({_pct(pnl_pct, 2, signed=True)})"
            if out.get("exit_reason"):
                text += f" {out['exit_reason']}"
            return Text(text, style=_signed_style(pnl))
        status = str(out.get("status") or "done")
        return Text(status, style="green" if status == "FILLED" else "yellow")
    if rec.get("kind") == "order":
        if order.get("accepted") is False or str(rec.get("notes") or "").startswith("REFUSED"):
            return Text("not sent", style="yellow")
        return Text("working…", style="cyan")
    return Text(DASH, style="dim")


def _decisions_body(snap: DashboardSnapshot, now: float) -> RenderableType:
    if not snap.decisions:
        return Text("no trade decisions logged yet", style="dim")
    t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), show_edge=False, expand=True)
    t.add_column("Time", no_wrap=True, style="dim")
    t.add_column("Kind", no_wrap=True)
    t.add_column("Strat", no_wrap=True, style="bold")
    t.add_column("Symbol", no_wrap=True)
    t.add_column("Side", no_wrap=True)
    t.add_column("Qty", justify="right", no_wrap=True)
    t.add_column("Risk", no_wrap=True, max_width=30, overflow="ellipsis")
    t.add_column("Reason", ratio=1, no_wrap=True, overflow="ellipsis")
    t.add_column("Outcome", no_wrap=True, max_width=34, overflow="ellipsis")
    for rec in reversed(snap.decisions):  # newest first
        intent = _dict(rec.get("intent"))
        req = _dict(_dict(rec.get("order")).get("request"))
        side = str(intent.get("side") or req.get("side") or "")
        qty = _dec(_dict(rec.get("risk")).get("adjusted_qty")) or _dec(intent.get("qty")) or _dec(req.get("qty"))
        reason = intent.get("reason") or rec.get("notes") or ""
        t.add_row(
            _hms(_ts(rec.get("ts"))),
            str(rec.get("kind") or ""),
            str(rec.get("strategy") or ""),
            str(rec.get("symbol") or ""),
            Text(side, style="green" if side == "BUY" else "red" if side == "SELL" else ""),
            _qty(qty),
            _risk_text(rec),
            _short(reason, 160),
            _outcome_text(rec),
        )
    return t


# ---------------------------------------------------------------- render & run


def render(snapshot: DashboardSnapshot, now: float | None = None) -> RenderableType:
    """One dashboard frame (a ``Group``); ``now`` defaults to the snapshot's read time."""
    now = snapshot.now if now is None else now
    parts: list[RenderableType] = []
    if snapshot.synthetic:
        parts.append(_banner(snapshot))
    parts.append(_header(snapshot, now))
    parts.append(
        _Row(
            [
                ("Equity", _equity_body(snapshot, now), "green"),
                ("Markets", _markets_body(snapshot, now), "cyan"),
                ("Health", _health_body(snapshot, now), "blue"),
            ],
            [21, 57, 22],
        )
    )
    parts.append(
        _Row(
            [
                ("Current plan", _plan_body(snapshot, now), "magenta"),
                ("Positions & open orders", _positions_body(snapshot, now), "yellow"),
            ],
            [40, 60],
        )
    )
    parts.append(_Row([(f"Last {DECISIONS_SHOWN} decisions", _decisions_body(snapshot, now), "white")], [1]))
    return Group(*parts)


def _frame(log_dir: str | Path, state_db: str | Path | None, settings: DashboardSettings) -> RenderableType:
    now = time.time()
    try:
        return render(build_snapshot(log_dir, state_db, now=now, settings=settings), now)
    except Exception as exc:  # noqa: BLE001 - keep the live screen up; the next refresh may succeed
        return Panel(Text(f"dashboard error: {type(exc).__name__}: {exc}", style="red"), title="wta tui")


def run(
    log_dir: str | Path,
    state_db: str | Path | None,
    refresh_s: float = 2.0,
    once: bool = False,
    *,
    cfg: Config | None = None,
    console: Console | None = None,
) -> int:
    """Draw the dashboard: one frame (``once``) or a live screen refreshed every ``refresh_s``.

    Read-only: it opens the DB with ``Store.open_readonly`` and only reads the logs. Ctrl-C quits.
    """
    console = console or Console()
    settings = DashboardSettings.from_config(cfg) if cfg is not None else DashboardSettings()
    if once:
        console.print(_frame(log_dir, state_db, settings))
        return 0
    interval = max(0.2, float(refresh_s))
    with Live(_frame(log_dir, state_db, settings), console=console, screen=True, auto_refresh=False) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(_frame(log_dir, state_db, settings), refresh=True)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    """``python -m wta.dashboard.tui [--log-dir logs] [--db data/wta.sqlite] [--once]``."""
    ap = argparse.ArgumentParser(prog="wta-tui", description="read-only terminal dashboard (PRD-06 §3)")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--db", default="data/wta.sqlite", help="engine state DB (opened read-only)")
    ap.add_argument("--refresh", type=float, default=2.0, help="seconds between refreshes")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--no-config", action="store_true", help="do not read configs/*.yaml for limits")
    args = ap.parse_args(argv)
    cfg = None
    if not args.no_config:
        try:
            from wta.config import load_config

            cfg = load_config()
        except Exception:  # noqa: BLE001 - the dashboard works with built-in defaults
            cfg = None
    return run(args.log_dir, args.db, refresh_s=args.refresh, once=args.once, cfg=cfg)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
