"""Feature engine (PRD-03 §2): every number a strategy, the risk manager or the LLM looks at.

Pure functions over book snapshots and 1-minute candles, plus one small piece of state (the
rolling cross-book deviation history used for ``dev_zscore``) that is serialisable so replay is
exact. All arithmetic is ``Decimal``; outputs are Decimals (``None`` when not computable).

Conventions (see PRD-03):
* ``spread_pct = (ask - bid) / mid``.
* Cross-book for coin X (needs XTMN, XUSDT, USDTTMN):
  ``implied_mid_usdt = mid(XTMN) / mid(USDTTMN)``, ``dev_mid_pct = implied / mid(XUSDT) - 1``
  (positive => TMN book expensive);
  ``edge_tmn_cheap_pct = bid(XUSDT) * bid(USDTTMN) / ask(XTMN) - 1`` (buy TMN book, sell USDT book);
  ``edge_usdt_cheap_pct = bid(XTMN) / (ask(XUSDT) * ask(USDTTMN)) - 1`` (buy USDT book, sell TMN book).
* 15-minute features use completed 15-minute bars resampled from 1-minute candles.
* Lagged returns are looked up **by time**, not by candle count: ``ret_Xm`` compares the newest close
  with the close of the last 1-minute bar at or before ``newest bar - X min`` (None when that bar is
  missing or more than one bar older), so a minute without a candle never stretches ``ret_24h`` to
  25 h. ``realized_vol_1h_pct`` uses only consecutive 1-minute bars of the last hour.
* ``dev_zscore`` history: a deviation is added only when all three books are fresh
  (``engine.stale_data_s``) and at least one of them changed since the last sample, so a stalled
  feed does not fill the hour with copies of one stale deviation (which would shrink the std and
  inflate z). The current deviation is still scored against the clean history.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wta.config import Config
from wta.models import BookSnapshot, Candle, MarketInfo
from wta.utils.decimalutil import D

USDTTMN = "USDTTMN"
ZERO = Decimal(0)
ONE = Decimal(1)
BAR_S = 60  # 1-minute candles


# ---------------------------------------------------------------- small math helpers


def resample(candles: list[Candle], minutes: int, now: float | None = None) -> list[Candle]:
    """Aggregate 1-minute candles into ``minutes`` bars aligned to epoch multiples.

    When ``now`` is given, the still-open bar (``ts + minutes*60 > now``) is dropped.
    """
    span = minutes * 60
    out: list[Candle] = []
    cur: dict[str, Any] | None = None
    for c in sorted(candles, key=lambda x: x.ts):
        bar = c.ts // span * span
        if cur is None or cur["ts"] != bar:
            if cur is not None:
                out.append(Candle(symbol=c.symbol, resolution=str(minutes), **cur))
            cur = {"ts": bar, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
        else:
            cur["h"] = max(cur["h"], c.h)
            cur["l"] = min(cur["l"], c.l)
            cur["c"] = c.c
            cur["v"] = cur["v"] + c.v
    if cur is not None:
        out.append(Candle(symbol=candles[-1].symbol if candles else "", resolution=str(minutes), **cur))
    if now is not None:
        out = [c for c in out if c.ts + span <= now]
    return out


def ema(values: list[Decimal], period: int) -> Decimal | None:
    """Classic EMA seeded with the SMA of the first ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    k = D(2) / D(period + 1)
    e = sum(values[:period], ZERO) / period
    for v in values[period:]:
        e = v * k + e * (ONE - k)
    return e


def atr(bars: list[Candle], period: int) -> Decimal | None:
    """Wilder ATR over completed bars."""
    if len(bars) < period + 1:
        return None
    trs: list[Decimal] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    a = sum(trs[:period], ZERO) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def mean_std(values: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    n = len(values)
    if n < 2:
        return None
    m = sum(values, ZERO) / n
    var = sum(((v - m) ** 2 for v in values), ZERO) / (n - 1)
    return m, var.sqrt()


def median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def ret(closes: list[Decimal], lag: int) -> Decimal | None:
    """Return over ``lag`` *bars* (index based; candle features use ``ret_by_time``)."""
    if len(closes) <= lag or closes[-1 - lag] == 0:
        return None
    return closes[-1] / closes[-1 - lag] - ONE


def ret_by_time(candles: list[Candle], lag_s: float, bar_s: int = BAR_S) -> Decimal | None:
    """Close-to-close return over ``lag_s`` seconds of time-sorted ``candles``.

    The reference is the last bar with ``ts <= newest.ts - lag_s``; None when there is none, when it
    is more than one bar older than that target (a gap: the return would span a longer horizon), or
    when its close is 0.
    """
    if not candles:
        return None
    last = candles[-1]
    target = last.ts - lag_s
    i = bisect_right([c.ts for c in candles], target) - 1
    if i < 0:
        return None
    ref = candles[i]
    if ref.ts < target - bar_s or ref.c == 0:
        return None
    return last.c / ref.c - ONE


# ---------------------------------------------------------------- feature sets


def book_features(book: BookSnapshot | None, now: float, quote_to_tmn: Decimal | None) -> dict[str, Any]:
    if book is None or book.mid is None:
        return {"bid": None, "ask": None, "mid": None, "spread_pct": None, "book_age_s": None}
    mid = book.mid
    lo, hi = mid * D("0.997"), mid * D("1.003")
    depth_bid = sum((lv.price * lv.qty for lv in book.bids if lv.price >= lo), ZERO)
    depth_ask = sum((lv.price * lv.qty for lv in book.asks if lv.price <= hi), ZERO)
    qb = sum((lv.qty for lv in book.bids[:5]), ZERO)
    qa = sum((lv.qty for lv in book.asks[:5]), ZERO)
    conv = quote_to_tmn if quote_to_tmn is not None else None
    return {
        "bid": book.best_bid,
        "ask": book.best_ask,
        "bid_qty": book.best_bid_qty,
        "ask_qty": book.best_ask_qty,
        "mid": mid,
        "spread_pct": book.spread_pct,
        "book_age_s": D(round(now - book.ts, 3)),
        "depth_bid_tmn_0p3": depth_bid * conv if conv is not None else None,
        "depth_ask_tmn_0p3": depth_ask * conv if conv is not None else None,
        "imbalance": (qb - qa) / (qb + qa) if qb + qa > 0 else None,
    }


def candle_features(candles_1m: list[Candle], now: float, cfg: Config) -> dict[str, Any]:
    """Return/volatility/trend features from 1-minute candles (last ~25 h is plenty)."""
    s2 = cfg.strategies.s2_usdtalloc
    s3 = cfg.strategies.s3_momentum
    out: dict[str, Any] = {}
    one = sorted(candles_1m, key=lambda c: c.ts)
    out["ret_1m"] = ret_by_time(one, 60)
    out["ret_5m"] = ret_by_time(one, 5 * 60)
    out["ret_15m"] = ret_by_time(one, 15 * 60)
    out["ret_1h"] = ret_by_time(one, 3600)
    out["ret_24h"] = ret_by_time(one, 86400)
    # 1-minute returns of the last hour, between consecutive bars only (a gap is not a 1-minute move)
    start = one[-1].ts - 3600 if one else 0
    rets = [
        cur.c / prev.c - ONE
        for prev, cur in zip(one, one[1:], strict=False)
        if cur.ts > start and cur.ts - prev.ts == BAR_S and prev.c
    ]
    ms = mean_std(rets) if len(rets) >= 10 else None
    out["realized_vol_1h_pct"] = ms[1] * D(60).sqrt() if ms else None

    bars = resample(one, 15, now=now)
    out["bars_15m"] = len(bars)
    c15 = [b.c for b in bars]
    out["ema20_15m"] = ema(c15, s2.ema_fast)
    out["ema60_15m"] = ema(c15, s2.ema_slow)
    a = atr(bars, s3.atr_period)
    out["atr_15m"] = a
    out["atr_15m_pct"] = a / bars[-1].c if a is not None and bars and bars[-1].c else None
    lb = s3.breakout_lookback
    if len(bars) >= lb + 1:
        prev = bars[-lb - 1 : -1]
        out["hh20_15m"] = max(b.h for b in prev)
        mean_vol = sum((b.v for b in prev), ZERO) / lb
        out["vol_ratio_15m"] = bars[-1].v / mean_vol if mean_vol > 0 else None
        out["last_close_15m"] = bars[-1].c
        out["last_bar_ts_15m"] = bars[-1].ts
    else:
        out["hh20_15m"] = None
        out["vol_ratio_15m"] = None
        out["last_close_15m"] = bars[-1].c if bars else None
        out["last_bar_ts_15m"] = bars[-1].ts if bars else None
    # ATR% history (24h of 15m bars) for the S2 "chaos" rule: median of rolling ATR%.
    hist: list[Decimal] = []
    for end in range(max(s3.atr_period + 1, len(bars) - 96), len(bars) + 1):
        sub = bars[:end]
        a_i = atr(sub, s3.atr_period)
        if a_i is not None and sub[-1].c:
            hist.append(a_i / sub[-1].c)
    out["atr_15m_pct_median_24h"] = median(hist)
    e20, e60 = out["ema20_15m"], out["ema60_15m"]
    if e20 is not None and e60 is not None and e60 != 0:
        gap = (e20 - e60) / e60
        out["ema_gap_pct"] = gap
        out["trend_flag"] = (1 if gap > 0 else -1) if abs(gap) > s2.trend_flag_min_gap_pct else 0
    else:
        out["ema_gap_pct"] = None
        out["trend_flag"] = 0
    return out


def cross_features(
    coin: str,
    books: dict[str, BookSnapshot],
    markets: dict[str, MarketInfo],
    cfg: Config,
    now: float,
) -> dict[str, Any] | None:
    t_sym, u_sym = f"{coin}TMN", f"{coin}USDT"
    tb, ub, dollar = books.get(t_sym), books.get(u_sym), books.get(USDTTMN)
    tm, um = markets.get(t_sym), markets.get(u_sym)
    if not (tb and ub and dollar and tm and um):
        return None
    if None in (tb.best_bid, tb.best_ask, ub.best_bid, ub.best_ask, dollar.best_bid, dollar.best_ask):
        return None
    s1 = cfg.strategies.s1_crossbook
    u_bid, u_ask, u_mid = dollar.best_bid, dollar.best_ask, dollar.mid
    implied = tb.mid / u_mid  # type: ignore[operator]
    dev = implied / ub.mid - ONE  # type: ignore[operator]
    edge_tmn_cheap = ub.best_bid * u_bid / tb.best_ask - ONE  # type: ignore[operator]
    edge_usdt_cheap = tb.best_bid / (ub.best_ask * u_ask) - ONE  # type: ignore[operator]
    leg1_t = tm.taker_fee if s1.leg1_taker else tm.maker_fee
    leg1_u = um.taker_fee if s1.leg1_taker else um.maker_fee
    return {
        "coin": coin,
        "tmn_symbol": t_sym,
        "usdt_symbol": u_sym,
        "u_bid": u_bid,
        "u_ask": u_ask,
        "u_mid": u_mid,
        "implied_mid_usdt": implied,
        "dev_mid_pct": dev,
        "edge_tmn_cheap_pct": edge_tmn_cheap,
        "edge_usdt_cheap_pct": edge_usdt_cheap,
        "cost_lockin_tmn_cheap": leg1_t + um.taker_fee,
        "cost_lockin_usdt_cheap": leg1_u + tm.taker_fee,
        "cost_lockin_tmn_cheap_maker": tm.maker_fee + um.taker_fee,
        "cost_lockin_usdt_cheap_maker": um.maker_fee + tm.taker_fee,
        "tmn_book_age_s": D(round(now - tb.ts, 3)),
        "usdt_book_age_s": D(round(now - ub.ts, 3)),
        "dollar_book_age_s": D(round(now - dollar.ts, 3)),
    }


@dataclass
class FeatureSnapshot:
    """Features for one fast tick. Keys: symbol -> dict; ``cross[coin]``; ``dollar``."""

    ts: float
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    cross: dict[str, dict[str, Any]] = field(default_factory=dict)
    dollar: dict[str, Any] = field(default_factory=dict)

    def compact(self, symbols: list[str] | None = None, coins: list[str] | None = None) -> dict[str, Any]:
        """JSON-safe subset for the decision log / LLM (Decimals -> str, sorted keys)."""
        out: dict[str, Any] = {"ts": self.ts}
        if symbols is None:
            symbols = sorted(self.symbols)
        out["symbols"] = {s: _jsonable(self.symbols[s]) for s in sorted(symbols) if s in self.symbols}
        if coins is None:
            coins = sorted(self.cross)
        out["cross"] = {c: _jsonable(self.cross[c]) for c in sorted(coins) if c in self.cross}
        out["dollar"] = _jsonable(self.dollar)
        return out


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in sorted(d):
        v = d[k]
        if isinstance(v, Decimal):
            out[k] = format(v.normalize(), "f") if v == v.to_integral_value() else _fmt_dec(v)
        else:
            out[k] = v
    return out


def _fmt_dec(v: Decimal) -> str:
    """Stable, compact rendering: at most 10 significant digits after the point."""
    q = v.quantize(Decimal("1e-10")) if abs(v) < 1 else v.quantize(Decimal("1e-6"))
    return format(q.normalize(), "f")


class FeatureEngine:
    """Computes a FeatureSnapshot per tick and keeps the rolling dev history for ``dev_zscore``."""

    def __init__(self, cfg: Config, markets: dict[str, MarketInfo]) -> None:
        self.cfg = cfg
        self.markets = markets
        self._dev_hist: dict[str, deque[tuple[float, Decimal]]] = {}
        # per coin: the (TMN, USDT, USDTTMN) book timestamps of the last sample taken
        self._dev_books: dict[str, tuple[float, float, float]] = {}
        # candle features only change when a 1-minute candle changes or a 15m bar completes
        self._candle_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    def _candle_features_cached(self, sym: str, candles: list[Candle], now: float) -> dict[str, Any]:
        """Candle features, recomputed only when the candles or the current 15-minute bar change.

        The key holds the whole last candle (not just its close): a late update of the last minute
        can change its high/low/volume — and so a just-completed 15-minute bar — with the same close.
        """
        last = candles[-1] if candles else None
        key = (len(candles), last, int(now // 900))
        hit = self._candle_cache.get(sym)
        if hit is not None and hit[0] == key:
            return hit[1]
        f = candle_features(candles, now, self.cfg)
        self._candle_cache[sym] = (key, f)
        return f

    def compute(
        self,
        now: float,
        books: dict[str, BookSnapshot],
        candles_1m: dict[str, list[Candle]],
        symbols: list[str],
    ) -> FeatureSnapshot:
        snap = FeatureSnapshot(ts=now)
        dollar_book = books.get(USDTTMN)
        u_bid = dollar_book.best_bid if dollar_book else None
        for sym in sorted(symbols):
            m = self.markets.get(sym)
            conv = Decimal(1) if (m and m.quote == "TMN") else u_bid
            f = book_features(books.get(sym), now, conv)
            f.update(self._candle_features_cached(sym, candles_1m.get(sym, []), now))
            snap.symbols[sym] = f
        snap.dollar = dict(snap.symbols.get(USDTTMN, {}))
        for coin in sorted(self.cfg.strategies.s1_crossbook.coins):
            cf = cross_features(coin, books, self.markets, self.cfg, now)
            if cf is None:
                continue
            cf.update(self._zscore(coin, now, cf["dev_mid_pct"], self._sample_books(coin, books, now)))
            snap.cross[coin] = cf
        return snap

    def _sample_books(self, coin: str, books: dict[str, BookSnapshot], now: float) -> bool:
        """True when this tick's deviation may join the z-score history: all three books fresh and
        at least one of them new since the last sample (``cross_features`` checked they exist)."""
        stamps = (books[f"{coin}TMN"].ts, books[f"{coin}USDT"].ts, books[USDTTMN].ts)
        limit = self.cfg.engine.stale_data_s
        if any(now - ts > limit for ts in stamps) or self._dev_books.get(coin) == stamps:
            return False
        self._dev_books[coin] = stamps
        return True

    def _zscore(self, coin: str, now: float, dev: Decimal, sample: bool = True) -> dict[str, Any]:
        window = self.cfg.strategies.s1_crossbook.dev_window_s
        hist = self._dev_hist.setdefault(coin, deque())
        while hist and hist[0][0] < now - window:
            hist.popleft()
        prior = [v for _, v in hist]
        ms = mean_std(prior) if len(prior) >= 30 else None
        if sample:
            hist.append((now, dev))
        if ms is None or ms[1] == 0:
            return {"dev_mean_1h": ms[0] if ms else None, "dev_std_1h": ms[1] if ms else None, "dev_zscore": None,
                    "dev_samples": len(prior)}
        return {"dev_mean_1h": ms[0], "dev_std_1h": ms[1], "dev_zscore": (dev - ms[0]) / ms[1], "dev_samples": len(prior)}

    # ------------------------------------------------------------ persistence (replay)

    def state(self) -> dict[str, Any]:
        """``{coin: [[ts, dev], ...]}`` (the book stamps of the last sample are not kept: after a
        restart the first fresh sample is always taken)."""
        return {c: [[t, str(v)] for t, v in h] for c, h in sorted(self._dev_hist.items())}

    def load_state(self, state: dict[str, Any] | None) -> None:
        self._dev_hist = {c: deque((float(t), D(v)) for t, v in rows) for c, rows in (state or {}).items()}
        self._dev_books = {}
