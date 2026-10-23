"""PRD-05 §3 / §8: the backtest runner end to end on tiny, hand-made (synthetic) datasets.

Every test drives the real ``Engine`` (FeatureEngine, StrategyRouter, S1/S2/S3, RiskManager,
OrderManager, positions, decision log) through ``backtest.runner.arun_backtest`` with the
``BacktestExchange`` books or candles driver and a simulated clock. The market databases are written
here (candles/books with the ``synthetic_market_db`` marker, so every report says SYNTHETIC) or by
``make_synthetic_market_db``. Nothing touches the network (``ai=on`` runs a scripted fake provider)
or the wall clock.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backtest.fills import synthetic_quotes
from backtest.runner import (
    SYNTHETIC_MARKER_KEY,
    arun_backtest,
    book_steps,
    candle_steps,
    make_synthetic_market_db,
    needed_symbols,
    parse_strategies,
    plan_steps,
    synthetic_marker,
)
from wta.ai.llm_client import LLMResponse
from wta.ai.plan_schema import PLAN_EXAMPLE
from wta.config import Config, Settings, load_config
from wta.data.store import Store
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
from wta.models import BookSnapshot, Candle, Level
from wta.utils.decimalutil import D, quantize_down

T = 1_790_812_800  # 2026-10-01T00:00:00Z, a multiple of 900 (15-minute bars line up with the window)
WARM = 26 * 60  # minutes of candles before an S3 window (the runner preloads them as warm-up)
U_MID = 266_700
SETTINGS = Settings(_env_file=None)

Row = tuple[float, float, float, float, float]  # o, h, l, c, v


# ---------------------------------------------------------------- datasets


def _q(x: float, places: int) -> Decimal:
    return quantize_down(D(repr(x)), places)


def write_db(
    path: Path,
    candles: dict[str, list[Row]] | None = None,
    books: list[BookSnapshot] | None = None,
    start: int = T,
    places: dict[str, int] | None = None,
) -> Path:
    """A market DB with 1-minute candles (``candles[symbol][i]`` opens at ``start + 60 i``) and books,
    marked as a synthetic test fixture."""
    places = places or {}
    with Store(path) as store:
        rows = [
            Candle(symbol=sym, resolution="1", ts=start + 60 * i, o=_q(o, places.get(sym, 2)), h=_q(h, places.get(sym, 2)),
                   l=_q(lo, places.get(sym, 2)), c=_q(c, places.get(sym, 2)), v=D(repr(v)))
            for sym, series in sorted((candles or {}).items())
            for i, (o, h, lo, c, v) in enumerate(series)
        ]
        store.upsert_candles(rows)
        for b in books or []:
            store.add_book(b)
        store.kv_set(SYNTHETIC_MARKER_KEY, {"synthetic": True, "source": "tests/test_backtest_runner.py fixture"}, start)
        store.flush()
    return path


def dollar_series(n: int, mid: float = U_MID, dip: float = 0.0003) -> list[Row]:
    """USDTTMN bars that dip ``dip`` below the close every minute (a resting maker BUY can fill)."""
    return [(mid, mid * 1.0001, mid * (1 - dip), mid, 1.0) for _ in range(n)]


def btc_breakout_series(n_window: int = 120) -> list[Row]:
    """26 h of a +2 % uptrend with ±0.15 % zigzag (ATR ~0.4 % per 15-minute bar), then in the window:
    30 min more trend, a 15-minute breakout bar (+0.8 %, 4x volume), 10 min of chop around the new
    level (the maker entry fills), a slide of -0.15 %/min for 15 min (the server stop fires) and chop."""
    rows: list[Row] = []
    prev: float | None = None
    top = 80_000 * (1 + 0.02 * (WARM + 30) / WARM)
    level = top * 1.008
    for m in range(WARM + n_window):
        w = m - WARM
        zig = 0.0015 if m % 2 == 0 else -0.0015
        vol = 1.0
        if w < 30:
            c = 80_000 * (1 + 0.02 * m / WARM) * (1 + zig)
        elif w < 45:
            c, vol = top + (level - top) * (w - 29) / 15, 4.0
        elif w < 55:
            c = level * (1 + zig)
        elif w < 70:
            c = level * (1 - 0.0015 * (w - 54))
        else:
            c = level * (1 - 0.0015 * 15) * (1 + zig)
        o = c if prev is None else prev
        rows.append((o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, vol))
        prev = c
    return rows


def btc_flat_series(n: int) -> list[Row]:
    return [(80_000.0, 80_010.0, 79_990.0, 80_000.0, 1.0) for _ in range(n)]


def make_book(symbol: str, ts: float, mid: float, spread: float, qty: str, places: int) -> BookSnapshot:
    half = mid * spread / 2
    return BookSnapshot(
        symbol=symbol, ts=ts, source="synthetic",
        bids=[Level(price=_q(mid - half, places), qty=D(qty))],
        asks=[Level(price=_q(mid + half + 10 ** -places, places), qty=D(qty))],
    )


def s3_cfg(**over: Any) -> Config:
    return load_config(env={}, overrides={"strategies.s3_momentum.allow_rules_only": True,
                                          "strategies.s3_momentum.symbols": ["BTCUSDT"], **over})


@pytest.fixture(scope="module")
def s3_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    n = WARM + 120
    return write_db(tmp_path_factory.mktemp("s3") / "market.sqlite",
                    {"BTCUSDT": btc_breakout_series(), "USDTTMN": dollar_series(n)}, places={"USDTTMN": 0})


@pytest.fixture(scope="module")
def dollar_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_db(tmp_path_factory.mktemp("dollar") / "market.sqlite", {"USDTTMN": dollar_series(9 * 60 + 5)},
                    places={"USDTTMN": 0})


async def bt(cfg: Config, **kwargs: Any) -> Any:
    kwargs.setdefault("settings", SETTINGS)
    return await arun_backtest(cfg, **kwargs)


def records(out: Path) -> list[dict[str, Any]]:
    return list(iter_family(out, "decisions", strict=True))


def merged(out: Path) -> list[dict[str, Any]]:
    return [dict(v) for v in merge_decisions(records(out)).values()]


def intents(out: Path, **match: Any) -> list[dict[str, Any]]:
    out_ = []
    for rec in merged(out):
        intent = rec.get("intent") or {}
        if rec["kind"] != "plan" and all(intent.get(k) == v or rec.get(k) == v for k, v in match.items()):
            out_.append(rec)
    return out_


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- helpers (pure)


def test_parse_strategies_and_needed_symbols() -> None:
    assert parse_strategies("s3, S2") == ("S2", "S3")
    assert parse_strategies(None, "books") == ("S1", "S2")
    assert parse_strategies(None, "candles") == ("S2", "S3")
    with pytest.raises(ValueError, match="unknown strategies S9"):
        parse_strategies("S2,S9")
    with pytest.raises(ValueError, match="at least one"):
        parse_strategies(" , ")
    cfg = s3_cfg()
    assert needed_symbols(cfg, ["S2"]) == ["USDTTMN"]
    assert needed_symbols(cfg, ["S3"]) == ["BTCTMN", "BTCUSDT", "USDTTMN"]  # + the TMN pair: flatten / valuation
    s1 = needed_symbols(load_config(env={}, overrides={"strategies.s1_crossbook.coins": ["ETH"]}), ["S1"])
    assert s1 == ["ETHTMN", "ETHUSDT", "USDTTMN"]


def test_step_grids() -> None:
    # books: each 2-s cell with a snapshot is ticked once, at its end; never past the window end
    assert book_steps([10.0, 10.3, 11.9, 12.0, 15.1, 99.0], 2.0, 50.0) == [10.0, 12.0, 16.0, 50.0]
    # candles: a tick at every bar close inside the window
    assert candle_steps([0, 60, 120, 180], 60.0, 180.0) == [60.0, 120.0, 180.0]


# ---------------------------------------------------------------- candle driver: S2, equity math


async def test_candles_s2_initial_allocation_fills_and_equity_math(tmp_path: Path, dollar_db: Path) -> None:
    cfg = load_config(env={})
    out = tmp_path / "s2"
    # 25 minutes: one rebalance (the next is allowed after s2 min_rebalance_interval_s = 30 min)
    res = await bt(cfg, driver="candles", from_ts=T + 60, to_ts=T + 1500, strategies="S2", ai="off",
                   market_db=dollar_db, out_dir=out)
    m, meta = res.metrics, res.meta
    assert meta["driver"] == "candles" and meta["strategies"] == ["S2"] and meta["synthetic"] is True
    assert meta["ticks"] == 25 and meta["coverage_hours"] == pytest.approx(25 / 60, abs=1e-4)
    assert meta["tick_crashes"] == 0 and meta["ai_source"] == "disabled"
    (rec,) = intents(out, strategy="S2")
    intent = rec["intent"]
    assert rec["kind"] == "order" and rec["order"]["accepted"] is True and rec["risk"]["approved"] is True
    assert intent["purpose"] == "rebalance" and intent["side"] == "BUY" and intent["symbol"] == "USDTTMN"
    assert "initial allocation" in intent["reason"]
    out_rec = rec["outcome"]
    assert out_rec["status"] == "FILLED" and out_rec["fee_asset"] == "USDT"
    # the maker BUY was placed on one bar and filled on a LATER one (every order acts one step later)
    assert out_rec["done_ts"] > intent["ts"]
    qty, price, fee = D(out_rec["filled_qty"]), D(out_rec["avg_price"]), D(out_rec["fee_paid"])
    # equity math: balances from the fill, valued at the last synthetic book's bid
    tmn = D("5000000") - qty * price
    usdt = qty - fee
    spread = D(meta["spreads"]["USDTTMN"]["spread_pct"])
    bid, _ = synthetic_quotes(D(U_MID), spread, 0)
    rows = read_csv(out / "equity.csv")
    last = rows[-1]
    assert D(last["tmn"]) == tmn
    assert D(last["usdt_tmn_value"]) == usdt * bid
    assert D(last["equity_tmn"]) == tmn + usdt * bid
    assert m.initial_equity_tmn == 5_000_000
    assert m.final_equity_tmn == pytest.approx(float(tmn + usdt * bid))
    assert m.return_pct == pytest.approx(float((tmn + usdt * bid) / D("5000000") - 1))
    assert m.fees_paid_tmn == pytest.approx(float(fee * price), rel=1e-5)  # the USDT fee at the bid of the fill
    assert m.n_orders == 1 and m.n_trades == 0
    assert D("0.20") < usdt * bid / (tmn + usdt * bid) < D("0.30")  # MAX_ORDER_PCT capped the first step at 25 %
    # outputs
    for name in ("report.md", "summary.json", "equity.csv", "trades.csv", "equity.png", "drawdown.png",
                 "vetoes.png", "regimes.png", "decisions.jsonl", "orders.jsonl", "events.jsonl", "health.jsonl",
                 "state.sqlite"):
        assert (out / name).exists(), name
    report = (out / "report.md").read_text()
    assert "SYNTHETIC DATA — not evidence of performance" in report
    assert "S1 not backtested on candles" in report and "Candle window = 0.4 h" in report
    summary = json.loads((out / "summary.json").read_text())
    assert summary["meta"]["config_hash"] == rec["config_hash"] and summary["meta"]["git_commit"]
    assert check_logs(out).ok
    # sparse heartbeats: at the start and at the end (hourly in between)
    assert len(list(iter_family(out, "health"))) == 2


async def test_no_signal_run_keeps_flat_equity(tmp_path: Path) -> None:
    db = write_db(tmp_path / "flat.sqlite", {"BTCUSDT": btc_flat_series(120), "USDTTMN": dollar_series(120)},
                  places={"USDTTMN": 0})
    res = await bt(s3_cfg(), driver="candles", from_ts=T + 60, to_ts=T + 7200, strategies="S3", ai="off",
                   market_db=db, out_dir=tmp_path / "out")
    m = res.metrics
    assert m.n_orders == 0 and m.n_trades == 0 and m.fees_paid_tmn == 0
    assert m.return_pct == 0 and m.max_drawdown_pct == 0 and m.final_equity_tmn == 5_000_000
    assert any("No candles before the window" in x for x in res.limitations)


# ---------------------------------------------------------------- S3 breakout -> one trade with a stop


async def test_s3_breakout_produces_one_trade_with_a_server_stop(tmp_path: Path, s3_db: Path) -> None:
    out = tmp_path / "s3"
    res = await bt(s3_cfg(), driver="candles", from_ts=T + WARM * 60, to_ts=T + (WARM + 120) * 60, strategies="S3",
                   ai="off", market_db=s3_db, out_dir=out, initial_balances={"TMN": 2_500_000, "USDT": "9.4"})
    assert res.meta["warmup_candles"] > 2 * 24 * 60  # 26 h of both symbols preloaded
    entries = intents(out, strategy="S3", purpose="entry")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "order" and entry["order"]["accepted"] is True
    assert entry["intent"]["side"] == "BUY" and entry["intent"]["symbol"] == "BTCUSDT"
    reason = entry["intent"]["reason"]
    assert "above the previous 20 bars' high" in reason and "4.00x their average volume" in reason
    assert "rules-only regime trend_up" in reason and "server STOP_LIMIT stop" in reason
    stop_price = D(entry["intent"]["stop_price"])
    assert stop_price < D(entry["intent"]["price"])
    stops = intents(out, strategy="S3", purpose="stop")
    assert len(stops) == 1 and stops[0]["order"]["request"]["type"] == "STOP_LIMIT"
    assert D(stops[0]["order"]["request"]["stop_price"]) == stop_price
    trades = read_csv(out / "trades.csv")
    assert len(trades) == 1
    (t,) = trades
    assert t["strategy"] == "S3" and t["symbol"] == "BTCUSDT" and t["exit_reason"] == "stop"
    assert float(t["pnl_tmn"]) < 0
    m = res.metrics
    assert m.n_trades == 1 and m.losses == 1 and m.exit_reasons == {"stop": 1}
    assert m.per_strategy["S3"]["n_trades"] == 1
    orders_log = (out / "orders.jsonl").read_text()
    assert "STOP_LIMIT" in orders_log
    assert check_logs(out).ok


async def test_rerun_is_deterministic_and_replaces_its_own_logs(tmp_path: Path, s3_db: Path) -> None:
    kwargs: dict[str, Any] = dict(driver="candles", from_ts=T + WARM * 60, to_ts=T + (WARM + 80) * 60, strategies="S3",
                                  ai="off", market_db=s3_db, initial_balances={"TMN": 2_500_000, "USDT": "9.4"})
    a = await bt(s3_cfg(), out_dir=tmp_path / "a", **kwargs)
    b = await bt(s3_cfg(), out_dir=tmp_path / "b", **kwargs)
    da = (tmp_path / "a" / "decisions.jsonl").read_bytes()
    assert da == (tmp_path / "b" / "decisions.jsonl").read_bytes() and da
    assert (tmp_path / "a" / "equity.csv").read_bytes() == (tmp_path / "b" / "equity.csv").read_bytes()
    assert a.metrics.to_dict() == b.metrics.to_dict()
    # a rerun into the same folder starts from scratch (no appended lines)
    await bt(s3_cfg(), out_dir=tmp_path / "a", **kwargs)
    assert (tmp_path / "a" / "decisions.jsonl").read_bytes() == da


async def test_refuses_an_out_dir_with_foreign_logs(tmp_path: Path, dollar_db: Path) -> None:
    out = tmp_path / "logs"
    out.mkdir()
    (out / "decisions-20261001.jsonl").write_text('{"kind": "order"}\n')
    with pytest.raises(ValueError, match="did not write"):
        await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 600, strategies="S2",
                 market_db=dollar_db, out_dir=out)
    assert (out / "decisions-20261001.jsonl").read_text() == '{"kind": "order"}\n'


async def test_s1_is_never_backtested_on_candles(tmp_path: Path, dollar_db: Path) -> None:
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 600, strategies="S1,S2",
                   market_db=dollar_db, out_dir=tmp_path / "out")
    assert res.meta["strategies"] == ["S2"] and res.meta["strategies_requested"] == ["S1", "S2"]
    assert any("S1 was requested but is not backtested on candles" in x for x in res.limitations)
    with pytest.raises(ValueError, match="S1 cannot be backtested on candles"):
        await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 600, strategies="S1",
                 market_db=dollar_db, out_dir=tmp_path / "out2")


async def test_flatten_at_end_leaves_only_tmn(tmp_path: Path, dollar_db: Path) -> None:
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 9 * 3600, strategies="S2",
                   market_db=dollar_db, out_dir=tmp_path / "out")
    assert res.meta["flatten_at_end"] is True  # a 9-h window flattens in its last 2 h by default
    rvu = res.metrics.realized_vs_unrealized
    assert rvu["flat"] is True and abs(rvu["unrealized_tmn"]) < 50_000  # dust below USDTTMN's min notional
    kinds = [e["data"].get("kind") for e in iter_family(tmp_path / "out", "events") if e["kind"] == "flatten"]
    assert "flatten_start" in kinds and "flatten_final" in kinds
    assert any("deadline flatten ran" in x for x in res.limitations)


async def test_empty_window_writes_an_insufficient_data_report(tmp_path: Path, dollar_db: Path) -> None:
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 30 * 86400, to_ts=T + 31 * 86400,
                   strategies="S2", market_db=dollar_db, out_dir=tmp_path / "out")
    assert res.meta["ticks"] == 0 and res.metrics.insufficient_data
    assert "INSUFFICIENT DATA" in (tmp_path / "out" / "report.md").read_text()


# ---------------------------------------------------------------- books driver


async def test_books_driver_on_a_synthetic_db(tmp_path: Path) -> None:
    info = make_synthetic_market_db(tmp_path / "synth.sqlite", hours=0.1, seed=3, step_s=2)
    assert info["books"] > 0 and info["candles"] > 0 and not info["reused"]
    out = tmp_path / "out"
    res = await bt(load_config(env={}), driver="books", from_ts=info["from_ts"], to_ts=info["to_ts"],
                   strategies="S1,S2", market_db=info["path"], out_dir=out)
    meta = res.meta
    assert meta["synthetic"] is True and meta["data_source"].startswith("SYNTHETIC")
    assert meta["ticks"] == 181 and meta["snapshots"] == info["books"]
    assert meta["s1_coverage_hours"] > 0
    (rec,) = intents(out, strategy="S2")
    assert rec["kind"] == "order" and rec["outcome"]["status"] == "FILLED"
    assert rec["outcome"]["done_ts"] > rec["intent"]["ts"]  # matched on a newer snapshot only
    start = next(e for e in iter_family(out, "events") if e["kind"] == "start")
    assert start["data"]["mode"] == "backtest" and start["data"]["feed"] == "synthetic"
    assert start["data"]["exchange"] == "backtest" and start["data"]["synthetic"] is True
    hb = list(iter_family(out, "health"))[-1]
    assert not any("feed: no fresh data" in r for r in hb["reasons"])
    assert "SYNTHETIC DATA" in (out / "report.md").read_text()
    assert check_logs(out).ok


async def test_books_driver_skips_gaps_and_drops_stale_books(tmp_path: Path) -> None:
    books = []
    for start in (T, T + 3600):  # two 2-minute recording windows, an hour apart
        for k in range(61):
            ts = start + 2.0 * k
            books.append(make_book("USDTTMN", ts, U_MID, 0.0002, "500", 0))
            if start == T and k <= 30:  # BTCUSDT only during the first minute
                books.append(make_book("BTCUSDT", ts, 80_000, 0.0008, "0.05", 2))
    db = write_db(tmp_path / "books.sqlite", books=books)
    cfg = load_config(env={}, overrides={"strategies.s1_crossbook.coins": ["BTC"]})
    plan = plan_steps(cfg, driver="books", from_ts=T, to_ts=T + 7200, strategies="S1,S2", market_db=db)
    assert len(plan.steps) == 122 and plan.data_symbols == ["BTCUSDT", "USDTTMN"]
    assert plan.missing_symbols == ["BTCTMN"]
    res = await bt(cfg, driver="books", from_ts=T, to_ts=T + 7200, strategies="S1,S2", market_db=db,
                   out_dir=tmp_path / "out")
    meta = res.meta
    assert meta["ticks"] == 122 and meta["coverage_hours"] == pytest.approx(122 * 2 / 3600, abs=1e-4)
    by_sym = meta["coverage_hours_by_symbol"]
    assert by_sym["USDTTMN"] == pytest.approx(122 * 2 / 3600, abs=1e-4)
    # BTCUSDT's last snapshot is at +60 s: visible while <= stale_data_s (30 s) old, then removed
    assert by_sym["BTCUSDT"] == pytest.approx(46 * 2 / 3600, abs=1e-4)
    assert meta["s1_coverage_hours"] == 0  # never a TMN book next to the USDT one
    assert any("Book window = 0.07 h" in x for x in res.limitations)
    assert "requested window is 2.0 h" in (tmp_path / "out" / "report.md").read_text()


async def test_books_driver_an_order_resting_before_a_recording_gap_is_not_filled_after_it(tmp_path: Path) -> None:
    def book(ts: float, bid: int, ask: int) -> BookSnapshot:
        return BookSnapshot(symbol="USDTTMN", ts=ts, source="synthetic", bids=[Level(price=D(bid), qty=D("500"))],
                            asks=[Level(price=D(ask), qty=D("500"))])

    # 6 s of books, a 1-hour recording gap, then an ask BELOW S2's resting maker BUY
    books = [book(T + 2 * k, 266_700, 266_760) for k in range(4)]
    books += [book(T + 3600 + 2 * k, 266_500, 266_560) for k in range(30)]
    db = write_db(tmp_path / "gap.sqlite", books=books)
    out = tmp_path / "out"
    res = await bt(load_config(env={}), driver="books", from_ts=T, to_ts=T + 3700, strategies="S2", market_db=db,
                   out_dir=out)
    assert res.meta["orders_stopped_by_gaps"] == 1 and res.meta["ticks"] == 34
    orders = list(iter_family(out, "orders"))
    first = next(o for o in orders if o["action"] == "place")["client_id"]
    mine = [o for o in orders if o["client_id"] == first]
    # live, the order would have been re-priced or canceled long before the next snapshot: no fill an
    # hour later (before the fix it filled at 01:00:00, 3,600 s after placement)
    assert not any(o["action"] == "fill" for o in mine)
    cancel = next(o for o in mine if o["action"] == "cancel")
    assert cancel["ts"] == "2026-10-01T01:00:00.000Z" and cancel["data"]["why"] == "EXPIRED_UNFILLED"
    assert any("1 order(s) were open across a recording gap" in x for x in res.limitations)


async def test_candle_spreads_come_only_from_books_up_to_the_window_end(tmp_path: Path) -> None:
    # recorded books AFTER the window (wide spread) must not calibrate the window's synthetic book
    inside = [make_book("USDTTMN", T + 60 * k, U_MID, 0.0002, "500", 0) for k in range(10)]
    after = [make_book("USDTTMN", T + 7200 + 60 * k, U_MID, 0.01, "500", 0) for k in range(50)]
    db = write_db(tmp_path / "m.sqlite", {"USDTTMN": dollar_series(60)}, books=inside + after,
                  places={"USDTTMN": 0})
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 1800, strategies="S2",
                   market_db=db, out_dir=tmp_path / "out")
    info = res.meta["spreads"]["USDTTMN"]
    assert info["source"] == "observed" and info["books"] == 10
    assert info["from"] == "2026-10-01T00:00:00.000Z" and info["to"] == "2026-10-01T00:09:00.000Z"
    assert D(info["spread_pct"]) < D("0.001")  # the 1 % spreads recorded after the window are not used
    text = (tmp_path / "out" / "report.md").read_text()
    assert "the median spread of recorded books (10 books" in text and "none after the window end" in text
    # no book at or before the window end: the reference spread, and the report says so
    late = write_db(tmp_path / "late.sqlite", {"USDTTMN": dollar_series(60)}, books=after, places={"USDTTMN": 0})
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 1800, strategies="S2",
                   market_db=late, out_dir=tmp_path / "out_late")
    assert res.meta["spreads"]["USDTTMN"]["source"] == "reference"


async def test_report_date_is_never_the_data_end(tmp_path: Path, dollar_db: Path) -> None:
    from wta.utils import gitinfo

    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 1500, strategies="S2",
                   market_db=dollar_db, out_dir=tmp_path / "a")
    meta = res.meta
    assert "generated_at" not in meta and meta["data_end"] == "2026-10-01T00:25:00.000Z"
    assert meta["report_date"] == gitinfo.commit_date() and meta["report_date_source"].startswith("commit date of ")
    text = (tmp_path / "a" / "report.md").read_text()
    assert "| Data end (UTC) | 2026-10-01T00:25:00Z |" in text
    assert f"| Report date | {gitinfo.commit_date()} (commit date of " in text
    summary = json.loads((tmp_path / "a" / "summary.json").read_text())
    assert summary["report_date"] == gitinfo.commit_date()
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 1500, strategies="S2",
                   market_db=dollar_db, out_dir=tmp_path / "b", report_date="2026-10-23")
    assert res.meta["report_date"] == "2026-10-23" and res.meta["report_date_source"] == "given with --report-date"
    assert "| Report date | 2026-10-23 (given with --report-date) |" in (tmp_path / "b" / "report.md").read_text()
    with pytest.raises(ValueError, match="report date"):
        await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 1500, strategies="S2",
                 market_db=dollar_db, out_dir=tmp_path / "c", report_date="yesterday")


# ---------------------------------------------------------------- AI


async def test_ai_offline_plans_at_simulated_times_capped_and_cached(tmp_path: Path, dollar_db: Path) -> None:
    out = tmp_path / "out"
    res = await bt(load_config(env={}), driver="candles", from_ts=T + 60, to_ts=T + 3600, strategies="S2",
                   ai="offline", ai_max_calls=3, market_db=dollar_db, out_dir=out,
                   settings=Settings(_env_file=None, openrouter_api_key="sk-or-never-used"))
    meta = res.meta
    assert meta["ai_source"] == "offline-rules"  # the key is removed for ai=offline: never an LLM call
    assert meta["analyst_cycles"] == 3 and meta["analyst_plans"] == 3 and meta["ai_capped_at"] is not None
    plans = [r for r in records(out) if r["kind"] == "plan"]
    assert len(plans) == 3
    assert all("offline" in p["plan_snapshot"]["model"] for p in plans)
    ts = [p["plan_snapshot"]["ts"] for p in plans]
    assert ts == sorted(ts) and ts[1] - ts[0] >= 300  # engine.slow_loop_interval_s of simulated time
    assert res.metrics.n_plans == 3
    assert (out / "llm_cache").is_dir() and any((out / "llm_cache").iterdir())
    assert any("ai=offline" in x and "NOT an LLM" in x for x in res.limitations)


class _ScriptedLLM:
    """A fake provider under the configured analyst provider name: always answers with a valid plan."""

    name = "openrouter"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise AssertionError("the rerun must be served from the LLM cache")
        return LLMResponse(text=json.dumps(PLAN_EXAMPLE), model=model, provider=self.name, latency_ms=1,
                           usage={"prompt_tokens": 10, "completion_tokens": 10})


async def test_ai_on_caps_cycles_and_a_rerun_needs_no_provider(tmp_path: Path, dollar_db: Path) -> None:
    cfg = load_config(env={}, overrides={"llm.analyst.model": "fake/analyst-1", "llm.reviewer.model": "fake/r-1"})
    out = tmp_path / "out"
    kwargs: dict[str, Any] = dict(driver="candles", from_ts=T + 60, to_ts=T + 1800, strategies="S2", ai="on",
                                  ai_max_calls=2, market_db=dollar_db, out_dir=out)
    llm = _ScriptedLLM()
    first = await bt(cfg, ai_providers={"openrouter": llm}, **kwargs)
    assert first.meta["ai_source"] == "llm" and first.meta["analyst_cycles"] == 2 and llm.calls == 2
    plans1 = [r["plan_snapshot"] for r in records(out) if r["kind"] == "plan"]
    replay = _ScriptedLLM(fail=True)
    second = await bt(cfg, ai_providers={"openrouter": replay}, **kwargs)
    plans2 = [r["plan_snapshot"] for r in records(out) if r["kind"] == "plan"]
    assert replay.calls == 0 and second.meta["cached_calls"] == 2
    assert [p["prompt_hash"] for p in plans1] == [p["prompt_hash"] for p in plans2]
    assert [{k: v for k, v in p.items() if k not in ("cached", "latency_ms")} for p in plans1] == [
        {k: v for k, v in p.items() if k not in ("cached", "latency_ms")} for p in plans2
    ]
    assert any("at most 2 cycles per run" in x for x in second.limitations)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


async def test_ai_cache_reruns_a_cached_run_without_key_or_provider(tmp_path: Path, dollar_db: Path) -> None:
    """``--ai cache``: routes from the cache, refusing providers, a strict and untouched cache."""
    from wta.ai.cache import CacheMissError

    recorded = load_config(env={}, overrides={"llm.analyst.model": "fake/analyst-1"})
    kwargs: dict[str, Any] = dict(driver="candles", from_ts=T + 60, to_ts=T + 1800, strategies="S2",
                                  ai_max_calls=2, market_db=dollar_db)
    first = await bt(recorded, ai="on", ai_providers={"openrouter": _ScriptedLLM()}, out_dir=tmp_path / "on", **kwargs)
    cache = tmp_path / "on" / "llm_cache"
    before = _tree(cache)
    plain = load_config(env={}, overrides={"ai.cache_dir": str(cache)})  # no model configured, no key
    assert plain.llm.analyst.model is None
    res = await bt(plain, ai="cache", out_dir=tmp_path / "cache", settings=Settings(_env_file=None), **kwargs)
    meta = res.meta
    assert meta["ai"] == "cache" and meta["ai_source"] == "llm"
    assert meta["ai_cache_routes"] == ["openrouter/fake/analyst-1"] and meta["provider_calls"] == 0
    assert meta["model_calls"] == meta["cached_calls"] == first.meta["model_calls"] == 2
    plans = [{k: v for k, v in r["plan_snapshot"].items() if k not in ("cached", "latency_ms")}
             for r in records(tmp_path / "cache") if r["kind"] == "plan"]
    recorded_plans = [{k: v for k, v in r["plan_snapshot"].items() if k not in ("cached", "latency_ms")}
                      for r in records(tmp_path / "on") if r["kind"] == "plan"]
    assert plans == recorded_plans and res.metrics.return_pct == first.metrics.return_pct
    assert _tree(cache) == before  # read only: neither answers nor the cycle tape were rewritten
    assert any("ai=cache" in x and "no provider was called" in x for x in res.limitations)
    assert "| AI | cache (recorded LLM answers, no provider call) |" in (tmp_path / "cache" / "report.md").read_text()
    # another window asks other questions: a miss stops the run, nothing is called or written
    with pytest.raises(CacheMissError):
        await bt(plain, ai="cache", out_dir=tmp_path / "miss", settings=Settings(_env_file=None),
                 **{**kwargs, "from_ts": T + 600})
    assert _tree(cache) == before
    empty = load_config(env={}, overrides={"ai.cache_dir": str(tmp_path / "nothing")})
    with pytest.raises(ValueError, match="no cached LLM answers"):
        await bt(empty, ai="cache", out_dir=tmp_path / "none", **kwargs)


# ---------------------------------------------------------------- synthetic market DB


def test_synthetic_db_marker_reuse_and_refusal(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    a = make_synthetic_market_db(path, hours=0.05, seed=1, step_s=2)
    marker = synthetic_marker(path)
    assert marker is not None and marker["complete"] and marker["seed"] == 1 and marker["synthetic"] is True
    assert "not evidence" in marker["label"]
    with Store.open_readonly(path) as store:
        books = list(store.iter_books("USDTTMN"))
    assert books and {b.source for b in books} == {"synthetic"}
    again = make_synthetic_market_db(path, hours=0.05, seed=1, step_s=2)
    assert again["reused"] is True and again["books"] == a["books"]
    other = make_synthetic_market_db(path, hours=0.05, seed=2, step_s=2)
    assert other["reused"] is False and synthetic_marker(path)["seed"] == 2
    # candle-only mode (long candle backtests): no books, 1-minute bars of every symbol
    c = make_synthetic_market_db(tmp_path / "c.sqlite", hours=1, seed=1, step_s=10, with_books=False)
    assert c["books"] == 0 and c["candles"] == 7 * 60
    # never overwrite a database without the marker (e.g. the recorder's data/wta.sqlite)
    real = tmp_path / "real.sqlite"
    with Store(real) as store:
        store.add_event(1.0, "start", {})
    before = real.read_bytes()
    with pytest.raises(ValueError, match="not a synthetic market database"):
        make_synthetic_market_db(real, hours=0.05, seed=1)
    assert real.read_bytes() == before


def test_synthetic_db_is_deterministic(tmp_path: Path) -> None:
    def rows(p: Path) -> list[Any]:
        with Store.open_readonly(p) as store:
            return [(c.ts, c.o, c.h, c.l, c.c, c.v) for c in store.load_candles("BTCUSDT", "1")]

    make_synthetic_market_db(tmp_path / "a.sqlite", hours=0.2, seed=5, step_s=10, with_books=False)
    make_synthetic_market_db(tmp_path / "b.sqlite", hours=0.2, seed=5, step_s=10, with_books=False)
    assert rows(tmp_path / "a.sqlite") == rows(tmp_path / "b.sqlite") != []


def test_missing_market_db_is_a_clear_error(tmp_path: Path) -> None:
    from backtest.runner import run_backtest

    with pytest.raises(FileNotFoundError, match="does not exist"):
        run_backtest(load_config(env={}), driver="candles", from_ts=T, to_ts=T + 600, market_db=tmp_path / "x.sqlite",
                     out_dir=tmp_path / "out", settings=SETTINGS)


ArgCheck = Callable[..., Any]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"driver": "ticks"}, "unknown driver"),
        ({"ai": "maybe"}, "unknown ai mode"),
        ({"from_ts": T + 600, "to_ts": T}, "window is empty"),
        ({"from_ts": "yesterday"}, "not an ISO-8601 time"),
        ({"initial_equity_tmn": 0}, "initial equity"),
    ],
)
async def test_bad_arguments(tmp_path: Path, dollar_db: Path, kwargs: dict[str, Any], message: str) -> None:
    args: dict[str, Any] = dict(driver="candles", from_ts=T, to_ts=T + 600, market_db=dollar_db, out_dir=tmp_path / "o")
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await bt(load_config(env={}), **args)


async def test_state_db_outside_the_out_dir(tmp_path: Path, dollar_db: Path) -> None:
    state = tmp_path / "scratch" / "bt_state.sqlite"
    kwargs: dict[str, Any] = dict(driver="candles", from_ts=T + 60, to_ts=T + 600, strategies="S2", market_db=dollar_db,
                                  out_dir=tmp_path / "out", state_db=state)
    res = await bt(load_config(env={}), **kwargs)
    assert state.is_file() and not (tmp_path / "out" / "state.sqlite").exists()
    assert res.paths["state_db"] == state and len(read_csv(tmp_path / "out" / "equity.csv")) == res.meta["ticks"]
    await bt(load_config(env={}), **kwargs)  # a backtest state DB is replaced by the rerun
    foreign = tmp_path / "state_shadow.sqlite"
    with Store(foreign) as store:
        store.add_event(1.0, "start", {"mode": "shadow"})
    with pytest.raises(ValueError, match="not a backtest state database"):
        await bt(load_config(env={}), **{**kwargs, "state_db": foreign})
    assert foreign.is_file()
