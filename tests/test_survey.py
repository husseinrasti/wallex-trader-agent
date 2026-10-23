"""Market survey (PRD-05 §2): ``wta.research.survey`` + ``scripts/survey.py``.

The data is a temp store filled by the deterministic ``SyntheticFeed`` (3 simulated hours, books
every 2 s, frequent dislocation jumps) with three deliberate recording holes. The survey's numbers
are checked against an independent brute-force computation over the raw books: a two-pointer as-of
join instead of bisect, the edge formulas written out instead of ``cross_features``, and
opportunities found from maximal runs of ``edge >= cost`` instead of the survey's state machine.
Nothing touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import math
import statistics
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wta.config import ROOT, Config, load_config
from wta.data.store import Store
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot, snapshot_reference
from wta.models import BookSnapshot, Candle
from wta.research import survey as sv
from wta.utils.timeutil import iso, utc_day

from .conftest import T0

SYMS = ["USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT"]
COINS = ("BTC", "ETH")
STEP_S = 2.0
HOURS = 3.0
NOW = T0 + 4 * 3600  # report "generated" time (never the wall clock in these tests)
# recording holes (symbol, from, to) in seconds after T0: books in [from, to) are not stored
HOLES = (("ETHTMN", 3600.0, 3646.0), ("BTCUSDT", 5400.0, 5426.0), ("USDTTMN", 7200.0, 7212.0))
EXPECTED_GAPS = {"ETHTMN": 48.0, "BTCUSDT": 28.0, "USDTTMN": 14.0}  # last stored before the hole -> first after
DIRS = {"tmn_cheap": 1, "usdt_cheap": 2}  # index into a brute-force grid row


# ---------------------------------------------------------------- data


def _record(
    path: Path,
    hours: float = HOURS,
    source: str | None = None,
    candles: bool = True,
    holes: tuple[tuple[str, float, float], ...] = HOLES,
    start: float = T0,
) -> Path:
    """Fill a store at ``path`` with the synthetic feed; ``source`` relabels the books."""
    markets = load_markets_snapshot()
    ref = snapshot_reference()
    store = Store(path, flush_rows=5000)
    bus = MarketDataBus()

    def on_book(book: BookSnapshot) -> None:
        rel = book.ts - T0
        if any(book.symbol == sym and lo <= rel < hi for sym, lo, hi in holes):
            return
        store.add_book(book if source is None else book.model_copy(update={"source": source}))

    bus.on_book(on_book)
    feed = SyntheticFeed(
        markets={s: markets[s] for s in SYMS},
        symbols=list(SYMS),
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        params=SyntheticParams(seed=3, jump_rate_per_hour=4.0, jump_size_pct=0.008),
        on_candle=store.upsert_candles if candles else None,
    )
    feed.publish(start)
    for i in range(1, int(hours * 3600 / STEP_S)):
        feed.step(STEP_S)
        feed.publish(start + i * STEP_S)
    store.close()
    return path


@pytest.fixture(scope="module")
def survey_cfg() -> Config:
    return load_config(env={})


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _record(tmp_path_factory.mktemp("survey") / "wta.sqlite")


@pytest.fixture(scope="module")
def ro_store(synth_db: Path) -> Iterator[Store]:
    store = Store.open_readonly(synth_db)
    yield store
    store.close()


@pytest.fixture(scope="module")
def result(ro_store: Store, synth_db: Path, survey_cfg: Config) -> sv.SurveyResult:
    params = sv.SurveyParams(since_s=HOURS * 3600)
    return sv.run_survey(ro_store, survey_cfg, params, db_path=str(synth_db), now=NOW)


# ---------------------------------------------------------------- brute force


def _lin_percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    k = q * (len(s) - 1)
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _depth(levels: list[Any], mid: Decimal, bid_side: bool) -> Decimal:
    if bid_side:
        return sum((lv.price * lv.qty for lv in levels if lv.price >= mid * Decimal("0.997")), Decimal(0))
    return sum((lv.price * lv.qty for lv in levels if lv.price <= mid * Decimal("1.003")), Decimal(0))


def _brute_market(store: Store, sym: str, quote: str, from_ts: float, to_ts: float) -> dict[str, Any]:
    books = [b for b in store.iter_books(sym) if from_ts <= b.ts <= to_ts]
    dollar = list(store.iter_books("USDTTMN"))
    j = -1
    depth_b: list[float] = []
    depth_a: list[float] = []
    for b in books:
        if quote == "TMN":
            rate: float | None = 1.0
        else:
            while j + 1 < len(dollar) and dollar[j + 1].ts <= b.ts:
                j += 1
            ok = j >= 0 and b.ts - dollar[j].ts <= 10.0
            rate = float(dollar[j].mid) if ok else None  # type: ignore[arg-type]
        if rate is None:
            continue
        depth_b.append(float(_depth(b.bids, b.mid, True)) * rate)  # type: ignore[arg-type]
        depth_a.append(float(_depth(b.asks, b.mid, False)) * rate)  # type: ignore[arg-type]
    spreads = [float(b.spread_pct) for b in books]  # type: ignore[arg-type]
    gaps = [b2.ts - b1.ts for b1, b2 in zip(books, books[1:], strict=False) if b2.ts - b1.ts > 10.0]
    return {
        "snapshots": len(books),
        "spread_median": statistics.median(spreads),
        "spread_p90": _lin_percentile(spreads, 0.9),
        "depth_bid": statistics.median(depth_b),
        "depth_ask": statistics.median(depth_a),
        "gaps": gaps,
    }


def _brute_grid(
    store: Store, coin: str, from_ts: float, to_ts: float, grid: float = 2.0, max_age: float = 10.0
) -> list[tuple[float, Decimal | None, Decimal | None]]:
    """``(t, edge_tmn_cheap, edge_usdt_cheap)`` at every grid point of ``[from_ts, to_ts]``."""
    syms = (f"{coin}TMN", f"{coin}USDT", "USDTTMN")
    books = {s: list(store.iter_books(s)) for s in syms}
    ptr = dict.fromkeys(syms, -1)
    rows: list[tuple[float, Decimal | None, Decimal | None]] = []
    k = math.ceil(from_ts / grid)
    while k * grid <= to_ts:
        t = k * grid
        cur: dict[str, BookSnapshot | None] = {}
        for s in syms:
            while ptr[s] + 1 < len(books[s]) and books[s][ptr[s] + 1].ts <= t:
                ptr[s] += 1
            b = books[s][ptr[s]] if ptr[s] >= 0 else None
            cur[s] = b if b is not None and t - b.ts <= max_age else None
        tb, ub, u = (cur[s] for s in syms)
        if tb is None or ub is None or u is None:
            rows.append((t, None, None))
        else:
            e_tmn = ub.best_bid * u.best_bid / tb.best_ask - 1  # type: ignore[operator]
            e_usdt = tb.best_bid / (ub.best_ask * u.best_ask) - 1  # type: ignore[operator]
            rows.append((t, e_tmn, e_usdt))
        k += 1
    return rows


def _brute_costs(coin: str) -> dict[tuple[str, str], Decimal]:
    m = load_markets_snapshot()
    tm, um = m[f"{coin}TMN"], m[f"{coin}USDT"]
    return {
        ("taker", "tmn_cheap"): tm.taker_fee + um.taker_fee,
        ("taker", "usdt_cheap"): um.taker_fee + tm.taker_fee,
        ("maker", "tmn_cheap"): tm.maker_fee + um.taker_fee,
        ("maker", "usdt_cheap"): um.maker_fee + tm.taker_fee,
    }


def _brute_opps(
    points: list[tuple[float, Decimal | None]], cost: Decimal, margin: Decimal, sep: float, grid: float
) -> list[tuple[float, float, Decimal]]:
    """Each maximal run of valid points with edge >= cost holds at most one opportunity: its first
    point with edge >= cost + margin that is >= ``sep`` after the previous opportunity ended."""
    out: list[tuple[float, float, Decimal]] = []
    last_end: float | None = None
    i, n = 0, len(points)
    while i < n:
        e = points[i][1]
        if e is None or e < cost:
            i += 1
            continue
        j = i
        while j < n and points[j][1] is not None and points[j][1] >= cost:  # type: ignore[operator]
            j += 1
        end = points[j][0] if j < n else points[-1][0] + grid
        for t, edge in points[i:j]:
            if edge >= cost + margin and (last_end is None or t - last_end >= sep):  # type: ignore[operator]
                out.append((t, end, edge))  # type: ignore[arg-type]
                last_end = end
                break
        i = j
    return out


# ---------------------------------------------------------------- per-market statistics


def test_header_counts_and_window(result: sv.SurveyResult, ro_store: Store) -> None:
    newest = T0 + (HOURS * 3600 / STEP_S - 1) * STEP_S
    assert result.status == "ok"
    assert result.to_ts == newest  # the window ends at the newest recorded book
    assert result.from_ts == newest - HOURS * 3600
    assert result.until_note == "the newest recorded order book"
    stored = sum(1 for s in SYMS for b in ro_store.iter_books(s) if result.from_ts <= b.ts <= result.to_ts)
    assert result.snapshots == stored == sum(m.snapshots for m in result.markets)
    assert result.source_counts == {"synthetic": stored}
    assert result.synthetic
    assert [m.symbol for m in result.markets] == sorted(SYMS)


def test_market_stats_match_brute_force(result: sv.SurveyResult, ro_store: Store) -> None:
    for m in result.markets:
        quote = "USDT" if m.symbol.endswith("USDT") else "TMN"
        bf = _brute_market(ro_store, m.symbol, quote, result.from_ts, result.to_ts)
        assert m.snapshots == bf["snapshots"]
        assert m.spread_median == pytest.approx(bf["spread_median"], rel=1e-12)
        assert m.spread_p90 == pytest.approx(bf["spread_p90"], rel=1e-12)
        assert m.depth_bid_tmn_median == pytest.approx(bf["depth_bid"], rel=1e-9)
        assert m.depth_ask_tmn_median == pytest.approx(bf["depth_ask"], rel=1e-9)
        assert m.gaps == len(bf["gaps"])
        assert m.longest_gap_s == pytest.approx(max(bf["gaps"], default=0.0))
        assert m.gap_total_s == pytest.approx(sum(bf["gaps"]))
        # the deliberate holes are the only gaps
        assert m.gaps == (1 if m.symbol in EXPECTED_GAPS else 0)
        if m.symbol in EXPECTED_GAPS:
            assert m.longest_gap_s == pytest.approx(EXPECTED_GAPS[m.symbol])
        assert m.depth_bid_tmn_median and m.depth_bid_tmn_median > 0


# ---------------------------------------------------------------- cross-book


def test_cross_book_matches_brute_force(result: sv.SurveyResult, ro_store: Store) -> None:
    by_coin = {c.coin: c for c in result.coins}
    assert sorted(by_coin) == list(COINS)
    p = result.params
    total_found = 0
    for coin in COINS:
        cs = by_coin[coin]
        grid = _brute_grid(ro_store, coin, result.from_ts, result.to_ts)
        valid = sum(1 for _, e, _ in grid if e is not None)
        assert cs.aligned.valid_points == valid
        assert cs.aligned.coverage_s == valid * p.grid_s
        assert cs.aligned.costs == _brute_costs(coin)
        for variant in sv.VARIANTS:
            count = 0
            for margin in sv.EVAL_MARGINS:
                for d, col in DIRS.items():
                    points = [(row[0], row[col]) for row in grid]
                    want = _brute_opps(points, cs.aligned.costs[(variant, d)], margin, p.separation_s, p.grid_s)
                    got = [(o.start, o.end, o.edge) for o in cs.opportunities[(variant, margin, d)]]
                    assert got == want, (coin, variant, margin, d)
                    total_found += len(want)
                cell = cs.cell(variant, margin)
                pooled = [o for d in DIRS for o in cs.opportunities[(variant, margin, d)]]
                count = len(pooled)
                assert cell.count == count
                assert cell.per_day == pytest.approx(count * 86_400 / (valid * p.grid_s))
                assert cell.gross_edge_sum == sum((o.edge for o in pooled), Decimal(0))
                durations = [o.end - o.start for o in pooled]
                assert cell.median_duration_s == (statistics.median(durations) if durations else None)
    assert total_found > 0  # the synthetic dislocations do produce opportunities


def test_holes_make_grid_points_invalid(result: sv.SurveyResult) -> None:
    eth = next(c for c in result.coins if c.coin == "ETH").aligned
    edge_at = dict(zip(eth.times, eth.edges["tmn_cheap"], strict=True))
    # ETHTMN: last book T0+3598, next T0+3646 -> stale (> 10 s) from T0+3610 to T0+3644
    assert edge_at[T0 + 3608] is not None
    assert all(edge_at[T0 + t] is None for t in range(3610, 3646, 2))
    # USDTTMN: last book T0+7198, next T0+7212 -> age exactly 10 s at T0+7208 is still valid
    assert edge_at[T0 + 7208] is not None
    assert edge_at[T0 + 7210] is None
    assert edge_at[T0 + 7212] is not None
    assert eth.coverage_s < HOURS * 3600


def test_totals_add_per_coin_rates(result: sv.SurveyResult) -> None:
    for variant in sv.VARIANTS:
        for margin in sv.EVAL_MARGINS:
            cells = [c.cell(variant, margin) for c in result.coins if c.included]
            total = result.totals[(variant, margin)]
            assert total.count == sum(c.count for c in cells)
            assert total.per_day == pytest.approx(sum(c.per_day or 0.0 for c in cells))
    assert all(c.included for c in result.coins)
    # S1 coins without books in the store are listed, not silently dropped
    assert "SOL" in result.missing_coins and "SOLTMN" in result.missing_coins["SOL"]


def test_find_opportunities_rules() -> None:
    cost, margin = Decimal("0.0043"), Decimal("0.0010")
    edges = {
        0: None,
        2: Decimal("0.0050"),  # >= cost but below cost + margin: no trigger
        4: Decimal("0.0053"),  # exactly cost + margin: opens #1
        6: Decimal("0.0043"),  # exactly cost: still open
        8: Decimal("0.0042"),  # < cost: #1 ends at 8
        20: Decimal("0.0060"),  # 12 s after the end: ignored (same burst)
        22: Decimal("0.0010"),
        68: Decimal("0.0060"),  # exactly 60 s after the end of #1: opens #2
        70: None,  # a hole ends #2 at 70
        200: Decimal("0.0070"),  # opens #3, still open when the data ends
        202: Decimal("0.0050"),
    }
    times = sorted(edges)
    opps = sv.find_opportunities(times, [edges[t] for t in times], cost, margin, 60.0, end_ts=204.0)
    assert [(o.start, o.end, o.edge, o.censored) for o in opps] == [
        (4, 8, Decimal("0.0053"), False),
        (68, 70, Decimal("0.0060"), False),
        (200, 204.0, Decimal("0.0070"), True),
    ]
    assert opps[0].duration_s == 4
    assert opps[0].net == Decimal("0.0010")
    assert sv.find_opportunities([], [], cost, margin) == []


# ---------------------------------------------------------------- recommendation


def _cells(taker: dict[str, float], maker: dict[str, float] | None = None, maker_median: float = 30.0
           ) -> dict[tuple[str, Decimal], sv.OppCell]:
    out: dict[tuple[str, Decimal], sv.OppCell] = {}
    for m in sv.EVAL_MARGINS:
        t = taker.get(str(m), 0.0)
        mk = (maker if maker is not None else taker).get(str(m), 0.0)
        out[("taker", m)] = sv.OppCell(count=round(t), per_day=t, median_duration_s=10.0 if t else None)
        out[("maker", m)] = sv.OppCell(count=round(mk), per_day=mk, median_duration_s=maker_median if mk else None)
    return out


def test_recommend_insufficient_data(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    for totals, coverage in (({}, 0.0), (_cells({"0.0015": 50.0}), 0.5)):
        rec = sv.recommend(totals, coverage, s1, min_coverage_h=1.0)
        assert rec.status == "insufficient_data"
        assert (rec.margin_min_pct, rec.leg1_taker, rec.lockin_viable) == (s1.margin_min_pct, s1.leg1_taker, None)
        assert "insufficient data" in rec.reasons[0]


def test_recommend_largest_margin_meeting_target(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    taker = {"0.0005": 20, "0.0008": 15, "0.0010": 12, "0.0015": 8, "0.0025": 3.0, "0.0040": 2.9}
    rec = sv.recommend(_cells(taker), 30.0, s1)
    assert rec.status == "ok"
    assert rec.margin_min_pct == Decimal("0.0025")  # exactly 3.0/day qualifies; 2.9 does not
    assert rec.leg1_taker is True  # maker gives the same count (1.0x)
    assert rec.lockin_viable is True
    assert rec.low_activity is False
    assert not any("extrapolated" in r for r in rec.reasons)  # >= 24 h of data


def test_recommend_keeps_default_between_two_and_three_per_day(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    taker = {"0.0005": 6, "0.0008": 4, "0.0010": 3.0, "0.0015": 2.0, "0.0025": 1.0}
    rec = sv.recommend(_cells(taker), 6.0, s1)
    assert rec.margin_min_pct == Decimal("0.0015")  # 2.0/day at 0.15 % is not the PRD-03 trigger
    assert rec.low_activity is False
    assert rec.lockin_viable is True  # 3.0/day at 0.10 %
    assert any("extrapolated from 6.0 h" in r for r in rec.reasons)


def test_recommend_prd03_trigger_lowers_margin_and_picks_maker(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    taker = {"0.0005": 3.0, "0.0008": 2.5, "0.0010": 2.0, "0.0015": 1.99}
    maker = {"0.0005": 5.0, "0.0008": 3.75, "0.0010": 3.0, "0.0015": 2.5}  # 1.5x the taker count at 0.08 %
    rec = sv.recommend(_cells(taker, maker, maker_median=25.0), 12.0, s1)
    assert rec.low_activity is True
    assert rec.margin_min_pct == Decimal("0.0008")
    assert rec.leg1_taker is False
    assert rec.lockin_viable is True  # maker variant: 3.0/day at 0.10 %
    assert "PRD-03" in rec.reasons[0]


def test_recommend_prd03_trigger_keeps_taker_for_short_opportunities(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    taker = {"0.0005": 3.0, "0.0008": 2.5, "0.0010": 2.0, "0.0015": 1.0}
    maker = {"0.0008": 3.75, "0.0010": 3.0}
    rec = sv.recommend(_cells(taker, maker, maker_median=20.0), 12.0, s1)  # median not > 20 s
    assert (rec.margin_min_pct, rec.leg1_taker) == (Decimal("0.0008"), True)
    assert rec.lockin_viable is False  # taker: 2.0/day at 0.10 %


def test_recommend_not_viable_and_rare(survey_cfg: Config) -> None:
    s1 = survey_cfg.strategies.s1_crossbook
    rec = sv.recommend(_cells({"0.0005": 10, "0.0008": 5, "0.0010": 2.9, "0.0015": 2.5}), 24.0, s1)
    assert (rec.margin_min_pct, rec.lockin_viable, rec.low_activity) == (Decimal("0.0015"), False, False)
    rare = sv.recommend(_cells({"0.0005": 1.0, "0.0008": 0.5}), 24.0, s1)
    assert rare.margin_min_pct == Decimal("0.0008") and rare.lockin_viable is False
    assert any("rare" in r for r in rare.reasons)


def test_survey_recommendation_is_consistent_with_totals(result: sv.SurveyResult) -> None:
    rec = result.recommendation
    assert rec.status == "ok"
    assert rec == sv.recommend(result.totals, result.coverage_h, result.s1_cfg, result.params.min_coverage_h)
    assert rec.margin_min_pct in sv.EVAL_MARGINS


# ---------------------------------------------------------------- USDTTMN


def _usdt_candles(start: int, n: int, growth: Decimal) -> list[Candle]:
    out, p = [], Decimal("266000")
    for k in range(n):
        o, c = p, p * growth
        out.append(Candle(symbol="USDTTMN", resolution="1", ts=start + 60 * k, o=o,
                          h=max(o, c) * Decimal("1.0001"), l=min(o, c) * Decimal("0.9999"), c=c, v=Decimal(1)))
        p = c
    return out


@pytest.mark.parametrize(("growth", "regime"), [(Decimal("1.00002"), "trend_up"), (Decimal("1"), "range")])
def test_dollar_stats_regimes_and_returns(
    tmp_path: Path, survey_cfg: Config, growth: Decimal, regime: str
) -> None:
    start = int(T0)
    store = Store(tmp_path / "dollar.sqlite")
    store.upsert_candles(_usdt_candles(start, 31 * 60, growth))  # 25 h of history before the 6-h window
    store.flush()
    to_ts = start + 31 * 3600.0
    res = sv.run_survey(store, survey_cfg, sv.SurveyParams(since_s=6 * 3600), db_path="d", now=NOW)
    store.close()
    assert res.status == "ok"  # candles but no books: the dollar section still works
    assert res.to_ts == to_ts and res.until_note.startswith("the newest USDTTMN candle")
    d = res.dollar
    assert d.candle_source == "store"
    assert d.steps == 24 and d.regimes == {regime: 24}  # 6 h of 15-minute steps, 24 h of history each
    assert len(d.atr_pct) == 24
    assert d.candles_in_window == 6 * 60
    assert len(d.ret_1h) == len(d.ret_24h) == 6 * 60
    assert statistics.median(d.ret_1h) == pytest.approx(float(growth**60 - 1), abs=1e-12)
    assert statistics.median(d.ret_24h) == pytest.approx(float(growth**1440 - 1), abs=1e-12)
    if regime == "trend_up":
        assert 0.0002 < statistics.median(d.atr_pct) < 0.001
    md = sv.render_markdown(res)
    assert f"| {regime} | 24 | 100.0% | 6.00 |" in md


def test_dollar_stats_from_book_mids_without_candles(tmp_path: Path, survey_cfg: Config) -> None:
    db = _record(tmp_path / "nocandles.sqlite", hours=0.5, candles=False, holes=())
    store = Store.open_readonly(db)
    res = sv.run_survey(store, survey_cfg, sv.SurveyParams(since_s=1800), db_path=str(db), now=NOW)
    store.close()
    assert res.dollar.candle_source == "book mids"
    assert res.dollar.candles_in_window == 30
    assert res.dollar.regimes == {"n/a": 2}  # half an hour: no 24-h history
    assert "derived from USDTTMN book mids" in sv.render_markdown(res)


# ---------------------------------------------------------------- report


def test_report_has_honest_header_and_synthetic_warning(result: sv.SurveyResult, tmp_path: Path) -> None:
    paths = sv.write_report(result, tmp_path, png=True, json_out=True)
    md_path, png_path, json_path = paths
    assert md_path.name == f"survey_{utc_day(result.to_ts)}.md"
    md = md_path.read_text()
    first = md.index("WARNING: SYNTHETIC")
    assert first < md.index("## Data") < md.index("## Recommendation")
    assert "do not copy into the live config" in md
    assert f"| Snapshots | {result.snapshots:,} in 5 markets" in md
    assert f"| Sources | synthetic: {result.snapshots:,} |" in md
    assert f"| Window | {iso(result.from_ts)} → {iso(result.to_ts)}" in md
    assert f"`{result.git_commit}`" in md and result.git_commit != ""
    assert "strategies.s1_crossbook.margin_min_pct:" in md and "S1 lock-in viable:" in md
    assert f"![Cross-book edges per coin]({png_path.name})" in md
    assert png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    data = json.loads(json_path.read_text())
    assert data["synthetic"] is True and data["snapshots"] == result.snapshots
    assert data["recommendation"]["margin_min_pct"] == str(result.recommendation.margin_min_pct)


def test_no_synthetic_warning_for_recorded_sources(tmp_path: Path, survey_cfg: Config) -> None:
    db = _record(tmp_path / "rest.sqlite", hours=0.5, source="rest", holes=())
    store = Store.open_readonly(db)
    res = sv.run_survey(store, survey_cfg, sv.SurveyParams(since_s=1800), db_path=str(db), now=NOW)
    store.close()
    assert not res.synthetic and res.source_counts == {"rest": res.snapshots}
    md = sv.render_markdown(res)
    assert "SYNTHETIC" not in md
    assert "| Synthetic data | no |" in md
    # 0.5 h of aligned data is below min_coverage_h: no totals, the configured values are kept
    assert res.recommendation.status == "insufficient_data"
    assert "Insufficient data" in md
    # ... and the message names the best coin's real coverage (the per-coin table shows it), not 0 h
    best = max(c.aligned.coverage_s for c in res.coins) / 3600
    assert best > 0.4 and res.coverage_h == best
    assert (f"no coin has at least 1 h of aligned cross-book data (XTMN + XUSDT + USDTTMN books; best coin: "
            f"{best:.2f} h)") in res.recommendation.reasons[0]
    assert "0.00 h of aligned" not in md


def test_source_filter_excludes_synthetic(result: sv.SurveyResult, ro_store: Store, survey_cfg: Config) -> None:
    params = sv.SurveyParams(since_s=HOURS * 3600, sources=("rest", "ws"))
    res = sv.run_survey(ro_store, survey_cfg, params, db_path="x", now=NOW)
    assert res.snapshots == 0 and not res.synthetic
    assert res.excluded_counts == {"synthetic": result.snapshots}
    assert "| Excluded by the source filter | synthetic:" in sv.render_markdown(res)


def test_survey_is_deterministic(result: sv.SurveyResult, ro_store: Store, synth_db: Path,
                                 survey_cfg: Config) -> None:
    again = sv.run_survey(ro_store, survey_cfg, sv.SurveyParams(since_s=HOURS * 3600), db_path=str(synth_db),
                          now=NOW)
    assert again.to_dict() == result.to_dict()
    assert sv.render_markdown(again) == sv.render_markdown(result)


# ---------------------------------------------------------------- empty store


def test_empty_store_reports_no_recorded_data(tmp_path: Path, survey_cfg: Config) -> None:
    Store(tmp_path / "empty.sqlite").close()
    store, note = sv.open_store(tmp_path / "empty.sqlite")
    assert store is not None and note == ""
    res = sv.run_survey(store, survey_cfg, sv.SurveyParams(), db_path="empty.sqlite", now=NOW)
    store.close()
    assert res.status == "no_data"
    assert res.message.startswith("no recorded data in window")
    assert res.to_ts == NOW and res.until_note.startswith("now")
    assert res.recommendation.status == "insufficient_data"
    md = sv.render_markdown(res)
    assert "## No recorded data in window" in md and "SYNTHETIC" not in md
    assert "## 1. Markets" not in md  # nothing computed from market data


def test_open_store_without_data(tmp_path: Path) -> None:
    assert sv.open_store(tmp_path / "missing.sqlite") == (None, f"database not found ({tmp_path / 'missing.sqlite'})")
    assert not (tmp_path / "missing.sqlite").exists()  # never created
    (tmp_path / "zero.sqlite").write_bytes(b"")
    store, note = sv.open_store(tmp_path / "zero.sqlite")
    assert store is None and "no recorded market data" in note


# ---------------------------------------------------------------- CLI


def _cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("survey_cli", ROOT / "scripts" / "survey.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_writes_report_with_png(synth_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "reports"
    code = _cli().main(["--since", "3h", "--db", str(synth_db), "--out", str(out), "--png", "--json"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "WARNING: SYNTHETIC" in printed and "Recommendation (ok)" in printed
    day = "20261022"
    assert sorted(p.name for p in out.iterdir()) == [f"survey_{day}.json", f"survey_{day}.md",
                                                     f"survey_{day}_edges.png"]
    md = (out / f"survey_{day}.md").read_text()
    assert "WARNING: SYNTHETIC" in md and "| Git commit |" in md


def test_cli_until_and_since_hours(synth_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "reports"
    code = _cli().main(["--since", "1", "--until", "2026-10-22T11:30:00Z", "--db", str(synth_db), "--out",
                        str(out), "--json"])
    assert code == 0
    data = json.loads((out / "survey_20261022.json").read_text())
    assert data["window"]["from"] == "2026-10-22T10:30:00.000Z"
    assert data["window"]["to"] == "2026-10-22T11:30:00.000Z"
    assert data["window"]["until"] == "--until"
    # books every 2 s, both ends inclusive (1801), minus the holes inside [T0+1800, T0+5400]
    counts = {m["symbol"]: m["snapshots"] for m in data["markets"]}
    assert counts == {"BTCTMN": 1801, "BTCUSDT": 1800, "ETHTMN": 1801 - 23, "ETHUSDT": 1801, "USDTTMN": 1801}
    assert data["snapshots"] == sum(counts.values())


def test_cli_empty_and_missing_database_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _cli()
    Store(tmp_path / "empty.sqlite").close()
    for db in (tmp_path / "empty.sqlite", tmp_path / "missing.sqlite"):
        out = tmp_path / f"out_{db.stem}"
        assert cli.main(["--since", "24h", "--db", str(db), "--out", str(out)]) == 0
        assert "no recorded data in window" in capsys.readouterr().out
        (report,) = out.glob("survey_*.md")
        assert "No recorded data in window" in report.read_text()
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.parametrize("bad", [["--since", "abc"], ["--since", "0h"], ["--until", "yesterday"],
                                 ["--grid", "0"]])
def test_cli_rejects_bad_arguments(bad: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _cli().main([*bad, "--db", str(tmp_path / "x.sqlite"), "--out", str(tmp_path)])
    assert exc.value.code == 2


def test_parse_duration() -> None:
    assert sv.parse_duration("24h") == 86_400
    assert sv.parse_duration("6h") == 21_600
    assert sv.parse_duration("12") == 43_200  # bare number = hours
    assert sv.parse_duration("90m") == 5_400
    assert sv.parse_duration("2d") == 172_800
    for bad in ("", "h", "-1h", "abc", "nanh"):
        with pytest.raises(ValueError):
            sv.parse_duration(bad)
