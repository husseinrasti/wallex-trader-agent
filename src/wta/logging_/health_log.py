"""Heartbeat log ``logs/health.jsonl`` (PRD-01 §8): one ``HealthRecord`` every 60 s.

``latest()`` gives the TUI / status command the most recent heartbeat without reading the whole
file: it reads the newest file backwards from its end.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson

from wta.logging_.jsonl import JsonlWriter, family_files
from wta.models import HealthRecord
from wta.utils.timeutil import Clock


class HealthLog:
    """Writer for ``health.jsonl``. The caller builds the ``HealthRecord`` (``ts = iso(clock.now())``)."""

    def __init__(
        self,
        log_dir: Path,
        clock: Clock,
        secrets: list[str] | None = None,
        *,
        base: str = "health",
        rotate_daily: bool = True,
        fsync: bool = False,
    ) -> None:
        self.clock = clock
        self.writer = JsonlWriter(
            log_dir, base, clock, rotate_daily=rotate_daily, secrets=secrets, fsync=fsync
        )

    def write(self, record: HealthRecord) -> HealthRecord:
        if not isinstance(record, HealthRecord):
            raise TypeError(f"HealthLog.write expects a HealthRecord, got {type(record).__name__}")
        self.writer.write(record)
        return record

    def close(self) -> None:
        self.writer.close()


def _reverse_lines(path: Path, block: int = 8192) -> Iterator[bytes]:
    """Non-blank lines of a file, last line first, reading ``block`` bytes at a time from the end."""
    with path.open("rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        tail = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + tail).split(b"\n")
            tail = lines.pop(0)  # may be the second half of a line that starts in the previous block
            for line in reversed(lines):
                if line.strip():
                    yield line
        if tail.strip():
            yield tail


def latest(log_dir: Path, base: str = "health") -> dict[str, Any] | None:
    """The last decodable record of the newest file of a family, or None if there is none."""
    for path in reversed(family_files(log_dir, base)):
        for line in _reverse_lines(path):
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue  # e.g. a half-written last line after a crash
            if isinstance(obj, dict):
                return obj
    return None
