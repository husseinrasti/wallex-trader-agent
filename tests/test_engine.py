"""Engine integration (PRD-02 §5-§8): risk -> orders -> fills -> positions -> kill/flatten -> restart.

Every test drives the real components (RiskManager, OrderManager, PaperExchange, Reconciler,
FlattenManager, PositionManager, Store, JSONL logs) through ``Engine.tick(now)`` on a ``SimClock``.
``FakeFeed`` publishes deterministic books (BTCTMN = BTCUSDT x USDTTMN) before every tick, so a
test can move or crash the market by hand. Markets come from ``configs/markets_snapshot.yaml``.
Nothing touches the network; the only wall-clock runs are the short ``run_engine`` wiring test
and the CLI tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta import cli
from wta import engine as engine_mod
from wta.config import Config, Settings, load_config
from wta.data.store import Store
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.engine import (
    KV_KILL_REQUEST,
    KV_KILL_RESET,
    KV_PAPER,
    VETO_REPEAT_S,
    Engine,
    TickClock,
    load_injected_intents,
    parse_injected_line,
    parse_paper_balances,
    request_kill,
    request_kill_reset,
    run_engine,
)
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot
from wta.exchange.paper import PaperExchange
from wta.execution.reconcile import KV_FLAGS
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import EventsLog, merge_decisions
from wta.logging_.jsonl import iter_family
from wta.models import BookSnapshot, Intent, Level, MarketInfo
from wta.risk.context import RiskContext
from wta.risk.manager import CHECK_CODES
from wta.utils.decimalutil import D, quantize_down, quantize_up
from wta.utils.timeutil import SimClock, iso

from .conftest import T0

SYMBOLS = ["USDTTMN", "BTCTMN", "BTCUSDT"]
SPREADS = {"USDTTMN": D("0.0002"), "BTCUSDT": D("0.0008"), "BTCTMN": D("0.0014")}
LEVEL_QTY = {"USDTTMN": D("500"), "BTCUSDT": D("0.01"), "BTCTMN": D("0.01")}
START_BALANCES = {"TMN": D("3000000"), "USDT": D("10")}


# ---------------------------------------------------------------- fixtures


class FakeFeed:
    """Three levels per side around a mid; ``shock(f)`` multiplies the dollar and BTC by ``f``."""

    def __init__(self, bus: MarketDataBus, markets: dict[str, MarketInfo]) -> None:
        self.bus = bus
        self.markets = markets
        self.usdt = D("266739")
        self.btc = D("82877")
        self.skip: set[str] = set()  # symbols not published (to test stale/dedupe paths)

    def mid(self, symbol: str) -> Decimal:
        return {"USDTTMN": self.usdt, "BTCUSDT": self.btc, "BTCTMN": self.btc * self.usdt}[symbol]

    def book(self, symbol: str, now: float) -> BookSnapshot:
        m, mid = self.markets[symbol], self.mid(symbol)
        half, step, qty = mid * SPREADS[symbol] / 2, mid * D("0.0002"), LEVEL_QTY[symbol]
        bids = [Level(price=quantize_down(mid - half - i * step, m.tick_decimals), qty=qty) for i in range(3)]
        asks = [Level(price=quantize_up(mid + half + i * step, m.tick_decimals), qty=qty) for i in range(3)]
        return BookSnapshot(symbol=symbol, ts=now, source="rest", bids=bids, asks=asks)

    def publish(self, now: float) -> None:
        for symbol in SYMBOLS:
            if symbol not in self.skip:
                self.bus.publish_book(self.book(symbol, now))

    def shock(self, factor: str) -> None:
        self.usdt *= D(factor)
        self.btc *= D(factor)


def make_cfg(tmp_path: Path, **overrides: Any) -> Config:
    values = {
        "markets.watchlist": list(SYMBOLS),
        "data.sqlite_path": str(tmp_path / "wta.sqlite"),
        "data.ws_enabled": False,
        "engine.reconcile_interval_s": 60,
        **overrides,
    }
    return load_config(overrides=values, env={})


@dataclass
class Rig:
    engine: Engine
    clock: SimClock
    feed: FakeFeed
    store: Store
    paper: PaperExchange
    tmp: Path

    @property
    def logs(self) -> Path:
        return self.tmp / "logs"

    async def tick(self, n: int = 1, dt: float = 2.0) -> None:
        for _ in range(n):
            self.clock.advance(dt)
            now = self.clock.now()
            self.feed.publish(now)
            await self.engine.tick(now)

    async def tick_until(self, done: Callable[[], bool], limit: int = 120) -> int:
        for i in range(limit):
            if done():
                return i
            await self.tick()
        raise AssertionError(f"condition not reached in {limit} ticks")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        out = list(iter_family(self.logs, "events", strict=True))
        return [e for e in out if kind is None or e["kind"] == kind]

    def decisions(self) -> dict[str, dict[str, Any]]:
        return merge_decisions(iter_family(self.logs, "decisions", strict=True))

    def orders_log(self) -> list[dict[str, Any]]:
        return list(iter_family(self.logs, "orders", strict=True))

    def close(self, reason: str = "test end") -> None:
        self.engine.shutdown(reason)
        self.store.close()


async def make_rig(
    tmp_path: Path,
    *,
    cfg: Config | None = None,
    injected: list[engine_mod.InjectedIntent] | None = None,
    balances: dict[str, Decimal] | None = None,
    start: float = T0,
    sources: list[Callable[[RiskContext], list[Intent]]] | None = None,
    clock: SimClock | None = None,
    publish: bool = True,
) -> Rig:
    cfg = cfg or make_cfg(tmp_path)
    markets = load_markets_snapshot()
    clock = clock or SimClock(start)
    bus = MarketDataBus()
    feed = FakeFeed(bus, markets)
    if publish:
        feed.publish(start)
    store = Store(cfg.data.sqlite_path, flush_interval_s=cfg.data.store_flush_interval_s)
    state = store.kv_get(KV_PAPER)
    tick_clock = TickClock(clock)  # as build_engine wires it: frozen at the tick's now while a tick runs
    paper = PaperExchange(
        markets,
        bus.snapshot,
        tick_clock,
        initial_balances=None if state is not None else (balances or START_BALANCES),
        state=state,
    )
    engine = Engine(
        cfg,
        Settings(_env_file=None),
        "shadow",
        tick_clock,
        bus,
        store,
        exchange=paper,
        markets=markets,
        markets_source="snapshot",
        feed="test",
        injected=injected,
        intent_sources=sources,
        kill_file=tmp_path / "KILL",
        log_dir=tmp_path / "logs",
    )
    await engine.startup()
    return Rig(engine, clock, feed, store, paper, tmp_path)


def inject_file(tmp_path: Path, *rows: dict[str, Any]) -> list[engine_mod.InjectedIntent]:
    """Rows go to lines 2, 3, ... (line 1 is a comment), so row k gets the id suffix ``inj{k + 1}``."""
    path = tmp_path / "inject.jsonl"
    path.write_text("# acceptance intents\n" + "\n".join(json.dumps(r) for r in rows) + "\n")
    return load_injected_intents(path)


ENTRY = {  # a BTCUSDT taker entry with a server-side stop ~1 % below the market
    "at_s": 0,
    "strategy": "S3",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "order_type": "LIMIT",
    "qty": "0.00003",
    "stop_price": "82000",
    "purpose": "entry",
    "taker": True,
    "expected_edge_pct": "0.01",
    "reason": "test breakout entry",
}
TOO_SMALL = {  # vetoed: 0.01 USDT is far below the 50,000 TMN minimum order
    "at_s": 0,
    "strategy": "S2",
    "symbol": "USDTTMN",
    "side": "BUY",
    "order_type": "LIMIT",
    "qty": "0.01",
    "purpose": "rebalance",
    "reason": "test veto",
}


def _decision(rig: Rig, suffix: str) -> dict[str, Any]:
    matches = [d for k, d in rig.decisions().items() if k.endswith(suffix)]
    assert len(matches) == 1, f"{suffix}: {sorted(rig.decisions())}"
    return matches[0]


def _check(decision: dict[str, Any], code: str) -> dict[str, Any]:
    return next(c for c in decision["risk"]["checks"] if c["code"] == code)


def _non_tmn_value(rig: Rig) -> Decimal:
    val = rig.engine.portfolio.last_valuation
    assert val is not None
    return val.usdt_tmn_value + val.coins_tmn_value


# ---------------------------------------------------------------- entry -> stop -> exit -> outcome


async def test_injected_entry_fills_gets_a_server_stop_and_a_complete_decision_trail(tmp_path: Path) -> None:
    entry = {**ENTRY, "features": {"time_stop_s": 20}}
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, entry, TOO_SMALL))
    await rig.tick()  # injections due: the entry is placed (taker), the tiny rebalance is vetoed
    om = rig.engine.order_manager
    assert [m.purpose for m in om.live()] == ["entry"]

    await rig.tick()  # the paper exchange fills the entry; the OrderManager places the server stop
    (pos,) = rig.engine.portfolio.positions.values()
    assert pos.meta["stop_mode"] == "server" and pos.stop_order_client_id
    stop = om.managed[pos.stop_order_client_id]
    assert stop.is_stop and stop.order.req.type == "STOP_LIMIT" and stop.order.req.stop_price == D("82000")
    assert rig.paper._locked["BTC"] == stop.order.req.qty  # the resting stop locks the coins

    # time stop after 20 s: the exit passes BALANCE only because the stop's locked coins count as free
    await rig.tick_until(lambda: not rig.engine.portfolio.positions, limit=20)
    assert not om.live()

    entry_rec = _decision(rig, "_S3_BTCUSDT_inj2")
    assert entry_rec["kind"] == "order" and entry_rec["risk"]["approved"] is True
    assert [c["code"] for c in entry_rec["risk"]["checks"]] == list(CHECK_CODES)
    assert entry_rec["order"]["accepted"] is True and entry_rec["intent"]["features"]["injected"] is True
    assert entry_rec["outcome"]["exit_reason"] == "time_stop" and "pnl_tmn" in entry_rec["outcome"]

    stop_rec = rig.decisions()[f"{entry_rec['decision_id']}_stop1"]
    assert stop_rec["order"]["request"]["type"] == "STOP_LIMIT"
    assert stop_rec["outcome"]["done_reason"] == "CANCELED_FOR_EXIT"  # canceled right before the exit

    exit_rec = next(d for d in rig.decisions().values() if d["intent"] and d["intent"]["purpose"] == "exit")
    assert exit_rec["risk"]["approved"] is True and exit_rec["outcome"]["status"] == "FILLED"
    assert "<= free 0.000029961" in _check(exit_rec, "BALANCE")["detail"]

    veto = _decision(rig, "_S2_USDTTMN_inj3")
    assert veto["kind"] == "veto" and veto["risk"]["approved"] is False
    assert veto["risk"]["veto_reason"].startswith("MIN_NOTIONAL")
    assert len(veto["risk"]["checks"]) >= len(CHECK_CODES)

    places = [r for r in rig.orders_log() if r["action"] == "place"]
    assert len(places) == 3  # entry, server stop, exit; the veto never reached the exchange
    rig.close()
    assert check_logs(rig.logs).ok


# ---------------------------------------------------------------- drawdown kill -> flatten -> reset


async def test_drawdown_kill_cancels_entries_flattens_and_reset_kill_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        ENTRY,
        {  # a resting maker entry on BTCTMN, 1 % below the market: the kill must cancel it
            "at_s": 4,
            "strategy": "S1",
            "symbol": "BTCTMN",
            "side": "BUY",
            "order_type": "LIMIT",
            "qty": "0.000003",
            "price": str(quantize_down(D("82877") * D("266739") * D("0.99"), 0)),
            "purpose": "entry",
            "expected_edge_pct": "0.02",
            "reason": "resting entry",
        },
        {**TOO_SMALL, "at_s": 20, "qty": "1", "reason": "entry while killed"},
    ]
    # the rig's store is the file the CLI resolves for shadow (``{mode}`` -> shadow)
    cfg = make_cfg(tmp_path, **{"data.sqlite_path": str(tmp_path / "wta.shadow.sqlite")})
    rig = await make_rig(tmp_path, cfg=cfg, injected=inject_file(tmp_path, *rows))
    await rig.tick(3)  # entry filled + stop placed; resting BTCTMN entry placed
    assert len(rig.engine.portfolio.positions) == 1
    resting = [m for m in rig.engine.order_manager.live() if m.symbol == "BTCTMN"]
    assert len(resting) == 1 and resting[0].purpose == "entry"

    # dollar crash: USDTTMN -16 % while BTCTMN stays put (BTCUSDT rises): the USDT holding loses
    # ~5.6 % of equity, the resting BTCTMN BUY is not crossed and the BTC stop is not hit
    rig.feed.usdt *= D("0.84")
    rig.feed.btc /= D("0.84")
    await rig.tick()
    eng = rig.engine
    assert eng.flags.killed and eng.flags.kill_reason.startswith("drawdown_kill")
    (kill,) = rig.events("kill")
    assert kill["data"]["source"] == "drawdown" and D(kill["data"]["drawdown_pct"]) >= D("0.05")
    start_flat = [e for e in rig.events("flatten") if e["data"]["kind"] == "kill_flatten_start"]
    assert start_flat and start_flat[0]["data"]["entries_canceled"] == 1
    assert resting[0].done and resting[0].done_reason == "kill"

    await rig.tick_until(lambda: any(e["data"]["kind"] == "complete" for e in rig.events("flatten")))
    assert not eng.portfolio.positions
    assert _non_tmn_value(rig) < D("50000")  # only dust left
    assert eng.flatten.is_complete(eng._context(rig.clock.now(), eng._snapshot()))
    entry_rec = _decision(rig, "_S3_BTCUSDT_inj2")
    assert entry_rec["outcome"]["exit_reason"] == "kill"
    while_killed = _decision(rig, "_S2_USDTTMN_inj4")
    assert while_killed["kind"] == "veto" and _check(while_killed, "KILLED")["passed"] is False
    flatten_sells = [d for d in rig.decisions().values() if d["strategy"] == "FLATTEN"]
    assert flatten_sells and all(d["intent"]["side"] == "SELL" for d in flatten_sells)

    # monitoring mode: heartbeats still go out while killed
    assert eng.write_heartbeat(rig.clock.now()).killed is True

    # manual reset through the CLI; the running engine applies it on its next tick
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(engine_mod, "KILL_FILE", tmp_path / "KILL")
    rig.store.flush()
    argv = [
        "reset-kill",
        "--reason",
        "drawdown reviewed",
        "--log-dir",
        str(rig.logs),
        "--set",
        f"data.state_sqlite_path={tmp_path / 'wta.{mode}.sqlite'}",  # reset-kill works on the engine's state DB
    ]
    assert cli.main(argv) == 0
    await rig.tick()
    assert not eng.flags.killed and eng.portfolio.drawdown_pct == 0
    assert eng.portfolio.peak_equity_tmn == eng.portfolio.equity_tmn
    assert [e["reason"] for e in rig.events("reset_kill")] == ["drawdown reviewed"]
    assert len(rig.events("reset_kill_applied")) == 1
    assert rig.store.kv_get(KV_KILL_RESET)["applied"] is True
    await rig.tick(3)
    assert not eng.flags.killed and len(rig.events("kill")) == 1  # the old drawdown does not kill again
    rig.close()
    assert check_logs(rig.logs).ok


# ---------------------------------------------------------------- other kill triggers


async def test_kill_file_kills_and_heartbeats_continue(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick()
    (tmp_path / "KILL").write_text("")
    await rig.tick()
    assert rig.engine.flags.killed and rig.engine.flags.kill_reason == "kill file"
    (kill,) = rig.events("kill")
    assert kill["data"]["source"] == "kill_file"
    assert rig.store.kv_get(KV_FLAGS)["killed"] is True  # persisted at once
    record = rig.engine.write_heartbeat(rig.clock.now())
    assert record.killed and "killed: kill file" in record.reasons
    rig.close()


async def test_web_kill_request_kills_once_and_a_reset_supersedes_a_pending_one(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick()

    def other_process(fn: Callable[[Store, EventsLog, float], Any]) -> Any:  # the web dashboard's own Store
        store, events = Store(rig.store.path), EventsLog(rig.logs, rig.clock)
        try:
            return fn(store, events, rig.clock.now())
        finally:
            store.close()
            events.close()

    other_process(lambda st, ev, now: request_kill(st, ev, "demo of the kill-switch", now))
    await rig.tick()
    eng = rig.engine
    assert eng.flags.killed and eng.flags.kill_reason == "web dashboard kill: demo of the kill-switch"
    (kill,) = rig.events("kill")
    assert kill["data"]["source"] == "web"
    assert [e["reason"] for e in rig.events("kill_request")] == ["demo of the kill-switch"]
    assert rig.store.kv_get(KV_KILL_REQUEST)["applied"] is True
    assert rig.store.kv_get(KV_FLAGS)["killed"] is True  # persisted at once, so a reset finds it

    # a second Kill while killed is ignored (and marked applied, so it never fires after the reset)
    other_process(lambda st, ev, now: request_kill(st, ev, "twice", now))
    await rig.tick()
    assert len(rig.events("kill")) == 1 and rig.store.kv_get(KV_KILL_REQUEST)["applied"] is True
    assert any("already active" in e["reason"] for e in rig.events("operator_command_ignored"))

    # a Kill written but not applied yet is superseded by a reset: the reset never kills again
    other_process(lambda st, ev, now: request_kill(st, ev, "pending", now))
    record = other_process(lambda st, ev, now: request_kill_reset(st, ev, "drill over", now))
    assert record is not None
    await rig.tick(2)
    assert not eng.flags.killed and len(rig.events("kill")) == 1
    assert rig.store.kv_get(KV_KILL_REQUEST)["superseded_by_reset_ts"] is not None
    rig.close()
    assert check_logs(rig.logs).ok


async def test_exception_storm_kills_but_the_engine_keeps_running(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick()
    real_poll = rig.engine.order_manager.poll

    async def broken_poll(books: Any, now: float) -> Any:
        raise RuntimeError("bug in the poll")

    rig.engine.order_manager.poll = broken_poll  # type: ignore[method-assign]
    for _ in range(3):
        rig.clock.advance(2)
        rig.feed.publish(rig.clock.now())
        assert await rig.engine.safe_tick(rig.clock.now()) is False
    assert rig.engine.flags.killed and rig.engine.flags.kill_reason.startswith("exception storm: 3")
    assert len(rig.events("tick_error")) == 3 and rig.events("kill")[0]["data"]["source"] == "exception_storm"
    rig.engine.order_manager.poll = real_poll  # type: ignore[method-assign]
    rig.clock.advance(2)
    rig.feed.publish(rig.clock.now())
    assert await rig.engine.safe_tick(rig.clock.now()) is True
    rig.close()


async def test_crashes_spread_over_more_than_the_window_do_not_kill(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, **{"engine.exception_storm_window_s": 10})
    rig = await make_rig(tmp_path, cfg=cfg)

    async def broken_poll(books: Any, now: float) -> Any:
        raise RuntimeError("rare bug")

    rig.engine.order_manager.poll = broken_poll  # type: ignore[method-assign]
    for _ in range(4):
        rig.clock.advance(6)  # two crashes at most inside any 10 s window
        await rig.engine.safe_tick(rig.clock.now())
    assert not rig.engine.flags.killed and rig.engine.tick_crashes == 4
    rig.close()


# ---------------------------------------------------------------- deadline phases


async def test_flatten_start_cancels_entries_blocks_new_ones_and_sells_to_tmn(tmp_path: Path) -> None:
    end = T0 + 3 * 3600
    cfg = make_cfg(tmp_path, **{"engine.challenge_end_utc": iso(end)})
    flatten_start = end - 7200
    bid = D("266739") * D("0.99")
    rows = [
        {**TOO_SMALL, "qty": "1", "price": str(quantize_down(bid, 0)), "reason": "resting rebalance"},
        {**ENTRY, "at_s": 30, "reason": "entry after flatten_start"},
    ]
    rig = await make_rig(tmp_path, cfg=cfg, injected=inject_file(tmp_path, *rows), start=flatten_start - 20)
    await rig.tick()
    (rebalance,) = rig.engine.order_manager.live()
    assert rebalance.purpose == "rebalance"

    await rig.tick(15)  # crosses flatten_start; the entry is due 10 s later
    assert rig.engine.flags.flatten_phase == 1
    (start_event,) = [e for e in rig.events("flatten") if e["data"]["kind"] == "flatten_start"]
    assert start_event["data"]["orders_canceled"] == 1 and rebalance.done_reason == "flatten_start"
    late_entry = _decision(rig, "_S3_BTCUSDT_inj3")
    assert late_entry["kind"] == "veto" and _check(late_entry, "DEADLINE")["passed"] is False

    await rig.tick_until(lambda: any(e["data"]["kind"] == "complete" for e in rig.events("flatten")))
    assert _non_tmn_value(rig) < D("50000")
    flatten = [d for d in rig.decisions().values() if d["strategy"] == "FLATTEN"]
    assert flatten and {d["intent"]["order_type"] for d in flatten} == {"LIMIT"}  # maker-first in phase 1
    rig.close()
    assert check_logs(rig.logs).ok


async def test_a_late_start_applies_every_phase_sells_with_market_orders_and_stops_at_the_end(
    tmp_path: Path,
) -> None:
    end = T0 + 3 * 3600
    cfg = make_cfg(tmp_path, **{"engine.challenge_end_utc": iso(end)})
    balances = {**START_BALANCES, "BTC": D("0.00005")}
    rows = [{**ENTRY, "at_s": 1700, "reason": "after the end"}]
    rig = await make_rig(
        tmp_path, cfg=cfg, injected=inject_file(tmp_path, *rows), balances=balances, start=end - 1700
    )
    await rig.tick()  # restart at T-28min: flatten_start and flatten_final are both applied, in order
    kinds = [e["data"]["kind"] for e in rig.events("flatten")]
    assert kinds == ["flatten_start", "flatten_final"] and rig.engine.flags.flatten_phase == 2
    flatten = [d for d in rig.decisions().values() if d["strategy"] == "FLATTEN"]
    assert sorted(d["symbol"] for d in flatten) == ["BTCTMN", "USDTTMN"]
    assert all(d["intent"]["order_type"] == "MARKET" for d in flatten)

    await rig.tick(2)
    assert _non_tmn_value(rig) < D("50000")
    rig.clock.set(end - 301)
    await rig.tick()  # T-5min
    (final,) = rig.events("final_state")
    assert final["data"]["complete"] is True and final["reason"].startswith("final state: 100% TMN")
    rig.clock.set(end - 1)
    await rig.tick()  # the challenge is over; the injected entry is due now and vetoed
    assert rig.engine.flags.flatten_phase == 3
    assert [e["data"]["kind"] for e in rig.events("flatten")][-1] == "challenge_end"
    after = _decision(rig, "_S3_BTCUSDT_inj2")
    assert after["kind"] == "veto" and "challenge is over" in _check(after, "DEADLINE")["detail"]
    end_iso = iso(end)
    assert not [r for r in rig.orders_log() if r["action"] == "place" and r["ts"] >= end_iso]
    rig.close()


# ---------------------------------------------------------------- restart


async def test_restart_keeps_peak_day_start_fees_counters_positions_and_the_kill_flag(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick(4)
    p = rig.engine.portfolio
    (pid,) = p.positions
    stop_cid = p.positions[pid].stop_order_client_id
    before = {
        "peak": p.peak_equity_tmn,
        "day_start": p.day_start_equity_tmn,
        "fees": p.fees_paid_tmn_total,
        "orders_today": p.orders_today(rig.clock.now()),
    }
    assert before["fees"] > 0 and before["orders_today"] == 2  # entry + server stop
    paper_btc = rig.paper._free.get("BTC"), rig.paper._locked.get("BTC")
    rig.close("restart test")

    rig2 = await make_rig(tmp_path, start=rig.clock.now() + 30)  # back 30 s later
    p2 = rig2.engine.portfolio
    now = rig2.clock.now()
    after = {
        "peak": p2.peak_equity_tmn,
        "day_start": p2.day_start_equity_tmn,
        "fees": p2.fees_paid_tmn_total,
        "orders_today": p2.orders_today(now),
    }
    assert after == before
    assert list(p2.positions) == [pid] and p2.positions[pid].stop_order_client_id == stop_cid
    assert stop_cid in rig2.engine.order_manager.managed  # the server stop is still managed
    assert (rig2.paper._free.get("BTC"), rig2.paper._locked.get("BTC")) == paper_btc
    starts = rig2.events("start")
    assert [s["data"]["reason"] for s in starts] == ["fresh", "restart"]
    (restart,) = rig2.events("restart")
    assert restart["data"]["restore"]["positions"] == [pid]
    await rig2.tick(2)
    assert list(p2.positions) == [pid] and not rig2.engine.flags.killed

    (tmp_path / "KILL").write_text("")
    await rig2.tick()
    assert rig2.engine.flags.killed
    rig2.close("restart again")
    (tmp_path / "KILL").unlink()

    rig3 = await make_rig(tmp_path, start=rig2.clock.now() + 30)
    assert rig3.engine.flags.killed and rig3.engine.flags.kill_reason == "kill file"
    rig3.close()


async def test_a_reset_written_while_the_engine_was_down_is_applied_at_startup(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick(6)  # a few equity rows (~5.67M TMN)
    (tmp_path / "KILL").write_text("")
    await rig.tick()
    (tmp_path / "KILL").unlink()
    rig.close()
    store = Store(tmp_path / "wta.sqlite")
    store.add_equity(T0 + 1, D("9000000"), peak_tmn=D("9000000"))  # an old, higher peak before the reset
    events = EventsLog(tmp_path / "logs", SimClock(T0 + 100))
    record = request_kill_reset(store, events, "operator checked the bot", T0 + 100)
    events.close()
    store.close()
    assert record is not None and record["applied"] is False
    assert D(record["peak_equity_tmn"]) < D("6000000")  # the latest equity row, not the old peak

    rig2 = await make_rig(tmp_path, start=T0 + 200)
    eng = rig2.engine
    assert not eng.flags.killed
    assert eng.portfolio.peak_equity_tmn == D(record["peak_equity_tmn"])  # 9M must not come back
    assert rig2.store.kv_get(KV_KILL_RESET)["applied"] is True
    assert len(rig2.events("reset_kill_applied")) == 1
    await rig2.tick(2)
    assert not eng.flags.killed and eng.portfolio.drawdown_pct < D("0.01")
    rig2.close()


# ---------------------------------------------------------------- replay inputs, vetoes, health


async def test_engine_books_and_ticks_are_stored_once_per_new_book(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick(2)
    rig.feed.skip = {"BTCTMN"}  # its last book stays on the bus: used again, not stored again
    await rig.tick(2)
    rig.close()
    store = Store.open_readonly(tmp_path / "wta.sqlite")
    ticks = list(store.iter_ticks("shadow"))
    books = list(store.iter_engine_books("shadow"))
    store.close()
    assert [t["ts"] for t in ticks] == [T0 + 2, T0 + 4, T0 + 6, T0 + 8]
    assert ticks[-1]["book_ts"] == {"BTCTMN": T0 + 4, "BTCUSDT": T0 + 8, "USDTTMN": T0 + 8}
    counts = {s: sum(1 for b in books if b.symbol == s) for s in SYMBOLS}
    assert counts == {"USDTTMN": 4, "BTCTMN": 2, "BTCUSDT": 4}
    assert len(books[0].bids) == 3  # every level the engine saw


async def test_identical_vetoes_are_logged_once_per_minute(tmp_path: Path) -> None:
    def tiny_rebalance(ctx: RiskContext) -> list[Intent]:
        return [
            Intent(
                id=f"d_{int(ctx.now * 1000)}_S2_USDTTMN_1",
                ts=ctx.now,
                strategy="S2",
                symbol="USDTTMN",
                side="BUY",
                order_type="LIMIT",
                qty=D("0.01"),
                purpose="rebalance",
                reason="too small every tick",
            )
        ]

    rig = await make_rig(tmp_path, sources=[tiny_rebalance])
    await rig.tick(int(VETO_REPEAT_S // 2) + 2)  # 64 s of ticks
    vetoes = [d for d in rig.decisions().values() if d["kind"] == "veto"]
    assert len(vetoes) == 2
    assert vetoes[1]["features"]["veto_repeats_suppressed"] == int(VETO_REPEAT_S // 2) - 1
    rig.close()


async def test_heartbeat_reports_portfolio_risk_and_feed(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick(3)
    record = rig.engine.write_heartbeat(rig.clock.now())
    assert record.equity_tmn is not None and record.positions == 1 and record.open_orders == 1
    extra = record.extra
    assert extra["feed"] == "test" and extra["markets_source"] == "snapshot" and extra["flatten_phase"] == 0
    assert extra["orders_today"] == 2 and D(extra["fees_paid_tmn"]) > 0
    assert D(extra["coin_exposure_pct"]) > 0 and D(extra["usdt_share"]) > 0
    assert {"daily_pnl_pct", "fee_budget_used_pct"} <= set(extra)
    rig.close()


async def test_a_new_utc_day_logs_day_start(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, start=T0 + 14 * 3600 - 3)  # 23:59:57 UTC
    await rig.tick(3)
    (event,) = rig.events("day_start")
    assert event["reason"].startswith("new UTC day 2026-10-23")
    rig.close()


# ---------------------------------------------------------------- wiring, synthetic feed, CLI


async def test_run_engine_wires_paper_exchange_and_the_synthetic_feed(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        **{
            "engine.fast_loop_interval_s": 0.05,
            "engine.heartbeat_interval_s": 0.3,
            "data.store_flush_interval_s": 0.1,
            "markets.watchlist": ["USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT"],
        },
    )
    code = await run_engine(
        cfg,
        Settings(_env_file=None),
        "shadow",
        feed="synthetic",
        seconds=1.2,
        log_dir=tmp_path / "logs",
        handle_signals=False,
    )
    assert code == 0
    events = list(iter_family(tmp_path / "logs", "events", strict=True))
    assert [e["kind"] for e in events] == ["start", "stop"]
    start = events[0]["data"]
    assert start["feed"] == "synthetic" and start["synthetic"] is True and "never evidence" in start["note"]
    assert (
        start["reason"] == "fresh" and start["markets_source"] == "snapshot" and start["exchange"] == "paper"
    )
    health = list(iter_family(tmp_path / "logs", "health", strict=True))
    assert health and all(h["extra"]["feed"] == "synthetic" for h in health)
    assert health[-1]["equity_tmn"] is not None and health[-1]["reasons"] == []
    state_db = tmp_path / "wta.state_shadow_synthetic.sqlite"  # synthetic data: its own account
    assert start["strategies"] == ["S1", "S2"] and start["state_db"] == str(state_db)
    # the engine state lives in the per-mode state DB derived from data.sqlite_path (PRD-03 wiring)
    assert not (tmp_path / "wta.sqlite").exists()
    store = Store.open_readonly(state_db)
    counts = store.counts()
    tmn = store.kv_get(KV_PAPER)["balances"]["TMN"]
    store.close()
    # S2's initial allocation may already lock (or spend) TMN for USDT; nothing else trades
    assert 0 < D(tmn["free"]) + D(tmn["locked"]) <= D("5000000")
    assert counts["equity"] >= 1 and counts["ticks"] >= 5 and counts["engine_books"] >= 5
    assert counts["candles"] >= 5  # synthetic candles go to the state DB (and the candle cache)
    assert check_logs(tmp_path / "logs").ok


def test_synthetic_shock_hits_the_dollar_and_every_coin_once() -> None:
    markets = load_markets_snapshot()
    bus = MarketDataBus()
    params = SyntheticParams(seed=1, shock_at_s=10, shock_pct=-0.08, jump_rate_per_hour=0)
    feed = SyntheticFeed(
        markets,
        ["USDTTMN", "BTCTMN", "BTCUSDT"],
        bus,
        {"USDTTMN": D("266739"), "BTCUSDT": D("82877")},
        {},
        params=params,
    )
    feed.publish(T0)
    before = {s: feed.mid(s) for s in ("USDTTMN", "BTCUSDT", "BTCTMN")}
    feed.publish(T0 + 5)
    assert feed.shocked_ts is None
    feed.publish(T0 + 10)
    assert feed.shocked_ts == T0 + 10
    after = {s: feed.mid(s) for s in before}
    assert after["USDTTMN"] == pytest.approx(before["USDTTMN"] * 0.92)
    assert after["BTCUSDT"] == pytest.approx(before["BTCUSDT"] * 0.92)
    assert after["BTCTMN"] == pytest.approx(before["BTCTMN"] * 0.92 * 0.92)
    feed.publish(T0 + 20)
    assert feed.mid("USDTTMN") == pytest.approx(after["USDTTMN"])  # applied once only


def test_injected_intents_are_validated_with_line_numbers(tmp_path: Path) -> None:
    good = parse_injected_line({**ENTRY, "features": {"time_stop_s": 5}}, 3)
    intent = good.to_intent(T0)
    assert intent.id == "d_1792663200000_S3_BTCUSDT_inj3" and intent.qty == D("0.00003")
    assert intent.reason.startswith("[injected for acceptance testing]")
    assert intent.features == {"time_stop_s": 5, "injected": True, "inject_line": 3}
    bad = [
        ({**ENTRY, "colour": "red"}, "unknown field"),
        ({**ENTRY, "strategy": "TEST"}, "must start with one of"),
        ({k: v for k, v in ENTRY.items() if k != "qty"}, "qty or notional"),
        ({**ENTRY, "side": "HOLD"}, "side must be"),
        ({**ENTRY, "qty": "lots"}, "qty is not a number"),
        ({**ENTRY, "at_s": -1}, "at_s must be >= 0"),
    ]
    for obj, message in bad:
        with pytest.raises(ValueError, match=message):
            parse_injected_line(obj, 7)
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(ENTRY) + "\nnot json\n")
    with pytest.raises(ValueError, match="line 2: not valid JSON"):
        load_injected_intents(path)


def test_paper_balances_accept_inline_json_or_a_file(tmp_path: Path) -> None:
    assert parse_paper_balances('{"tmn": 1000, "USDT": "2.5"}') == {"TMN": D("1000"), "USDT": D("2.5")}
    path = tmp_path / "b.json"
    path.write_text('{"BTC": "0.0001"}')
    assert parse_paper_balances(str(path)) == {"BTC": D("0.0001")}
    with pytest.raises(ValueError, match="negative"):
        parse_paper_balances('{"TMN": -1}')


def test_cli_run_passes_phase2_options_only_when_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run_engine(cfg: Any, settings: Any, mode: str, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_engine", fake_run_engine)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))
    path = tmp_path / "i.jsonl"
    path.write_text(json.dumps(ENTRY) + "\n")
    argv = [
        "run",
        "--feed",
        "synthetic",
        "--seconds",
        "3",
        "--inject-intents",
        str(path),
        "--paper-balances",
        '{"TMN": 1000000}',
        "--synthetic-shock-at",
        "60",
        "--synthetic-shock-pct",
        "-0.08",
    ]
    assert cli.main(argv) == 0
    (kwargs,) = calls
    assert kwargs["feed"] == "synthetic" and kwargs["synthetic_shock"] == (60.0, -0.08)
    assert kwargs["paper_balances"] == {"TMN": D("1000000")} and len(kwargs["injected"]) == 1
    assert cli.main(["run", "--synthetic-shock-at", "5", "--synthetic-shock-pct", "-0.1"]) == 2  # live feed
    assert cli.main(["run", "--feed", "synthetic", "--synthetic-shock-at", "5"]) == 2  # pct missing
    path.write_text('{"at_s": 0}\n')
    assert cli.main(["run", "--inject-intents", str(path)]) == 2
    assert len(calls) == 1


def test_cli_reset_kill_needs_a_reason_and_refuses_while_the_kill_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))
    kill_file = tmp_path / "KILL"
    monkeypatch.setattr(engine_mod, "KILL_FILE", kill_file)
    db = tmp_path / "wta.shadow.sqlite"  # the shadow state DB of the template below
    common = ["--log-dir", str(tmp_path / "logs"), "--set", f"data.state_sqlite_path={tmp_path / 'wta.{mode}.sqlite'}"]
    assert cli.main(["reset-kill", *common]) == 2  # --reason is required
    assert cli.main(["reset-kill", "--reason", "  ", *common]) == 2
    assert cli.main(["reset-kill", "--reason", "x", *common]) == 1  # no database yet
    store = Store(db)
    store.kv_set(KV_FLAGS, {"killed": False}, T0)
    store.close()
    assert cli.main(["reset-kill", "--reason", "x", *common]) == 0
    assert "nothing to reset" in capsys.readouterr().out
    store = Store(db)
    store.kv_set(KV_FLAGS, {"killed": True, "kill_reason": "kill file", "flatten_phase": 0}, T0)
    store.close()
    kill_file.write_text("")
    assert cli.main(["reset-kill", "--reason", "x", *common]) == 1
    assert "remove it first" in capsys.readouterr().err
    kill_file.unlink()
    assert cli.main(["reset-kill", "--reason", "checked", *common]) == 0
    store = Store(db)
    assert store.kv_get(KV_FLAGS)["killed"] is False and store.kv_get(KV_KILL_RESET)["reason"] == "checked"
    assert [e["kind"] for e in store.iter_events()] == ["reset_kill"]
    store.close()
    events = list(iter_family(tmp_path / "logs", "events", strict=True))
    assert [(e["kind"], e["reason"]) for e in events] == [("reset_kill", "checked")]
    assert events[0]["data"]["previous_kill_reason"] == "kill file"


# ---------------------------------------------------------------- review fixes (Phase 2)


RESTING_BTCTMN = {  # a maker entry 1 % below the market: it rests
    "at_s": 0,
    "strategy": "S1",
    "symbol": "BTCTMN",
    "side": "BUY",
    "order_type": "LIMIT",
    "qty": "0.000003",
    "price": str(quantize_down(D("82877") * D("266739") * D("0.99"), 0)),
    "purpose": "entry",
    "expected_edge_pct": "0.02",
    "reason": "resting entry",
}


async def test_a_pause_cancels_the_working_entries(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, RESTING_BTCTMN))
    await rig.tick()
    (resting,) = rig.engine.order_manager.live()
    rig.feed.skip = {"USDTTMN"}  # the dollar book goes stale: anomaly pause
    await rig.tick(dt=31)
    assert rig.engine.flags.paused and rig.engine.flags.pause_reason.startswith("anomaly")
    (pause,) = rig.events("pause")
    assert pause["data"]["entries_canceled"] == 1
    assert resting.done and resting.done_reason == "pause"  # nothing re-prices it without a risk check
    rig.close()


async def test_a_kill_reset_cancels_the_kill_flatten_orders(tmp_path: Path) -> None:
    balances = {"TMN": D("3000000"), "USDT": D("10"), "BTC": D("0.0005")}
    rig = await make_rig(tmp_path, balances=balances)
    await rig.tick()
    (tmp_path / "KILL").write_text("")
    await rig.tick()
    eng = rig.engine
    assert eng.flags.killed and [m.purpose for m in eng.order_manager.live()] == ["flatten", "flatten"]
    (tmp_path / "KILL").unlink()
    assert request_kill_reset(rig.store, eng.events_log, "drawdown reviewed: resume", rig.clock.now())
    await rig.tick()
    assert not eng.flags.killed and eng.order_manager.live() == []
    (applied,) = rig.events("reset_kill_applied")
    assert applied["data"]["flatten_orders_canceled"] == 2
    await rig.tick(20)
    assert rig.paper._free.get("BTC", D(0)) + rig.paper._locked.get("BTC", D(0)) == D("0.0005")  # not sold
    rig.close()


async def test_a_stop_never_cuts_a_tick_in_half(tmp_path: Path) -> None:
    """A stop request while a placement is in flight: the tick finishes, the order is tracked."""
    import asyncio

    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    real_place = rig.paper.place_order

    async def slow_place(req: Any) -> Any:  # the order is registered, the answer comes late
        placed = await real_place(req)
        await asyncio.sleep(0.3)
        return placed

    rig.paper.place_order = slow_place  # type: ignore[method-assign]
    reason = await rig.engine.run(seconds=0.05, handle_signals=False)
    assert reason == "ran for 0.05 s"
    (order,) = [o for o in await rig.paper.get_open_orders() if o.req.purpose == "entry"]
    om = rig.engine.order_manager
    assert order.client_id in om.managed and order.client_id in rig.engine.portfolio.open_orders
    assert rig.engine.portfolio.placements  # counted for the rate limits
    saved = rig.store.kv_get("order_manager_state")
    assert [m["client_id"] for m in saved["managed"]] == [order.client_id]
    (stored,) = rig.store.load_orders(non_terminal_only=True)
    assert stored.client_id == order.client_id and stored.status == "NEW"
    rig.store.close()


async def test_the_startup_reconcile_waits_for_the_books_it_values_with(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick(2)
    (pid,) = rig.engine.portfolio.positions
    rig.close("restart test")
    calls: list[float] = []

    def strategy(ctx: RiskContext) -> list[Intent]:
        calls.append(ctx.now)
        return []

    rig2 = await make_rig(tmp_path, start=rig.clock.now() + 30, publish=False, sources=[strategy])
    eng = rig2.engine
    (restart,) = rig2.events("restart")
    deferred = restart["data"]["reconcile"]
    assert deferred["deferred"] is True and {"BTCUSDT", "USDTTMN"} <= set(deferred["missing_books"])
    assert eng.last_reconcile is None and list(eng.portfolio.positions) == [pid]  # nothing valued at 0
    rig2.feed.skip = set(SYMBOLS)
    await rig2.tick()
    assert calls == [] and eng.last_reconcile is None  # strategies wait for the reconcile
    rig2.feed.skip = set()
    await rig2.tick()
    assert eng.last_reconcile is not None and eng.last_reconcile["reason"] == "startup"
    assert eng.last_reconcile["closed_positions"] == [] and list(eng.portfolio.positions) == [pid]
    assert calls == [rig2.clock.now()]
    rig2.close()


async def test_live_trading_refuses_to_start_without_a_challenge_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wta import safety
    from wta.engine import build_engine
    from wta.safety import LiveGuardError

    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    settings = Settings(_env_file=None, wallex_api_key="engine-test-key-0000", wallex_base_url="http://127.0.0.1:9")
    cfg = make_cfg(tmp_path)
    assert cfg.engine.challenge_end_utc is None
    with pytest.raises(LiveGuardError, match="CHALLENGE_END_UTC"):
        await build_engine(cfg, settings, "live", i_understand_live=True, log_dir=tmp_path / "logs")
    assert not Path(cfg.data.sqlite_path).exists()  # refused before anything was built
    past = make_cfg(tmp_path, **{"engine.challenge_end_utc": "2001-01-01T00:00:00Z"})
    with pytest.raises(LiveGuardError, match="the challenge ended"):
        await build_engine(past, settings, "live", i_understand_live=True, log_dir=tmp_path / "logs")
    # Engine.run checks it again (any other entry point)
    rig = await make_rig(tmp_path)
    live = Engine(
        cfg, settings, "live", SimClock(T0), MarketDataBus(), rig.store, exchange=rig.paper,
        markets=load_markets_snapshot(), log_dir=tmp_path / "logs2", i_understand_live=True,
    )
    with pytest.raises(LiveGuardError, match="CHALLENGE_END_UTC"):
        await live.run(seconds=0.1, handle_signals=False)
    assert live.ticks == 0
    rig.close()


async def test_one_engine_per_account(tmp_path: Path) -> None:
    from wta.engine import EngineLockError, build_engine

    cfg = make_cfg(tmp_path)

    async def build() -> Engine:
        return await build_engine(
            cfg, Settings(_env_file=None), "shadow", feed="synthetic", log_dir=tmp_path / "logs",
            kill_file=tmp_path / "KILL",
        )

    first = await build()
    with pytest.raises(EngineLockError, match="another engine or flatten_now"):
        await build()
    await first.aclose()
    again = await build()  # released by aclose()
    await again.aclose()


class SteppingClock(SimClock):
    """Every read is 0.7 ms later than the previous one (like a real clock inside a long tick)."""

    def now(self) -> float:
        t = self._t
        self._t += 0.0007
        return t


async def test_ids_and_timers_come_from_the_tick_time_not_from_later_clock_reads(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY), clock=SteppingClock(T0))
    await rig.tick()
    tick_ts = max(t["ts"] for t in rig.store.iter_ticks("shadow"))
    (mo,) = rig.engine.order_manager.live()
    assert mo.client_id.split("_")[2] == f"{int(round(tick_ts * 1000)):013d}"
    assert mo.first_ts == mo.placed_ts == tick_ts and rig.engine.portfolio.placements == [tick_ts]
    assert mo.order.created_ts == tick_ts  # the paper exchange reads the frozen clock too
    rig.close()


async def test_a_crashed_tick_is_still_recorded_for_replay(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick()

    async def broken_poll(books: Any, now: float) -> Any:
        raise RuntimeError("bug in the poll")

    rig.engine.order_manager.poll = broken_poll  # type: ignore[method-assign]
    rig.clock.advance(2)
    now = rig.clock.now()
    rig.feed.publish(now)
    assert await rig.engine.safe_tick(now) is False
    assert now in [t["ts"] for t in rig.store.iter_ticks("shadow")]
    # the paper fill of the crashed tick is saved together with the paper account
    assert rig.store.kv_get(KV_PAPER)["tick"] == rig.paper.tick_count
    rig.close()


async def test_a_crash_inside_a_tick_restores_the_previous_tick(tmp_path: Path) -> None:
    """kill -9 between the paper fill and the end of the tick: nothing of that tick is on disk."""
    from wta.execution.reconcile import KV_ORDER_MANAGER, KV_PORTFOLIO

    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick()  # the entry is placed and committed
    committed = {k: rig.store.kv_get(k) for k in (KV_PAPER, KV_PORTFOLIO, KV_ORDER_MANAGER)}
    orders = [(o.client_id, o.status, o.filled_qty) for o in rig.store.load_orders()]

    async def die(books: Any, now: float) -> Any:  # the paper exchange has just filled the entry
        assert rig.paper._free.get("BTC", D(0)) > 0
        rig.store._conn.close()
        raise SystemExit("killed")

    rig.engine.order_manager.poll = die  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        await rig.tick()
    reopened = Store(tmp_path / "wta.sqlite")
    assert {k: reopened.kv_get(k) for k in committed} == committed  # one consistent tick, not a mix
    assert [(o.client_id, o.status, o.filled_qty) for o in reopened.load_orders()] == orders
    reopened.close()


async def test_safe_tick_never_raises_even_when_the_store_fails_during_a_storm(tmp_path: Path) -> None:
    import sqlite3

    rig = await make_rig(tmp_path)
    await rig.tick()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug")

    def locked(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    rig.engine.order_manager.poll = broken  # type: ignore[method-assign]
    rig.engine._kill = broken  # type: ignore[method-assign]
    rig.engine._persist_flags = locked  # type: ignore[method-assign]
    for _ in range(3):
        rig.clock.advance(2)
        assert await rig.engine.safe_tick(rig.clock.now()) is False
    assert rig.engine.flags.killed  # set in memory; the next successful save writes it
    rig.close()


async def test_a_stranded_server_stop_is_replaced_by_a_taker_stop_exit(tmp_path: Path) -> None:
    """The price gaps through the STOP_LIMIT's limit (82000 x 0.996 = 81672): it triggers but can
    never fill there. The position must not stay unprotected until a time stop."""
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick(2)
    (pos,) = rig.engine.portfolio.positions.values()
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    rig.feed.btc *= D("0.97")  # bid ~80,350: below the stop and its limit
    await rig.tick_until(lambda: not rig.engine.portfolio.positions, limit=10)
    exits = [d for d in rig.decisions().values() if d["intent"] and d["intent"]["purpose"] == "stop"]
    stranded = [d for d in exits if d["intent"]["features"].get("stranded_server_stop") == stop_cid]
    assert stranded and stranded[0]["risk"]["approved"] is True
    entry_rec = _decision(rig, "_S3_BTCUSDT_inj2")
    assert entry_rec["outcome"]["exit_reason"] == "stop"
    assert rig.paper._free.get("BTC", D(0)) + rig.paper._locked.get("BTC", D(0)) < D("0.000001")
    rig.close()


async def test_a_server_stop_the_reconciler_finds_dead_hands_over_to_the_software_stop(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, injected=inject_file(tmp_path, ENTRY))
    await rig.tick(2)
    (pos,) = rig.engine.portfolio.positions.values()
    stop_cid = pos.stop_order_client_id
    assert stop_cid is not None
    await rig.paper.cancel_order(stop_cid)  # canceled on the exchange side
    rig.clock.advance(12)  # past the reconciler's grace period, before the next stop poll
    report = await rig.engine.reconcile(rig.engine._snapshot(), rig.clock.now(), reason="test")
    assert stop_cid in report.finalized
    assert pos.stop_order_client_id is None and pos.meta["stop_mode"] == "software"
    rig.feed.btc *= D("0.98")  # below the 82,000 stop
    await rig.tick_until(lambda: not rig.engine.portfolio.positions, limit=10)
    assert _decision(rig, "_S3_BTCUSDT_inj2")["outcome"]["exit_reason"] == "stop"
    rig.close()
