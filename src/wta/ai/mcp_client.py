"""Read-only Wallex MCP client (PRD-04 §4): JSON-RPC 2.0 over streamable HTTP.

* ``initialize`` sends protocol version ``2025-06-18`` and our ``clientInfo``, then the
  ``notifications/initialized`` notification. A ``Mcp-Session-Id`` returned by the server is sent on
  every later request, together with the negotiated ``MCP-Protocol-Version``. If the server forgets
  the session (HTTP 404), the client starts a new one once and repeats the request.
* Every request is a ``POST`` with ``Accept: application/json, text/event-stream``. The answer is
  either plain JSON or an SSE stream of ``data: {...}`` events; the message whose ``id`` matches the
  request is used and anything else on the stream (notifications, progress) is ignored.
* JSON-RPC errors raise ``MCPError``. Fractional numbers are decoded as ``Decimal``, never float.
* The client is read-only by construction: ``call_tool`` only calls names on its ``allowlist``
  (``READ_ONLY_ALLOWLIST`` unless a caller passes another one; an empty or ``None`` allowlist
  allows nothing), and refuses every name that ``is_state_changing`` flags even when it is listed.
* The API key travels only in the auth header. It is scrubbed from every error message, from every
  string of a ``tools/call`` result (``content``, ``structuredContent``, an ``isError`` text that
  echoes it) and never shown by ``repr``: nothing the server sends back can carry it into a prompt,
  the LLM cache or the MCP tape.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import httpx

from wta import __version__
from wta.config import canonical_json, mask_secrets

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
ACCEPT = "application/json, text/event-stream"
CLIENT_INFO = {"name": "wallex-trader-agent", "version": __version__}
MAX_LIST_PAGES = 20

# The only Wallex MCP tools the Regime Analyst may call: market data and read-only account views.
READ_ONLY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "wallex_basic_get_markets",
        "wallex_basic_get_market_depth",
        "wallex_basic_candle_history",
        "wallex_basic_get_latest_trades",
        "wallex_basic_get_account_balances",
        "wallex_basic_get_account_fees",
        "wallex_spot_get_open_order",
        "wallex_spot_get_orders",
    }
)

# A tool whose name contains one of these words (as a word, or the start of one) may change state.
# The list is deliberately broad: refusing a harmless tool costs nothing, calling a harmful one could.
# It is the second layer; the allowlist is the first.
STATE_CHANGING_WORDS: tuple[str, ...] = (
    "post",
    "put",
    "patch",
    "delete",
    "create",
    "cancel",
    "update",
    "place",
    "submit",
    "transfer",
    "withdraw",
    "margin",
    "otc",
    "iban",
    "card",
    "convert",
    "swap",
    "borrow",
    "repay",
    "buy",
    "sell",
    "new",
    "add",
    "send",
    "close",
    "set",
    "deposit",
    "stake",
    "redeem",
)
# "order" is state-changing unless a read verb comes before it (``get_orders``, ``list_orders``).
ORDER_WORD = "order"
READ_VERBS: tuple[str, ...] = ("get", "list")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class MCPError(Exception):
    """Transport failure, HTTP error or JSON-RPC error from the MCP server. Never carries the key."""

    def __init__(self, message: str, code: int | str | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class MCPToolNotAllowed(MCPError):
    """The tool is state-changing or not on the allowlist; nothing was sent."""


class _SessionExpired(Exception):
    """HTTP 404 on a request that carried a session id: the server dropped the session."""


def is_state_changing(name: str) -> bool:
    """True when a tool name looks like it can change account state (orders, transfers, ...).

    ``wallex_spot_post_account_orders`` -> True, ``wallex_spot_new_order`` -> True,
    ``wallex_spot_get_orders`` -> False. Names are split into words on ``_``, ``-``, ``.``, ``/``
    and camelCase boundaries.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", _CAMEL_RE.sub("_", name).lower()) if w]
    if any(w.startswith(bad) for w in words for bad in STATE_CHANGING_WORDS):
        return True
    for i, word in enumerate(words):
        if word.startswith(ORDER_WORD) and not any(w in READ_VERBS for w in words[:i]):
            return True
    return False


# ---------------------------------------------------------------- framing helpers


def _decode_json(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal)


class _SSEDecoder:
    """Incremental server-sent-events decoder: feed lines, get each complete event's JSON payload."""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> Any:
        """Process one line (without newline). Returns the decoded payload when an event ends."""
        if line == "":
            return self.flush()
        if line.startswith(":"):  # comment / keep-alive
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        return None  # "event:", "id:" and "retry:" fields are not needed here

    def flush(self) -> Any:
        """Dispatch the pending event (at a blank line or the end of the stream)."""
        if not self._data:
            return None
        text = "\n".join(self._data)
        self._data = []
        try:
            return _decode_json(text)
        except ValueError:
            log.debug("mcp: skipping an SSE event that is not JSON")
            return None


def parse_sse(text: str) -> list[Any]:
    """Every JSON payload in an SSE body (``data: {...}`` events separated by blank lines)."""
    decoder = _SSEDecoder()
    out = [decoder.feed(line) for line in text.splitlines()]
    out.append(decoder.flush())
    return [p for p in out if p is not None]


def _messages(payload: Any) -> list[dict[str, Any]]:
    """JSON-RPC messages in a payload (a single message or a batch)."""
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return []


def _match(payload: Any, request_id: int) -> dict[str, Any] | None:
    """The response to ``request_id`` in ``payload``, if any (requests/notifications are skipped)."""
    for msg in _messages(payload):
        if msg.get("id") == request_id and ("result" in msg or "error" in msg):
            return msg
    return None


def _json_safe(obj: Any) -> Any:
    """Decimals (and other non-JSON values) to plain JSON types, via the canonical encoder."""
    return json.loads(canonical_json(obj))


def tool_payload(result: Any) -> Any:
    """The data inside a ``tools/call`` result.

    ``structuredContent`` when present; otherwise the text content, decoded as JSON when it is JSON
    (fractions as ``Decimal``). Several text items give a list.
    """
    if not isinstance(result, dict):
        return result
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [
        str(c.get("text", ""))
        for c in result.get("content") or []
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    decoded: list[Any] = []
    for text in texts:
        try:
            decoded.append(_decode_json(text))
        except ValueError:
            decoded.append(text)
    if not decoded:
        return None
    return decoded[0] if len(decoded) == 1 else decoded


# ---------------------------------------------------------------- truncation

# Lists under these keys are time series: keep the newest (last) items. Others (order book levels,
# market lists) keep the first items, which are the best levels.
_KEEP_LAST_KEYS = frozenset({"t", "o", "h", "l", "c", "v", "candles", "trades", "latesttrades", "history"})


def _size(obj: Any) -> int:
    return len(canonical_json(obj))


def _trim(node: Any, keep: int, keep_last: bool = False) -> Any:
    if isinstance(node, list):
        items = node[-keep:] if keep_last else node[:keep]
        return [_trim(x, keep) for x in items]
    if isinstance(node, dict):
        return {k: _trim(v, keep, str(k).lower() in _KEEP_LAST_KEYS) for k, v in node.items()}
    return node


def _expand_text(data: Any) -> Any:
    """Decode JSON text content items of an MCP result, so their lists can be trimmed."""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return data
    content = []
    for item in data["content"]:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(str(item.get("text", "")))
            except ValueError:
                parsed = None
            if isinstance(parsed, dict | list):
                item = {"type": "json", "json": parsed}
        content.append(item)
    return {**data, "content": content}


def truncate_result(obj: Any, max_bytes: int = 4096) -> Any:
    """Shrink a tool result to at most ``max_bytes`` of canonical JSON before it reaches the model.

    Small results pass through (made JSON-safe). Larger ones get their lists trimmed (keeping the
    best order-book levels and the newest candles/trades) and are marked ``"_truncated": true``.
    If that is not enough, the result becomes ``{"truncated": true, "json_prefix": "..."}``.
    """
    data = _json_safe(obj)
    if _size(data) <= max_bytes:
        return data
    data = _expand_text(data)
    for keep in (50, 20, 10, 5, 2):  # >= 2 keeps [price, qty] pairs whole
        candidate = _trim(data, keep)
        if isinstance(candidate, dict):
            candidate = {**candidate, "_truncated": True}
        if _size(candidate) <= max_bytes:
            return candidate
    text = canonical_json(data).decode("utf-8")
    cut = max(0, max_bytes - 40)
    while True:
        wrapper = {"truncated": True, "json_prefix": text[:cut]}
        excess = _size(wrapper) - max_bytes
        if excess <= 0 or cut == 0:
            return wrapper
        cut = max(0, cut - excess)


# ---------------------------------------------------------------- client


class MCPClient:
    """Streamable-HTTP MCP client for ``url`` (e.g. ``https://api.wallex.ir/mcp``).

    Only tools on ``allowlist`` can be called (default ``READ_ONLY_ALLOWLIST``; ``None`` or empty
    allows nothing), and never a state-changing one.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        auth_header: str = "X-API-Key",
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        allowlist: Iterable[str] | None = READ_ONLY_ALLOWLIST,
    ) -> None:
        self.url = url
        self.auth_header = auth_header
        self.allowlist: frozenset[str] = frozenset(allowlist or ())
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=transport)
        self._ids = itertools.count(1)
        self.session_id: str | None = None
        self.protocol_version: str | None = None  # negotiated, set by initialize()
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.initialized = False
        self.tools: dict[str, dict[str, Any]] = {}  # name -> tool description, from list_tools()

    def __repr__(self) -> str:
        return f"MCPClient(url={self.url!r}, api_key={'<set>' if self._api_key else '<empty>'})"

    # ------------------------------------------------------------ public API

    async def initialize(self) -> dict[str, Any]:
        """Start a session: ``initialize`` then ``notifications/initialized``. Returns the server result."""
        self.session_id = None
        self.protocol_version = None
        self.initialized = False
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
        result = await self._rpc("initialize", params, allow_reinit=False)
        result = result if isinstance(result, dict) else {}
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        try:
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        except (MCPError, _SessionExpired) as exc:  # the session usually works anyway; the next request tells
            log.warning("mcp: notifications/initialized failed: %s", exc or type(exc).__name__)
        self.initialized = True
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server offers (follows ``nextCursor`` pages)."""
        if not self.initialized:
            await self.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            result = result if isinstance(result, dict) else {}
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools = {str(t["name"]): t for t in tools if t.get("name")}
        return tools

    def allowed_tools(self) -> list[dict[str, Any]]:
        """The listed tools this client may call (for the model's tool schemas), sorted by name."""
        out = []
        for name in sorted(self.tools):
            try:
                self.check_allowed(name)
            except MCPToolNotAllowed:
                continue
            out.append(self.tools[name])
        return out

    def check_allowed(self, name: str) -> None:
        """Raise ``MCPToolNotAllowed`` unless this client may call ``name``."""
        if is_state_changing(name):
            raise MCPToolNotAllowed(f"tool {name!r} can change account state; this client is read-only")
        if name not in self.allowlist:
            raise MCPToolNotAllowed(f"tool {name!r} is not on the allowlist")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """``tools/call``; returns the result object (``content``, ``isError``, ``structuredContent``).

        The allowlist is checked before anything is sent. A tool-level failure comes back as a
        result with ``isError: true``; protocol failures raise ``MCPError``.
        """
        self.check_allowed(name)
        if not self.initialized:
            await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise MCPError(f"tools/call {name}: the server returned no result object")
        # a server may echo the key (e.g. "invalid api key <key>" in an isError result): the result
        # goes into the model's messages, the LLM cache and the tape, so it is masked here, once
        return self._mask_obj(result)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MCPClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------ JSON-RPC plumbing

    def _mask(self, text: str) -> str:
        return mask_secrets(text, [self._api_key]) if self._api_key else text

    def _mask_obj(self, obj: Any) -> Any:
        """``obj`` with the key masked in every string (dict keys included); numbers (Decimal) and
        the structure stay as they are."""
        if not self._api_key:
            return obj
        if isinstance(obj, str):
            return self._mask(obj)
        if isinstance(obj, dict):
            return {self._mask(k) if isinstance(k, str) else k: self._mask_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self._mask_obj(v) for v in obj]
        return obj

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
        if self._api_key:
            value = self._api_key
            if self.auth_header.lower() == "authorization" and not value.lower().startswith("bearer "):
                value = f"Bearer {value}"
            headers[self.auth_header] = value
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def _rpc(self, method: str, params: dict[str, Any], allow_reinit: bool = True) -> Any:
        """Send one request and return its ``result``; JSON-RPC errors raise ``MCPError``."""
        request_id = next(self._ids)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            reply = await self._send(message, request_id)
        except _SessionExpired:
            if not allow_reinit:
                raise MCPError(f"{method}: the MCP session expired") from None
            log.info("mcp: session expired; starting a new one")
            await self.initialize()
            return await self._rpc(method, params, allow_reinit=False)
        assert reply is not None  # a request always gets a reply (or _send raises)
        error = reply.get("error")
        if error is not None:
            err = error if isinstance(error, dict) else {"message": str(error)}
            text = self._mask(f"{method}: {err.get('message') or 'JSON-RPC error'}")
            data = err.get("data")
            if data is not None:
                data = self._mask_obj(data)
            raise MCPError(text, code=err.get("code"), data=data)
        return reply.get("result")

    async def _send(self, message: dict[str, Any], request_id: int | None) -> dict[str, Any] | None:
        """POST one message. For a request, return the matching response; for a notification, None."""
        method = message.get("method", "?")
        had_session = self.session_id is not None
        try:
            body = canonical_json(message)  # handles Decimal arguments, unlike json=
            async with self._http.stream("POST", self.url, content=body, headers=self._headers()) as resp:
                if resp.status_code == 404 and had_session:
                    raise _SessionExpired
                if resp.status_code >= 400:
                    # mask before cutting: a cut inside the key would leave an unmasked prefix
                    text = self._mask((await resp.aread()).decode("utf-8", errors="replace"))[:300]
                    raise MCPError(
                        f"{method}: HTTP {resp.status_code} from MCP server: {text}".strip(),
                        code=resp.status_code,
                    )
                session_id = resp.headers.get("mcp-session-id")
                if session_id:
                    self.session_id = session_id
                if request_id is None:
                    await resp.aread()
                    return None
                reply = await self._read_reply(resp, request_id)
        except httpx.TimeoutException as exc:
            raise MCPError(self._mask(f"{method}: MCP request timed out ({type(exc).__name__})"), "timeout") from None
        except httpx.TransportError as exc:
            raise MCPError(
                self._mask(f"{method}: MCP server unreachable ({type(exc).__name__}: {exc})"), "transport"
            ) from None
        if reply is None:
            raise MCPError(f"{method}: no JSON-RPC response with id {request_id} in the answer")
        return reply

    async def _read_reply(self, resp: httpx.Response, request_id: int) -> dict[str, Any] | None:
        """The response for ``request_id`` from a JSON body or an SSE stream."""
        if "text/event-stream" in resp.headers.get("content-type", ""):
            decoder = _SSEDecoder()
            async for line in resp.aiter_lines():
                found = _match(decoder.feed(line), request_id)
                if found is not None:
                    return found  # do not wait for the server to close the stream
            return _match(decoder.flush(), request_id)
        raw = await resp.aread()
        if not raw.strip():
            return None
        try:
            payload = _decode_json(raw)
        except ValueError:
            raise MCPError("the MCP server answered with something that is not JSON") from None
        return _match(payload, request_id)


def tool_names(tools: Iterable[dict[str, Any]]) -> list[str]:
    """Sorted tool names from a ``list_tools()`` result."""
    return sorted(str(t["name"]) for t in tools if t.get("name"))
