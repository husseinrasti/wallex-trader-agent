"""MCP client: JSON-RPC framing (plain JSON and SSE), sessions, errors, the read-only allowlist and
result truncation. Every server is a respx mock; nothing touches the network."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from wta.ai.mcp_client import (
    PROTOCOL_VERSION,
    READ_ONLY_ALLOWLIST,
    MCPClient,
    MCPError,
    MCPToolNotAllowed,
    is_state_changing,
    parse_sse,
    tool_payload,
    truncate_result,
)
from wta.config import canonical_json

URL = "https://mcp.test/mcp"
# built at runtime so the source never holds a key-like literal
FAKE_KEY = "".join(["mcp", "-test-", "k" * 6, "9" * 6])


def _reply(request: httpx.Request, result: Any) -> httpx.Response:
    msg = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})


def _sse(*payloads: Any) -> str:
    return "".join(f"event: message\ndata: {json.dumps(p)}\n\n" for p in payloads)


class FakeServer:
    """A tiny MCP server: records every request; ``handlers`` map a method to a result or a callable."""

    def __init__(self, session_id: str | None = "sess-1") -> None:
        self.session_id = session_id
        self.requests: list[tuple[dict[str, Any], httpx.Headers]] = []
        self.handlers: dict[str, Any] = {
            "initialize": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "wallex-mcp", "version": "1.0"},
                "capabilities": {"tools": {}},
            }
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        self.requests.append((msg, request.headers))
        if "id" not in msg:
            return httpx.Response(202)  # notification accepted
        handler = self.handlers[msg["method"]]
        if callable(handler):
            return handler(request, msg)
        headers = {"Mcp-Session-Id": self.session_id} if msg["method"] == "initialize" and self.session_id else {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": handler}, headers=headers)

    def methods(self) -> list[str]:
        return [m["method"] for m, _ in self.requests]


# ---------------------------------------------------------------- framing + session


async def test_initialize_then_list_tools_with_session_and_headers() -> None:
    server = FakeServer()
    pages = {
        None: {"tools": [{"name": "wallex_basic_get_markets"}], "nextCursor": "p2"},
        "p2": {"tools": [{"name": "wallex_spot_post_account_orders"}]},
    }
    server.handlers["tools/list"] = lambda req, msg: _reply(req, pages[msg["params"].get("cursor")])
    with respx.mock(assert_all_called=True) as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY) as mcp:
            init = await mcp.initialize()
            tools = await mcp.list_tools()

    assert init["serverInfo"]["name"] == "wallex-mcp"
    assert [t["name"] for t in tools] == ["wallex_basic_get_markets", "wallex_spot_post_account_orders"]
    assert server.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    first_msg, first_headers = server.requests[0]
    assert first_msg["params"]["protocolVersion"] == "2025-06-18"
    assert first_msg["params"]["clientInfo"]["name"]
    assert first_msg["jsonrpc"] == "2.0"
    assert "mcp-session-id" not in first_headers and "mcp-protocol-version" not in first_headers
    for _, headers in server.requests:
        assert headers["accept"] == "application/json, text/event-stream"
        assert headers["x-api-key"] == FAKE_KEY
    for _, headers in server.requests[1:]:  # everything after initialize carries the session
        assert headers["mcp-session-id"] == "sess-1"
        assert headers["mcp-protocol-version"] == PROTOCOL_VERSION
    assert mcp.session_id == "sess-1" and "wallex_basic_get_markets" in mcp.tools


async def test_sse_response_picks_the_message_with_the_matching_id() -> None:
    server = FakeServer(session_id=None)

    def call(req: httpx.Request, msg: dict[str, Any]) -> httpx.Response:
        rid = msg["id"]
        text = json.dumps({"bid": "266720", "qty": 1.25})
        body = (
            _sse({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
            + _sse({"jsonrpc": "2.0", "id": rid + 100, "result": {"wrong": True}})
            # the matching response, split over two data lines
            + "data: {\"jsonrpc\": \"2.0\", \"id\": " + str(rid) + ",\n"
            + "data: \"result\": {\"content\": [{\"type\": \"text\", \"text\": " + json.dumps(text) + "}]}}\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    server.handlers["tools/call"] = call
    with respx.mock() as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY, allowlist=READ_ONLY_ALLOWLIST) as mcp:
            result = await mcp.call_tool("wallex_basic_get_market_depth", {"symbol": "USDTTMN"})

    assert server.methods()[0] == "initialize"  # call_tool initialises on demand
    assert "mcp-session-id" not in server.requests[-1][1]  # the server gave no session id
    assert server.requests[-1][0]["params"] == {"name": "wallex_basic_get_market_depth", "arguments": {"symbol": "USDTTMN"}}
    payload = tool_payload(result)
    assert payload == {"bid": "266720", "qty": Decimal("1.25")}
    assert isinstance(payload["qty"], Decimal)


async def test_sse_reply_is_used_without_waiting_for_the_stream_to_close() -> None:
    server = FakeServer()

    def call(req: httpx.Request, msg: dict[str, Any]) -> httpx.Response:
        async def stream():
            yield _sse({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": []}}).encode()
            await asyncio.Event().wait()  # the server keeps the stream open
            yield b""

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    server.handlers["tools/call"] = call
    with respx.mock() as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY) as mcp:
            result = await asyncio.wait_for(mcp.call_tool("wallex_basic_get_markets"), timeout=5)
    assert result == {"content": []}


def test_parse_sse_handles_comments_multiline_data_and_bad_events() -> None:
    text = ": keep-alive\n\ndata: {\"a\":\ndata: 1.5}\n\nevent: x\ndata: not json\n\ndata: [1, 2]"
    assert parse_sse(text) == [{"a": Decimal("1.5")}, [1, 2]]


async def test_jsonrpc_error_raises_mcperror_without_the_key() -> None:
    server = FakeServer()

    def call(req: httpx.Request, msg: dict[str, Any]) -> httpx.Response:
        error = {"code": -32602, "message": f"bad params for key {FAKE_KEY}", "data": {"echo": FAKE_KEY}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": error})

    server.handlers["tools/call"] = call
    with respx.mock() as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY) as mcp:
            with pytest.raises(MCPError) as info:
                await mcp.call_tool("wallex_basic_get_markets", {})
    assert info.value.code == -32602
    assert "bad params" in str(info.value)
    assert FAKE_KEY not in str(info.value) and FAKE_KEY not in json.dumps(info.value.data)


async def test_http_error_and_transport_error_are_mcperrors_without_the_key() -> None:
    with respx.mock() as router:
        router.post(URL).respond(401, text=f"invalid api key {FAKE_KEY}")
        async with MCPClient(URL, FAKE_KEY) as mcp:
            with pytest.raises(MCPError) as info:
                await mcp.initialize()
    assert info.value.code == 401 and "HTTP 401" in str(info.value)
    assert FAKE_KEY not in str(info.value)

    with respx.mock() as router:
        router.post(URL).mock(side_effect=httpx.ConnectError("connection refused"))
        async with MCPClient(URL, FAKE_KEY) as mcp:
            with pytest.raises(MCPError) as info:
                await mcp.list_tools()
    assert info.value.code == "transport" and "unreachable" in str(info.value)
    assert FAKE_KEY not in repr(mcp)


async def test_expired_session_starts_a_new_one_and_repeats_the_request() -> None:
    server = FakeServer()
    sessions = iter(["sess-1", "sess-2"])
    calls = {"n": 0}

    def initialize(req: httpx.Request, msg: dict[str, Any]) -> httpx.Response:
        result = {"protocolVersion": PROTOCOL_VERSION, "serverInfo": {}, "capabilities": {}}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result}, headers={"Mcp-Session-Id": next(sessions)}
        )

    def call(req: httpx.Request, msg: dict[str, Any]) -> httpx.Response:
        calls["n"] += 1
        if req.headers.get("mcp-session-id") == "sess-1":
            return httpx.Response(404)
        return _reply(req, {"content": [{"type": "text", "text": "ok"}]})

    server.handlers.update({"initialize": initialize, "tools/call": call})
    with respx.mock() as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY) as mcp:
            await mcp.initialize()
            result = await mcp.call_tool("wallex_basic_get_markets")
    assert tool_payload(result) == "ok"
    assert mcp.session_id == "sess-2" and calls["n"] == 2
    assert server.methods().count("initialize") == 2


# ---------------------------------------------------------------- allowlist


async def test_state_changing_tool_is_refused_even_when_requested_and_listed() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(URL).respond(500)
        dangerous = READ_ONLY_ALLOWLIST | {"wallex_spot_post_account_orders"}
        async with MCPClient(URL, FAKE_KEY, allowlist=dangerous) as mcp:
            with pytest.raises(MCPToolNotAllowed, match="read-only"):
                await mcp.call_tool("wallex_spot_post_account_orders", {"symbol": "BTCTMN", "side": "BUY"})
        async with MCPClient(URL, FAKE_KEY) as mcp:  # the default allowlist is READ_ONLY_ALLOWLIST
            assert mcp.allowlist == READ_ONLY_ALLOWLIST
            for name in ("wallex_spot_delete_orders_client", "wallex_spot_new_order", "wallex_spot_get_tickers"):
                with pytest.raises(MCPToolNotAllowed):
                    await mcp.call_tool(name, {"client_id": "x"})
        async with MCPClient(URL, FAKE_KEY, allowlist=None) as mcp:  # None or empty: nothing at all
            with pytest.raises(MCPToolNotAllowed, match="allowlist"):
                await mcp.call_tool("wallex_basic_get_markets")
        async with MCPClient(URL, FAKE_KEY, allowlist=READ_ONLY_ALLOWLIST) as mcp:
            with pytest.raises(MCPToolNotAllowed, match="allowlist"):
                await mcp.call_tool("wallex_credit_active_sub_accounts_list")
    assert not route.called  # nothing was sent, not even initialize


async def test_allowed_tools_hides_state_changing_and_unlisted_tools() -> None:
    server = FakeServer()
    names = ["wallex_spot_post_account_orders", "wallex_basic_get_markets", "wallex_credit_active_sub_accounts_list"]
    server.handlers["tools/list"] = {"tools": [{"name": n} for n in names]}
    with respx.mock() as router:
        router.post(URL).mock(side_effect=server)
        async with MCPClient(URL, FAKE_KEY, allowlist=READ_ONLY_ALLOWLIST) as mcp:
            await mcp.list_tools()
        wider = READ_ONLY_ALLOWLIST | set(names)  # even a listed state-changing tool stays hidden
        async with MCPClient(URL, FAKE_KEY, allowlist=wider) as open_mcp:
            await open_mcp.list_tools()
    assert [t["name"] for t in mcp.allowed_tools()] == ["wallex_basic_get_markets"]
    assert [t["name"] for t in open_mcp.allowed_tools()] == [
        "wallex_basic_get_markets",
        "wallex_credit_active_sub_accounts_list",
    ]


def test_read_only_allowlist_is_the_documented_set() -> None:
    assert READ_ONLY_ALLOWLIST == {
        "wallex_basic_get_markets",
        "wallex_basic_get_market_depth",
        "wallex_basic_candle_history",
        "wallex_basic_get_latest_trades",
        "wallex_basic_get_account_balances",
        "wallex_basic_get_account_fees",
        "wallex_spot_get_open_order",
        "wallex_spot_get_orders",
    }
    assert not any(is_state_changing(n) for n in READ_ONLY_ALLOWLIST)


@pytest.mark.parametrize(
    "name",
    [
        "wallex_spot_post_account_orders",
        "wallex_spot_delete_orders_client",
        "wallex_spot_cancel_order",
        "wallex_credit_create_sub_account",
        "wallex_credit_transfer_to_sub_account",
        "wallex_wallet_withdraw",
        "wallex_margin_get_positions",
        "wallex_otc_quote",
        "wallex_user_iban_list",
        "wallex_bank_card_list",
        "placeOrder",
        "createOrder",
        "wallex-spot-update-order",
        # order tools whose names avoid post/create/place (reviewer's list)
        "wallex_spot_new_order",
        "wallex_spot_buy",
        "wallex_spot_sell",
        "wallex_spot_order",
        "wallex_spot_market_order",
        "wallex_spot_stopLimitOrder",
        "wallex_spot_close_position",
        "wallex_credit_add_order",
        "wallex_spot_send_order",
        "wallex_wallet_deposit",
        "wallex_earn_stake",
        "wallex_earn_redeem",
        "wallex_spot_set_leverage",
    ],
)
def test_is_state_changing_flags_dangerous_names(name: str) -> None:
    assert is_state_changing(name)


@pytest.mark.parametrize(
    "name",
    [
        "wallex_spot_get_markets",
        "wallex_spot_get_depth",
        "wallex_spot_get_orders_client",
        "wallex_credit_active_sub_accounts_list",
        "wallex_credit_sub_account_balances_details",
        "wallex_basic_candle_history",
        "wallex_spot_get_open_order",
        "wallex_spot_list_orders",
        *sorted(READ_ONLY_ALLOWLIST),
    ],
)
def test_is_state_changing_allows_read_only_names(name: str) -> None:
    assert not is_state_changing(name)


# ---------------------------------------------------------------- truncation


def _size(obj: Any) -> int:
    return len(canonical_json(obj))


def test_truncate_result_passes_small_results_and_makes_them_json_safe() -> None:
    small = {"price": Decimal("82845.00"), "levels": [1, 2, 3]}
    assert truncate_result(small) == {"price": "82845.00", "levels": [1, 2, 3]}


def test_truncate_result_keeps_best_levels_and_newest_candles() -> None:
    depth = {"bid": [[str(1000 - i), "1.5"] for i in range(500)], "ask": [[str(1001 + i), "2"] for i in range(500)]}
    out = truncate_result(depth, max_bytes=1024)
    assert _size(out) <= 1024 and out["_truncated"] is True
    assert out["bid"][0] == ["1000", "1.5"] and out["ask"][0] == ["1001", "2"]  # best levels kept, pairs whole

    candles = {"s": "ok", "t": list(range(1000)), "c": [str(x) for x in range(1000)]}
    out = truncate_result(candles, max_bytes=800)
    assert _size(out) <= 800
    assert out["t"][-1] == 999 and out["c"][-1] == "999"  # newest bars kept, columns aligned
    assert len(out["t"]) == len(out["c"])


def test_truncate_result_trims_json_inside_mcp_text_content() -> None:
    inner = {"result": {"latestTrades": [{"price": str(i), "quantity": "0.1"} for i in range(400)]}}
    result = {"content": [{"type": "text", "text": json.dumps(inner)}], "isError": False}
    out = truncate_result(result, max_bytes=4096)
    assert _size(out) <= 4096
    trades = out["content"][0]["json"]["result"]["latestTrades"]
    assert trades[-1]["price"] == "399"  # newest trades kept


def test_truncate_result_falls_back_to_a_prefix_for_one_huge_string() -> None:
    blob = {"content": [{"type": "text", "text": 'x"' * 20_000}]}
    out = truncate_result(blob, max_bytes=500)
    assert out["truncated"] is True and _size(out) <= 500
    assert out["json_prefix"].startswith('{"content"')
