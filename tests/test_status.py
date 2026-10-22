"""``python -m wta status`` summary (PRD-06 §2): ``build_status`` + ``format_status``.

Every fixture is written by the real writers (HealthLog, DecisionLog, EventsLog, Store) into
temporary directories; market data comes from the deterministic SyntheticFeed. Nothing reads the
wall clock, the network or the developer's ``logs/`` / ``data/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import orjson
import pytest

from wta.config import Config
from wta.data.store import Store
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot, snapshot_reference
from wta.logging_.decision_log import DecisionLog, EventsLog
from wta.logging_.health_log import HealthLog
from wta.models import (
    BookSnapshot,
    HealthRecord,
    Intent,
    Level,
    Order,
    OrderRequest,
    Position,
    RiskCheck,
    RiskDecision,
)
from wta.status import (
    OFFLINE_TEXT,
    SYNTHETIC_WARNING,
    build_status,
    format_status,
    resolve_state_db,
    status_for_config,
)
from wta.utils.timeutil import SimClock, iso

from .conftest import T0

NOW = T0 + 3600.0  # 2026-10-22T11:00:00Z
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CFG_HASH = "a1b2c3d4e5f6"
FEED_TICKS = 120  # synthetic books published every second for the last 2 minutes
ANSI = "\x1b"


@dataclass
class Fixture:
    root: Path
    log_dir: Path
    state_db: Path
    market_db: Path
    pid_dir: Path
    btc_bid: Decimal
    symbols: int


def _intent(did: str, strategy: str, symbol: str, side: str = "BUY") -> Intent:
    return Intent(
        id=did,
        ts=NOW - 300,
        strategy=strategy,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type="LIMIT",
        notional=Decimal("100000"),
        purpose="entry",
        reason=f"{strategy} test intent on {symbol}",
    )


def _approved() -> RiskDecision:
    return RiskDecision(
        approved=True, checks=[RiskCheck(code="MAX_OPEN_ORDERS", passed=True, detail="1 < 8")]
    )


def _veto(*codes: str) -> RiskDecision:
    checks = [RiskCheck(code=c, passed=False, detail=f"{c} failed") for c in codes]
    checks.append(RiskCheck(code="BANNED_MARKET", passed=True, detail="allowed"))
    return RiskDecision(approved=False, checks=checks, veto_reason=f"{codes[0]} failed")


def _order(cid: str, status: str, ts: float) -> Order:
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
    return Order(req=req, status=status, created_ts=ts, updated_ts=ts)  # type: ignore[arg-type]


def _position(pid: str, symbol: str, base: str, quote: str, entry: Decimal, qty: str = "0.5") -> Position:
    return Position(
        id=pid,
        strategy="S1",
        symbol=symbol,
        base=base,
        quote=quote,
        qty=Decimal(qty),
        entry_price=entry,
        entry_ts=NOW - 900,
    )


def _health(
    log_dir: Path, ts: float, *, feed: str = "synthetic", killed: bool = False, extra: dict | None = None
) -> None:
    clock = SimClock(ts)
    reasons = ["synthetic feed: no fresh data"] if feed == "synthetic" else []
    if killed:
        reasons.append("killed: max drawdown 5.2% >= 5.0%")
    with_extra = {
        "feed": feed,
        "uptime_s": 3500.0,
        "git_dirty": False,
        "flatten_phase": 0,
        "daily_pnl_pct": "0.008",
        "fees_paid_tmn": "10000.00",
        "fee_budget_used_pct": "0.1",
        "usdt_share": "0.45",
        "coin_exposure_pct": "0.12",
        "peak_equity_tmn": "5080000",
        "realized_pnl_tmn": "750.00",
        **(extra or {}),
    }
    log = HealthLog(log_dir, clock)
    log.write(
        HealthRecord(
            ts=iso(ts),
            mode="shadow",
            git_commit=COMMIT,
            config_hash=CFG_HASH,
            equity_tmn=Decimal("5040000"),
            drawdown_pct=Decimal("0.007874"),
            open_orders=2,
            positions=3,
            ws_ok=False,
            rest_ok=True,
            api_errors_1m=1,
            killed=killed,
            reasons=reasons,
            extra=with_extra,
        )
    )
    log.close()


def _feed_into(store: Store, end: float, ticks: int) -> tuple[MarketDataBus, int]:
    markets = load_markets_snapshot()
    ref = snapshot_reference()
    bus = MarketDataBus()
    bus.on_book(store.add_book)
    feed = SyntheticFeed(
        markets=markets,
        symbols=list(markets),
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=SimClock(end),
        params=SyntheticParams(seed=3),
    )
    start = end - (ticks - 1)
    feed.publish(start)
    for i in range(1, ticks):
        feed.step(1.0)
        feed.publish(start + i)
    store.flush()
    return bus, len(markets)


@pytest.fixture
def populated(tmp_path: Path) -> Fixture:
    log_dir = tmp_path / "logs"
    pid_dir = tmp_path / "run"
    state_db = tmp_path / "data" / "state_shadow.sqlite"
    market_db = tmp_path / "data" / "wta.sqlite"

    # recorder: synthetic books, newest at NOW
    market = Store(market_db)
    bus, n_symbols = _feed_into(market, NOW, FEED_TICKS)
    market.close()
    btc_bid = bus.books["BTCUSDT"].best_bid
    assert btc_bid is not None

    # engine state
    state = Store(state_db)
    state.upsert_position(
        _position("p_btc", "BTCUSDT", "BTC", "USDT", Decimal("80000.00"), "0.000120"), True, NOW
    )
    state.upsert_position(_position("p_paxg", "PAXGUSDT", "PAXG", "USDT", Decimal("2000.00")), True, NOW)
    state.upsert_position(_position("p_xaut", "XAUTUSDT", "XAUT", "USDT", Decimal("2100.00")), True, NOW)
    state.upsert_position(_position("p_closed", "ETHUSDT", "ETH", "USDT", Decimal("2400.00")), False, NOW)
    old_btc = BookSnapshot(
        symbol="BTCUSDT",
        ts=NOW - 60,
        source="synthetic",
        bids=[Level(price=Decimal("1.00"), qty=Decimal("1"))],
        asks=[Level(price=Decimal("2.00"), qty=Decimal("1"))],
    )
    paxg = BookSnapshot(
        symbol="PAXGUSDT",
        ts=NOW - 30,
        source="rest",
        bids=[Level(price=Decimal("2010.00"), qty=Decimal("1"))],
        asks=[Level(price=Decimal("2012.00"), qty=Decimal("1"))],
    )
    state.add_engine_book(old_btc, "shadow")  # older than the recorder's book: must not be used
    state.add_engine_book(paxg, "shadow")  # only the engine saw this market
    state.upsert_order(_order("S1_BTCUSDT_1_aaaa", "NEW", NOW - 50))
    state.upsert_order(_order("S1_BTCUSDT_2_bbbb", "FILLED", NOW - 40))
    state.kv_set(
        "engine_flags",
        {
            "paused": True,
            "pause_reason": "daily loss -2.6% <= -2.5%",
            "paused_until_ts": None,
            "killed": False,
            "kill_reason": "",
            "flatten_phase": 1,
        },
        NOW - 20,
    )
    state.kv_set(
        "portfolio_state",
        {
            "peak_equity_tmn": "5100000",
            "day_start_equity_tmn": "5000000",
            "day_start_ts": T0 - 36000,
            "fees_paid_tmn_total": "12345.67",
            "placements": [],
            "realized_pnl_tmn": {"S1": "1000", "S2": "-200"},
        },
        NOW - 20,
    )
    state.add_equity(
        NOW - 5,
        Decimal("5050000"),
        tmn=Decimal("2020000"),
        usdt_tmn_value=Decimal("2525000"),
        coins_tmn_value=Decimal("505000"),
        peak_tmn=Decimal("5100000"),
        drawdown_pct=Decimal("0.0098039"),
    )
    plan = {
        "ts": NOW - 130,
        "model": "offline-rules-v1",
        "regimes": {
            "USDTTMN": {"regime": "range", "confidence": 0.62, "evidence": "flat"},
            "BTCUSDT": {"regime": "trend_up", "confidence": 0.7, "evidence": "ema gap"},
        },
        "enabled_strategies": ["S1", "S2"],
        "size_multiplier": "0.5",
        "usdt_target_share": "0.55",
        "notes": "[offline rules provider - not an LLM] dollar flat, BTC trending.",
    }
    state.add_plan(NOW - 130, plan, "hash_offline")
    state.close()

    # logs
    eclock = SimClock(T0)
    events = EventsLog(log_dir, eclock)
    events.log(
        "start",
        "engine started in shadow mode",
        {
            "mode": "shadow",
            "feed": "synthetic",
            "reason": "fresh",
            "synthetic": True,
            "git_commit": COMMIT,
            "git_tag": "live-20261022-1000",
            "git_dirty": False,
            "config_hash": CFG_HASH,
            "pid": 4242,
            "challenge_end_utc": iso(NOW + 2 * 86400 + 3 * 3600),
        },
    )
    eclock.set(NOW - 100)
    events.log("pause", "daily loss -2.6% <= -2.5%", {})
    events.close()

    _health(log_dir, NOW - 40)

    clock = SimClock(NOW - 400)
    dlog = DecisionLog(log_dir, "shadow", COMMIT, CFG_HASH, clock)
    dlog.record(
        "plan",
        "plan_1",
        "AI",
        "",
        plan_snapshot={"ts": NOW - 400, "model": "openrouter/some-model", "regimes": {}, "notes": "older"},
        notes="older plan",
    )
    clock.set(NOW - 300)
    dlog.record("intent", "d1", "S2", "USDTTMN", intent=_intent("d1", "S2", "USDTTMN"), risk=_approved())
    clock.set(NOW - 290)
    dlog.record(
        "order",
        "d1",
        "S2",
        "USDTTMN",
        intent=_intent("d1", "S2", "USDTTMN"),
        risk=_approved(),
        order={"request": {"client_id": "S2_USDTTMN_1_cccc"}, "accepted": True},
    )
    clock.set(NOW - 200)
    dlog.record(
        "veto",
        "d3",
        "S1",
        "BTCTMN",
        intent=_intent("d3", "S1", "BTCTMN", "SELL"),
        risk=_veto("MAX_OPEN_ORDERS"),
    )
    clock.set(NOW - 100)
    dlog.outcome("d1", {"pnl_tmn": "600", "pnl_pct": "0.0012", "exit_reason": "target"})
    clock.set(NOW - 90)
    dlog.record("no_trade", "d4", "S2", "USDTTMN", notes="gap below threshold")
    clock.set(NOW - 80)
    dlog.record("intent", "d5", "S3", "ETHUSDT", intent=_intent("d5", "S3", "ETHUSDT"), risk=_approved())
    clock.set(NOW - 70)
    dlog.record(
        "veto",
        "d6",
        "S2",
        "USDTTMN",
        intent=_intent("d6", "S2", "USDTTMN"),
        risk=_veto("DAILY_LOSS", "FEE_BUDGET"),
    )
    dlog.close()

    pid_dir.mkdir()
    (pid_dir / "agent.pid").write_text("4242\n")
    (pid_dir / "recorder.pid").write_text("999\n")
    return Fixture(tmp_path, log_dir, state_db, market_db, pid_dir, btc_bid, n_symbols)


def _build(fx: Fixture, now: float = NOW, **kw: object) -> dict:
    return build_status(
        fx.log_dir,
        kw.pop("state_db", fx.state_db),  # type: ignore[arg-type]
        now,
        fx.pid_dir,
        market_db=kw.pop("market_db", fx.market_db),  # type: ignore[arg-type]
        is_alive=lambda pid: pid == 4242,
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- populated


def test_populated_status_fields(populated: Fixture) -> None:
    st = _build(populated)
    assert st["errors"] == []

    eng = st["engine"]
    assert eng["mode"] == "shadow" and eng["feed"] == "synthetic" and eng["synthetic"] is True
    assert eng["heartbeat_age_s"] == pytest.approx(40.0) and eng["heartbeat_stale"] is False
    assert eng["git_commit"] == COMMIT and eng["git_tag"] == "live-20261022-1000"
    assert eng["config_hash"] == CFG_HASH and eng["pid"] == 4242
    assert eng["deadline_in_s"] == pytest.approx(2 * 86400 + 3 * 3600)
    assert eng["last_event"]["kind"] == "pause"
    assert st["warnings"][0] == SYNTHETIC_WARNING

    # the equity row (NOW-5) is newer than the heartbeat (NOW-40): it wins
    eq = st["equity"]
    assert eq["source"] == "state_db"
    assert eq["equity_tmn"] == "5050000"
    assert Decimal(eq["return_pct"]) == Decimal("0.010000")
    assert Decimal(eq["drawdown_pct"]) == Decimal("0.009804")
    assert Decimal(eq["daily_pnl_pct"]) == Decimal("0.010000")
    assert Decimal(eq["daily_pnl_tmn"]) == Decimal("50000")
    assert eq["fees_paid_tmn"] == "10000.00"  # heartbeat value
    assert Decimal(eq["fee_budget_tmn"]) == Decimal("100000")
    assert Decimal(eq["fee_budget_used_pct"]) == Decimal("0.1")
    assert Decimal(eq["usdt_share"]) == Decimal("0.5")
    assert Decimal(eq["coin_exposure_pct"]) == Decimal("0.1")

    risk = st["risk"]
    assert risk["source"] == "state_db" and risk["paused"] is True and risk["killed"] is False
    assert risk["pause_reason"] == "daily loss -2.6% <= -2.5%"
    assert risk["flatten_phase"] == 1
    assert any(w.startswith("PAUSED: daily loss") for w in st["warnings"])
    assert any(w.startswith("flatten phase 1") for w in st["warnings"])

    assert st["open_orders"] == 1  # the FILLED order is not open
    by_id = {p["id"]: p for p in st["positions"]}
    assert sorted(by_id) == ["p_btc", "p_paxg", "p_xaut"]  # the closed position is not listed
    btc = by_id["p_btc"]
    assert btc["bid_source"] == "market db" and Decimal(btc["bid"]) == populated.btc_bid
    assert btc["bid_age_s"] == pytest.approx(0.0)
    expected = (populated.btc_bid / Decimal("80000.00") - 1).quantize(Decimal("0.000001"))
    assert Decimal(btc["pnl_pct"]) == expected
    paxg = by_id["p_paxg"]
    assert paxg["bid_source"] == "state db" and paxg["bid"] == "2010.00"
    assert Decimal(paxg["pnl_pct"]) == Decimal("0.005000")
    assert by_id["p_xaut"]["bid"] is None and by_id["p_xaut"]["pnl_pct"] is None


def test_last_decisions_newest_first_deduped(populated: Fixture) -> None:
    rows = _build(populated)["decisions"]
    assert [r["decision_id"] for r in rows] == ["d6", "d5", "d3", "d1", "plan_1"]
    assert rows[0]["result"] == "VETO DAILY_LOSS,FEE_BUDGET"
    assert rows[1]["result"] == "approved" and rows[1]["kind"] == "intent" and rows[1]["side"] == "BUY"
    assert rows[2]["result"] == "VETO MAX_OPEN_ORDERS" and rows[2]["side"] == "SELL"
    # d1: the order record (newest of its id) with the later outcome attached
    assert rows[3]["kind"] == "order" and rows[3]["result"] == "approved, order sent"
    assert rows[3]["outcome_pnl_pct"] == "0.0012"
    assert rows[4]["kind"] == "plan" and rows[4]["result"] == "model openrouter/some-model"
    assert all(r["kind"] != "no_trade" for r in rows)


def test_plan_newest_source_wins(populated: Fixture) -> None:
    plan = _build(populated)["plan"]
    assert plan["source"] == "state_db"  # NOW-130 in the plans table beats the NOW-400 log record
    assert plan["offline"] is True and plan["model_text"] == OFFLINE_TEXT
    assert plan["age_s"] == pytest.approx(130.0) and plan["stale"] is False
    assert plan["regimes"] == {
        "BTCUSDT": {"regime": "trend_up", "confidence": 0.7},
        "USDTTMN": {"regime": "range", "confidence": 0.62},
    }
    assert plan["size_multiplier"] == "0.5" and plan["usdt_target_share"] == "0.55"


def test_processes_and_recorder(populated: Fixture) -> None:
    st = _build(populated)
    assert [(p["name"], p["pid"], p["alive"]) for p in st["processes"]] == [
        ("agent", 4242, True),
        ("recorder", 999, False),
    ]
    assert any("process recorder (pid 999) is DEAD" in w for w in st["warnings"])
    rec = st["recorder"]
    assert rec["exists"] is True
    assert rec["books_in_window"] == FEED_TICKS * populated.symbols
    assert len(rec["symbols"]) == populated.symbols
    assert rec["symbols"]["BTCUSDT"] == {"count": FEED_TICKS, "last_age_s": 0.0}
    assert rec["last_book_age_s"] == pytest.approx(0.0)


def test_format_populated(populated: Fixture) -> None:
    st = _build(populated)
    text = format_status(st)
    assert ANSI not in text
    lines = text.splitlines()
    assert all(len(line) <= 100 for line in lines)
    assert len(lines) <= 45  # one screen
    assert lines[1] == f"!! {SYNTHETIC_WARNING}"
    for needle in (
        "mode shadow",
        "feed SYNTHETIC",
        "heartbeat 40s ago",
        "commit 0123456789ab",
        "tag live-20261022-1000",
        f"config {CFG_HASH}",
        "5,050,000 TMN",
        "return +1.00%",
        "drawdown 0.98%",
        "daily PnL +1.00% (+50,000 TMN)",
        "10,000 / 100,000 TMN budget (10.00% used)",
        "USDT share 50.0%",
        "coins 10.0%",
        "PAUSED: daily loss -2.6% <= -2.5%",
        "flatten phase 1",
        "deadline in 2d 03h",
        "3 open",
        "1 open orders",
        "VETO DAILY_LOSS,FEE_BUDGET",
        "VETO MAX_OPEN_ORDERS",
        "approved, order sent, closed +0.12%",
        OFFLINE_TEXT,
        "2m 10s old",
        "size x0.5",
        "USDT target 55%",
        "USDTTMN range 0.62",
        "BTCUSDT trend_up 0.70",
        "agent 4242 alive",
        "recorder 999 DEAD",
        f"{FEED_TICKS * populated.symbols:,} books in the last 10m 00s",
        "newest 0s ago",
    ):
        assert needle in text, needle
    paxg_line = next(line for line in lines if "PAXGUSDT" in line)
    assert "2,010.00" in paxg_line and "+0.50%" in paxg_line
    xaut_line = next(line for line in lines if "XAUTUSDT" in line)
    assert "n/a" in xaut_line and "no book" in xaut_line


def test_status_is_json_safe_and_deterministic(populated: Fixture) -> None:
    a = _build(populated)
    b = _build(populated)
    assert orjson.dumps(a) == orjson.dumps(b)  # no Decimals left, same bytes twice
    assert format_status(a) == format_status(b)


def test_narrow_width_for_telegram(populated: Fixture) -> None:
    text = format_status(_build(populated), width=48)
    assert all(len(line) <= 48 for line in text.splitlines())
    assert "STRATEGY" not in text  # compact rows, no aligned table
    flat = " ".join(text.split())  # compact rows and long parts are wrapped, not cut
    for needle in (
        OFFLINE_TEXT,
        "S1 PAXGUSDT +0.50% | qty 0.5 @ 2,000.00 | bid 2,010.00 (30s)",
        "veto S2 USDTTMN BUY: VETO DAILY_LOSS,FEE_BUDGET",
        "approved, order sent, closed +0.12%",
        "flatten phase 1 (flatten start, maker exits)",
        "10,000 / 100,000 TMN budget (10.00% used)",
        "dollar flat, BTC trending.",
    ):
        assert needle in flat, needle
    assert "..." not in text.replace(str(populated.root), "")  # only the long tmp paths may be cut


# ---------------------------------------------------------------- missing / stale / broken


def test_empty_everything_missing(tmp_path: Path) -> None:
    st = build_status(
        tmp_path / "no-logs",
        tmp_path / "no-state.sqlite",
        NOW,
        tmp_path / "no-run",
        market_db=tmp_path / "no-market.sqlite",
    )
    assert st["engine"]["heartbeat_ts"] is None and st["engine"]["heartbeat_stale"] is True
    assert st["warnings"] == [
        f"no heartbeat in {tmp_path / 'no-logs'}/health*.jsonl: the agent has not run with these logs"
    ]
    assert st["equity"]["equity_tmn"] is None
    assert st["positions"] == [] and st["decisions"] == [] and st["plan"] is None
    assert st["processes"] == [] and st["recorder"]["exists"] is False
    assert st["errors"] == [f"state db {tmp_path / 'no-state.sqlite'}: not found"]
    text = format_status(st)
    assert ANSI not in text
    for needle in (
        "no heartbeat",
        "no equity yet",
        "none logged",
        "no plan yet",
        "no pid files",
        "not found (recorder never ran here)",
        "details need the engine state db",
        "no deadline set",
    ):
        assert needle in text, needle


def test_empty_state_db_and_no_paths(tmp_path: Path) -> None:
    empty = Store(tmp_path / "state.sqlite")  # schema only
    empty.close()
    st = build_status(tmp_path / "logs", tmp_path / "state.sqlite", NOW, tmp_path / "run", market_db=None)
    assert st["errors"] == []
    assert st["positions"] == [] and st["positions_count"] == 0 and st["open_orders"] == 0
    assert st["recorder"]["db"] is None
    assert "no market database configured" in format_status(st)
    st2 = build_status(tmp_path / "logs", None, NOW, tmp_path / "run", market_db=None)
    assert st2["sources"]["state_db"] is None and st2["errors"] == []


@pytest.mark.parametrize(("age", "stale"), [(60.0, False), (180.0, False), (181.0, True), (3700.0, True)])
def test_heartbeat_staleness(tmp_path: Path, age: float, stale: bool) -> None:
    log_dir = tmp_path / "logs"
    _health(log_dir, NOW - age, feed="live")
    st = build_status(log_dir, None, NOW, tmp_path / "run", market_db=None)
    assert st["engine"]["heartbeat_stale"] is stale
    warned = [w for w in st["warnings"] if w.startswith("last heartbeat")]
    if stale:
        expected = "1h 01m" if age == 3700.0 else "3m 01s"
        assert warned == [f"last heartbeat {expected} ago (> 180s): the agent may be down"]
        assert "(STALE)" in format_status(st)
    else:
        assert warned == []
        assert "(STALE)" not in format_status(st)
    assert SYNTHETIC_WARNING not in st["warnings"]  # feed=live


def test_git_tag_only_for_the_heartbeat_commit(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _health(log_dir, NOW - 30, feed="live")
    events = EventsLog(log_dir, SimClock(T0))
    events.log("start", "engine started", {"git_commit": "f" * 40, "git_tag": "live-old", "pid": 1})
    events.close()
    st = build_status(log_dir, None, NOW, tmp_path / "run", market_db=None)
    assert st["engine"]["git_commit"] == COMMIT and st["engine"]["git_tag"] is None
    assert "no tag" in format_status(st)


def test_health_only_fallbacks(tmp_path: Path) -> None:
    """Without a state DB everything comes from the heartbeat (killed + reason from its reasons)."""
    log_dir = tmp_path / "logs"
    _health(log_dir, NOW - 30, feed="live", killed=True, extra={"flatten_phase": 2})
    st = build_status(log_dir, None, NOW, tmp_path / "run", market_db=None)
    eq = st["equity"]
    assert eq["source"] == "health" and eq["equity_tmn"] == "5040000"
    assert Decimal(eq["return_pct"]) == Decimal("0.008")
    assert Decimal(eq["daily_pnl_pct"]) == Decimal("0.008") and eq["daily_pnl_tmn"] is None
    assert Decimal(eq["usdt_share"]) == Decimal("0.45")
    assert eq["peak_tmn"] == "5080000" and eq["realized_pnl_tmn"] == "750.00"
    risk = st["risk"]
    assert risk["source"] == "health" and risk["killed"] is True
    assert risk["kill_reason"] == "max drawdown 5.2% >= 5.0%"
    assert risk["flatten_phase"] == 2
    assert st["warnings"][0].startswith("KILLED: max drawdown 5.2% >= 5.0%")
    assert st["positions_count"] == 3 and st["open_orders"] == 2
    text = format_status(st)
    assert "KILLED: max drawdown" in text and "rest ok" in text and "ws DOWN" in text


def test_initial_equity_and_fee_budget_parameters(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _health(log_dir, NOW - 30, feed="live")
    st = build_status(
        log_dir,
        None,
        NOW,
        tmp_path / "run",
        market_db=None,
        initial_equity_tmn=Decimal("4000000"),
        fee_budget_pct=Decimal("0.01"),
    )
    assert Decimal(st["equity"]["return_pct"]) == Decimal("0.26")
    assert Decimal(st["equity"]["fee_budget_tmn"]) == Decimal("40000")
    assert Decimal(st["equity"]["fee_budget_used_pct"]) == Decimal("0.25")


def test_recorder_stale_and_stale_plan(tmp_path: Path) -> None:
    market_db = tmp_path / "wta.sqlite"
    market = Store(market_db)
    _feed_into(market, NOW - 1800, 5)  # newest book 30 minutes old
    market.close()
    log_dir = tmp_path / "logs"
    dlog = DecisionLog(log_dir, "shadow", COMMIT, CFG_HASH, SimClock(NOW - 1500))
    dlog.record(
        "plan",
        "plan_x",
        "AI",
        "",
        plan_snapshot={"ts": NOW - 1500, "model": "openrouter/m", "size_multiplier": "1"},
    )
    dlog.close()
    st = build_status(log_dir, None, NOW, tmp_path / "run", market_db=market_db)
    rec = st["recorder"]
    assert rec["books_in_window"] == 0 and rec["last_book_age_s"] == pytest.approx(1800.0)
    assert "recorder: no books in the last 10m 00s (newest 30m 00s ago)" in st["warnings"]
    plan = st["plan"]
    assert plan["source"] == "decisions" and plan["stale"] is True and plan["model_text"] == "openrouter/m"
    assert any(w.startswith("plan is 25m 00s old (> 1200s)") for w in st["warnings"])
    assert "(STALE)" in format_status(st)


def test_broken_files_are_reported_not_raised(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _health(log_dir, NOW - 30, feed="live")
    health_file = next(log_dir.glob("health-*.jsonl"))
    with health_file.open("a") as fh:
        fh.write('{"ts": "2026-10-22T10:59:5')  # a line cut short by a crash
    (log_dir / "decisions-20261022.jsonl").write_text("not json\n[1, 2]\n")
    bad_db = tmp_path / "state.sqlite"
    bad_db.write_bytes(b"this is not a sqlite database" * 10)
    run = tmp_path / "run"
    run.mkdir()
    (run / "agent.pid").write_text("garbage\n")
    st = build_status(log_dir, bad_db, NOW, run, market_db=bad_db, is_alive=lambda pid: True)
    assert st["engine"]["heartbeat_age_s"] == pytest.approx(30.0)  # the last complete heartbeat
    assert st["decisions"] == []
    assert st["processes"] == [{"name": "agent", "file": str(run / "agent.pid"), "pid": None, "alive": False}]
    assert st["errors"], "the broken database must be reported"
    assert all("Error" in e or "not found" in e for e in st["errors"])
    text = format_status(st)
    assert "(error) " in text and "agent ? DEAD" in text


# ---------------------------------------------------------------- config wiring


def test_resolve_state_db(tmp_path: Path) -> None:
    state_tpl = str(tmp_path / "state_{mode}.sqlite")
    market = tmp_path / "wta.sqlite"
    assert resolve_state_db(state_tpl, str(market), "shadow") is None
    market.write_bytes(b"")
    assert resolve_state_db(state_tpl, str(market), "shadow") == market  # state kept in the market db
    (tmp_path / "state_shadow.sqlite").write_bytes(b"")
    assert resolve_state_db(state_tpl, str(market), "shadow") == tmp_path / "state_shadow.sqlite"
    assert resolve_state_db(state_tpl, str(market), "live") == market
    assert resolve_state_db(state_tpl, str(market), None) == market


def test_status_for_config(populated: Fixture) -> None:
    cfg = Config.model_validate(
        {
            "data": {
                "sqlite_path": str(populated.market_db),
                "state_sqlite_path": str(populated.root / "data" / "state_{mode}.sqlite"),
            },
            "engine": {"initial_equity_tmn": "5000000"},
            "risk": {"fee_budget_pct": "0.02"},
        }
    )
    st = status_for_config(cfg, NOW, log_dir=populated.log_dir, pid_dir=populated.pid_dir)
    assert st["sources"]["state_db"] == str(populated.state_db)  # mode "shadow" from the heartbeat
    assert st["sources"]["market_db"] == str(populated.market_db)
    assert st["equity"]["equity_tmn"] == "5050000"
    assert st["plan"]["model_text"] == OFFLINE_TEXT
