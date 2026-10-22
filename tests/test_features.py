"""Feature engine formulas (PRD-03 §2) on hand-computed fixtures.

Every expected value below is derived by hand from the literal prices in the fixture (the arithmetic
is written out next to the assertion), including the Overview §2.1 snapshot where the TMN books
imply BTCUSDT 82,818.4 vs the real mid 82,877.08 -> ``dev_mid_pct`` ≈ -0.07 %.
"""

from __future__ import annotations

import json
import statistics
from decimal import Decimal

import pytest

from wta.config import Config, load_config
from wta.features import engine as fe
from wta.features.engine import (
    FeatureEngine,
    FeatureSnapshot,
    atr,
    book_features,
    candle_features,
    cross_features,
    ema,
    mean_std,
    median,
    resample,
    ret,
)
from wta.models import BookSnapshot, Candle, Level, MarketInfo

from .conftest import T0, BookFactory

D = Decimal
assert T0 % 900 == 0  # the suite clock sits on a 15-minute boundary (10:00:00Z)


# ---------------------------------------------------------------- candle builders


def _c(ts: float, o: object, h: object, l: object, c: object, v: object = 1, symbol: str = "USDTTMN") -> Candle:  # noqa: E741
    return Candle(symbol=symbol, resolution="1", ts=int(ts), o=D(str(o)), h=D(str(h)), l=D(str(l)), c=D(str(c)),
                  v=D(str(v)))


def _flat_minutes(closes: list[object], start: float, symbol: str = "USDTTMN") -> list[Candle]:
    """One 1-minute candle per close with o=h=l=c (pure close series)."""
    return [_c(start + 60 * i, x, x, x, x, 1, symbol) for i, x in enumerate(closes)]


def _bars_as_minutes(bars: list[tuple[object, object, object, object, object]], start: float) -> list[Candle]:
    """Expand 15-minute bars (o, h, l, c, v_per_minute) into 15 one-minute candles each.

    Minute 0 carries the bar's o/h/l/c; minutes 1..14 sit flat on the close. Resampling gives back
    exactly (o, h, l, c, 15 x v_per_minute) as long as l <= c <= h.
    """
    out: list[Candle] = []
    for j, (o, h, l, c, v) in enumerate(bars):  # noqa: E741
        t = start + 900 * j
        out.append(_c(t, o, h, l, c, v))
        out.extend(_c(t + 60 * k, c, c, c, c, v) for k in range(1, 15))
    return out


@pytest.fixture
def small_ema_cfg() -> Config:
    """EMA 2/3 instead of 20/60 so the trend flag is hand-computable on four bars."""
    return load_config(overrides={"strategies.s2_usdtalloc.ema_fast": 2, "strategies.s2_usdtalloc.ema_slow": 3}, env={})


# ---------------------------------------------------------------- helpers


def test_ema_hand_computed() -> None:
    # SMA seed (1+2+3)/3 = 2, k = 2/(3+1) = 0.5: 4*0.5 + 2*0.5 = 3; 5*0.5 + 3*0.5 = 4
    assert ema([D(1), D(2), D(3), D(4), D(5)], 3) == D(4)
    assert ema([D(2)] * 3, 3) == D(2)
    assert ema([D(1), D(2)], 3) is None
    assert ema([D(1)], 0) is None


def _bar(ts: int, o: str, h: str, l: str, c: str) -> Candle:  # noqa: E741
    return Candle(symbol="X", resolution="15", ts=ts, o=D(o), h=D(h), l=D(l), c=D(c), v=D(1))


def test_wilder_atr_hand_computed() -> None:
    bars = [
        _bar(0, "9", "10", "8", "9"),
        _bar(1, "9", "11", "9", "10"),  # TR = max(2, |11-9|, |9-9|) = 2
        _bar(2, "10", "12", "9", "11"),  # TR = max(3, |12-10|, |9-10|) = 3
        _bar(3, "11", "11", "10", "10.5"),  # TR = max(1, |11-11|, |10-11|) = 1
        _bar(4, "12", "15", "12", "14"),  # gap up: TR = max(3, |15-10.5|, |12-10.5|) = 4.5
    ]
    assert atr(bars[:3], 3) is None  # needs period + 1 bars
    assert atr(bars[:4], 3) == D(2)  # seed = (2 + 3 + 1) / 3
    assert atr(bars, 3) == (D(2) * 2 + D("4.5")) / 3  # Wilder: (2*2 + 4.5) / 3 = 2.8333...


def test_small_helpers() -> None:
    assert ret([D(100), D(110)], 1) == D("0.1")
    assert ret([D(100)], 1) is None
    assert ret([D(0), D(1)], 1) is None
    assert median([]) is None
    assert median([D(3), D(1), D(2)]) == D(2)
    assert median([D(4), D(1), D(3), D(2)]) == D("2.5")
    assert mean_std([D(1)]) is None
    m, s = mean_std([D(2), D(4), D(4), D(4), D(5), D(5), D(7), D(9)])  # type: ignore[misc]
    assert m == D(5)
    assert s == (D(32) / 7).sqrt()  # sample std: sum of squares 32 over n-1 = 7


# ---------------------------------------------------------------- resample


def _ramp_minutes(start: float, n: int) -> list[Candle]:
    """Minute i: o=100+i, h=102+i, l=99+i, c=101+i, v=1+i."""
    return [_c(start + 60 * i, 100 + i, 102 + i, 99 + i, 101 + i, 1 + i) for i in range(n)]


def test_resample_aligned_bars_and_partial_dropped() -> None:
    minutes = _ramp_minutes(T0 - 1800, 35)  # 09:30 .. 10:04 -> bars 09:30, 09:45 and a partial 10:00
    every = resample(minutes, 15)
    assert [b.ts for b in every] == [T0 - 1800, T0 - 900, T0]
    b0, b1, b2 = every
    # minutes 0..14: o = first open, h = max high, l = min low, c = last close, v = sum
    assert (b0.o, b0.h, b0.l, b0.c, b0.v) == (D(100), D(116), D(99), D(115), D(120))  # 1+..+15 = 120
    assert (b1.o, b1.h, b1.l, b1.c, b1.v) == (D(115), D(131), D(114), D(130), D(345))  # 16+..+30
    assert (b2.o, b2.h, b2.l, b2.c, b2.v) == (D(130), D(136), D(129), D(135), D(165))  # 31+..+35
    assert all(b.resolution == "15" and b.symbol == "USDTTMN" for b in every)
    # with a clock, the still-open 10:00 bar is dropped ...
    assert [b.ts for b in resample(minutes, 15, now=T0 + 270)] == [T0 - 1800, T0 - 900]
    # ... until 10:15:00 exactly, when it is complete
    assert [b.ts for b in resample(minutes, 15, now=T0 + 900)] == [T0 - 1800, T0 - 900, T0]


def test_resample_alignment_unsorted_and_empty() -> None:
    minutes = _ramp_minutes(T0 - 1800 + 420, 10)  # starts at 09:37 -> still the 09:30 bar
    bars = resample(list(reversed(minutes)), 15)  # input order does not matter
    assert [b.ts for b in bars] == [T0 - 1800, T0 - 900]
    assert bars[0].o == D(100) and bars[0].c == D(108)  # minutes 09:37..09:44 (i = 0..7)
    assert resample([], 15) == []
    hourly = resample(_ramp_minutes(T0, 120), 60)
    assert [b.ts for b in hourly] == [T0, T0 + 3600]


# ---------------------------------------------------------------- book features


def _levels(pairs: list[tuple[str, str]]) -> list[Level]:
    return [Level(price=D(p), qty=D(q)) for p, q in pairs]


@pytest.fixture
def deep_book() -> BookSnapshot:
    return BookSnapshot(
        symbol="BTCTMN", ts=T0, source="rest",
        bids=_levels([("1000", "2"), ("998", "3"), ("996", "1"), ("990", "5"), ("980", "1"), ("970", "10")]),
        asks=_levels([("1002", "1"), ("1004", "2"), ("1010", "4"), ("1020", "1"), ("1030", "1"), ("1040", "7")]),
    )


def test_book_features_hand_computed(deep_book: BookSnapshot) -> None:
    f = book_features(deep_book, T0 + 1.5, D(1))
    assert (f["bid"], f["ask"], f["mid"]) == (D(1000), D(1002), D(1001))
    assert (f["bid_qty"], f["ask_qty"]) == (D(2), D(1))
    assert f["spread_pct"] == D(2) / D(1001)  # (ask - bid) / mid
    assert f["book_age_s"] == D("1.5")
    # within 0.3 % of mid 1001: bids >= 997.997 -> 1000x2 + 998x3; asks <= 1004.003 -> 1002x1 + 1004x2
    assert f["depth_bid_tmn_0p3"] == D(4994)
    assert f["depth_ask_tmn_0p3"] == D(3010)
    # top 5 qty: bids 2+3+1+5+1 = 12, asks 1+2+4+1+1 = 9 -> (12-9)/(12+9)
    assert f["imbalance"] == D(3) / D(21)


def test_book_depth_is_converted_to_tmn(deep_book: BookSnapshot) -> None:
    f = book_features(deep_book, T0, D(266720))  # a USDT-quoted book: notional x bid(USDTTMN)
    assert f["depth_bid_tmn_0p3"] == D(4994) * D(266720)
    assert f["depth_ask_tmn_0p3"] == D(3010) * D(266720)
    assert book_features(deep_book, T0, None)["depth_bid_tmn_0p3"] is None  # no dollar price yet


def test_book_features_missing_or_one_sided(make_book: BookFactory) -> None:
    assert book_features(None, T0, D(1))["mid"] is None
    f = book_features(make_book("BTCTMN", "1000", None), T0, D(1))
    assert f == {"bid": None, "ask": None, "mid": None, "spread_pct": None, "book_age_s": None}


# ---------------------------------------------------------------- candle features


def test_returns_close_to_close(cfg: Config) -> None:
    closes = [1000 + i for i in range(1441)]  # 24h + 1 minute of closes: 1000 .. 2440
    f = candle_features(_flat_minutes(closes, T0 - 1440 * 60), T0 + 60, cfg)
    assert f["ret_1m"] == D(2440) / D(2439) - 1
    assert f["ret_5m"] == D(2440) / D(2435) - 1
    assert f["ret_15m"] == D(2440) / D(2425) - 1
    assert f["ret_1h"] == D(2440) / D(2380) - 1
    assert f["ret_24h"] == D("1.44")  # 2440 / 1000 - 1
    short = candle_features(_flat_minutes(closes[:61], T0), T0 + 3660, cfg)
    assert short["ret_1h"] == D(1060) / D(1000) - 1
    assert short["ret_24h"] is None


def test_realized_vol_1h(cfg: Config) -> None:
    closes = [100 if i % 2 == 0 else 102 for i in range(61)]  # 60 alternating returns
    f = candle_features(_flat_minutes(closes, T0), T0 + 3660, cfg)
    rets = [D(102) / D(100) - 1 if i % 2 else D(100) / D(102) - 1 for i in range(1, 61)]
    # sample std of the 60 one-minute returns x sqrt(60)
    assert f["realized_vol_1h_pct"] == pytest.approx(statistics.stdev(rets) * D(60).sqrt(), abs=D("1e-24"))
    assert f["realized_vol_1h_pct"] == pytest.approx(D("0.1547"), abs=D("0.0001"))
    few = candle_features(_flat_minutes(closes[:10], T0), T0 + 600, cfg)  # 9 returns < 10
    assert few["realized_vol_1h_pct"] is None


def test_15m_features_hand_computed(cfg: Config) -> None:
    """21 quiet bars (100 +-1, volume 15) then a breakout-looking bar (high 110, volume 30)."""
    quiet = [(100, 101, 99, 100, 1)] * 21
    last = [(100, 110, 99, 100, 2)]
    start = T0 - 22 * 900
    minutes = _bars_as_minutes(quiet + last, start)
    # a still-open bar with absurd prices: must not leak into any 15-minute feature
    minutes += [_c(T0 + 60 * k, 500, 1000, 500, 500, 99) for k in range(5)]
    f = candle_features(minutes, T0 + 300, cfg)
    assert f["bars_15m"] == 22
    assert f["last_bar_ts_15m"] == T0 - 900
    assert f["last_close_15m"] == D(100)
    # hh20 = max high of the 20 bars BEFORE the last completed one
    assert f["hh20_15m"] == D(101)
    assert f["vol_ratio_15m"] == D(2)  # 30 / mean(15)
    # ATR(14): TRs of bars 1..20 are 2, the last bar's TR is max(110-99, |110-100|, |99-100|) = 11
    assert f["atr_15m"] == (D(2) * 13 + D(11)) / 14
    assert f["atr_15m_pct"] == (D(2) * 13 + D(11)) / 14 / 100
    # rolling ATR% over the last bars: seven windows at 2 % and one at 2.64 % -> median 2 %
    assert f["atr_15m_pct_median_24h"] == D("0.02")
    assert f["ema20_15m"] == D(100)  # 22 closes at 100
    assert f["ema60_15m"] is None  # needs 60 bars
    assert f["ema_gap_pct"] is None and f["trend_flag"] == 0


def test_15m_features_need_enough_bars(cfg: Config) -> None:
    f = candle_features(_bars_as_minutes([(100, 101, 99, 100, 1)] * 5, T0), T0 + 5 * 900, cfg)
    assert f["bars_15m"] == 5
    assert f["hh20_15m"] is None and f["vol_ratio_15m"] is None
    assert f["atr_15m"] is None and f["atr_15m_pct"] is None and f["atr_15m_pct_median_24h"] is None
    assert f["last_close_15m"] == D(100) and f["last_bar_ts_15m"] == T0 + 4 * 900
    empty = candle_features([], T0, cfg)
    assert empty["bars_15m"] == 0 and empty["last_close_15m"] is None and empty["ret_1m"] is None


@pytest.mark.parametrize(
    ("last_close", "flag", "gap"),
    [
        # fast EMA(2): seed 100, k=2/3; slow EMA(3): seed 100, k=1/2
        ("101", 1, D(1) / D(603)),  # fast 302/3, slow 201/2 -> gap 1/603 = 0.166 % > 0.15 %
        ("99", -1, D(-1) / D(597)),  # fast 298/3, slow 199/2 -> gap -1/597 = -0.168 %
        ("100.5", 0, D(1) / D(1203)),  # fast 301/3, slow 401/4 -> gap 1/1203 = 0.083 % (flat)
    ],
)
def test_trend_flag_threshold(small_ema_cfg: Config, last_close: str, flag: int, gap: Decimal) -> None:
    bars = [(100, 100, 100, 100, 1)] * 3 + [(100, max(100, float(last_close)), min(100, float(last_close)),
                                             last_close, 1)]
    f = candle_features(_bars_as_minutes(bars, T0), T0 + 4 * 900, small_ema_cfg)
    assert f["ema_gap_pct"] == pytest.approx(gap, abs=D("1e-20"))
    assert f["trend_flag"] == flag


def test_trend_flag_threshold_is_configurable() -> None:
    cfg = load_config(overrides={"strategies.s2_usdtalloc.ema_fast": 2, "strategies.s2_usdtalloc.ema_slow": 3,
                                 "strategies.s2_usdtalloc.trend_flag_min_gap_pct": 0.0017}, env={})
    bars = [(100, 100, 100, 100, 1)] * 3 + [(100, 101, 100, 101, 1)]
    f = candle_features(_bars_as_minutes(bars, T0), T0 + 4 * 900, cfg)
    assert f["trend_flag"] == 0  # gap 0.166 % is now below the 0.17 % threshold


# ---------------------------------------------------------------- cross-book features


@pytest.fixture
def overview_books(make_book: BookFactory) -> dict[str, BookSnapshot]:
    """Overview §2.1 snapshot (2026-10-22)."""
    return {
        "USDTTMN": make_book("USDTTMN", "266720", "266758", ts=T0 - 1),
        "BTCTMN": make_book("BTCTMN", "22075699081", "22106074058", ts=T0 - 2),
        "BTCUSDT": make_book("BTCUSDT", "82845.00", "82909.16", ts=T0 - 0.5),
    }


def test_overview_snapshot_dev_mid(overview_books: dict[str, BookSnapshot], markets: dict[str, MarketInfo],
                                   cfg: Config) -> None:
    f = cross_features("BTC", overview_books, markets, cfg, T0)
    assert f is not None
    assert (f["coin"], f["tmn_symbol"], f["usdt_symbol"]) == ("BTC", "BTCTMN", "BTCUSDT")
    assert (f["u_bid"], f["u_ask"], f["u_mid"]) == (D(266720), D(266758), D(266739))
    # implied BTCUSDT = mid(BTCTMN) / mid(USDTTMN) = 22,090,886,569.5 / 266,739 = 82,818.36
    assert f["implied_mid_usdt"] == D("22090886569.5") / D(266739)
    assert f["implied_mid_usdt"] == pytest.approx(D("82818.36"), abs=D("0.01"))
    # vs real mid (82,845.00 + 82,909.16) / 2 = 82,877.08 -> -0.0709 %
    assert f["dev_mid_pct"] == f["implied_mid_usdt"] / D("82877.08") - 1
    assert D("-0.00072") <= f["dev_mid_pct"] <= D("-0.00069")
    # executable edges are negative (inside the spreads): no free lunch in calm markets
    assert f["edge_tmn_cheap_pct"] == D("82845.00") * D(266720) / D("22106074058") - 1  # -0.0437 %
    assert f["edge_usdt_cheap_pct"] == D("22075699081") / (D("82909.16") * D(266758)) - 1  # -0.1853 %
    assert D("-0.00044") < f["edge_tmn_cheap_pct"] < D("-0.00043")
    assert D("-0.00186") < f["edge_usdt_cheap_pct"] < D("-0.00185")
    assert (f["tmn_book_age_s"], f["usdt_book_age_s"], f["dollar_book_age_s"]) == (D(2), D("0.5"), D(1))


@pytest.fixture
def dislocated(make_book: BookFactory) -> dict[str, BookSnapshot]:
    """Round numbers: dollar 100,000/100,010, BTCUSDT 1,000/1,001, BTCTMN lagging 1 % low."""
    return {
        "USDTTMN": make_book("USDTTMN", "100000", "100010"),
        "BTCUSDT": make_book("BTCUSDT", "1000.00", "1001.00"),
        "BTCTMN": make_book("BTCTMN", "98900000", "99000000"),
    }


def test_tmn_cheap_edge(dislocated: dict[str, BookSnapshot], markets: dict[str, MarketInfo], cfg: Config) -> None:
    f = cross_features("BTC", dislocated, markets, cfg, T0)
    assert f is not None
    # buy BTCTMN at 99,000,000; sell BTCUSDT at 1,000 x bid 100,000 = 100,000,000 TMN -> +1/99
    assert f["edge_tmn_cheap_pct"] == D(100000000) / D(99000000) - 1
    assert f["edge_tmn_cheap_pct"] == pytest.approx(D("0.010101"), abs=D("0.000001"))
    # the other direction loses: 98,900,000 / (1,001 x 100,010) - 1
    assert f["edge_usdt_cheap_pct"] == D(98900000) / (D(1001) * D(100010)) - 1
    # mid view agrees: implied 98,950,000 / 100,005 = 989.45 vs 1,000.5 -> TMN book 1.1 % cheap
    assert f["dev_mid_pct"] == (D(98950000) / D(100005)) / D("1000.5") - 1
    assert f["dev_mid_pct"] < 0


def test_usdt_cheap_edge(dislocated: dict[str, BookSnapshot], markets: dict[str, MarketInfo], cfg: Config,
                         make_book: BookFactory) -> None:
    books = dict(dislocated, BTCTMN=make_book("BTCTMN", "101200000", "101300000"))
    f = cross_features("BTC", books, markets, cfg, T0)
    assert f is not None
    # buy BTCUSDT at 1,001 x ask 100,010 = 100,110,010 TMN; sell BTCTMN at 101,200,000 -> +1.0888 %
    assert f["edge_usdt_cheap_pct"] == D(101200000) / D(100110010) - 1
    assert f["edge_usdt_cheap_pct"] == pytest.approx(D("0.010888"), abs=D("0.000001"))
    assert f["dev_mid_pct"] > 0  # TMN book expensive


def test_lockin_costs(dislocated: dict[str, BookSnapshot], markets: dict[str, MarketInfo], cfg: Config) -> None:
    assert cfg.strategies.s1_crossbook.leg1_taker is True
    f = cross_features("BTC", dislocated, markets, cfg, T0)
    assert f is not None
    # leg 1 taker (default): TMN cheap = taker(BTCTMN) + taker(BTCUSDT) = 0.30 % + 0.13 %
    assert f["cost_lockin_tmn_cheap"] == D("0.0043")
    assert f["cost_lockin_usdt_cheap"] == D("0.0043")  # taker(BTCUSDT) + taker(BTCTMN)
    # maker-first leg 1 variants are always reported too
    assert f["cost_lockin_tmn_cheap_maker"] == D("0.0038")  # 0.25 % + 0.13 %
    assert f["cost_lockin_usdt_cheap_maker"] == D("0.0040")  # 0.10 % + 0.30 %
    maker_cfg = load_config(overrides={"strategies.s1_crossbook.leg1_taker": False}, env={})
    g = cross_features("BTC", dislocated, markets, maker_cfg, T0)
    assert g is not None
    assert g["cost_lockin_tmn_cheap"] == D("0.0038")
    assert g["cost_lockin_usdt_cheap"] == D("0.0040")


def test_cross_features_need_all_three_books(dislocated: dict[str, BookSnapshot], markets: dict[str, MarketInfo],
                                             cfg: Config, make_book: BookFactory) -> None:
    for missing in ("USDTTMN", "BTCTMN", "BTCUSDT"):
        books = {k: v for k, v in dislocated.items() if k != missing}
        assert cross_features("BTC", books, markets, cfg, T0) is None
    one_sided = dict(dislocated, BTCUSDT=make_book("BTCUSDT", "1000.00", None))
    assert cross_features("BTC", one_sided, markets, cfg, T0) is None
    no_market = {k: v for k, v in markets.items() if k != "BTCUSDT"}
    assert cross_features("BTC", dislocated, no_market, cfg, T0) is None
    assert cross_features("SOL", dislocated, markets, cfg, T0) is None  # no SOLUSDT market/book


# ---------------------------------------------------------------- engine: snapshot, z-score, state


def _cross_books(make_book: BookFactory, btc_tmn_bid: int, ts: float) -> dict[str, BookSnapshot]:
    return {
        "USDTTMN": make_book("USDTTMN", "100000", "100010", ts=ts),
        "BTCUSDT": make_book("BTCUSDT", "1000.00", "1001.00", ts=ts),
        "BTCTMN": make_book("BTCTMN", str(btc_tmn_bid), str(btc_tmn_bid + 100000), ts=ts),
    }


def test_compute_snapshot(make_book: BookFactory, markets: dict[str, MarketInfo], cfg: Config) -> None:
    eng = FeatureEngine(cfg, markets)
    books = _cross_books(make_book, 100000000, T0)
    snap = eng.compute(T0 + 1, books, {}, ["USDTTMN", "BTCUSDT", "BTCTMN"])
    assert isinstance(snap, FeatureSnapshot) and snap.ts == T0 + 1
    assert list(snap.symbols) == ["BTCTMN", "BTCUSDT", "USDTTMN"]  # sorted
    assert snap.dollar == snap.symbols["USDTTMN"]
    assert list(snap.cross) == ["BTC"]  # only coins with all three books
    # BTCUSDT depth is converted at bid(USDTTMN); BTCTMN is already in TMN
    assert snap.symbols["BTCUSDT"]["depth_bid_tmn_0p3"] == D("1000.00") * D(100000)
    assert snap.symbols["BTCTMN"]["depth_bid_tmn_0p3"] == D(100000000)
    assert snap.cross["BTC"]["dev_zscore"] is None and snap.cross["BTC"]["dev_samples"] == 0
    compact = snap.compact()
    json.dumps(compact)  # JSON-safe: Decimals rendered as strings
    assert compact["symbols"]["BTCTMN"]["bid"] == "100000000"
    assert compact["symbols"]["BTCTMN"]["spread_pct"] == "0.0009995002"  # 100,000 / 100,050,000, 10 dp
    assert compact["cross"]["BTC"]["u_mid"] == "100005"
    assert list(snap.compact(symbols=["USDTTMN"], coins=[])["symbols"]) == ["USDTTMN"]


def test_dev_zscore_after_warmup(make_book: BookFactory, markets: dict[str, MarketInfo], cfg: Config) -> None:
    eng = FeatureEngine(cfg, markets)
    devs: list[Decimal] = []
    for i in range(30):  # 30 samples of +-0.2 % noise around the fair price
        bid = 100000000 + (200000 if i % 3 == 0 else -100000 * (i % 4))
        f = eng.compute(T0 + i, _cross_books(make_book, bid, T0 + i), {}, ["BTCTMN"]).cross["BTC"]
        assert f["dev_zscore"] is None  # fewer than 30 prior samples
        assert f["dev_samples"] == i
        devs.append(f["dev_mid_pct"])
    # 31st sample: a 1 % dislocation, standardised by the 30 prior samples
    f = eng.compute(T0 + 30, _cross_books(make_book, 99000000, T0 + 30), {}, ["BTCTMN"]).cross["BTC"]
    mean, std = statistics.mean(devs), statistics.stdev(devs)
    assert f["dev_samples"] == 30
    assert f["dev_mean_1h"] == pytest.approx(mean, abs=D("1e-24"))
    assert f["dev_std_1h"] == pytest.approx(std, abs=D("1e-24"))
    assert f["dev_zscore"] == pytest.approx((f["dev_mid_pct"] - mean) / std, abs=D("1e-18"))
    assert f["dev_zscore"] < -5  # far outside the recent noise


def test_dev_zscore_constant_history(make_book: BookFactory, markets: dict[str, MarketInfo], cfg: Config) -> None:
    eng = FeatureEngine(cfg, markets)
    for i in range(31):
        f = eng.compute(T0 + i, _cross_books(make_book, 100000000, T0 + i), {}, ["BTCTMN"]).cross["BTC"]
    assert f["dev_samples"] == 30
    assert f["dev_std_1h"] == 0
    assert f["dev_zscore"] is None  # zero std: no z-score rather than a division by zero


def test_dev_window_pruning(make_book: BookFactory, markets: dict[str, MarketInfo], cfg: Config) -> None:
    assert cfg.strategies.s1_crossbook.dev_window_s == 3600
    eng = FeatureEngine(cfg, markets)
    for i in range(40):  # one sample every 100 s: 0 .. 3,900 s
        f = eng.compute(T0 + 100 * i, _cross_books(make_book, 100000000 + 1000 * i, T0 + 100 * i), {}, ["BTCTMN"])
    # at 3,900 s the window keeps samples at >= 300 s: i = 3 .. 38 -> 36 prior samples
    assert f.cross["BTC"]["dev_samples"] == 36
    rows = eng.state()["BTC"]
    assert len(rows) == 37  # 36 prior + the current one
    assert rows[0][0] == T0 + 300


def test_state_roundtrip(make_book: BookFactory, markets: dict[str, MarketInfo], cfg: Config) -> None:
    a = FeatureEngine(cfg, markets)
    for i in range(35):
        a.compute(T0 + i, _cross_books(make_book, 100000000 + 7000 * (i % 5), T0 + i), {}, ["BTCTMN"])
    b = FeatureEngine(cfg, markets)
    b.load_state(json.loads(json.dumps(a.state())))
    assert b.state() == a.state()
    books = _cross_books(make_book, 99500000, T0 + 40)
    fa = a.compute(T0 + 40, books, {}, ["BTCTMN"]).cross["BTC"]
    fb = b.compute(T0 + 40, books, {}, ["BTCTMN"]).cross["BTC"]
    assert fa == fb
    assert fa["dev_zscore"] is not None
    b.load_state(None)
    assert b.state() == {}


def test_candle_cache_invalidation(monkeypatch: pytest.MonkeyPatch, markets: dict[str, MarketInfo],
                                   cfg: Config, make_book: BookFactory) -> None:
    calls: list[float] = []
    real = fe.candle_features

    def counting(candles: list[Candle], now: float, c: Config) -> dict:
        calls.append(now)
        return real(candles, now, c)

    monkeypatch.setattr(fe, "candle_features", counting)
    eng = FeatureEngine(cfg, markets)
    books = {"USDTTMN": make_book("USDTTMN", "100000", "100010")}
    candles = _flat_minutes([100 + i for i in range(30)], T0 - 1800)

    def run(now: float, cs: list[Candle]) -> dict:
        return eng.compute(now, books, {"USDTTMN": cs}, ["USDTTMN"]).symbols["USDTTMN"]

    first = run(T0 + 10, candles)
    assert len(calls) == 1
    assert run(T0 + 20, candles) == {**first, "book_age_s": D(20)}  # same candles, same 15-min bar
    assert len(calls) == 1  # cache hit
    run(T0 + 899, candles)
    assert len(calls) == 1
    run(T0 + 900, candles)  # a 15-minute bar completed
    assert len(calls) == 2
    more = [*candles, _c(T0, 130, 130, 130, 130)]  # a new minute candle
    run(T0 + 900, more)
    assert len(calls) == 3
    repriced = [*candles[:-1], _c(candles[-1].ts, 129, 131, 129, 131)]  # last close changed
    run(T0 + 900, repriced)
    assert len(calls) == 4
    volume_only = [*repriced[:-1], _c(candles[-1].ts, 129, 131, 129, 131, v=7)]  # same close, new volume
    run(T0 + 900, volume_only)
    assert len(calls) == 5
    run(T0 + 900, volume_only)
    assert len(calls) == 5
