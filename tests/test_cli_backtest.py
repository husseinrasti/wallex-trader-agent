"""PRD-05 §3.3, §5-§7: the ``wta backtest | report | export | replay`` commands.

Wiring tests replace the runner/walk-forward/replay with stubs and check what the CLI passes; the
end-to-end tests run on tiny synthetic market DBs in ``tmp_path``. Never the network, never the
developer's ``.env`` (the ``use_settings`` fixture), never the repo's ``logs/`` or ``data/``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

import backtest.runner as bt
import backtest.walkforward as wf
from backtest.metrics import Metrics
from backtest.runner import BacktestResult, make_synthetic_market_db
from wta import cli
from wta.config import Settings, load_config

from .test_cli import ENV_VARS


@pytest.fixture
def use_settings(monkeypatch: pytest.MonkeyPatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def install(**values: Any) -> Settings:
        settings = Settings(_env_file=None, **values)
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        return settings

    install()
    return install


@pytest.fixture(scope="module")
def books_db(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_synthetic_market_db(tmp_path_factory.mktemp("cli") / "synth.sqlite", hours=0.05, seed=9, step_s=2)


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory, books_db: dict[str, Any]) -> Path:
    """A finished backtest's folder (its logs + state DB stand in for a shadow run's)."""
    out = tmp_path_factory.mktemp("run") / "bt"
    bt.run_backtest(load_config(env={}), driver="books", from_ts=books_db["from_ts"], to_ts=books_db["to_ts"],
                    strategies="S1,S2", market_db=books_db["path"], out_dir=out, settings=Settings(_env_file=None))
    return out


def _fake_result(out: Path, **meta: Any) -> BacktestResult:
    m = Metrics(return_pct=0.001, max_drawdown_pct=0.002, n_trades=1, n_orders=2, insufficient_data=False)
    base = {"driver": "books", "strategies": ["S2"], "ai": "off", "ai_source": "disabled", "coverage_hours": 1.0,
            "ticks": 10, "window_from": "a", "window_to": "b", "synthetic": False}
    return BacktestResult(metrics=m, paths={"out_dir": out, "report": out / "report.md"}, meta={**base, **meta})


# ---------------------------------------------------------------- placeholders are gone


def test_prd05_commands_are_real_commands() -> None:
    assert {"backtest", "report", "export", "replay"}.isdisjoint(cli.PLACEHOLDERS)
    parser = cli.build_parser()
    for name in ("backtest", "report", "export", "replay"):
        args = parser.parse_args([name])
        assert args.handler.__name__ == f"cmd_{name}"


# ---------------------------------------------------------------- backtest wiring


def test_backtest_wiring(use_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(cfg: Any, **kwargs: Any) -> BacktestResult:
        calls.append(kwargs)
        return _fake_result(Path(kwargs["out_dir"]))

    monkeypatch.setattr(bt, "run_backtest", fake_run)
    out = tmp_path / "out"
    code = cli.main(["backtest", "--driver", "books", "--from", "2026-10-22T10:00Z", "--to", "2026-10-23T10:00Z",
                     "--ai", "offline", "--strategies", "S1,S2", "--out", str(out), "--db", str(tmp_path / "m.sqlite"),
                     "--ai-max-calls", "7", "--seed", "3", "--balances", '{"TMN": 1000000, "USDT": 2}',
                     "--no-flatten-at-end", "--log-level", "WARNING"])
    assert code == 0
    (kw,) = calls
    assert kw["driver"] == "books" and kw["ai"] == "offline" and kw["strategies"] == "S1,S2"
    assert kw["from_ts"] == 1_792_663_200.0 and kw["to_ts"] == 1_792_663_200.0 + 86400
    assert kw["out_dir"] == str(out) and kw["market_db"] == str(tmp_path / "m.sqlite")
    assert kw["ai_max_calls"] == 7 and kw["seed"] == 3 and kw["flatten_at_end"] is False
    assert kw["initial_balances"] == {"TMN": 1000000, "USDT": 2}
    text = capsys.readouterr().out
    assert "return +0.100%" in text and "report:" in text
    # defaults: candles, ai off, 150 cycles, out under docs/REPORTS (not written: the runner is a stub)
    assert cli.main(["backtest", "--from", "2026-09-22", "--to", "2026-10-22", "--log-level", "WARNING"]) == 0
    kw = calls[-1]
    assert kw["driver"] == "candles" and kw["ai"] == "off" and kw["ai_max_calls"] == 150 and kw["strategies"] is None
    assert kw["out_dir"] == "docs/REPORTS/bt_candles_20260922_20261022" and kw["flatten_at_end"] is None


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["backtest", "--from", "2026-10-22"], "--from and --to are required"),
        (["backtest", "--from", "2026-10-22", "--to", "2026-10-23", "--synthetic-hours", "2"], "needs --synthetic-db"),
        (["backtest", "--from", "tomorrow", "--to", "2026-10-23"], "not an ISO-8601 time"),
        (["backtest", "--from", "2026-10-22", "--to", "2026-10-23", "--balances", "{bad"], "balances"),
    ],
)
def test_backtest_bad_arguments(use_settings, capsys, argv: list[str], message: str) -> None:
    assert cli.main([*argv, "--log-level", "WARNING"]) == cli.EXIT_CANNOT_RUN
    assert message in capsys.readouterr().err


def test_backtest_runner_errors_exit_2(use_settings, tmp_path: Path, capsys) -> None:
    code = cli.main(["backtest", "--from", "2026-10-22", "--to", "2026-10-23", "--db", str(tmp_path / "none.sqlite"),
                     "--out", str(tmp_path / "out"), "--log-level", "WARNING"])
    assert code == cli.EXIT_CANNOT_RUN and "does not exist" in capsys.readouterr().err
    code = cli.main(["backtest", "--from", "2026-10-22", "--to", "2026-10-23", "--strategies", "S1",
                     "--synthetic-db", str(tmp_path / "s.sqlite"), "--synthetic-hours", "0.02",
                     "--out", str(tmp_path / "out2"), "--log-level", "WARNING"])
    assert code == cli.EXIT_CANNOT_RUN and "S1 cannot be backtested on candles" in capsys.readouterr().err


def test_backtest_param_grid_goes_to_the_walk_forward(use_settings, monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path, capsys) -> None:
    seen: dict[str, Any] = {}

    def fake_wf(cfg: Any, *, grid: dict[str, list[Any]], out_dir: Any, **kwargs: Any) -> Any:
        seen.update(grid=grid, out_dir=out_dir, **kwargs)
        point = wf.Point({"strategies.s1_crossbook.margin_min_pct": 0.0012}, False, 1)
        return types.SimpleNamespace(chosen=point, reason="degradation 10%", final_params=point.params,
                                     paths={"markdown": Path(out_dir) / "walkforward.md"},
                                     final=_fake_result(Path(out_dir)))

    monkeypatch.setattr(wf, "run_walkforward", fake_wf)
    code = cli.main(["backtest", "--driver", "books", "--from", "2026-10-22", "--to", "2026-10-23", "--out",
                     str(tmp_path / "o"), "--param-grid", "strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015",
                     "--log-level", "WARNING"])
    assert code == 0
    assert seen["grid"] == {"strategies.s1_crossbook.margin_min_pct": [0.0008, 0.0012, 0.0015]}
    assert seen["driver"] == "books" and seen["out_dir"] == str(tmp_path / "o")
    assert "walk-forward: chosen margin_min_pct=0.0012" in capsys.readouterr().out
    bad = cli.main(["backtest", "--from", "2026-10-22", "--to", "2026-10-23", "--param-grid", "a.b=1",
                    "--log-level", "WARNING"])
    assert bad == cli.EXIT_CANNOT_RUN and "unknown config key a.b" in capsys.readouterr().err


def test_backtest_end_to_end_on_a_synthetic_db(use_settings, tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    code = cli.main(["backtest", "--driver", "books", "--synthetic-db", str(tmp_path / "s.sqlite"),
                     "--synthetic-hours", "0.05", "--strategies", "S2", "--out", str(out), "--log-level", "WARNING"])
    assert code == 0
    text = capsys.readouterr().out
    assert "SYNTHETIC — not evidence of performance" in text and "generated" in text
    report = (out / "report.md").read_text()
    assert "SYNTHETIC DATA — not evidence of performance" in report and "Book window = 0.05 h" in report
    # the same command reuses the synthetic DB (same marker) and replaces its own logs
    assert cli.main(["backtest", "--driver", "books", "--synthetic-db", str(tmp_path / "s.sqlite"),
                     "--synthetic-hours", "0.05", "--strategies", "S2", "--out", str(out), "--log-level", "WARNING"]) == 0
    assert "(reused)" in capsys.readouterr().out


# ---------------------------------------------------------------- report


def test_report_from_a_run_dir(use_settings, run_dir: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "rep"
    assert cli.main(["report", "--source", str(run_dir), "--out", str(out), "--log-level", "WARNING"]) == 0
    text = capsys.readouterr().out
    assert "SYNTHETIC" in text and "equity from state_db" in text
    report = (out / "report.md").read_text()
    assert "SYNTHETIC DATA — not evidence of performance" in report
    for name in ("summary.json", "equity.csv", "trades.csv", "equity.png", "drawdown.png"):
        assert (out / name).is_file()
    assert "Paper vs live divergence" not in report


def test_report_with_compare_adds_the_divergence(use_settings, run_dir: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "rep"
    code = cli.main(["report", "--source", str(run_dir), "--compare", str(run_dir), "--out", str(out),
                     "--log-level", "WARNING"])
    assert code == 0
    report = (out / "report.md").read_text()
    assert report.index("## Paper vs live divergence") < report.index("## Limitations")
    import json

    div = json.loads((out / "divergence.json").read_text())
    assert div["matched"] == div["signals_a"] == div["signals_b"] > 0  # a run against itself: all matched
    assert div["only_a"] == div["only_b"] == 0 and div["kind_differs"] == 0
    assert div["mean_abs_price_diff_bps"] in (None, 0.0)
    assert "divergence:" in capsys.readouterr().out


def test_report_falls_back_to_heartbeats_without_a_state_db(use_settings, books_db: dict[str, Any],
                                                             tmp_path: Path) -> None:
    run = tmp_path / "run"
    bt.run_backtest(load_config(env={}), driver="books", from_ts=books_db["from_ts"], to_ts=books_db["to_ts"],
                    strategies="S2", market_db=books_db["path"], out_dir=run, settings=Settings(_env_file=None))
    for p in run.glob("state.sqlite*"):
        p.unlink()
    res = bt.report_from_logs(source=run, out_dir=tmp_path / "rep")
    assert res.meta["equity_source"] == "heartbeats" and res.meta["state_db"] is None
    assert res.metrics.n_equity_points >= 2  # start and end heartbeats
    assert "Equity curve from the heartbeats" in (tmp_path / "rep" / "report.md").read_text()
    with pytest.raises(FileNotFoundError):
        bt.report_from_logs(source=run, out_dir=tmp_path / "rep2", state_db=tmp_path / "missing.sqlite")


def test_report_without_logs_exits_1(use_settings, tmp_path: Path, capsys) -> None:
    (tmp_path / "empty").mkdir()
    assert cli.main(["report", "--source", str(tmp_path / "empty"), "--out", str(tmp_path / "o"),
                     "--log-level", "WARNING"]) == cli.EXIT_FAILED
    assert "no log records" in capsys.readouterr().err


def test_divergence_matches_by_intent_and_time() -> None:
    def rec(ts: str, side: str, kind: str = "order", price: str | None = "100") -> dict[str, Any]:
        out = {"status": "FILLED", "avg_price": price} if price else None
        return {"kind": kind, "ts": ts, "strategy": "S2", "symbol": "USDTTMN", "decision_id": ts + side,
                "intent": {"side": side, "purpose": "rebalance"}, "order": {"accepted": True}, "outcome": out}

    a = [rec("2026-10-22T10:00:00Z", "BUY"), rec("2026-10-22T11:00:00Z", "SELL")]
    b = [rec("2026-10-22T10:00:10Z", "BUY", price="101"), rec("2026-10-22T12:00:00Z", "SELL")]
    d = bt.divergence(a, b, tolerance_s=30)
    assert (d["matched"], d["only_a"], d["only_b"]) == (1, 1, 1)
    assert d["rows"][0]["price_diff_bps"] == pytest.approx(100.0)
    assert d["mean_abs_price_diff_bps"] == pytest.approx(100.0)
    vetoed = [rec("2026-10-22T10:00:05Z", "BUY", kind="veto", price=None)]
    d = bt.divergence(a, vetoed)
    assert d["matched"] == 1 and d["kind_differs"] == 1 and d["fill_status_differs"] == 1


# ---------------------------------------------------------------- export


def test_export_falls_back_to_csv_without_pyarrow(use_settings, monkeypatch: pytest.MonkeyPatch,
                                                  books_db: dict[str, Any], tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(bt, "_has_pyarrow", lambda: False)
    out = tmp_path / "export"
    assert cli.main(["export", "--parquet", str(out), "--db", books_db["path"], "--log-level", "WARNING"]) == 0
    text = capsys.readouterr().out
    assert "pyarrow is not installed" in text
    import json

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["format"] == "csv" and manifest["tables"]["books"]["rows"] == books_db["books"]
    assert manifest["tables"]["candles"]["rows"] == books_db["candles"]
    assert (out / "books.csv").read_text().startswith("ts,symbol,source,bids_json,asks_json")
    assert manifest["tables"]["trades"]["rows"] == 0


def test_export_writes_parquet_when_pyarrow_is_there(use_settings, monkeypatch: pytest.MonkeyPatch,
                                                     books_db: dict[str, Any], tmp_path: Path) -> None:
    import pandas as pd

    written: list[str] = []
    monkeypatch.setattr(bt, "_has_pyarrow", lambda: True)
    monkeypatch.setattr(pd.DataFrame, "to_parquet",
                        lambda self, path, index=False: (written.append(Path(path).name), Path(path).write_bytes(b"PAR1")))
    out = tmp_path / "export"
    assert cli.main(["export", "--parquet", str(out), "--db", books_db["path"], "--log-level", "WARNING"]) == 0
    assert sorted(written) == ["books.parquet", "candles.parquet", "trades.parquet"]
    assert '"format": "parquet"' in (out / "manifest.json").read_text()


def test_export_missing_db_exits_1(use_settings, tmp_path: Path, capsys) -> None:
    assert cli.main(["export", "--parquet", str(tmp_path / "x"), "--db", str(tmp_path / "none.sqlite"),
                     "--log-level", "WARNING"]) == cli.EXIT_FAILED
    assert "does not exist" in capsys.readouterr().err


# ---------------------------------------------------------------- replay wiring


def _stub_replay(monkeypatch: pytest.MonkeyPatch, ok: bool, calls: list[dict[str, Any]]) -> None:
    module = types.ModuleType("wta.replay.runner")

    def run_replay(cfg: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return types.SimpleNamespace(ok=ok, summary="3/3 decisions reproduced" if ok else "2/3 decisions reproduced")

    module.run_replay = run_replay  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wta.replay.runner", module)


def test_replay_wiring(use_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    calls: list[dict[str, Any]] = []
    _stub_replay(monkeypatch, True, calls)
    logs = tmp_path / "logs"
    logs.mkdir()
    code = cli.main(["replay", "--log-dir", str(logs), "--state-db", str(tmp_path / "s.sqlite"), "--out",
                     str(tmp_path / "rep"), "--reports-dir", str(tmp_path / "reports"), "--log-level", "WARNING"])
    assert code == 0
    (kw,) = calls
    assert kw["log_dir"] == logs and kw["state_db"] == tmp_path / "s.sqlite" and kw["out_dir"] == tmp_path / "rep"
    assert kw["reports_dir"] == str(tmp_path / "reports") and kw["strict"] is True
    assert "3/3 decisions reproduced (PASS)" in capsys.readouterr().out
    # defaults: the runner picks the state DB from the start event; --no-strict
    assert cli.main(["replay", "--log-dir", str(logs), "--no-strict", "--reports-dir", "", "--log-level",
                     "WARNING"]) == 0
    assert calls[-1]["state_db"] is None and calls[-1]["strict"] is False and calls[-1]["reports_dir"] is None


def test_replay_mismatch_exits_1(use_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    _stub_replay(monkeypatch, False, [])
    (tmp_path / "logs").mkdir()
    assert cli.main(["replay", "--log-dir", str(tmp_path / "logs"), "--log-level", "WARNING"]) == cli.EXIT_FAILED
    assert "(FAIL)" in capsys.readouterr().out


def test_replay_without_the_runner_exits_3(use_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           capsys) -> None:
    monkeypatch.setitem(sys.modules, "wta.replay.runner", None)  # import raises ImportError
    (tmp_path / "logs").mkdir()
    assert cli.main(["replay", "--log-dir", str(tmp_path / "logs"), "--log-level", "WARNING"]) == cli.EXIT_NOT_IMPLEMENTED
    assert "not available" in capsys.readouterr().out


def test_replay_missing_log_dir_exits_2(use_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_replay(monkeypatch, True, [])
    assert cli.main(["replay", "--log-dir", str(tmp_path / "nope"), "--log-level", "WARNING"]) == cli.EXIT_CANNOT_RUN
