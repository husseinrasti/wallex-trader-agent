"""LLM client (PRD-04 §2): OpenRouter (OpenAI-compatible) primary, Google Gemini fallback.

``LLMClient.chat`` = cache lookup -> (replay: stop) -> daily budget -> primary provider (timeout)
-> fallback provider once -> ``None``. Every successful response is cached by
``sha256(provider + model + messages + tools + temperature)`` so the whole agentic trajectory
replays offline. ``LLMClient.structured`` adds JSON extraction + pydantic validation with one
"fix the JSON" retry; a second failure returns ``None`` (the engine keeps the last valid plan).
Keys travel only in HTTP headers and never appear in logs, cache files or exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from wta.ai.cache import CacheMissError, LLMCache, cache_key
from wta.utils.timeutil import Clock, SystemClock, utc_day

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    cached: bool = False
    cache_key: str = ""

    def to_entry(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls],
            "usage": self.usage,
            "model": self.model,
            "provider": self.provider,
        }

    @classmethod
    def from_entry(cls, entry: dict[str, Any], key: str) -> LLMResponse:
        r = entry["response"]
        return cls(
            text=r.get("text", ""),
            tool_calls=[ToolCall(t["id"], t["name"], t.get("arguments") or {}) for t in r.get("tool_calls", [])],
            usage=r.get("usage", {}),
            model=r.get("model", ""),
            provider=r.get("provider", ""),
            latency_ms=int(entry.get("latency_ms", 0)),
            cached=True,
            cache_key=key,
        )


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse: ...


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


# ---------------------------------------------------------------- OpenRouter (OpenAI-compatible)


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1", timeout: float = 40,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        if not self._key:
            raise ProviderError("openrouter: no API key configured")
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens, "seed": 42}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}", "HTTP-Referer": "https://github.com/wallex-trader-agent",
                   "X-Title": "wallex-trader-agent"}
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"openrouter transport error: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise ProviderError(f"openrouter HTTP {resp.status_code}: {_scrub(resp.text, self._key)[:300]}")
        data = resp.json()
        if "error" in data:
            raise ProviderError(f"openrouter error: {_scrub(str(data['error']), self._key)[:300]}")
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("openrouter: malformed response") from None
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name", ""), args if isinstance(args, dict) else {}))
        return LLMResponse(text=msg.get("content") or "", tool_calls=calls, usage=data.get("usage") or {},
                           model=data.get("model", model), provider=self.name,
                           latency_ms=int((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------- Gemini


def _to_gemini(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system: dict[str, Any] | None = None
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system = {"parts": [{"text": m.get("content") or ""}]}
        elif role == "tool":
            try:
                payload = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                payload = {"result": m.get("content")}
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name", "tool"), "response": payload if isinstance(payload, dict) else {"result": payload}}}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                parts.append({"functionCall": {"name": fn.get("name"),
                                               "args": json.loads(args) if isinstance(args, str) and args else (args or {})}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        else:
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
    return system, contents


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 timeout: float = 40, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        if not self._key:
            raise ProviderError("gemini: no API key configured")
        system, contents = _to_gemini(messages)
        body: dict[str, Any] = {"contents": contents,
                                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = system
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}
                for t in tools]}]
        elif json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                resp = await client.post(f"{self.base_url}/models/{model}:generateContent", json=body,
                                         headers={"x-goog-api-key": self._key})
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini transport error: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise ProviderError(f"gemini HTTP {resp.status_code}: {_scrub(resp.text, self._key)[:300]}")
        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("gemini: malformed response") from None
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        calls = [ToolCall(f"{p['functionCall'].get('name')}_{i}", p["functionCall"].get("name", ""),
                          p["functionCall"].get("args") or {}) for i, p in enumerate(parts) if "functionCall" in p]
        um = data.get("usageMetadata") or {}
        return LLMResponse(text=text, tool_calls=calls,
                           usage={"prompt_tokens": um.get("promptTokenCount"), "completion_tokens": um.get("candidatesTokenCount")},
                           model=model, provider=self.name, latency_ms=int((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------- client


@dataclass
class Route:
    provider: str
    model: str


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` (tolerates ```json fences and prose around it)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


class LLMClient:
    """Provider failover + cache + per-role daily call budget."""

    def __init__(self, providers: dict[str, Provider], primary: Route, fallback: Route | None, cache: LLMCache,
                 mode: str = "shadow", clock: Clock | None = None, daily_budget: dict[str, int] | None = None,
                 on_call: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.providers = providers
        self.primary = primary
        self.fallback = fallback
        self.cache = cache
        self.mode = mode
        self.clock = clock or SystemClock()
        self.daily_budget = daily_budget or {}
        self._calls: dict[tuple[str, str], int] = {}
        self.on_call = on_call
        self.consecutive_failures = 0
        self.last_error = ""

    def _budget_ok(self, role: str) -> bool:
        limit = self.daily_budget.get(role)
        if limit is None:
            return True
        return self._calls.get((role, utc_day(self.clock.now())), 0) < limit

    def _count(self, role: str) -> None:
        k = (role, utc_day(self.clock.now()))
        self._calls[k] = self._calls.get(k, 0) + 1

    async def _call_route(self, route: Route, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                          temperature: float, max_tokens: int, json_mode: bool, timeout: float, role: str) -> LLMResponse:
        key = cache_key(route.provider, route.model, messages, tools, temperature)
        hit = self.cache.get(key)  # raises CacheMissError in strict replay
        if hit is not None:
            return LLMResponse.from_entry(hit, key)
        if self.mode == "replay":
            raise CacheMissError(f"replay: no cached response for {key}")
        if not route.model:
            raise ProviderError(f"{route.provider}: no model configured")
        provider = self.providers.get(route.provider)
        if provider is None:
            raise ProviderError(f"provider {route.provider} not configured")
        if not self._budget_ok(role):
            raise ProviderError(f"daily {role} call budget exhausted")
        self._count(role)
        resp = await asyncio.wait_for(
            provider.chat(messages, tools, route.model, temperature, max_tokens, json_mode), timeout=timeout)
        resp.cache_key = key
        self.cache.put(key, {
            "request": {"provider": route.provider, "model": route.model, "messages": messages, "tools": tools or [],
                        "temperature": temperature},
            "response": resp.to_entry(),
            "ts": self.clock.now(),
            "latency_ms": resp.latency_ms,
            "usage": resp.usage,
        })
        return resp

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                   role: str = "analyst", temperature: float = 0.0, max_tokens: int = 1500,
                   json_mode: bool = True, timeout: float = 40, route: Route | None = None) -> LLMResponse | None:
        routes = [route or self.primary] + ([self.fallback] if self.fallback and route is None else [])
        miss: CacheMissError | None = None
        for i, r in enumerate(routes):
            try:
                resp = await self._call_route(r, messages, tools, temperature, max_tokens, json_mode, timeout, role)
                self.consecutive_failures = 0
                if self.on_call:
                    self.on_call({"role": role, "provider": resp.provider, "model": resp.model, "cached": resp.cached,
                                  "latency_ms": resp.latency_ms, "usage": resp.usage, "fallback": i > 0})
                return resp
            except CacheMissError as exc:
                # replay: if the recorded run failed over, the answer is cached under the fallback route
                miss = miss or exc
            except (ProviderError, TimeoutError, httpx.HTTPError, ValueError) as exc:
                self.last_error = f"{r.provider}: {type(exc).__name__}: {str(exc)[:200]}"
                log.warning("LLM call failed (%s)", self.last_error)
        if miss is not None:
            raise miss
        self.consecutive_failures += 1
        return None

    async def structured(self, messages: list[dict[str, Any]], model_cls: type[T], *, role: str = "analyst",
                         temperature: float = 0.0, max_tokens: int = 1500, timeout: float = 40,
                         route: Route | None = None) -> tuple[T | None, LLMResponse | None, str]:
        """Ask for JSON and validate it. Returns (object|None, last response, error text)."""
        msgs = list(messages)
        last: LLMResponse | None = None
        err = ""
        for attempt in range(2):
            last = await self.chat(msgs, None, role=role, temperature=temperature, max_tokens=max_tokens,
                                   json_mode=True, timeout=timeout, route=route)
            if last is None:
                return None, None, self.last_error or "llm unavailable"
            try:
                return model_cls.model_validate(extract_json(last.text)), last, ""
            except (ValueError, ValidationError) as exc:
                err = str(exc)[:800]
                if attempt == 0:
                    msgs = msgs + [{"role": "assistant", "content": last.text},
                                   {"role": "user", "content": f"Your JSON was invalid: {err}\nFix the JSON. "
                                                               "Return only the corrected JSON object."}]
        return None, last, f"llm_invalid: {err}"
