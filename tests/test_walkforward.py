"""PRD-05 §3.4: the walk-forward grid, the 70/30 split, the choice and the accept/reject guard.

The rule tests are pure; the orchestration tests replace the backtest with a scripted runner
(returns chosen in-sample / out-of-sample returns per parameter value), and one test runs the real
runner end to end on a tiny synthetic candle dataset. No network, no wall clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backtest.metrics import Metrics
from backtest.runner import BacktestResult, make_synthetic_market_db
from backtest.walkforward import (
    MAX_VALUES,
    Point,
    accept_rule,
    arun_walkforward,
    choose,
    combos,
    default_params,
    parse_param_grid,
    split_window,
)
from wta.config import Config, Settings, get_path, load_config

KEY = "strategies.s2_usdtalloc.rebalance_threshold_pp"


@pytest.fixture
def cfg() -> Config:
    return load_config(env={})


# ---------------------------------------------------------------- grid parsing


def test_parse_param_grid(cfg: Config) -> None:
    grid = parse_param_grid(["strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015,0.0025"], cfg)
    assert grid == {"strategies.s1_crossbook.margin_min_pct": [0.0008, 0.0012, 0.0015, 0.0025]}
    two = parse_param_grid([f"{KEY}=0.1,0.2", "strategies.s2_usdtalloc.ema_fast=10,20"], cfg)
    assert len(combos(two)) == 4 and combos(two)[0] == {KEY: 0.1, "strategies.s2_usdtalloc.ema_fast": 10}
    assert parse_param_grid(None) == {}


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ([f"{KEY}=" + ",".join(str(i / 100) for i in range(1, MAX_VALUES + 2))], "at most 8"),
        ([f"{KEY}=0.1", "strategies.s2_usdtalloc.ema_fast=10", "strategies.s2_usdtalloc.ema_slow=50"], "at most 2"),
        (["strategies.s9.nothing=1,2"], "unknown config key"),
        ([f"{KEY}=abc"], "invalid"),
        ([f"{KEY}=0.1,0.1"], "duplicate"),
        ([f"{KEY}=0.1", f"{KEY}=0.2"], "given twice"),
        (["no-equals-sign"], "key=v1,v2"),
        ([f"{KEY}="], "no values"),
    ],
)
def test_parse_param_grid_refuses(cfg: Config, specs: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_param_grid(specs, cfg)


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    ("is_ret", "oos_ret", "accepted"),
    [
        (0.02, 0.012, True),  # -40 %: within the 50 % allowance
        (0.02, 0.010, True),  # exactly -50 %
        (0.02, 0.009, False),  # -55 %
        (0.02, 0.03, True),  # better out of sample
        (0.02, -0.001, False),  # negative while in-sample is positive
        (-0.01, -0.012, False),  # a non-positive in-sample result is never adopted (STRATEGY.md §10)
        (-0.01, -0.005, False),  # ... even when out-of-sample is better
        (0.0, 0.0, False),
        (0.0, -0.001, False),
        (None, 0.01, False),
        (0.01, None, False),
    ],
)
def test_accept_rule(is_ret: float | None, oos_ret: float | None, accepted: bool) -> None:
    ok, why = accept_rule(is_ret, oos_ret)
    assert ok is accepted and why


def test_accept_rule_per_day_of_data() -> None:
    # a steady strategy: the same rate per day in both halves (70 % / 30 % of 1 day of data)
    ok, why = accept_rule(0.007, 0.003, is_days=0.7, oos_days=0.3)
    assert ok and "per day" in why
    # the same totals compared as totals would be rejected (-57 %)
    assert accept_rule(0.007, 0.003)[0] is False
    # a real degradation per day still rejects: 1 %/day in-sample vs 0.3 %/day out-of-sample
    assert accept_rule(0.007, 0.0009, is_days=0.7, oos_days=0.3)[0] is False


def _point(ret: float | None, dd: float | None, *, default: bool = False, index: int = 0) -> Point:
    m = Metrics(return_pct=ret, max_drawdown_pct=dd)
    res = BacktestResult(metrics=m, paths={"out_dir": Path(".")})
    return Point({KEY: index}, default, index, is_result=res, oos_result=res)


def test_choose_by_in_sample_return_then_drawdown_then_default() -> None:
    a, b, c = _point(0.01, 0.02, index=1), _point(0.02, 0.03, index=2), _point(0.02, 0.01, index=3)
    assert choose([a, b, c]) is c  # tie on return -> lower drawdown
    d, e = _point(0.02, 0.01, index=4), _point(0.02, 0.01, default=True, index=5)
    assert choose([d, e]) is e  # full tie -> the default
    assert choose([_point(None, None, index=6), a]) is a  # no return ranks last


def test_split_window_by_data_ticks() -> None:
    steps = [float(t) for t in range(10)]
    (a0, a1), (b0, b1), cut = split_window(steps, -5.0, 100.0)
    assert (a0, a1, b0, b1, cut) == (-5.0, 6.0, 7.0, 100.0, 7.0)  # 7 ticks in-sample, 3 out
    (a0, a1), (b0, b1), cut = split_window([], 0.0, 10.0)
    assert (a1, b0) == (7.0, 7.0)  # no data: by time
    with pytest.raises(ValueError):
        split_window(steps, 0.0, 1.0, fraction=1.0)


# ---------------------------------------------------------------- orchestration (scripted runner)


@pytest.fixture(scope="module")
def candle_db(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_synthetic_market_db(tmp_path_factory.mktemp("wf") / "synth.sqlite", hours=2, seed=4, step_s=10,
                                    with_books=False)


class Scripted:
    """Returns IS/OOS returns per parameter value; the full-window run writes a minimal report."""

    def __init__(self, table: dict[float, tuple[float, float]], split_ts: list[float]) -> None:
        self.table = table
        self.calls: list[dict[str, Any]] = []
        self.split = split_ts

    async def __call__(self, cfg: Config, *, from_ts: float, to_ts: float, out_dir: Path, **kw: Any) -> BacktestResult:
        value = float(get_path(cfg.model_dump(mode="json"), KEY))
        self.calls.append({"value": value, "from": from_ts, "to": to_ts, "out": Path(out_dir), **kw})
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if "Walk-forward" not in str(kw.get("title") or ""):  # the final full-window run
            (out / "report.md").write_text("# Backtest\n\n## Metrics\n\nx\n\n## Limitations\n\n- y\n")
            (out / "summary.json").write_text(json.dumps({"title": "Backtest"}))
            m = Metrics(return_pct=0.0, max_drawdown_pct=0.0)
            return BacktestResult(metrics=m, paths={"out_dir": out, "report": out / "report.md",
                                                    "summary": out / "summary.json"})
        is_ret, oos_ret = self.table[value]
        oos = "out-of-sample" in str(kw.get("title"))
        m = Metrics(return_pct=oos_ret if oos else is_ret, max_drawdown_pct=0.001, n_trades=3)
        return BacktestResult(metrics=m, paths={"out_dir": out})


async def test_walkforward_accepts_a_value_that_holds_out_of_sample(tmp_path: Path, cfg: Config,
                                                                    candle_db: dict[str, Any]) -> None:
    assert default_params(cfg, [KEY]) == {KEY: "0.15"}  # Decimals are dumped as text
    runner = Scripted({0.15: (0.001, 0.001), 0.1: (0.004, 0.003), 0.2: (0.002, 0.002)}, [])
    out = tmp_path / "wf"
    res = await arun_walkforward(cfg, grid={KEY: [0.1, 0.2]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=out, strategies="S2",
                                 market_db=candle_db["path"], runner=runner, settings=Settings(_env_file=None))
    # the default (0.15) is a candidate next to the grid: 3 candidates x 2 halves + the final run
    assert len(runner.calls) == 7
    assert [p.label for p in res.points] == [
        "rebalance_threshold_pp=0.15 (default)", "rebalance_threshold_pp=0.1", "rebalance_threshold_pp=0.2"]
    assert res.chosen.params == {KEY: 0.1} and res.accepted is True and res.final_params == {KEY: 0.1}
    final = runner.calls[-1]
    assert final["value"] == 0.1 and final["from"] == candle_db["from_ts"] and final["to"] == candle_db["to_ts"]
    assert any("grid value accepted" in x for x in final["limitations"])
    # IS = the first 70 % of the data ticks, OOS = the rest, no overlap
    is_call, oos_call = runner.calls[0], runner.calls[1]
    assert is_call["from"] == candle_db["from_ts"] and is_call["to"] < oos_call["from"]
    assert oos_call["to"] == candle_db["to_ts"]
    span = candle_db["to_ts"] - candle_db["from_ts"]
    assert is_call["to"] - candle_db["from_ts"] == pytest.approx(0.7 * span, abs=120)
    md = (out / "walkforward.md").read_text()
    assert "**Decision: ACCEPTED.**" in md and "| rebalance_threshold_pp=0.1 |" in md and "chosen" in md
    assert "SYNTHETIC" in md
    report = (out / "report.md").read_text()
    assert report.index("## Walk-forward (parameter grid)") < report.index("## Limitations")
    summary = json.loads((out / "summary.json").read_text())
    assert summary["walkforward"]["accepted"] is True and summary["walkforward"]["final_params"] == {KEY: 0.1}
    assert json.loads((out / "walkforward.json").read_text())["chosen"] == {KEY: 0.1}


async def test_walkforward_rejects_a_value_that_degrades_and_keeps_the_default(
        tmp_path: Path, cfg: Config, candle_db: dict[str, Any]) -> None:
    # per day of data: IS 0.004 over 70 % of the ticks vs OOS 0.0005 over 30 % -> -71 % per day
    runner = Scripted({0.15: (0.001, 0.001), 0.1: (0.004, 0.0005), 0.2: (0.002, 0.002)}, [])
    res = await arun_walkforward(cfg, grid={KEY: [0.1, 0.2]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=tmp_path / "wf", strategies="S2",
                                 market_db=candle_db["path"], runner=runner)
    assert res.chosen.params == {KEY: 0.1} and res.accepted is False
    assert res.final_params == {KEY: "0.15"} and "pre-grid default is kept" in res.reason
    assert runner.calls[-1]["value"] == 0.15
    assert "**Decision: REJECTED.**" in (tmp_path / "wf" / "walkforward.md").read_text()


async def test_walkforward_default_wins_in_sample(tmp_path: Path, cfg: Config, candle_db: dict[str, Any]) -> None:
    runner = Scripted({0.15: (0.005, 0.0), 0.1: (0.004, 0.004)}, [])
    res = await arun_walkforward(cfg, grid={KEY: [0.15, 0.1]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=tmp_path / "wf", strategies="S2",
                                 market_db=candle_db["path"], runner=runner)
    assert len(res.points) == 2 and res.points[0].is_default  # 0.15 in the grid is the default itself
    assert res.chosen.is_default and res.final_params == {KEY: "0.15"}
    assert "DEFAULT KEPT" in (tmp_path / "wf" / "walkforward.md").read_text()


# ---------------------------------------------------------------- end to end with the real runner


async def test_walkforward_end_to_end_real_runner(tmp_path: Path, cfg: Config, candle_db: dict[str, Any]) -> None:
    out = tmp_path / "wf"
    res = await arun_walkforward(cfg, grid={KEY: [0.1, 0.2]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=out, strategies="S2",
                                 market_db=candle_db["path"], settings=Settings(_env_file=None))
    assert len(res.points) == 3
    for p in res.points:
        assert p.is_result is not None and p.oos_result is not None
        assert (p.is_result.out_dir / "report.md").is_file() and (p.oos_result.out_dir / "report.md").is_file()
        assert p.is_result.meta["synthetic"] is True
    assert res.final is not None and res.final.out_dir == out
    report = (out / "report.md").read_text()
    assert "## Walk-forward (parameter grid)" in report and "SYNTHETIC DATA" in report
    summary = json.loads((out / "summary.json").read_text())
    assert summary["walkforward"]["final_params"] == res.final_params
    # the final run's decisions carry the config hash of the final parameters
    hashes = {json.loads(line)["config_hash"] for line in (out / "decisions.jsonl").read_text().splitlines()
              if '"config_hash"' in line}
    assert hashes == {res.final.meta["config_hash"]}


async def test_every_walkforward_run_ends_under_the_same_rule(tmp_path: Path, cfg: Config,
                                                              candle_db: dict[str, Any]) -> None:
    """The runner's automatic end rule (flatten in windows >= 8 h) would flatten only the longer
    in-sample half of a 24-h grid: every half and the final run must get the same flatten_at_end."""
    runner = Scripted({0.15: (0.001, 0.001), 0.1: (0.004, 0.003)}, [])
    res = await arun_walkforward(cfg, grid={KEY: [0.1]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=tmp_path / "wf", strategies="S2",
                                 market_db=candle_db["path"], runner=runner, flatten_at_end=None)
    assert [c["flatten_at_end"] for c in runner.calls] == [False] * len(runner.calls) == [False] * 5
    assert res.flatten_at_end is False and res.to_dict()["flatten_at_end"] is False
    md = (tmp_path / "wf" / "walkforward.md").read_text()
    assert "no end-of-window flatten; holdings at the end are valued at the bid" in md

    runner = Scripted({0.15: (0.001, 0.001), 0.1: (0.004, 0.003)}, [])
    res = await arun_walkforward(cfg, grid={KEY: [0.1]}, driver="candles", from_ts=candle_db["from_ts"],
                                 to_ts=candle_db["to_ts"], out_dir=tmp_path / "wf2", strategies="S2",
                                 market_db=candle_db["path"], runner=runner, flatten_at_end=True)
    assert {c["flatten_at_end"] for c in runner.calls} == {True} and res.flatten_at_end is True
    assert "the deadline flatten in its last 2 h" in (tmp_path / "wf2" / "walkforward.md").read_text()
