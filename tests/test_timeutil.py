"""Time helpers: ISO-8601 UTC formatting/parsing, UTC day boundaries and the simulated clock."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from wta.utils.timeutil import SimClock, SystemClock, day_start, iso, parse_iso, utc_day

T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z
MIDNIGHT = 1_792_627_200.0  # 2026-10-22T00:00:00Z
DAY = 86_400.0


@pytest.fixture
def tehran_local_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run with a non-UTC local zone (+03:30) to prove nothing depends on the machine's TZ."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")
    monkeypatch.setenv("TZ", "Asia/Tehran")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# ---------------------------------------------------------------- iso / parse_iso


def test_iso_format() -> None:
    assert iso(T0) == "2026-10-22T10:00:00.000Z"
    assert iso(T0 + 0.123) == "2026-10-22T10:00:00.123Z"


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-22T10:00:00Z",
        "2026-10-22T10:00:00.000Z",
        "2026-10-22T10:00:00+00:00",
        "2026-10-22T13:30:00+03:30",  # Tehran
        "2026-10-22T10:00:00",  # naive -> treated as UTC
        "  2026-10-22T10:00:00Z  ",
    ],
)
def test_parse_iso_variants(text: str) -> None:
    assert parse_iso(text) == T0


@pytest.mark.parametrize("text", [None, ""])
def test_parse_iso_empty(text: str | None) -> None:
    assert parse_iso(text) is None


@pytest.mark.parametrize("ts", [T0, T0 + 0.123, T0 + 0.999, MIDNIGHT, 0.0])
def test_iso_parse_round_trip(ts: float) -> None:
    assert parse_iso(iso(ts)) == pytest.approx(ts, abs=1e-6)


@pytest.mark.parametrize("text", ["2026-10-22T10:00:00.123Z", "2026-10-22T23:59:59.999Z"])
def test_parse_iso_then_iso_is_identity(text: str) -> None:
    ts = parse_iso(text)
    assert ts is not None
    assert iso(ts) == text


def test_offset_and_z_forms_are_the_same_instant() -> None:
    assert parse_iso("2026-10-22T10:00:00.250Z") == parse_iso("2026-10-22T10:00:00.250+00:00")


# ---------------------------------------------------------------- UTC days


def test_utc_day() -> None:
    assert utc_day(T0) == "20261022"
    assert utc_day(MIDNIGHT) == "20261022"
    assert utc_day(MIDNIGHT - 0.001) == "20261021"
    assert utc_day(MIDNIGHT + DAY) == "20261023"


def test_day_start() -> None:
    assert day_start(T0) == MIDNIGHT
    assert day_start(MIDNIGHT) == MIDNIGHT
    assert day_start(MIDNIGHT - 0.001) == MIDNIGHT - DAY
    assert parse_iso("2026-10-22T00:00:00Z") == MIDNIGHT


def test_days_are_utc_regardless_of_local_timezone(tehran_local_time: None) -> None:
    late_evening = MIDNIGHT + 22 * 3600  # 22:00Z = 01:30 next day in Tehran
    assert utc_day(late_evening) == "20261022"
    assert day_start(late_evening) == MIDNIGHT
    assert iso(late_evening) == "2026-10-22T22:00:00.000Z"


# ---------------------------------------------------------------- clocks


def test_sim_clock() -> None:
    clock = SimClock()
    assert clock.now() == 0.0
    clock.set(T0)
    assert clock.now() == T0
    clock.advance(2.5)
    assert clock.now() == T0 + 2.5
    assert isinstance(SimClock(1).now(), float)


def test_sim_clock_fixture_starts_at_t0(sim_clock: SimClock) -> None:
    assert sim_clock.now() == parse_iso("2026-10-22T10:00:00Z")
    sim_clock.advance(60)
    assert iso(sim_clock.now()) == "2026-10-22T10:01:00.000Z"


def test_system_clock_reads_wall_time() -> None:
    before = time.time()
    now = SystemClock().now()
    assert before <= now <= time.time()
