"""``scripts/flatten_now.py`` and the engine's manual-flatten mode (PRD-02 §7).

A mixed paper portfolio (TMN, USDT and four coins, one of them on a market without MARKET orders)
is driven to 100 % TMN minus dust on the deterministic synthetic feed. The first two tests tick a
``SimClock`` by hand; the last ones run the script itself for a few wall-clock seconds with tiny
intervals. Nothing touches the network.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wta.config import ROOT, Config, Settings, load_config
from wta.data.store import Store
from wta.engine import KV_PAPER, Engine, build_engine
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock

from .conftest import T0

MIXED = {
    "TMN": D("1000000"),
    "USDT": D("5"),
    "BTC": D("0.00002"),
    "ETH": D("0.001"),
    "SOL": D("0.05"),  # SOLTMN has MARKET orders disabled: phase 2 crosses with a LIMIT
    "DOGE": D("40"),
}


def _cfg(tmp_path: Path, **extra: Any) -> Config:
    return load_config(
        overrides={
            "data.sqlite_path": str(tmp_path / "wta.sqlite"),
            "data.state_sqlite_path": str(tmp_path / "wta_{mode}.sqlite"),  # the engine's state DB (PRD-03)
            "data.ws_enabled": False,
            **extra,
        },
        env={},
    )


async def _drive(tmp_path: Path, phase1_s: float, max_ticks: int = 200) -> tuple[Engine, int]:
    clock = SimClock(T0)
    engine = await build_engine(
        _cfg(tmp_path),
        Settings(_env_file=None),
        "shadow",
        feed="synthetic",
        clock=clock,
        paper_balances=MIXED,
        log_dir=tmp_path / "logs",
        kill_file=tmp_path / "KILL",
    )
    engine.enable_manual_flatten(phase1_s)
    feed = engine.synthetic
    feed.publish(clock.now())
    await engine.startup()
    ticks = 0
    while not engine._stop.is_set() and ticks < max_ticks:
        clock.advance(2)
        feed.step(2)
        feed.publish(clock.now())
        await engine.tick(clock.now())
        ticks += 1
    engine.shutdown(engine._stop_reason or "test limit")
    await engine.aclose()
    return engine, ticks


def _flatten_records(tmp_path: Path) -> list[dict[str, Any]]:
    merged = merge_decisions(iter_family(tmp_path / "logs", "decisions", strict=True))
    return [d for d in merged.values() if d["strategy"] == "FLATTEN"]


def _assert_flat(engine: Engine, tmp_path: Path) -> None:
    after = engine.flatten_report["after"]
    assert after is not None and engine._stop_reason == "flatten complete"
    for asset, total in after["balances"].items():
        if asset == "TMN":
            continue
        market = engine.flatten.target_market(asset)
        assert market is not None
        bid = engine.bus.latest(market.symbol).best_bid
        assert D(total) * bid < engine.flatten.min_notional(market), f"{asset} is not dust"
    before = engine.flatten_report["before"]
    assert before is not None and set(before["balances"]) == set(MIXED)
    # equity is kept, minus fees and spread (a few tenths of a percent)
    assert D(after["equity_tmn"]) > D(before["equity_tmn"]) * D("0.98")
    assert D(after["tmn"]) / D(after["equity_tmn"]) > D("0.999")
    records = _flatten_records(tmp_path)
    assert records and all(r["intent"]["side"] == "SELL" for r in records)  # never a BUY
    places = [r for r in iter_family(tmp_path / "logs", "orders", strict=True) if r["action"] == "place"]
    assert places and all(r["data"]["request"]["side"] == "SELL" for r in places)
    assert check_logs(tmp_path / "logs").ok


async def test_phase_two_sells_everything_with_market_or_crossing_orders(tmp_path: Path) -> None:
    engine, ticks = await _drive(tmp_path, phase1_s=0)
    _assert_flat(engine, tmp_path)
    assert ticks <= 5
    by_symbol = {r["symbol"]: r["intent"]["order_type"] for r in _flatten_records(tmp_path)}
    assert by_symbol == {
        "BTCTMN": "MARKET",
        "DOGETMN": "MARKET",
        "ETHTMN": "MARKET",
        "SOLTMN": "LIMIT",  # MARKET disabled: a LIMIT 0.5 % under the bid
        "USDTTMN": "MARKET",
    }
    (complete,) = [e for e in iter_family(tmp_path / "logs", "events") if e["kind"] == "flatten"]
    assert complete["data"]["kind"] == "complete" and complete["data"]["trigger"] == "manual"


async def test_phase_one_is_maker_first_and_the_state_survives_in_the_store(tmp_path: Path) -> None:
    engine, ticks = await _drive(tmp_path, phase1_s=600)
    _assert_flat(engine, tmp_path)
    records = _flatten_records(tmp_path)
    assert {r["intent"]["order_type"] for r in records} == {"LIMIT"}
    assert all(r["intent"]["price"] is None for r in records)  # the OrderManager prices maker-first
    # a synthetic exercise keeps its own paper account (never the real-feed shadow one)
    store = Store.open_readonly(tmp_path / "wta_shadow_synthetic.sqlite")
    paper = store.kv_get(KV_PAPER)
    store.close()
    assert set(paper["balances"]) >= {"TMN", "BTC"} and paper["trades"]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flatten_now", ROOT / "scripts" / "flatten_now.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_flattens_a_mixed_paper_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "load_settings", lambda: Settings(_env_file=None))
    overrides = {
        "data.sqlite_path": tmp_path / "wta.sqlite",
        "data.ws_enabled": "false",
        "data.store_flush_interval_s": 0.1,
        "engine.fast_loop_interval_s": 0.05,
        "engine.heartbeat_interval_s": 1,
        "execution.poll_interval_s": 0.05,
        "execution.limit_reprice_after_s": 0.2,
        "execution.flatten_max_reprices": 1,
    }
    argv = [
        "--feed",
        "synthetic",
        "--phase1-seconds",
        "0.5",
        "--timeout",
        "30",
        "--log-dir",
        str(tmp_path / "logs"),
    ]
    argv += ["--paper-balances", '{"TMN": 1000000, "USDT": 5, "BTC": "0.00002", "ETH": "0.001", "DOGE": 40}']
    for key, value in overrides.items():
        argv += ["--set", f"{key}={value}"]
    assert script.main(argv) == 0
    out = capsys.readouterr().out
    assert "stopped: flatten complete" in out and "result: 100 % TMN (dust excepted)" in out
    before, after = out.split("before:")[1].split("after:")
    assert "BTC" in before and "DOGE" in before
    assert "DOGE" not in after and "equity" in after
    assert check_logs(tmp_path / "logs").ok


def test_the_script_refuses_live_without_the_guards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.delenv("WTA_LIVE", raising=False)
    monkeypatch.setattr(
        script, "load_settings", lambda: Settings(_env_file=None, wallex_api_key="flatten-test-0000")
    )
    assert script.main(["--i-understand-live"]) == 2
    assert "WTA_LIVE=1" in capsys.readouterr().err
    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr("wta.safety.tree_is_dirty", lambda root=None: False)
    assert script.main(["--i-understand-live", "--feed", "synthetic"]) == 2
    assert "live feed only" in capsys.readouterr().err


def test_a_store_without_a_paper_account_and_no_balances_is_already_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "load_settings", lambda: Settings(_env_file=None))
    argv = ["--feed", "synthetic", "--timeout", "10", "--log-dir", str(tmp_path / "logs")]
    argv += [
        "--set",
        f"data.sqlite_path={tmp_path / 'wta.sqlite'}",
        "--set",
        "engine.fast_loop_interval_s=0.05",
    ]
    assert script.main(argv) == 0
    out = capsys.readouterr().out
    assert "flatten complete" in out and f"TMN    {Decimal('5000000')}" in out


def test_the_script_refuses_to_run_while_an_engine_holds_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two processes must never trade the same account: the engine's lock is checked."""
    from wta.engine import acquire_engine_lock, release_engine_lock

    script = _load_script()
    monkeypatch.setattr(script, "load_settings", lambda: Settings(_env_file=None))
    db = tmp_path / "wta_shadow_synthetic.sqlite"  # the synthetic paper account of the template below
    held = acquire_engine_lock(str(db))  # a running `wta run --feed synthetic` on the same store
    argv = ["--feed", "synthetic", "--timeout", "5", "--log-dir", str(tmp_path / "logs")]
    argv += ["--set", f"data.state_sqlite_path={tmp_path / 'wta_{mode}.sqlite'}"]  # the lock follows the state DB
    try:
        assert script.main(argv) == 2
        assert "another engine or flatten_now" in capsys.readouterr().err
    finally:
        release_engine_lock(held)
    assert not db.exists()  # nothing was opened, nothing traded
