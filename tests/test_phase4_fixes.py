"""Regression tests for the Phase-4 review findings on the AI layer (engine side).

Each test names the defect it pins: the MCP key leaking through a tool result, the reviewer
overlay shared by every account / applied in the ablation / losing pinned entries / forgetting its
cooldown / written non-atomically, a reviewer reload in the middle of a tick or of an analyst
cycle, the analyst budget counted per request, a whole-cycle timeout that ignores the fallback,
the review window without an upper bound, and replays that cannot follow a mid-cycle failover or a
config change. No network, no wall clock (one test sleeps for well under a second).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml

from wta.ai.analyst import RegimeAnalyst
from wta.ai.cache import LLMCache
from wta.ai.config_manager import PINNED_WHY, ConfigManager, overlay_meta
from wta.ai.llm_client import LLMClient, ProviderError, Route, ToolCall
from wta.ai.mcp_client import MCPClient
from wta.ai.replay import _empty_tools, replay_recorded_plans
from wta.ai.reviewer import PostTradeReviewer
from wta.ai.wiring import load_runtime_overlay, runtime_dir_for
from wta.config import CONFIG_DIR, Config, apply_changes, canonical_json
from wta.engine import read_log_window
from wta.logging_.jsonl import iter_family
from wta.utils.timeutil import SimClock

from .conftest import T0
from .test_ai_engine import (
    FAKE_PROVIDER,
    FakeProvider,
    _synthetic_engine,
    ai_cfg,
    ai_rig,
    analyst_script,
    is_reviewer,
    no_keys,
    plan_records,
    plan_text,
    review_text,
)
from .test_mcp_client import URL, FakeServer, _reply
from .test_strategies_engine import make_cfg

MARGIN = "strategies.s1_crossbook.margin_min_pct"
REBAL = "strategies.s2_usdtalloc.rebalance_threshold_pp"
SLOW = "engine.slow_loop_interval_s"
MCP_KEY = "".join(["wx-", "fixes", "-test-", "q" * 14])  # built at runtime: no key-like literal
BALANCES = "wallex_basic_get_account_balances"


class GeminiFake(FakeProvider):
    name = "gemini"


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.json"))


# ---------------------------------------------------------------- MCP: the key never leaves the client


async def test_mcp_tool_result_is_masked_before_it_reaches_the_model_cache_and_tape(tmp_path: Path) -> None:
    """A server that echoes the API key in an ``isError`` result and in ``structuredContent``."""
    server = FakeServer()
    server.handlers["tools/list"] = {"tools": [{"name": BALANCES, "inputSchema": {"type": "object"}}]}
    server.handlers["tools/call"] = lambda req, msg: _reply(req, {
        "isError": True,
        "content": [{"type": "text", "text": f"invalid api key {MCP_KEY}"}],
        "structuredContent": {"echo": MCP_KEY, "balance": 1.5, MCP_KEY: "as a key"},
    })
    with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, MCP_KEY) as mcp:
            raw = await mcp.call_tool(BALANCES, {})
            assert MCP_KEY not in canonical_json(raw).decode()
            assert raw["structuredContent"]["balance"] == Decimal("1.5")  # numbers stay Decimal
            assert raw["isError"] is True and "***" in raw["content"][0]["text"]

            def script(messages: list[dict[str, Any]], tools: Any, n: int) -> Any:
                if n == 1:
                    return [ToolCall("c1", BALANCES, {})]
                return plan_text()

            fake = FakeProvider(script)
            clock = SimClock(T0)
            cfg = ai_cfg(tmp_path)
            cache = LLMCache(tmp_path / "cache")
            llm = LLMClient({FAKE_PROVIDER: fake}, Route(FAKE_PROVIDER, "fake/analyst-1"), None, cache, clock=clock)
            analyst = RegimeAnalyst(cfg, llm, _empty_tools(cfg, clock), mcp, clock)
            plan = await analyst.run({}, "manual")
    assert plan is not None and plan.tools_source == "mcp"
    sent = json.dumps(fake.calls[1]["messages"])
    assert '\\"echo\\":\\"***\\"' in sent and MCP_KEY not in sent  # structuredContent is what the model sees
    assert MCP_KEY not in _all_text(tmp_path / "cache")  # LLM cache + mcp_tape


async def test_analyst_masks_secrets_in_mcp_results_as_a_second_layer(tmp_path: Path) -> None:
    """Even if a client returned the key unmasked, the analyst's ``secrets`` keep it out."""

    class LeakyMCP:
        url = "https://mcp.test/mcp"

        async def initialize(self) -> dict[str, Any]:
            return {}

        async def list_tools(self) -> list[dict[str, Any]]:
            return [{"name": BALANCES}]

        def allowed_tools(self) -> list[dict[str, Any]]:
            return [{"name": BALANCES, "inputSchema": {"type": "object"}}]

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": f"key={MCP_KEY}"}]}

    fake = FakeProvider(lambda m, t, n: [ToolCall("c1", BALANCES, {})] if n == 1 else plan_text())
    clock = SimClock(T0)
    cfg = ai_cfg(tmp_path)
    llm = LLMClient({FAKE_PROVIDER: fake}, Route(FAKE_PROVIDER, "fake/analyst-1"), None,
                    LLMCache(tmp_path / "cache"), clock=clock)
    analyst = RegimeAnalyst(cfg, llm, _empty_tools(cfg, clock), LeakyMCP(), clock,  # type: ignore[arg-type]
                            secrets=[MCP_KEY])
    assert await analyst.run({}, "manual") is not None
    assert MCP_KEY not in json.dumps(fake.calls[1]["messages"]) and "key=***" in json.dumps(fake.calls[1]["messages"])
    assert MCP_KEY not in _all_text(tmp_path / "cache")


# ---------------------------------------------------------------- runtime overlay: one per account


def test_runtime_dir_defaults_per_account() -> None:
    assert runtime_dir_for("live") == CONFIG_DIR / "runtime" / "live"
    assert runtime_dir_for("shadow") == CONFIG_DIR / "runtime" / "shadow"
    assert runtime_dir_for("shadow", "synthetic") == CONFIG_DIR / "runtime" / "shadow_synthetic"


async def test_engine_without_runtime_dir_uses_its_accounts_directory(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    from wta.engine import build_engine

    eng = await build_engine(cfg, no_keys(), "shadow", feed="synthetic", log_dir=tmp_path / "logs",
                             clock=SimClock(T0), kill_file=None, mcp=None)
    try:
        assert eng.runtime_dir == CONFIG_DIR / "runtime" / "shadow_synthetic"
        assert eng.config_manager is not None and eng.config_manager.runtime_dir == eng.runtime_dir
        assert eng.config_manager.account == "shadow_synthetic"
    finally:
        await eng.aclose()


def test_overlay_header_names_the_account_and_another_account_is_refused(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cm = ConfigManager(cfg, tmp_path / "rt", account="shadow_synthetic", mode="shadow", feed="synthetic",
                       git_commit="abc1234")
    res = cm.apply([{"path": MARGIN, "value": 0.002, "rationale": "synthetic fills"}], now=T0)
    assert res.changed
    latest = tmp_path / "rt" / "latest.yaml"
    meta = overlay_meta(latest.read_text())
    assert meta == {"account": "shadow_synthetic", "mode": "shadow", "feed": "synthetic", "git_commit": "abc1234",
                    "applied_ts": T0, "old_hash": res.old_hash, "new_hash": res.new_hash, "source": "reviewer"}

    same, info = load_runtime_overlay(cfg, latest, account="shadow_synthetic")
    assert info is not None and info["applied"] == {MARGIN: 0.002} and info["applied_ts"] == T0
    assert same.strategies.s1_crossbook.margin_min_pct == Decimal("0.002")

    live, refused = load_runtime_overlay(cfg, latest, account="live")
    assert refused is not None and live.config_hash == cfg.config_hash and refused["applied"] == {}
    assert "written by another account (shadow_synthetic" in refused["skipped"]
    assert refused["rejected"][0]["path"] == "*"


async def test_ablation_does_not_apply_the_reviewer_overlay(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "latest.yaml").write_text(yaml.safe_dump({"strategies": {"s1_crossbook": {
        "margin_min_pct": 0.002}}}))
    cfg = make_cfg(tmp_path, **{"ai.enabled": False})
    eng = await _synthetic_engine(tmp_path, cfg)
    try:
        assert eng.cfg.strategies.s1_crossbook.margin_min_pct == cfg.strategies.s1_crossbook.margin_min_pct
        assert eng.config_hash == cfg.config_hash  # the rules-only run is the configs/*.yaml baseline
        assert eng.runtime_overlay is not None and eng.runtime_overlay["skipped"] == "ai.enabled is false"
        await eng.startup()
    finally:
        eng.shutdown("test")
        await eng.aclose()
    (start,) = [e for e in iter_family(tmp_path / "logs", "events") if e["kind"] == "start"]
    assert start["data"]["runtime_overlay"]["skipped"] == "ai.enabled is false"
    assert start["data"]["config_hash"] == cfg.config_hash


# ---------------------------------------------------------------- pinned keys, cooldown, atomic write


def test_pinned_keys_are_never_offered_or_changed_by_the_reviewer(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cm = ConfigManager(cfg, tmp_path / "rt", pinned={MARGIN})
    res = cm.apply([{"path": MARGIN, "value": 0.0008, "rationale": "lower"},
                    {"path": REBAL, "value": 0.2, "rationale": "ok"}], now=T0)
    assert [a["path"] for a in res.accepted] == [REBAL]
    assert {r["path"]: r["why"] for r in res.rejected} == {MARGIN: PINNED_WHY}
    reviewer = PostTradeReviewer(cfg, None, cm, SimClock(T0), journal_path=tmp_path / "journal.md")
    prompt = reviewer.system_prompt()
    assert f"`{REBAL}`" in prompt and f"`{MARGIN}`" not in prompt


async def test_a_pinned_overlay_entry_survives_the_next_review(tmp_path: Path) -> None:
    """latest.yaml {margin, slow}; restart with margin pinned; the reviewer accepts REBAL: latest.yaml
    must still hold margin (the next start without the pin gets it back)."""
    rt = tmp_path / "rt"
    rt.mkdir()
    meta = {"account": "shadow_test", "applied_ts": T0 - 7200}
    (rt / "latest.yaml").write_text(f"# meta {json.dumps(meta)}\n"
                                    + yaml.safe_dump({"strategies": {"s1_crossbook": {"margin_min_pct": 0.0023}},
                                                      "engine": {"slow_loop_interval_s": 420}}))
    base = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    pinned_cfg = apply_changes(base, {MARGIN: 0.001})  # the operator's --set
    cfg, info = load_runtime_overlay(pinned_cfg, rt / "latest.yaml", frozenset({MARGIN}), account="shadow_test")
    assert info is not None and info["pinned"] == {MARGIN: 0.0023} and info["applied"] == {SLOW: 420.0}

    fake = FakeProvider(lambda m, t, n: review_text([{"path": REBAL, "value": 0.2, "rationale": "x"}])
                        if is_reviewer(m) else plan_text())
    rig = await ai_rig(tmp_path, cfg=cfg, enable=False)
    eng = rig.engine
    eng.enable_ai(providers={FAKE_PROVIDER: fake}, mcp=None, runtime_dir=rt, runtime_overlay=info,
                  pinned_keys=[MARGIN])
    assert eng.config_manager is not None and eng.config_manager.last_apply_ts == T0 - 7200
    await rig.tick()
    result = await eng.review_cycle(rig.clock.now())
    assert [a["path"] for a in result.change.accepted] == [REBAL]
    assert eng.cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.001")  # the pin still wins now
    after = yaml.safe_load((rt / "latest.yaml").read_text())
    assert after["strategies"]["s1_crossbook"]["margin_min_pct"] == 0.0023
    assert after["engine"]["slow_loop_interval_s"] == 420.0
    assert after["strategies"]["s2_usdtalloc"]["rebalance_threshold_pp"] == 0.2
    rig.close()


async def test_the_reviewer_cooldown_survives_a_restart(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    base = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    first = ConfigManager(base, rt, account="shadow_test")
    assert first.apply([{"path": MARGIN, "value": 0.002, "rationale": "x"}], now=T0 - 600).changed
    cfg, info = load_runtime_overlay(base, rt / "latest.yaml", account="shadow_test")
    assert info is not None and info["applied_ts"] == T0 - 600

    fake = FakeProvider(lambda m, t, n: review_text([{"path": REBAL, "value": 0.2, "rationale": "x"}])
                        if is_reviewer(m) else plan_text())
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake}, runtime_overlay=info)
    eng = rig.engine
    assert eng.config_manager is not None and eng.config_manager.last_apply_ts == T0 - 600
    await rig.tick()
    result = await eng.review_cycle(rig.clock.now())  # 602 s after the last apply, cooldown 3600 s
    assert not result.change.accepted
    assert all("cooldown" in r["why"] for r in result.change.rejected)
    rig.close()


def test_overlay_files_are_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    rt = tmp_path / "rt"
    cm = ConfigManager(cfg, rt)
    assert cm.apply([{"path": MARGIN, "value": 0.002, "rationale": "x"}], now=T0).changed
    before = (rt / "latest.yaml").read_text()

    def crash(src: Any, dst: Any) -> None:
        raise OSError("power loss")

    monkeypatch.setattr("wta.ai.config_manager.os.replace", crash)
    with pytest.raises(OSError):
        cm.apply([{"path": REBAL, "value": 0.2, "rationale": "x"}], now=T0 + 7200)
    assert (rt / "latest.yaml").read_text() == before  # never empty or half-written
    assert not [p for p in rt.iterdir() if p.name.startswith(".tmp-")]  # no temp file left behind


# ---------------------------------------------------------------- reviewer reload: at a tick boundary


async def test_a_reviewer_reload_waits_for_the_next_tick_boundary(tmp_path: Path) -> None:
    fake = FakeProvider(lambda m, t, n: review_text([{"path": MARGIN, "value": 0.002, "rationale": "x"}])
                        if is_reviewer(m) else plan_text())
    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    await rig.tick()
    old_hash = eng.config_hash
    gate = asyncio.Event()
    original = eng._refresh_balances

    async def slow_refresh(now: float, books: Any) -> None:  # a tick suspended on an exchange await
        await gate.wait()
        await original(now, books)

    eng._refresh_balances = slow_refresh  # type: ignore[method-assign]
    rig.clock.advance(2)
    now = rig.clock.now()
    rig.feed.publish(now)
    tick = asyncio.create_task(eng.tick(now))
    for _ in range(50):
        await asyncio.sleep(0)
        if eng._in_tick and not gate.is_set():
            break
    assert eng._in_tick
    result = await eng.review_cycle(now)
    assert result.change is not None and result.change.accepted
    new_hash = result.change.new_hash
    # the running tick keeps the config its intents were computed under
    assert eng.config_hash == old_hash and eng.decision_log.config_hash == old_hash
    assert eng.router.by_id("S1").cfg.margin_min_pct == cfg.strategies.s1_crossbook.margin_min_pct
    assert eng.last_review["config_hash"] == new_hash
    gate.set()
    await tick
    assert eng.config_hash == old_hash
    eng._refresh_balances = original  # type: ignore[method-assign]
    await rig.tick()  # the swap happens first thing in the next tick
    assert eng.config_hash == new_hash == eng.decision_log.config_hash
    assert eng.router.by_id("S1").cfg.margin_min_pct == Decimal("0.002")
    records = [r for r in rig.raw_decisions() if r.get("ts", "").startswith("2026-10-22T10:00:04")]
    assert all(r["config_hash"] == old_hash for r in records)
    rig.close()


async def test_a_reload_during_an_analyst_cycle_does_not_change_that_cycle(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def held() -> str:
        started.set()
        await release.wait()
        return plan_text()

    fake = FakeProvider(lambda m, t, n: held() if n == 1 else plan_text())
    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    await rig.tick()
    old_hash = eng.config_hash
    cycle = asyncio.create_task(eng.analyst_cycle(rig.clock.now(), "manual"))
    await asyncio.wait_for(started.wait(), 2)
    new_cfg = apply_changes(eng.cfg, {MARGIN: 0.002})
    eng.reload_config(new_cfg)
    release.set()
    plan = await cycle
    assert plan is not None
    system = fake.calls[0]["messages"][0]["content"]
    assert "Current config margin: 0.0015 " in system  # the prompt saw the old margin
    (rec,) = plan_records(rig)
    assert rec["config_hash"] == old_hash != new_cfg.config_hash  # stamped with the cycle's config
    assert "config_hash" not in rec["features"]
    tape = LLMCache(rig.logs / "llm_cache" / "mcp_tape").get(f"analyst-cycle-{plan.prompt_hash}")
    assert tape is not None and tape["config_hash"] == old_hash
    assert eng.analyst.cfg is new_cfg  # the next cycle uses the new config
    await rig.tick(dt=300)
    await eng.analyst_cycle(rig.clock.now(), "manual")
    assert [r["config_hash"] for r in plan_records(rig)] == [old_hash, new_cfg.config_hash]
    rig.close()


# ---------------------------------------------------------------- budgets and the cycle timeout


async def test_the_analyst_budget_counts_cycles_not_requests(tmp_path: Path) -> None:
    """5 cycles a day, each 2 requests (a tool round + the answer): all 5 run (the old per-request
    count stopped after 2 cycles); the 6th is refused before any request; a new UTC day resets."""
    fake = FakeProvider(analyst_script(plan_text()))
    cfg = ai_cfg(tmp_path, **{"ai.max_analyst_calls_per_day": 5})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    for i in range(5):
        assert await eng.analyst_cycle(T0 + 60 * (i + 1), "scheduled") is not None, eng.analyst.last_error
    assert len(fake.calls) == 10
    assert await eng.analyst_cycle(T0 + 600, "scheduled") is None
    assert "daily analyst cycle budget exhausted" in eng.analyst.last_error and len(fake.calls) == 10
    assert eng.ai_health(T0 + 600)["llm_ok"] is False
    assert await eng.analyst_cycle(T0 + 86400, "scheduled") is not None  # next UTC day
    rig.close()


async def test_the_reviewer_budget_counts_reviews(tmp_path: Path) -> None:
    fake = FakeProvider(lambda m, t, n: review_text([]) if is_reviewer(m) else plan_text())
    cfg = ai_cfg(tmp_path, **{"ai.max_reviewer_calls_per_day": 1, "ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    first = await rig.engine.review_cycle(T0 + 60)
    second = await rig.engine.review_cycle(T0 + 120)
    assert first.raw_ok and not second.raw_ok
    assert "daily reviewer budget exhausted" in second.skipped_reason
    assert sum(1 for c in fake.calls if is_reviewer(c["messages"])) == 1
    rig.close()


async def test_the_cycle_cap_lets_the_fallback_answer_every_call_of_a_hung_primary(tmp_path: Path) -> None:
    async def hang() -> str:
        await asyncio.sleep(3600)
        return ""

    primary = FakeProvider(lambda m, t, n: hang())
    replies = iter([[ToolCall("c1", "get_health", {})], [ToolCall("c2", "get_health", {})], "not json", plan_text()])
    fallback = GeminiFake(lambda m, t, n: next(replies))
    cfg = ai_cfg(tmp_path, **{"ai.analyst_timeout_s": 0.1, "ai.analyst_max_tool_rounds": 2,
                              "llm.fallback.model": "fake/fallback-1"})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: primary, "gemini": fallback})
    eng = rig.engine
    # 4 calls (2 tool rounds, the final answer, one JSON fix) x 0.1 s on each of 2 routes
    assert eng._analyst_timeout_s() == pytest.approx(0.1 * 4 * 2)
    plan = await eng.analyst_cycle(T0 + 60, "manual")  # 4 primary timeouts = 0.4 s: the old cap
    assert plan is not None, eng.analyst.last_error
    assert len(fallback.calls) == 4 and plan.model == "fake/fallback-1"
    rig.close()


def test_the_cycle_cap_counts_mcp_tool_calls() -> None:
    from wta.ai.analyst import MAX_CALLS_PER_ROUND
    from wta.ai.wiring import MCP_TIMEOUT_S
    from wta.engine import Engine

    class Fake:
        cfg = ai_cfg(Path("/nonexistent"))
        analyst = None
        mcp = object()

    cap = Engine._analyst_timeout_s(Fake())  # type: ignore[arg-type]
    ai = Fake.cfg.ai
    assert cap == ai.analyst_timeout_s * (ai.analyst_max_tool_rounds + 2) + \
        MCP_TIMEOUT_S * MAX_CALLS_PER_ROUND * ai.analyst_max_tool_rounds + 30.0


# ---------------------------------------------------------------- the review window has an upper bound


def test_read_log_window_skips_records_after_until(tmp_path: Path) -> None:
    from wta.utils.timeutil import iso

    now = T0
    lines = [
        {"kind": "order", "decision_id": "d1", "ts": iso(now - 100)},
        {"kind": "outcome", "decision_id": "d1", "ts": iso(now + 50), "outcome": {"pnl_tmn": "123"}},
        {"kind": "note", "decision_id": "d2"},  # undated, written after the cut
    ]
    (tmp_path / "decisions-20261022.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    unbounded = read_log_window(tmp_path, "decisions", now - 3600)
    assert len(unbounded) == 3
    bounded = read_log_window(tmp_path, "decisions", now - 3600, until=now)
    assert [r["kind"] for r in bounded] == ["order"]


async def test_review_input_never_contains_an_outcome_stamped_after_now(tmp_path: Path) -> None:
    rig = await ai_rig(tmp_path)
    eng = rig.engine
    await rig.tick()
    now = rig.clock.now()
    with eng.clock.frozen(now - 1):
        eng.decision_log.record(kind="order", decision_id="d_x", strategy="S2", symbol="USDTTMN")
    with eng.clock.frozen(now + 50):
        eng.decision_log.outcome("d_x", {"exit_reason": "target", "pnl_tmn": "123"})
    on_disk = [r for r in iter_family(rig.logs, "decisions") if r.get("decision_id") == "d_x"]
    assert [r["kind"] for r in on_disk] == ["order", "outcome"]  # both lines are in the file
    logs = eng.read_review_logs(now)
    mine = [r["kind"] for r in logs["decisions"] if r.get("decision_id") == "d_x"]
    assert mine == ["order"]  # the outcome stamped after the review's now is not part of it
    rig.close()


# ---------------------------------------------------------------- replay: failover and config changes


async def test_replay_follows_a_mid_cycle_failover(tmp_path: Path) -> None:
    def primary_script(messages: Any, tools: Any, n: int) -> Any:
        if n == 1:
            return [ToolCall("call_f", "get_features", {"symbols": ["BTCUSDT"]})]
        raise ProviderError("openrouter HTTP 429: rate limited")

    primary = FakeProvider(primary_script)
    fallback = GeminiFake(lambda m, t, n: plan_text(usdt_target_share=0.55))
    cfg = ai_cfg(tmp_path, **{"llm.fallback.model": "fake/fallback-1"})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: primary, "gemini": fallback})
    await rig.tick()
    plan = await rig.engine.slow_step(rig.clock.now())
    assert plan is not None and len(primary.calls) == 2 and len(fallback.calls) == 1
    rig.close()
    (rec,) = plan_records(rig)
    assert [c["provider"] for c in rec["features"]["calls"]] == [FAKE_PROVIDER, "gemini"]

    report = await replay_recorded_plans(cfg, rig.logs)
    assert report.ok, report.summary()
    assert report.provider_calls == 0 and len(primary.calls) == 2 and len(fallback.calls) == 1
    assert report.results[0].route == "openrouter/fake/analyst-1 + gemini/fake/fallback-1"

    # an older record without ``calls``: the run's start event names the fallback route
    path = next(rig.logs.glob("decisions*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get("kind") == "plan":
            row["features"].pop("calls")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    legacy = await replay_recorded_plans(cfg, rig.logs)
    assert legacy.ok, legacy.summary()


async def test_replay_uses_each_cycles_own_config(tmp_path: Path) -> None:
    """plan 1 -> reviewer changes the margin the system prompt shows -> plan 2: one base config
    could replay only one of them; the taped cycle config replays both."""
    fake = FakeProvider(lambda m, t, n: review_text([{"path": MARGIN, "value": 0.002, "rationale": "x"}])
                        if is_reviewer(m) else plan_text())
    cfg = ai_cfg(tmp_path, **{"ai.analyst_tools_enabled": False})
    rig = await ai_rig(tmp_path, cfg=cfg, providers={FAKE_PROVIDER: fake})
    eng = rig.engine
    await rig.tick()
    assert await eng.slow_step(rig.clock.now()) is not None
    await rig.tick(dt=60)
    assert (await eng.review_cycle(rig.clock.now())).change.accepted
    await rig.tick(dt=300)
    assert await eng.slow_step(rig.clock.now()) is not None
    rig.close()
    hashes = [r["config_hash"] for r in plan_records(rig)]
    assert hashes[0] == cfg.config_hash != hashes[1]

    report = await replay_recorded_plans(cfg, rig.logs)  # the base config, as the CLI loads it
    assert report.ok and report.reproduced == 2, report.summary()
    assert [r.config_source for r in report.results] == ["tape", "tape"]
    assert [r.config_hash_replayed for r in report.results] == hashes

    # an older tape without the cycle config: the mismatch is named, not an opaque miss
    for entry in (rig.logs / "llm_cache" / "mcp_tape").glob("analyst-cycle-*.json"):
        data = json.loads(entry.read_text())
        data.pop("config")
        entry.write_bytes(canonical_json(data))
    old = await replay_recorded_plans(cfg, rig.logs)
    assert old.results[0].reproduced and not old.results[1].reproduced
    assert old.results[1].error.startswith(f"config mismatch: recorded {hashes[1]}, replay {cfg.config_hash}")


def test_a_taped_config_round_trips_to_the_same_hash(cfg: Config) -> None:
    assert Config.model_validate(cfg.model_dump(mode="json")).config_hash == cfg.config_hash
