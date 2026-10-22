"""Deterministic synthetic market feed (offline demo / tests / engine smoke runs).

Used ONLY when the real Wallex API is unreachable; every snapshot is labelled
``source="synthetic"`` and every log line written while it runs says so. It is not evidence of
strategy performance — just a realistic-looking stream that exercises the full engine:

* USDTTMN mid follows a geometric random walk (the "dollar").
* Each coin's USDT mid follows its own random walk (correlated with BTC).
* Each coin's TMN mid = USDT mid x USDTTMN mid x (1 + d), where d is a mean-reverting
  lag/dislocation process with occasional jumps (TMN book lagging a fast move) — so cross-book
  opportunities (S1) appear at a plausible, tunable rate.
* Ten levels per side with the reference spreads from ``configs/markets_snapshot.yaml``.
* 1-minute candles are aggregated from the mids and handed to ``on_candle`` (e.g. Store.upsert).
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from wta.exchange.base import MarketDataBus
from wta.models import BookSnapshot, Candle, Level, MarketInfo, Trade
from wta.utils.decimalutil import D, quantize_down, quantize_up
from wta.utils.timeutil import Clock, SystemClock

USDTTMN = "USDTTMN"


@dataclass
class SyntheticParams:
    seed: int = 7
    step_s: float = 1.0
    dollar_vol_per_sqrt_min: float = 0.0004  # 0.04% per sqrt(minute) ~ 0.3% per hour
    coin_vol_per_sqrt_min: float = 0.0009
    coin_beta_to_btc: float = 0.7
    lag_mean_revert_per_s: float = 1 / 45  # dislocation half-life ~30 s
    lag_noise_per_sqrt_s: float = 0.00004
    jump_rate_per_hour: float = 1.0  # dislocation episodes per coin per hour
    jump_size_pct: float = 0.006
    level_notional_tmn: tuple[float, float] = (2e6, 2e7)


@dataclass
class _CandleAcc:
    ts: int
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


@dataclass
class SyntheticFeed:
    markets: dict[str, MarketInfo]
    symbols: list[str]
    bus: MarketDataBus
    reference_mids: dict[str, Decimal]
    reference_spreads: dict[str, Decimal]
    clock: Clock = field(default_factory=SystemClock)
    params: SyntheticParams = field(default_factory=SyntheticParams)
    on_candle: Callable[[list[Candle]], None] | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.params.seed)
        self.symbols = sorted(self.symbols)
        self._usdt_mid = float(self.reference_mids.get(USDTTMN, D("266739")))
        self._coin_usdt: dict[str, float] = {}
        self._lag: dict[str, float] = {}
        for sym in self.symbols:
            m = self.markets.get(sym)
            if m is None or m.quote != "USDT":
                continue
            self._coin_usdt[m.base] = float(self.reference_mids.get(sym, D("1")))
        for base in self._coin_usdt:
            self._lag[base] = 0.0
        self._candles: dict[str, _CandleAcc] = {}
        self.ticks = 0

    # ------------------------------------------------------------ simulation

    def step(self, dt: float) -> None:
        p = self.params
        z_dollar = self.rng.gauss(0, 1)
        self._usdt_mid *= math.exp(p.dollar_vol_per_sqrt_min * math.sqrt(dt / 60) * z_dollar)
        z_btc = self.rng.gauss(0, 1)
        for base in sorted(self._coin_usdt):
            z_own = self.rng.gauss(0, 1)
            z = z_btc if base == "BTC" else p.coin_beta_to_btc * z_btc + math.sqrt(1 - p.coin_beta_to_btc**2) * z_own
            self._coin_usdt[base] *= math.exp(p.coin_vol_per_sqrt_min * math.sqrt(dt / 60) * z)
            lag = self._lag[base]
            lag += -lag * p.lag_mean_revert_per_s * dt + p.lag_noise_per_sqrt_s * math.sqrt(dt) * self.rng.gauss(0, 1)
            if self.rng.random() < p.jump_rate_per_hour * dt / 3600:
                lag += self.rng.choice((-1, 1)) * p.jump_size_pct * (0.6 + 0.8 * self.rng.random())
            self._lag[base] = lag

    def mid(self, symbol: str) -> float | None:
        m = self.markets.get(symbol)
        if symbol == USDTTMN:
            return self._usdt_mid
        if m is None or m.base not in self._coin_usdt:
            return None
        usdt = self._coin_usdt[m.base]
        if m.quote == "USDT":
            return usdt
        return usdt * self._usdt_mid * (1 + self._lag[m.base])

    def book(self, symbol: str, now: float) -> BookSnapshot | None:
        m = self.markets.get(symbol)
        mid = self.mid(symbol)
        if m is None or mid is None:
            return None
        spread = float(self.reference_spreads.get(symbol, D("0.001")))
        half = mid * spread / 2
        tick = float(m.tick)
        lo_n, hi_n = self.params.level_notional_tmn
        quote_to_tmn = 1.0 if m.quote == "TMN" else self._usdt_mid
        bids: list[Level] = []
        asks: list[Level] = []
        for i in range(10):
            off = half + i * max(tick, mid * 0.0002)
            bp = quantize_down(D(mid - off), m.tick_decimals)
            ap = quantize_up(D(mid + off), m.tick_decimals)
            if ap <= bp:
                ap = bp + m.tick
            notional_tmn = lo_n + (hi_n - lo_n) * self.rng.random()
            qb = quantize_down(D(notional_tmn / quote_to_tmn / float(bp)), m.step_decimals) if bp > 0 else D(0)
            qa = quantize_down(D(notional_tmn / quote_to_tmn / float(ap)), m.step_decimals)
            if qb > 0 and bp > 0:
                bids.append(Level(price=bp, qty=qb))
            if qa > 0:
                asks.append(Level(price=ap, qty=qa))
        return BookSnapshot(symbol=symbol, ts=now, source="synthetic", bids=bids, asks=asks)

    def _update_candles(self, symbol: str, price: Decimal, now: float) -> list[Candle]:
        bar_ts = int(now // 60 * 60)
        acc = self._candles.get(symbol)
        closed: list[Candle] = []
        vol = D(round(self.rng.random() * 5, 4))
        if acc is None or acc.ts != bar_ts:
            if acc is not None:
                closed.append(Candle(symbol=symbol, resolution="1", ts=acc.ts, o=acc.o, h=acc.h, l=acc.l, c=acc.c, v=acc.v))
            self._candles[symbol] = _CandleAcc(bar_ts, price, price, price, price, vol)
        else:
            acc.h = max(acc.h, price)
            acc.l = min(acc.l, price)
            acc.c = price
            acc.v += vol
        cur = self._candles[symbol]
        closed.append(Candle(symbol=symbol, resolution="1", ts=cur.ts, o=cur.o, h=cur.h, l=cur.l, c=cur.c, v=cur.v))
        return closed

    def publish(self, now: float) -> int:
        """Publish one snapshot per symbol (+ a synthetic trade and candle update). Returns count."""
        n = 0
        candles: list[Candle] = []
        for sym in self.symbols:
            b = self.book(sym, now)
            if b is None or b.mid is None:
                continue
            self.bus.publish_book(b)
            n += 1
            m = self.markets[sym]
            px = quantize_down(b.mid, m.tick_decimals)
            side = "BUY" if self.rng.random() < 0.5 else "SELL"
            qty = b.best_ask_qty if side == "BUY" else b.best_bid_qty
            self.bus.publish_trade(Trade(symbol=sym, ts=now, price=px, qty=(qty or D(0)) / 10, side=side))
            candles.extend(self._update_candles(sym, px, now))
        if self.on_candle and candles:
            self.on_candle(candles)
        self.bus.mark_source("synthetic", True, now)
        self.ticks += 1
        return n

    async def run(self, stop_event: asyncio.Event | None = None, sleep: Callable = asyncio.sleep) -> None:
        last = self.clock.now()
        self.publish(last)
        while not (stop_event and stop_event.is_set()):
            await sleep(self.params.step_s)
            now = self.clock.now()
            self.step(max(now - last, 0.0))
            last = now
            self.publish(now)
