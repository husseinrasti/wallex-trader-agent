"""SQLite store for market data and engine state (PRD-01 §7).

One file (``data/wta.sqlite``), stdlib ``sqlite3`` in WAL mode so the TUI and scripts can read
while the recorder/engine writes. Market data and log-like rows (books, trades, candles, balances,
equity, plans, events) are buffered and written in batches (every ``flush_interval_s`` or
``flush_rows`` rows). Engine state that must survive a crash (orders, fills, positions, kv) is
written through immediately — it is rare, so batching would buy nothing.

``transaction()`` groups writes into one commit: inside it nothing is committed (immediate writes
and size/timer flushes wait), rows are executed lazily (before a read, so reads see them) and
everything is committed once at the end. The engine wraps each fast tick in one, so orders, fills,
positions and the kv state (portfolio, flags, orders, paper exchange) always move together: a crash
restores the state as of the end of a tick, never a mix. ``checkpoint()`` commits now, also inside a
transaction (the OrderManager's write-ahead record before an order is sent). A failed commit keeps
every row of the transaction pending (rolled back, retried by the next flush).

Replay inputs (PRD-05): ``engine_books`` holds every order book the engine actually used in a fast
tick (all levels, deduplicated by ``(symbol, ts)``) and ``ticks`` one row per fast tick with the
book timestamps it saw, both tagged with the engine ``mode``. Replay feeds them back tick by tick.

Every price/quantity/money value is stored as TEXT (``str(Decimal)``) and comes back as an exact
``Decimal``. Books are compact JSON: ``[["price", "qty"], ...]`` limited to ``depth_levels``.
Time bounds on readers are inclusive (``from_ts <= ts <= to_ts``).
"""

from __future__ import annotations

import heapq
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

from wta.config import canonical_json
from wta.models import Balance, BookSnapshot, Candle, Fill, Level, Order, Position, Trade

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(ts REAL, symbol TEXT, source TEXT, bids_json TEXT, asks_json TEXT);
CREATE TABLE IF NOT EXISTS trades(ts REAL, symbol TEXT, price TEXT, qty TEXT, side TEXT);
CREATE TABLE IF NOT EXISTS candles(
    symbol TEXT, resolution TEXT, ts INTEGER, o TEXT, h TEXT, l TEXT, c TEXT, v TEXT,
    PRIMARY KEY(symbol, resolution, ts)
);
CREATE TABLE IF NOT EXISTS balances(ts REAL, asset TEXT, free TEXT, locked TEXT);
CREATE TABLE IF NOT EXISTS equity(
    ts REAL, equity_tmn TEXT, tmn TEXT, usdt_tmn_value TEXT, coins_tmn_value TEXT,
    peak_tmn TEXT, drawdown_pct TEXT
);
CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY, json TEXT, updated_ts REAL);
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, order_client_id TEXT, json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY, json TEXT, open INTEGER, updated_ts REAL);
CREATE TABLE IF NOT EXISTS plans(ts REAL, json TEXT, prompt_hash TEXT);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, json TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, json TEXT, updated_ts REAL);
CREATE TABLE IF NOT EXISTS engine_books(
    ts REAL, mode TEXT, symbol TEXT, source TEXT, bids_json TEXT, asks_json TEXT
);
CREATE TABLE IF NOT EXISTS ticks(ts REAL, mode TEXT, json TEXT);
CREATE INDEX IF NOT EXISTS idx_books_symbol_ts ON books(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_ts ON candles(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_plans_ts ON plans(ts);
CREATE INDEX IF NOT EXISTS idx_engine_books_mode_ts ON engine_books(mode, ts);
CREATE INDEX IF NOT EXISTS idx_ticks_mode_ts ON ticks(mode, ts);
"""

TABLES = (
    "books",
    "trades",
    "candles",
    "balances",
    "equity",
    "orders",
    "fills",
    "positions",
    "plans",
    "events",
    "kv",
    "engine_books",
    "ticks",
)
# the time column of each table (for time_range) and which tables have a symbol column
_TS_COLUMN = {t: "ts" for t in TABLES} | {"orders": "updated_ts", "positions": "updated_ts", "kv": "updated_ts"}
_SYMBOL_TABLES = frozenset({"books", "trades", "candles", "engine_books"})

_SQL_BOOK = "INSERT INTO books(ts, symbol, source, bids_json, asks_json) VALUES (?, ?, ?, ?, ?)"
_SQL_TRADE = "INSERT INTO trades(ts, symbol, price, qty, side) VALUES (?, ?, ?, ?, ?)"
_SQL_CANDLE = (
    "INSERT OR REPLACE INTO candles(symbol, resolution, ts, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_SQL_BALANCE = "INSERT INTO balances(ts, asset, free, locked) VALUES (?, ?, ?, ?)"
_SQL_EQUITY = (
    "INSERT INTO equity(ts, equity_tmn, tmn, usdt_tmn_value, coins_tmn_value, peak_tmn, drawdown_pct)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_SQL_ORDER = "INSERT OR REPLACE INTO orders(client_id, json, updated_ts) VALUES (?, ?, ?)"
_SQL_FILL = "INSERT INTO fills(order_client_id, json, ts) VALUES (?, ?, ?)"
_SQL_POSITION = "INSERT OR REPLACE INTO positions(id, json, open, updated_ts) VALUES (?, ?, ?, ?)"
_SQL_PLAN = "INSERT INTO plans(ts, json, prompt_hash) VALUES (?, ?, ?)"
_SQL_EVENT = "INSERT INTO events(ts, kind, json) VALUES (?, ?, ?)"
_SQL_KV = "INSERT OR REPLACE INTO kv(key, json, updated_ts) VALUES (?, ?, ?)"
_SQL_ENGINE_BOOK = (
    "INSERT INTO engine_books(ts, mode, symbol, source, bids_json, asks_json) VALUES (?, ?, ?, ?, ?, ?)"
)
_SQL_TICK = "INSERT INTO ticks(ts, mode, json) VALUES (?, ?, ?)"

# If the disk keeps failing, keep at most this many unwritten rows in memory (oldest dropped).
MAX_PENDING_ROWS = 100_000


# ---------------------------------------------------------------- encoding helpers


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _levels_json(levels: list[Level], depth: int) -> str:
    return json.dumps([[str(lv.price), str(lv.qty)] for lv in levels[:depth]], separators=(",", ":"))


def _levels_from_json(text: str) -> list[Level]:
    return [Level(price=Decimal(p), qty=Decimal(q)) for p, q in json.loads(text)]


def _log_json(data: Any) -> str:
    """Events/plans: canonical JSON, Decimals rendered as strings (same as the JSONL logs)."""
    return canonical_json(data).decode()


def _tag_decimals(value: Any) -> Any:
    """kv values keep their Decimals: ``Decimal("1.5")`` is stored as ``{"$decimal": "1.5"}``."""
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, dict):
        return {str(k): _tag_decimals(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_tag_decimals(v) for v in value]
    return value


def _untag_decimal(obj: dict[str, Any]) -> Any:
    if obj.keys() == {"$decimal"}:
        return Decimal(obj["$decimal"])
    return obj


# ---------------------------------------------------------------- store


class Store:
    """Buffered SQLite writer + typed readers. Not thread-safe: use it from one thread (the loop)."""

    def __init__(
        self,
        path: str | Path,
        flush_interval_s: float = 1.0,
        flush_rows: int = 200,
        readonly: bool = False,
        depth_levels: int = 10,
    ) -> None:
        self.path = str(path)
        self.flush_interval_s = flush_interval_s
        self.flush_rows = flush_rows
        self.readonly = readonly
        self.depth_levels = depth_levels
        self.rows_written: dict[str, int] = dict.fromkeys(TABLES, 0)  # committed rows since open
        self._pending: list[tuple[str, str, tuple[Any, ...]]] = []  # (table, sql, params)
        self._tx_depth = 0  # > 0 inside transaction(): nothing is committed
        self._tx_rows: list[tuple[str, str, tuple[Any, ...]]] = []  # executed, not committed yet
        self._last_flush_ts = 0.0
        self._closed = False
        self._conn = self._connect()

    @classmethod
    def open_readonly(cls, path: str | Path) -> Store:
        """Read-only connection (TUI, status scripts) that never blocks or modifies the writer."""
        return cls(path, readonly=True)

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------ buffering

    @property
    def pending_rows(self) -> int:
        return len(self._pending)

    @property
    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    def _enqueue(self, table: str, sql: str, params: tuple[Any, ...], immediate: bool = False) -> None:
        if self.readonly:
            raise RuntimeError("store is read-only")
        self._pending.append((table, sql, params))
        if self._tx_depth == 0 and (immediate or len(self._pending) >= self.flush_rows):
            self.flush()

    def maybe_flush(self, now: float) -> int:
        """Flush if ``flush_interval_s`` passed since the last timed flush or the buffer is full.

        Inside a transaction nothing happens: the transaction commits at its end.
        """
        if self._tx_depth > 0:
            return 0
        if len(self._pending) >= self.flush_rows or now - self._last_flush_ts >= self.flush_interval_s:
            self._last_flush_ts = now
            return self.flush()
        return 0

    @contextmanager
    def transaction(self, raise_errors: bool = True) -> Iterator[Store]:
        """Group every write in the block into one commit (nested blocks join the outer one).

        With ``raise_errors=False`` a failed final commit is logged instead of raised; its rows stay
        pending and the next flush retries them.
        """
        if self.readonly:
            raise RuntimeError("store is read-only")
        self._tx_depth += 1
        failed = True
        try:
            yield self
            failed = False
        finally:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                try:
                    self.flush()
                except sqlite3.Error:
                    if raise_errors and not failed:
                        raise
                    log.exception("store: commit at the end of a transaction failed; rows kept pending")

    def checkpoint(self) -> int:
        """Commit everything written so far, also inside a transaction. Returns rows written."""
        depth, self._tx_depth = self._tx_depth, 0
        try:
            return self.flush()
        finally:
            self._tx_depth = depth

    def flush(self) -> int:
        """Write every pending row in one transaction. Returns the number of rows written.

        Inside ``transaction()`` the rows are executed (so this connection's reads see them) but
        not committed. On a database error everything not committed yet (this batch and the rows
        executed earlier in the transaction) is rolled back and stays pending (retried on the next
        flush), and the error is raised.
        """
        if self.readonly:
            return 0
        if not self._pending and (self._tx_depth > 0 or not self._tx_rows):
            return 0
        batch, self._pending = self._pending, []
        try:
            # consecutive rows with the same statement go through one executemany; order is kept
            for (_table, sql), group in groupby(batch, key=lambda row: (row[0], row[1])):
                self._conn.executemany(sql, [row[2] for row in group])
            if self._tx_depth > 0:
                self._tx_rows.extend(batch)
                return len(batch)
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            self._pending = self._tx_rows + batch + self._pending
            self._tx_rows = []
            if len(self._pending) > MAX_PENDING_ROWS:
                dropped = len(self._pending) - MAX_PENDING_ROWS
                del self._pending[:dropped]
                log.error("store: dropped %d unwritten rows (database keeps failing)", dropped)
            raise
        committed, self._tx_rows = self._tx_rows + batch, []
        for table, _sql, _params in committed:
            self.rows_written[table] += 1
        return len(batch)

    def _before_read(self) -> None:
        if not self.readonly and self._pending:
            self.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._tx_depth = 0  # closing commits whatever a transaction left behind
        try:
            self.flush()
        except sqlite3.Error:
            log.exception("store: final flush failed; %d rows lost", len(self._pending))
        self._conn.close()
        self._closed = True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ writers

    def add_book(self, book: BookSnapshot) -> None:
        bids = _levels_json(book.bids, self.depth_levels)
        asks = _levels_json(book.asks, self.depth_levels)
        self._enqueue("books", _SQL_BOOK, (book.ts, book.symbol, book.source, bids, asks))

    def add_trade(self, trade: Trade) -> None:
        params = (trade.ts, trade.symbol, str(trade.price), str(trade.qty), trade.side)
        self._enqueue("trades", _SQL_TRADE, params)

    def upsert_candles(self, candles: Iterable[Candle]) -> int:
        """Insert or replace candles keyed by (symbol, resolution, ts). Idempotent."""
        n = 0
        for c in candles:
            params = (c.symbol, c.resolution, int(c.ts), str(c.o), str(c.h), str(c.l), str(c.c), str(c.v))
            self._enqueue("candles", _SQL_CANDLE, params)
            n += 1
        return n

    def add_balances(self, ts: float, balances: dict[str, Balance]) -> None:
        for asset in sorted(balances):
            b = balances[asset]
            self._enqueue("balances", _SQL_BALANCE, (ts, b.asset, str(b.free), str(b.locked)))

    def add_equity(
        self,
        ts: float,
        equity_tmn: Decimal,
        tmn: Decimal | None = None,
        usdt_tmn_value: Decimal | None = None,
        coins_tmn_value: Decimal | None = None,
        peak_tmn: Decimal | None = None,
        drawdown_pct: Decimal | None = None,
    ) -> None:
        values = (equity_tmn, tmn, usdt_tmn_value, coins_tmn_value, peak_tmn, drawdown_pct)
        params = (ts, *(_s(v) for v in values))
        self._enqueue("equity", _SQL_EQUITY, params)

    def upsert_order(self, order: Order) -> None:
        params = (order.client_id, order.model_dump_json(), order.updated_ts)
        self._enqueue("orders", _SQL_ORDER, params, immediate=True)

    def add_fill(self, fill: Fill) -> None:
        self._enqueue("fills", _SQL_FILL, (fill.order_client_id, fill.model_dump_json(), fill.ts), immediate=True)

    def upsert_position(self, pos: Position, open: bool, ts: float) -> None:
        """Save a position; ``open=False`` keeps the closed position for reports. Written immediately.

        ``ts`` comes from the caller's clock (``clock.now()``) so backtests and replays stay reproducible.
        """
        params = (pos.id, pos.model_dump_json(), int(open), ts)
        self._enqueue("positions", _SQL_POSITION, params, immediate=True)

    def add_plan(self, ts: float, plan_json: dict[str, Any], prompt_hash: str) -> None:
        self._enqueue("plans", _SQL_PLAN, (ts, _log_json(plan_json), prompt_hash))

    def add_event(self, ts: float, kind: str, data: dict[str, Any] | None = None) -> None:
        self._enqueue("events", _SQL_EVENT, (ts, kind, _log_json(data or {})))

    def kv_set(self, key: str, value: Any, ts: float) -> None:
        """Persist a JSON-able value (Decimals stay Decimals on read). Written immediately.

        ``ts`` comes from the caller's clock (``clock.now()``), never the wall clock.
        """
        text = json.dumps(_tag_decimals(value), separators=(",", ":"))
        self._enqueue("kv", _SQL_KV, (key, text, ts), immediate=True)

    def add_engine_book(self, book: BookSnapshot, mode: str) -> None:
        """A book the engine used in a fast tick (replay input). All levels are kept, not only
        ``depth_levels``: the paper exchange may have walked deeper than the recorder stores."""
        bids = _levels_json(book.bids, len(book.bids))
        asks = _levels_json(book.asks, len(book.asks))
        params = (book.ts, mode, book.symbol, book.source, bids, asks)
        self._enqueue("engine_books", _SQL_ENGINE_BOOK, params)

    def add_tick(self, ts: float, mode: str, data: dict[str, Any]) -> None:
        """One fast tick of the engine (replay input), e.g. ``{"ts": ..., "book_ts": {symbol: ts}}``."""
        self._enqueue("ticks", _SQL_TICK, (ts, mode, _log_json(data)))

    def kv_get(self, key: str, default: Any = None) -> Any:
        self._before_read()
        row = self._conn.execute("SELECT json FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0], object_hook=_untag_decimal)

    # ------------------------------------------------------------ readers: market data

    @staticmethod
    def _time_filter(from_ts: float | None, to_ts: float | None, column: str = "ts") -> tuple[str, list[Any]]:
        sql, params = "", []
        if from_ts is not None:
            sql += f" AND {column} >= ?"
            params.append(from_ts)
        if to_ts is not None:
            sql += f" AND {column} <= ?"
            params.append(to_ts)
        return sql, params

    @staticmethod
    def _row_to_book(row: tuple[Any, ...]) -> BookSnapshot:
        ts, symbol, source, bids, asks = row
        return BookSnapshot(
            symbol=symbol, ts=ts, source=source, bids=_levels_from_json(bids), asks=_levels_from_json(asks)
        )

    def iter_books(
        self, symbol: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterator[BookSnapshot]:
        """Books of one symbol in time order (``source`` preserved)."""
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        cur = self._conn.execute(
            "SELECT ts, symbol, source, bids_json, asks_json FROM books"
            f" WHERE symbol = ?{where} ORDER BY ts, rowid",
            [symbol, *params],
        )
        return (self._row_to_book(row) for row in cur)

    def iter_books_multi(
        self, symbols: Iterable[str], from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterator[BookSnapshot]:
        """Books of several symbols merged by ts (ties keep the order of ``symbols``)."""
        streams = [self.iter_books(s, from_ts, to_ts) for s in symbols]
        return heapq.merge(*streams, key=lambda b: b.ts)

    def latest_book(self, symbol: str) -> BookSnapshot | None:
        self._before_read()
        row = self._conn.execute(
            "SELECT ts, symbol, source, bids_json, asks_json FROM books"
            " WHERE symbol = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return None if row is None else self._row_to_book(row)

    def iter_engine_books(
        self, mode: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterator[BookSnapshot]:
        """Books the engine used in ``mode`` (all symbols), in the order they were stored."""
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        cur = self._conn.execute(
            "SELECT ts, symbol, source, bids_json, asks_json FROM engine_books"
            f" WHERE mode = ?{where} ORDER BY ts, rowid",
            [mode, *params],
        )
        return (self._row_to_book(row) for row in cur)

    def iter_ticks(
        self, mode: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """The engine's fast-tick rows of ``mode`` in time order (the dicts given to ``add_tick``)."""
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        cur = self._conn.execute(
            f"SELECT json FROM ticks WHERE mode = ?{where} ORDER BY ts, rowid", [mode, *params]
        )
        return (json.loads(text) for (text,) in cur)

    def iter_trades(
        self, symbol: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterator[Trade]:
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        cur = self._conn.execute(
            f"SELECT ts, symbol, price, qty, side FROM trades WHERE symbol = ?{where} ORDER BY ts, rowid",
            [symbol, *params],
        )
        return (
            Trade(symbol=sym, ts=ts, price=Decimal(price), qty=Decimal(qty), side=side)
            for ts, sym, price, qty, side in cur
        )

    def load_candles(
        self, symbol: str, resolution: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> list[Candle]:
        """Candles ordered by bar open time."""
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        rows = self._conn.execute(
            "SELECT symbol, resolution, ts, o, h, l, c, v FROM candles"
            f" WHERE symbol = ? AND resolution = ?{where} ORDER BY ts",
            [symbol, resolution, *params],
        ).fetchall()
        return [
            Candle(
                symbol=sym,
                resolution=res,
                ts=ts,
                o=Decimal(o),
                h=Decimal(h),
                l=Decimal(lo),
                c=Decimal(c),
                v=Decimal(v),
            )
            for sym, res, ts, o, h, lo, c, v in rows
        ]

    def latest_candle_ts(self, symbol: str, resolution: str) -> int | None:
        self._before_read()
        row = self._conn.execute(
            "SELECT MAX(ts) FROM candles WHERE symbol = ? AND resolution = ?", (symbol, resolution)
        ).fetchone()
        return None if row[0] is None else int(row[0])

    def latest_balances(self) -> dict[str, Balance]:
        """Balances from the most recent ``add_balances`` call ({} if none)."""
        self._before_read()
        rows = self._conn.execute(
            "SELECT asset, free, locked FROM balances WHERE ts = (SELECT MAX(ts) FROM balances)"
        ).fetchall()
        return {a: Balance(asset=a, free=Decimal(f), locked=Decimal(lk)) for a, f, lk in rows}

    # ------------------------------------------------------------ readers: engine state

    def latest_equity(self) -> dict[str, Any] | None:
        """Most recent equity row with Decimal values, or None."""
        self._before_read()
        row = self._conn.execute(
            "SELECT ts, equity_tmn, tmn, usdt_tmn_value, coins_tmn_value, peak_tmn, drawdown_pct"
            " FROM equity ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        keys = ("equity_tmn", "tmn", "usdt_tmn_value", "coins_tmn_value", "peak_tmn", "drawdown_pct")
        return {"ts": row[0], **{k: _d(v) for k, v in zip(keys, row[1:], strict=True)}}

    def max_peak_equity(self) -> Decimal | None:
        """Highest equity ever recorded (max of ``peak_tmn`` and ``equity_tmn``), exact Decimal."""
        self._before_read()
        peak: Decimal | None = None
        for peak_text, equity_text in self._conn.execute("SELECT peak_tmn, equity_tmn FROM equity"):
            for text in (peak_text, equity_text):
                if text is not None and (peak is None or Decimal(text) > peak):
                    peak = Decimal(text)
        return peak

    def load_open_positions(self) -> list[Position]:
        """Open positions ordered by entry time."""
        return self.load_positions(open_only=True)

    def load_positions(self, open_only: bool = False) -> list[Position]:
        """All positions (open and closed) ordered by entry time."""
        self._before_read()
        sql = "SELECT json FROM positions" + (" WHERE open = 1" if open_only else "")
        positions = [Position.model_validate_json(r[0]) for r in self._conn.execute(sql)]
        return sorted(positions, key=lambda p: (p.entry_ts, p.id))

    def load_orders(self, non_terminal_only: bool = False) -> list[Order]:
        """Orders by last update; ``non_terminal_only`` drops FILLED/CANCELED/REJECTED/EXPIRED."""
        self._before_read()
        rows = self._conn.execute("SELECT json FROM orders ORDER BY updated_ts, client_id").fetchall()
        orders = [Order.model_validate_json(r[0]) for r in rows]
        return [o for o in orders if not o.is_terminal] if non_terminal_only else orders

    def load_fills(self, from_ts: float | None = None, to_ts: float | None = None) -> list[Fill]:
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        rows = self._conn.execute(f"SELECT json FROM fills WHERE 1=1{where} ORDER BY ts, id", params).fetchall()
        return [Fill.model_validate_json(r[0]) for r in rows]

    def iter_events(self, kind: str | None = None, from_ts: float | None = None) -> Iterator[dict[str, Any]]:
        """Events as ``{"ts", "kind", "data"}`` in time order."""
        self._before_read()
        where, params = self._time_filter(from_ts, None)
        if kind is not None:
            where += " AND kind = ?"
            params.append(kind)
        cur = self._conn.execute(f"SELECT ts, kind, json FROM events WHERE 1=1{where} ORDER BY ts, rowid", params)
        return ({"ts": ts, "kind": k, "data": json.loads(text)} for ts, k, text in cur)

    def load_plans(self, from_ts: float | None = None, to_ts: float | None = None) -> list[dict[str, Any]]:
        """Plans as ``{"ts", "plan", "prompt_hash"}`` in time order."""
        self._before_read()
        where, params = self._time_filter(from_ts, to_ts)
        rows = self._conn.execute(
            f"SELECT ts, json, prompt_hash FROM plans WHERE 1=1{where} ORDER BY ts, rowid", params
        ).fetchall()
        return [{"ts": ts, "plan": json.loads(text), "prompt_hash": h} for ts, text, h in rows]

    # ------------------------------------------------------------ readers: stats

    def counts(self) -> dict[str, int]:
        """Row count of every table."""
        self._before_read()
        return {t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def book_counts_by_symbol(self, since_ts: float | None = None) -> dict[str, int]:
        self._before_read()
        where, params = self._time_filter(since_ts, None)
        rows = self._conn.execute(
            f"SELECT symbol, COUNT(*) FROM books WHERE 1=1{where} GROUP BY symbol ORDER BY symbol", params
        ).fetchall()
        return {sym: n for sym, n in rows}

    def time_range(self, table: str, symbol: str | None = None) -> tuple[float | None, float | None]:
        """``(min_ts, max_ts)`` of a table (optionally one symbol); ``(None, None)`` when empty."""
        if table not in _TS_COLUMN:
            raise ValueError(f"unknown table {table!r}")
        if symbol is not None and table not in _SYMBOL_TABLES:
            raise ValueError(f"table {table!r} has no symbol column")
        self._before_read()
        col = _TS_COLUMN[table]
        sql = f"SELECT MIN({col}), MAX({col}) FROM {table}"
        params: tuple[Any, ...] = ()
        if symbol is not None:
            sql += " WHERE symbol = ?"
            params = (symbol,)
        lo, hi = self._conn.execute(sql, params).fetchone()
        return lo, hi
