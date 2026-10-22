"""PRD-04 §8 engine integration: the slow loop (Regime Analyst), the hourly reviewer, the runtime
overlay, Telegram and the ablation switch — through the real engine path on a ``SimClock``.

The models are the deterministic offline rules provider (no key) or a scripted fake provider behind
the REAL ``LLMClient`` + ``LLMCache``; "invalid key" is the real ``OpenRouterProvider`` on an
``httpx.MockTransport`` answering 401; Telegram is a ``MockTransport`` too. Nothing touches the
network or the wall clock (the one ``engine.run`` test sleeps for real, but reads time from the
SimClock).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, OpenRouterProvider, Route, ToolCall
from wta.ai.offline_provider import OFFLINE_LABEL, OFFLINE_MODEL, OFFLINE_STATUS, REVIEWER_MARKER
from wta.ai.plan_schema import PLAN_EXAMPLE
from wta.ai.replay import replay_recorded_plans
from wta.ai.tools import is_feature_view
from wta.config import Config, Settings, canonical_json
from wta.data.store import Store
from wta.engine import Engine, TickClock, build_engine, default_strategies, state_db_path
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot
from wta.exchange.paper import PaperExchange
from wta.logging_.checker import check_logs
from wta.logging_.jsonl import iter_family
from wta.utils import telegram as tg
from wta.utils.timeutil import SimClock

from .conftest import T0
from .test_strategies_engine import QUIET_BALANCES, CrossFeed, Rig, make_cfg

FAKE_PROVIDER = "openrouter"  # the configured analyst provider; tests put a fake under this name
INVALID_KEY = "".join(["sk-or-", "invalid", "-test-", "k" * 12])  # built at runtime: no key-like literal
BOT_TOKEN = "".join(["12345", ":", "tg", "-test-", "t" * 10])
CHAT_ID = "4242"


def no_keys(**values: Any) -> Settings:
    """Settings without any LLM key or Telegram config (whatever the developer's environment holds)."""
    base: dict[str, Any] = {"openrouter_api_key": "", "gemini_api_key": "", "telegram_bot_token": "",
                            "telegram_chat_id": "", "wallex_api_key": ""}
    return Settings(_env_file=None, **{**base, **values})


def ai_cfg(tmp_path: Path, **over: Any) -> Config:
    values: dict[str, Any] = {"llm.analyst.model": "fake/analyst-1", "llm.reviewer.model": "fake/reviewer-1",
                              "engine.heartbeat_interval_s": 60}
    values.update(over)
    return make_cfg(tmp_path, **values)


class FakeProvider:
    """A scripted model behind the real LLMClient: ``script(messages, tools, n)`` returns a text, a
    list of ToolCalls, an awaitable of either, or raises. Calls are recorded."""

    name = FAKE_PROVIDER

    def __init__(self, script: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None, int], Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "model": model})
        out = self.script(messages, tools, len(self.calls))
        if asyncio.iscoroutine(out):
            out = await out
        if isinstance(out, list):
            return LLMResponse(text="", tool_calls=out, model=model, provider=self.name, latency_ms=5,
                               usage={"prompt_tokens": 900, "completion_tokens": 40})
        return LLMResponse(text=str(out), model=model, provider=self.name, latency_ms=9,
                           usage={"prompt_tokens": 1200, "completion_tokens": 300})


def plan_text(**over: Any) -> str:
    body = copy.deepcopy(PLAN_EXAMPLE)
    body.update(over)
    return json.dumps(body)


def is_reviewer(messages: list[dict[str, Any]]) -> bool:
    return REVIEWER_MARKER in str(messages[0].get("content"))


def analyst_script(plan: str) -> Callable[..., Any]:
    """Round 1: ask get_features for the regime symbols; then answer ``plan``."""

    def script(messages: list[dict[str, Any]], tools: Any, n: int) -> Any:
        if tools and not any(m.get("role") == "tool" for m in messages):
            return [ToolCall("call_f", "get_features", {"symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "USDTTMN"]})]
        return plan

    return script


async def ai_rig(
    tmp_path: Path,
    *,
    cfg: Config | None = None,
    providers: dict[str, Any] | None = None,
    settings: Settings | None = None,
    start: float = T0,
    runtime_overlay: dict[str, Any] | None = None,
    enable: bool = True,
) -> Rig:
    """The strategies rig of ``test_strategies_engine`` with the AI layer attached before startup."""
    cfg = cfg or ai_cfg(tmp_path)
    markets = load_markets_snapshot()
    clock = SimClock(start)
    bus = MarketDataBus()
    feed = CrossFeed(bus, markets)
    feed.publish(start)
    store = Store(state_db_path(cfg, "shadow"), flush_interval_s=cfg.data.store_flush_interval_s)
    state = store.kv_get("paper_state")
    tick_clock = TickClock(clock)
    paper = PaperExchange(markets, bus.snapshot, tick_clock,
                          initial_balances=None if state is not None else QUIET_BALANCES, state=state)
    engine = Engine(cfg, settings or no_keys(), "shadow", tick_clock, bus, store, exchange=paper, markets=markets,
                    markets_source="snapshot", feed="test", kill_file=tmp_path / "KILL", log_dir=tmp_path / "logs",
                    strategies=default_strategies(cfg))
    if enable:
        engine.enable_ai(providers=providers, mcp=None, runtime_dir=tmp_path / "runtime",
                         runtime_overlay=runtime_overlay)
    await engine.startup()
    return Rig(engine, clock, feed, store, paper, tmp_path)


def plan_records(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.raw_decisions() if r["kind"] == "plan"]


def s2_diag(rig: Rig) -> dict[str, Any]:
    s2 = rig.engine.router.by_id("S2") if rig.engine.router is not None else None
    assert s2 is not None
    return s2.diagnostics()


# ---------------------------------------------------------------- plan on schedule (offline rules provider)


async def test_offline_plan_on_schedule_persisted_and_logged(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path, cfg=ai_cfg(tmp_path, **{"engine.slow_loop_interval_s": 300}))
    eng = rig.engine
    assert eng.ai_source == "offline-rules" and eng.analyst is not None
    await rig.tick()
    now = rig.clock.now()
    plan = await eng.slow_step(now)
    assert plan is not None and plan.model == OFFLINE_MODEL and plan.ts == now
    assert eng.plan is plan and eng.current_plan(now) is plan
    assert plan.notes.startswith(OFFLINE_LABEL)
    assert plan.tools_source == "local" and "get_features" in plan.tools_used

    # decisions.jsonl: one kind="plan" record, stamped with the cycle time
    (rec,) = plan_records(rig)
    assert rec["decision_id"] == f"d_{int(now * 1000)}_AI_PLAN_1"
    assert rec["strategy"] == "AI" and rec["symbol"] == "PLAN" and rec["notes"] == plan.notes
    assert rec["ts"].startswith("2026-10-22T10:00:02")
    feats = rec["features"]
    for key in ("prompt_hash", "model", "cached", "tools_used", "tools_source", "latency_ms", "clamped", "reason"):
        assert key in feats, key
    assert feats["reason"] == "startup" and feats["model"] == OFFLINE_MODEL and feats["cached"] is False
    assert feats["ai_source"] == "offline-rules" and feats["ai_label"] == OFFLINE_STATUS
    assert rec["plan_snapshot"] == json.loads(canonical_json(plan.model_dump(mode="json")))
    assert rec["config_hash"] == eng.config_hash

    # the state DB plans table
    eng.store.flush()
    rows = eng.store.load_plans()
    assert [r["prompt_hash"] for r in rows] == [plan.prompt_hash] and rows[0]["ts"] == now

    # the cadence: nothing before slow_loop_interval_s, then a scheduled cycle
    await rig.tick()
    assert await eng.slow_step(rig.clock.now()) is None
    await rig.tick(dt=300)
    plan2 = await eng.slow_step(rig.clock.now())
    assert plan2 is not None and eng.analyst.last_reason == "scheduled"
    assert [r["features"]["reason"] for r in plan_records(rig)] == ["startup", "scheduled"]

    # strategy decisions carry the plan in force; the heartbeat shows the AI source honestly
    hb = eng.write_heartbeat(rig.clock.now())
    assert hb.llm_ok is None  # the offline provider is not an LLM
    assert hb.extra["ai_source"] == "offline-rules"
    ai = hb.extra["ai"]
    assert ai["plan_model"] == OFFLINE_MODEL and ai["tools_source"] == "local" and ai["label"] == OFFLINE_STATUS
    assert ai["plan_age_s"] == 0 and ai["llm_consecutive_failures"] == 0
    rig.close()
    assert check_logs(rig.logs).ok


async def test_kill_event_triggers_a_post_mortem_cycle(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path)
    eng = rig.engine
    await rig.tick()
    assert await eng.slow_step(rig.clock.now()) is not None
    await rig.tick(dt=30)
    assert await eng.slow_step(rig.clock.now()) is None  # < 60 s since the last cycle
    (tmp_path / "KILL").write_text("")
    await rig.tick(dt=40)  # 70 s after the first cycle
    assert eng.flags.killed
    assert await eng.slow_step(rig.clock.now()) is not None
    assert eng.analyst.last_reason == "event:kill"
    rig.close()


# ---------------------------------------------------------------- LLM path (fake provider, real client)


async def test_llm_plan_reaches_the_strategies(tmp_path: Path) -> None:
    regimes = copy.deepcopy(PLAN_EXAMPLE["regimes"])
    regimes["USDTTMN"] = {"regime": "trend_up", "confidence": 0.8, "evidence": "ret_1h +0.4%, ema gap +0.3%"}
    fake = FakeProvider(analyst_script(plan_text(regimes=regimes, usdt_target_share=0.65, size_multiplier=0.5,
                                                 notes="Dollar trending up: USDT target 65 %.")))
    rig = await ai_rig(tmp_path, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    assert eng.ai_source == "llm"
    await rig.tick()
    before = s2_diag(rig)
    assert before["target_origin"].startswith("rules table") and before["plan_fresh"] is False
    plan = await eng.slow_step(rig.clock.now())
    assert plan is not None, eng.ai_last_error
    assert plan.model == "fake/analyst-1" and plan.tools_used == ["get_features"] and plan.tools_source == "local"
    assert plan.usdt_target_share == Decimal("0.65") and plan.latency_ms == 5 + 9

    # the get_features result the model saw: the curated view, within the 4 kB tool cap
    tool_msg = next(m for m in fake.calls[1]["messages"] if m.get("role") == "tool")
    view = json.loads(tool_msg["content"])
    assert is_feature_view(view) and len(tool_msg["content"].encode()) <= eng.cfg.ai.tool_result_max_bytes
    assert "USDTTMN" in view["symbols"] and "BTCUSDT" in view["symbols"]
    # the first message carried the whole watchlist with the key legend
    first = json.loads(fake.calls[0]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\n", 1)[0])
    assert is_feature_view(first["features"]) and "keys" in first["features"]

    # S2 uses the plan's target on the next tick (40 % USDT now, 65 % target: a rebalance BUY)
    await rig.tick()
    after = s2_diag(rig)
    assert after["target"] == "0.65" and after["target_origin"] == "plan" and after["plan_fresh"] is True
    s2 = [r for r in rig.raw_decisions() if r["strategy"] == "S2" and r["kind"] in ("intent", "order", "veto")]
    assert s2 and s2[-1]["plan_snapshot"]["usdt_target_share"] == "0.65"
    assert s2[-1]["intent"]["side"] == "BUY"

    hb = eng.write_heartbeat(rig.clock.now())
    assert hb.llm_ok is True and hb.extra["ai"]["plan_model"] == "fake/analyst-1"
    assert hb.extra["ai"]["stats"]["model_calls"] == 2
    rig.close()
    assert check_logs(rig.logs).ok


async def test_stale_plan_is_ignored(tmp_path: Path) -> None:
    fake = FakeProvider(analyst_script(plan_text(usdt_target_share=0.65)))
    rig = await ai_rig(tmp_path, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    await rig.tick()
    plan = await eng.slow_step(rig.clock.now())
    assert plan is not None
    await rig.tick(dt=eng.cfg.ai.plan_stale_s + 2)  # no new cycle in between
    now = rig.clock.now()
    assert eng.plan is plan and eng.current_plan(now) is None
    diag = s2_diag(rig)
    assert diag["plan_fresh"] is False and diag["target_origin"].startswith("rules table")
    health = eng.strategy_health(now)
    assert health is not None and health["plan"] is None and health["plan_stale"]["ts"] == plan.ts
    hb = eng.write_heartbeat(now)
    assert hb.extra["ai"]["plan_stale"] is True and any(r.startswith("plan stale") for r in hb.reasons)
    eng._check_plan_stale(now)
    eng._check_plan_stale(now + 5)
    assert len(rig.events("plan_stale")) == 1  # logged once per plan
    assert eng._context(now, rig.engine._snapshot()).plan is None  # risk sees no plan either
    rig.close()


# ---------------------------------------------------------------- failures degrade to rules-only


def openrouter_401() -> OpenRouterProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {INVALID_KEY}"
        return httpx.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}})

    return OpenRouterProvider(INVALID_KEY, transport=httpx.MockTransport(handler))


async def test_llm_failure_means_rules_only_and_llm_ok_false(tmp_path: Path) -> None:
    settings = no_keys(openrouter_api_key=INVALID_KEY)
    rig = await ai_rig(tmp_path, providers={FAKE_PROVIDER: openrouter_401()}, settings=settings)
    eng = rig.engine
    assert eng.ai_source == "llm"  # a key is set: the offline provider is never used
    await rig.tick()
    assert await eng.slow_step(rig.clock.now()) is None
    assert eng.plan is None and eng.llm_consecutive_failures == 1
    assert "401" in eng.ai_last_error and INVALID_KEY not in eng.ai_last_error
    hb = eng.write_heartbeat(rig.clock.now())
    assert hb.llm_ok is False and hb.extra["ai"]["llm_consecutive_failures"] == 1
    assert any(r.startswith("llm: 1 failed analyst cycle") and "rules-only" in r for r in hb.reasons)
    (ev,) = rig.events("ai_degraded")
    assert "strategies run rules-only" in ev["reason"]
    await rig.tick()
    assert s2_diag(rig)["plan_fresh"] is False and plan_records(rig) == []
    for name in ("decisions", "events", "health"):
        for path in rig.logs.glob(f"{name}*.jsonl"):
            assert INVALID_KEY not in path.read_text()
    rig.close()


async def test_a_failed_cycle_keeps_the_last_plan_until_it_is_stale(tmp_path: Path) -> None:
    calls = {"n": 0}

    def script(messages: Any, tools: Any, n: int) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise httpx.ConnectError("network down")
        return plan_text()

    rig = await ai_rig(tmp_path, cfg=ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False}),
                       providers={FAKE_PROVIDER: FakeProvider(script)})
    eng = rig.engine
    await rig.tick()
    plan = await eng.slow_step(rig.clock.now())
    assert plan is not None
    await rig.tick(dt=300)
    assert await eng.slow_step(rig.clock.now()) is None
    assert eng.current_plan(rig.clock.now()) is plan  # still fresh: kept
    (ev,) = rig.events("ai_degraded")
    assert "stays in force" in ev["reason"]
    await rig.tick(dt=300)
    assert await eng.slow_step(rig.clock.now()) is None
    assert eng.llm_consecutive_failures == 2 and eng.current_plan(rig.clock.now()) is plan
    await rig.tick(dt=700)  # 1300 s after the plan: stale, rules-only
    assert eng.current_plan(rig.clock.now()) is None and s2_diag(rig)["plan_fresh"] is False
    assert len(rig.events("ai_degraded")) == 1  # one event per outage, not per cycle
    rig.close()


async def test_fast_loop_is_not_delayed_by_a_slow_llm(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(messages: Any, tools: Any, n: int) -> Any:
        await release.wait()
        return plan_text(usdt_target_share=0.65)

    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: FakeProvider(lambda m, t, n: slow(m, t, n))})
    eng = rig.engine
    await rig.tick()
    cycle = asyncio.create_task(eng.analyst_cycle(rig.clock.now(), "manual"))
    await asyncio.sleep(0)  # the cycle is now waiting on the model
    ticks = eng.ticks
    for _ in range(5):
        await asyncio.wait_for(rig.tick(), timeout=1.0)  # every tick completes while the LLM call hangs
    assert eng.ticks == ticks + 5 and not cycle.done() and eng.plan is None
    assert eng._analyst_busy and await eng.slow_step(rig.clock.now()) is None  # no second cycle meanwhile
    release.set()
    plan = await asyncio.wait_for(cycle, timeout=1.0)
    assert plan is not None and eng.plan is plan
    await rig.tick()
    assert s2_diag(rig)["target_origin"] == "plan"
    rig.close()


async def test_engine_run_keeps_ticking_during_a_slow_llm_call(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def slow(messages: Any, tools: Any, n: int) -> Any:
        started.set()
        await asyncio.sleep(1.0)
        return plan_text()

    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False, "engine.fast_loop_interval_s": 0.02})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: FakeProvider(lambda m, t, n: slow(m, t, n))})
    eng = rig.engine
    counts: dict[str, int] = {}

    async def watch() -> None:
        await started.wait()
        counts["at_call"] = eng.ticks
        await asyncio.sleep(0.6)
        counts["during"] = eng.ticks - counts["at_call"]

    watcher = asyncio.create_task(watch())
    await eng.run(seconds=1.6, handle_signals=False)
    await watcher
    assert counts["during"] >= 10  # ~30 ticks of 20 ms fit in 0.6 s while the model call sleeps
    assert eng.plan is not None and plan_records(rig)
    rig.store.close()


# ---------------------------------------------------------------- reviewer


def review_text(suggestions: list[dict[str, Any]]) -> str:
    return json.dumps({"journal": "- S1 idle: cross-book edges stayed below cost.\n- S2 rebalanced once.",
                       "param_suggestions": suggestions, "alerts": ["no anomalies"]})


async def test_reviewer_applies_at_most_two_bounded_changes_and_the_hash_moves(tmp_path: Path) -> None:
    suggestions = [
        {"path": "risk.max_order_pct_equity", "value": 0.5, "rationale": "bigger orders"},  # never editable
        {"path": "strategies.s1_crossbook.margin_min_pct", "value": 0.002, "rationale": "books erratic"},
        {"path": "strategies.s2_usdtalloc.rebalance_threshold_pp", "value": 0.4, "rationale": "out of bounds"},
        {"path": "strategies.s2_usdtalloc.rebalance_threshold_pp", "value": 0.12, "rationale": "rebalance earlier"},
        {"path": "strategies.s3_momentum.min_regime_confidence", "value": 0.7, "rationale": "third change"},
    ]

    def script(messages: Any, tools: Any, n: int) -> Any:
        if is_reviewer(messages):
            return review_text(suggestions)
        return plan_text()

    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: FakeProvider(script)})
    eng = rig.engine
    old_hash, old_risk = eng.config_hash, eng.cfg.risk
    await rig.tick()
    await eng.slow_step(rig.clock.now())
    await rig.tick(dt=60)
    result = await eng.review_cycle(rig.clock.now())
    assert result is not None and result.change is not None
    accepted = {a["path"]: a["new"] for a in result.change.accepted}
    assert accepted == {"strategies.s1_crossbook.margin_min_pct": "0.002",
                        "strategies.s2_usdtalloc.rebalance_threshold_pp": "0.12"}
    why = {(r["path"], str(r["value"])): r["why"] for r in result.change.rejected}
    assert "forbidden" in why[("risk.max_order_pct_equity", "0.5")]
    assert "outside bounds" in why[("strategies.s2_usdtalloc.rebalance_threshold_pp", "0.4")]
    assert "max_changes" in why[("strategies.s3_momentum.min_regime_confidence", "0.7")]

    # reload_config: strategies, router, features and analyst see the new values; risk does not change
    new_hash = eng.config_hash
    assert new_hash != old_hash and new_hash == eng.cfg.config_hash
    assert eng.router.by_id("S1").cfg.margin_min_pct == Decimal("0.002")
    assert eng.router.by_id("S2").cfg.rebalance_threshold_pp == Decimal("0.12")
    assert eng.router.cfg is eng.cfg and eng.features.cfg is eng.cfg and eng.analyst.cfg is eng.cfg
    assert eng.cfg.risk == old_risk and eng.risk.cfg.risk == old_risk
    (ev,) = rig.events("config_change")
    data = ev["data"]
    assert (data["old_hash"], data["new_hash"], data["source"]) == (old_hash, new_hash, "reviewer")
    assert Path(data["overlay_path"]).parent == tmp_path / "runtime" and len(data["diff"]) == 2
    latest = yaml.safe_load((tmp_path / "runtime" / "latest.yaml").read_text())
    assert latest["strategies"]["s1_crossbook"]["margin_min_pct"] == 0.002
    journal = (rig.logs / "journal.md").read_text()
    assert f"`{old_hash}` -> `{new_hash}`" in journal and "S1 idle" in journal and "forbidden path" in journal

    # later decisions carry the new config_hash, earlier ones keep the old one
    await rig.tick(dt=300)
    await eng.slow_step(rig.clock.now())
    hashes = [r["config_hash"] for r in plan_records(rig)]
    assert hashes == [old_hash, new_hash]
    hb = eng.write_heartbeat(rig.clock.now())
    assert hb.config_hash == new_hash and hb.extra["ai"]["last_review"]["accepted"] == 2

    # risk and execution stay out of reach of reload_config
    with pytest.raises(ValueError, match="not reviewer-editable"):
        eng.reload_config(eng.cfg.model_copy(update={"risk": eng.cfg.risk.model_copy(
            update={"max_order_pct_equity": Decimal("0.9")})}))
    rig.close()
    assert check_logs(rig.logs).ok


async def test_reviewer_runs_on_its_interval(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path, cfg=ai_cfg(tmp_path, **{"engine.reviewer_interval_s": 300}))
    eng = rig.engine
    assert await eng.review_step(T0 + 10) is None  # the first review comes one interval after the start
    result = await eng.review_step(T0 + 301)
    assert result is not None and result.change is not None and not result.change.accepted  # offline: no tuning
    assert await eng.review_step(T0 + 400) is None and await eng.review_step(T0 + 602) is not None
    journal = (rig.logs / "journal.md").read_text()
    assert journal.count("## Review ") == 2 and OFFLINE_LABEL in journal
    rig.close()


# ---------------------------------------------------------------- runtime overlay at startup


async def _synthetic_engine(tmp_path: Path, cfg: Config, **options: Any) -> Engine:
    return await build_engine(cfg, no_keys(), "shadow", feed="synthetic", log_dir=tmp_path / "logs",
                              clock=SimClock(T0), kill_file=None, runtime_dir=tmp_path / "runtime",
                              mcp=None, **options)


async def test_runtime_overlay_is_applied_at_startup_within_bounds(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "latest.yaml").write_text(
        "# runtime overlay written by reviewer\n"
        + yaml.safe_dump({
            "strategies": {"s1_crossbook": {"margin_min_pct": 0.002},
                           "s2_usdtalloc": {"rebalance_threshold_pp": 0.5}},  # out of bounds
            "risk": {"max_order_pct_equity": 0.9},  # never editable
            "engine": {"slow_loop_interval_s": 600},  # pinned by the operator below
        })
    )
    cfg = make_cfg(tmp_path)
    eng = await _synthetic_engine(tmp_path, cfg, pinned_keys=["engine.slow_loop_interval_s"])
    try:
        assert eng.cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.002")
        assert eng.cfg.risk.max_order_pct_equity == cfg.risk.max_order_pct_equity
        assert eng.cfg.engine.slow_loop_interval_s == cfg.engine.slow_loop_interval_s
        assert eng.config_hash != cfg.config_hash
        assert eng.router.by_id("S1").cfg.margin_min_pct == Decimal("0.002")
        assert eng.config_manager is not None
        # the pinned key is not applied, but it stays in the manager's overlay: the reviewer's next
        # write of latest.yaml keeps it, and the reviewer can never change it
        assert eng.config_manager.overlay == {"strategies.s1_crossbook.margin_min_pct": 0.002,
                                              "engine.slow_loop_interval_s": 600.0}
        assert eng.config_manager.pinned == {"engine.slow_loop_interval_s"}
        await eng.startup()
    finally:
        eng.shutdown("test")
        await eng.aclose()
    (start,) = [e for e in iter_family(tmp_path / "logs", "events") if e["kind"] == "start"]
    info = start["data"]["runtime_overlay"]
    assert info["applied"] == {"strategies.s1_crossbook.margin_min_pct": 0.002}
    assert info["pinned"] == {"engine.slow_loop_interval_s": 600.0}
    assert info["config_hash_before"] == cfg.config_hash and info["config_hash_after"] == eng.config_hash
    assert start["data"]["config_hash"] == eng.config_hash and len(info["sha256"]) == 12
    why = {r["path"]: r["why"] for r in info["rejected"]}
    assert why == {"engine.slow_loop_interval_s": "overridden by --set",
                   "risk.max_order_pct_equity": "not reviewer-editable",
                   "strategies.s2_usdtalloc.rebalance_threshold_pp": why["strategies.s2_usdtalloc.rebalance_threshold_pp"]}
    assert "outside bounds" in why["strategies.s2_usdtalloc.rebalance_threshold_pp"]
    assert start["data"]["ai"]["source"] == "offline-rules"

    off = await _synthetic_engine(tmp_path / "off", make_cfg(tmp_path / "off"), runtime_overlay=False)
    try:
        assert off.cfg.strategies.s1_crossbook.margin_min_pct == cfg.strategies.s1_crossbook.margin_min_pct
        assert off.runtime_overlay is None
    finally:
        await off.aclose()


# ---------------------------------------------------------------- ablation


async def test_ai_disabled_means_no_analyst_and_no_plans(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path, cfg=ai_cfg(tmp_path, **{"ai.enabled": False}))
    eng = rig.engine
    assert eng.ai_source == "disabled" and eng.analyst is None and eng.reviewer is None
    await rig.tick()
    assert await eng.slow_step(rig.clock.now()) is None and await eng.analyst_cycle(rig.clock.now()) is None
    await rig.tick(dt=600)
    assert eng.plan is None and plan_records(rig) == []
    assert all(r["plan_snapshot"] is None for r in rig.raw_decisions())
    hb = eng.write_heartbeat(rig.clock.now())
    assert hb.llm_ok is None and hb.extra["ai_source"] == "disabled"
    assert s2_diag(rig)["target_origin"].startswith("rules table")
    rig.close()
    built = await _synthetic_engine(tmp_path / "b", make_cfg(tmp_path / "b", **{"ai.enabled": False}))
    try:
        assert built.analyst is None and built.ai_source == "disabled"
    finally:
        await built.aclose()


# ---------------------------------------------------------------- replay


async def test_replay_reproduces_offline_plans_with_zero_provider_calls(tmp_path: Path) -> None:
    cfg = ai_cfg(tmp_path, **{"engine.slow_loop_interval_s": 300})
    rig = await ai_rig(tmp_path, cfg=cfg)
    for _ in range(3):
        await rig.tick()
        assert await rig.engine.slow_step(rig.clock.now()) is not None
        await rig.tick(dt=300)
    rig.close()
    recorded = plan_records(rig)
    report = await replay_recorded_plans(cfg, rig.logs)
    assert report.ok, report.summary()
    assert (report.plans, report.reproduced, report.provider_calls, report.cache_misses) == (3, 3, 0, 0)
    assert [r.replayed_prompt_hash for r in report.results] == [r["features"]["prompt_hash"] for r in recorded]
    assert all(r.route == "offline/offline-rules-v1" for r in report.results)


async def test_replay_of_llm_plans_is_strict_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(analyst_script(plan_text(usdt_target_share=0.6)))
    cfg = ai_cfg(tmp_path)
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    await rig.tick()
    plan = await rig.engine.slow_step(rig.clock.now())
    assert plan is not None and len(fake.calls) == 2
    rig.close()

    async def no_network(self: Any, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        raise AssertionError(f"network call in a replay: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    report = await replay_recorded_plans(cfg, rig.logs)
    assert report.ok and report.provider_calls == 0 and len(fake.calls) == 2, report.summary()
    assert report.results[0].route == "openrouter/fake/analyst-1"
    assert report.results[0].replayed_prompt_hash == plan.prompt_hash

    # a missing answer is a hard error, never a provider call
    cache_dir = rig.logs / "llm_cache"
    (cache_dir / f"{plan.prompt_hash}.json").rename(tmp_path / "moved.json")
    broken = await replay_recorded_plans(cfg, rig.logs)
    assert not broken.ok and broken.provider_calls == 0 and "not in the cache" in broken.results[0].error
    (tmp_path / "moved.json").rename(cache_dir / f"{plan.prompt_hash}.json")
    tool_answer = next(p for p in cache_dir.glob("*.json") if p.stem != plan.prompt_hash)
    tool_answer.unlink()
    missing = await replay_recorded_plans(cfg, rig.logs)
    assert not missing.ok and missing.provider_calls == 0 and "cache miss" in missing.results[0].error
    strict = LLMClient({}, Route(FAKE_PROVIDER, "fake/analyst-1"), None, LLMCache(cache_dir, strict=True),
                       mode="replay", clock=SimClock(T0))
    with pytest.raises(CacheMissError):
        await strict.chat([{"role": "user", "content": "never recorded"}], None)


# ---------------------------------------------------------------- Telegram


class TelegramServer:
    """``httpx.MockTransport`` handler for the Bot API: records sendMessage, serves getUpdates."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.updates: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.sent.append(body["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(404, json={"ok": False, "description": "not found"})

    def command(self, update_id: int, text: str, date: float, chat: str = CHAT_ID) -> None:
        self.updates.append({"update_id": update_id, "message": {"message_id": update_id, "date": int(date),
                                                                 "chat": {"id": int(chat)}, "text": text}})


async def telegram_rig(tmp_path: Path, max_msgs: int = 20) -> tuple[Rig, TelegramServer]:
    server = TelegramServer()
    settings = no_keys(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID)
    cfg = ai_cfg(tmp_path, **{"alerts.telegram_enabled": True, "alerts.max_msgs_per_10min": max_msgs})
    rig = await ai_rig(tmp_path, cfg=cfg, settings=settings, enable=False)
    assert rig.engine.enable_telegram(transport=httpx.MockTransport(server))
    rig.engine.enable_ai(mcp=None, runtime_dir=tmp_path / "runtime")
    return rig, server


async def test_telegram_is_off_without_config(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path)
    assert rig.engine.enable_telegram() is False and rig.engine.notifier is None
    rig.close()
    cfg_on = ai_cfg(tmp_path / "x", **{"alerts.telegram_enabled": False})
    rig2 = await ai_rig(tmp_path / "x", cfg=cfg_on,
                        settings=no_keys(telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID))
    assert rig2.engine.enable_telegram() is False  # configured but alerts.telegram_enabled is false
    rig2.close()


async def test_telegram_commands_become_engine_actions_and_events(tmp_path: Path) -> None:
    rig, server = await telegram_rig(tmp_path)
    eng = rig.engine
    commands = eng.telegram_commands
    assert commands is not None and commands.enabled
    server.command(1, "/pause checking a fill", rig.clock.now())
    server.command(2, "/buy BTCUSDT 1", rig.clock.now())  # no trading command exists
    server.command(3, "/pause from a stranger", rig.clock.now(), chat="999")
    await commands.poll_once()
    await eng.notifier.drain()
    assert any("pause requested" in s for s in server.sent) and any("not supported" in s.lower() or "no trading"
                                                                   in s.lower() for s in server.sent)
    await rig.tick()
    assert eng.flags.paused and eng.flags.pause_reason == "telegram: checking a fill"
    (pause,) = rig.events("pause")
    assert pause["data"]["source"] == "telegram"
    ops = rig.events("operator_command")
    assert [o["data"]["command"] for o in ops] == ["pause"] and ops[0]["data"]["source"] == "telegram"

    # /resume lifts only a /pause; /status only reads; /kill trips the kill-switch
    assert "resume requested" in await eng._telegram_command("resume", "")
    await rig.tick()
    assert not eng.flags.paused and rig.events("resume")[-1]["data"]["source"] == "telegram"
    assert "WTA status" in await eng._telegram_command("status", "")
    await eng._telegram_command("kill", "operator says stop")
    await rig.tick()
    assert eng.flags.killed and "telegram /kill" in eng.flags.kill_reason
    assert rig.events("kill")[-1]["data"]["source"] == "telegram"
    assert "resume requested" in await eng._telegram_command("resume", "")
    await rig.tick()
    assert eng.flags.killed  # /resume never resets a kill
    assert rig.events("operator_command_ignored")
    await eng.notifier.drain()
    assert any(s.startswith("[wta shadow] RISK PAUSE") for s in server.sent)
    assert any("RISK KILL-SWITCH" in s for s in server.sent)
    rig.close()
    assert check_logs(rig.logs).ok


async def test_telegram_alerts_are_rate_limited_into_a_digest(tmp_path: Path) -> None:
    rig, server = await telegram_rig(tmp_path, max_msgs=3)
    eng = rig.engine
    for i in range(6):
        eng._alert(f"alert {i}", "test")
    await eng.notifier.drain()
    assert len(server.sent) == 3 and eng.notifier.digest_pending == 3
    rig.clock.advance(601)
    assert await eng.notifier.flush_digest()
    assert len(server.sent) == 4 and "alert 5" in server.sent[-1]  # the overflow as ONE message
    rig.close()


async def test_telegram_alert_after_three_llm_failures(tmp_path: Path) -> None:
    server = TelegramServer()
    settings = no_keys(openrouter_api_key=INVALID_KEY, telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID)
    cfg = ai_cfg(tmp_path, **{"alerts.telegram_enabled": True})
    rig = await ai_rig(tmp_path, cfg=cfg, settings=settings, providers={FAKE_PROVIDER: openrouter_401()})
    eng = rig.engine
    eng.enable_telegram(transport=httpx.MockTransport(server))
    await rig.tick()
    for _ in range(4):
        assert await eng.slow_step(rig.clock.now()) is None
        await rig.tick(dt=301)
    await eng.notifier.drain()
    llm_alerts = [s for s in server.sent if "LLM: " in s]
    assert len(llm_alerts) == 1 and "3 failures in a row" in llm_alerts[0]
    assert all(INVALID_KEY not in s for s in server.sent)
    rig.close()


async def test_hourly_equity_alert(tmp_path: Path) -> None:
    rig, server = await telegram_rig(tmp_path)
    eng = rig.engine
    await rig.tick()
    eng.write_heartbeat(rig.clock.now())
    await rig.tick(dt=1800)
    eng.write_heartbeat(rig.clock.now())
    await rig.tick(dt=1801)
    eng.write_heartbeat(rig.clock.now())
    await eng.notifier.drain()
    assert len([s for s in server.sent if "EQUITY" in s]) == 1
    rig.close()


def test_status_text_reads_this_engines_files(tmp_path: Path) -> None:
    async def go() -> str:
        rig = await ai_rig(tmp_path)
        await rig.tick()
        await rig.engine.slow_step(rig.clock.now())
        rig.engine.write_heartbeat(rig.clock.now())
        text = rig.engine.status_text(rig.clock.now())
        rig.close()
        return text

    text = asyncio.run(go())
    assert "WTA status" in text and "offline rules (no LLM key)" in text
    assert tg.TELEGRAM_MAX_CHARS > len(text)
