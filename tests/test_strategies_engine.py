"""PRD-03 engine wiring: strategies through the real engine path (features -> router -> risk -> orders).

Every test drives the real FeatureEngine, StrategyRouter, S1/S2, RiskManager, OrderManager,
PaperExchange, PositionManager, Store and JSONL logs through ``Engine.tick(now)`` on a ``SimClock``.
``CrossFeed`` publishes deterministic books before every tick: ``BTCTMN = BTCUSDT x USDTTMN x
(1 + lag)``, so a test opens a cross-book gap by setting ``lag``. Markets (fees: TMN 0.25 % maker /
0.30 % taker, USDT 0.10 % / 0.13 %) come from ``configs/markets_snapshot.yaml``. Nothing touches the
network or the wall clock.

S1 numbers used below: leg 1 taker, so a lock-in needs edge >= 0.30 % + 0.13 % + 0.15 % margin
= 0.58 %. With ``lag = -0.9 %`` the TMN book is ~0.79 % cheap (spreads included).
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wta import cli
from wta.config import ROOT, Config, Settings, load_config
from wta.data.collector import CandleCache, CandleRefresher
from wta.data.store import Store
from wta.engine import (
    KV_FEATURES,
    KV_STRATEGY,
    Engine,
    EngineLockError,
    TickClock,
    acquire_engine_lock,
    build_engine,
    default_strategies,
    release_engine_lock,
    state_db_path,
)
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.exchange.markets import load_markets_snapshot
from wta.exchange.paper import PaperExchange
from wta.execution.reconcile import KV_FLAGS
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
from wta.models import BookSnapshot, Candle, Intent, Level, MarketInfo, RiskCheck, RiskDecision
from wta.risk.context import RiskContext
from wta.strategies.base import Strategy
from wta.utils.decimalutil import D, quantize_down, quantize_up
from wta.utils.timeutil import SimClock

from .conftest import T0

SYMBOLS = ["USDTTMN", "BTCTMN", "BTCUSDT"]
SPREADS = {"USDTTMN": D("0.0002"), "BTCUSDT": D("0.0008"), "BTCTMN": D("0.0014")}
LEVEL_QTY = {"USDTTMN": D("500"), "BTCUSDT": D("0.05"), "BTCTMN": D("0.05")}
# 40 % USDT: S2's range target is 50 % (gap 10 pp < 15 pp threshold) -> S2 stays quiet,
# and a TMN-cheap lock-in (+20 % USDT) stays inside the 70 % USDT bound.
QUIET_BALANCES = {"TMN": D("6000000"), "USDT": D("15")}
LAG_TMN_CHEAP = D("-0.009")


# ---------------------------------------------------------------- rig


class CrossFeed:
    """Three levels per side around each mid; ``BTCTMN = BTCUSDT x USDTTMN x (1 + lag)``."""

    def __init__(self, bus: MarketDataBus, markets: dict[str, MarketInfo]) -> None:
        self.bus = bus
        self.markets = markets
        self.usdt = D("266739")
        self.btc = D("82877")
        self.lag = D(0)
        self.skip: set[str] = set()

    def mid(self, symbol: str) -> Decimal:
        if symbol == "USDTTMN":
            return self.usdt
        if symbol == "BTCUSDT":
            return self.btc
        return self.btc * self.usdt * (1 + self.lag)

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


def make_cfg(tmp_path: Path, **overrides: Any) -> Config:
    values = {
        "markets.watchlist": list(SYMBOLS),
        "data.sqlite_path": str(tmp_path / "market.sqlite"),
        "data.state_sqlite_path": str(tmp_path / "state_{mode}.sqlite"),
        "data.ws_enabled": False,
        "engine.reconcile_interval_s": 60,
        "strategies.s1_crossbook.coins": ["BTC"],
        **overrides,
    }
    return load_config(overrides=values, env={})


@dataclass
class Rig:
    engine: Engine
    clock: SimClock
    feed: CrossFeed
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

    def decisions(self) -> dict[str, dict[str, Any]]:
        return merge_decisions(iter_family(self.logs, "decisions", strict=True))

    def raw_decisions(self) -> list[dict[str, Any]]:
        return list(iter_family(self.logs, "decisions", strict=True))

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        out = list(iter_family(self.logs, "events", strict=True))
        return [e for e in out if kind is None or e["kind"] == kind]

    def close(self, reason: str = "test end") -> None:
        self.engine.shutdown(reason)
        self.store.close()


async def make_rig(
    tmp_path: Path,
    *,
    cfg: Config | None = None,
    balances: dict[str, Decimal] | None = None,
    strategies: list[Strategy] | None = None,
    start: float = T0,
    lag: Decimal = D(0),
    log_dir: Path | None = None,
    db_path: Path | None = None,
) -> Rig:
    cfg = cfg or make_cfg(tmp_path)
    markets = load_markets_snapshot()
    clock = SimClock(start)
    bus = MarketDataBus()
    feed = CrossFeed(bus, markets)
    feed.lag = lag
    feed.publish(start)
    store = Store(db_path or state_db_path(cfg, "shadow"), flush_interval_s=cfg.data.store_flush_interval_s)
    state = store.kv_get("paper_state")
    tick_clock = TickClock(clock)
    paper = PaperExchange(
        markets,
        bus.snapshot,
        tick_clock,
        initial_balances=None if state is not None else (balances or QUIET_BALANCES),
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
        kill_file=tmp_path / "KILL",
        log_dir=log_dir or tmp_path / "logs",
        strategies=default_strategies(cfg) if strategies is None else strategies,
    )
    await engine.startup()
    return Rig(engine, clock, feed, store, paper, tmp_path)


def _records(rig: Rig, **match: Any) -> list[dict[str, Any]]:
    out = []
    for rec in rig.raw_decisions():
        if rec["kind"] == "outcome":
            continue
        intent = rec.get("intent") or {}
        if all(intent.get(k) == v or rec.get(k) == v for k, v in match.items()):
            out.append(rec)
    return out


def _outcomes(rig: Rig, decision_id: str) -> list[dict[str, Any]]:
    return [r["outcome"] for r in rig.raw_decisions() if r["kind"] == "outcome" and r["decision_id"] == decision_id]


def _failed(rec: dict[str, Any]) -> list[str]:
    return [c["code"] for c in rec["risk"]["checks"] if not c["passed"]]


# ---------------------------------------------------------------- S2 initial allocation


async def test_s2_initial_allocation_goes_through_risk_and_paper_fills(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, balances={"TMN": D("5000000")})
    await rig.tick()
    (rec,) = _records(rig, strategy="S2")
    intent = rec["intent"]
    assert rec["kind"] == "order" and rec["order"]["accepted"] is True and rec["risk"]["approved"] is True
    assert intent["purpose"] == "rebalance" and intent["side"] == "BUY" and intent["symbol"] == "USDTTMN"
    assert "initial allocation from 100% TMN" in intent["reason"] and "target USDT share 50%" in intent["reason"]
    # the risk manager capped the order at 25 % of equity (1,250,000 TMN of the 2,500,000 TMN gap)
    cap = next(c for c in rec["risk"]["checks"] if c["code"] == "MAX_ORDER_PCT")
    assert cap["passed"] is True and rec["risk"]["adjusted_qty"] is not None
    order_qty = D(rec["order"]["request"]["qty"])
    price = D(rec["order"]["request"]["price"])
    assert order_qty * price <= D("1250000") * D("1.0001")
    # decision features = the compact feature snapshot of the symbols used (+ plan snapshot slot)
    assert set(rec["features"]) == {"ts", "symbols", "cross", "dollar"}
    assert list(rec["features"]["symbols"]) == ["USDTTMN"] and rec["plan_snapshot"] is None

    # the dollar dips: the resting maker BUY fills on the next tick
    rig.feed.usdt = D("266600")
    await rig.tick()
    s2 = rig.engine.router.by_id("S2")
    assert s2.rebalances == 1 and s2.last_rebalance_ts == rig.clock.now()
    (outcome,) = _outcomes(rig, intent["id"])
    assert outcome["kind"] == "order" and outcome["status"] == "FILLED" and outcome["purpose"] == "rebalance"
    usdt = rig.engine.portfolio.total("USDT")
    assert usdt == order_qty * (1 - D("0.0025"))  # a resting maker BUY pays the maker fee in USDT
    assert D("0.24") < rig.engine.portfolio.usdt_share < D("0.26")
    await rig.tick(5)
    assert len(_records(rig, strategy="S2")) == 1  # min interval: no churn
    rig.close()
    assert check_logs(rig.logs).ok


# ---------------------------------------------------------------- S1 lock-in cycle


async def test_full_s1_lockin_cycle_leg1_leg2_closed_with_outcome_pnl(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick()  # S1 sees the gap: leg 1 (taker BUY BTCTMN) is placed
    (entry,) = _records(rig, strategy="S1", purpose="entry")
    assert entry["kind"] == "order" and entry["order"]["accepted"] is True
    ei = entry["intent"]
    assert ei["symbol"] == "BTCTMN" and ei["side"] == "BUY" and ei["taker"] is True
    assert ei["features"]["cost_model"] == "lockin" and ei["features"]["leg2_symbol"] == "BTCUSDT"
    assert "TMN book" in ei["reason"] and "cheap" in ei["reason"] and "lock-in: buy" in ei["reason"]
    edge = next(c for c in entry["risk"]["checks"] if c["code"] == "EDGE_VS_COST")
    assert edge["passed"] is True
    assert set(entry["features"]["symbols"]) == {"BTCTMN", "BTCUSDT", "USDTTMN"}
    assert list(entry["features"]["cross"]) == ["BTC"]

    await rig.tick()  # leg 1 fills -> on_fill -> leg 2 (taker SELL BTCUSDT) in the same tick
    (leg2,) = _records(rig, purpose="leg2")
    li = leg2["intent"]
    assert leg2["kind"] == "order" and leg2["order"]["accepted"] is True
    assert li["symbol"] == "BTCUSDT" and li["side"] == "SELL" and li["features"]["trigger"] == "fill"
    pos_id = li["linked_position_id"]
    assert pos_id in rig.engine.portfolio.positions

    await rig.tick()  # leg 2 fills: the position closes, the entry gets its outcome
    assert pos_id not in rig.engine.portfolio.positions
    (outcome,) = _outcomes(rig, ei["id"])
    assert outcome["exit_reason"] == "lockin" and outcome["strategy"] == "S1"
    assert D(outcome["pnl_tmn"]) > 0 and D(outcome["pnl_pct"]) > D("0.002")  # ~0.79 % edge - 0.43 % fees
    assert rig.engine.router.by_id("S1").cycles == {}  # pruned after the close
    rig.feed.lag = D(0)
    await rig.tick(3)
    assert len(_records(rig, strategy="S1", purpose="entry")) == 1
    rig.close()
    assert check_logs(rig.logs).ok


async def test_vetoed_leg2_is_retried_on_the_next_tick(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    real = rig.engine.risk.evaluate
    vetoed: list[str] = []

    def evaluate(intent: Intent, ctx: RiskContext) -> RiskDecision:
        decision = real(intent, ctx)
        if intent.purpose == "leg2" and not vetoed:
            vetoed.append(intent.id)
            check = RiskCheck(code="STALE_DATA", passed=False, detail="forced by the test")
            return decision.model_copy(
                update={"approved": False, "checks": [*decision.checks, check], "veto_reason": "STALE_DATA: test"}
            )
        return decision

    rig.engine.risk.evaluate = evaluate  # type: ignore[method-assign]
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick(2)  # entry, then leg-1 fill -> leg 2 vetoed
    (veto,) = _records(rig, purpose="leg2")
    assert veto["kind"] == "veto" and "STALE_DATA" in _failed(veto) and veto["intent"]["id"] == vetoed[0]
    pid = veto["intent"]["linked_position_id"]
    assert pid in rig.engine.portfolio.positions and not rig.engine.portfolio.open_orders

    await rig.tick()  # on_tick: nothing hedges the coins -> leg 2 again, approved
    leg2s = _records(rig, purpose="leg2")
    assert [r["kind"] for r in leg2s] == ["veto", "order"]
    retry = leg2s[1]["intent"]
    assert retry["features"]["trigger"] == "retry" and "retry of the unhedged remainder" in retry["reason"]
    await rig.tick()
    assert pid not in rig.engine.portfolio.positions
    (outcome,) = _outcomes(rig, _records(rig, purpose="entry")[0]["intent"]["id"])
    assert outcome["exit_reason"] == "lockin"
    rig.close()
    assert check_logs(rig.logs).ok


async def test_lockin_leg1_never_chases_a_market_that_moved_away(tmp_path: Path) -> None:
    """The gap closes before leg 1 fills: leg 1 rests at its own price and is given up (no re-cross
    at the new, edge-less ask, which would buy into a losing cycle)."""
    rig = await make_rig(tmp_path)
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick()  # leg 1 placed at the dislocated ask
    (entry,) = _records(rig, strategy="S1", purpose="entry")
    limit = D(entry["order"]["request"]["price"])
    rig.feed.lag = D(0)  # the TMN book catches up before the next tick: the ask is ~0.9 % higher
    await rig.tick(10)  # 20 s > limit_reprice_after_s
    (outcome,) = _outcomes(rig, entry["intent"]["id"])
    assert outcome["done_reason"] == "EXPIRED_UNFILLED" and outcome["reprices"] == 0
    assert outcome["filled_qty"] == "0" and not rig.engine.portfolio.positions
    places = [r for r in iter_family(rig.logs, "orders") if r["action"] == "place" and r["symbol"] == "BTCTMN"]
    assert [D(p["data"]["request"]["price"]) for p in places] == [limit]  # placed once, never re-crossed
    rig.close()
    assert check_logs(rig.logs).ok


async def test_unfilled_leg2_is_not_chased_and_the_cycle_degrades(tmp_path: Path) -> None:
    """Review fix: leg 2 rests at the bid S1 checked; the BTCUSDT bid then drops 1 % (BTCTMN
    unchanged). The OrderManager no longer re-crosses leg 2 at the lower bid: it gives it up after
    limit_reprice_after_s, and S1's next tick re-checks the loss (now beyond leg2_max_loss_pct 0.3 %)
    and degrades the cycle to revert mode with a protective stop instead of selling into the drop."""
    rig = await make_rig(tmp_path)
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick(2)  # leg 1 placed, then filled -> leg 2 placed at the BTCUSDT bid
    (leg2,) = _records(rig, purpose="leg2")
    leg2_price = D(leg2["order"]["request"]["price"])
    pid = leg2["intent"]["linked_position_id"]
    rig.feed.btc = rig.feed.btc * D("0.99")  # BTCUSDT -1 %
    rig.feed.lag = (1 + LAG_TMN_CHEAP) / D("0.99") - 1  # BTCTMN stays where it was
    await rig.tick(9)  # 18 s > limit_reprice_after_s (15 s)
    (outcome,) = _outcomes(rig, leg2["intent"]["id"])
    assert outcome["done_reason"] == "EXPIRED_UNFILLED" and outcome["reprices"] == 0
    places = [r for r in iter_family(rig.logs, "orders") if r["action"] == "place" and r["symbol"] == "BTCUSDT"]
    assert [D(p["data"]["request"]["price"]) for p in places] == [leg2_price]  # never re-crossed lower
    stops = [r for r in _records(rig, purpose="stop") if r["kind"] == "order"]
    assert stops and stops[0]["intent"]["features"]["note"] == "lockin_degraded_to_revert"
    assert "lockin_degraded_to_revert" in stops[0]["intent"]["reason"]
    assert pid in rig.engine.portfolio.positions  # kept as a revert position under its stop
    rig.close()
    assert check_logs(rig.logs).ok


# ---------------------------------------------------------------- diagnostics, router drops


async def test_heartbeat_carries_strategy_diagnostics(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick(3)
    record = rig.engine.write_heartbeat(rig.clock.now())
    strat = record.extra["strategies"]
    assert strat["status"] == "active" and strat["plan"] is None
    btc = strat["S1"]["coins"]["BTC"]
    for key in ("edge_tmn_cheap_pct", "cost_tmn_cheap_pct", "need_tmn_cheap_pct", "edge_usdt_cheap_pct"):
        assert key in btc
    assert D(btc["cost_tmn_cheap_pct"]) == D("0.0043") and D(btc["need_tmn_cheap_pct"]) == D("0.0058")
    assert btc["why_no_trade"].startswith("no lock-in")
    assert btc["stats"]["evaluated"] == 3 and btc["stats"]["opportunity_ticks"] == 0
    s2 = strat["S2"]
    assert s2["status"] == "within_threshold" and D(s2["target"]) == D("0.5") and "current" in s2
    assert "S3" not in strat  # disabled by default: not constructed
    assert record.extra["candles"]["bars"] == {"BTCTMN": 0, "BTCUSDT": 0, "USDTTMN": 0}
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick()
    btc = rig.engine.write_heartbeat(rig.clock.now()).extra["strategies"]["S1"]["coins"]["BTC"]
    assert btc["stats"]["opportunity_ticks"] == 1 and D(btc["stats"]["best_excess_pct"]) > 0
    assert btc["decision"].startswith("ENTER lockin TMN_CHEAP")
    rig.close()
    assert check_logs(rig.logs).ok


async def test_router_drops_get_a_no_trade_record_once(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.engine.router.note_fill("BTCTMN", rig.clock.now() + 1)  # a fill on BTCTMN just happened
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick()
    (rec,) = _records(rig, strategy="S1")
    assert rec["kind"] == "no_trade" and "symbol cooldown 30s" in rec["notes"]
    assert rec["intent"]["purpose"] == "entry" and set(rec["features"]) == {"ts", "symbols", "cross", "dollar"}
    rig.close()
    assert check_logs(rig.logs).ok


async def test_strategies_are_not_asked_while_killed(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick()
    (tmp_path / "KILL").write_text("")
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick(2)
    assert rig.engine.flags.killed
    assert rig.engine.strategy_status.startswith("killed")
    assert not _records(rig, strategy="S1")
    rig.close()


# ---------------------------------------------------------------- persistence


async def test_restart_restores_strategy_and_feature_state(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, balances={"TMN": D("5000000")})
    for i in range(40):  # 40 deviation samples (z-score warm), an S2 attempt
        rig.feed.lag = D("0.0001") * (i % 3 - 1)
        await rig.tick()
    router_state = rig.engine.router.state()
    feature_state = rig.engine.features.state()
    assert router_state["strategies"]["S2"]["attempts"] >= 1
    assert len(feature_state["BTC"]) == 40
    rig.close()

    stored = Store.open_readonly(state_db_path(rig.engine.cfg, "shadow"))
    assert stored.kv_get(KV_STRATEGY) == json.loads(json.dumps(router_state))
    assert len(stored.kv_get(KV_FEATURES)["BTC"]) == 40
    stored.close()

    rig2 = await make_rig(tmp_path, start=rig.clock.now() + 4)
    assert rig2.engine.router.state() == json.loads(json.dumps(router_state))
    assert rig2.engine.features.state() == feature_state
    (restart,) = rig2.events("restart")
    assert restart["data"]["restore"]["strategy_state"] is True
    assert restart["data"]["restore"]["feature_state"] is True
    await rig2.tick()
    btc = rig2.engine.write_heartbeat(rig2.clock.now()).extra["strategies"]["S1"]["coins"]["BTC"]
    assert btc["dev_samples"] == 40 and btc["dev_zscore"] is not None  # warm at once after the restart
    rig2.close()


async def test_feature_state_is_saved_at_most_once_a_minute(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await rig.tick(5)  # 10 s after the startup save
    stored = Store.open_readonly(state_db_path(rig.engine.cfg, "shadow"))
    assert stored.kv_get(KV_FEATURES) == {}  # saved at startup (empty), not every tick
    stored.close()
    await rig.tick(26)
    stored = Store.open_readonly(state_db_path(rig.engine.cfg, "shadow"))
    assert len(stored.kv_get(KV_FEATURES)["BTC"]) >= 29
    stored.close()
    rig.close()


# ---------------------------------------------------------------- per-mode state DB


def test_state_db_path_per_mode(tmp_path: Path) -> None:
    default = load_config(env={})
    assert state_db_path(default, "shadow") == "data/state_shadow.sqlite"
    assert state_db_path(default, "live") == "data/state_live.sqlite"
    moved = load_config(overrides={"data.sqlite_path": str(tmp_path / "x" / "wta.sqlite")}, env={})
    assert state_db_path(moved, "live") == str(tmp_path / "x" / "wta.state_live.sqlite")
    synth = load_config(overrides={"data.sqlite_path": "data/shadow_synth.sqlite"}, env={})
    assert state_db_path(synth, "shadow") == "data/shadow_synth.state_shadow.sqlite"  # not the real shadow
    explicit = load_config(overrides={"data.state_sqlite_path": str(tmp_path / "s_{mode}.db")}, env={})
    assert state_db_path(explicit, "shadow") == str(tmp_path / "s_shadow.db")
    memory = load_config(overrides={"data.sqlite_path": ":memory:"}, env={})
    assert state_db_path(memory, "shadow") == ":memory:"


async def test_shadow_and_live_never_share_state_or_lock(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, **{"markets.watchlist": ["USDTTMN", "BTCTMN", "BTCUSDT"]})
    clock = SimClock(T0)
    engine = await build_engine(
        cfg, Settings(_env_file=None), "shadow", feed="synthetic", clock=clock,
        log_dir=tmp_path / "logs", kill_file=tmp_path / "KILL",
    )
    try:
        assert engine.store.path == str(tmp_path / "state_shadow_synthetic.sqlite")  # its own account
        assert [s.id for s in engine.router.strategies] == ["S1", "S2"]
        with pytest.raises(EngineLockError):  # a second shadow engine on the same paper account
            await build_engine(cfg, Settings(_env_file=None), "shadow", feed="synthetic", clock=clock,
                               log_dir=tmp_path / "logs2")
        live_lock = acquire_engine_lock(state_db_path(cfg, "live"))  # the live account is another lock
        release_engine_lock(live_lock)
        # the synthetic feed fills the candle cache and the state DB
        engine.synthetic.publish(T0)
        assert engine.candles.count("BTCUSDT") == 1
        engine.store.flush()
        assert engine.store.counts()["candles"] == 3
    finally:
        engine.shutdown("test end")
        await engine.aclose()
    assert (tmp_path / "state_shadow_synthetic.sqlite").exists()
    assert not (tmp_path / "state_shadow.sqlite").exists()  # the real-feed shadow account is untouched
    assert not (tmp_path / "state_live.sqlite").exists() and not (tmp_path / "market.sqlite").exists()


def _kill_db(path: Path) -> None:
    store = Store(path)
    store.kv_set(KV_FLAGS, {"killed": True, "kill_reason": "test", "flatten_phase": 0}, T0)
    store.close()


def test_reset_kill_picks_the_killed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wta import engine as engine_mod

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(engine_mod, "KILL_FILE", tmp_path / "KILL")
    common = ["--log-dir", str(tmp_path / "logs"), "--set", f"data.sqlite_path={tmp_path / 'wta.sqlite'}"]
    assert cli.main(["reset-kill", "--reason", "x", *common]) == 1  # no state DB at all
    _kill_db(tmp_path / "wta.state_shadow.sqlite")
    _kill_db(tmp_path / "wta.state_live.sqlite")
    assert cli.main(["reset-kill", "--reason", "x", *common]) == 2  # both killed: --mode needed
    assert "--mode" in capsys.readouterr().err
    assert cli.main(["reset-kill", "--reason", "live checked", "--mode", "live", *common]) == 0

    def killed(mode: str) -> bool:
        store = Store.open_readonly(tmp_path / f"wta.state_{mode}.sqlite")
        try:
            return bool(store.kv_get(KV_FLAGS)["killed"])
        finally:
            store.close()

    assert killed("shadow") and not killed("live")
    assert cli.main(["reset-kill", "--reason", "shadow too", *common]) == 0  # the only killed one
    assert not killed("shadow")
    assert cli.main(["reset-kill", "--reason", "again", *common]) == 0
    assert "nothing to reset" in capsys.readouterr().out


# ---------------------------------------------------------------- candles


def _candle(symbol: str, ts: int, close: str = "100", res: str = "1") -> Candle:
    c = D(close)
    return Candle(symbol=symbol, resolution=res, ts=ts, o=c, h=c, l=c, c=c, v=D(1))


def test_candle_cache_replaces_prunes_and_filters() -> None:
    cache = CandleCache(["BTCUSDT"], keep_s=600)
    base = int(T0)
    assert cache.add([_candle("BTCUSDT", base + 60 * i) for i in range(5)]) == 5
    assert cache.add([_candle("BTCUSDT", base + 240, "101")]) == 1  # the forming minute changed
    assert cache.add([_candle("BTCUSDT", base + 240, "101")]) == 0  # same bar again: no change
    assert cache.add([_candle("ETHUSDT", base), _candle("BTCUSDT", base, res="15")]) == 0  # filtered
    got = cache.get("BTCUSDT")
    assert [c.ts for c in got] == [base + 60 * i for i in range(5)] and got[-1].c == D("101")
    got.clear()  # callers get copies
    assert cache.count("BTCUSDT") == 5
    cache.add([_candle("BTCUSDT", base + 60 * i) for i in range(5, 20)])
    assert cache.count("BTCUSDT") == 11 and cache.get("BTCUSDT")[0].ts == base + 60 * 9  # last 600 s
    assert cache.snapshot(["ETHUSDT", "BTCUSDT"])["ETHUSDT"] == []
    assert cache.latest_ts("BTCUSDT") == base + 60 * 19


def test_candle_cache_fills_missing_minutes_and_trims_on_every_update() -> None:
    """Review fix: a minute without a candle (no trades, or a feed gap) is a flat bar at the previous
    close with volume 0 in what the features read, and old bars are trimmed on every update."""
    cache = CandleCache(["BTCUSDT"], keep_s=3600)
    base = int(T0)
    cache.add([_candle("BTCUSDT", base, "100"), _candle("BTCUSDT", base + 240, "104")])
    got = cache.get("BTCUSDT")
    assert [c.ts for c in got] == [base + 60 * i for i in range(5)]
    assert [(c.o, c.h, c.l, c.c, c.v) for c in got[1:4]] == [(D(100), D(100), D(100), D(100), D(0))] * 3
    assert cache.count("BTCUSDT") == 2  # only received bars are counted
    cache.add([_candle("BTCUSDT", base + 120, "102")])  # a late real candle replaces the filler
    assert [c.c for c in cache.get("BTCUSDT")] == [D(100), D(100), D(102), D(102), D(104)]
    # a sparse symbol: a bar 40 h later drops the old ones although the cache is far from full
    cache.add([_candle("BTCUSDT", base + 40 * 3600, "150")])
    assert [c.ts for c in cache.get("BTCUSDT")] == [base + 40 * 3600] and cache.count("BTCUSDT") == 1


class CandleRest:
    """``get_candles`` answering one bar per minute in the window; ``reject`` -> HTTP 400."""

    def __init__(self, reject: set[str] | None = None) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.reject = reject or set()

    async def get_candles(self, symbol: str, resolution: str, from_ts: float, to_ts: float) -> list[Candle]:
        self.calls.append((symbol, int(from_ts), int(to_ts)))
        if symbol in self.reject:
            raise WallexAPIError(400, "bad_symbol", f"{symbol} unknown")
        first = (int(from_ts) + 59) // 60 * 60
        return [_candle(symbol, t) for t in range(first, int(to_ts) - 59, 3600)]  # sparse: hourly


async def test_candle_refresher_backfills_26h_in_day_windows_then_refreshes() -> None:
    clock = SimClock(T0)
    cache = CandleCache(["BTCUSDT", "ETHUSDT"])
    rest = CandleRest(reject={"ETHUSDT"})
    sunk: list[int] = []
    refresher = CandleRefresher(
        rest, cache, ["ETHUSDT", "BTCUSDT"], sink=lambda cs: sunk.append(cache.add(cs)), clock=clock
    )
    got = await refresher.poll_once()
    btc_calls = [c for c in rest.calls if c[0] == "BTCUSDT"]
    assert btc_calls == [
        ("BTCUSDT", int(T0) - 26 * 3600, int(T0) - 2 * 3600),
        ("BTCUSDT", int(T0) - 2 * 3600, int(T0)),
    ]
    assert all(b - a <= 86400 for _, a, b in rest.calls)
    assert [c for c in rest.calls if c[0] == "ETHUSDT"] == [("ETHUSDT", int(T0) - 26 * 3600, int(T0) - 2 * 3600)]
    assert got == 26 and cache.count("BTCUSDT") == 26 and refresher.rejected == 1
    rest.calls.clear()
    clock.advance(60)
    last = cache.latest_ts("BTCUSDT")
    await refresher.poll_once()
    assert ("BTCUSDT", last, int(T0) + 60) in rest.calls  # incremental: from the newest cached bar
    assert refresher.stats()["rounds_done"] == 2


async def test_live_feed_engine_runs_the_candle_refresher_into_cache_and_state_db(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    markets = load_markets_snapshot()
    clock = SimClock(T0)
    bus = MarketDataBus()
    CrossFeed(bus, markets).publish(T0)
    store = Store(state_db_path(cfg, "shadow"))
    tick_clock = TickClock(clock)
    rest = CandleRest()
    engine = Engine(
        cfg, Settings(_env_file=None), "shadow", tick_clock, bus, store,
        exchange=PaperExchange(markets, bus.snapshot, tick_clock, initial_balances=dict(QUIET_BALANCES)),
        rest=rest, markets=markets, feed="live", kill_file=tmp_path / "KILL", log_dir=tmp_path / "logs",
        strategies=default_strategies(cfg),
    )
    assert engine.candle_refresher is not None
    await engine.startup()
    await engine.candle_refresher.poll_once()
    assert engine.candles.count("BTCUSDT") == 26
    store.flush()
    assert store.counts()["candles"] == 3 * 26
    await engine.tick(T0)
    # hourly bars over 25 h, filled minute by minute (no trades = flat bars) -> 101 completed 15m bars
    assert engine.last_features.symbols["BTCUSDT"]["bars_15m"] == 25 * 4 + 1
    engine.shutdown("test end")
    store.close()

    # a restart loads the candles back from the state DB (no SQLite read in the fast loop)
    store = Store(state_db_path(cfg, "shadow"))
    clock2 = TickClock(SimClock(T0 + 10))
    engine2 = Engine(
        cfg, Settings(_env_file=None), "shadow", clock2, bus, store,
        exchange=PaperExchange(markets, bus.snapshot, clock2, initial_balances=dict(QUIET_BALANCES)),
        markets=markets, feed="test", kill_file=tmp_path / "KILL", log_dir=tmp_path / "logs",
        strategies=default_strategies(cfg),
    )
    await engine2.startup()
    # the bars within 26 h of the newest stored bar: exactly what the cache held (its own trim rule),
    # so a replay of the engine_candles log rebuilds the same cache
    assert engine2.candles.count("BTCUSDT") == 26
    engine2.shutdown("test end")
    store.close()


# ---------------------------------------------------------------- CLI overlays


def test_run_overlay_merges_yaml_files_in_order_and_set_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Config, dict[str, Any]]] = []

    async def fake_run_engine(cfg: Config, settings: Any, mode: str, **kwargs: Any) -> int:
        calls.append((cfg, kwargs))
        return 0

    monkeypatch.setattr(cli, "run_engine", fake_run_engine)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))
    second = tmp_path / "second.yaml"
    second.write_text("strategies:\n  s1_crossbook:\n    size_pct_equity: 0.05\n    cooldown_s: 90\n")
    half = str(ROOT / "configs" / "live_halfsize.yaml")
    argv = ["run", "--overlay", half, "--overlay", str(second), "--set", "strategies.s1_crossbook.cooldown_s=120"]
    assert cli.main(argv) == 0
    [(cfg, kwargs)] = calls
    assert cfg.risk.max_order_pct_equity == D("0.125")  # from live_halfsize.yaml
    assert cfg.strategies.s1_crossbook.size_pct_equity == D("0.05")  # the later overlay wins
    assert cfg.strategies.s1_crossbook.cooldown_s == 120  # --set wins over every overlay
    assert cfg.strategies.s1_crossbook.margin_min_pct == D("0.0015")  # untouched keys keep their value
    assert kwargs["overlays"] == [half, str(second)]
    assert cli.main(["run"]) == 0 and "overlays" not in calls[-1][1]  # a plain run is unchanged
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n")
    assert cli.main(["run", "--overlay", str(bad)]) == 2
    assert cli.main(["run", "--overlay", str(tmp_path / "missing.yaml")]) == 2


def test_config_hash_changes_with_the_halfsize_overlay(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config", "hash"]) == 0
    base = capsys.readouterr().out.strip()
    assert cli.main(["config", "hash", "--overlay", str(ROOT / "configs" / "live_halfsize.yaml")]) == 0
    half = capsys.readouterr().out.strip()
    assert base != half and len(half) == 12


async def test_start_event_names_overlays_strategies_and_state_db(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    store = Store(state_db_path(cfg, "shadow"))
    markets = load_markets_snapshot()
    bus = MarketDataBus()
    clock = TickClock(SimClock(T0))
    engine = Engine(
        cfg, Settings(_env_file=None), "shadow", clock, bus, store,
        exchange=PaperExchange(markets, bus.snapshot, clock), markets=markets, feed="test",
        kill_file=tmp_path / "KILL", log_dir=tmp_path / "logs", strategies=default_strategies(cfg),
        overlays=["configs/live_halfsize.yaml"],
    )
    await engine.startup()
    engine.shutdown("test end")
    store.close()
    start = next(e for e in iter_family(tmp_path / "logs", "events") if e["kind"] == "start")["data"]
    assert start["overlays"] == ["configs/live_halfsize.yaml"] and start["config_hash"] == cfg.config_hash
    assert start["strategies"] == ["S1", "S2"] and start["rules_only"] is True
    assert start["state_db"] == str(tmp_path / "state_shadow.sqlite")


# ---------------------------------------------------------------- shadow report


def _load_report_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shadow_report", ROOT / "scripts" / "shadow_report.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_shadow_report_summarises_a_run(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, balances={"TMN": D("6000000"), "USDT": D("15")})
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick(3)  # one S1 lock-in cycle
    rig.feed.lag = D(0)
    rig.engine.write_heartbeat(rig.clock.now())
    await rig.tick(3)
    rig.close()
    script = _load_report_script()
    out = tmp_path / "report.md"
    db = state_db_path(rig.engine.cfg, "shadow")
    assert script.main(["--log-dir", str(rig.logs), "--state-db", db, "--out", str(out)]) == 0
    text = out.read_text()
    for heading in ("## Window", "## Feed", "## Heartbeats", "## Equity", "## Decisions", "## Vetoes",
                    "## Fills and closed positions", "## Fees", "## S1 opportunities", "## S2 rebalances",
                    "## Errors", "## Logs check"):
        assert heading in text, heading
    assert "SYNTHETIC" not in text  # feed "test" is not the synthetic feed
    assert "| S1 | BTCTMN | lockin |" in text  # the closed cycle with its PnL
    assert "logs check: OK" in text

    # a synthetic run gets the loud banner
    (rig.logs / "events.jsonl").write_text(
        (rig.logs / "events.jsonl").read_text().replace('"feed":"test"', '"feed":"synthetic","synthetic":true')
    )
    assert script.main(["--log-dir", str(rig.logs), "--out", str(out)]) == 0
    assert "SYNTHETIC — not evidence" in out.read_text()
