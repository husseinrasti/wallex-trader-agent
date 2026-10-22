"""LLM cache (PRD-04 §3): key stability, hit/miss accounting, strict replay misses, atomic canonical
writes, and a no-secret scan of entries written through the real ``LLMClient`` (HTTP mocked with
respx; nothing touches the network). Fake keys are assembled at runtime so this file never holds a
complete key-like literal."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import wta.ai.cache as cache_mod
from wta.ai.cache import CacheMissError, LLMCache, cache_key
from wta.ai.llm_client import GeminiProvider, LLMClient, OpenRouterProvider, Route
from wta.config import canonical_json
from wta.logging_.checker import scan_for_secrets
from wta.utils.timeutil import SimClock

OR_KEY = "sk" + "-or-v1-" + "5e1f" * 16
GEM_KEY = "AI" + "za" + "Sy" + "Zq7" * 11
OR_URL = "https://openrouter.test/api/v1"
GEM_URL = "https://gemini.test/v1beta"

MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "You are the regime analyst."},
    {"role": "user", "content": "Decide the plan."},
]
TOOLS: list[dict[str, Any]] = [
    {"name": "get_health", "description": "health", "parameters": {"type": "object", "properties": {}}},
]


def _key(**over: Any) -> str:
    args: dict[str, Any] = {"provider": "openrouter", "model": "m-fast", "messages": MESSAGES, "tools": TOOLS,
                            "temperature": 0.0}
    args.update(over)
    return cache_key(args["provider"], args["model"], args["messages"], args["tools"], args["temperature"])


def _entry(text: str = '{"ok": true}') -> dict[str, Any]:
    return {"request": {"provider": "openrouter", "model": "m-fast", "messages": MESSAGES, "tools": [],
                        "temperature": 0.0},
            "response": {"text": text, "tool_calls": [], "usage": {"total_tokens": 7}, "model": "m-fast",
                         "provider": "openrouter"},
            "ts": 1_792_663_200.0, "latency_ms": 12, "usage": {"total_tokens": 7}}


# ---------------------------------------------------------------- key


def test_key_is_stable_sha256_hex() -> None:
    k = _key()
    assert k == _key()
    assert len(k) == 64 and int(k, 16) >= 0
    # a value computed once, so an accidental change of the key recipe (which would orphan every
    # committed cache file) fails loudly
    assert k == cache_key("openrouter", "m-fast", json.loads(json.dumps(MESSAGES)), list(TOOLS), 0)


def test_key_ignores_dict_key_order() -> None:
    reordered_msgs = [{"content": m["content"], "role": m["role"]} for m in MESSAGES]
    reordered_tools = [{"parameters": {"properties": {}, "type": "object"}, "description": "health",
                        "name": "get_health"}]
    assert _key(messages=reordered_msgs, tools=reordered_tools) == _key()


def test_no_tools_and_empty_tools_share_a_key() -> None:
    assert _key(tools=None) == _key(tools=[])


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "gemini"},
        {"model": "m-strong"},
        {"temperature": 0.2},
        {"tools": []},
        {"tools": [{**TOOLS[0], "description": "health (v2)"}]},
        {"messages": [*MESSAGES, {"role": "assistant", "content": "{}"}]},
        {"messages": [MESSAGES[0], {"role": "user", "content": "Decide the plan!"}]},
        {"messages": list(reversed(MESSAGES))},  # message order is part of the conversation
    ],
)
def test_key_changes_with_every_input(change: dict[str, Any]) -> None:
    assert _key(**change) != _key()


def test_key_fields_do_not_run_together() -> None:
    # separators between fields: ("ab", "c") and ("a", "bc") must not collide
    assert _key(provider="ab", model="c") != _key(provider="a", model="bc")


def test_temperature_int_and_float_are_the_same_key() -> None:
    assert _key(temperature=0) == _key(temperature=0.0)


# ---------------------------------------------------------------- get / put


def test_hit_and_miss_counters(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "c")
    k = _key()
    assert cache.get(k) is None
    assert (cache.hits, cache.misses) == (0, 1)
    assert len(cache) == 0  # directory does not even exist yet

    cache.put(k, _entry())
    assert cache.get(k) == json.loads(canonical_json(_entry()))
    assert cache.get(k) is not None
    assert (cache.hits, cache.misses) == (2, 1)
    assert len(cache) == 1
    assert cache.path(k) == tmp_path / "c" / f"{k}.json"


def test_strict_miss_raises_and_counts(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path, strict=True)
    with pytest.raises(CacheMissError, match="replay_strict"):
        cache.get(_key())
    assert cache.misses == 1
    cache.put(_key(), _entry())
    assert cache.get(_key())["response"]["text"] == '{"ok": true}'
    assert cache.hits == 1


def test_entries_are_canonical_json(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path)
    entry = {"z": 1, "a": {"y": Decimal("0.0015"), "b": [3, {"d": 1, "c": 2}]}, "m": "ف"}
    p = cache.put(_key(), entry)
    raw = p.read_bytes()
    assert raw == canonical_json(json.loads(raw))  # sorted keys, compact, UTF-8
    assert raw == b'{"a":{"b":[3,{"c":2,"d":1}],"y":"0.0015"},"m":"\xd9\x81","z":1}'


def test_put_overwrites_atomically(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path)
    cache.put(_key(), _entry("first"))
    cache.put(_key(), _entry("second"))
    assert cache.get(_key())["response"]["text"] == "second"
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{_key()}.json"]


def test_failed_write_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = LLMCache(tmp_path)
    cache.put(_key(), _entry("good"))

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cache_mod.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        cache.put(_key(), _entry("half-written"))
    with pytest.raises(OSError, match="disk full"):
        cache.put(_key(model="other"), _entry("new"))
    # the old entry is intact, the temp files are gone and no new entry appeared
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{_key()}.json"]
    assert cache.get(_key())["response"]["text"] == "good"


def test_unserialisable_entry_writes_nothing(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path)
    with pytest.raises(TypeError):
        cache.put(_key(), {"bad": object()})
    assert list(tmp_path.iterdir()) == []


def test_orphaned_temp_file_never_looks_like_an_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard kill between write and rename leaves the temp file behind: it must not count as an
    entry nor match the ``*.json`` glob used to collect cache files."""
    cache = LLMCache(tmp_path)

    def boom(src: str, dst: str) -> None:
        raise OSError("killed")

    monkeypatch.setattr(cache_mod.os, "replace", boom)
    monkeypatch.setattr(cache_mod.os, "unlink", lambda p: None)  # simulate: no cleanup ran
    with pytest.raises(OSError):
        cache.put(_key(), _entry())
    leftovers = list(tmp_path.iterdir())
    assert len(leftovers) == 1 and leftovers[0].name.startswith(".tmp-")
    assert len(cache) == 0
    assert list(tmp_path.glob("*.json")) == []
    os.remove(leftovers[0])


# ---------------------------------------------------------------- no secrets in cache files


def _openrouter_ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"model": "m-fast", "choices": [{"message": {"content": text}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def _gemini_ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}],
                                     "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}})


async def test_cache_files_never_contain_api_keys(tmp_path: Path) -> None:
    cache_dir = tmp_path / "llm_cache"
    providers = {"openrouter": OpenRouterProvider(OR_KEY, base_url=OR_URL),
                 "gemini": GeminiProvider(GEM_KEY, base_url=GEM_URL)}
    client = LLMClient(providers, Route("openrouter", "m-fast"), Route("gemini", "g-flash"), LLMCache(cache_dir),
                       mode="shadow", clock=SimClock(1_792_663_200.0))
    with respx.mock(assert_all_called=True) as router:
        # the primary answers once, then fails while echoing the key; the fallback takes over
        router.post(f"{OR_URL}/chat/completions").mock(side_effect=[
            _openrouter_ok('{"plan": "a"}'),
            httpx.Response(401, json={"error": {"message": f"bad key {OR_KEY}"}}),
        ])
        router.post(f"{GEM_URL}/models/g-flash:generateContent").mock(return_value=_gemini_ok('{"plan": "b"}'))
        first = await client.chat(MESSAGES, TOOLS)
        second = await client.chat([*MESSAGES, {"role": "user", "content": "again"}])
    assert first is not None and first.provider == "openrouter"
    assert second is not None and second.provider == "gemini"

    files = sorted(cache_dir.glob("*.json"))
    assert len(files) == 2
    for f in files:
        text = f.read_text(encoding="utf-8")
        for secret in (OR_KEY, GEM_KEY, OR_KEY[-24:], GEM_KEY[-20:]):
            assert secret not in text
        entry = json.loads(text)
        assert sorted(entry) == ["latency_ms", "request", "response", "ts", "usage"]
        assert entry["ts"] == 1_792_663_200.0
        assert f.read_bytes() == canonical_json(entry)
    # the repo's secrets guard agrees: neither the literal keys nor any key-like pattern
    assert scan_for_secrets([cache_dir], {"openrouter_api_key": OR_KEY, "gemini_api_key": GEM_KEY}) == []
