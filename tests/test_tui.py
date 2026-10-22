"""Terminal dashboard (PRD-06 §3, §8): snapshot building and rendering, read-only.

Fixtures are written with the real writers (HealthLog, EventsLog, DecisionLog, Store) into temp
dirs; books come from the deterministic SyntheticFeed. Every frame is rendered into a recording
``rich`` console 200 columns wide (the 1080p demo layout) and checked for its key strings.
"""

from __future__ import annotations

import hashlib
import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from wta.config import Config
from wta.dashboard.tui import (
    OFFLINE_PLAN_MODEL,
    SYNTHETIC_BANNER,
    DashboardSettings,
    build_snapshot,
    render,
    run,
    s1_edges,
    tail_records,
)
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
    MarketPlan,
    Order,
    OrderRequest,
    Position,
    RegimeCall,
    RiskCheck,
    RiskDecision,
)
from wta.utils.timeutil import SimClock, iso

from .conftest import T0

COMMIT = "6094408abcdef0123"
CONFIG_HASH = "a1b2c3d4e5f6"
TAG = "live-20261022-0900"
WATCHLIST = ["USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT"]
END_UTC = "2026-10-25T12:00:00Z"


def _text(snapshot: Any, now: float | None = None, width: int = 200) -> str:
    console = Console(record=True, width=width, file=io.StringIO(), color_system=None)
    console.print(render(snapshot, now))
    return console.export_text()


def _s1_diag() -> dict[str, Any]:
    return {
        "margin_min_pct": "0.0015",
        "size_multiplier": "1",
        "coins": {
            "BTC": {
                "edge_tmn_cheap": "0.00610",
                "edge_usdt_cheap": "-0.00640",
                "cost_tmn_cheap": "0.0043",
                "cost_usdt_cheap": "0.0043",
                "dev_mid": "-0.0062",
                "z": "3.10",
                "lockin": "ENTER TMN_CHEAP",
            },
            "ETH": {
                "edge_tmn_cheap": "-0.00120",
                "edge_usdt_cheap": "0.00080",
                "cost_tmn_cheap": "0.0043",
                "cost_usdt_cheap": "0.0043",
                "dev_mid": "0.0004",
                "z": "0.40",
                "skip": "cooldown",
            },
        },
    }


def _write_health(log_dir: Path, clock: SimClock, **over: Any) -> HealthRecord:
    extra = {
        "feed": "synthetic",
        "uptime_s": 3600.0,
        "ticks": 1800,
        "book_age_s": {s: 1.0 for s in WATCHLIST},
        "rest": {},
        "rest_poller": None,
        "ws": None,
        "task_crashes": {},
        "tick_crashes": 0,
        "git_dirty": False,
        "markets_source": "snapshot",
        "flatten_phase": 0,
        "daily_pnl_pct": "0.001200",
        "fees_paid_tmn": "1234.50",
        "fee_budget_used_pct": "0.012345",
        "usdt_share": "0.450000",
        "coin_exposure_pct": "0.120000",
        "orders_today": 7,
        "orders_last_hour": 2,
        "peak_equity_tmn": "5020000",
        "realized_pnl_tmn": "1234.50",
        "positions": ["pos-1"],
        "strategies": {"S1": _s1_diag()},
    }
    extra.update(over.pop("extra", {}))
    fields: dict[str, Any] = {
        "ts": iso(clock.now()),
        "mode": "shadow",
        "git_commit": COMMIT,
        "config_hash": CONFIG_HASH,
        "equity_tmn": Decimal("5010000"),
        "drawdown_pct": Decimal("0.002"),
        "open_orders": 1,
        "positions": 1,
        "ws_ok": False,
        "rest_ok": False,
        "api_errors_1m": 0,
        "reasons": [],
        "extra": extra,
    }
    fields.update(over)
    log = HealthLog(log_dir, clock)
    rec = log.write(HealthRecord(**fields))
    log.close()
    return rec


def _write_start(log_dir: Path, clock: SimClock, feed: str = "synthetic", reason: str = "fresh") -> None:
    log = EventsLog(log_dir, clock)
    log.log(
        "start",
        "engine started in shadow mode",
        {
            "mode": "shadow",
            "feed": feed,
            "reason": reason,
            "git_commit": COMMIT,
            "git_tag": TAG,
            "config_hash": CONFIG_HASH,
            "watchlist": WATCHLIST,
            "challenge_end_utc": END_UTC,
        },
    )
    log.close()


def _intent(iid: str, strategy: str, symbol: str, side: str, qty: str, reason: str) -> Intent:
    return Intent(
        id=iid,
        ts=T0,
        strategy=strategy,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type="LIMIT",
        qty=Decimal(qty),
        price=Decimal("100"),
        purpose="entry",
        reason=reason,
    )


def _write_decisions(log_dir: Path, clock: SimClock) -> None:
    log = DecisionLog(log_dir, "shadow", COMMIT, CONFIG_HASH, clock)
    clock.set(T0 - 300)
    log.record(
        "veto",
        "S3_ETHUSDT_1",
        "S3",
        "ETHUSDT",
        intent=_intent("S3_ETHUSDT_1", "S3", "ETHUSDT", "BUY", "0.005", "ETH 15-min breakout with volume"),
        risk=RiskDecision(
            approved=False,
            checks=[
                RiskCheck(code="BALANCE", passed=True, detail="ok"),
                RiskCheck(code="COIN_EXPOSURE", passed=False, detail="coins 41% > 40%"),
            ],
            veto_reason="COIN_EXPOSURE",
        ),
        notes="vetoed",
    )
    clock.set(T0 - 200)
    intent = _intent("S1_BTCTMN_1", "S1", "BTCTMN", "BUY", "0.0002", "BTC: TMN book 0.61% cheap; lock-in")
    log.record(
        "order",
        "S1_BTCTMN_1",
        "S1",
        "BTCTMN",
        intent=intent,
        risk=RiskDecision(approved=True, checks=[RiskCheck(code="BALANCE", passed=True, detail="ok")]),
        order={"request": {"symbol": "BTCTMN", "side": "BUY", "qty": "0.0002"}, "accepted": True},
    )
    clock.set(T0 - 100)
    log.outcome(
        "S1_BTCTMN_1",
        {"pnl_tmn": "1234.5", "pnl_pct": "0.0025", "hold_s": 100.0, "exit_reason": "lockin"},
    )
    log.close()


def _synthetic_books(ts: float) -> list[BookSnapshot]:
    markets = load_markets_snapshot()
    ref = snapshot_reference()
    bus = MarketDataBus()
    seen: list[BookSnapshot] = []
    bus.on_book(seen.append)
    feed = SyntheticFeed(
        markets=markets,
        symbols=[s for s in WATCHLIST if s in markets],
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=SimClock(ts),
        params=SyntheticParams(seed=3),
    )
    feed.publish(ts)
    return seen


def _plan(model: str = OFFLINE_PLAN_MODEL) -> MarketPlan:
    return MarketPlan(
        ts=T0 - 120,
        regimes={
            "BTCUSDT": RegimeCall(regime="trend_up", confidence=0.72),
            "USDTTMN": RegimeCall(regime="range", confidence=0.6),
        },
        enabled_strategies=["S1", "S2"],
        size_multiplier=Decimal("0.8"),
        usdt_target_share=Decimal("0.45"),
        notes="Dollar is ranging inside its 1-h band; BTC trends up on rising volume. Keep S3 off.",
        model=model,
        prompt_hash="ph123",
    )


def _write_db(path: Path, flags: dict[str, Any] | None = None, plan: MarketPlan | None = None) -> None:
    store = Store(path)
    for book in _synthetic_books(T0 - 2):
        store.add_book(book)
    store.add_equity(
        T0 - 5,
        Decimal("5012345"),
        tmn=Decimal("2162345"),
        usdt_tmn_value=Decimal("2250000"),
        coins_tmn_value=Decimal("600000"),
        peak_tmn=Decimal("5020000"),
        drawdown_pct=Decimal("0.00152"),
    )
    pos = Position(
        id="pos-1",
        strategy="S3",
        symbol="ETHUSDT",
        base="ETH",
        quote="USDT",
        qty=Decimal("0.005"),
        entry_price=Decimal("2400.50"),
        entry_ts=T0 - 600,
        stop_price=Decimal("2350"),
        meta={"mode": "breakout"},
    )
    store.upsert_position(pos, open=True, ts=T0 - 600)
    req = OrderRequest(
        client_id="S1_BTCTMN_1792663170000_ab12",
        symbol="BTCTMN",
        side="BUY",
        type="LIMIT",
        qty=Decimal("0.0002"),
        price=Decimal("22075000000"),
        strategy="S1",
        purpose="entry",
        decision_id="S1_BTCTMN_1",
    )
    store.upsert_order(Order(req=req, status="NEW", created_ts=T0 - 30, updated_ts=T0 - 30))
    done = OrderRequest(**{**req.model_dump(), "client_id": "S1_BTCTMN_done"})
    store.upsert_order(Order(req=done, status="FILLED", created_ts=T0 - 900, updated_ts=T0 - 890))
    if plan is not None:
        store.add_plan(plan.ts, plan.model_dump(mode="json"), plan.prompt_hash)
    if flags is not None:
        store.kv_set("engine_flags", flags, T0)
    store.close()


@pytest.fixture
def populated(tmp_path: Path) -> tuple[Path, Path]:
    log_dir, db = tmp_path / "logs", tmp_path / "data" / "wta.sqlite"
    clock = SimClock(T0 - 600)
    _write_start(log_dir, clock)
    _write_decisions(log_dir, clock)
    clock.set(T0 - 20)
    _write_health(log_dir, clock)
    _write_db(db, flags={"paused": False, "killed": False, "flatten_phase": 0}, plan=_plan())
    return log_dir, db


# ---------------------------------------------------------------- empty / missing sources


def test_empty_log_dir_and_missing_db_render(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    snap = build_snapshot(tmp_path / "logs", tmp_path / "missing.sqlite", now=T0)
    assert snap.health is None and not snap.db_found and snap.decisions == []
    out = _text(snap)
    for key in ("WALLEX TRADER AGENT", "NO HEARTBEAT", "rules-only", "no trade decisions logged yet",
                "state DB not found", "no equity yet", "deadline not set", "USDTTMN", "no book"):
        assert key in out, key
    assert SYNTHETIC_BANNER not in out


def test_missing_log_dir_and_no_db_given(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path / "nope", None, now=T0)
    assert any("log dir not found" in p for p in snap.problems)
    assert "no state DB given" in snap.problems
    assert "NO HEARTBEAT" in _text(snap)


def test_garbage_inputs_never_raise(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "health-20261022.jsonl").write_text(
        '{"ts": "2026-10-22T09:59:00Z", "mode": "shadow", "equity_tmn": "abc", "reasons": "x",'
        ' "extra": {"strategies": {"S1": {"coins": {"BTC": "oops"}}}, "orders_today": "7"}}\n'
        '{"ts": "2026-10-22T09:59:'  # a line cut short by a crash
    )
    (log_dir / "decisions-20261022.jsonl").write_text(
        '{"kind": "order", "decision_id": "x"}\n[1, 2]\nnot json\n'
        '{"kind": "veto", "decision_id": "y", "risk": {"approved": false, "checks": [1, {"passed": false}]},'
        ' "intent": "nope", "order": {"request": "bad"}}\n'
        '{"kind": "outcome", "decision_id": "x", "outcome": {"pnl_tmn": "zz", "status": "FILLED"}}\n'
    )
    (log_dir / "events-20261022.jsonl").write_text('{"kind": "start", "data": [], "ts": 5}\n')
    db = tmp_path / "corrupt.sqlite"
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    snap = build_snapshot(log_dir, db, now=T0)
    assert snap.health is not None and snap.health["mode"] == "shadow"
    assert len(snap.decisions) == 2
    assert snap.problems, "the corrupt DB is reported"
    out = _text(snap)
    assert "FILLED" in out and "✗" in out


def test_db_without_tables_is_tolerated(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "other.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated(x)")
    snap = build_snapshot(tmp_path, db, now=T0)
    assert snap.db_found
    assert any(p.startswith("equity:") for p in snap.problems)
    assert "equity: OperationalError" in _text(snap)


# ---------------------------------------------------------------- populated fixtures


def test_populated_snapshot_fields(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    snap = build_snapshot(log_dir, db, now=T0)
    assert snap.problems == []
    assert snap.mode == "shadow" and snap.feed == "synthetic" and snap.synthetic
    assert snap.git_tag == TAG and snap.config_hash == CONFIG_HASH
    assert snap.watchlist == WATCHLIST
    assert set(snap.books) == set(WATCHLIST)
    assert snap.equity is not None and snap.equity["equity_tmn"] == Decimal("5012345")
    assert [p.id for p in snap.positions] == ["pos-1"]
    assert [o.client_id for o in snap.open_orders] == ["S1_BTCTMN_1792663170000_ab12"]  # FILLED one dropped
    assert snap.plan is not None and snap.plan["model"] == OFFLINE_PLAN_MODEL and snap.plan_source == "db"
    assert [d["decision_id"] for d in snap.decisions] == ["S3_ETHUSDT_1", "S1_BTCTMN_1"]
    assert snap.decisions[1]["outcome"]["pnl_tmn"] == "1234.5"
    assert snap.deadline_ts is not None and iso(snap.deadline_ts) == "2026-10-25T12:00:00.000Z"


def test_populated_render_shows_every_panel(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    out = _text(build_snapshot(log_dir, db, now=T0))
    expected = [
        # header
        "mode SHADOW", "feed synthetic", COMMIT[:12], TAG, CONFIG_HASH, "markets snapshot",
        "uptime 10m00s", "2026-10-25 12:00Z", "in 3d02h", "flatten none", "RUNNING", "heartbeat 20s ago",
        # equity
        "5,012,345 TMN", "+0.25%", "5,020,000", "0.15%", "+0.12%", "+1,234 TMN", "1,234 / 100,000",
        "44.9%", "target 45.0%", "12.0%", "7 / 60", "5s ago (DB equity row)",
        # markets + S1 edges
        "Markets", "USDTTMN", "BTCTMN", "ETHUSDT", "266,720", "0.014%", "+0.610/0.580", "-0.640/0.580",
        "▶ lock-in", "cooldown", "from the S1 diagnostics",
        # health
        "REST", "✗ down", "WebSocket", "off", "Restarts", "no warnings",
        # plan
        "AI: offline rules (no LLM key)", "Enabled S1 S2", "×0.8", "trend_up", "0.72", "Dollar is ranging",
        # positions & orders
        "Positions (1)", "ETHUSDT", "2,400.50", "2,350.00", "+1.6", "breakout", "Open orders (1)",
        "S1_BTCTMN_1792663170000_ab12", "22,075,000,000", "0/0.0002", "NEW",
        # decisions
        "Last 10 decisions", "veto", "✗ COIN_EXPOSURE", "✓", "+1,234 TMN (+0.25%) lockin",
        "BTC: TMN book 0.61% cheap; lock-in",
        # synthetic banner
        SYNTHETIC_BANNER,
    ]
    for key in expected:
        assert key in out, key


def _fully_loaded(tmp_path: Path) -> tuple[Path, Path]:
    """The busiest frame the engine allows: full watchlist, 3 positions, 8 open orders, 15 decisions."""
    log_dir, db = tmp_path / "logs", tmp_path / "wta.sqlite"
    clock = SimClock(T0 - 900)
    _write_start(log_dir, clock)
    log = DecisionLog(log_dir, "shadow", COMMIT, CONFIG_HASH, clock)
    for i in range(15):
        clock.advance(30)
        rid = f"S1_BTCTMN_{i:02d}"
        log.record("order", rid, "S1", "BTCTMN", intent=_intent(rid, "S1", "BTCTMN", "BUY", "0.0002", "x " * 200),
                   risk=RiskDecision(approved=True))
    log.close()
    clock.set(T0 - 20)
    reasons = [f"stale books (> 30s): SYM{i}" for i in range(5)]
    _write_health(log_dir, clock, reasons=reasons, extra={"strategies": {"S1": {
        "margin_min_pct": "0.0015",
        "coins": {c: _s1_diag()["coins"]["ETH"] for c in ("BTC", "DOGE", "ETH", "SOL", "XRP")}}}})
    plan = MarketPlan(
        ts=T0 - 60,
        regimes={s: RegimeCall(regime="range", confidence=0.5) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT",
                                                                         "XRPUSDT", "DOGEUSDT", "USDTTMN")},
        enabled_strategies=["S1", "S2", "S3"],
        notes="word " * 140,
        model="openrouter/fast",
    )
    _write_db(db, plan=plan)
    store = Store(db)
    for i in range(3):
        store.upsert_position(Position(id=f"p{i}", strategy="S1", symbol="BTCUSDT", base="BTC", quote="USDT",
                                       qty=Decimal("0.0001"), entry_price=Decimal("82000"), entry_ts=T0 - 60), True, T0)
    for i in range(7):
        req = OrderRequest(client_id=f"S2_USDTTMN_17926631{i:05d}_abcd", symbol="USDTTMN", side="SELL",
                           type="LIMIT", qty=Decimal("1.5"), price=Decimal("266800"), strategy="S2",
                           purpose="rebalance", decision_id=f"d{i}")
        store.upsert_order(Order(req=req, status="PARTIALLY_FILLED", filled_qty=Decimal("0.5"),
                                 created_ts=T0 - 100 + i, updated_ts=T0))
    store.close()
    return log_dir, db


def test_render_fits_a_1080p_terminal(populated: tuple[Path, Path], tmp_path: Path) -> None:
    for log_dir, db in (populated, _fully_loaded(tmp_path)):
        snap = build_snapshot(log_dir, db, now=T0, settings=DashboardSettings(watchlist=tuple(
            ["USDTTMN"] + [f"{c}{q}" for c in ("BTC", "ETH", "SOL", "XRP", "DOGE") for q in ("TMN", "USDT")])))
        out = _text(snap)
        lines = out.rstrip("\n").split("\n")
        assert max(len(line) for line in lines) <= 200
        assert len(lines) <= 55, (len(lines), out)


def test_fully_loaded_frame_caps_long_lists(tmp_path: Path) -> None:
    log_dir, db = _fully_loaded(tmp_path)
    snap = build_snapshot(log_dir, db, now=T0)
    assert len(snap.open_orders) == 8 and len(snap.positions) == 4 and len(snap.decisions) == 10
    out = _text(snap)
    assert [d["decision_id"] for d in snap.decisions] == [f"S1_BTCTMN_{i:02d}" for i in range(5, 15)]
    for key in ("Open orders (8)", "+4 older open orders", "+2 more reasons", "Positions (4)", "PARTIAL", "0.5/1.5",
                "AI: openrouter/fast", "Regimes (confidence)", "stale books"):
        assert key in out, key


def test_newest_decision_first(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    out = _text(build_snapshot(log_dir, db, now=T0))
    assert out.index("BTC: TMN book") < out.index("ETH 15-min breakout")


def test_run_once_prints_one_frame(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    console = Console(record=True, width=200, file=io.StringIO(), color_system=None)
    assert run(log_dir, db, once=True, console=console) == 0
    assert "WALLEX TRADER AGENT" in console.export_text()


def test_live_loop_refreshes_and_quits_on_ctrl_c(populated: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import wta.dashboard.tui as tui

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(tui.time, "sleep", fake_sleep)
    log_dir, db = populated
    console = Console(record=True, width=200, file=io.StringIO(), color_system=None)
    assert run(log_dir, db, refresh_s=0.01, console=console) == 0
    assert sleeps == [0.2, 0.2]  # refresh interval clamped to >= 0.2 s


def test_main_once(populated: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    from wta.dashboard.tui import main

    log_dir, db = populated
    assert main(["--log-dir", str(log_dir), "--db", str(db), "--once", "--no-config"]) == 0
    assert "WALLEX TRADER AGENT" in capsys.readouterr().out


def test_dashboard_is_read_only(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated

    def digest() -> dict[str, str]:
        # SQLite's -wal/-shm sidecars may appear for any WAL reader; the data files must not change
        files = [*log_dir.iterdir(), *db.parent.iterdir()]
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))
        }

    before = digest()
    for _ in range(2):
        _text(build_snapshot(log_dir, db, now=T0))
    assert digest() == before


# ---------------------------------------------------------------- synthetic / AI / flags


def test_synthetic_banner_only_for_synthetic_feed(tmp_path: Path) -> None:
    clock = SimClock(T0 - 10)
    live_dir = tmp_path / "live"
    _write_health(live_dir, clock, extra={"feed": "live", "strategies": {}})
    out = _text(build_snapshot(live_dir, None, now=T0))
    assert SYNTHETIC_BANNER not in out and "feed live" in out
    syn_dir = tmp_path / "syn"
    _write_health(syn_dir, clock)
    assert SYNTHETIC_BANNER in _text(build_snapshot(syn_dir, None, now=T0))


def test_synthetic_detected_from_books_alone(tmp_path: Path) -> None:
    db = tmp_path / "wta.sqlite"
    _write_db(db)
    snap = build_snapshot(tmp_path / "no-logs", db, now=T0)
    assert snap.feed == "synthetic"
    assert SYNTHETIC_BANNER in _text(snap)


@pytest.mark.parametrize(
    ("model", "label"),
    [
        (OFFLINE_PLAN_MODEL, "AI: offline rules (no LLM key)"),
        ("openrouter/some-fast-model", "AI: openrouter/some-fast-model"),
        (None, "rules-only"),
    ],
)
def test_plan_ai_label(tmp_path: Path, model: str | None, label: str) -> None:
    db = tmp_path / "wta.sqlite"
    _write_db(db, plan=None if model is None else _plan(model))
    out = _text(build_snapshot(tmp_path, db, now=T0))
    assert label in out
    if model != OFFLINE_PLAN_MODEL:
        assert "offline rules" not in out


def test_plan_falls_back_to_decision_log(tmp_path: Path) -> None:
    clock = SimClock(T0 - 60)
    log = DecisionLog(tmp_path, "shadow", COMMIT, CONFIG_HASH, clock)
    log.record("plan", "plan-1", "AI", "*", plan_snapshot=_plan("gemini-x"))
    log.close()
    snap = build_snapshot(tmp_path, None, now=T0)
    assert snap.plan is not None and snap.plan_source == "decisions"
    assert "AI: gemini-x" in _text(snap)


def test_killed_from_db_flags(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    store = Store(db)
    store.kv_set("engine_flags", {"killed": True, "kill_reason": "drawdown 5.2% >= 5.0%", "flatten_phase": 0}, T0)
    store.close()
    out = _text(build_snapshot(log_dir, db, now=T0))
    assert "KILLED" in out and "drawdown 5.2% >= 5.0%" in out
    assert "RUNNING" not in out


def test_paused_from_heartbeat_without_db(tmp_path: Path) -> None:
    clock = SimClock(T0 - 10)
    _write_health(tmp_path, clock, paused=True, reasons=["paused: daily loss -2.6% <= -2.5%"])
    out = _text(build_snapshot(tmp_path, None, now=T0))
    assert "PAUSED" in out and "daily loss -2.6% <= -2.5%" in out
    assert "state DB not available" in out


def test_db_flags_win_over_older_heartbeat(tmp_path: Path) -> None:
    clock = SimClock(T0 - 50)
    _write_health(tmp_path, clock, killed=True, reasons=["killed: drawdown"])
    db = tmp_path / "wta.sqlite"
    _write_db(db, flags={"killed": False, "paused": False, "kill_reason": "", "flatten_phase": 0})
    out = _text(build_snapshot(tmp_path, db, now=T0))
    assert "RUNNING" in out and "KILLED" not in out


def test_flatten_phase_and_stale_heartbeat(tmp_path: Path) -> None:
    clock = SimClock(T0 - 600)
    _write_health(tmp_path, clock, extra={"flatten_phase": 2})
    out = _text(build_snapshot(tmp_path, None, now=T0))
    assert "FLATTENING" in out and "2 · market (T-30m)" in out
    assert "STALE: no heartbeat for 10m00s" in out


def test_restarts_counted_once_per_restart(tmp_path: Path) -> None:
    clock = SimClock(T0 - 100)
    _write_start(tmp_path, clock)
    _write_start(tmp_path, clock, reason="restart")
    EventsLog(tmp_path, clock).log("restart", "state restored from the store")
    snap = build_snapshot(tmp_path, None, now=T0)
    assert snap.restarts == 1


# ---------------------------------------------------------------- S1 edges


def test_s1_edges_from_diagnostics(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    rows = {r.coin: r for r in s1_edges(build_snapshot(log_dir, db, now=T0))}
    btc, eth = rows["BTC"], rows["ETH"]
    assert btc.need_tmn_cheap == Decimal("0.0058") and btc.hit_tmn_cheap and not btc.hit_usdt_cheap
    assert btc.status == "▶ lock-in"
    assert not eth.hit_tmn_cheap and not eth.hit_usdt_cheap and eth.status == "cooldown"
    assert not btc.estimated


def test_s1_edges_estimated_from_books(tmp_path: Path) -> None:
    db = tmp_path / "wta.sqlite"
    _write_db(db)
    snap = build_snapshot(tmp_path / "no-logs", db, now=T0)
    rows = s1_edges(snap)
    assert {r.coin for r in rows} == {"BTC", "ETH"}
    assert all(r.estimated and r.need_tmn_cheap == Decimal("0.0058") for r in rows)
    assert "estimated from the books" in _text(snap)


# ---------------------------------------------------------------- log reading


def test_last_ten_decisions_only(tmp_path: Path) -> None:
    clock = SimClock(T0 - 1000)
    log = DecisionLog(tmp_path, "shadow", COMMIT, CONFIG_HASH, clock)
    for i in range(15):
        clock.advance(10)
        rid = f"S2_USDTTMN_{i:02d}"
        log.record("order", rid, "S2", "USDTTMN", intent=_intent(rid, "S2", "USDTTMN", "SELL", "1.5", f"rebalance {i}"),
                   risk=RiskDecision(approved=True))
        log.record("no_trade", f"nt-{i}", "S1", "*", notes="no edge")
    log.outcome("S2_USDTTMN_14", {"kind": "order", "status": "FILLED"})
    log.close()
    snap = build_snapshot(tmp_path, None, now=T0)
    assert [d["decision_id"] for d in snap.decisions] == [f"S2_USDTTMN_{i:02d}" for i in range(5, 15)]
    assert snap.decisions[-1]["outcome"]["status"] == "FILLED"
    out = _text(snap)
    assert "rebalance 14" in out and "rebalance 4" not in out and "no edge" not in out


def test_tail_records_reads_backwards_across_days(tmp_path: Path) -> None:
    (tmp_path / "events-20261021.jsonl").write_text('{"n": 1}\n{"n": 2}\n')
    (tmp_path / "events-20261022.jsonl").write_text('{"n": 3}\n\n{"n": 4}\n')
    assert [r["n"] for r in tail_records(tmp_path, "events")] == [1, 2, 3, 4]
    assert [r["n"] for r in tail_records(tmp_path, "events", max_lines=3)] == [2, 3, 4]
    assert [r["n"] for r in tail_records(tmp_path, "events", until=lambda r: r["n"] == 3)] == [3, 4]


# ---------------------------------------------------------------- settings


def test_settings_from_config(cfg: Config) -> None:
    s = DashboardSettings.from_config(cfg)
    assert s.initial_equity_tmn == cfg.engine.initial_equity_tmn
    assert list(s.watchlist) == [w for w in cfg.markets.watchlist if not cfg.markets.is_banned(w)]
    assert list(s.s1_coins) == cfg.strategies.s1_crossbook.coins
    snap = build_snapshot(Path("/nonexistent"), None, now=T0, cfg=cfg)
    assert snap.watchlist == list(s.watchlist)


def test_settings_deadline_overrides_logs(populated: tuple[Path, Path]) -> None:
    log_dir, db = populated
    settings = DashboardSettings(challenge_end_utc="2026-10-22T11:00:00Z")
    out = _text(build_snapshot(log_dir, db, now=T0, settings=settings))
    assert "2026-10-22 11:00Z" in out and "in 1h00m" in out
