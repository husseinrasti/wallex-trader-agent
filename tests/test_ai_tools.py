"""Analyst tools (PRD-04 §4–5): every internal and ``*_local`` tool returns JSON-safe data, errors
come back as dicts (never raised into the agent loop), ``truncate`` respects its byte budget, the
``get_candle_stats`` math is checked on hand-computable fixtures, and the JSON schemas agree with the
dispatcher. Pure in-memory fakes; no network, no wall clock."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from wta.ai.mcp_client import READ_ONLY_ALLOWLIST
from wta.ai.tools import (
    INTERNAL_TOOL_SCHEMAS,
    LOCAL_TOOL_SCHEMAS,
    InternalTools,
    ToolDeps,
    feature_rows,
    feature_view,
    is_feature_view,
    round_sig,
    to_jsonable,
    truncate,
)
from wta.config import Config, canonical_json
from wta.features.engine import FeatureSnapshot
from wta.models import BookSnapshot, Candle, Trade

T0 = 1_792_663_200  # 2026-10-22T10:00:00Z, a multiple of 15 minutes
SYM = "BTCUSDT"

# one representative argument set per tool; the coverage test below keeps this in sync with the schemas
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "get_features": {"symbols": [SYM]},
    "get_pnl_summary": {},
    "get_recent_decisions": {"n": 5},
    "get_health": {},
    "get_candle_stats": {"symbol": SYM, "resolution": "1", "n": 80},
    "wallex_basic_get_market_depth_local": {"symbol": SYM},
    "wallex_basic_candle_history_local": {"symbol": SYM, "resolution": "15", "n": 3},
    "wallex_basic_get_latest_trades_local": {"symbol": SYM},
}


def linear_candles(symbol: str, count: int, end_ts: int, base: int = 100, last_volume: int = 30) -> list[Candle]:
    """1-minute candles ending at ``end_ts``: close = base + i, high/low = close ± 1, volume 10
    (the last one ``last_volume``). Every true range is exactly 2."""
    start = end_ts - (count - 1) * 60
    out = []
    for i in range(count):
        c = Decimal(base + i)
        out.append(Candle(symbol=symbol, resolution="1", ts=start + i * 60, o=c - Decimal("0.5"), h=c + 1,
                          l=c - 1, c=c, v=Decimal(last_volume if i == count - 1 else 10)))
    return out


class Fake:
    """Mutable engine state behind ToolDeps; records what the tools asked for."""

    def __init__(self) -> None:
        self.features: FeatureSnapshot | None = None
        self.books: dict[str, BookSnapshot] = {}
        self.candles: dict[str, list[Candle]] = {}
        self.trades: dict[str, list[Trade]] = {}
        self.pnl: dict[str, Any] = {}
        self.health: dict[str, Any] = {}
        self.decision_n: list[int] = []
        self.candle_queries: list[tuple[str, float, float]] = []

    def candles_1m(self, symbol: str, frm: float, to: float) -> list[Candle]:
        self.candle_queries.append((symbol, frm, to))
        return [c for c in self.candles.get(symbol, []) if frm <= c.ts <= to]

    def trades_in(self, symbol: str, frm: float, to: float) -> list[Trade]:
        return [t for t in self.trades.get(symbol, []) if frm <= t.ts <= to]

    def recent(self, n: int) -> list[dict[str, Any]]:
        self.decision_n.append(n)
        return [{"kind": "intent", "reason": f"edge {i}", "pnl_tmn": Decimal(i) / 3} for i in range(n)]

    def deps(self, cfg: Config) -> ToolDeps:
        return ToolDeps(cfg=cfg, now=lambda: float(T0), features=lambda: self.features, books=lambda: self.books,
                        candles_1m=self.candles_1m, trades=self.trades_in, pnl_summary=lambda: self.pnl,
                        recent_decisions=self.recent, health=lambda: self.health)


@pytest.fixture
def fake(make_book: Callable[..., BookSnapshot]) -> Fake:
    f = Fake()
    f.features = FeatureSnapshot(
        ts=float(T0),
        symbols={SYM: {"mid": Decimal("82877.08"), "spread_pct": Decimal("0.000774"), "bars_15m": 96,
                       "ema20_15m": None, "trend_flag": 1},
                 "ETHUSDT": {"mid": Decimal("3001.5"), "spread_pct": Decimal("0.0005")}},
        cross={"BTC": {"dev_mid_pct": Decimal("-0.00123"), "dev_zscore": None}},
        dollar={"mid": Decimal("101500")},
    )
    f.books = {SYM: make_book(SYM, "82845.00", "82909.16", qty="0.5", ts=float(T0) - 1, levels=12,
                              level_step="0.5")}
    f.candles = {SYM: linear_candles(SYM, 80, T0 - 60)}
    f.trades = {SYM: [Trade(symbol=SYM, ts=float(T0) - 1200 + 20 * i, price=Decimal("82900") + i,
                            qty=Decimal("0.001"), side="BUY" if i % 2 else "SELL") for i in range(60)]}
    f.pnl = {"equity_tmn": Decimal("5012345.67"), "drawdown_pct": Decimal("0.004"), "daily_pnl_tmn": Decimal("-1200"),
             "realized_by_strategy": {"S1": Decimal("3400"), "S2": Decimal("0")}, "fees_paid_tmn": Decimal("812.5"),
             "open_positions": [{"symbol": SYM, "qty": Decimal("0.001")}]}
    f.health = {"data_fresh": True, "api_errors": 0, "paused": False, "killed": False, "flatten_phase": None,
                "time_to_deadline_s": 3600.0}
    return f


@pytest.fixture
def tools(cfg: Config, fake: Fake) -> InternalTools:
    return InternalTools(fake.deps(cfg))


def assert_json_safe(value: Any) -> None:
    """Only JSON types, no NaN/inf, and an exact JSON round trip."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert all(isinstance(k, str) for k in node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        else:
            assert node is None or isinstance(node, (str, int, float, bool)), type(node)

    walk(value)
    assert json.loads(json.dumps(value, allow_nan=False)) == value


def size(value: Any) -> int:
    return len(canonical_json(value))


# ---------------------------------------------------------------- every tool


def test_tool_args_cover_every_tool(tools: InternalTools) -> None:
    assert set(TOOL_ARGS) == tools.names


@pytest.mark.parametrize("name", sorted(TOOL_ARGS))
def test_every_tool_returns_json_safe_data(tools: InternalTools, name: str) -> None:
    out = tools.call(name, TOOL_ARGS[name])
    assert_json_safe(out)
    assert isinstance(out, dict)
    assert "error" not in out, out
    assert size(out) <= tools.max_bytes


@pytest.mark.parametrize("name", sorted(TOOL_ARGS))
def test_every_tool_degrades_to_json_safe_output_on_empty_state(cfg: Config, name: str) -> None:
    empty = Fake()
    out = InternalTools(empty.deps(cfg)).call(name, TOOL_ARGS[name])
    assert_json_safe(out)
    assert isinstance(out, dict)


def test_empty_state_errors_are_explicit(cfg: Config) -> None:
    t = InternalTools(Fake().deps(cfg))
    assert t.call("get_features") == {"error": "features not available yet"}
    assert t.call("get_candle_stats", {"symbol": SYM}) == {"symbol": SYM, "error": "no candles"}
    assert t.call("wallex_basic_get_market_depth_local", {"symbol": SYM}) == {"symbol": SYM, "error": "no book"}
    assert t.call("wallex_basic_get_latest_trades_local", {"symbol": SYM}) == {"symbol": SYM, "trades": []}


def test_decimals_and_nested_values_become_strings(tools: InternalTools) -> None:
    pnl = tools.call("get_pnl_summary")
    assert pnl["equity_tmn"] == "5012345.67"
    assert pnl["realized_by_strategy"] == {"S1": "3400", "S2": "0"}
    assert pnl["open_positions"] == [{"qty": "0.001", "symbol": SYM}]
    assert tools.call("get_health")["flatten_phase"] is None


def test_get_features_filters_and_compacts(tools: InternalTools) -> None:
    out = tools.call("get_features", {"symbols": [SYM, "NOPEUSDT"]})
    assert is_feature_view(out) and out["view"] == "features"
    assert out["ts"] == "2026-10-22T10:00:00.000Z"
    assert sorted(out["symbols"]) == [SYM]  # USDTTMN is always included, but has no features here
    # short keys, JSON numbers, None dropped (ema20_15m and bars_15m are not key fields)
    assert out["symbols"][SYM] == {"mid": 82877.1, "spread": 0.000774, "trend": 1}
    assert out["cross"] == {"BTC": {"dev": -0.00123}}  # dev_zscore None dropped
    assert out["unknown_symbols"] == ["NOPEUSDT"]
    assert "brief_rows" not in out  # asked rows carry every key
    assert sorted(tools.call("get_features")["symbols"]) == ["BTCUSDT", "ETHUSDT"]  # all symbols
    # read back under the FeatureSnapshot field names (offline provider, analyst triggers)
    assert feature_rows(out) == {SYM: {"mid": 82877.1, "spread_pct": 0.000774, "trend_flag": 1}}


def test_round_sig_gives_plain_json_numbers() -> None:
    assert round_sig(Decimal("82877.0812345")) == 82877.1
    assert round_sig(Decimal("0.000123456789")) == 0.000123457
    assert round_sig(Decimal("9234567890")) == 9234570000 and isinstance(round_sig(Decimal("9234567890")), int)
    assert round_sig(Decimal("-0.0040000")) == -0.004
    assert round_sig(1) == 1 and round_sig(0) == 0 and round_sig(Decimal("0E-10")) == 0
    for bad in (None, True, "abc", Decimal("NaN"), float("inf")):
        assert round_sig(bad) is None


def _full_snapshot(cfg: Config) -> FeatureSnapshot:
    """Every key field present for the whole default watchlist and every S1 coin, with long, hard to
    round values (Decimal divisions): the largest view the engine can produce."""
    symbols: dict[str, dict[str, Any]] = {}
    for i, sym in enumerate(cfg.markets.watchlist):
        mid = Decimal(123456789 + 7919 * i) / 7 if sym.endswith("TMN") else Decimal(98765 + 13 * i) / 7
        third = Decimal(1) / Decimal(3 + i)
        symbols[sym] = {
            "bid": mid * Decimal("0.9995"), "ask": mid * Decimal("1.0005"), "mid": mid,
            "spread_pct": third / 977, "ret_15m": -third / 71, "ret_1h": third / 53, "ret_24h": -third / 17,
            "atr_15m_pct": third / 313, "atr_15m_pct_median_24h": third / 317, "ema_gap_pct": -third / 419,
            "trend_flag": -1, "vol_ratio_15m": third * 7, "book_age_s": Decimal("12.345"),
            "realized_vol_1h_pct": third / 99, "ema20_15m": mid, "ema60_15m": mid, "bars_15m": 97,
        }
    cross = {
        coin: {"dev_mid_pct": -Decimal(1) / (701 + j), "dev_zscore": -Decimal(13) / (7 + j),
               "edge_tmn_cheap_pct": -Decimal(1) / (307 + j), "edge_usdt_cheap_pct": -Decimal(1) / (409 + j),
               "cost_lockin_tmn_cheap": Decimal("0.0043"), "cost_lockin_usdt_cheap": Decimal("0.0043"),
               "u_mid": Decimal("101250.5"), "dev_samples": 900}
        for j, coin in enumerate(cfg.strategies.s1_crossbook.coins)
    }
    return FeatureSnapshot(ts=float(T0), symbols=symbols, cross=cross, dollar=dict(symbols["USDTTMN"]))


def test_default_watchlist_features_fit_the_tool_cap(cfg: Config, fake: Fake) -> None:
    assert len(cfg.markets.watchlist) == 21 and len(cfg.strategies.s1_crossbook.coins) == 10
    fake.features = _full_snapshot(cfg)
    tools = InternalTools(fake.deps(cfg))
    out = tools.call("get_features")
    assert size(out) <= cfg.ai.tool_result_max_bytes == 4096
    assert "truncated" not in out and "omitted_symbols" not in out and "significant_digits" not in out
    assert sorted(out["symbols"]) == sorted(cfg.markets.watchlist)  # every book, 6 significant digits
    assert sorted(out["cross"]) == sorted(cfg.strategies.s1_crossbook.coins)
    full = {"mid", "spread", "ret_15m", "ret_1h", "ret_24h", "atr", "atr_med", "ema_gap", "trend", "vol_x", "age_s"}
    for sym in ("USDTTMN", "BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert set(out["symbols"][sym]) == full
    assert sorted(out["brief_rows"]) == sorted(set(cfg.markets.watchlist) - {"USDTTMN", "BTCUSDT", "ETHUSDT",
                                                                            "SOLUSDT"})
    assert set(out["symbols"]["XRPTMN"]) == {"mid", "spread", "ret_1h", "ret_24h"}
    assert set(out["cross"]["BTC"]) == {"dev", "z", "net_tmn_cheap", "net_usdt_cheap"}
    assert out["lockin_cost"] == {"tmn_cheap": 0.0043, "usdt_cheap": 0.0043}
    btc = fake.features.cross["BTC"]
    assert out["cross"]["BTC"]["net_tmn_cheap"] == round_sig(btc["edge_tmn_cheap_pct"] - btc["cost_lockin_tmn_cheap"])
    # the four regime symbols (what the offline provider asks for) are far below the cap
    regime = tools.call("get_features", {"symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "USDTTMN"]})
    assert size(regime) < 2048 and sorted(regime["cross"]) == ["BTC", "ETH", "SOL"]
    # asking for every book (every key) still fits: brief rows first, then fewer digits, never a cut
    every = tools.call("get_features", {"symbols": list(cfg.markets.watchlist)})
    assert size(every) <= 4096 and "truncated" not in every and "omitted_symbols" not in every
    # the analyst's first message (4 x the cap, with the key legend) carries every key of every book
    first = feature_view(fake.features, cfg.markets.watchlist, max_bytes=4 * 4096, legend=True)
    assert size(first) <= 4 * 4096 and "brief_rows" not in first and set(first["keys"]) >= full


def test_feature_view_degrades_by_dropping_rows_never_by_cutting(cfg: Config) -> None:
    snap = _full_snapshot(cfg)
    view = feature_view(snap, max_bytes=2600)
    assert size(view) <= 2600 and view["significant_digits"] == 4
    assert view["omitted_symbols"] and set(view["omitted_symbols"]).isdisjoint(view["symbols"])
    assert {"USDTTMN", "BTCUSDT", "ETHUSDT", "SOLUSDT"} <= set(view["symbols"])  # regime rows are kept
    assert is_feature_view(view) and not is_feature_view({"symbols": {}}) and not is_feature_view(None)


def test_features_without_values_are_listed_not_padded(cfg: Config) -> None:
    snap = FeatureSnapshot(ts=float(T0), symbols={"BTCUSDT": {"mid": None, "trend_flag": 0}, "ETHUSDT": {}})
    view = feature_view(snap)
    assert view["symbols"] == {"BTCUSDT": {"trend": 0}} and view["no_data"] == ["ETHUSDT"]
    assert "lockin_cost" not in view and view["cross"] == {}


@pytest.mark.parametrize(("n", "expected"), [(None, 20), (5, 5), (500, 50), (0, 1), (-3, 1), ("7", 7)])
def test_get_recent_decisions_clamps_n(tools: InternalTools, fake: Fake, n: Any, expected: int) -> None:
    out = tools.call("get_recent_decisions", {} if n is None else {"n": n})
    assert fake.decision_n == [expected]
    assert len(out["decisions"]) == expected
    assert_json_safe(out)


def test_market_depth_local_is_top_10_as_strings(tools: InternalTools) -> None:
    out = tools.call("wallex_basic_get_market_depth_local", {"symbol": SYM})
    assert len(out["bid"]) == len(out["ask"]) == 10
    assert out["bid"][0] == ["82845.00", "0.5"] and out["ask"][0] == ["82909.16", "0.5"]
    assert out["bid"][9] == ["82840.50", "0.5"]
    assert (out["ts"], out["source"]) == (T0 - 1, "rest")


def test_candle_history_local_returns_newest_completed_bars(tools: InternalTools) -> None:
    out = tools.call("wallex_basic_candle_history_local", {"symbol": SYM, "resolution": "15", "n": 3})
    # 80 one-minute candles from T0-4800 to T0-60 -> six completed 15m bars (the first holds only 5 candles)
    assert out["t"] == [T0 - 2700, T0 - 1800, T0 - 900]
    assert out["c"] == ["149", "164", "179"]
    assert out["h"][-1] == "180" and out["l"][-1] == "164"
    assert out["v"][-1] == str(14 * 10 + 30)
    many = tools.call("wallex_basic_candle_history_local", {"symbol": SYM, "resolution": "1", "n": 500})
    assert len(many["t"]) == 50 and many["t"][-1] == T0 - 60  # capped at 50, newest last


def test_latest_trades_local_last_30_of_15_minutes(tools: InternalTools) -> None:
    out = tools.call("wallex_basic_get_latest_trades_local", {"symbol": SYM})
    trades = out["trades"]
    # trades every 20 s from T0-1200; the last 15 minutes hold 45 of them -> the newest 30
    assert len(trades) == 30
    assert trades[-1]["price"] == "82959" and trades[-1]["ts"] == T0 - 20
    assert trades[0]["ts"] == T0 - 20 * 30
    assert all(t["ts"] >= T0 - 900 for t in trades)


# ---------------------------------------------------------------- get_candle_stats math


def ema_linear_end(last: int, period: int) -> Decimal:
    """EMA of a +1-per-bar series seeded with its SMA sits exactly (period - 1) / 2 below the last value."""
    return Decimal(last) - Decimal(period - 1) / 2


def close_to(actual: str | None, expected: Decimal, tol: Decimal = Decimal("1e-18")) -> bool:
    return actual is not None and abs(Decimal(actual) - expected) <= tol


def test_candle_stats_math_on_one_minute_fixture(tools: InternalTools) -> None:
    out = tools.call("get_candle_stats", {"symbol": SYM, "resolution": "1", "n": 80})
    assert out["bars"] == 80 and out["last_close"] == "179"
    one = Decimal(1)
    assert Decimal(out["ret_1_bar"]) == Decimal(179) / Decimal(178) - one
    assert Decimal(out["ret_4_bars"]) == Decimal(179) / Decimal(175) - one
    assert Decimal(out["ret_all"]) == Decimal("0.79")
    # every true range is 2 -> ATR14 == 2 exactly
    assert Decimal(out["atr14_pct"]) == Decimal(2) / Decimal(179)
    e20, e60 = ema_linear_end(179, 20), ema_linear_end(179, 60)
    assert (e20, e60) == (Decimal("169.5"), Decimal("149.5"))
    assert close_to(out["ema20"], e20) and close_to(out["ema60"], e60)
    assert close_to(out["ema_gap_pct"], (e20 - e60) / e60)
    assert Decimal(out["volume_ratio_last"]) == 3
    assert (out["high"], out["low"]) == ("180", "99")


def test_candle_stats_uses_only_completed_15m_bars(tools: InternalTools, fake: Fake) -> None:
    # 60 minutes = 4 completed 15m bars, plus a candle in the still-open bar starting at T0
    fake.candles[SYM] = linear_candles(SYM, 61, T0)
    out = tools.call("get_candle_stats", {"symbol": SYM, "resolution": "15", "n": 60})
    assert out["bars"] == 4
    assert out["last_close"] == "159"  # close of the 09:59 candle, not the open 10:00 one
    assert Decimal(out["ret_1_bar"]) == Decimal(159) / Decimal(144) - 1
    assert Decimal(out["ret_all"]) == Decimal(159) / Decimal(114) - 1
    assert out["ret_4_bars"] is None and out["atr14_pct"] is None
    assert out["ema20"] is None and out["ema_gap_pct"] is None and out["volume_ratio_last"] is None
    assert out["high"] == "160" and out["low"] == "99"
    assert fake.candle_queries[-1] == (SYM, T0 - 26 * 3600, T0)


def test_candle_stats_n_is_clamped(tools: InternalTools) -> None:
    assert tools.call("get_candle_stats", {"symbol": SYM, "resolution": "1", "n": 1})["bars"] == 5
    assert tools.call("get_candle_stats", {"symbol": SYM, "resolution": "1", "n": 30})["bars"] == 30
    out = tools.call("get_candle_stats", {"symbol": SYM, "resolution": "1", "n": 30})
    assert Decimal(out["ret_all"]) == Decimal(179) / Decimal(150) - 1


# ---------------------------------------------------------------- dispatcher errors


@pytest.mark.parametrize(
    "name", ["nope", "wallex_spot_post_account_orders", "get_features_local", "call", "", "schemas", "t_get_health"]
)
def test_unknown_tool_is_an_error_dict(tools: InternalTools, name: str) -> None:
    assert tools.call(name, {}) == {"error": f"unknown tool {name}"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_health", {"verbose": True}),
        ("get_candle_stats", {}),  # missing required symbol
        ("get_candle_stats", {"symbol": SYM, "window": 5}),
        ("wallex_basic_get_market_depth_local", {"sym": SYM}),
        ("get_pnl_summary", ["positional"]),
        ("get_recent_decisions", {"_raw": "{not json"}),  # what the client yields for unparseable arguments
    ],
)
def test_bad_arguments_are_an_error_dict(tools: InternalTools, name: str, args: Any) -> None:
    out = tools.call(name, args)
    assert set(out) == {"error"}
    assert out["error"].startswith(f"bad arguments for {name}")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_candle_stats", {"symbol": SYM, "resolution": "abc"}),
        ("get_candle_stats", {"symbol": SYM, "resolution": "0"}),
        ("get_candle_stats", {"symbol": SYM, "n": "many"}),
        ("get_recent_decisions", {"n": None}),
        ("wallex_basic_candle_history_local", {"symbol": SYM, "resolution": "x"}),
    ],
)
def test_bad_argument_values_are_an_error_dict(tools: InternalTools, name: str, args: dict[str, Any]) -> None:
    out = tools.call(name, args)
    assert set(out) == {"error"}
    assert name in out["error"]


def test_a_failing_dependency_is_an_error_dict(cfg: Config, fake: Fake) -> None:
    deps = fake.deps(cfg)

    def broken() -> dict[str, Any]:
        raise RuntimeError("store locked")

    deps.health = broken
    assert InternalTools(deps).call("get_health") == {"error": "get_health failed: RuntimeError: store locked"}


def test_tool_output_is_truncated_to_the_configured_budget(cfg: Config, fake: Fake) -> None:
    fake.pnl = {f"position_{i:04d}": {"note": 'say "hi" \\ ' * 5, "qty": Decimal(i)} for i in range(800)}
    tools = InternalTools(fake.deps(cfg))
    out = tools.call("get_pnl_summary")
    assert size(out) <= cfg.ai.tool_result_max_bytes == tools.max_bytes
    assert_json_safe(out)


# ---------------------------------------------------------------- truncate


def test_truncate_passes_small_results_and_makes_them_json_safe() -> None:
    src = {"b": Decimal("1.50"), "a": (1, 2), "s": {3, 1}, "f": 0.25}
    assert truncate(src, 4096) == {"a": [1, 2], "b": "1.50", "f": 0.25, "s": [1, 3]}
    assert to_jsonable(src) == truncate(src, 4096)


def test_truncate_trims_lists_keeping_the_newest_items() -> None:
    src = {"symbol": SYM, "candles": [{"t": i, "c": "100.25"} for i in range(500)]}
    out = truncate(src, 4096)
    assert size(out) <= 4096
    assert out["symbol"] == SYM
    assert 0 < len(out["candles"]) <= 50
    assert out["candles"][-1] == {"c": "100.25", "t": 499}
    assert [c["t"] for c in out["candles"]] == list(range(500 - len(out["candles"]), 500))


BIG_INPUTS: dict[str, Any] = {
    "many_keys": {f"k{i:05d}": "v" * 10 for i in range(2000)},
    "quotes_and_backslashes": {"text": '"\\' * 5000},
    "non_ascii": {"notes": "بازار " * 3000},
    "emoji": {"notes": "\U0001f4c8" * 4000},
    "control_chars": {"log": "\x01\n\t" * 4000},
    "long_list": list(range(100_000)),
    "nested_lists": [[str(i) * 20 for i in range(200)] for _ in range(200)],
    "deep": {"a": {"b": {"c": [{"d": "x" * 9000}]}}},
}


@pytest.mark.parametrize("max_bytes", [256, 1024, 4096])
@pytest.mark.parametrize("kind", sorted(BIG_INPUTS))
def test_truncate_never_exceeds_max_bytes(kind: str, max_bytes: int) -> None:
    out = truncate(BIG_INPUTS[kind], max_bytes)
    assert size(out) <= max_bytes
    assert_json_safe(out)


def test_truncate_prefix_fallback_is_a_marked_valid_prefix() -> None:
    src = BIG_INPUTS["quotes_and_backslashes"]
    out = truncate(src, 1024)
    assert out["truncated"] is True
    full = canonical_json(src).decode()
    assert out["json_prefix"] and full.startswith(out["json_prefix"])
    assert size(out) <= 1024


# ---------------------------------------------------------------- schemas


JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
SCHEMA_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "minimum", "maximum",
                   "description"}
PY_FOR_TYPE: dict[str, tuple[type, ...]] = {"string": (str,), "integer": (int,), "number": (int, float),
                                            "boolean": (bool,), "array": (list,), "object": (dict,)}


def check_schema(node: dict[str, Any], where: str) -> None:
    """A small JSON-schema (draft 2020-12 subset) structural validator for the keywords we use."""
    assert isinstance(node, dict), where
    assert set(node) <= SCHEMA_KEYWORDS, f"{where}: unknown keywords {set(node) - SCHEMA_KEYWORDS}"
    assert node.get("type") in JSON_TYPES, where
    t = node["type"]
    if t == "object":
        props = node.get("properties", {})
        assert isinstance(props, dict), where
        for k, sub in props.items():
            check_schema(sub, f"{where}.{k}")
        req = node.get("required", [])
        assert isinstance(req, list) and len(set(req)) == len(req) and set(req) <= set(props), where
        assert isinstance(node.get("additionalProperties", True), bool), where
    if t == "array":
        check_schema(node["items"], f"{where}[]")
    if "enum" in node:
        assert node["enum"] and all(isinstance(v, PY_FOR_TYPE[t]) for v in node["enum"]), where
    for bound in ("minimum", "maximum"):
        if bound in node:
            assert t in ("integer", "number") and isinstance(node[bound], (int, float)), where
    if "minimum" in node and "maximum" in node:
        assert node["minimum"] <= node["maximum"], where


@pytest.mark.parametrize("schema", INTERNAL_TOOL_SCHEMAS + LOCAL_TOOL_SCHEMAS, ids=lambda s: s["name"])
def test_schemas_are_valid_function_definitions(schema: dict[str, Any]) -> None:
    assert set(schema) == {"name", "description", "parameters"}
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", schema["name"])  # OpenAI / Gemini function-name rule
    assert isinstance(schema["description"], str) and schema["description"].strip()
    params = schema["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False  # the model cannot invent arguments
    check_schema(params, schema["name"])
    json.dumps(schema, allow_nan=False)


def test_schema_names_match_the_dispatcher(tools: InternalTools) -> None:
    names = [s["name"] for s in tools.schemas]
    assert len(names) == len(set(names))
    methods = {m[2:] for m, _ in inspect.getmembers(InternalTools, inspect.isfunction) if m.startswith("t_")}
    assert set(names) == methods == tools.names


@pytest.mark.parametrize("schema", INTERNAL_TOOL_SCHEMAS + LOCAL_TOOL_SCHEMAS, ids=lambda s: s["name"])
def test_schema_parameters_match_the_method_signature(tools: InternalTools, schema: dict[str, Any]) -> None:
    sig = inspect.signature(getattr(tools, f"t_{schema['name']}"))
    params = schema["parameters"]
    assert set(sig.parameters) == set(params.get("properties", {}))
    required = {n for n, p in sig.parameters.items() if p.default is inspect.Parameter.empty}
    assert required == set(params.get("required", []))


def test_local_tools_mirror_read_only_wallex_tools() -> None:
    for s in LOCAL_TOOL_SCHEMAS:
        assert s["name"].endswith("_local")
        assert s["name"].removesuffix("_local") in READ_ONLY_ALLOWLIST
    assert not any(s["name"].endswith("_local") for s in INTERNAL_TOOL_SCHEMAS)
