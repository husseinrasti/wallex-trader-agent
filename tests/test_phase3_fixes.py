"""Phase-3 review fixes at the engine / CLI level (each test names the finding it pins down).

Uses the rig of ``tests/test_strategies_engine.py`` (real FeatureEngine, router, S1/S2, RiskManager,
OrderManager, PaperExchange, Store and JSONL logs on a ``SimClock``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from wta import cli
from wta.config import Settings, load_config
from wta.data.collector import CandleCache
from wta.data.store import Store
from wta.engine import KV_FEATURES, KV_STRATEGY, Engine, TickClock, default_strategies, state_db_path
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot
from wta.exchange.paper import PaperExchange
from wta.execution.reconcile import KV_FLAGS, KV_PORTFOLIO
from wta.models import Candle, MarketPlan
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock

from .conftest import T0
from .test_strategies_engine import LAG_TMN_CHEAP, CrossFeed, _records, make_cfg, make_rig

# ---------------------------------------------------------------- a failed startup never overwrites state


async def test_failed_restore_persists_nothing_and_is_retried(tmp_path: Path) -> None:
    """Review fix: a transient error while restoring (database locked) used to let shutdown() and
    the next ticks force-write default flags (killed=False), peak None and empty strategy/feature
    state over the real rows. Now nothing is persisted until the restore succeeded, and the next
    tick runs the whole startup again."""
    cfg = make_cfg(tmp_path)
    rig = await make_rig(tmp_path, cfg=cfg)
    await rig.tick(40)  # 40 deviation samples, S2 state
    await rig.engine._kill("test kill", rig.clock.now(), source="kill_file")
    await rig.tick()
    end = rig.clock.now()
    rig.close()
    path = state_db_path(cfg, "shadow")
    with Store.open_readonly(path) as s:
        before = {k: s.kv_get(k) for k in (KV_FLAGS, KV_PORTFOLIO, KV_STRATEGY, KV_FEATURES)}
    assert before[KV_FLAGS]["killed"] is True and len(before[KV_FEATURES]["BTC"]) >= 40

    store = Store(path)
    real_get = store.kv_get
    failures = {"n": 0}

    def flaky(key: str, default: Any = None) -> Any:
        if key == KV_PORTFOLIO and failures["n"] < 2:  # _has_prior_state (caught), then the restore
            failures["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real_get(key, default)

    store.kv_get = flaky  # type: ignore[method-assign]

    def new_engine(t: float) -> Engine:
        markets = load_markets_snapshot()
        clock = TickClock(SimClock(t))
        bus = MarketDataBus()
        CrossFeed(bus, markets).publish(t)
        return Engine(
            cfg, Settings(_env_file=None), "shadow", clock, bus, store,
            exchange=PaperExchange(markets, bus.snapshot, clock, state=real_get("paper_state")),
            markets=markets, markets_source="snapshot", feed="test", kill_file=tmp_path / "KILL",
            log_dir=tmp_path / "logs2", strategies=default_strategies(cfg),
        )

    # 1) the restore fails and the process stops: shutdown() writes nothing
    engine = new_engine(end + 10)
    try:
        await engine.startup()
        raise AssertionError("the restore error must propagate")
    except sqlite3.OperationalError:
        pass
    engine.shutdown("restore failed")  # what run()'s finally does
    store.flush()
    after = {k: real_get(k) for k in (KV_FLAGS, KV_PORTFOLIO, KV_STRATEGY, KV_FEATURES)}
    assert after == before  # nothing was overwritten

    # 2) the restore fails at the first tick: the tick crashes (nothing trades, nothing is written)
    # and the next tick runs the whole startup again
    failures["n"] = 0
    engine = new_engine(end + 20)
    assert await engine.safe_tick(end + 20) is False
    store.flush()
    assert {k: real_get(k) for k in (KV_FLAGS, KV_PORTFOLIO, KV_STRATEGY, KV_FEATURES)} == before
    assert await engine.safe_tick(end + 22) is True
    assert engine.flags.killed is True and engine.flags.kill_reason == "test kill"
    store.flush()
    assert real_get(KV_FLAGS)["killed"] is True
    assert len(real_get(KV_FEATURES)["BTC"]) >= 40
    engine.shutdown("test end")
    store.close()


# ---------------------------------------------------------------- state DB identity: mode and feed


def test_state_db_path_never_shared_by_modes_or_feeds(tmp_path: Path) -> None:
    default = load_config(env={})
    assert state_db_path(default, "shadow") == "data/state_shadow.sqlite"
    assert state_db_path(default, "live") == "data/state_live.sqlite"
    # review fix: synthetic data never lands in the real-feed shadow account (paper state, candles,
    # z-score history)
    assert state_db_path(default, "shadow", "synthetic") == "data/state_shadow_synthetic.sqlite"
    # review fix: a path without {mode} gets the mode inserted (shadow and live never share kv state)
    plain = load_config(overrides={"data.state_sqlite_path": "/x/state3.sqlite"}, env={})
    assert state_db_path(plain, "shadow") == "/x/state3.shadow.sqlite"
    assert state_db_path(plain, "live") == "/x/state3.live.sqlite"
    assert state_db_path(plain, "shadow", "synthetic") == "/x/state3.shadow_synthetic.sqlite"
    templ = load_config(overrides={"data.state_sqlite_path": str(tmp_path / "s_{mode}.db")}, env={})
    assert state_db_path(templ, "shadow", "synthetic") == str(tmp_path / "s_shadow_synthetic.db")
    synth = load_config(overrides={"data.sqlite_path": "data/shadow_synth.sqlite"}, env={})
    assert state_db_path(synth, "shadow", "synthetic") == "data/shadow_synth.state_shadow_synthetic.sqlite"
    memory = load_config(overrides={"data.state_sqlite_path": ":memory:"}, env={})
    assert state_db_path(memory, "live") == ":memory:"
    paths = {state_db_path(c, m, f) for c in (default, plain) for m, f in
             (("shadow", "live"), ("live", "live"), ("shadow", "synthetic"))}
    assert len(paths) == 6


def test_reset_kill_finds_a_killed_synthetic_account(tmp_path: Path, capsys: Any) -> None:
    template = str(tmp_path / "st_{mode}.sqlite")
    cfg = load_config(overrides={"data.state_sqlite_path": template}, env={})
    path = state_db_path(cfg, "shadow", "synthetic")
    with Store(path) as s:
        s.kv_set(KV_FLAGS, {"killed": True, "kill_reason": "synthetic storm"}, 1.0)
    common = ["--set", f"data.state_sqlite_path={template}", "--log-dir", str(tmp_path / "logs")]
    assert cli.main(["reset-kill", "--reason", "synthetic exercise over", *common]) == 0
    assert "kill-switch reset" in capsys.readouterr().out
    with Store.open_readonly(path) as s:
        assert s.kv_get("kill_reset")["reason"] == "synthetic exercise over"
    # --feed narrows the search the same way --mode does
    assert cli.main(["reset-kill", "--reason", "x", "--feed", "live", *common]) == 1  # no real-feed DB


# ---------------------------------------------------------------- the decision log's plan is the plan in force


async def test_stale_plan_is_not_logged_as_the_envelope_in_force(tmp_path: Path) -> None:
    """Review fix: strategies ignore a plan older than ai.plan_stale_s (rules-only); the decision
    records and the health line now say the same instead of showing the stale plan."""
    cfg = make_cfg(tmp_path, **{"ai.enabled": True})
    rig = await make_rig(tmp_path, cfg=cfg)
    now = rig.clock.now()
    rig.engine.plan = MarketPlan(ts=now - cfg.ai.plan_stale_s - 1, enabled_strategies=["S1", "S2"],
                                 size_multiplier=D("0.5"))
    assert rig.engine.current_plan(now) is None
    rig.feed.lag = LAG_TMN_CHEAP
    await rig.tick()
    (entry,) = _records(rig, strategy="S1", purpose="entry")
    assert entry["plan_snapshot"] is None  # the strategies ran rules-only, and the record says so
    assert entry["intent"]["features"]["size_multiplier"] == "1"
    health = rig.engine.strategy_health(rig.clock.now())
    assert health is not None and health["plan"] is None and health["plan_stale"]["ts"] == rig.engine.plan.ts
    # a fresh plan is the one in force and is logged
    rig.engine.plan = MarketPlan(ts=rig.clock.now(), enabled_strategies=["S1", "S2"], size_multiplier=D("0.5"))
    assert rig.engine.current_plan(rig.clock.now()) is rig.engine.plan
    assert rig.engine.strategy_health(rig.clock.now())["plan"]["size_multiplier"] == "0.5"  # type: ignore[index]
    rig.close()


# ---------------------------------------------------------------- `wta run` log dir per mode


def test_run_log_dir_defaults_per_mode(monkeypatch: Any) -> None:
    """Review fix: a shadow engine next to the live one must not write into the live logs."""
    seen: dict[str, Any] = {}

    async def fake_run_engine(cfg: Any, settings: Any, mode: str, **kw: Any) -> int:
        seen[mode] = kw["log_dir"]
        return 0

    monkeypatch.setattr(cli, "run_engine", fake_run_engine)
    assert cli.main(["run", "--mode", "shadow", "--seconds", "1"]) == 0
    assert seen["shadow"] == "logs/shadow"
    assert cli.main(["run", "--mode", "shadow", "--seconds", "1", "--log-dir", "elsewhere"]) == 0
    assert seen["shadow"] == "elsewhere"
    monkeypatch.setattr(cli, "live_guard_reason", lambda *a: None)
    monkeypatch.setattr(cli, "deadline_guard_reason", lambda *a: None)
    assert cli.main(["run", "--mode", "live", "--i-understand-live", "--seconds", "1"]) == 0
    assert seen["live"] == "logs"


# ---------------------------------------------------------------- candle replay inputs


def _bar(symbol: str, ts: float, close: str, v: str = "1") -> Candle:
    c = D(close)
    return Candle(symbol=symbol, resolution="1", ts=int(ts), o=c, h=c, l=c, c=c, v=D(v))


async def test_every_tick_records_the_candles_it_used(tmp_path: Path) -> None:
    """Review fix: ``candles`` keeps only the latest version of a bar (a re-fetched, still-forming
    bar overwrites the one a tick used), and a refresh can land during a tick's awaits. Now every
    arrival is logged with a sequence number and each tick row names the ``candle_seq`` of its
    snapshot (taken with the books, at the start of the tick): replaying the log up to it gives
    exactly the candles the tick's features were computed from."""
    rig = await make_rig(tmp_path)
    eng = rig.engine
    sym = "USDTTMN"
    minute = int(T0 // 60 * 60)
    eng.ingest_candles([_bar(sym, minute - 120 + 60 * i, "266700") for i in range(2)])  # two closed bars
    used: list[dict[str, list[Candle]]] = []

    async def tick() -> None:
        await rig.tick()
        assert eng._tick_candles is not None
        used.append(eng._tick_candles)

    eng.ingest_candles([_bar(sym, minute, "266700", v="1")])  # the forming minute
    await tick()
    eng.ingest_candles([_bar(sym, minute, "266900", v="5")])  # re-fetched: it moved and traded more
    await tick()

    # a refresh landing while the tick awaits the order poll belongs to the next tick
    real_poll = eng.order_manager.poll

    async def poll_with_refresh(books: Any, now: float) -> Any:
        eng.ingest_candles([_bar(sym, minute + 60, "267000")])
        return await real_poll(books, now)

    eng.order_manager.poll = poll_with_refresh  # type: ignore[method-assign]
    await tick()
    eng.order_manager.poll = real_poll  # type: ignore[method-assign]
    assert [c.ts for c in used[2][sym]][-1] == minute  # not in this tick's snapshot ...
    await tick()
    assert [c.ts for c in used[3][sym]][-1] == minute + 60  # ... but in the next one
    rig.store.flush()

    ticks = list(rig.store.iter_ticks("shadow"))[-4:]
    # 3 bars before the first tick, the re-fetch before the second; the mid-tick refresh (seq 5)
    # is after the third tick's snapshot (still 4) and in the fourth
    assert [t["candle_seq"] for t in ticks] == [3, 4, 4, 5]
    for row, snapshot in zip(ticks, used, strict=True):
        cache = CandleCache(eng.watchlist)
        cache.add(c for _, c in rig.store.iter_engine_candles("shadow", to_seq=row["candle_seq"]))
        assert cache.snapshot(eng.watchlist) == snapshot
    # the latest-version table alone could not replay the first tick: its forming bar is gone
    stored = rig.store.load_candles(sym, "1")
    assert [c.c for c in stored if c.ts == minute] == [D("266900")]
    assert used[0][sym][-1].c == D("266700")
    rig.close()


async def test_candle_seq_continues_after_a_restart(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rig = await make_rig(tmp_path, cfg=cfg)
    rig.engine.ingest_candles([_bar("BTCUSDT", T0 - 60 * i, "82877") for i in range(3)])
    await rig.tick()
    end = rig.clock.now()
    rig.close()
    rig2 = await make_rig(tmp_path, cfg=cfg, start=end + 5)
    assert rig2.engine._candle_seq == 3 and rig2.engine.candles.count("BTCUSDT") == 3
    rig2.engine.ingest_candles([_bar("BTCUSDT", T0 + 60, "82900")])
    rig2.store.flush()
    assert [seq for seq, _ in rig2.store.iter_engine_candles("shadow")] == [1, 2, 3, 4]
    rig2.close()
