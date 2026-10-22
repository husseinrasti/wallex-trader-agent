"""Regime Analyst (PRD-04 §6): schema validation, clamping, the tool loop, MCP fallback, JSON retry,
determinism, cache replay, triggers and staleness.

The model is a scripted fake provider driven through the REAL ``LLMClient`` + ``LLMCache`` (cache in
``tmp_path``); the Wallex MCP server is an ``httpx.MockTransport``. Nothing touches the network or the
wall clock.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from wta.ai.analyst import (
    FIX_JSON,
    FORCE_FINAL,
    MCP_RETRY_S,
    RegimeAnalyst,
    compact_features,
    plan_is_fresh,
    render_template,
    should_run,
)
from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, ProviderError, Route, ToolCall
from wta.ai.mcp_client import PROTOCOL_VERSION, MCPClient
from wta.ai.plan_schema import (
    MISSING_EVIDENCE,
    PLAN_EXAMPLE,
    PLAN_JSON_SCHEMA,
    REQUIRED_SYMBOLS,
    PlanOutput,
    clamp_plan,
    configured_enabled,
)
from wta.ai.tools import INTERNAL_TOOL_SCHEMAS, LOCAL_TOOL_SCHEMAS, InternalTools, ToolDeps
from wta.config import Config, apply_changes, canonical_json
from wta.features.engine import FeatureSnapshot
from wta.logging_.decision_log import DecisionLog
from wta.models import MarketPlan, RegimeCall
from wta.utils.timeutil import SimClock

from .conftest import T0, _make_book

D = Decimal
INTERNAL_NAMES = {s["name"] for s in INTERNAL_TOOL_SCHEMAS}
LOCAL_NAMES = {s["name"] for s in LOCAL_TOOL_SCHEMAS}
MCP_URL = "https://mcp.test/mcp"
MCP_KEY = "".join(["mcp", "-test-", "q" * 6, "7" * 6])  # built at runtime: no key-like literal in source


# ---------------------------------------------------------------- fakes


def plan_json(**over: Any) -> str:
    return json.dumps({**copy.deepcopy(PLAN_EXAMPLE), **over})


class FakeProvider:
    """A scripted model. ``script(messages, tools, n)`` returns text, a list of ToolCalls, an
    ``LLMResponse`` or raises; every call is recorded (deep copies of messages and tools)."""

    name = "fake"

    def __init__(self, script: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None, int], Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
                           "temperature": temperature, "model": model})
        out = self.script(messages, tools, len(self.calls))
        if isinstance(out, LLMResponse):
            return out
        if isinstance(out, list):
            return LLMResponse(text="", tool_calls=out, model=model, provider=self.name, latency_ms=7,
                               usage={"prompt_tokens": 100, "completion_tokens": 10})
        return LLMResponse(text=str(out), model=model, provider=self.name, latency_ms=11,
                           usage={"prompt_tokens": 120, "completion_tokens": 80})


def always(text: str) -> Callable[..., Any]:
    return lambda messages, tools, n: text


def no_network(messages: Any, tools: Any, n: int) -> Any:
    raise AssertionError("the provider must not be called (expected a cache hit)")


def make_llm(cache_dir: Path, provider: FakeProvider | None = None, *, mode: str = "shadow",
             strict: bool = False) -> LLMClient:
    prov = provider or FakeProvider(no_network)
    return LLMClient({"fake": prov}, Route("fake", "fake-model"), None, LLMCache(cache_dir, strict=strict),
                     mode=mode, clock=SimClock(T0))


FEATURES = FeatureSnapshot(
    ts=T0,
    symbols={
        "USDTTMN": {"mid": D("101250"), "spread_pct": D("0.0009"), "ret_15m": D("0.0012"),
                    "ret_1h": D("0.002"), "atr_15m_pct": D("0.0015"), "ema_gap_pct": D("0.0004"),
                    "trend_flag": 0, "hh20_15m": None},
        "BTCUSDT": {"mid": D("82877.08"), "spread_pct": D("0.0007"), "ret_15m": D("-0.004"),
                    "ret_1h": D("0.009"), "trend_flag": 1, "bars_15m": 96},
        "ETHUSDT": {"mid": D("3011.5"), "ret_15m": D("0.0005")},
        "SOLUSDT": {"mid": D("151.2"), "ret_15m": D("0.0101")},
    },
    cross={"BTC": {"dev_mid_pct": D("0.0011"), "dev_zscore": D("1.2"), "edge_tmn_cheap_pct": D("-0.002"),
                   "u_bid": D("101200")}},
    dollar={"mid": D("101250")},
)
PNL = {"equity_tmn": "5012345", "drawdown_pct": "0.001", "daily_pnl_tmn": "12345", "open_positions": []}
HEALTH = {"data_fresh": True, "api_errors_1m": 0, "paused": False, "killed": False, "flatten_phase": 0}


def make_tools(cfg: Config, clock: SimClock) -> InternalTools:
    books = {"BTCUSDT": _make_book("BTCUSDT", "82845.00", "82909.16", qty="0.5", ts=T0, levels=3)}
    deps = ToolDeps(
        cfg=cfg,
        now=clock.now,
        features=lambda: FEATURES,
        books=lambda: books,
        candles_1m=lambda symbol, a, b: [],
        trades=lambda symbol, a, b: [],
        pnl_summary=lambda: dict(PNL),
        recent_decisions=lambda n: [{"decision_id": f"d{i}", "kind": "intent"} for i in range(min(n, 3))],
        health=lambda: dict(HEALTH),
    )
    return InternalTools(deps)


def tuned(cfg: Config, **changes: Any) -> Config:
    return apply_changes(cfg, changes) if changes else cfg


def make_analyst(cfg: Config, llm: LLMClient | None, *, mcp: MCPClient | None = None,
                 on_plan: Callable[[MarketPlan, dict[str, Any]], None] | None = None,
                 clock: SimClock | None = None) -> RegimeAnalyst:
    clock = clock or SimClock(T0)
    return RegimeAnalyst(cfg, llm, make_tools(cfg, clock), mcp, clock, on_plan=on_plan)


STATE: dict[str, Any] = {"events": [{"kind": "start", "reason": "engine started"}]}


# ---------------------------------------------------------------- MCP mock server


class MCPServer:
    """Minimal streamable-HTTP MCP server for ``httpx.MockTransport``."""

    def __init__(self, tools: list[dict[str, Any]], call: Callable[[dict[str, Any]], httpx.Response] | None = None,
                 init_error: Exception | None = None) -> None:
        self.tools = tools
        self.call = call
        self.init_error = init_error
        self.methods: list[str] = []
        self.called: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        self.methods.append(msg["method"])
        if msg["method"] == "initialize" and self.init_error is not None:
            raise self.init_error
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            result: Any = {"protocolVersion": PROTOCOL_VERSION, "serverInfo": {"name": "wallex"}, "capabilities": {}}
        elif msg["method"] == "tools/list":
            result = {"tools": self.tools}
        elif msg["method"] == "tools/call":
            self.called.append(msg["params"])
            if self.call is not None:
                return self.call(msg)
            depth = {"symbol": msg["params"]["arguments"].get("symbol"), "bid": [["82845.00", "0.5"]],
                     "ask": [["82909.16", "0.4"]]}
            result = {"content": [{"type": "text", "text": json.dumps(depth)}], "isError": False}
        else:
            return httpx.Response(400)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result},
                              headers={"Mcp-Session-Id": "s1"})


MCP_TOOLS = [
    {"name": "wallex_basic_get_market_depth", "description": "Order book for a market.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "wallex_basic_candle_history", "description": "Candles.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                      "limit": {"type": "number", "maximum": 0.5e3}}}},
    {"name": "wallex_spot_post_account_orders", "description": "Place an order.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wallex_margin_open_position", "description": "Margin.", "inputSchema": {"type": "object"}},
]


def mcp_client(server: MCPServer) -> MCPClient:
    return MCPClient(MCP_URL, MCP_KEY, transport=httpx.MockTransport(server))


def tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {t["name"] for t in tools or []}


# ---------------------------------------------------------------- schema + clamping


def test_plan_json_schema_is_strict_and_example_validates(cfg: Config) -> None:
    assert PLAN_JSON_SCHEMA["additionalProperties"] is False
    assert PLAN_JSON_SCHEMA["properties"]["regimes"]["required"] == list(REQUIRED_SYMBOLS)
    assert set(PLAN_JSON_SCHEMA["required"]) == set(PlanOutput.model_fields)
    out = PlanOutput.model_validate(PLAN_EXAMPLE)
    assert out.size_multiplier == 0.8
    for bad in ({"bogus": 1}, {"size_multiplier": True}, {"size_multiplier": float("nan")}):
        with pytest.raises(ValueError):
            PlanOutput.model_validate({**PLAN_EXAMPLE, **bad})
    missing = {k: v for k, v in PLAN_EXAMPLE.items() if k != "notes"}
    with pytest.raises(ValueError):
        PlanOutput.model_validate(missing)
    bad_regime = copy.deepcopy(PLAN_EXAMPLE)
    bad_regime["regimes"]["BTCUSDT"]["regime"] = "moon"
    with pytest.raises(ValueError):
        PlanOutput.model_validate(bad_regime)


def test_clamp_plan_in_bounds_records_nothing(cfg: Config) -> None:
    out = PlanOutput.model_validate(PLAN_EXAMPLE)
    plan = clamp_plan(out, cfg, configured_enabled(cfg), T0, {"model": "m", "prompt_hash": "h", "cached": True,
                                                              "tools_used": ["b", "a", "a"], "tools_source": "local",
                                                              "latency_ms": 12})
    assert plan.clamped == {}
    assert plan.s1_margin_min_pct == D("0.0015") and isinstance(plan.s1_margin_min_pct, Decimal)
    assert str(plan.size_multiplier) == "0.8" and str(plan.usdt_target_share) == "0.5"
    assert plan.enabled_strategies == ["S1", "S2"]
    assert list(plan.regimes) == list(REQUIRED_SYMBOLS)
    assert plan.regimes["BTCUSDT"] == RegimeCall(regime="trend_up", confidence=0.7,
                                                 evidence=PLAN_EXAMPLE["regimes"]["BTCUSDT"]["evidence"])
    assert (plan.ts, plan.model, plan.prompt_hash, plan.cached) == (T0, "m", "h", True)
    assert plan.tools_used == ["a", "b"] and plan.tools_source == "local" and plan.latency_ms == 12


def test_clamp_plan_lower_bounds_and_integers(cfg: Config) -> None:
    out = PlanOutput.model_validate({**PLAN_EXAMPLE, "s1_margin_min_pct": 0.0001, "size_multiplier": -2,
                                     "usdt_target_share": 0.1})
    plan = clamp_plan(out, cfg, None, T0)
    assert plan.s1_margin_min_pct == D("0.0008")
    assert plan.size_multiplier == D("0") and plan.usdt_target_share == D("0.25")
    assert plan.clamped == {"s1_margin_min_pct": 0.0001, "size_multiplier": -2.0, "usdt_target_share": 0.1}
    one = clamp_plan(PlanOutput.model_validate({**PLAN_EXAMPLE, "size_multiplier": 1}), cfg, None, T0)
    assert str(one.size_multiplier) == "1" and one.clamped == {}


def test_enabled_strategies_filtered_to_configured(cfg: Config) -> None:
    assert configured_enabled(cfg) == ["S1", "S2"]  # S3 is disabled in configs/default.yaml
    out = PlanOutput.model_validate({**PLAN_EXAMPLE, "enabled_strategies": ["S3", "s2", "S9", "S1"]})
    plan = clamp_plan(out, cfg, configured_enabled(cfg), T0)
    assert plan.enabled_strategies == ["S1", "S2"]
    assert plan.clamped == {"enabled_strategies": ["S3", "s2", "S9", "S1"]}

    with_s3 = tuned(cfg, **{"strategies.s3_momentum.enabled": True, "strategies.s1_crossbook.enabled": False})
    plan = clamp_plan(out, with_s3, configured_enabled(with_s3), T0)
    assert plan.enabled_strategies == ["S2", "S3"]
    # only order/case/duplicates changed: same set, nothing recorded
    same = PlanOutput.model_validate({**PLAN_EXAMPLE, "enabled_strategies": ["s2", "S1", "S1"]})
    plan = clamp_plan(same, cfg, configured_enabled(cfg), T0)
    assert plan.enabled_strategies == ["S1", "S2"] and plan.clamped == {}


# ---------------------------------------------------------------- run: parse + clamp


async def test_run_parses_and_clamps_out_of_bounds_values(cfg: Config, tmp_path: Path) -> None:
    cfg = tuned(cfg, **{"ai.analyst_tools_enabled": False})
    regimes = copy.deepcopy(PLAN_EXAMPLE["regimes"])
    regimes["BTCUSDT"]["confidence"] = 1.4
    regimes["ETHUSDT"]["confidence"] = -0.3
    del regimes["SOLUSDT"]
    regimes["XRPUSDT"] = {"regime": "chaos", "confidence": 0.9, "evidence": "not asked"}
    text = "Here is the plan:\n```json\n" + plan_json(
        regimes=regimes, s1_margin_min_pct=0.5, size_multiplier=1.7, usdt_target_share=0.9,
        enabled_strategies=["S1", "S2", "S3", "S9"]) + "\n```"
    provider = FakeProvider(always(text))
    seen: list[tuple[MarketPlan, dict[str, Any]]] = []
    analyst = make_analyst(cfg, make_llm(tmp_path / "cache", provider), on_plan=lambda p, r: seen.append((p, r)))

    plan = await analyst.run(STATE, reason="scheduled")

    assert plan is not None, analyst.last_error
    assert len(provider.calls) == 1 and provider.calls[0]["tools"] is None
    assert provider.calls[0]["temperature"] == 0.0
    assert plan.s1_margin_min_pct == D("0.01")
    assert plan.size_multiplier == D("1")
    assert plan.usdt_target_share == D("0.70")
    assert plan.enabled_strategies == ["S1", "S2"]
    assert plan.regimes["BTCUSDT"].confidence == 1.0
    assert plan.regimes["ETHUSDT"].confidence == 0.0
    assert list(plan.regimes) == list(REQUIRED_SYMBOLS)  # XRPUSDT dropped
    assert plan.regimes["SOLUSDT"] == RegimeCall(regime="range", confidence=0.0, evidence=MISSING_EVIDENCE)
    assert plan.clamped == {
        "s1_margin_min_pct": 0.5,
        "size_multiplier": 1.7,
        "usdt_target_share": 0.9,
        "enabled_strategies": ["S1", "S2", "S3", "S9"],
        "regimes.BTCUSDT.confidence": 1.4,
        "regimes.ETHUSDT.confidence": -0.3,
        "regimes.dropped": ["XRPUSDT"],
        "regimes.missing": ["SOLUSDT"],
    }
    assert plan.tools_source == "none" and plan.tools_used == []
    assert plan.model == "fake-model" and plan.cached is False and plan.latency_ms == 11
    assert plan.ts == T0 and len(plan.prompt_hash) == 64
    assert analyst.last_plan == plan

    # on_plan gets the plan and the audit record; the record fits a decisions.jsonl "plan" line
    assert len(seen) == 1 and seen[0][0] is plan
    record = seen[0][1]
    for key in ("prompt_hash", "model", "cached", "tools_used", "latency_ms", "notes", "clamped"):
        assert key in record
    assert record["prompt_hash"] == plan.prompt_hash and record["clamped"] == plan.clamped
    assert record["notes"] == plan.notes and record["reason"] == "scheduled"
    dlog = DecisionLog(tmp_path / "logs", "shadow", "abc1234", cfg.config_hash, SimClock(T0), rotate_daily=False)
    dlog.record("plan", "plan-1", strategy="AI", symbol="", features=record, plan_snapshot=plan, notes=plan.notes)
    dlog.close()
    line = json.loads((tmp_path / "logs" / "decisions.jsonl").read_text().splitlines()[0])
    assert line["kind"] == "plan" and line["plan_snapshot"]["prompt_hash"] == plan.prompt_hash
    assert line["plan_snapshot"]["s1_margin_min_pct"] == "0.01"


async def test_ai_disabled_or_no_llm_returns_none(cfg: Config, tmp_path: Path) -> None:
    analyst = make_analyst(cfg, None)
    assert await analyst.run(STATE) is None
    assert analyst.last_error == "llm not configured"

    off = tuned(cfg, **{"ai.enabled": False})
    provider = FakeProvider(always(plan_json()))
    analyst = make_analyst(off, make_llm(tmp_path / "c", provider))
    assert await analyst.run(STATE) is None
    assert "ai.enabled" in analyst.last_error and provider.calls == []


async def test_provider_failure_returns_none(cfg: Config, tmp_path: Path) -> None:
    def boom(messages: Any, tools: Any, n: int) -> Any:
        raise ProviderError("fake: HTTP 503")

    calls: list[Any] = []
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", FakeProvider(boom)), on_plan=lambda p, r: calls.append(p))
    assert await analyst.run(STATE) is None
    assert analyst.last_error.startswith("llm unavailable") and "503" in analyst.last_error
    assert calls == [] and analyst.last_plan is None


async def test_invalid_json_then_fixed_on_retry(cfg: Config, tmp_path: Path) -> None:
    cfg = tuned(cfg, **{"ai.analyst_tools_enabled": False})
    provider = FakeProvider(lambda m, t, n: "I think the market is calm." if n == 1 else plan_json())
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider))
    plan = await analyst.run(STATE)
    assert plan is not None
    assert len(provider.calls) == 2
    retry_msgs = provider.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "I think the market is calm."}
    assert retry_msgs[-1]["role"] == "user" and retry_msgs[-1]["content"].startswith("Your JSON was invalid:")
    assert retry_msgs[-1]["content"].endswith(FIX_JSON.split("\n")[1])
    assert analyst.last_record is not None and analyst.last_record["json_retry"] is True
    assert plan.latency_ms == 22  # both calls counted
    assert plan.prompt_hash == provider_key(provider, 0, tmp_path / "c")


def provider_key(provider: FakeProvider, i: int, cache_dir: Path) -> str:
    """Cache key of the provider's i-th call, recomputed independently of the analyst."""
    from wta.ai.cache import cache_key

    call = provider.calls[i]
    return cache_key("fake", "fake-model", call["messages"], call["tools"], 0.0)


async def test_invalid_twice_returns_none(cfg: Config, tmp_path: Path) -> None:
    cfg = tuned(cfg, **{"ai.analyst_tools_enabled": False})
    provider = FakeProvider(always(plan_json(bogus_key=1)))
    seen: list[Any] = []
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider), on_plan=lambda p, r: seen.append(p))
    assert await analyst.run(STATE) is None
    assert len(provider.calls) == 2
    assert analyst.last_error.startswith("llm_invalid:") and "bogus_key" in analyst.last_error
    assert seen == []


# ---------------------------------------------------------------- tool loop


async def test_tool_loop_executes_tools_and_stops_at_cap(cfg: Config, tmp_path: Path) -> None:
    cfg = tuned(cfg, **{"ai.analyst_max_tool_rounds": 2})

    def script(messages: Any, tools: Any, n: int) -> Any:
        if tools:  # a greedy model: always wants more data while tools are offered
            return [ToolCall(f"c{n}a", "get_features", {"symbols": ["BTCUSDT"]}),
                    ToolCall(f"c{n}b", "get_health", {})]
        return plan_json()

    provider = FakeProvider(script)
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider))
    plan = await analyst.run(STATE)

    assert plan is not None, analyst.last_error
    assert len(provider.calls) == 3  # 2 tool rounds + 1 forced final answer
    assert tool_names(provider.calls[0]["tools"]) == INTERNAL_NAMES | LOCAL_NAMES
    assert provider.calls[1]["tools"] == provider.calls[0]["tools"]
    assert provider.calls[2]["tools"] is None
    final_msgs = provider.calls[2]["messages"]
    assert final_msgs[-1] == {"role": "user", "content": FORCE_FINAL.format(rounds=2)}

    # OpenAI tool-call format: assistant tool_calls, then one role=tool message per call
    msgs = provider.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assistant = msgs[2]
    assert assistant["content"] is None
    assert assistant["tool_calls"][0] == {"id": "c1a", "type": "function",
                                          "function": {"name": "get_features", "arguments": '{"symbols":["BTCUSDT"]}'}}
    tool_msg = msgs[4]
    assert tool_msg["tool_call_id"] == "c1b" and tool_msg["name"] == "get_health"
    assert json.loads(tool_msg["content"]) == HEALTH
    features = json.loads(msgs[3]["content"])
    assert list(features["symbols"]) == ["BTCUSDT"] and features["symbols"]["BTCUSDT"]["mid"] == "82877.08"

    assert plan.tools_used == ["get_features", "get_health"]
    assert plan.tools_source == "local"
    assert plan.latency_ms == 7 + 7 + 11
    assert analyst.last_record is not None and analyst.last_record["tool_rounds"] == 2
    assert analyst.last_record["usage"] == {"prompt_tokens": 320, "completion_tokens": 100}


async def test_tool_loop_ends_when_model_answers(cfg: Config, tmp_path: Path) -> None:
    def script(messages: Any, tools: Any, n: int) -> Any:
        if n == 1:
            return [ToolCall("x1", "wallex_basic_get_market_depth_local", {"symbol": "BTCUSDT"}),
                    ToolCall("x2", "wallex_spot_post_account_orders", {"symbol": "BTCUSDT"}),
                    ToolCall("x3", "get_recent_decisions", {"n": 2})]
        return plan_json()

    provider = FakeProvider(script)
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider))
    plan = await analyst.run(STATE)
    assert plan is not None
    assert len(provider.calls) == 2 and provider.calls[1]["tools"] is not None  # no forced final
    results = {m["tool_call_id"]: json.loads(m["content"]) for m in provider.calls[1]["messages"] if m["role"] == "tool"}
    assert results["x1"]["bid"][0] == ["82845.00", "0.5"]
    assert results["x2"] == {"error": "unknown tool 'wallex_spot_post_account_orders'"}  # never executed
    assert results["x3"] == {"decisions": [{"decision_id": "d0", "kind": "intent"},
                                           {"decision_id": "d1", "kind": "intent"}]}
    assert plan.tools_used == ["get_recent_decisions", "wallex_basic_get_market_depth_local"]


async def test_tools_disabled_or_zero_rounds_means_single_call(cfg: Config, tmp_path: Path) -> None:
    for i, change in enumerate(({"ai.analyst_tools_enabled": False}, {"ai.analyst_max_tool_rounds": 0})):
        provider = FakeProvider(always(plan_json()))
        server = MCPServer(MCP_TOOLS)
        analyst = make_analyst(tuned(cfg, **change), make_llm(tmp_path / f"c{i}", provider), mcp=mcp_client(server))
        plan = await analyst.run(STATE)
        assert plan is not None and plan.tools_source == "none"
        assert [c["tools"] for c in provider.calls] == [None]
        assert server.methods == []  # MCP is not even initialized


# ---------------------------------------------------------------- Wallex MCP


async def test_mcp_tools_offered_and_called(cfg: Config, tmp_path: Path) -> None:
    def script(messages: Any, tools: Any, n: int) -> Any:
        if n == 1:
            return [ToolCall("m1", "wallex_basic_get_market_depth", {"symbol": "BTCUSDT"}),
                    ToolCall("m2", "wallex_spot_post_account_orders", {"side": "BUY"})]
        return plan_json()

    provider = FakeProvider(script)
    server = MCPServer(MCP_TOOLS)
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider), mcp=mcp_client(server))
    plan = await analyst.run(STATE)

    assert plan is not None, analyst.last_error
    offered = provider.calls[0]["tools"]
    assert tool_names(offered) == INTERNAL_NAMES | {"wallex_basic_get_market_depth", "wallex_basic_candle_history"}
    candle = next(t for t in offered if t["name"] == "wallex_basic_candle_history")
    assert candle["parameters"]["properties"]["limit"]["maximum"] == 500  # Decimal -> plain JSON number
    json.dumps(offered)  # provider-serialisable (no Decimals)
    assert [p["name"] for p in server.called] == ["wallex_basic_get_market_depth"]  # state-changing never sent
    results = {m["tool_call_id"]: json.loads(m["content"]) for m in provider.calls[1]["messages"] if m["role"] == "tool"}
    assert results["m1"]["symbol"] == "BTCUSDT" and results["m1"]["bid"] == [["82845.00", "0.5"]]
    assert "error" in results["m2"]
    assert plan.tools_source == "mcp" and plan.tools_used == ["wallex_basic_get_market_depth"]
    assert MCP_KEY not in (tmp_path / "c").joinpath(f"{plan.prompt_hash}.json").read_text()

    # the session is reused on the next cycle (no second initialize)
    analyst.clock.advance(300)  # type: ignore[attr-defined]
    await analyst.run(STATE)
    assert server.methods.count("initialize") == 1 and server.methods.count("tools/list") == 1


async def test_mcp_unreachable_falls_back_to_local_tools(cfg: Config, tmp_path: Path) -> None:
    provider = FakeProvider(lambda m, t, n: [ToolCall("l1", "wallex_basic_get_market_depth_local",
                                                      {"symbol": "BTCUSDT"})] if n == 1 else plan_json())
    server = MCPServer(MCP_TOOLS, init_error=httpx.ConnectError("connection refused"))
    clock = SimClock(T0)
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider), mcp=mcp_client(server), clock=clock)
    plan = await analyst.run(STATE)

    assert plan is not None, analyst.last_error
    assert tool_names(provider.calls[0]["tools"]) == INTERNAL_NAMES | LOCAL_NAMES
    assert plan.tools_source == "local" and plan.tools_used == ["wallex_basic_get_market_depth_local"]
    assert "initialize failed" in analyst.mcp_error and MCP_KEY not in analyst.mcp_error

    # no retry storm: the next cycle inside MCP_RETRY_S does not touch the server again
    clock.advance(300)
    await analyst.run(STATE)
    assert server.methods.count("initialize") == 1
    clock.advance(MCP_RETRY_S)
    await analyst.run(STATE)
    assert server.methods.count("initialize") == 2


async def test_mcp_call_failure_mid_loop_switches_to_local(cfg: Config, tmp_path: Path) -> None:
    def script(messages: Any, tools: Any, n: int) -> Any:
        if n == 1:
            return [ToolCall("m1", "wallex_basic_get_market_depth", {"symbol": "BTCUSDT"})]
        if n == 2:
            return [ToolCall("l1", "wallex_basic_get_market_depth_local", {"symbol": "BTCUSDT"})]
        return plan_json()

    provider = FakeProvider(script)
    server = MCPServer(MCP_TOOLS, call=lambda msg: httpx.Response(502, text="bad gateway"))
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", provider), mcp=mcp_client(server))
    plan = await analyst.run(STATE)

    assert plan is not None, analyst.last_error
    err = json.loads(provider.calls[1]["messages"][-1]["content"])
    assert "Wallex MCP unavailable" in err["error"] and "_local" in err["error"]
    assert tool_names(provider.calls[1]["tools"]) == INTERNAL_NAMES | LOCAL_NAMES
    assert plan.tools_source == "local"
    assert plan.tools_used == ["wallex_basic_get_market_depth", "wallex_basic_get_market_depth_local"]


# ---------------------------------------------------------------- determinism + replay


def _split_user(content: str) -> dict[str, Any]:
    body = content.split("\n", 1)[1].rsplit("\n\n", 1)[0]
    parsed = json.loads(body)
    assert canonical_json(parsed).decode() == body  # canonical: sorted keys, no whitespace
    return parsed


def test_first_user_message_is_deterministic(cfg: Config, tmp_path: Path) -> None:
    a = make_analyst(cfg, None)
    b = make_analyst(cfg, None)
    m1 = a.user_message(STATE, "scheduled", T0)
    assert m1 == b.user_message(copy.deepcopy(STATE), "scheduled", T0) == a.user_message(STATE, "scheduled", T0)
    assert a.system_prompt(T0, STATE) == b.system_prompt(T0, STATE)

    data = _split_user(m1)
    assert set(data) == {"reason", "now_utc", "time_to_deadline_s", "features", "pnl_summary", "health",
                         "last_plan", "recent_events"}
    assert data["now_utc"] == "2026-10-22T10:00:00.000Z" and data["last_plan"] is None
    assert data["pnl_summary"] == PNL and data["health"] == HEALTH
    btc = data["features"]["symbols"]["BTCUSDT"]
    assert btc == {"mid": "82877.08", "spread_pct": "0.0007", "ret_15m": "-0.004", "ret_1h": "0.009",
                   "trend_flag": 1}  # compact: only the selected keys, None dropped
    assert data["features"]["cross"]["BTC"] == {"dev_mid_pct": "0.0011", "dev_zscore": "1.2",
                                                "edge_tmn_cheap_pct": "-0.002"}

    assert a.user_message(STATE, "move:USDTTMN", T0) != m1
    assert a.user_message(STATE, "scheduled", T0 + 1) != m1
    plan = clamp_plan(PlanOutput.model_validate(PLAN_EXAMPLE), cfg, None, T0 - 300)
    with_plan = _split_user(a.user_message({**STATE, "last_plan": plan}, "scheduled", T0))
    assert with_plan["last_plan"]["age_s"] == 300 and with_plan["last_plan"]["size_multiplier"] == "0.8"
    assert with_plan["last_plan"]["regimes"]["BTCUSDT"] == {"regime": "trend_up", "confidence": 0.7}
    # a plan read back from the store as a dict gives the same block
    again = _split_user(a.user_message({**STATE, "last_plan": plan.model_dump(mode="json")}, "scheduled", T0))
    assert again["last_plan"] == with_plan["last_plan"]


def test_system_prompt_renders_config_values(cfg: Config) -> None:
    cfg = tuned(cfg, **{"engine.challenge_end_utc": "2026-10-25T14:30:00Z"})
    text = make_analyst(cfg, None).system_prompt(T0, {})
    assert "3d 04h 30m (ends 2026-10-25T14:30Z)" in text
    for name in ("time_to_deadline", "s1_coins", "s1_margin", "s2_lo", "s2_hi", "s2_threshold", "s2_defaults",
                 "s3_status", "s3_conf", "enabled", "schema"):
        assert "{" + name + "}" not in text
    assert "BTC, ETH, SOL" in text and "(bounds 0.25–0.7)" in text and '["S1", "S2"]' in text
    assert "Current config margin: 0.0015 (bounds 0.0008–0.01)" in text and "≥ 0.15 away" in text
    assert "range 0.5, trend_up 0.65, trend_down 0.3, chaos 0.4" in text
    assert "currently disabled" in text and "confidence ≥ 0.6" in text  # S3
    # the exact schema, then a valid example in its own fence
    fences = text.split("```json\n")
    schema = json.loads(fences[1].split("\n```", 1)[0])
    assert schema["additionalProperties"] is False and schema["properties"]["usdt_target_share"]["maximum"] == 0.7
    assert PlanOutput.model_validate(json.loads(fences[2].split("\n```", 1)[0])) is not None
    no_end = make_analyst(tuned(cfg, **{"engine.challenge_end_utc": None}), None).system_prompt(T0, {})
    assert "unknown (challenge end not configured)" in no_end


def test_render_template_keeps_literal_json_braces() -> None:
    tpl = 'list of {"path", "value", "rationale"} — at most {max_changes}; {unknown} stays; {"a": 1}'
    assert render_template(tpl, {"max_changes": "2"}) == (
        'list of {"path", "value", "rationale"} — at most 2; {unknown} stays; {"a": 1}')


def _tool_script(messages: Any, tools: Any, n: int) -> Any:
    if tools and not any(m["role"] == "tool" for m in messages):
        return [ToolCall("t1", "get_candle_stats", {"symbol": "USDTTMN", "resolution": "15"}),
                ToolCall("t2", "get_pnl_summary", {})]
    return plan_json(size_multiplier=1.3)


async def test_second_identical_run_hits_cache_and_replays(cfg: Config, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    provider = FakeProvider(_tool_script)
    first = await make_analyst(cfg, make_llm(cache_dir, provider)).run(STATE, reason="startup")
    assert first is not None and first.cached is False
    assert len(provider.calls) == 2

    second = await make_analyst(cfg, make_llm(cache_dir, provider)).run(STATE, reason="startup")
    assert second is not None
    assert len(provider.calls) == 2  # no new model calls
    assert second.cached is True and second.prompt_hash == first.prompt_hash
    assert second.model_dump(exclude={"cached"}) == first.model_dump(exclude={"cached"})

    # replay mode, strict cache, a provider that must not be called: the same plan
    replay = make_analyst(cfg, make_llm(cache_dir, FakeProvider(no_network), mode="replay", strict=True))
    third = await replay.run(STATE, reason="startup")
    assert third == second
    assert third.clamped == {"size_multiplier": 1.3}


async def test_strict_replay_with_empty_cache_raises(cfg: Config, tmp_path: Path) -> None:
    analyst = make_analyst(cfg, make_llm(tmp_path / "empty", FakeProvider(no_network), mode="replay", strict=True))
    with pytest.raises(CacheMissError):
        await analyst.run(STATE)
    # replay mode never falls through to the network: even a non-strict cache miss is an error
    soft = make_analyst(cfg, make_llm(tmp_path / "empty2", FakeProvider(no_network), mode="replay", strict=False))
    with pytest.raises(CacheMissError):
        await soft.run(STATE)


async def test_mcp_trajectory_replays_offline(cfg: Config, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"

    def script(messages: Any, tools: Any, n: int) -> Any:
        if n == 1:
            return [ToolCall("m1", "wallex_basic_get_market_depth", {"symbol": "USDTTMN"})]
        return plan_json()

    server = MCPServer(MCP_TOOLS)
    live = await make_analyst(cfg, make_llm(cache_dir, FakeProvider(script)), mcp=mcp_client(server)).run(STATE)
    assert live is not None and live.tools_source == "mcp"
    calls_before = list(server.methods)

    # replay: no MCP client at all, strict cache, no provider -> byte-identical plan from cache + tape
    replay = make_analyst(cfg, make_llm(cache_dir, FakeProvider(no_network), mode="replay", strict=True))
    again = await replay.run(STATE)
    assert again is not None
    assert again.model_dump(exclude={"cached"}) == live.model_dump(exclude={"cached"})
    assert again.cached is True and again.tools_source == "mcp"
    assert server.methods == calls_before


# ---------------------------------------------------------------- triggers + staleness


def test_triggers(cfg: Config) -> None:
    calm = {"symbols": {"USDTTMN": {"ret_15m": "0.004"}, "BTCUSDT": {"ret_15m": "-0.015"}}}
    assert should_run(cfg, T0, None, calm) == (True, "startup")
    assert should_run(cfg, T0 + 120, T0, calm) == (False, "not_due")
    assert should_run(cfg, T0 + 300, T0, calm) == (True, "scheduled")
    # never more often than every 60 s, whatever the trigger
    assert should_run(cfg, T0 + 59, T0, calm, events=["kill"]) == (False, "min_interval")
    assert should_run(cfg, T0 + 60, T0, calm, events=[{"kind": "kill", "reason": "dd"}]) == (True, "event:kill")
    assert should_run(cfg, T0 + 60, T0, calm, events=[{"kind": "pause"}]) == (True, "event:pause")
    assert should_run(cfg, T0 + 60, T0, calm, events=[{"kind": "flatten", "data": {"kind": "flatten_start"}}]) == (
        True, "event:flatten_start")
    assert should_run(cfg, T0 + 60, T0, calm, events=[{"kind": "flatten", "data": {"trigger": "kill"}}]) == (
        True, "event:flatten_start")
    for quiet in ({"kind": "flatten", "data": {"kind": "flatten_final"}}, {"kind": "start"}, "config_change"):
        assert should_run(cfg, T0 + 60, T0, calm, events=[quiet]) == (False, "not_due")
    # 15-minute moves: USDTTMN > 1 %, BTCUSDT > 2 % (either sign)
    dollar = {"symbols": {"USDTTMN": {"ret_15m": "-0.0125"}}}
    assert should_run(cfg, T0 + 60, T0, dollar) == (True, "move:USDTTMN")
    assert should_run(cfg, T0 + 60, T0, {"symbols": {"USDTTMN": {"ret_15m": "0.01"}}}) == (False, "not_due")
    btc = {"symbols": {"BTCUSDT": {"ret_15m": "0.021"}, "USDTTMN": {"ret_15m": None}}}
    assert should_run(cfg, T0 + 60, T0, btc) == (True, "move:BTCUSDT")
    snap = FeatureSnapshot(ts=T0, symbols={"BTCUSDT": {"ret_15m": D("-0.03")}})
    assert should_run(cfg, T0 + 60, T0, snap) == (True, "move:BTCUSDT")
    assert should_run(cfg, T0 + 60, T0, None) == (False, "not_due")


async def test_move_trigger_fires_once_per_window(cfg: Config, tmp_path: Path) -> None:
    cfg = tuned(cfg, **{"ai.analyst_tools_enabled": False})
    clock = SimClock(T0)
    analyst = make_analyst(cfg, make_llm(tmp_path / "c", FakeProvider(always(plan_json()))), clock=clock)
    moving = {"symbols": {"USDTTMN": {"ret_15m": "0.02"}}}
    ok, reason = analyst.should_run(clock.now(), T0 - 600, moving)
    assert (ok, reason) == (True, "move:USDTTMN")
    await analyst.run(STATE, reason=reason)
    clock.advance(120)
    assert analyst.should_run(clock.now(), analyst.last_run_ts, moving) == (False, "not_due")
    clock.advance(180)  # the regular schedule still applies
    assert analyst.should_run(clock.now(), analyst.last_run_ts, moving) == (True, "scheduled")
    clock.set(T0 + 900)
    assert analyst.should_run(clock.now(), T0 + 600, moving) == (True, "move:USDTTMN")


def test_plan_staleness(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    plan = clamp_plan(PlanOutput.model_validate(PLAN_EXAMPLE), cfg, None, T0)
    assert cfg.ai.plan_stale_s == 1200
    assert plan_is_fresh(plan, T0, cfg) and plan_is_fresh(plan, T0 + 1200, cfg)
    assert not plan_is_fresh(plan, T0 + 1201, cfg)
    assert not plan_is_fresh(None, T0, cfg)

    analyst = make_analyst(cfg, None)
    analyst.last_plan = plan
    assert analyst.fresh_plan(T0 + 600) is plan
    with caplog.at_level(logging.WARNING, logger="wta.ai.analyst"):
        assert analyst.fresh_plan(T0 + 1300) is None
        assert analyst.fresh_plan(T0 + 1400) is None
    assert sum("older than" in r.message for r in caplog.records) == 1  # logged once


def test_compact_features_passes_errors_through() -> None:
    assert compact_features({"error": "features not available yet"}) == {"error": "features not available yet"}
    assert compact_features(None) is None
