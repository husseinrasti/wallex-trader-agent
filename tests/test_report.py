"""Report generation (PRD-05 §7): per-run report folder, reports index, assembled final report.

Inputs come from the offline SyntheticFeed written into a temporary Store (there is no recorded
Wallex data on the development machine): candles -> an equity curve stored in the ``equity`` table
-> ``read_equity_table`` -> ``compute_metrics`` -> ``write_report``, plus real ``MarketPlan`` /
``RiskDecision`` dumps for the AI and veto sections. Nothing here touches the network or the clock.
"""

from __future__ import annotations

import csv
import json
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backtest.metrics import (
    Metrics,
    ablation_delta,
    compute_metrics,
    read_csv_rows,
    read_equity_table,
)
from backtest.report import (
    DRAWDOWN_PNG,
    EQUITY_CSV,
    EQUITY_CSV_COLUMNS,
    EQUITY_PNG,
    INDEX_MD,
    NOT_AVAILABLE,
    REGIMES_PNG,
    REPORT_FILES,
    REPORT_MD,
    SUMMARY_JSON,
    SYNTHETIC_BANNER,
    TRADES_CSV,
    VETOES_PNG,
    FinalSection,
    assemble_final_report,
    update_index,
    write_report,
)
from wta.data.store import Store
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot, snapshot_reference
from wta.models import MarketPlan, RegimeCall, RiskCheck, RiskDecision
from wta.utils.timeutil import SimClock

from .conftest import T0

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNGS = (EQUITY_PNG, DRAWDOWN_PNG, VETOES_PNG, REGIMES_PNG)
HOURS = 3
STEP_S = 30
SYMBOLS = ["BTCTMN", "BTCUSDT", "USDTTMN"]
TMN_CASH = Decimal("2500000")
USDT_HELD = Decimal("9.37")


# ---------------------------------------------------------------- synthetic inputs


@pytest.fixture(scope="module")
def synth_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    """3 h of synthetic 1-min candles (seeded) plus an equity curve (TMN cash + USDT valued at the
    USDTTMN close, never flattened) in a temporary SQLite store."""
    store = Store(tmp_path_factory.mktemp("store") / "wta.sqlite")
    markets = {k: v for k, v in load_markets_snapshot().items() if k in SYMBOLS}
    ref = snapshot_reference()
    feed = SyntheticFeed(
        markets=markets,
        symbols=SYMBOLS,
        bus=MarketDataBus(),
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=SimClock(T0),
        params=SyntheticParams(seed=5),
        on_candle=store.upsert_candles,
    )
    feed.publish(T0)
    for i in range(1, HOURS * 3600 // STEP_S):
        feed.step(STEP_S)
        feed.publish(T0 + i * STEP_S)
    store.flush()
    for c in store.load_candles("USDTTMN", "1"):
        usdt = USDT_HELD * c.c
        store.add_equity(float(c.ts), TMN_CASH + usdt, TMN_CASH, usdt, Decimal("0"))
    store.flush()
    yield store
    store.close()


def _plans(start: float, end: float, model: str = "test/model") -> list[dict[str, Any]]:
    cycle = ["range", "trend_up", "trend_up", "chaos", "trend_down", "range"]
    out = []
    for k, ts in enumerate(range(int(start), int(end), 300)):
        plan = MarketPlan(
            ts=float(ts),
            regimes={
                "USDTTMN": RegimeCall(regime=cycle[(k // 6) % 6], confidence=0.7),  # type: ignore[arg-type]
                "BTCUSDT": RegimeCall(regime=cycle[(k // 4) % 6], confidence=0.6),  # type: ignore[arg-type]
            },
            enabled_strategies=["S1", "S2"] if k % 5 else ["S2"],
            size_multiplier=Decimal("0.8"),
            cached=k % 2 == 0,
            model=model,
            prompt_hash=f"h{k:04d}",
        )
        out.append(plan.model_dump(mode="json"))
    return out


def _vetoes() -> list[dict[str, Any]]:
    codes = ["FEE_BUDGET", "MAX_ORDER_PCT", "FEE_BUDGET", "STALE_DATA", "FEE_BUDGET"]
    out = []
    for k, code in enumerate(codes):
        risk = RiskDecision(
            approved=False,
            checks=[
                RiskCheck(code="OPEN_ORDERS", passed=True, detail="ok"),
                RiskCheck(code=code, passed=False, detail="x"),
            ],
            veto_reason=code,
        )
        out.append(
            {"kind": "veto", "decision_id": f"v{k}", "strategy": "S1", "risk": risk.model_dump(mode="json")}
        )
    out.append(
        {"kind": "intent", "decision_id": "i0", "strategy": "S2", "risk": {"approved": True, "checks": []}}
    )
    return out


@pytest.fixture(scope="module")
def inputs(synth_store: Store) -> dict[str, Any]:
    """Equity from the store, trades on the BTCUSDT candles, metrics from ``compute_metrics``."""
    dollar = synth_store.load_candles("USDTTMN", "1")
    btc = synth_store.load_candles("BTCUSDT", "1")
    lo, hi = synth_store.time_range("candles", "USDTTMN")
    assert dollar and btc and lo is not None and hi is not None
    equity = read_equity_table(synth_store)
    assert len(equity) == len(dollar)
    usdt_tmn = {c.ts: c.c for c in dollar}
    trades = []
    for k in range(0, len(btc) - 6, 3):  # 2-min holds every 3 min -> > 50 trades in 3 h
        entry, exit_ = btc[k], btc[k + 2]
        qty = Decimal("0.00011")
        rate = usdt_tmn.get(exit_.ts, dollar[-1].c)
        fee = (entry.c + exit_.c) * qty * Decimal("0.001") * rate
        trades.append(
            {
                "entry_ts": float(entry.ts),
                "closed_ts": float(exit_.ts),
                "strategy": "S3" if k % 8 else "S2",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": qty,
                "entry_price": entry.c,
                "exit_price": exit_.c,
                "pnl_tmn": (exit_.c - entry.c) * qty * rate - fee,
                "pnl_pct": float((exit_.c / entry.c) - 1),
                "fees_tmn": fee,
                "hold_s": exit_.ts - entry.ts,
                "exit_reason": "time_stop" if k % 4 else "stop",
            }
        )
    decisions = _vetoes()
    plans = _plans(lo, hi)
    metrics = compute_metrics(equity, trades, decisions=decisions, plans=plans, label="books").to_dict()
    return {
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
        "decisions": decisions,
        "plans": plans,
        "data_from": lo,
        "data_to": hi,
    }


def _meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "config_hash": "5751839f7e2d",
        "git_commit": "0123456789abcdef0123456789abcdef01234567",
        "mode": "backtest",
        "driver": "books",
        "window_from": "2026-10-22T10:00:00Z",
        "window_to": "2026-10-22T13:00:00Z",
        "data_source": "sqlite:data/wta.sqlite (SyntheticFeed seed 5)",
        "synthetic": True,
        "ai": "on",
        "strategies": ["S1", "S2", "S3"],
    }
    meta.update(overrides)
    return meta


def _real_meta(**overrides: Any) -> dict[str, Any]:
    return _meta(synthetic=False, data_source="sqlite:data/wta.sqlite (recorded)", **overrides)


def _write(out: Path, inputs: dict[str, Any], **kw: Any) -> Path:
    args: dict[str, Any] = {
        "title": "Backtest — books window",
        "meta": _meta(),
        "metrics": inputs["metrics"],
        "equity": inputs["equity"],
        "trades": inputs["trades"],
        "decisions": inputs["decisions"],
        "plans": inputs["plans"],
        "ablation": None,
        "limitations": [],
    }
    args.update(kw)
    return write_report(out, **args)


def _section(text: str, heading: str) -> str:
    """The body of ``heading`` up to the next ``## `` heading."""
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == PNG_MAGIC
    return struct.unpack(">II", data[16:24])


# ---------------------------------------------------------------- write_report


def test_report_creates_all_files(tmp_path: Path, inputs: dict[str, Any]) -> None:
    md = _write(tmp_path / "bt_books_3h", inputs)
    assert md == tmp_path / "bt_books_3h" / REPORT_MD
    for name in REPORT_FILES:
        assert (md.parent / name).is_file(), name
    text = md.read_text()
    for png in PNGS:
        width, height = _png_size(md.parent / png)
        assert width == 1440 and 300 <= height <= 1080, png  # readable on a 1080p screen
        assert f"]({png})" in text, png  # every chart is embedded with a relative link

    assert text.startswith("# Backtest — books window\n")
    assert "`5751839f7e2d`" in text and "`0123456789abcdef0123456789abcdef01234567`" in text
    assert "| Requested window (UTC) | 2026-10-22T10:00:00Z → 2026-10-22T13:00:00Z (3.0 h) |" in text
    assert "| Data present (UTC) | 2026-10-22T10:00:00Z → 2026-10-22T12:59:00Z (3.0 h) |" in text
    assert "| Data source | sqlite:data/wta.sqlite (SyntheticFeed seed 5) (SYNTHETIC) |" in text
    for heading in (
        "## Metrics",
        "### End state: realized vs unrealized",
        "## Equity curve (TMN)",
        "## Drawdown",
        "## Trades",
        "## Per strategy",
        "## Per day",
        "## Risk vetoes",
        "## AI (Regime Analyst)",
        "### Ablation",
        "## Fees vs budget",
        "## Limitations",
    ):
        assert heading in text, heading

    summary = json.loads((md.parent / SUMMARY_JSON).read_text())
    assert summary["title"] == "Backtest — books window"
    assert summary["report_date"] == "2026-10-22"
    assert summary["synthetic"] is True and summary["insufficient_data"] is False
    assert summary["counts"]["equity_points"] == len(inputs["equity"])
    assert summary["counts"]["plans"] == len(inputs["plans"])
    assert summary["counts"]["decisions_by_kind"] == {"intent": 1, "veto": 5}
    assert summary["vetoes_by_code"] == {"FEE_BUDGET": 3, "MAX_ORDER_PCT": 1, "STALE_DATA": 1}
    assert summary["meta"]["config_hash"] == "5751839f7e2d"
    assert summary["metrics"] == json.loads(json.dumps(inputs["metrics"]))  # metrics kept as fractions
    assert summary["files"] == list(REPORT_FILES)


def test_metrics_table_shows_fractions_as_percent(tmp_path: Path, inputs: dict[str, Any]) -> None:
    m = inputs["metrics"]
    assert m["max_drawdown_pct"] > 0 and m["n_trades"] > 50
    text = _write(tmp_path / "r", inputs).read_text()
    metrics = _section(text, "## Metrics")
    assert f"| Return | {m['return_pct'] * 100:.2f}% |" in metrics
    assert f"| Max drawdown (below running peak) | {m['max_drawdown_pct'] * 100:.2f}% |" in metrics
    assert f"| Win rate | {m['win_rate'] * 100:.2f}% |" in metrics
    assert f"| Closed trades / cycles | {m['n_trades']} |" in metrics
    assert f"| Fees paid (TMN) | {m['fees_paid_tmn']:,.0f} |" in metrics
    assert "| Longest drawdown |" in metrics and "| Sharpe (annualized, from 1-h returns) |" in metrics
    assert "start_ts" not in metrics and "| label |" not in metrics  # header data is not repeated
    assert m["sharpe_note"] in metrics  # the metrics' own caveats are shown
    assert "Sources: fees from trades." in metrics
    end_state = metrics.split("### End state: realized vs unrealized", 1)[1]
    assert "| TMN cash | 2,500,000 |" in end_state
    assert "| Flat (all TMN) | no |" in end_state

    strategies = _section(text, "## Per strategy")
    assert "| strategy | trades | wins | losses | win rate | PnL (TMN) |" in strategies
    assert re.search(r"^\| S1 \| 0 \| 0 \| 0 \| n/a \| 0 \|.* \| 5 \|$", strategies, re.M)  # vetoes only
    s3 = m["per_strategy"]["S3"]
    assert (
        f"| S3 | {s3['n_trades']} | {s3['wins']} | {s3['losses']} | {s3['win_rate'] * 100:.2f}% |"
        in strategies
    )
    assert "| 2026-10-22 |" in _section(text, "## Per day")


def test_equity_csv_matches_metrics_and_round_trips(tmp_path: Path, inputs: dict[str, Any]) -> None:
    out = _write(tmp_path / "r", inputs).parent
    with (out / EQUITY_CSV).open() as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == EQUITY_CSV_COLUMNS
        rows = list(reader)
    assert len(rows) == len(inputs["equity"])
    assert rows[0]["ts"] == "2026-10-22T10:00:00Z"
    assert rows[0]["equity_tmn"] == inputs["equity"][0]["equity_tmn"]  # stored text kept verbatim
    assert rows[0]["tmn"] == "2500000"
    drawdowns = [float(r["drawdown_pct"]) for r in rows]
    assert min(drawdowns) == 0.0 and all(d >= 0 for d in drawdowns)  # positive, like the metrics
    assert max(drawdowns) == pytest.approx(inputs["metrics"]["max_drawdown_pct"], abs=1e-8)

    # equity.csv and trades.csv read back through backtest.metrics give the same numbers
    again = compute_metrics(read_csv_rows(out / EQUITY_CSV), read_csv_rows(out / TRADES_CSV)).to_dict()
    m = inputs["metrics"]
    for key in (
        "return_pct",
        "max_drawdown_pct",
        "n_trades",
        "win_rate",
        "closed_trades_pnl_tmn",
        "fees_paid_tmn",
    ):
        assert again[key] == pytest.approx(m[key], rel=1e-9), key
    assert again["realized_vs_unrealized"]["unrealized_tmn"] == pytest.approx(
        m["realized_vs_unrealized"]["unrealized_tmn"]
    )


def test_trades_table_shows_first_50_and_links_csv(tmp_path: Path, inputs: dict[str, Any]) -> None:
    n = len(inputs["trades"])
    assert n > 50
    out = _write(tmp_path / "r", inputs).parent
    text = (out / REPORT_MD).read_text()
    assert f"first 50 of {n}" in text
    assert f"[{TRADES_CSV}]({TRADES_CSV})" in text
    section = _section(text, "## Trades")
    table_rows = [ln for ln in section.splitlines() if ln.startswith("| ") and not ln.startswith("| ---")]
    assert len(table_rows) == 1 + 50  # header + 50 trades
    assert table_rows[0].startswith("| entry_ts | closed_ts | strategy | symbol | side | qty |")
    assert "| 2026-10-22T10:00:00Z | 2026-10-22T10:02:00Z |" in table_rows[1]
    assert "0.00011" in section  # quantities shown at exchange precision, never rounded
    assert "Exit reasons: time_stop " in section and ", stop " in section

    with (out / TRADES_CSV).open() as fh:
        trades = list(csv.DictReader(fh))
    assert len(trades) == n  # the CSV has all of them
    assert Decimal(trades[0]["qty"]) == Decimal("0.00011")
    assert Decimal(trades[0]["pnl_tmn"]) == inputs["trades"][0]["pnl_tmn"]  # Decimal precision preserved


def test_synthetic_banner_present_only_when_synthetic(tmp_path: Path, inputs: dict[str, Any]) -> None:
    synth = _write(tmp_path / "synth", inputs).read_text()
    assert SYNTHETIC_BANNER in synth
    assert synth.index(SYNTHETIC_BANNER) < synth.index("| Config hash |")  # loud: before any number

    real = _write(tmp_path / "real", inputs, meta=_real_meta()).read_text()
    assert SYNTHETIC_BANNER not in real and "SYNTHETIC" not in real
    assert "synthetic" not in _section(real, "## Limitations").lower()
    string_false = _write(tmp_path / "str", inputs, meta=_real_meta() | {"synthetic": "false"}).read_text()
    assert SYNTHETIC_BANNER not in string_false

    # the flag is missing but the data source says synthetic: err on the honest side
    by_source = _write(tmp_path / "src", inputs, meta=_meta(synthetic=None)).read_text()
    assert SYNTHETIC_BANNER in by_source

    # every chart of a synthetic run carries the watermark (so the PNG differs from the real one)
    for png in PNGS:
        assert (tmp_path / "synth" / png).read_bytes() != (tmp_path / "real" / png).read_bytes(), png


def test_limitations_auto_filled_and_custom_first(tmp_path: Path, inputs: dict[str, Any]) -> None:
    books = _write(tmp_path / "books", inputs, limitations=["Recorder was down 10:40-10:55."]).read_text()
    lim = _section(books, "## Limitations")
    # the inputs are synthetic: the S1 window is "generated", never called "recorded"
    assert "S1 backtest window limited to generated (synthetic) 3.0 h of order books." in lim
    assert "synthetic" in lim.lower() and "not evidence" in lim
    assert "pessimistic book fill model" in lim and "at most 80% of each level's quantity per snapshot" in lim
    assert "taker fee is paid on every fill of an order that was crossing at placement" in lim
    assert "best-level" not in lim and "unlimited depth" not in lim  # the candle model's words, not this one's
    assert "Sharpe and Sortino are computed from" in lim and "over 3.0 h of data" in lim
    assert "was still held outside TMN (unrealized)" in lim and "not flattened" in lim
    assert lim.index("Recorder was down") < lim.index("S1 backtest window")
    assert "offline rules provider" not in lim

    candles = _write(
        tmp_path / "candles",
        inputs,
        meta=_real_meta(
            driver="candles",
            strategies="S2,S3",
            ai="off",
            window_from="2026-09-22T00:00:00Z",
            window_to="2026-10-22T00:00:00Z",
        ),
        plans=None,
        limitations=["S1 backtest window limited to recorded 3.0 h of order books."],  # deduplicated
    ).read_text()
    lim = _section(candles, "## Limitations")
    assert "S1 not backtested on candles" in lim
    assert "synthetic book: mid = close" in lim and "unlimited depth at the best level" in lim
    assert "pessimistic candle fill model" in lim and "80%" not in lim  # no level cap in the candle driver
    assert "median observed" not in lim  # the spread's source is stated from meta.spreads, never assumed
    assert "AI disabled (ai=off)" in lim
    assert "requested window is 720.0 h but the data present covers only 3.0 h" in lim
    assert lim.count("S1 backtest window limited") == 1

    spreads = {"BTCUSDT": {"spread_pct": "0.0018", "source": "observed", "books": 290,
                           "from": "2026-10-22T14:20:05.929Z", "to": "2026-10-22T14:40:15.724Z"},
               "SOLUSDT": {"spread_pct": "0.0008", "source": "reference"}}
    mixed = _write(tmp_path / "candles_spreads", inputs, plans=None, meta=_real_meta(
        driver="candles", strategies="S2,S3", ai="off", spreads=spreads)).read_text()
    lim = _section(mixed, "## Limitations")
    assert ("spread = the median spread of recorded books (290 books, 2026-10-22T14:20:05.929Z → "
            "2026-10-22T14:40:15.724Z, none after the window end) for BTCUSDT; the reference spread of "
            "configs/markets_snapshot.yaml for SOLUSDT") in lim
    only_ref = _write(tmp_path / "candles_ref", inputs, plans=None, meta=_real_meta(
        driver="candles", strategies="S2,S3", ai="off", spreads={"SOLUSDT": spreads["SOLUSDT"]})).read_text()
    assert "the reference spread of configs/markets_snapshot.yaml for every symbol" in only_ref

    offline = _write(tmp_path / "offline", inputs, plans=_plans(T0, T0 + 3600, model="offline-rules-v1"))
    lim = _section(offline.read_text(), "## Limitations")
    assert "offline rules provider (offline-rules-v1), not an LLM" in lim


def test_vetoes_ai_and_fees_sections(tmp_path: Path, inputs: dict[str, Any]) -> None:
    text = _write(tmp_path / "r", inputs).read_text()
    vetoes = _section(text, "## Risk vetoes")
    assert f"]({VETOES_PNG})" in vetoes
    assert "| FEE_BUDGET | 3 | 60.00% |" in vetoes
    assert vetoes.index("FEE_BUDGET") < vetoes.index("MAX_ORDER_PCT") < vetoes.index("STALE_DATA")
    assert "OPEN_ORDERS" not in vetoes  # passed checks are not vetoes
    assert "Decision records: intent 1, veto 5." in vetoes

    ai = _section(text, "## AI (Regime Analyst)")
    n = len(inputs["plans"])
    assert f"| Plans produced | {n} |" in ai
    assert "| Average size multiplier | 0.80 |" in ai
    assert f"| Plans served from the LLM cache | {(n + 1) // 2} |" in ai
    assert "| Models | test/model |" in ai
    assert "| symbol | trend_up | range | trend_down | chaos | calls |" in ai  # canonical regime order
    btc = inputs["metrics"]["regime_distribution"]["BTCUSDT"]  # same numbers as the metrics
    share = btc["trend_up"] / n * 100
    assert re.search(rf"^\| BTCUSDT \| {btc['trend_up']} \({share:.2f}%\) \|.* \| {n} \|$", ai, re.M)
    assert re.search(rf"^\| USDTTMN \|.* \| {n} \|$", ai, re.M)
    assert f"]({REGIMES_PNG})" in ai
    assert f"_Ablation {NOT_AVAILABLE} for this run._" in ai

    fees = _section(text, "## Fees vs budget")
    m = inputs["metrics"]
    assert f"| Fees paid (TMN) | {m['fees_paid_tmn']:,.0f} (from trades) |" in fees
    budget = m["initial_equity_tmn"] * 0.02
    assert f"| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | {budget:,.0f} |" in fees
    assert m["fees_paid_tmn"] > budget  # these toy trades overspend: flagged, not hidden
    assert f"| Budget used | **{m['fees_paid_tmn'] / budget * 100:.2f}% (over budget)** |" in fees
    assert "| Fees per 72 h at this run's pace (2.98 h of data, TMN) |" in fees

    small = _write(tmp_path / "small", inputs, metrics=inputs["metrics"] | {"fees_paid_tmn": 1000.0})
    fees = _section(small.read_text(), "## Fees vs budget")
    assert f"| Budget used | {1000 / budget * 100:.2f}% |" in fees  # within budget: no marker
    empty = write_report(tmp_path / "empty", "E", {}, compute_metrics([], []), [], [])
    assert "| Fees paid (TMN) | 0 (no fee data recorded) |" in empty.read_text()


def test_ai_section_from_metrics_when_plans_are_not_given(tmp_path: Path, inputs: dict[str, Any]) -> None:
    text = _write(tmp_path / "r", inputs, plans=None).read_text()
    ai = _section(text, "## AI (Regime Analyst)")
    m = inputs["metrics"]
    assert m["n_plans"] == len(inputs["plans"])
    assert f"| Plans produced | {m['n_plans']} |" in ai  # from Metrics.n_plans
    assert "| S3 entries gated by regime | 0 |" in ai  # from Metrics.regime_gated
    btc = m["regime_distribution"]["BTCUSDT"]
    assert f"| BTCUSDT | {btc['trend_up']} (" in ai  # Metrics.regime_distribution is {symbol: {regime: n}}
    assert f"]({REGIMES_PNG})" not in ai  # no plans, no timeline


def test_ablation_from_metrics_ablation_delta(tmp_path: Path, inputs: dict[str, Any]) -> None:
    m_on = Metrics(return_pct=0.012, max_drawdown_pct=0.004, n_trades=60, vetoes_by_code={"FEE_BUDGET": 2})
    m_off = Metrics(return_pct=0.009, max_drawdown_pct=0.006, n_trades=44, vetoes_by_code={"STALE_DATA": 1})
    text = _write(tmp_path / "r", inputs, ablation=ablation_delta(m_on, m_off)).read_text()
    ai = _section(text, "## AI (Regime Analyst)")
    assert "| metric | ai=off | ai=on | delta (on − off) |" in ai
    assert "| return_pct | 0.90% | 1.20% | +0.30 pp |" in ai
    assert "| max_drawdown_pct | 0.60% | 0.40% | -0.20 pp |" in ai
    assert "| n_trades | 44 | 60 | +16 |" in ai
    assert "| vetoes_by_code.FEE_BUDGET | 0 | 2 | +2 |" in ai
    assert "| vetoes_by_code.STALE_DATA | 1 | 0 | -1 |" in ai
    assert ai.index("| return_pct") < ai.index("| max_drawdown_pct") < ai.index("| n_trades")
    assert "drew down more" in ai

    # {"ai_on": metrics, "ai_off": metrics}: only the comparable metrics, never timestamps or windows
    full = {"ai_on": m_on.to_dict(), "ai_off": m_off.to_dict(), "note": "3-day window (PRD-05 §3.1)"}
    ai = _section(_write(tmp_path / "full", inputs, ablation=full).read_text(), "## AI (Regime Analyst)")
    assert "| return_pct | 0.90% | 1.20% | +0.30 pp |" in ai
    assert "_3-day window (PRD-05 §3.1)_" in ai
    for key in ("start_ts", "| days |", "n_equity_points", "initial_equity_tmn"):
        assert key not in ai, key


def test_metrics_object_and_other_shapes_accepted(tmp_path: Path, inputs: dict[str, Any]) -> None:
    @dataclass
    class Fill:  # a dataclass trade row
        strategy: str
        pnl_tmn: Decimal
        closed_ts: float

    metrics = compute_metrics(inputs["equity"], inputs["trades"])
    md = _write(
        tmp_path / "r",
        inputs,
        metrics=metrics,  # the Metrics object itself, not its dict
        trades=[Fill("S2", Decimal("12.5"), T0 + 60)],
        plans=[MarketPlan(ts=T0, regimes={"BTCUSDT": RegimeCall(regime="chaos")})],  # models, not dumps
    )
    text = md.read_text()
    assert f"| Return | {metrics.return_pct * 100:.2f}% |" in text  # type: ignore[operator]
    trades = _section(text, "## Trades")
    assert "| closed_ts | strategy | pnl_tmn |" in trades and "| 2026-10-22T10:01:00Z | S2 | 12 |" in trades
    assert (md.parent / TRADES_CSV).read_text() == f"closed_ts,strategy,pnl_tmn\n{T0 + 60!r},S2,12.5\n"
    assert "| Plans produced | 1 |" in text


def test_equity_input_shapes_are_equivalent(tmp_path: Path) -> None:
    pairs = [(T0, Decimal("5000000")), (T0 + 60, Decimal("5001000")), (T0 + 120, Decimal("4990000"))]
    shapes: dict[str, Any] = {
        "pairs": pairs,
        "iso": [
            ("2026-10-22T10:00:00Z", 5000000),
            ("2026-10-22T10:01:00+00:00", 5001000),
            ("2026-10-22T10:02:00Z", 4990000),
        ],
        "dicts_ms": [{"ts": int(t * 1000), "equity_tmn": str(v)} for t, v in pairs],
        "store_rows": [(t, str(v), str(v), "0", "0", None, None) for t, v in reversed(pairs)],  # unsorted
        "frame": pd.DataFrame({"ts": [t for t, _ in pairs], "equity_tmn": [float(v) for _, v in pairs]}),
        "series": pd.Series(
            [float(v) for _, v in pairs], index=pd.to_datetime([t for t, _ in pairs], unit="s", utc=True)
        ),
    }
    outs = {}
    for name, eq in shapes.items():
        write_report(tmp_path / name, "Eq", {}, {}, eq, [])
        with (tmp_path / name / EQUITY_CSV).open() as fh:
            outs[name] = [(r["ts"], float(r["equity_tmn"]), r["drawdown_pct"]) for r in csv.DictReader(fh)]
    first = outs["pairs"]
    assert all(rows == first for rows in outs.values()), outs
    assert first[-1][2] == f"{1 - 4990000 / 5001000:.8f}"  # positive drawdown below the 5,001,000 peak
    with (tmp_path / "store_rows" / EQUITY_CSV).open() as fh:
        assert next(csv.DictReader(fh))["tmn"] == "5000000"  # valuation columns carried through


def test_report_is_deterministic(tmp_path: Path, inputs: dict[str, Any]) -> None:
    a = _write(tmp_path / "a", inputs).parent
    b = _write(tmp_path / "b", inputs).parent
    for name in REPORT_FILES:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert b"matplotlib" not in (a / EQUITY_PNG).read_bytes()  # no version metadata in the PNGs


def test_empty_inputs_do_not_crash(tmp_path: Path) -> None:
    md = write_report(tmp_path / "empty", "Empty store", {}, {}, [], [], None, None, None, None)
    for name in REPORT_FILES:
        assert (md.parent / name).is_file(), name
    text = md.read_text()
    assert "INSUFFICIENT DATA" in text and "Insufficient data" in text
    assert SYNTHETIC_BANNER not in text
    assert "_No trades in the window._" in text and "_No vetoes recorded._" in text
    summary = json.loads((md.parent / SUMMARY_JSON).read_text())
    assert summary["insufficient_data"] is True and summary["report_date"] is None
    assert (
        md.parent / TRADES_CSV
    ).read_text() == "entry_ts,closed_ts,strategy,symbol,pnl_tmn,pnl_pct,hold_s,fees_tmn,exit_reason\n"
    assert (md.parent / EQUITY_CSV).read_text() == ",".join(EQUITY_CSV_COLUMNS) + "\n"
    for png in PNGS:
        assert _png_size(md.parent / png)[0] == 1440  # placeholders, still valid PNGs

    # None everywhere, an empty ablation, and AI on with no plans and no window
    md2 = write_report(tmp_path / "none", "None", None, None, None, None, None, None, {}, None)
    assert "INSUFFICIENT DATA" in md2.read_text()
    md3 = write_report(
        tmp_path / "ai",
        "AI on, no data",
        _meta(window_from=None, window_to=None),
        {},
        [],
        [],
        [],
        [],
        None,
        ["Store is empty: api.wallex.ir unreachable."],
    )
    text3 = md3.read_text()
    assert "Store is empty: api.wallex.ir unreachable." in text3
    assert "ai=on but no AI plans were recorded" in text3
    assert "| Requested window (UTC) | none |" in text3


def test_empty_store_report(tmp_path: Path) -> None:
    """The real 'no recorded Wallex data' situation: the store exists but holds nothing."""
    with Store(tmp_path / "empty.sqlite") as store:
        lo, hi = store.time_range("candles")
        equity = read_equity_table(store)
    metrics = compute_metrics(equity, [])
    assert metrics.insufficient_data
    md = write_report(
        tmp_path / "bt",
        "Backtest — no data",
        _real_meta(
            data_from=lo, data_to=hi, window_from="2026-10-21T10:00:00Z", window_to="2026-10-22T10:00:00Z"
        ),
        metrics,
        equity,
        [],
    )
    text = md.read_text()
    assert "INSUFFICIENT DATA" in text
    assert "| Data present (UTC) | none |" in text
    assert "insufficient data: no equity rows" in text  # the metrics' own note
    assert "No closed trades" not in text  # not claimed: there was no data at all
    assert "| Return | n/a |" in text


# ---------------------------------------------------------------- index


def test_index_lists_reports(tmp_path: Path, inputs: dict[str, Any]) -> None:
    reports = tmp_path / "REPORTS"
    _write(reports / "bt_books_3h", inputs, title="Books backtest (synthetic)")
    _write(
        reports / "shadow_2026-10-21",
        inputs,
        title="Shadow run",
        meta=_real_meta(mode="shadow", window_from="2026-10-21T06:00:00Z", window_to="2026-10-21T10:00:00Z"),
    )
    (reports / "api_probe.md").write_text(
        "# Wallex API probe\n\n- generated: 2026-10-20T10:43:05.764Z (UTC)\n"
    )
    (reports / "notes").mkdir()  # a folder without markdown is not a report
    readme = update_index(reports)
    assert readme == reports / INDEX_MD
    text = readme.read_text()
    assert (
        "| [Books backtest (synthetic)](bt_books_3h/report.md) | 2026-10-22 | synthetic | `bt_books_3h/` |"
        in text
    )
    assert "| [Shadow run](shadow_2026-10-21/report.md) | 2026-10-21 | — | `shadow_2026-10-21/` |" in text
    assert "| [Wallex API probe](api_probe.md) | 2026-10-20 | — | `api_probe.md` |" in text
    assert "notes" not in text
    assert (
        text.index("bt_books_3h") < text.index("shadow_2026-10-21") < text.index("api_probe.md")
    )  # newest first
    assert update_index(reports).read_text() == text  # idempotent; never lists itself


def test_index_on_empty_dir(tmp_path: Path) -> None:
    text = update_index(tmp_path / "REPORTS").read_text()
    assert "_No reports yet._" in text


# ---------------------------------------------------------------- final report


def _image_links(text: str) -> list[str]:
    return re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)


def test_final_assembly_with_missing_sections(tmp_path: Path, inputs: dict[str, Any]) -> None:
    reports = tmp_path / "docs" / "REPORTS"
    _write(
        reports / "bt_candles_30d",
        inputs,
        title="Candles backtest",
        meta=_meta(driver="candles", strategies="S2,S3", ai="off"),
    )
    (reports / "survey_2026-10-22").mkdir(parents=True)
    (reports / "survey_2026-10-22" / "spreads.png").write_bytes(PNG_MAGIC)
    (reports / "survey_2026-10-22.md").write_text(
        "# Market survey 2026-10-22\n\n## S1 opportunities\n\n![spreads](survey_2026-10-22/spreads.png)\n"
        "\n```\n# not a heading\n```\n"
    )
    out = assemble_final_report(reports, tmp_path / "deliverables" / "REPORT.md")
    assert out == tmp_path / "deliverables" / "REPORT.md"
    text = out.read_text()

    h1 = [ln for ln in text.splitlines() if ln.startswith("# ")]
    assert h1 == ["# Wallex Trader Agent — final report", "# not a heading"]  # 2nd is inside a code fence
    assert (
        text.index("## 1. Market survey")
        < text.index("## 2. Backtest: 1-min candles")
        < text.index("## 7. Replay")
    )
    assert "### S1 opportunities" in text  # embedded headings demoted under the section
    assert "### Metrics" in text and "#### End state" in text
    assert "```\n# not a heading\n```" in text  # code fences untouched
    for title in (
        "Backtest: recorded order-book window",
        "AI ablation",
        "Shadow (paper) run",
        "Live challenge run",
        "Replay proof",
    ):
        section = text.split(f". {title}", 1)[1].split("\n## ", 1)[0]
        assert NOT_AVAILABLE.capitalize() in section, title
    assert f"| 6 | Live challenge run | {NOT_AVAILABLE} | — |" in text
    assert "| 2 | Backtest: 1-min candles, 30 days | included (2026-10-22) — SYNTHETIC |" in text
    assert SYNTHETIC_BANNER in text  # the candles report was synthetic
    assert "from `../docs/REPORTS`" in text
    assert str(tmp_path) not in text  # no absolute paths in a deliverable

    links = _image_links(text)
    assert "../docs/REPORTS/bt_candles_30d/equity.png" in links
    assert "../docs/REPORTS/survey_2026-10-22/spreads.png" in links
    for link in links:
        assert (out.parent / link).resolve().is_file(), link  # every PNG resolves from deliverables/
    assert "[trades.csv](../docs/REPORTS/bt_candles_30d/trades.csv)" in text


def test_final_assembly_latest_and_most_specific_report_wins(tmp_path: Path, inputs: dict[str, Any]) -> None:
    reports = tmp_path / "REPORTS"
    _write(reports / "bt_books_old", inputs, title="Books OLD", meta=_meta(window_to="2026-10-21T10:00:00Z"))
    _write(reports / "bt_books_new", inputs, title="Books NEW", meta=_meta(window_to="2026-10-23T10:00:00Z"))
    _write(
        reports / "bt_books_ablation",
        inputs,
        title="Ablation run",
        meta=_meta(window_to="2026-10-24T10:00:00Z"),
    )
    text = assemble_final_report(reports, tmp_path / "REPORT.md").read_text()
    books = text.split("## 3. Backtest: recorded order-book window", 1)[1].split("\n## 4.", 1)[0]
    assert "Books NEW" in books and "Books OLD" not in books and "Ablation run" not in books
    ablation = text.split("## 4. AI ablation", 1)[1].split("\n## 5.", 1)[0]
    assert "Ablation run" in ablation


def test_final_assembly_custom_sections_and_empty_dir(tmp_path: Path) -> None:
    reports = tmp_path / "REPORTS"
    reports.mkdir()
    (reports / "extra_notes.md").write_text(
        "# Extra notes\n\nSee [the index](README.md#top) and [web](https://x.y/z).\n"
    )
    out = assemble_final_report(
        reports,
        tmp_path / "out" / "R.md",
        sections=["survey", ("Operator notes", "extra_*.md"), FinalSection("x", "Custom", ("nope*.md",))],
    )
    text = out.read_text()
    assert "## 1. Market survey" in text and "## 2. Operator notes" in text and "## 3. Custom" in text
    assert "[the index](../REPORTS/README.md#top)" in text  # anchors kept, path rebased
    assert "[web](https://x.y/z)" in text  # absolute URLs untouched
    assert text.count(NOT_AVAILABLE) >= 2

    with pytest.raises(ValueError, match="unknown section"):
        assemble_final_report(reports, tmp_path / "x.md", sections=["nonsense"])

    empty = assemble_final_report(tmp_path / "missing", tmp_path / "empty" / "REPORT.md").read_text()
    assert empty.count(f"| {NOT_AVAILABLE} |") == 7
    assert SYNTHETIC_BANNER not in empty


def test_decision_log_links_cover_every_daily_file(tmp_path: Path, inputs: dict[str, Any]) -> None:
    """``decisions.jsonl`` is the engine's link to the newest day only: the report links every day."""
    out = tmp_path / "bt"
    out.mkdir()
    for day in ("20261001", "20261002", "20261003"):
        (out / f"decisions-{day}.jsonl").write_text('{"kind": "veto"}\n')
    (out / "decisions.jsonl").symlink_to("decisions-20261003.jsonl")
    text = _write(out, inputs).read_text()
    assert ("Decision records (3 daily files, together the whole run: [decisions-20261001.jsonl]"
            "(decisions-20261001.jsonl), [decisions-20261002.jsonl](decisions-20261002.jsonl), "
            "[decisions-20261003.jsonl](decisions-20261003.jsonl); `decisions.jsonl` links only the newest day") in text
    assert "([decisions.jsonl](decisions.jsonl))" not in text
    single = tmp_path / "one"
    single.mkdir()
    (single / "decisions-20261001.jsonl").write_text('{"kind": "veto"}\n')
    assert "Decision records ([decisions-20261001.jsonl](decisions-20261001.jsonl)):" in _write(single, inputs).read_text()


def test_report_date_is_labelled_and_never_taken_for_the_data_end(tmp_path: Path, inputs: dict[str, Any]) -> None:
    given = _write(tmp_path / "given", inputs, meta=_meta(report_date="2026-10-23", data_end="2026-10-22T13:00:00Z",
                                                          report_date_source="commit date of abc123")).read_text()
    assert "| Data end (UTC) | 2026-10-22T13:00:00Z |" in given
    assert "| Report date | 2026-10-23 (commit date of abc123) |" in given
    assert json.loads((tmp_path / "given" / SUMMARY_JSON).read_text())["report_date"] == "2026-10-23"
    fallback = _write(tmp_path / "fallback", inputs).read_text()  # no date given: says it is the data's end
    assert "| Report date | 2026-10-22 (no report date given: the data's end) |" in fallback


def test_a_companion_markdown_takes_the_date_of_its_runs_summary(tmp_path: Path, inputs: dict[str, Any]) -> None:
    """``walkforward.md`` sits next to the final run's ``summary.json``: its date is that report date,
    not the first data date in its text (2026-10-01 here)."""
    reports = tmp_path / "REPORTS"
    _write(reports / "wf", inputs, meta=_meta(report_date="2026-10-23"))
    (reports / "wf" / "README.md").unlink(missing_ok=True)
    (reports / "wf" / "report.md").unlink()
    (reports / "wf" / "walkforward.md").write_text("# Walk-forward: `x`\n\n- In-sample: 2026-10-01T00:00:00.000Z\n")
    text = update_index(reports).read_text()
    assert "| [Walk-forward: `x`](wf/walkforward.md) | 2026-10-23 |" in text
