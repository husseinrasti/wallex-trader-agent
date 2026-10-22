"""Candle utilities: historical backfill, resampling and a pandas view (PRD-01 §7, PRD-05 §1).

* ``backfill`` pages ``/v1/udf/history`` in windows of at most one day and upserts into the store
  (idempotent, so it can be re-run or overlap the recorder's own candles).
* ``resample`` builds 5/15/60-minute bars from 1-minute bars so every resolution derives from one
  stored truth. Bars are aligned to epoch multiples of the target size.
* ``to_frame`` is for analysis and plots only — it converts to float; never use it for order fields.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pandas as pd

from wta.data.store import Store
from wta.models import Candle

log = logging.getLogger(__name__)

DAY_S = 86_400
Sleep = Callable[[float], Awaitable[Any]]


def resolution_seconds(resolution: str) -> int:
    """UDF resolution -> bar length in seconds: ``"1"`` -> 60, ``"15"`` -> 900, ``"1D"``/``"D"`` -> 86400."""
    res = resolution.strip().upper()
    if res.endswith("D"):
        return int(res[:-1] or 1) * DAY_S
    if res.isdigit():
        return int(res) * 60
    raise ValueError(f"unsupported resolution {resolution!r}")


def backfill_windows(from_ts: int, to_ts: int, window_s: int = DAY_S) -> list[tuple[int, int]]:
    """Split ``[from_ts, to_ts]`` into consecutive windows of at most ``window_s`` seconds.

    Adjacent windows share their boundary second; the upsert makes that overlap harmless,
    whereas a gap would silently lose a bar.
    """
    if window_s <= 0 or window_s > DAY_S:
        raise ValueError("window_s must be in (0, 86400]")
    windows = []
    start = from_ts
    while start < to_ts:
        end = min(start + window_s, to_ts)
        windows.append((start, end))
        start = end
    return windows


async def backfill(
    rest: Any,
    store: Store,
    symbols: list[str],
    resolution: str = "1",
    days: float = 30,
    now: float | None = None,
    sleep: Sleep = asyncio.sleep,
    window_s: int = DAY_S,
    retries: int = 3,
) -> dict[str, int]:
    """Fetch ``days`` of history for each symbol and upsert it. Returns candles stored per symbol.

    ``rest`` needs ``async get_candles(symbol, resolution, from_ts, to_ts) -> list[Candle]``.
    A failing window is retried ``retries`` times with backoff (1, 2, 4 s), then skipped and logged.
    """
    end = int(time.time() if now is None else now)
    start = end - int(days * DAY_S)
    windows = backfill_windows(start, end, window_s)
    totals: dict[str, int] = {}
    for symbol in symbols:
        total = 0
        for i, (a, b) in enumerate(windows, start=1):
            candles = await _fetch_window(rest, symbol, resolution, a, b, sleep, retries)
            if candles is None:
                continue
            total += store.upsert_candles(candles)
            log.info("backfill %s res=%s window %d/%d: %d candles", symbol, resolution, i, len(windows), len(candles))
        store.flush()
        totals[symbol] = total
        log.info("backfill %s res=%s done: %d candles over %.1f days", symbol, resolution, total, days)
    return totals


async def _fetch_window(
    rest: Any, symbol: str, resolution: str, a: int, b: int, sleep: Sleep, retries: int
) -> list[Candle] | None:
    for attempt in range(retries + 1):
        try:
            return list(await rest.get_candles(symbol, resolution, a, b))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network/API errors: retry, then skip this window
            if attempt == retries:
                log.error("backfill %s [%d, %d] skipped after %d attempts: %s", symbol, a, b, attempt + 1, e)
                return None
            delay = 2.0**attempt
            log.warning("backfill %s [%d, %d] failed (%s); retrying in %.0fs", symbol, a, b, e, delay)
            await sleep(delay)
    return None


def resample(candles: list[Candle], minutes: int, include_partial: bool = False) -> list[Candle]:
    """Aggregate bars of one symbol/resolution into ``minutes``-minute bars.

    Bucket = ``ts - ts % (minutes * 60)``; o=first, h=max, l=min, c=last, v=sum. The last bucket is
    dropped unless its final input bar closes it (it is still forming) or ``include_partial`` is set.
    Missing input bars (no trades) inside a bucket do not make it partial.
    """
    if not candles:
        return []
    if len({(c.symbol, c.resolution) for c in candles}) > 1:
        raise ValueError("resample expects candles of a single symbol and resolution")
    src_s = resolution_seconds(candles[0].resolution)
    out_s = minutes * 60
    if minutes <= 0 or out_s < src_s or out_s % src_s:
        raise ValueError(f"cannot resample {candles[0].resolution} bars into {minutes}-minute bars")

    ordered = sorted(candles, key=lambda c: c.ts)
    out: list[Candle] = []
    group: list[Candle] = []
    for c in ordered:
        if group and _bucket(c.ts, out_s) != _bucket(group[0].ts, out_s):
            out.append(_merge(group, out_s, minutes))
            group = []
        group.append(c)
    last_bucket = _bucket(group[0].ts, out_s)
    if include_partial or group[-1].ts + src_s >= last_bucket + out_s:
        out.append(_merge(group, out_s, minutes))
    return out


def _bucket(ts: int, size_s: int) -> int:
    return ts - ts % size_s


def _merge(group: list[Candle], size_s: int, minutes: int) -> Candle:
    return Candle(
        symbol=group[0].symbol,
        resolution=str(minutes),
        ts=_bucket(group[0].ts, size_s),
        o=group[0].o,
        h=max(c.h for c in group),
        l=min(c.l for c in group),
        c=group[-1].c,
        v=sum((c.v for c in group), Decimal(0)),
    )


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Candles as a float DataFrame indexed by UTC bar time (analysis/plots only)."""
    columns = ["ts", "o", "h", "l", "c", "v"]
    if not candles:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC", name="time"))
    rows = [(c.ts, float(c.o), float(c.h), float(c.l), float(c.c), float(c.v)) for c in candles]
    frame = pd.DataFrame(rows, columns=columns)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame["ts"], unit="s", utc=True), name="time")
    return frame
