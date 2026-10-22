"""Candle backfill paging, resampling and the pandas view."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from wta.data.candles import backfill, backfill_windows, resample, resolution_seconds, to_frame
from wta.data.store import Store
from wta.models import Candle

DAY0 = 1_792_627_200  # 2026-10-22T00:00:00Z, a multiple of every bar size used here


def _bars(start: int, n: int, symbol: str = "BTCUSDT", step_s: int = 60) -> list[Candle]:
    """n one-minute bars; bar i has o=100+i, h=o+2, l=o-1, c=o+1, v=i+0.5 (all exact Decimals)."""
    out = []
    for i in range(n):
        o = Decimal(100 + i)
        out.append(
            Candle(
                symbol=symbol,
                resolution=str(step_s // 60),
                ts=start + i * step_s,
                o=o,
                h=o + 2,
                l=o - 1,
                c=o + 1,
                v=Decimal(i) + Decimal("0.5"),
            )
        )
    return out


class FakeRest:
    """Returns one-minute bars covering [from_ts, to_ts]; can fail the first N calls."""

    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[tuple[str, str, int, int]] = []
        self.fail_first = fail_first

    async def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> list[Candle]:
        self.calls.append((symbol, resolution, from_ts, to_ts))
        if len(self.calls) <= self.fail_first:
            raise ConnectionError("api unreachable")
        first = from_ts - from_ts % 60
        return _bars(first, (to_ts - first) // 60 + 1, symbol=symbol)


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_resolution_seconds() -> None:
    assert resolution_seconds("1") == 60
    assert resolution_seconds("15") == 900
    assert resolution_seconds("1D") == 86_400 and resolution_seconds("D") == 86_400
    with pytest.raises(ValueError):
        resolution_seconds("1W")


def test_resample_aggregates_and_drops_partial_bar() -> None:
    bars = _bars(DAY0, 32)  # 00:00 .. 00:31 -> two full 15-min buckets + 2 bars of the third
    out = resample(bars, 15)
    assert [c.ts for c in out] == [DAY0, DAY0 + 900]
    first = out[0]
    assert first.resolution == "15" and first.symbol == "BTCUSDT"
    assert first.o == Decimal(100) and first.c == Decimal(115)  # first open, last close
    assert first.h == Decimal(116) and first.l == Decimal(99)  # max high, min low
    assert first.v == sum((Decimal(i) + Decimal("0.5") for i in range(15)), Decimal(0))
    assert out[1].o == Decimal(115) and out[1].c == Decimal(130)

    with_partial = resample(bars, 15, include_partial=True)
    assert [c.ts for c in with_partial] == [DAY0, DAY0 + 900, DAY0 + 1800]
    assert with_partial[2].o == Decimal(130) and with_partial[2].v == Decimal("62")


def test_resample_aligns_to_epoch_and_keeps_closed_buckets_with_gaps() -> None:
    bars = _bars(DAY0 + 7 * 60, 23)  # 00:07 .. 00:29: bucket 00:00 starts mid-way, 00:15 closes at 00:29
    del bars[10]  # a minute without trades inside the second bucket
    out = resample(bars, 15)
    assert [c.ts for c in out] == [DAY0, DAY0 + 900]
    assert out[0].o == Decimal(100)  # first bar present in the bucket
    assert resample(list(reversed(bars)), 15) == out  # input order does not matter


def test_resample_complete_last_bucket_is_kept() -> None:
    assert [c.ts for c in resample(_bars(DAY0, 30), 15)] == [DAY0, DAY0 + 900]
    assert resample([], 15) == []


def test_resample_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        resample(_bars(DAY0, 3), 0)
    with pytest.raises(ValueError):
        resample(_bars(DAY0, 3, step_s=900), 5)  # cannot make 5-min bars from 15-min bars
    with pytest.raises(ValueError):
        resample(_bars(DAY0, 2) + _bars(DAY0, 2, symbol="ETHUSDT"), 15)


def test_to_frame() -> None:
    frame = to_frame(_bars(DAY0, 3))
    assert list(frame.columns) == ["ts", "o", "h", "l", "c", "v"]
    assert len(frame) == 3 and frame["c"].iloc[-1] == 103.0
    assert str(frame.index.tz) == "UTC" and frame.index[0].timestamp() == DAY0
    assert to_frame([]).empty


def test_backfill_windows_cover_range_without_gaps() -> None:
    windows = backfill_windows(DAY0 - 2 * 86_400 - 100, DAY0)
    assert windows[0][0] == DAY0 - 2 * 86_400 - 100 and windows[-1][1] == DAY0
    assert all(b - a <= 86_400 for a, b in windows)
    assert all(prev[1] == nxt[0] for prev, nxt in zip(windows, windows[1:], strict=False))
    with pytest.raises(ValueError):
        backfill_windows(0, 10, window_s=90_000)


async def test_backfill_pages_one_day_windows_and_upserts(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    rest = FakeRest()
    totals = await backfill(rest, store, ["BTCUSDT", "ETHUSDT"], resolution="1", days=2, now=DAY0, sleep=FakeSleep())
    assert [c[:2] for c in rest.calls] == [("BTCUSDT", "1")] * 2 + [("ETHUSDT", "1")] * 2
    assert [c[2:] for c in rest.calls[:2]] == [(DAY0 - 2 * 86_400, DAY0 - 86_400), (DAY0 - 86_400, DAY0)]
    assert totals == {"BTCUSDT": 2 * 1441, "ETHUSDT": 2 * 1441}
    stored = store.load_candles("BTCUSDT", "1")
    assert len(stored) == 2 * 1440 + 1  # the shared boundary bar is stored once
    assert [c.ts for c in stored] == sorted({c.ts for c in stored})

    again = await backfill(rest, store, ["BTCUSDT"], days=2, now=DAY0, sleep=FakeSleep())
    assert again["BTCUSDT"] == 2 * 1441
    assert len(store.load_candles("BTCUSDT", "1")) == 2 * 1440 + 1  # idempotent
    store.close()


async def test_backfill_small_windows_and_retries(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    rest = FakeRest(fail_first=2)
    sleep = FakeSleep()
    totals = await backfill(rest, store, ["BTCUSDT"], days=0.25, now=DAY0, sleep=sleep, window_s=3 * 3600)
    windows = [c[2:] for c in rest.calls]
    assert windows == [(DAY0 - 6 * 3600, DAY0 - 3 * 3600)] * 3 + [(DAY0 - 3 * 3600, DAY0)]
    assert sleep.delays == [1.0, 2.0]
    assert totals["BTCUSDT"] == 2 * 181
    store.close()


async def test_backfill_skips_a_window_that_keeps_failing(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    rest = FakeRest(fail_first=3)  # retries=1 -> window 1 fails twice and is skipped, window 2 fails once
    totals = await backfill(rest, store, ["BTCUSDT"], days=2, now=DAY0, sleep=FakeSleep(), retries=1)
    assert len(rest.calls) == 4
    assert totals["BTCUSDT"] == 1441
    assert store.load_candles("BTCUSDT", "1")[0].ts == DAY0 - 86_400
    store.close()
