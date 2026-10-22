"""LLM client (PRD-04 §2): OpenRouter + Gemini request/response mapping, key hygiene, failover,
timeouts, per-role daily budget, cache/replay and ``structured()`` validation with one retry.

Every provider is a respx mock (or an in-process fake); nothing touches the network or the wall
clock. Fake keys are assembled at runtime so this file never holds a complete key-like literal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from wta.ai.cache import CacheMissError, LLMCache, cache_key
from wta.ai.llm_client import (
    GEMINI_SCHEMA_KEYS,
    GeminiProvider,
    LLMClient,
    LLMResponse,
    OpenRouterProvider,
    ProviderError,
    Route,
    ToolCall,
    extract_json,
    gemini_function,
    gemini_schema,
)
from wta.models import RegimeCall
from wta.utils.timeutil import SimClock

OR_KEY = "sk" + "-or-v1-" + "c0ffee12" * 8
GEM_KEY = "AI" + "za" + "Sy" + "Xk9" * 11
OR_URL = "https://openrouter.test/api/v1"
GEM_URL = "https://gemini.test/v1beta"
OR_CHAT = f"{OR_URL}/chat/completions"
PRIMARY = Route("openrouter", "or-fast")
FALLBACK = Route("gemini", "gem-flash")
GEM_CHAT = f"{GEM_URL}/models/{FALLBACK.model}:generateContent"
T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z

MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "You are the regime analyst. Output JSON only."},
    {"role": "user", "content": "Decide the plan."},
]
TOOL = {"name": "get_candle_stats", "description": "Computed stats for a symbol.",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
                       "additionalProperties": False}}
VALID = {"regime": "range", "confidence": 0.7, "evidence": "flat EMAs"}


def or_ok(content: str | None = "{}", tool_calls: list[dict[str, Any]] | None = None,
          usage: dict[str, Any] | None = None, model: str = "or-fast-2026") -> httpx.Response:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"id": "gen-1", "model": model, "choices": [{"index": 0, "message": msg}],
                                     "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20,
                                                        "total_tokens": 120}})


def gem_ok(parts: list[dict[str, Any]] | None = None, text: str = "{}") -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 15, "totalTokenCount": 95}})


def body(route: respx.Route, i: int = -1) -> dict[str, Any]:
    return json.loads(route.calls[i].request.content)


def make_client(tmp_path: Any, *, mode: str = "shadow", strict: bool = False, fallback: Route | None = FALLBACK,
                budget: dict[str, int] | None = None, clock: SimClock | None = None,
                events: list[dict[str, Any]] | None = None, providers: dict[str, Any] | None = None) -> LLMClient:
    providers = providers or {"openrouter": OpenRouterProvider(OR_KEY, base_url=OR_URL),
                              "gemini": GeminiProvider(GEM_KEY, base_url=GEM_URL)}
    return LLMClient(providers, PRIMARY, fallback, LLMCache(tmp_path / "llm_cache", strict=strict), mode=mode,
                     clock=clock or SimClock(T0), daily_budget=budget,
                     on_call=events.append if events is not None else None)


def assert_no_key(text: str) -> None:
    for secret in (OR_KEY, GEM_KEY):
        assert secret not in text
        assert secret[8:] not in text  # not even most of it


# ---------------------------------------------------------------- OpenRouter


async def test_openrouter_request_body_headers_and_json_mode(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok('{"a": 1}'))
        resp = await client.chat(MESSAGES, max_tokens=700)
    req = route.calls.last.request
    sent = body(route)
    assert sent == {"model": "or-fast", "messages": MESSAGES, "temperature": 0.0, "max_tokens": 700, "seed": 42,
                    "response_format": {"type": "json_object"}}
    assert req.headers["authorization"] == f"Bearer {OR_KEY}"
    assert req.headers["x-title"] == "wallex-trader-agent"
    assert "http-referer" in req.headers
    assert OR_KEY not in str(req.url)
    assert resp is not None
    assert resp.text == '{"a": 1}'
    assert resp.tool_calls == []
    assert resp.usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert (resp.provider, resp.model, resp.cached) == ("openrouter", "or-fast-2026", False)
    assert resp.cache_key == cache_key("openrouter", "or-fast", MESSAGES, None, 0.0)


async def test_openrouter_tools_in_function_format_and_tool_call_parsing() -> None:
    provider = OpenRouterProvider(OR_KEY, base_url=OR_URL)
    calls = [
        {"id": "call_a", "type": "function",
         "function": {"name": "get_candle_stats", "arguments": '{"symbol": "BTCUSDT"}'}},
        {"type": "function", "function": {"name": "get_health", "arguments": ""}},
        {"id": "call_c", "type": "function", "function": {"name": "get_features", "arguments": "{not json"}},
    ]
    with respx.mock(assert_all_called=True) as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok(None, tool_calls=calls))
        resp = await provider.chat(MESSAGES, [TOOL], "or-fast", 0.0, 500, json_mode=True)
    sent = body(route)
    assert sent["tools"] == [{"type": "function", "function": TOOL}]
    assert sent["tool_choice"] == "auto"
    assert "response_format" not in sent  # JSON mode only without tools
    assert sent["temperature"] == 0.0 and sent["seed"] == 42
    assert resp.text == ""
    assert resp.tool_calls == [
        ToolCall("call_a", "get_candle_stats", {"symbol": "BTCUSDT"}),
        ToolCall("call_1", "get_health", {}),
        ToolCall("call_c", "get_features", {"_raw": "{not json"}),
    ]


async def test_openrouter_json_mode_off_sends_no_response_format() -> None:
    provider = OpenRouterProvider(OR_KEY, base_url=OR_URL)
    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok("plain"))
        await provider.chat(MESSAGES, None, "or-fast", 0.0, 100, json_mode=False)
    assert "response_format" not in body(route) and "tools" not in body(route)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": f"Invalid key {OR_KEY}", "code": 401}}),
        httpx.Response(500, text=f"upstream said: Authorization: Bearer {OR_KEY}"),
        httpx.Response(200, json={"error": {"message": f"quota for {OR_KEY} exceeded"}}),
    ],
)
async def test_openrouter_errors_mask_the_key_even_when_echoed(response: httpx.Response) -> None:
    provider = OpenRouterProvider(OR_KEY, base_url=OR_URL)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=response)
        with pytest.raises(ProviderError) as ei:
            await provider.chat(MESSAGES, None, "or-fast", 0.0, 100, json_mode=True)
    msg = str(ei.value)
    assert_no_key(msg)
    assert "***" in msg
    assert ei.value.__cause__ is None


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_openrouter_transport_errors_and_timeouts(exc: Exception) -> None:
    provider = OpenRouterProvider(OR_KEY, base_url=OR_URL, timeout=3)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(side_effect=exc)
        with pytest.raises(ProviderError, match=f"transport error: {type(exc).__name__}") as ei:
            await provider.chat(MESSAGES, None, "or-fast", 0.0, 100, json_mode=True)
    assert_no_key(str(ei.value))
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


async def test_openrouter_malformed_response_and_missing_key() -> None:
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=httpx.Response(200, json={"choices": []}))
        with pytest.raises(ProviderError, match="malformed"):
            await OpenRouterProvider(OR_KEY, base_url=OR_URL).chat(MESSAGES, None, "m", 0.0, 10, True)
        with pytest.raises(ProviderError, match="no API key"):
            await OpenRouterProvider("", base_url=OR_URL).chat(MESSAGES, None, "m", 0.0, 10, True)
        assert router.calls.call_count == 1  # the key-less provider never sent anything


# ---------------------------------------------------------------- Gemini


async def test_gemini_request_mapping_header_key_and_parsing() -> None:
    provider = GeminiProvider(GEM_KEY, base_url=GEM_URL)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Decide."},
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_candle_stats", "arguments": '{"symbol": "BTCUSDT"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "get_candle_stats", "content": '{"atr14_pct": "0.004"}'},
        {"role": "tool", "tool_call_id": "c2", "name": "get_recent_decisions", "content": "[1, 2]"},
        {"role": "tool", "tool_call_id": "c3", "name": "get_health", "content": "not json"},
    ]
    with respx.mock(assert_all_called=True) as router:
        route = router.post(GEM_CHAT).mock(return_value=gem_ok([{"text": '{"a":'}, {"text": " 1}"}]))
        resp = await provider.chat(messages, None, FALLBACK.model, 0.0, 900, json_mode=True)
    req = route.calls.last.request
    # the key travels only in the x-goog-api-key header: never as a ?key= query parameter
    assert req.headers["x-goog-api-key"] == GEM_KEY
    assert req.url.query == b""
    assert GEM_KEY not in str(req.url)
    assert "authorization" not in req.headers
    sent = body(route)
    assert sent["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert sent["generationConfig"] == {"temperature": 0.0, "maxOutputTokens": 900,
                                        "responseMimeType": "application/json"}
    assert "tools" not in sent
    assert sent["contents"] == [
        {"role": "user", "parts": [{"text": "Decide."}]},
        {"role": "model", "parts": [{"text": "Checking."},
                                    {"functionCall": {"name": "get_candle_stats", "args": {"symbol": "BTCUSDT"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "get_candle_stats",
                                                         "response": {"atr14_pct": "0.004"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "get_recent_decisions",
                                                         "response": {"result": [1, 2]}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "get_health", "response": {"result": "not json"}}}]},
    ]
    assert resp.text == '{"a": 1}'
    assert resp.tool_calls == []
    assert resp.usage == {"prompt_tokens": 80, "completion_tokens": 15}
    assert (resp.provider, resp.model) == ("gemini", FALLBACK.model)


async def test_gemini_tools_become_function_declarations_and_calls_are_parsed() -> None:
    provider = GeminiProvider(GEM_KEY, base_url=GEM_URL)
    parts = [{"text": "Let me look."},
             {"functionCall": {"name": "get_candle_stats", "args": {"symbol": "ETHUSDT", "n": 20}}},
             {"functionCall": {"name": "get_health"}}]
    with respx.mock() as router:
        route = router.post(GEM_CHAT).mock(return_value=gem_ok(parts))
        resp = await provider.chat(MESSAGES, [TOOL], FALLBACK.model, 0.0, 500, json_mode=True)
    sent = body(route)
    params = {k: v for k, v in TOOL["parameters"].items() if k != "additionalProperties"}  # Gemini: HTTP 400
    assert sent["tools"] == [{"functionDeclarations": [
        {"name": TOOL["name"], "description": TOOL["description"], "parameters": params}]}]
    assert "responseMimeType" not in sent["generationConfig"]
    assert resp.text == "Let me look."
    assert resp.tool_calls == [ToolCall("get_candle_stats_1", "get_candle_stats", {"symbol": "ETHUSDT", "n": 20}),
                               ToolCall("get_health_2", "get_health", {})]


def _gemini_rejects(decl: dict[str, Any]) -> str | None:
    """What the real Gemini API refused in a shadow run (and documents): unknown schema keywords
    and an OBJECT without properties."""

    def walk(node: Any, where: str) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key not in GEMINI_SCHEMA_KEYS:
                    return f"Unknown name {key!r} at {where}"
                if key == "properties":
                    if not value:
                        return f"{where}.properties: should be non-empty for OBJECT type"
                    for name, sub in value.items():
                        found = walk(sub, f"{where}.properties.{name}")
                        if found:
                            return found
                elif key in ("items",):
                    found = walk(value, f"{where}.{key}")
                    if found:
                        return found
        return None

    return walk(decl.get("parameters", {}), "parameters") if "parameters" in decl else None


def test_every_analyst_tool_is_a_valid_gemini_function_declaration() -> None:
    from wta.ai.analyst import mcp_tool_schema
    from wta.ai.tools import INTERNAL_TOOL_SCHEMAS, LOCAL_TOOL_SCHEMAS

    mcp_like = mcp_tool_schema({"name": "wallex_basic_get_market_depth", "description": "Depth.",
                                "inputSchema": {"type": "object", "$schema": "x", "additionalProperties": False,
                                                "properties": {"symbol": {"type": "string"}},
                                                "required": ["symbol", "ghost"]}})
    for tool in [*INTERNAL_TOOL_SCHEMAS, *LOCAL_TOOL_SCHEMAS, mcp_like]:
        decl = gemini_function(tool)
        assert _gemini_rejects(decl) is None, (tool["name"], decl)
        assert decl["name"] == tool["name"]
    no_args = gemini_function({"name": "get_health", "description": "h",
                               "parameters": {"type": "object", "properties": {}, "additionalProperties": False}})
    assert no_args == {"name": "get_health", "description": "h"}
    assert gemini_function(mcp_like)["parameters"]["required"] == ["symbol"]
    stats = gemini_function(next(t for t in INTERNAL_TOOL_SCHEMAS if t["name"] == "get_candle_stats"))
    assert stats["parameters"]["properties"]["resolution"]["enum"] == ["1", "15", "60"]
    assert gemini_schema({"type": "array", "uniqueItems": True, "items": {"type": "string", "$ref": "#/x"}}) == {
        "type": "array", "items": {"type": "string"}}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": {"message": f"API key not valid: {GEM_KEY}"}}),
        httpx.Response(200, json={"candidates": []}),
    ],
)
async def test_gemini_errors_never_carry_the_key(response: httpx.Response) -> None:
    provider = GeminiProvider(GEM_KEY, base_url=GEM_URL)
    with respx.mock() as router:
        router.post(GEM_CHAT).mock(return_value=response)
        with pytest.raises(ProviderError) as ei:
            await provider.chat(MESSAGES, None, FALLBACK.model, 0.0, 100, json_mode=True)
    assert_no_key(str(ei.value))


# ---------------------------------------------------------------- failover, timeouts


async def test_primary_error_fails_over_to_gemini(tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    events: list[dict[str, Any]] = []
    client = make_client(tmp_path, events=events)
    with caplog.at_level(logging.DEBUG), respx.mock(assert_all_called=True) as router:
        primary = router.post(OR_CHAT).mock(
            return_value=httpx.Response(503, json={"error": {"message": f"overloaded ({OR_KEY})"}}))
        fallback = router.post(GEM_CHAT).mock(return_value=gem_ok(text='{"ok": true}'))
        resp = await client.chat(MESSAGES)
    assert resp is not None and resp.provider == "gemini" and resp.text == '{"ok": true}'
    assert (primary.call_count, fallback.call_count) == (1, 1)
    assert client.consecutive_failures == 0
    assert client.last_error.startswith("openrouter: ProviderError: openrouter HTTP 503")
    assert events == [{"role": "analyst", "provider": "gemini", "model": FALLBACK.model, "cached": False,
                       "latency_ms": resp.latency_ms, "usage": resp.usage, "fallback": True}]
    assert_no_key(client.last_error)
    assert_no_key(caplog.text)
    assert "LLM call failed" in caplog.text


async def test_both_providers_fail_returns_none_and_counts_failures(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=httpx.Response(500, text="down"))
        gem = router.post(GEM_CHAT).mock(return_value=httpx.Response(429, text=f"slow down {GEM_KEY}"))
        assert await client.chat(MESSAGES) is None
        assert client.consecutive_failures == 1
        assert await client.chat([*MESSAGES, {"role": "user", "content": "retry"}]) is None
        assert client.consecutive_failures == 2
        assert client.last_error.startswith("gemini: ProviderError: gemini HTTP 429")
        assert_no_key(client.last_error)
        gem.mock(return_value=gem_ok(text="{}"))
        assert await client.chat(MESSAGES) is not None
    assert client.consecutive_failures == 0
    assert len(client.cache) == 1  # failures are never cached


async def test_explicit_route_has_no_fallback(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock(assert_all_called=False) as router:
        router.post(OR_CHAT).mock(return_value=httpx.Response(500))
        gem = router.post(GEM_CHAT).mock(return_value=gem_ok())
        assert await client.chat(MESSAGES, route=PRIMARY) is None
    assert gem.call_count == 0


class SlowProvider:
    """In-process provider that never answers in time (asyncio-level timeout)."""

    name = "openrouter"

    def __init__(self) -> None:
        self.cancelled = False

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


async def test_timeout_cancels_the_call_and_fails_over(tmp_path: Any) -> None:
    slow = SlowProvider()
    client = make_client(tmp_path, providers={"openrouter": slow, "gemini": GeminiProvider(GEM_KEY, base_url=GEM_URL)})
    with respx.mock() as router:
        router.post(GEM_CHAT).mock(return_value=gem_ok(text="{}"))
        resp = await client.chat(MESSAGES, timeout=0.05)
    assert resp is not None and resp.provider == "gemini"
    assert slow.cancelled
    assert client.last_error == "openrouter: TimeoutError: "


async def test_http_timeout_on_both_routes_returns_none(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(side_effect=httpx.ReadTimeout("read timed out"))
        router.post(GEM_CHAT).mock(side_effect=httpx.ConnectTimeout("connect timed out"))
        assert await client.chat(MESSAGES) is None
    assert client.last_error == "gemini: ProviderError: gemini transport error: ConnectTimeout"


async def test_unconfigured_routes_fail_without_network(tmp_path: Any) -> None:
    client = LLMClient({"openrouter": OpenRouterProvider(OR_KEY, base_url=OR_URL)}, Route("openrouter", ""),
                       Route("gemini", "gem-flash"), LLMCache(tmp_path), clock=SimClock(T0))
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=or_ok())
        assert await client.chat(MESSAGES) is None
        assert router.calls.call_count == 0
    assert client.last_error == "gemini: ProviderError: provider gemini not configured"


# ---------------------------------------------------------------- budget


async def test_daily_budget_is_per_role_and_per_utc_day(tmp_path: Any) -> None:
    clock = SimClock(T0)
    client = make_client(tmp_path, fallback=None, budget={"analyst": 2, "reviewer": 1}, clock=clock)

    def msgs(i: int) -> list[dict[str, Any]]:
        return [*MESSAGES, {"role": "user", "content": f"tick {i}"}]

    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok("{}"))
        assert await client.chat(msgs(1)) is not None
        assert await client.chat(msgs(2)) is not None
        assert await client.chat(msgs(3)) is None  # analyst budget spent
        assert client.last_error == "openrouter: ProviderError: daily analyst call budget exhausted"
        assert route.call_count == 2
        # a cache hit costs no budget
        hit = await client.chat(msgs(1))
        assert hit is not None and hit.cached
        # the reviewer has its own budget
        assert await client.chat(msgs(4), role="reviewer") is not None
        assert await client.chat(msgs(5), role="reviewer") is None
        # roles without a configured limit are unlimited
        assert await client.chat(msgs(6), role="adhoc") is not None
        assert route.call_count == 4
        # the next UTC day resets the counters
        clock.advance(86_400)
        assert await client.chat(msgs(3)) is not None
        assert await client.chat(msgs(5), role="reviewer") is not None
        assert route.call_count == 6


# ---------------------------------------------------------------- cache + replay


async def test_cache_hit_skips_the_network(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    client = make_client(tmp_path, events=events)
    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok('{"x": 1}'))
        first = await client.chat(MESSAGES, [TOOL])
    assert first is not None and not first.cached and route.call_count == 1

    # a fresh client over the same cache directory, with every request answered by an error route
    again = make_client(tmp_path, events=events)
    with respx.mock(assert_all_called=False) as router:
        guard = router.route().mock(return_value=httpx.Response(500))
        second = await again.chat(MESSAGES, [TOOL])
        assert router.calls.call_count == 0
    assert guard.call_count == 0
    assert second is not None and second.cached
    assert (second.text, second.provider, second.model, second.usage) == (first.text, first.provider, first.model,
                                                                         first.usage)
    assert second.cache_key == first.cache_key
    assert [e["cached"] for e in events] == [False, True]
    assert again.cache.hits == 1


async def test_cache_entry_records_the_request_and_the_clock(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=or_ok("{}"))
        resp = await client.chat(MESSAGES, [TOOL], temperature=0.0)
    assert resp is not None
    entry = json.loads(client.cache.path(resp.cache_key).read_text())
    assert entry["request"] == {"provider": "openrouter", "model": "or-fast", "messages": MESSAGES, "tools": [TOOL],
                                "temperature": 0.0}
    assert entry["ts"] == T0
    assert entry["usage"] == resp.usage
    assert entry["response"]["text"] == "{}"


@pytest.mark.parametrize("strict", [True, False])
async def test_replay_miss_is_a_hard_error_with_zero_network(tmp_path: Any, strict: bool) -> None:
    client = make_client(tmp_path, mode="replay", strict=strict)
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=or_ok())
        with pytest.raises(CacheMissError):
            await client.chat(MESSAGES)
        assert router.calls.call_count == 0
    assert client.consecutive_failures == 0


async def test_replay_reproduces_a_failed_over_call_from_the_fallback_entry(tmp_path: Any) -> None:
    """The recorded run failed over to Gemini, so only the Gemini route has a cache entry."""
    recorder = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=httpx.Response(502))
        router.post(GEM_CHAT).mock(return_value=gem_ok(text='{"from": "gemini"}'))
        recorded = await recorder.chat(MESSAGES)
    assert recorded is not None

    events: list[dict[str, Any]] = []
    replay = make_client(tmp_path, mode="replay", strict=True, events=events)
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(500))
        replayed = await replay.chat(MESSAGES)
        assert router.calls.call_count == 0
    assert replayed is not None and replayed.cached
    assert (replayed.provider, replayed.text) == ("gemini", '{"from": "gemini"}')
    assert events[0]["fallback"] is True


async def test_tool_loop_trajectory_replays_offline(tmp_path: Any) -> None:
    async def run(client: LLMClient) -> list[LLMResponse]:
        msgs = list(MESSAGES)
        first = await client.chat(msgs, [TOOL])
        assert first is not None
        call = first.tool_calls[0]
        msgs += [{"role": "assistant", "content": first.text, "tool_calls": [
                     {"id": call.id, "type": "function",
                      "function": {"name": call.name, "arguments": json.dumps(call.arguments, sort_keys=True)}}]},
                 {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": '{"ret_1_bar": "0.001"}'}]
        final = await client.chat(msgs, [TOOL])
        assert final is not None
        return [first, final]

    live = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(side_effect=[
            or_ok(None, tool_calls=[{"id": "call_1", "type": "function",
                                     "function": {"name": "get_candle_stats", "arguments": '{"symbol": "BTCUSDT"}'}}]),
            or_ok('{"done": true}'),
        ])
        recorded = await run(live)
    assert len(live.cache) == 2  # the tool result is part of the second call's key

    replay = make_client(tmp_path, mode="replay", strict=True)
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(500))
        replayed = await run(replay)
        assert router.calls.call_count == 0
    assert [(r.text, r.tool_calls) for r in replayed] == [(r.text, r.tool_calls) for r in recorded]
    assert all(r.cached for r in replayed)


# ---------------------------------------------------------------- structured()


class Pick(BaseModel):
    regime: str
    confidence: float


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(VALID),
        "Here is the plan:\n```json\n" + json.dumps(VALID, indent=2) + "\n```\nThanks.",
        "```\n" + json.dumps(VALID) + "\n```",
        "Sure! " + json.dumps(VALID) + " -- end",
    ],
)
async def test_structured_accepts_plain_and_fenced_json(tmp_path: Any, text: str) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(return_value=or_ok(text))
        obj, resp, err = await client.structured(MESSAGES, RegimeCall)
    assert obj == RegimeCall(**VALID)
    assert resp is not None and resp.text == text
    assert err == ""
    assert route.call_count == 1
    assert body(route)["response_format"] == {"type": "json_object"}


async def test_structured_retries_once_with_the_error_appended(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    bad = '{"regime": "sideways", "confidence": 0.7}'
    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(side_effect=[or_ok(bad), or_ok(json.dumps(VALID))])
        obj, resp, err = await client.structured(MESSAGES, RegimeCall, role="reviewer")
    assert obj == RegimeCall(**VALID) and err == ""
    assert resp is not None and resp.text == json.dumps(VALID)
    assert route.call_count == 2
    retry_msgs = body(route, 1)["messages"]
    assert retry_msgs[:2] == MESSAGES
    assert retry_msgs[2] == {"role": "assistant", "content": bad}
    assert retry_msgs[3]["role"] == "user"
    assert retry_msgs[3]["content"].startswith("Your JSON was invalid: ")
    assert "regime" in retry_msgs[3]["content"]
    assert "Fix the JSON" in retry_msgs[3]["content"]
    assert len(client.cache) == 2  # both attempts are replayable


@pytest.mark.parametrize(
    ("first", "second", "needle"),
    [
        ("not json at all", "still not json", "no JSON object"),
        ('{"regime": "range"}', '{"confidence": 0.5}', "regime"),
        ('{"regime": "range", "confidence": "high"}', "[1, 2]", "no JSON object"),
    ],
)
async def test_structured_invalid_twice_returns_none_and_llm_invalid(tmp_path: Any, first: str, second: str,
                                                                     needle: str) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        route = router.post(OR_CHAT).mock(side_effect=[or_ok(first), or_ok(second)])
        obj, resp, err = await client.structured(MESSAGES, Pick)
    assert obj is None
    assert resp is not None and resp.text == second
    assert err.startswith("llm_invalid: ") and needle in err
    assert route.call_count == 2  # exactly one retry


async def test_structured_when_no_provider_answers(tmp_path: Any) -> None:
    client = make_client(tmp_path)
    with respx.mock() as router:
        router.post(OR_CHAT).mock(return_value=httpx.Response(500))
        router.post(GEM_CHAT).mock(return_value=httpx.Response(500))
        obj, resp, err = await client.structured(MESSAGES, Pick)
    assert (obj, resp) == (None, None)
    assert err.startswith("gemini: ProviderError")


def test_extract_json_variants() -> None:
    assert extract_json('  {"a": {"b": [1, 2]}}  ') == {"a": {"b": [1, 2]}}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('prefix {"a": 1} suffix {"b": 2}') == {"a": 1}
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("nothing here")
    with pytest.raises(ValueError):
        extract_json('{"a": 1')
