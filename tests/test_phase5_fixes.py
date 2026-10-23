"""Small Phase-5 fixes found while running the PRD-05 reports: clustered synthetic candle volume
(S3's volume filter can pass), per-trade fees from the state DB's positions, the engine's fee total
for shadow/live reports, and fee columns that say n/a instead of a false 0. Offline, no network."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from backtest.metrics import trade_stats, trades_from_decisions
from backtest.runner import (
    add_trade_fees,
    make_synthetic_market_db,
    run_backtest,
    state_fees_tmn,
    synthetic_marker,
)
from wta.config import Config, Settings, load_config
from wta.data.store import Store


def _vol_ratios_15m(path: Path, symbol: str) -> list[float]:
    with Store.open_readonly(path) as store:
        candles = list(store.load_candles(symbol, "1"))
    bars: dict[int, float] = {}
    for c in candles:
        bars[int(c.ts) // 900 * 900] = bars.get(int(c.ts) // 900 * 900, 0.0) + float(c.v)
    vols = [v for _, v in sorted(bars.items())]
    return [vols[i] / (sum(vols[i - 20:i]) / 20) for i in range(20, len(vols))]


def test_clustered_candle_volume_lets_the_s3_volume_filter_pass(tmp_path: Path) -> None:
    clustered = make_synthetic_market_db(tmp_path / "c.sqlite", hours=24, seed=7, step_s=10, with_books=False)
    uniform = make_synthetic_market_db(tmp_path / "u.sqlite", hours=24, seed=7, step_s=10, with_books=False,
                                       volume_model="uniform")
    assert clustered["volume_model"] == "clustered" and synthetic_marker(tmp_path / "c.sqlite")["version"] == 2
    assert uniform["volume_model"] == "uniform"
    # uniform per-step draws average out: a 15-minute bar never reaches 1.5x its 20-bar mean
    assert max(_vol_ratios_15m(tmp_path / "u.sqlite", "BTCUSDT")) < 1.5
    assert max(_vol_ratios_15m(tmp_path / "c.sqlite", "BTCUSDT")) >= 1.5
    # prices are the same random walk: the volume model never touches them
    with Store.open_readonly(tmp_path / "c.sqlite") as a, Store.open_readonly(tmp_path / "u.sqlite") as b:
        pa = [(c.ts, c.o, c.h, c.l, c.c) for c in a.load_candles("ETHUSDT", "1")]
        pb = [(c.ts, c.o, c.h, c.l, c.c) for c in b.load_candles("ETHUSDT", "1")]
    assert pa == pb != []
    # books mode keeps the feed's own volumes, and an unknown model is refused
    books = make_synthetic_market_db(tmp_path / "b.sqlite", hours=0.02, seed=7, step_s=2)
    assert books["volume_model"] == "feed"
    with pytest.raises(ValueError, match="volume_model"):
        make_synthetic_market_db(tmp_path / "x.sqlite", hours=1, seed=7, with_books=False, volume_model="loud")


def _state_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE positions(id TEXT PRIMARY KEY, json TEXT, open INTEGER, updated_ts REAL)")
    conn.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, json TEXT, updated_ts REAL)")
    conn.execute("INSERT INTO positions VALUES (?, ?, 0, 1)",
                 ("p_S1_ETHUSDT_1_1", json.dumps({"id": "p_S1_ETHUSDT_1_1", "meta": {"fees_tmn": "2293.5"}})))
    conn.execute("INSERT INTO kv VALUES ('portfolio_state', ?, 1)", (json.dumps({"fees_paid_tmn_total": "6239.78"}),))
    conn.commit()
    conn.close()
    return path


def test_trade_fees_come_from_the_positions_and_the_fee_total_from_kv(tmp_path: Path) -> None:
    db = _state_db(tmp_path / "state.sqlite")
    decisions = [
        {"kind": "order", "decision_id": "d1", "strategy": "S1", "symbol": "ETHUSDT", "ts": "2026-10-01T00:00:00Z"},
        {"kind": "outcome", "decision_id": "d1", "ts": "2026-10-01T00:02:00Z",
         "outcome": {"pnl_tmn": "-1359", "pnl_pct": "-0.0014", "hold_s": 108, "exit_reason": "target",
                     "position_id": "p_S1_ETHUSDT_1_1"}},
    ]
    merged = [{**decisions[0], "outcome": decisions[1]["outcome"]}]
    trades = trades_from_decisions(merged)
    assert trades and trades[0]["fees_tmn"] is None
    add_trade_fees(trades, db)
    assert trades[0]["fees_tmn"] == pytest.approx(2293.5)
    assert state_fees_tmn(db) == pytest.approx(6239.78)
    assert state_fees_tmn(tmp_path / "missing.sqlite") is None
    assert add_trade_fees([{"position_id": "p_x", "fees_tmn": None}], tmp_path / "missing.sqlite")[0]["fees_tmn"] is None


def test_fee_columns_say_na_without_fee_data() -> None:
    no_fees = [{"pnl_tmn": 5.0, "pnl_pct": 0.001, "hold_s": 10.0, "fees_tmn": None}]
    assert trade_stats(no_fees)["fees_tmn"] is None
    assert trade_stats([])["fees_tmn"] is None
    assert trade_stats([*no_fees, {"pnl_tmn": 1.0, "pnl_pct": None, "hold_s": None, "fees_tmn": 2.0}])["fees_tmn"] == 2.0


# ---------------------------------------------------------------- automatic limitations (kill, fee budget, S3 rules-only)


@pytest.fixture(scope="module")
def candle_db(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_synthetic_market_db(tmp_path_factory.mktemp("lim") / "synth.sqlite", hours=3, seed=11, step_s=10,
                                    with_books=False)


def _run(cfg: Config, db: dict[str, Any], out: Path, strategies: str) -> Any:
    return run_backtest(cfg, driver="candles", from_ts=db["from_ts"], to_ts=db["to_ts"], strategies=strategies,
                        ai="off", market_db=db["path"], out_dir=out, settings=Settings(_env_file=None))


def test_limitations_name_the_fee_budget_and_s3_rules_only(tmp_path: Path, candle_db: dict[str, Any]) -> None:
    cfg = load_config(env={}, overrides={"risk.fee_budget_pct": "0.00001",
                                         "strategies.s3_momentum.allow_rules_only": True})
    res = _run(cfg, candle_db, tmp_path / "fee", "S2,S3")
    assert res.metrics.vetoes_by_code.get("FEE_BUDGET", 0) >= 1
    assert any("fee budget" in x and "FEE_BUDGET" in x for x in res.limitations)
    assert any("allow_rules_only=true" in x for x in res.limitations)
    assert not any("kill-switch fired" in x for x in res.limitations)
    report = (tmp_path / "fee" / "report.md").read_text()
    assert "FEE_BUDGET from" in report


def test_limitations_name_the_kill_switch(tmp_path: Path, candle_db: dict[str, Any]) -> None:
    cfg = load_config(env={}, overrides={"risk.max_drawdown_kill_pct": "0.0001"})
    res = _run(cfg, candle_db, tmp_path / "kill", "S2")
    kills = [x for x in res.limitations if "kill-switch fired" in x]
    assert kills and "drawdown" in kills[0] and "only monitored afterwards" in kills[0]
    assert not any("allow_rules_only" in x for x in res.limitations)
