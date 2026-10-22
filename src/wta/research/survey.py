"""Market survey (PRD-05 §2): what the recorded order books say before S1/S2 are tuned.

Reads the order books (and USDTTMN 1-minute candles) the recorder stored for the last N hours and
measures, from data only:

* per market: snapshot count, median/p90 spread, median depth within 0.3 % of mid in TMN (USDT
  books converted at the USDTTMN mid of the same moment) and data gaps (> 10 s between snapshots);
* per S1 coin X with XTMN, XUSDT and USDTTMN recorded: the executable cross-book edges
  ``edge_tmn_cheap_pct`` / ``edge_usdt_cheap_pct`` on a common time grid, computed by
  ``wta.features.engine.cross_features`` itself, and the number of DISTINCT lock-in opportunities
  per margin and cost variant (leg 1 taker / leg 1 maker), their duration and gross edge;
* USDTTMN: 15-minute ATR %, 1-h / 24-h return distributions and the share of time in each
  rules-only regime (``wta.strategies.s2_usdtalloc.rule_regime`` over the engine's
  ``candle_features`` at every 15-minute step);
* a computed (not LLM) recommendation for ``strategies.s1_crossbook``: ``margin_min_pct``,
  ``leg1_taker`` and whether S1 lock-in is viable.

Definitions (printed in the report too):

* Grid: multiples of ``grid_s`` seconds. As-of join: at each grid point every book is the latest
  snapshot at or before it; the point is valid only when all three books are at most ``max_age_s``
  old (otherwise it is a hole in the data).
* Opportunity (one coin, one direction, one cost variant, margin m): it opens at a valid grid point
  where ``edge >= cost + m`` if at least ``separation_s`` passed since the previous opportunity of
  the same coin and direction ended; it ends at the first later grid point where ``edge < cost``
  or the books are not valid (duration = end - start). Triggers inside the separation window are
  ignored (same dislocation burst). An opportunity still open when the data ends is counted, closed
  at the end of the data and flagged as censored.
* Per day = count x 86,400 / aligned (valid) seconds of that coin; totals add the per-coin rates
  of the coins with at least ``min_coverage_h`` hours of aligned data.
* Window: the last ``since`` hours ending at the newest recorded order book (or ``--until``), so a
  survey run after the recorder stopped still sees the recorded hours; the header says which.

Recommendation rules (``recommend``; PRD-05 §2 + PRD-03 §3), on the all-coin per-day rates:

* PRD-03 trigger: fewer than 2 taker opportunities/day at 0.15 % -> ``margin_min_pct = 0.0008``.
  Otherwise the largest evaluated margin >= 0.15 % that still gives >= 3 taker opportunities/day
  (0.15 % if none does).
* ``leg1_taker = false`` only when, at that margin, the maker-leg-1 variant gives >= 1.5x the taker
  opportunities and they last a median of more than 20 s (a resting maker order needs time).
* "S1 lock-in viable" = >= 3 opportunities/day at some margin >= 0.10 % (recommended variant).
* Less than ``min_coverage_h`` hours of aligned data: keep the configured values (unknown viability).

Money comparisons (edge vs cost + margin) are exact ``Decimal``; summary statistics are floats.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import sys
import time
from array import array
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from wta.config import Config, S1Cfg, apply_changes
from wta.data.store import Store
from wta.exchange.markets import default_fees, load_fee_snapshot, load_markets_snapshot
from wta.features.engine import USDTTMN, book_features, candle_features, cross_features
from wta.models import BookSnapshot, Candle, Level, MarketInfo
from wta.strategies.s2_usdtalloc import rule_regime
from wta.utils import gitinfo
from wta.utils.timeutil import iso, utc_day

ZERO = Decimal(0)
ONE = Decimal(1)
NAN = float("nan")
DAY_S = 86_400.0

# PRD-05 §2 margin set; 0.08 % (the PRD-03 §3 fallback) is evaluated too so the fallback has numbers.
SURVEY_MARGINS: tuple[Decimal, ...] = tuple(Decimal(m) for m in ("0.0005", "0.0010", "0.0015", "0.0025", "0.0040"))
FALLBACK_MARGIN = Decimal("0.0008")
EVAL_MARGINS: tuple[Decimal, ...] = tuple(sorted({*SURVEY_MARGINS, FALLBACK_MARGIN}))
VARIANTS: tuple[str, ...] = ("taker", "maker")  # leg-1 fee: taker (default config) or maker-first
DIRECTIONS: tuple[str, ...] = ("tmn_cheap", "usdt_cheap")
EDGE_KEYS = {"tmn_cheap": "edge_tmn_cheap_pct", "usdt_cheap": "edge_usdt_cheap_pct"}
COST_KEYS = {  # cross_features keys; the taker keys are read from a config with leg1_taker forced on
    ("taker", "tmn_cheap"): "cost_lockin_tmn_cheap",
    ("taker", "usdt_cheap"): "cost_lockin_usdt_cheap",
    ("maker", "tmn_cheap"): "cost_lockin_tmn_cheap_maker",
    ("maker", "usdt_cheap"): "cost_lockin_usdt_cheap_maker",
}

# Recommendation rules (PRD-05 §2, PRD-03 §3).
TARGET_PER_DAY = 3.0  # opportunities/day across coins
VIABLE_MIN_MARGIN = Decimal("0.0010")  # "S1 lock-in viable" = >= 3/day at a margin >= 0.10 %
MAKER_RATIO = 1.5  # maker leg 1 only if it yields >= 1.5x the taker opportunities ...
MAKER_MIN_MEDIAN_S = 20.0  # ... and they last long enough (median > 20 s) for a maker fill
PRD03_MARGIN = Decimal("0.0015")  # PRD-03 §3: < 2/day at 0.15 % -> lower the margin / maker leg 1
PRD03_MIN_PER_DAY = 2.0

REGIME_STEP_S = 900  # the S2 regime is evaluated on 15-minute steps
CANDLE_LOOKBACK_S = 25 * 3600  # candle_features needs ~25 h (24-h return, EMA60 of 15-min bars)
QUANTILES: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)
NON_LIVE_SOURCES = frozenset({"synthetic", "replay", "backtest"})
REGIME_ORDER = ("range", "trend_up", "trend_down", "chaos", "n/a")

# Chart colours (dataviz reference palette, light surface).
_SURFACE, _INK, _INK2, _GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
_BLUE, _ORANGE = "#2a78d6", "#eb6834"


# ---------------------------------------------------------------- parameters


@dataclass(frozen=True)
class SurveyParams:
    """How the survey reads the store (defaults = PRD-05 §2)."""

    since_s: float = DAY_S  # window length, ending at ``until``
    until: float | None = None  # window end; None = the newest recorded data (books / USDTTMN candles)
    sources: tuple[str, ...] | None = None  # book sources to use, e.g. ("rest", "ws"); None = all
    grid_s: float = 2.0  # cross-book time grid
    max_age_s: float = 10.0  # as-of join: an older book at a grid point counts as missing
    gap_s: float = 10.0  # a data gap = more than this between two snapshots of one market
    separation_s: float = 60.0  # a new opportunity needs this long since the previous one ended
    min_coverage_h: float = 1.0  # coins with less aligned data stay out of totals / recommendation

    def __post_init__(self) -> None:
        if not self.since_s > 0:
            raise ValueError("since_s must be > 0")
        if not self.grid_s > 0:
            raise ValueError("grid_s must be > 0")
        if self.max_age_s < 0 or self.gap_s < 0 or self.separation_s < 0 or self.min_coverage_h < 0:
            raise ValueError("max_age_s, gap_s, separation_s and min_coverage_h must be >= 0")


def parse_duration(text: str) -> float:
    """``"24h"`` / ``"6h"`` / ``"90m"`` / ``"2d"`` / ``"3600s"`` / ``"12"`` (hours) -> seconds."""
    s = text.strip().lower()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": DAY_S}
    mult = 3600.0
    if s and s[-1] in units:
        mult, s = units[s[-1]], s[:-1]
    try:
        value = float(s)
    except ValueError:
        raise ValueError(f"not a duration: {text!r} (use e.g. 24h, 6h, 90m)") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"duration must be > 0: {text!r}")
    return value * mult


# ---------------------------------------------------------------- book series


@dataclass
class BookSeries:
    """One market's snapshots in time order, compact: top of book + 0.3 % depth, no level lists."""

    symbol: str
    ts: array = field(default_factory=lambda: array("d"))
    bid: list[Decimal | None] = field(default_factory=list)
    ask: list[Decimal | None] = field(default_factory=list)
    spread: array = field(default_factory=lambda: array("d"))  # (ask - bid) / mid; NaN if one side empty
    depth_bid: array = field(default_factory=lambda: array("d"))  # quote ccy within 0.3 % of mid; NaN = n/a
    depth_ask: array = field(default_factory=lambda: array("d"))
    source: list[str] = field(default_factory=list)

    def add(self, book: BookSnapshot) -> None:
        f = book_features(book, book.ts, ONE)  # ONE: depth stays in the quote currency
        self.ts.append(book.ts)
        self.bid.append(book.best_bid)
        self.ask.append(book.best_ask)
        sp = book.spread_pct
        self.spread.append(NAN if sp is None else float(sp))
        self.depth_bid.append(_float(f.get("depth_bid_tmn_0p3")))
        self.depth_ask.append(_float(f.get("depth_ask_tmn_0p3")))
        self.source.append(sys.intern(book.source))

    def __len__(self) -> int:
        return len(self.ts)

    def index_at(self, t: float) -> int:
        """Index of the latest snapshot at or before ``t`` (-1 when there is none)."""
        return bisect.bisect_right(self.ts, t) - 1

    def mid(self, i: int) -> Decimal | None:
        b, a = self.bid[i], self.ask[i]
        return None if b is None or a is None else (b + a) / 2

    def edge_book(self, i: int) -> BookSnapshot:
        """Top-of-book snapshot ``i`` for ``cross_features`` (prices only: level qty is not kept)."""
        b, a = self.bid[i], self.ask[i]
        return BookSnapshot(
            symbol=self.symbol,
            ts=self.ts[i],
            source=self.source[i],  # type: ignore[arg-type]
            bids=[] if b is None else [Level(price=b, qty=ZERO)],
            asks=[] if a is None else [Level(price=a, qty=ZERO)],
        )


def _float(value: Decimal | None) -> float:
    return NAN if value is None else float(value)


def load_book_series(
    store: Store, symbols: Iterable[str], from_ts: float, to_ts: float, params: SurveyParams
) -> tuple[dict[str, BookSeries], Counter[str], Counter[str]]:
    """Books of ``symbols`` in ``[from_ts - max_age_s, to_ts]`` that pass the source filter.

    The extra ``max_age_s`` before the window only seeds the as-of join at the first grid points.
    Returns ``(series, used, excluded)``: per-source counts of in-window snapshots kept / filtered out.
    """
    wanted = None if params.sources is None else frozenset(params.sources)
    series: dict[str, BookSeries] = {}
    used: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    for sym in sorted(set(symbols)):
        s = BookSeries(sym)
        for book in store.iter_books(sym, from_ts - params.max_age_s, to_ts):
            in_window = book.ts >= from_ts
            if wanted is not None and book.source not in wanted:
                if in_window:
                    excluded[book.source] += 1
                continue
            s.add(book)
            if in_window:
                used[book.source] += 1
        if len(s):
            series[sym] = s
    return series, used, excluded


def _split_symbol(symbol: str, quotes: Iterable[str]) -> tuple[str, str] | None:
    for q in sorted(quotes, key=len, reverse=True):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return None


def survey_markets(symbols: Iterable[str], cfg: Config) -> dict[str, MarketInfo]:
    """Offline market metadata: ``configs/markets_snapshot.yaml`` + fee snapshot (never the API).

    A recorded symbol missing from the snapshot gets the default fees of its quote asset.
    """
    snapshot = load_markets_snapshot()
    fees = load_fee_snapshot()
    out: dict[str, MarketInfo] = {}
    for sym in sorted(set(symbols)):
        if sym in snapshot:
            out[sym] = snapshot[sym]
            continue
        split = _split_symbol(sym, cfg.markets.quote_assets)
        if split is None:
            continue
        base, quote = split
        maker, taker = default_fees(quote, fees)
        sym_fee = (fees.get("symbols") or {}).get(sym)
        if sym_fee:
            maker, taker = Decimal(str(sym_fee["maker"])), Decimal(str(sym_fee["taker"]))
        out[sym] = MarketInfo(
            symbol=sym, base=base, quote=quote, tick_decimals=8, step_decimals=8, maker_fee=maker, taker_fee=taker
        )
    return out


# ---------------------------------------------------------------- per-market statistics


@dataclass(frozen=True)
class MarketStats:
    symbol: str
    quote: str
    snapshots: int
    sources: dict[str, int]
    first_ts: float
    last_ts: float
    spread_median: float | None
    spread_p90: float | None
    depth_bid_tmn_median: float | None  # within 0.3 % of mid, TMN
    depth_ask_tmn_median: float | None
    depth_samples: int  # snapshots with a TMN rate (USDT books need a fresh USDTTMN mid)
    gaps: int  # > gap_s between consecutive snapshots
    longest_gap_s: float
    gap_total_s: float

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _tmn_rate(quote: str, usdt: BookSeries | None, t: float, max_age_s: float) -> float | None:
    """TMN per unit of ``quote`` at ``t``: 1 for TMN, the as-of USDTTMN mid for USDT."""
    if quote == "TMN":
        return 1.0
    if quote != "USDT" or usdt is None:
        return None
    j = usdt.index_at(t)
    if j < 0 or t - usdt.ts[j] > max_age_s:
        return None
    m = usdt.mid(j)
    return None if m is None else float(m)


def _median(values: Sequence[float]) -> float | None:
    return float(np.median(np.asarray(values, dtype=float))) if len(values) else None


def _percentile(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(np.asarray(values, dtype=float), q)) if len(values) else None


def market_stats(
    series: BookSeries, quote: str, usdt: BookSeries | None, from_ts: float, params: SurveyParams
) -> MarketStats | None:
    i0 = bisect.bisect_left(series.ts, from_ts)
    n = len(series) - i0
    if n <= 0:
        return None
    spreads = [x for x in series.spread[i0:] if not math.isnan(x)]
    depth_b: list[float] = []
    depth_a: list[float] = []
    for i in range(i0, len(series)):
        rate = _tmn_rate(quote, usdt, series.ts[i], params.max_age_s)
        if rate is None:
            continue
        if not math.isnan(series.depth_bid[i]):
            depth_b.append(series.depth_bid[i] * rate)
        if not math.isnan(series.depth_ask[i]):
            depth_a.append(series.depth_ask[i] * rate)
    gaps = [
        series.ts[i] - series.ts[i - 1]
        for i in range(i0 + 1, len(series))
        if series.ts[i] - series.ts[i - 1] > params.gap_s
    ]
    return MarketStats(
        symbol=series.symbol,
        quote=quote,
        snapshots=n,
        sources=dict(sorted(Counter(series.source[i0:]).items())),
        first_ts=series.ts[i0],
        last_ts=series.ts[-1],
        spread_median=_median(spreads),
        spread_p90=_percentile(spreads, 90),
        depth_bid_tmn_median=_median(depth_b),
        depth_ask_tmn_median=_median(depth_a),
        depth_samples=max(len(depth_b), len(depth_a)),
        gaps=len(gaps),
        longest_gap_s=max(gaps, default=0.0),
        gap_total_s=float(sum(gaps)),
    )


# ---------------------------------------------------------------- cross-book alignment


@dataclass
class AlignedCoin:
    """Cross-book edges of one coin on the common grid (``None`` = no valid books at that point)."""

    coin: str
    symbols: tuple[str, str, str]
    grid_s: float
    times: list[float]
    edges: dict[str, list[Decimal | None]]  # direction -> edge per grid point
    costs: dict[tuple[str, str], Decimal]  # (variant, direction) -> lock-in fee cost

    @property
    def valid_points(self) -> int:
        return sum(1 for e in self.edges["tmn_cheap"] if e is not None)

    @property
    def coverage_s(self) -> float:
        return self.valid_points * self.grid_s

    @property
    def end_ts(self) -> float:
        """Where the data ends (an opportunity still open there is closed here, censored)."""
        return self.times[-1] + self.grid_s if self.times else 0.0


def _fee_costs(tm: MarketInfo, um: MarketInfo) -> dict[tuple[str, str], Decimal]:
    """Same fee arithmetic as ``cross_features`` (only used when a coin has no valid grid point)."""
    return {
        ("taker", "tmn_cheap"): tm.taker_fee + um.taker_fee,
        ("taker", "usdt_cheap"): um.taker_fee + tm.taker_fee,
        ("maker", "tmn_cheap"): tm.maker_fee + um.taker_fee,
        ("maker", "usdt_cheap"): um.maker_fee + tm.taker_fee,
    }


def align_coin(
    coin: str,
    series: dict[str, BookSeries],
    markets: dict[str, MarketInfo],
    cfg_taker: Config,
    params: SurveyParams,
    from_ts: float,
    to_ts: float,
) -> AlignedCoin | None:
    """As-of join of XTMN, XUSDT and USDTTMN on the grid; edges from ``cross_features``.

    ``cfg_taker`` must have ``strategies.s1_crossbook.leg1_taker = true`` so the ``cost_lockin_*``
    keys are the leg-1-taker costs (the ``*_maker`` keys are the maker variant either way).
    Returns None when one of the three books or markets is missing.
    """
    syms = (f"{coin}TMN", f"{coin}USDT", USDTTMN)
    if any(s not in series for s in syms) or syms[0] not in markets or syms[1] not in markets:
        return None
    sers = [series[s] for s in syms]
    start = max(from_ts, *(s.ts[0] for s in sers))
    end = min(to_ts, *(s.ts[-1] + params.max_age_s for s in sers))
    grid = params.grid_s
    times = [k * grid for k in range(math.ceil(start / grid), math.floor(end / grid) + 1)]
    edges: dict[str, list[Decimal | None]] = {d: [] for d in DIRECTIONS}
    costs: dict[tuple[str, str], Decimal] | None = None
    last: dict[str, tuple[int, BookSnapshot]] = {}
    for t in times:
        books: dict[str, BookSnapshot] | None = {}
        for s in sers:
            i = s.index_at(t)
            if i < 0 or t - s.ts[i] > params.max_age_s:
                books = None
                break
            hit = last.get(s.symbol)
            if hit is None or hit[0] != i:
                hit = (i, s.edge_book(i))
                last[s.symbol] = hit
            books[s.symbol] = hit[1]  # type: ignore[index]
        cf = None if books is None else cross_features(coin, books, markets, cfg_taker, t)
        if cf is None:
            for d in DIRECTIONS:
                edges[d].append(None)
            continue
        for d in DIRECTIONS:
            edges[d].append(cf[EDGE_KEYS[d]])
        if costs is None:
            costs = {key: cf[name] for key, name in COST_KEYS.items()}
    if costs is None:
        costs = _fee_costs(markets[syms[0]], markets[syms[1]])
    return AlignedCoin(coin=coin, symbols=syms, grid_s=grid, times=times, edges=edges, costs=costs)


# ---------------------------------------------------------------- opportunities


@dataclass(frozen=True)
class Opportunity:
    start: float
    end: float
    edge: Decimal  # gross edge at the trigger point (what an immediate entry would capture)
    cost: Decimal
    censored: bool = False  # still open when the data ended

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def net(self) -> Decimal:
        return self.edge - self.cost


def find_opportunities(
    times: Sequence[float],
    edges: Sequence[Decimal | None],
    cost: Decimal,
    margin: Decimal,
    separation_s: float = 60.0,
    end_ts: float | None = None,
) -> list[Opportunity]:
    """Distinct opportunities of one edge series (definition in the module docstring)."""
    out: list[Opportunity] = []
    trigger = cost + margin
    start: float | None = None
    start_edge = ZERO
    last_end: float | None = None
    for t, e in zip(times, edges, strict=True):
        if start is not None:
            if e is None or e < cost:
                out.append(Opportunity(start, t, start_edge, cost))
                start, last_end = None, t
            continue
        if e is not None and e >= trigger and (last_end is None or t - last_end >= separation_s):
            start, start_edge = t, e
    if start is not None:
        end = end_ts if end_ts is not None else times[-1]
        out.append(Opportunity(start, end, start_edge, cost, censored=True))
    return out


@dataclass(frozen=True)
class OppCell:
    """Opportunities of one (coin or all coins, variant, margin)."""

    count: int
    per_day: float | None
    median_duration_s: float | None
    gross_edge_sum: Decimal = ZERO
    net_edge_sum: Decimal = ZERO
    censored: int = 0
    by_direction: tuple[tuple[str, int], ...] = ()

    def direction(self, d: str) -> int:
        return dict(self.by_direction).get(d, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "per_day": self.per_day,
            "median_duration_s": self.median_duration_s,
            "gross_edge_sum": str(self.gross_edge_sum),
            "net_edge_sum": str(self.net_edge_sum),
            "censored": self.censored,
            "by_direction": dict(self.by_direction),
        }


def make_cell(opps: dict[str, list[Opportunity]], per_day: float | None) -> OppCell:
    pooled = [o for d in DIRECTIONS for o in opps.get(d, [])]
    return OppCell(
        count=len(pooled),
        per_day=per_day,
        median_duration_s=_median([o.duration_s for o in pooled]),
        gross_edge_sum=sum((o.edge for o in pooled), ZERO),
        net_edge_sum=sum((o.net for o in pooled), ZERO),
        censored=sum(1 for o in pooled if o.censored),
        by_direction=tuple((d, len(opps.get(d, []))) for d in DIRECTIONS),
    )


@dataclass
class CoinSurvey:
    coin: str
    aligned: AlignedCoin
    opportunities: dict[tuple[str, Decimal, str], list[Opportunity]]  # (variant, margin, direction)
    included: bool  # enough aligned data to count in the totals / recommendation

    def opps(self, variant: str, margin: Decimal) -> dict[str, list[Opportunity]]:
        return {d: self.opportunities[(variant, margin, d)] for d in DIRECTIONS}

    def per_day(self, count: int) -> float | None:
        cov = self.aligned.coverage_s
        return count * DAY_S / cov if cov > 0 else None

    def cell(self, variant: str, margin: Decimal) -> OppCell:
        opps = self.opps(variant, margin)
        return make_cell(opps, self.per_day(sum(len(v) for v in opps.values())))

    def edge_stats(self, direction: str) -> dict[str, float | None]:
        vals = [e for e in self.aligned.edges[direction] if e is not None]
        if not vals:
            return {"median": None, "p90": None, "max": None, "share_ge_taker": None, "share_ge_maker": None}
        arr = np.asarray([float(v) for v in vals], dtype=float)
        ct, cm = self.aligned.costs[("taker", direction)], self.aligned.costs[("maker", direction)]
        return {
            "median": float(np.median(arr)),
            "p90": float(np.percentile(arr, 90)),
            "max": float(arr.max()),
            "share_ge_taker": sum(1 for v in vals if v >= ct) / len(vals),
            "share_ge_maker": sum(1 for v in vals if v >= cm) / len(vals),
        }


def survey_coin(aligned: AlignedCoin, params: SurveyParams) -> CoinSurvey:
    opps: dict[tuple[str, Decimal, str], list[Opportunity]] = {}
    for variant in VARIANTS:
        for margin in EVAL_MARGINS:
            for d in DIRECTIONS:
                opps[(variant, margin, d)] = find_opportunities(
                    aligned.times,
                    aligned.edges[d],
                    aligned.costs[(variant, d)],
                    margin,
                    params.separation_s,
                    aligned.end_ts,
                )
    included = aligned.coverage_s > 0 and aligned.coverage_s >= params.min_coverage_h * 3600
    return CoinSurvey(coin=aligned.coin, aligned=aligned, opportunities=opps, included=included)


def total_cells(coins: Sequence[CoinSurvey]) -> dict[tuple[str, Decimal], OppCell]:
    """All-coin cells over the included coins; ``per_day`` adds the per-coin rates."""
    inc = [c for c in coins if c.included]
    out: dict[tuple[str, Decimal], OppCell] = {}
    if not inc:
        return out
    for variant in VARIANTS:
        for margin in EVAL_MARGINS:
            pooled: dict[str, list[Opportunity]] = {d: [] for d in DIRECTIONS}
            per_day = 0.0
            for c in inc:
                opps = c.opps(variant, margin)
                for d in DIRECTIONS:
                    pooled[d].extend(opps[d])
                per_day += c.per_day(sum(len(v) for v in opps.values())) or 0.0
            out[(variant, margin)] = make_cell(pooled, per_day)
    return out


# ---------------------------------------------------------------- recommendation


@dataclass(frozen=True)
class Recommendation:
    status: str  # "ok" | "insufficient_data"
    margin_min_pct: Decimal
    leg1_taker: bool
    lockin_viable: bool | None  # None = unknown (insufficient data)
    reasons: tuple[str, ...]
    low_activity: bool | None = None  # PRD-03 §3 trigger: < 2 taker opportunities/day at 0.15 %

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "margin_min_pct": str(self.margin_min_pct),
            "leg1_taker": self.leg1_taker,
            "lockin_viable": self.lockin_viable,
            "low_activity": self.low_activity,
            "reasons": list(self.reasons),
        }


def _pm(margin: Decimal) -> str:
    return f"{float(margin) * 100:.2f}%"


def recommend(
    totals: dict[tuple[str, Decimal], OppCell],
    coverage_h: float,
    s1: S1Cfg,
    min_coverage_h: float = 1.0,
) -> Recommendation:
    """Computed S1 recommendation from the all-coin cells (rules: module docstring + constants).

    * PRD-03 §3 trigger — fewer than 2 taker opportunities/day at 0.15 %: ``margin_min_pct`` 0.0008.
      Otherwise the LARGEST evaluated margin >= 0.15 % whose taker variant still gives >= 3/day
      (keep as much safety margin as the activity target allows), else 0.15 %.
    * ``leg1_taker``: false only if, at that margin, the maker-leg-1 variant gives >= 1.5x the taker
      opportunities AND its median duration is > 20 s (a maker order needs time to fill).
    * lock-in viable: >= 3 opportunities/day at a margin >= 0.10 % for the recommended variant.

    With less than ``min_coverage_h`` hours of aligned data the configured values are kept.
    """
    if not totals or coverage_h < min_coverage_h:
        keep = (
            f"keep the configured values (margin_min_pct {s1.margin_min_pct}, "
            f"leg1_taker {str(s1.leg1_taker).lower()})"
        )
        why = (
            f"insufficient data: {coverage_h:.2f} h of aligned cross-book data "
            f"(at least {min_coverage_h:g} h of XTMN + XUSDT + USDTTMN books needed)"
        )
        return Recommendation("insufficient_data", s1.margin_min_pct, s1.leg1_taker, None, (f"{why}; {keep}.",))

    margins = sorted({m for _, m in totals})

    def rate(variant: str, margin: Decimal) -> float:
        cell = totals.get((variant, margin))
        return 0.0 if cell is None or cell.per_day is None else cell.per_day

    reasons: list[str] = []
    at_default = rate("taker", PRD03_MARGIN)
    low_activity = at_default < PRD03_MIN_PER_DAY
    if low_activity:
        margin = FALLBACK_MARGIN
        reasons.append(
            f"margin_min_pct {margin}: the taker variant gives {at_default:.2f} opportunities/day at "
            f"{_pm(PRD03_MARGIN)} (< {PRD03_MIN_PER_DAY:g}/day, the PRD-03 §3 trigger) -> lower the margin to "
            f"{_pm(FALLBACK_MARGIN)} ({rate('taker', FALLBACK_MARGIN):.2f}/day there)."
        )
        if rate("taker", FALLBACK_MARGIN) < PRD03_MIN_PER_DAY and rate("maker", FALLBACK_MARGIN) < PRD03_MIN_PER_DAY:
            reasons.append(
                f"Even at {_pm(FALLBACK_MARGIN)} both variants stay below {PRD03_MIN_PER_DAY:g}/day: expect S1 "
                "lock-in to be rare; S2 provides the activity."
            )
    else:
        higher = [m for m in margins if m >= PRD03_MARGIN and rate("taker", m) >= TARGET_PER_DAY]
        if higher:
            margin = max(higher)
            reasons.append(
                f"margin_min_pct {margin}: the largest evaluated margin >= {_pm(PRD03_MARGIN)} at which the taker "
                f"variant still gives >= {TARGET_PER_DAY:g} distinct opportunities/day across coins "
                f"({rate('taker', margin):.1f}/day) — as much safety margin as the activity target allows."
            )
        else:
            margin = PRD03_MARGIN
            reasons.append(
                f"margin_min_pct {margin}: keep the PRD-03 default; the taker variant gives {at_default:.2f}/day "
                f"at {_pm(PRD03_MARGIN)} (>= {PRD03_MIN_PER_DAY:g}/day) and no larger margin reaches "
                f"{TARGET_PER_DAY:g}/day."
            )
    t_rate, m_rate = rate("taker", margin), rate("maker", margin)
    maker_cell = totals.get(("maker", margin))
    m_med = None if maker_cell is None else maker_cell.median_duration_s
    more = m_rate > 0 and m_rate >= MAKER_RATIO * t_rate
    lasts = m_med is not None and m_med > MAKER_MIN_MEDIAN_S
    leg1_taker = not (more and lasts)
    ratio = "n/a" if m_rate == 0 else ("inf" if t_rate == 0 else f"{m_rate / t_rate:.2f}x")
    med_txt = "n/a" if m_med is None else f"{m_med:.0f} s"
    head = "leg1_taker true" if leg1_taker else "leg1_taker false"
    if low_activity:
        head += " (PRD-03 §3 also allows leg1_taker=false; decided by the maker rule)"
    reasons.append(
        f"{head}: at {_pm(margin)} the maker-leg-1 variant gives {m_rate:.1f}/day vs {t_rate:.1f}/day taker "
        f"({ratio}), median duration {med_txt}; a maker leg 1 is chosen only with >= {MAKER_RATIO:g}x the "
        f"opportunities and a median duration > {MAKER_MIN_MEDIAN_S:g} s."
    )
    variant = "taker" if leg1_taker else "maker"
    strong = [m for m in margins if m >= VIABLE_MIN_MARGIN and rate(variant, m) >= TARGET_PER_DAY]
    viable = bool(strong)
    shown = max(strong) if strong else VIABLE_MIN_MARGIN
    reasons.append(
        f"S1 lock-in viable: {'yes' if viable else 'no'} ({variant} variant: {rate(variant, shown):.1f}/day at "
        f"{_pm(shown)}; viable needs >= {TARGET_PER_DAY:g}/day at a margin >= {_pm(VIABLE_MIN_MARGIN)})."
    )
    if coverage_h < 24:
        reasons.append(
            f"Per-day rates are extrapolated from {coverage_h:.1f} h of aligned data (best-covered coin); "
            "treat them as indicative."
        )
    return Recommendation("ok", margin, leg1_taker, viable, tuple(reasons), low_activity)


# ---------------------------------------------------------------- USDTTMN (the dollar)


@dataclass
class DollarStats:
    candle_source: str  # "store" | "book mids" | "none"
    candles_in_window: int
    atr_pct: list[float]  # engine atr_15m_pct at every 15-minute step with enough bars
    ret_1h: list[float]  # close-to-close, every minute of the window
    ret_24h: list[float]
    regimes: dict[str, int]  # rules regime -> 15-minute steps ("n/a" = not enough history)
    steps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "candle_source": self.candle_source,
            "candles_in_window": self.candles_in_window,
            "atr_15m_pct": {"n": len(self.atr_pct), "median": _median(self.atr_pct),
                            "p90": _percentile(self.atr_pct, 90)},
            "ret_1h": quantiles(self.ret_1h),
            "ret_24h": quantiles(self.ret_24h),
            "regimes": dict(self.regimes),
            "steps": self.steps,
        }


def quantiles(values: Sequence[float]) -> dict[str, float] | None:
    """``{"n", "min", "p5", "p25", "p50", "p75", "p95", "max"}`` (numpy linear percentiles)."""
    if not len(values):
        return None
    arr = np.asarray(values, dtype=float)
    out: dict[str, float] = {"n": float(len(arr)), "min": float(arr.min())}
    for q in QUANTILES:
        out[f"p{round(q * 100)}"] = float(np.percentile(arr, q * 100))
    out["max"] = float(arr.max())
    return out


def candles_from_mids(series: BookSeries) -> list[Candle]:
    """1-minute candles (o/h/l/c of the mids, volume 0) when the store has no USDTTMN candles."""
    out: list[Candle] = []
    cur: list[Any] | None = None  # [minute, o, h, l, c]
    for i in range(len(series)):
        mid = series.mid(i)
        if mid is None:
            continue
        minute = int(series.ts[i] // 60 * 60)
        if cur is None or cur[0] != minute:
            if cur is not None:
                out.append(_candle(series.symbol, cur))
            cur = [minute, mid, mid, mid, mid]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], mid), min(cur[3], mid), mid
    if cur is not None:
        out.append(_candle(series.symbol, cur))
    return out


def _candle(symbol: str, acc: list[Any]) -> Candle:
    return Candle(symbol=symbol, resolution="1", ts=acc[0], o=acc[1], h=acc[2], l=acc[3], c=acc[4], v=ZERO)


def dollar_stats(
    store: Store, usdt: BookSeries | None, cfg: Config, from_ts: float, to_ts: float
) -> DollarStats:
    candles = store.load_candles(USDTTMN, "1", from_ts - CANDLE_LOOKBACK_S, to_ts)
    source = "store"
    if not candles and usdt is not None and len(usdt):
        candles, source = candles_from_mids(usdt), "book mids"
    if not candles:
        return DollarStats("none", 0, [], [], [], {}, 0)
    close = {c.ts: c.c for c in candles}
    in_window = [c for c in candles if from_ts <= c.ts <= to_ts]
    ret_1h = [float(c.c / close[c.ts - 3600] - ONE) for c in in_window if close.get(c.ts - 3600)]
    ret_24h = [float(c.c / close[c.ts - 86400] - ONE) for c in in_window if close.get(c.ts - 86400)]
    ts_list = [c.ts for c in candles]
    atr_pct: list[float] = []
    regimes: Counter[str] = Counter()
    steps = 0
    # one step per 15 minutes of the window [from_ts, to_ts): step t stands for [t, t + 15 min)
    for k in range(math.ceil(from_ts / REGIME_STEP_S), math.ceil(to_ts / REGIME_STEP_S)):
        t = k * REGIME_STEP_S
        lo = bisect.bisect_left(ts_list, t - CANDLE_LOOKBACK_S)
        hi = bisect.bisect_right(ts_list, t - 60)  # minutes completed by t
        f = candle_features(candles[lo:hi], float(t), cfg)
        steps += 1
        if f.get("atr_15m_pct") is not None:
            atr_pct.append(float(f["atr_15m_pct"]))
        if f.get("ema60_15m") is None or f.get("ret_24h") is None:
            regimes["n/a"] += 1
        else:
            regimes[rule_regime(f, cfg.strategies.s2_usdtalloc)[0]] += 1
    return DollarStats(source, len(in_window), atr_pct, ret_1h, ret_24h, dict(regimes), steps)


# ---------------------------------------------------------------- the survey


@dataclass
class SurveyResult:
    status: str  # "ok" | "no_data"
    message: str
    db_path: str
    params: SurveyParams
    from_ts: float
    to_ts: float
    until_note: str  # how the window end was chosen
    generated_ts: float
    git_commit: str
    git_dirty: bool
    config_hash: str
    s1_cfg: S1Cfg
    store_books_range: tuple[float | None, float | None]
    source_counts: dict[str, int]  # in-window snapshots used, by source
    excluded_counts: dict[str, int]  # in-window snapshots dropped by the source filter
    markets: list[MarketStats]
    coins: list[CoinSurvey]
    missing_coins: dict[str, list[str]]  # configured S1 coin -> its symbols not in the window
    totals: dict[tuple[str, Decimal], OppCell]
    dollar: DollarStats
    recommendation: Recommendation
    fees: dict[str, tuple[Decimal, Decimal]]  # quote -> (maker, taker) of the markets surveyed

    @property
    def snapshots(self) -> int:
        return sum(self.source_counts.values())

    @property
    def synthetic(self) -> bool:
        return self.source_counts.get("synthetic", 0) > 0

    @property
    def data_first_ts(self) -> float | None:
        return min((m.first_ts for m in self.markets), default=None)

    @property
    def data_last_ts(self) -> float | None:
        return max((m.last_ts for m in self.markets), default=None)

    @property
    def report_day(self) -> str:
        return utc_day(self.to_ts)

    @property
    def coverage_h(self) -> float:
        return max((c.aligned.coverage_s for c in self.coins if c.included), default=0.0) / 3600

    def to_dict(self) -> dict[str, Any]:
        p = self.params
        return {
            "status": self.status,
            "message": self.message,
            "db_path": self.db_path,
            "window": {"from": iso(self.from_ts), "to": iso(self.to_ts), "since_h": p.since_s / 3600,
                       "until": self.until_note},
            "params": {"sources": list(p.sources) if p.sources else None, "grid_s": p.grid_s,
                       "max_age_s": p.max_age_s, "gap_s": p.gap_s, "separation_s": p.separation_s,
                       "min_coverage_h": p.min_coverage_h},
            "generated": iso(self.generated_ts),
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "config_hash": self.config_hash,
            "snapshots": self.snapshots,
            "sources": self.source_counts,
            "excluded_sources": self.excluded_counts,
            "synthetic": self.synthetic,
            "markets": [m.to_dict() for m in self.markets],
            "coins": {
                c.coin: {
                    "aligned_h": c.aligned.coverage_s / 3600,
                    "included": c.included,
                    "costs": {f"{v}_{d}": str(cost) for (v, d), cost in sorted(c.aligned.costs.items())},
                    "edges": {d: c.edge_stats(d) for d in DIRECTIONS},
                    "opportunities": {f"{v}@{m}": c.cell(v, m).to_dict() for v in VARIANTS for m in EVAL_MARGINS},
                }
                for c in self.coins
            },
            "missing_coins": self.missing_coins,
            "totals": {f"{v}@{m}": cell.to_dict() for (v, m), cell in sorted(self.totals.items())},
            "dollar": self.dollar.to_dict(),
            "recommendation": self.recommendation.to_dict(),
        }


def _latest_data_ts(store: Store) -> tuple[float | None, str]:
    """Default window end: the newest order book (the survey is about books), else the end of the
    newest USDTTMN candle. Candles alone never move the end past the books: a later ``backfill``
    would otherwise push the recorded hours out of the window."""
    _, books_hi = store.time_range("books")
    if books_hi is not None:
        return books_hi, "the newest recorded order book"
    candle = store.latest_candle_ts(USDTTMN, "1")
    if candle is not None:
        return float(candle + 60), "the newest USDTTMN candle (no order books recorded)"
    return None, ""


def open_store(path: str | Path) -> tuple[Store | None, str]:
    """Open the recorder database read-only (never creates or modifies it).

    Returns ``(None, why)`` when there is nothing to read: no file, or a file without the store's
    tables (e.g. an empty file). Other database errors propagate.
    """
    path = Path(path)
    if not path.is_file():
        return None, f"database not found ({path})"
    store = Store.open_readonly(path)
    try:
        store.time_range("books")
    except sqlite3.OperationalError as e:
        store.close()
        if "no such table" in str(e):
            return None, f"database {path} has no recorded market data (no `books` table)"
        raise
    return store, ""


def run_survey(
    store: Store | None,
    cfg: Config,
    params: SurveyParams | None = None,
    db_path: str = "",
    now: float | None = None,
    store_note: str = "",
) -> SurveyResult:
    """Survey ``store``; ``None`` = nothing to read (``store_note`` says why, see ``open_store``).

    ``now`` is only used for the report header and as the window end of an empty store; with data
    the window ends at the newest recorded order book (or ``params.until``).
    """
    params = params or SurveyParams()
    generated = time.time() if now is None else now
    git = gitinfo.get()
    s1 = cfg.strategies.s1_crossbook
    latest, latest_note = _latest_data_ts(store) if store is not None else (None, "")
    if params.until is not None:
        until, until_note = params.until, "--until"
    elif latest is not None:
        until, until_note = latest, latest_note
    else:
        until, until_note = generated, "now (no recorded data)"
    from_ts = until - params.since_s

    series: dict[str, BookSeries] = {}
    used: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    books_range: tuple[float | None, float | None] = (None, None)
    dollar = DollarStats("none", 0, [], [], [], {}, 0)
    if store is not None:
        books_range = store.time_range("books")
        symbols = store.book_counts_by_symbol(since_ts=from_ts - params.max_age_s)
        series, used, excluded = load_book_series(store, symbols, from_ts, until, params)
        dollar = dollar_stats(store, series.get(USDTTMN), cfg, from_ts, until)
    markets = survey_markets(series, cfg)

    base: dict[str, Any] = {
        "db_path": db_path,
        "params": params,
        "from_ts": from_ts,
        "to_ts": until,
        "until_note": until_note,
        "generated_ts": generated,
        "git_commit": str(git["commit"]),
        "git_dirty": bool(git["dirty"]),
        "config_hash": cfg.config_hash,
        "s1_cfg": s1,
        "store_books_range": books_range,
        "source_counts": dict(sorted(used.items())),
        "excluded_counts": dict(sorted(excluded.items())),
        "dollar": dollar,
    }
    if not used and dollar.candles_in_window == 0:
        if store is None:
            why = store_note or f"database not found ({db_path or 'no path given'})"
            message = f"no recorded data in window: {why}."
        else:
            message = "no recorded data in window: no order-book snapshots and no USDTTMN candles"
            if params.sources:
                message += f" with source in {{{', '.join(params.sources)}}}"
            message += f" between {iso(from_ts)} and {iso(until)}."
        return SurveyResult(
            status="no_data",
            message=message,
            markets=[],
            coins=[],
            missing_coins={},
            totals={},
            recommendation=recommend({}, 0.0, s1, params.min_coverage_h),
            fees={},
            **base,
        )

    usdt = series.get(USDTTMN)
    market_rows: list[MarketStats] = []
    for sym in sorted(series):
        quote = markets[sym].quote if sym in markets else ""
        stats = market_stats(series[sym], quote, usdt, from_ts, params)
        if stats is not None:
            market_rows.append(stats)

    cfg_taker = apply_changes(cfg, {"strategies.s1_crossbook.leg1_taker": True})
    coins: list[CoinSurvey] = []
    missing: dict[str, list[str]] = {}
    for coin in sorted(set(s1.coins)):
        aligned = align_coin(coin, series, markets, cfg_taker, params, from_ts, until)
        if aligned is None:
            missing[coin] = [s for s in (f"{coin}TMN", f"{coin}USDT", USDTTMN) if s not in series]
            continue
        coins.append(survey_coin(aligned, params))
    totals = total_cells(coins)
    coverage_h = max((c.aligned.coverage_s for c in coins if c.included), default=0.0) / 3600
    fees: dict[str, tuple[Decimal, Decimal]] = {}
    for m in markets.values():
        fees.setdefault(m.quote, (m.maker_fee, m.taker_fee))
    return SurveyResult(
        status="ok",
        message="",
        markets=market_rows,
        coins=coins,
        missing_coins=missing,
        totals=totals,
        recommendation=recommend(totals, coverage_h, s1, params.min_coverage_h),
        fees=dict(sorted(fees.items())),
        **base,
    )


# ---------------------------------------------------------------- report


def _pct(x: float | Decimal | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.{digits}f}%"


def _num(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{x:,.{digits}f}"


def _dur(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds:.0f} s"


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f} h"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]], align: str) -> list[str]:
    seps = {"l": "---", "r": "---:", "c": ":---:"}
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(seps[a] for a in align) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v:,}" for k, v in sorted(counts.items())) or "none"


def _warnings(r: SurveyResult) -> list[str]:
    out: list[str] = []
    if r.synthetic:
        n = r.source_counts.get("synthetic", 0)
        out += [
            f"> **WARNING: SYNTHETIC — not evidence.** {n:,} of the {r.snapshots:,} order-book snapshots in this "
            "window have source `synthetic`: they come from the offline demo feed (`wta.data.synthetic`), not "
            "from Wallex. The tables below only show that the survey pipeline works; they say nothing about "
            "the real markets, and the recommendation must not be copied into the live config.",
            "",
        ]
        if r.snapshots > n:
            out += [
                "> The window mixes synthetic and other books. Re-run with `--source ws,rest` to survey only "
                "the recorded Wallex books.",
                "",
            ]
    other = {k: v for k, v in r.source_counts.items() if k in NON_LIVE_SOURCES and k != "synthetic"}
    if other:
        out += [f"> Note: {_counts(other)} snapshots are not live market data (replay/backtest sources).", ""]
    return out


def _header_rows(r: SurveyResult) -> list[tuple[str, str]]:
    p = r.params
    first, last = r.data_first_ts, r.data_last_ts
    if first is not None and last is not None:
        data = f"{iso(first)} → {iso(last)} ({_hours(last - first)})"
    else:
        data = "no order books in the window"
    filt = f"filter `--source {','.join(p.sources)}`" if p.sources else "no source filter"
    rows = [
        ("Database", f"`{r.db_path}`" if r.db_path else "n/a"),
        ("Window", f"{iso(r.from_ts)} → {iso(r.to_ts)} (last {_hours(p.since_s)}, ending at {r.until_note})"),
        ("Order books in window", data),
        ("Snapshots", f"{r.snapshots:,} in {len(r.markets)} markets ({filt})"),
        ("Sources", _counts(r.source_counts)),
    ]
    if r.excluded_counts:
        rows.append(("Excluded by the source filter", _counts(r.excluded_counts)))
    synth = r.source_counts.get("synthetic", 0)
    rows.append(("Synthetic data", f"**YES** — {synth:,} snapshots with source `synthetic`" if synth else "no"))
    lo, hi = r.store_books_range
    if lo is not None and hi is not None:
        rows.append(("Order books in the whole store", f"{iso(lo)} → {iso(hi)} (all sources)"))
    d = r.dollar
    candle_txt = {
        "store": f"{d.candles_in_window:,} one-minute candles from the store in the window",
        "book mids": f"{d.candles_in_window:,} one-minute candles derived from USDTTMN book mids (none stored)",
        "none": "none",
    }[d.candle_source]
    rows.append(("USDTTMN candles", candle_txt))
    if r.fees:
        fee_txt = "; ".join(
            f"{q} pairs maker {_pct(mk, 2)} / taker {_pct(tk, 2)}" for q, (mk, tk) in sorted(r.fees.items())
        )
        rows.append(("Fees", f"{fee_txt} (offline snapshot `configs/fees_snapshot.yaml`)"))
    s1 = r.s1_cfg
    rows += [
        ("S1 config now", f"margin_min_pct {s1.margin_min_pct}, leg1_taker {str(s1.leg1_taker).lower()}"),
        ("Git commit", f"`{r.git_commit}`" + (" (working tree has uncommitted changes)" if r.git_dirty else "")),
        ("Config hash", f"`{r.config_hash}`"),
        ("Generated", iso(r.generated_ts)),
        (
            "Method",
            f"grid {p.grid_s:g} s; as-of join, max book age {p.max_age_s:g} s; gap = more than {p.gap_s:g} s "
            f"between snapshots; new opportunity >= {p.separation_s:g} s after the previous one ended",
        ),
    ]
    return rows


def _recommendation_lines(r: SurveyResult) -> list[str]:
    rec = r.recommendation
    viable = {True: "yes", False: "no", None: "unknown (insufficient data)"}[rec.lockin_viable]
    out = ["## Recommendation (computed, not LLM)", ""]
    if r.synthetic:
        out += ["**Computed from synthetic data: do not copy into the live config.**", ""]
    if rec.status != "ok":
        out += ["**Insufficient data — the configured values are kept.**", ""]
    out += [
        "```yaml",
        f"strategies.s1_crossbook.margin_min_pct: {rec.margin_min_pct}",
        f"strategies.s1_crossbook.leg1_taker: {str(rec.leg1_taker).lower()}",
        f"S1 lock-in viable: {viable}",
        "```",
        "",
    ]
    out += [f"- {reason}" for reason in rec.reasons]
    out.append("")
    return out


def _market_lines(r: SurveyResult) -> list[str]:
    out = ["## 1. Markets", ""]
    if not r.markets:
        return out + ["No order books in the window.", ""]
    rows = [
        [
            m.symbol,
            f"{m.snapshots:,}",
            ", ".join(m.sources),
            _hours(m.last_ts - m.first_ts),
            _pct(m.spread_median),
            _pct(m.spread_p90),
            _num(m.depth_bid_tmn_median),
            _num(m.depth_ask_tmn_median),
            str(m.gaps),
            _dur(m.longest_gap_s) if m.gaps else "-",
            _dur(m.gap_total_s) if m.gaps else "-",
        ]
        for m in r.markets
    ]
    headers = ["Market", "Snapshots", "Source", "Span", "Median spread", "p90 spread",
               "Median bid depth 0.3% (TMN)", "Median ask depth 0.3% (TMN)", f"Gaps > {r.params.gap_s:g} s",
               "Longest gap", "Time in gaps"]
    out += _table(headers, rows, "lrlrrrrrrrr")
    out += [
        "",
        "Span = first to last snapshot in the window. Depth = notional of the stored levels within 0.3 % of mid "
        "(`book_features`); USDT books are converted at the USDTTMN mid of the same moment (as-of, same max age). "
        "Spread = (ask - bid) / mid. A gap = more than "
        f"{r.params.gap_s:g} s between two consecutive snapshots of the market.",
        "",
    ]
    return out


def _cross_lines(r: SurveyResult, png_name: str | None) -> list[str]:
    out = ["## 2. Cross-book (S1 lock-in)", ""]
    if r.missing_coins:
        miss = "; ".join(f"{c} ({', '.join(s)})" for c, s in sorted(r.missing_coins.items()))
        out += [f"Configured S1 coins without all three books in the window: {miss}.", ""]
    if not r.coins:
        return out + ["No coin has XTMN, XUSDT and USDTTMN books in the window: nothing to align.", ""]

    out += ["### 2.1 Coverage and edge levels", ""]
    rows = []
    for c in r.coins:
        a = c.aligned
        et, eu = c.edge_stats("tmn_cheap"), c.edge_stats("usdt_cheap")
        rows.append([
            c.coin,
            _hours(a.coverage_s),
            _pct(a.costs[("taker", "tmn_cheap")], 2),
            f"{_pct(a.costs[('maker', 'tmn_cheap')], 2)} / {_pct(a.costs[('maker', 'usdt_cheap')], 2)}",
            f"{_pct(et['median'])} / {_pct(et['p90'])} / {_pct(et['max'])}",
            f"{_pct(eu['median'])} / {_pct(eu['p90'])} / {_pct(eu['max'])}",
            f"{_pct(et['share_ge_taker'], 2)} / {_pct(eu['share_ge_taker'], 2)}",
            "yes" if c.included else f"no (< {r.params.min_coverage_h:g} h)",
        ])
    headers = ["Coin", "Aligned data", "Cost taker", "Cost maker (TMN-cheap / USDT-cheap)",
               "Edge TMN-cheap median / p90 / max", "Edge USDT-cheap median / p90 / max",
               "Time edge >= taker cost (TMN / USDT)", "In totals"]
    out += _table(headers, rows, "lrrrrrrl")
    out += [
        "",
        "`edge_tmn_cheap_pct = bid(XUSDT) x bid(USDTTMN) / ask(XTMN) - 1` (buy the TMN book, sell the USDT book); "
        "`edge_usdt_cheap_pct = bid(XTMN) / (ask(XUSDT) x ask(USDTTMN)) - 1` — computed by "
        "`wta.features.engine.cross_features`. Cost taker = taker fee leg 1 + taker fee leg 2; cost maker = maker "
        "fee leg 1 + taker fee leg 2.",
        "",
    ]

    out += ["### 2.2 Distinct opportunities — all coins in totals", ""]
    if not r.totals:
        out += [f"No coin has at least {r.params.min_coverage_h:g} h of aligned data: no totals.", ""]
    else:
        rows = []
        for variant in VARIANTS:
            for margin in EVAL_MARGINS:
                cell = r.totals[(variant, margin)]
                tag = " (fallback)" if margin not in SURVEY_MARGINS else ""
                rows.append([
                    f"leg 1 {variant}",
                    _pm(margin) + tag,
                    f"{cell.count:,}",
                    _num(cell.per_day, 1),
                    _dur(cell.median_duration_s),
                    _pct(cell.gross_edge_sum, 2),
                    _pct(cell.net_edge_sum, 2),
                    str(cell.censored),
                ])
        headers = ["Variant", "Margin", "Opportunities", "Per day", "Median duration", "Gross edge sum",
                   "Net edge sum", "Open at end"]
        out += _table(headers, rows, "lrrrrrrr")
        out += [
            "",
            "Opportunity = edge >= cost + margin at a grid point, at least "
            f"{r.params.separation_s:g} s after the previous one (same coin and direction) ended; duration = "
            "seconds until edge < cost. Gross edge = edge at the trigger point (before fees); net = gross - cost. "
            "Per day = count x 24 h / aligned hours, added over coins.",
            "",
        ]

    out += ["### 2.3 Per coin", ""]
    rows = []
    for c in r.coins:
        for variant in VARIANTS:
            for margin in EVAL_MARGINS:
                cell = c.cell(variant, margin)
                rows.append([
                    c.coin,
                    f"leg 1 {variant}",
                    _pm(margin),
                    str(cell.direction("tmn_cheap")),
                    str(cell.direction("usdt_cheap")),
                    str(cell.count),
                    _num(cell.per_day, 1),
                    _dur(cell.median_duration_s),
                    _pct(cell.gross_edge_sum, 2),
                ])
    headers = ["Coin", "Variant", "Margin", "TMN-cheap", "USDT-cheap", "Total", "Per day", "Median duration",
               "Gross edge sum"]
    out += _table(headers, rows, "lllrrrrrr")
    out.append("")
    if png_name:
        out += [f"![Cross-book edges per coin]({png_name})", ""]
    return out


def _dollar_lines(r: SurveyResult) -> list[str]:
    d = r.dollar
    out = ["## 3. USDTTMN (the dollar)", ""]
    if d.candle_source == "none":
        return out + ["No USDTTMN candles or books in the window.", ""]
    src = "1-minute candles from the store" if d.candle_source == "store" else (
        "1-minute candles derived from the recorded USDTTMN book mids (the store has no USDTTMN candles)")
    out += [f"Source: {src}; {d.candles_in_window:,} candles in the window, plus up to 25 h before it as history.",
            ""]
    out += _table(
        ["Measure", "n", "Median", "p90"],
        [["15-min ATR(14) % of close (engine `atr_15m_pct` at each 15-min step)", str(len(d.atr_pct)),
          _pct(_median(d.atr_pct)), _pct(_percentile(d.atr_pct, 90))]],
        "lrrr",
    )
    out.append("")
    rows = []
    for name, values in (("1 h", d.ret_1h), ("24 h", d.ret_24h)):
        q = quantiles(values)
        if q is None:
            rows.append([name, "0"] + ["n/a"] * 7)
        else:
            rows.append([name, f"{int(q['n']):,}"] + [_pct(q[k]) for k in ("min", "p5", "p25", "p50", "p75", "p95",
                                                                            "max")])
    out += _table(["Return (close to close, every minute)", "n", "min", "p5", "p25", "median", "p75", "p95", "max"],
                  rows, "lrrrrrrrr")
    out.append("")
    valid = sum(v for k, v in d.regimes.items() if k != "n/a")
    rows = []
    for regime in REGIME_ORDER:
        n = d.regimes.get(regime, 0)
        if regime == "n/a":
            if n:
                rows.append(["n/a (less than 24 h of candles before the step)", str(n), "-",
                             f"{n * REGIME_STEP_S / 3600:.2f}"])
            continue
        rows.append([regime, str(n), _pct(n / valid if valid else None, 1), f"{n * REGIME_STEP_S / 3600:.2f}"])
    out += _table(["Rules regime (`rule_regime`)", "15-min steps", "Share", "Hours"], rows, "lrrr")
    out += [
        "",
        "Regime at each 15-minute step = `wta.strategies.s2_usdtalloc.rule_regime` over "
        "`wta.features.engine.candle_features` of the last 25 h of completed 1-minute candles — the S2 "
        "rules-only fallback exactly as the engine computes it. Shares exclude n/a steps.",
        "",
    ]
    return out


def _method_lines(r: SurveyResult) -> list[str]:
    p = r.params
    return [
        "## Method and limitations",
        "",
        "- Edges are executable at the best level only, with fees from the offline fee snapshot. Slippage beyond "
        "the best level, latency between the two legs, partial fills and queue position are not modelled: real "
        "captured edges will be smaller.",
        f"- The recorder does not store identical consecutive snapshots, so a gap > {p.gap_s:g} s can also be an "
        f"unchanged book. The as-of join treats a book older than {p.max_age_s:g} s as missing (conservative: "
        "it lowers coverage and can only end opportunities early).",
        "- Depth is limited to the stored levels (10 per side); a thin book may reach less than 0.3 % from mid.",
        "- Candles carry no source tag: when the books are synthetic, the candles in the same store most likely are "
        "too.",
        "- The maker-leg-1 variant only changes the fee (maker instead of taker on leg 1); it assumes leg 1 fills "
        "at the same executable price and does not model the chance that a resting order is not filled before "
        "the edge closes — hence the median-duration condition below.",
        "- Per-day numbers are rates over the aligned hours of each coin; a short window extrapolates a few hours "
        "to a day.",
        f"- Recommendation rules: if the taker variant gives < {PRD03_MIN_PER_DAY:g}/day at {_pm(PRD03_MARGIN)} "
        f"(PRD-03 §3) the margin is {_pm(FALLBACK_MARGIN)}; otherwise the largest evaluated margin >= "
        f"{_pm(PRD03_MARGIN)} with >= {TARGET_PER_DAY:g} taker opportunities/day (else {_pm(PRD03_MARGIN)}); "
        f"leg1_taker=false only if the maker variant gives >= {MAKER_RATIO:g}x the opportunities with a median "
        f"duration > {MAKER_MIN_MEDIAN_S:g} s; lock-in viable = >= {TARGET_PER_DAY:g}/day at a margin >= "
        f"{_pm(VIABLE_MIN_MARGIN)}; coins with less than {p.min_coverage_h:g} h of aligned data are left out.",
        "",
    ]


def render_markdown(r: SurveyResult, png_name: str | None = None) -> str:
    """The judge-facing report (``docs/REPORTS/survey_<YYYYMMDD>.md``)."""
    out = [
        f"# Market survey — {r.report_day}",
        "",
        "Generated by `scripts/survey.py` (PRD-05 §2). Every number below is computed from the recorded data "
        "named in the header; nothing is estimated or written by an LLM.",
        "",
    ]
    out += _warnings(r)
    if r.status == "no_data":
        out += [
            "## No recorded data in window",
            "",
            f"**No recorded data in window** — {r.message.removeprefix('no recorded data in window: ')}",
            "",
            "Nothing below is computed from market data. Record books with `python -m wta record` "
            "(or `make record`) for a few hours and run `make survey` again.",
            "",
        ]
    out += ["## Data", ""]
    out += _table(["Field", "Value"], [list(row) for row in _header_rows(r)], "ll")
    out.append("")
    out += _recommendation_lines(r)
    if r.status == "ok":
        out += _market_lines(r)
        out += _cross_lines(r, png_name)
        out += _dollar_lines(r)
    out += _method_lines(r)
    return "\n".join(out).rstrip() + "\n"


def summary_text(r: SurveyResult) -> str:
    """Short console summary (the CLI prints it)."""
    lines = [f"Market survey {r.report_day}: window {iso(r.from_ts)} -> {iso(r.to_ts)}"]
    if r.synthetic:
        lines.append("WARNING: SYNTHETIC — not evidence (books with source 'synthetic')")
    if r.status == "no_data":
        lines.append(r.message)
    else:
        lines.append(f"{r.snapshots:,} snapshots in {len(r.markets)} markets; sources: {_counts(r.source_counts)}")
        for c in r.coins:
            lines.append(f"  {c.coin}: {_hours(c.aligned.coverage_s)} aligned"
                         + ("" if c.included else " (not in totals)"))
        if r.totals:
            for variant in VARIANTS:
                rates = " | ".join(
                    f"{_pm(m)} {_num(r.totals[(variant, m)].per_day, 1)}" for m in SURVEY_MARGINS
                )
                lines.append(f"  opportunities/day, leg 1 {variant}: {rates}")
    rec = r.recommendation
    viable = {True: "yes", False: "no", None: "unknown"}[rec.lockin_viable]
    lines.append(
        f"Recommendation ({rec.status}): margin_min_pct={rec.margin_min_pct} "
        f"leg1_taker={str(rec.leg1_taker).lower()} S1 lock-in viable: {viable}"
    )
    return "\n".join(lines)


def plot_edges(r: SurveyResult, path: Path) -> bool:
    """PNG of the edge time series per coin (Agg backend, no pyplot state). False if nothing to plot."""
    coins = [c for c in r.coins if c.aligned.valid_points > 0]
    if not coins:
        return False
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    t0 = min(c.aligned.times[0] for c in coins)
    fig = Figure(figsize=(10.0, 1.0 + 2.3 * len(coins)), dpi=110, facecolor=_SURFACE)
    FigureCanvasAgg(fig)
    axes = fig.subplots(len(coins), 1, sharex=True, squeeze=False)[:, 0]
    for ax, c in zip(axes, coins, strict=True):
        a = c.aligned
        x = np.asarray([(t - t0) / 3600 for t in a.times], dtype=float)
        ax.set_facecolor(_SURFACE)
        for d, color, label in (("tmn_cheap", _BLUE, "TMN book cheap"), ("usdt_cheap", _ORANGE, "USDT book cheap")):
            y = np.asarray([NAN if e is None else float(e) * 100 for e in a.edges[d]], dtype=float)
            ax.plot(x, y, color=color, linewidth=0.9, label=label)
        taker = float(a.costs[("taker", "tmn_cheap")]) * 100
        maker = min(float(a.costs[("maker", d)]) for d in DIRECTIONS) * 100
        ax.axhline(taker, color=_INK2, linestyle="--", linewidth=0.9)
        ax.axhline(maker, color=_INK2, linestyle=":", linewidth=0.9)
        ax.axhline(0.0, color=_GRID, linewidth=0.8)
        ax.annotate(f"taker cost {taker:.2f}%", xy=(1.0, taker), xycoords=("axes fraction", "data"),
                    xytext=(-4, 3), textcoords="offset points", ha="right", va="bottom", fontsize=7, color=_INK2)
        ax.annotate(f"maker cost {maker:.2f}%", xy=(1.0, maker), xycoords=("axes fraction", "data"),
                    xytext=(-4, -3), textcoords="offset points", ha="right", va="top", fontsize=7, color=_INK2)
        ax.set_title(f"{c.coin}: executable cross-book edge", loc="left", fontsize=9, color=_INK)
        ax.set_ylabel("edge %", fontsize=8, color=_INK2)
        ax.grid(axis="y", color=_GRID, linewidth=0.6)
        ax.tick_params(labelsize=7, colors=_INK2)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(_GRID)
    axes[0].legend(loc="upper left", frameon=False, fontsize=7, ncol=2)
    axes[-1].set_xlabel(f"hours since {iso(t0)}", fontsize=8, color=_INK2)
    title = "Cross-book edges (survey)"
    if r.synthetic:
        title += " — SYNTHETIC, not evidence"
    fig.suptitle(title, fontsize=10, color=_INK, x=0.01, ha="left")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=_SURFACE)
    return True


def write_report(r: SurveyResult, out_dir: Path, png: bool = False, json_out: bool = False) -> list[Path]:
    """Write ``survey_<YYYYMMDD>.md`` (+ ``_edges.png`` / ``.json``) into ``out_dir``. Returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"survey_{r.report_day}"
    written: list[Path] = []
    png_name: str | None = None
    if png:
        png_path = out_dir / f"{stem}_edges.png"
        if plot_edges(r, png_path):
            png_name = png_path.name
            written.append(png_path)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(render_markdown(r, png_name), encoding="utf-8")
    written.insert(0, md_path)
    if json_out:
        json_path = out_dir / f"{stem}.json"
        json_path.write_text(json.dumps(r.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(json_path)
    return written
