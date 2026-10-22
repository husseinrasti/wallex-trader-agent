"""SQLite store: exact Decimal round-trips, ordering, buffering, engine state and stats."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from wta.data.store import Store
from wta.models import Balance, Candle, Fill, Order, OrderRequest, Position, Trade

T0 = 1_792_663_200.0


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "wta.sqlite", flush_interval_s=1.0, flush_rows=200)
    yield s
    s.close()


def _candle(ts: int, close: str = "100", symbol: str = "BTCUSDT", res: str = "1") -> Candle:
    c = Decimal(close)
    return Candle(symbol=symbol, resolution=res, ts=ts, o=c, h=c + 1, l=c - 1, c=c, v=Decimal("0.123456789"))


def _order(cid: str, status: str, updated: float) -> Order:
    req = OrderRequest(
        client_id=cid,
        symbol="BTCUSDT",
        side="BUY",
        type="LIMIT",
        qty=Decimal("0.000123"),
        price=Decimal("82845.01"),
        strategy="S1",
        purpose="entry",
        decision_id=f"d_{cid}",
    )
    return Order(req=req, status=status, created_ts=updated, updated_ts=updated)


def _position(pid: str, entry_ts: float) -> Position:
    return Position(
        id=pid,
        strategy="S1",
        symbol="BTCUSDT",
        base="BTC",
        quote="USDT",
        qty=Decimal("0.000123"),
        entry_price=Decimal("82845.01"),
        entry_ts=entry_ts,
        stop_price=Decimal("82016.56"),
        meta={"mode": "revert"},
    )


def test_schema_is_idempotent_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    Store(path).close()
    s = Store(path)  # reopening must not fail (CREATE ... IF NOT EXISTS)
    assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert set(s.counts()) >= {"books", "trades", "candles", "equity", "orders", "positions", "kv"}
    s.close()


def test_book_roundtrip_keeps_decimals_source_and_depth(tmp_path: Path, make_book) -> None:
    s = Store(tmp_path / "db.sqlite", depth_levels=3)
    book = make_book("BTCUSDT", "82845.10", "82909.16", qty="0.123456", ts=T0, levels=5, level_step="0.01")
    s.add_book(book.model_copy(update={"source": "ws"}))
    got = list(s.iter_books("BTCUSDT"))
    assert len(got) == 1
    b = got[0]
    assert b.source == "ws" and b.ts == T0
    assert len(b.bids) == 3 and len(b.asks) == 3  # limited to depth_levels
    assert b.bids == book.bids[:3] and b.asks == book.asks[:3]
    assert b.best_bid == Decimal("82845.10") and str(b.best_bid) == "82845.10"  # trailing zero kept
    s.close()


def test_iter_books_order_bounds_and_latest(store: Store, make_book) -> None:
    for ts in (T0 + 2, T0, T0 + 1, T0 + 3):
        store.add_book(make_book("ETHTMN", 650_660_181 + int(ts - T0), 650_859_142, ts=ts))
    store.add_book(make_book("BTCTMN", 1, 2, ts=T0 + 10))
    assert [b.ts for b in store.iter_books("ETHTMN")] == [T0, T0 + 1, T0 + 2, T0 + 3]
    assert [b.ts for b in store.iter_books("ETHTMN", from_ts=T0 + 1, to_ts=T0 + 2)] == [T0 + 1, T0 + 2]
    latest = store.latest_book("ETHTMN")
    assert latest is not None and latest.ts == T0 + 3 and latest.best_bid == Decimal("650660184")
    assert store.latest_book("XRPTMN") is None


def test_iter_books_multi_merges_by_ts(store: Store, make_book) -> None:
    store.add_book(make_book("A", 1, 2, ts=T0 + 1))
    store.add_book(make_book("A", 1, 2, ts=T0 + 3))
    store.add_book(make_book("B", 1, 2, ts=T0))
    store.add_book(make_book("B", 1, 2, ts=T0 + 3))
    merged = [(b.symbol, b.ts) for b in store.iter_books_multi(["A", "B"])]
    assert merged == [("B", T0), ("A", T0 + 1), ("A", T0 + 3), ("B", T0 + 3)]
    assert [b.symbol for b in store.iter_books_multi(["A", "B"], from_ts=T0 + 2)] == ["A", "B"]


def test_trades_roundtrip(store: Store) -> None:
    store.add_trade(Trade(symbol="BTCUSDT", ts=T0 + 1, price=Decimal("82877.50"), qty=Decimal("0.0001"), side="SELL"))
    store.add_trade(Trade(symbol="BTCUSDT", ts=T0, price=Decimal("82877.49"), qty=Decimal("2E-8"), side=None))
    trades = list(store.iter_trades("BTCUSDT"))
    assert [t.ts for t in trades] == [T0, T0 + 1]
    assert trades[0].qty == Decimal("2E-8") and trades[0].side is None
    assert str(trades[1].price) == "82877.50" and trades[1].side == "SELL"


def test_candle_upsert_is_idempotent_and_ordered(store: Store) -> None:
    ts = int(T0)
    store.upsert_candles([_candle(ts + 120), _candle(ts), _candle(ts + 60)])
    store.upsert_candles([_candle(ts + 60, close="101.5")])  # re-fetched bar replaces the old one
    store.upsert_candles([_candle(ts + 60, close="101.5")])
    store.upsert_candles([_candle(ts, symbol="ETHUSDT")])
    got = store.load_candles("BTCUSDT", "1")
    assert [c.ts for c in got] == [ts, ts + 60, ts + 120]
    assert got[1].c == Decimal("101.5") and got[1].v == Decimal("0.123456789")
    assert store.counts()["candles"] == 4
    assert store.latest_candle_ts("BTCUSDT", "1") == ts + 120
    assert store.latest_candle_ts("BTCUSDT", "15") is None
    assert [c.ts for c in store.load_candles("BTCUSDT", "1", from_ts=ts + 60)] == [ts + 60, ts + 120]


def test_writes_are_buffered_until_flush(tmp_path: Path, make_book) -> None:
    path = tmp_path / "db.sqlite"
    s = Store(path, flush_interval_s=1.0, flush_rows=3)
    other = sqlite3.connect(path)
    s.add_book(make_book("A", 1, 2, ts=T0))
    s.add_book(make_book("A", 1, 3, ts=T0 + 1))
    assert s.pending_rows == 2
    assert other.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    assert s.maybe_flush(T0) == 2  # first timed flush
    assert other.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2
    s.add_book(make_book("A", 1, 4, ts=T0 + 2))
    assert s.maybe_flush(T0 + 0.5) == 0  # interval not elapsed
    assert s.maybe_flush(T0 + 1.0) == 1
    for i in range(3):  # reaching flush_rows flushes on its own
        s.add_book(make_book("A", 1, 5 + i, ts=T0 + 3 + i))
    assert s.pending_rows == 0
    assert s.rows_written["books"] == 6
    other.close()
    s.close()


def test_readers_flush_pending_writes(store: Store, make_book) -> None:
    store.add_book(make_book("A", 1, 2, ts=T0))
    assert store.pending_rows == 1
    assert store.book_counts_by_symbol() == {"A": 1}
    assert store.pending_rows == 0


def test_kv_roundtrip_keeps_decimals(store: Store) -> None:
    assert store.kv_get("missing", default={"x": 1}) == {"x": 1}
    state = {"peak": Decimal("5012345.67"), "counters": [1, 2], "killed": False, "ts": 1.5}
    store.kv_set("risk_state", state, ts=T0)
    assert store.kv_get("risk_state") == state
    assert isinstance(store.kv_get("risk_state")["peak"], Decimal)
    store.kv_set("risk_state", {"killed": True}, ts=T0 + 1)
    assert store.kv_get("risk_state") == {"killed": True}
    store.kv_set("day_start_equity", Decimal("4990000.5"), ts=T0 + 2)
    assert store.kv_get("day_start_equity") == Decimal("4990000.5")


def test_state_timestamps_come_from_the_caller_clock(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with Store(path) as s:
        s.kv_set("risk_state", {"killed": False}, ts=T0 + 5)
        s.upsert_position(_position("p_S1_1", T0), open=True, ts=T0 + 7)
        with pytest.raises(TypeError):
            s.kv_set("x", 1)  # type: ignore[call-arg]  # no wall-clock fallback (replays must be reproducible)
        with pytest.raises(TypeError):
            s.upsert_position(_position("p_S1_2", T0), open=True)  # type: ignore[call-arg]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT updated_ts FROM kv WHERE key = 'risk_state'").fetchone()[0] == T0 + 5
        assert conn.execute("SELECT updated_ts FROM positions WHERE id = 'p_S1_1'").fetchone()[0] == T0 + 7


def test_equity_latest_and_peak(store: Store) -> None:
    assert store.latest_equity() is None and store.max_peak_equity() is None
    rows = [("5000000", "5000000"), ("5100000.25", "5100000.25"), ("998000", "5100000.25")]
    for i, (eq, peak) in enumerate(rows):
        store.add_equity(
            T0 + i,
            Decimal(eq),
            tmn=Decimal("2500000"),
            usdt_tmn_value=Decimal("2500000"),
            coins_tmn_value=Decimal("0"),
            peak_tmn=Decimal(peak),
            drawdown_pct=Decimal("0.01"),
        )
    store.add_equity(T0 + 3, Decimal("999999.99"))  # optional fields may be missing
    latest = store.latest_equity()
    assert latest is not None and latest["ts"] == T0 + 3
    assert latest["equity_tmn"] == Decimal("999999.99") and latest["peak_tmn"] is None
    # "998000" > "5100000.25" as text; the peak must be compared numerically
    assert store.max_peak_equity() == Decimal("5100000.25")


def test_positions_open_and_closed(store: Store) -> None:
    p1, p2 = _position("p_S1_1", T0 + 10), _position("p_S1_2", T0)
    store.upsert_position(p1, open=True, ts=T0 + 10)
    store.upsert_position(p2, open=True, ts=T0)
    assert [p.id for p in store.load_open_positions()] == ["p_S1_2", "p_S1_1"]
    p2.qty = Decimal("0")
    store.upsert_position(p2, open=False, ts=T0 + 20)
    open_now = store.load_open_positions()
    assert [p.id for p in open_now] == ["p_S1_1"]
    assert open_now[0].stop_price == Decimal("82016.56") and open_now[0].meta == {"mode": "revert"}
    assert {p.id for p in store.load_positions()} == {"p_S1_1", "p_S1_2"}
    assert store.counts()["positions"] == 2


def test_orders_non_terminal_filter(store: Store) -> None:
    store.upsert_order(_order("S1_BTCUSDT_1792663200000_AAAA", "NEW", T0))
    store.upsert_order(_order("S1_BTCUSDT_1792663201000_BBBB", "FILLED", T0 + 1))
    store.upsert_order(_order("S1_BTCUSDT_1792663202000_CCCC", "PARTIALLY_FILLED", T0 + 2))
    store.upsert_order(_order("S1_BTCUSDT_1792663200000_AAAA", "CANCELED", T0 + 3))  # update in place
    assert len(store.load_orders()) == 3
    live = store.load_orders(non_terminal_only=True)
    assert [o.client_id for o in live] == ["S1_BTCUSDT_1792663202000_CCCC"]
    assert live[0].req.price == Decimal("82845.01")


def test_state_writes_are_immediate(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    s = Store(path, flush_rows=1000)
    s.upsert_order(_order("S2_USDTTMN_1792663200000_DDDD", "NEW", T0))
    fill = Fill(
        order_client_id="S2_USDTTMN_1792663200000_DDDD",
        symbol="USDTTMN",
        side="BUY",
        price=Decimal("266720"),
        qty=Decimal("1.5"),
        fee=Decimal("0.00375"),
        fee_asset="USDT",
        ts=T0,
    )
    s.add_fill(fill)
    other = sqlite3.connect(path)
    assert other.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert other.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    other.close()
    assert s.load_fills() == [fill]
    s.close()


def test_balances_plans_events(store: Store) -> None:
    store.add_balances(T0, {"TMN": Balance(asset="TMN", free=Decimal("1"))})
    store.add_balances(
        T0 + 1,
        {
            "TMN": Balance(asset="TMN", free=Decimal("2500000"), locked=Decimal("10")),
            "USDT": Balance(asset="USDT", free=Decimal("9.37")),
        },
    )
    bal = store.latest_balances()
    assert set(bal) == {"TMN", "USDT"} and bal["TMN"].locked == Decimal("10")
    store.add_plan(T0, {"usdt_target_share": Decimal("0.5"), "notes": "range"}, "abc123")
    store.add_plan(T0 + 300, {"usdt_target_share": "0.6"}, "def456")
    plans = store.load_plans(from_ts=T0 + 1)
    assert plans == [{"ts": T0 + 300, "plan": {"usdt_target_share": "0.6"}, "prompt_hash": "def456"}]
    assert store.load_plans()[0]["plan"]["usdt_target_share"] == "0.5"
    store.add_event(T0, "start", {"mode": "shadow"})
    store.add_event(T0 + 1, "pause", {"reason": "stale"})
    assert [e["kind"] for e in store.iter_events()] == ["start", "pause"]
    assert list(store.iter_events("pause")) == [{"ts": T0 + 1, "kind": "pause", "data": {"reason": "stale"}}]


def test_counts_and_time_range(store: Store, make_book) -> None:
    assert store.time_range("books") == (None, None)
    store.add_book(make_book("A", 1, 2, ts=T0))
    store.add_book(make_book("A", 1, 3, ts=T0 + 5))
    store.add_book(make_book("B", 1, 2, ts=T0 + 9))
    assert store.counts()["books"] == 3
    assert store.book_counts_by_symbol() == {"A": 2, "B": 1}
    assert store.book_counts_by_symbol(since_ts=T0 + 1) == {"A": 1, "B": 1}
    assert store.time_range("books") == (T0, T0 + 9)
    assert store.time_range("books", symbol="A") == (T0, T0 + 5)
    with pytest.raises(ValueError):
        store.time_range("books; DROP TABLE books")
    with pytest.raises(ValueError):
        store.time_range("equity", symbol="A")


def test_open_readonly_sees_writer_data_and_refuses_writes(tmp_path: Path, make_book) -> None:
    path = tmp_path / "db.sqlite"
    writer = Store(path)
    writer.add_book(make_book("A", "1.5", "2.5", ts=T0))
    writer.flush()
    reader = Store.open_readonly(path)
    assert [b.best_bid for b in reader.iter_books("A")] == [Decimal("1.5")]
    writer.add_book(make_book("A", "1.6", "2.5", ts=T0 + 1))
    writer.flush()
    latest = reader.latest_book("A")
    assert latest is not None and latest.ts == T0 + 1
    with pytest.raises(RuntimeError):
        reader.add_book(make_book("A", 1, 2, ts=T0))
    with pytest.raises(sqlite3.OperationalError):
        reader._conn.execute("INSERT INTO kv(key, json, updated_ts) VALUES ('x', '1', 0)")
    reader.close()
    writer.close()


def test_open_readonly_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        Store.open_readonly(tmp_path / "nope.sqlite")
    assert not (tmp_path / "nope.sqlite").exists()


# ---------------------------------------------------------------- transactions (one commit per tick)


def test_transaction_commits_once_and_reads_inside_see_the_writes(tmp_path) -> None:
    path = tmp_path / "tx.sqlite"
    store = Store(path)
    other = Store.open_readonly(path)
    with store.transaction():
        store.kv_set("a", {"x": 1}, 1.0)  # an "immediate" write waits for the end of the block
        assert store.kv_get("a") == {"x": 1}  # this connection sees it ...
        assert other.kv_get("a") is None  # ... other readers only after the commit
        with store.transaction():  # nested blocks join the outer one
            store.kv_set("b", {"y": 2}, 1.0)
        assert other.kv_get("b") is None
        assert store.maybe_flush(10_000.0) == 0  # timed flushes wait too
    assert other.kv_get("a") == {"x": 1} and other.kv_get("b") == {"y": 2}
    assert store.rows_written["kv"] == 2
    other.close()
    store.close()


def test_checkpoint_commits_inside_a_transaction(tmp_path) -> None:
    path = tmp_path / "cp.sqlite"
    store = Store(path)
    other = Store.open_readonly(path)
    with store.transaction():
        store.kv_set("before_send", {"ok": True}, 1.0)
        store.checkpoint()  # e.g. the write-ahead record right before an order is sent
        assert other.kv_get("before_send") == {"ok": True}
        store.kv_set("after", {"ok": True}, 2.0)
        assert other.kv_get("after") is None
    assert other.kv_get("after") == {"ok": True}
    other.close()
    store.close()


def test_a_crash_inside_a_transaction_leaves_the_previous_commit(tmp_path) -> None:
    """Nothing of an unfinished tick reaches the disk: a restart sees the last committed tick."""
    path = tmp_path / "crash.sqlite"
    store = Store(path)
    store.kv_set("portfolio_state", {"fees": "1"}, 1.0)
    store._tx_depth += 1  # inside a tick's transaction ...
    store.kv_set("portfolio_state", {"fees": "2"}, 2.0)
    store.kv_set("paper_state", {"tick": 2}, 2.0)
    store.kv_get("paper_state")  # (a read executes the rows, still uncommitted)
    store._conn.close()  # ... the process dies here
    reopened = Store(path)
    assert reopened.kv_get("portfolio_state") == {"fees": "1"}
    assert reopened.kv_get("paper_state") is None
    reopened.close()


def test_a_failed_commit_keeps_every_row_of_the_transaction_pending(tmp_path) -> None:
    store = Store(tmp_path / "fail.sqlite")
    real_commit = store._conn.commit
    calls = {"n": 0}

    class Conn:  # sqlite3.Connection attributes are read-only: wrap it
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            real_commit()

    store._conn = Conn(store._conn)
    with store.transaction(raise_errors=False):
        store.kv_set("k", {"v": 1}, 1.0)
        store.kv_get("k")  # executed inside the transaction
    assert store.pending_rows == 1  # rolled back, kept for the next flush
    assert store.flush() == 1
    assert store.kv_get("k") == {"v": 1}
    store.close()
