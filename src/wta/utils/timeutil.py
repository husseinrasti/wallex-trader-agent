"""Time helpers. Strategies and risk never read the wall clock: they get a ``Clock``."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class SimClock:
    """Settable clock for backtest/replay/tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)

    def advance(self, seconds: float) -> None:
        self._t += seconds


def iso(ts: float) -> str:
    """Unix seconds -> ISO-8601 UTC with milliseconds, e.g. 2026-10-22T10:00:00.123Z."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str | None) -> float | None:
    """ISO-8601 (with Z or offset) -> unix seconds; None/'' -> None."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y%m%d")


def day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=UTC)
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
