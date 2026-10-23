"""LLM response cache (PRD-04 §3) — makes every model call replayable.

Key = sha256(provider + model + canonical_json(messages) + canonical_json(tools) + str(temperature)).
Value file ``<cache_dir>/<key>.json`` = ``{request, response, ts, latency_ms, usage}``.

* Every mode looks up the cache before calling a model.
* ``replay`` with ``strict=True``: a miss is a hard error (proves determinism, zero network).
* ``backtest``: a miss calls the model once and stores it, so the backtest is reproducible afterwards.
Tool-call loops are covered automatically: tool results are part of the next call's messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from wta.config import canonical_json


class CacheMissError(RuntimeError):
    """Raised by a strict cache (replay, ``backtest --ai cache``) when a request has no cached response."""


def cache_key(provider: str, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
              temperature: float) -> str:
    h = hashlib.sha256()
    h.update(provider.encode())
    h.update(b"\x00")
    h.update(model.encode())
    h.update(b"\x00")
    h.update(canonical_json(messages))
    h.update(b"\x00")
    h.update(canonical_json(tools or []))
    h.update(b"\x00")
    h.update(repr(float(temperature)).encode())
    return h.hexdigest()


class LLMCache:
    def __init__(self, cache_dir: str | Path, strict: bool = False) -> None:
        self.dir = Path(cache_dir)
        self.strict = strict
        self.hits = 0
        self.misses = 0

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self.path(key)
        if not p.exists():
            self.misses += 1
            if self.strict:
                raise CacheMissError(f"LLM cache miss for key {key} (strict cache)")
            return None
        self.hits += 1
        return json.loads(p.read_text(encoding="utf-8"))

    def put(self, key: str, entry: dict[str, Any]) -> Path:
        """Atomic write (tmp file + rename) so a crash never leaves a half-written entry."""
        self.dir.mkdir(parents=True, exist_ok=True)
        data = canonical_json(entry)
        # ".tmp" suffix: a temp file orphaned by a hard kill must never look like a "*.json" entry
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, self.path(key))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return self.path(key)

    def __len__(self) -> int:
        return len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0
