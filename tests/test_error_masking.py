"""Error texts are masked BEFORE they are cut: a cut inside an echoed key must not leave its prefix.

Covers the MCP client and both LLM providers (the REST client has its own test in
test_rest_client.py). Every server is a mocked transport; nothing leaves the machine.
"""

from __future__ import annotations

import httpx
import pytest

from wta.ai.llm_client import GeminiProvider, OpenRouterProvider, ProviderError
from wta.ai.mcp_client import MCPClient, MCPError

# built at runtime so the source never holds a key-like literal
KEY = "".join(["mask", "-test-", "z" * 10, "4" * 10])
HALF = KEY[: len(KEY) // 2]


def _echo_body(cut_at: int) -> str:
    """An error body whose ``cut_at``-char cut falls in the middle of the echoed key."""
    return "e" * (cut_at - len(KEY) // 2) + KEY + " (echoed by the server)"


def _assert_masked(text: str) -> None:
    assert KEY not in text and HALF not in text


async def test_mcp_http_error_body_is_masked_before_the_cut() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text=_echo_body(300)))
    async with MCPClient("https://mcp.test/mcp", KEY, transport=transport) as mcp:
        with pytest.raises(MCPError) as info:
            await mcp.initialize()
    _assert_masked(str(info.value))
    _assert_masked(info.value.message)


@pytest.mark.parametrize("provider_cls", [OpenRouterProvider, GeminiProvider])
async def test_llm_http_error_body_is_masked_before_the_cut(provider_cls: type) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text=_echo_body(300)))
    provider = provider_cls(KEY, transport=transport)
    with pytest.raises(ProviderError) as info:
        await provider.chat([{"role": "user", "content": "hi"}], None, "model-x", 0.0, 10, False)
    _assert_masked(str(info.value))


async def test_openrouter_error_object_is_masked_before_the_cut() -> None:
    error = {"message": _echo_body(300 - len("{'message': '"))}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"error": error}))
    with pytest.raises(ProviderError) as info:
        await OpenRouterProvider(KEY, transport=transport).chat(
            [{"role": "user", "content": "hi"}], None, "model-x", 0.0, 10, False
        )
    _assert_masked(str(info.value))
