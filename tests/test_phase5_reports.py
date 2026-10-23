"""PRD-05 §5 / §7: what ``wta report`` adds for a shadow/live run from its heartbeats and events.

* the S1 opportunity ticks per coin (the engine's own counters in ``health.extra.strategies.S1``),
  summed over engine processes (the counters restart with every process);
* the analyst's availability (cycles, cycles without a plan, failure streaks, the rules-only
  stretch after a stale plan), as a section and as a limitation line.

Hand-written log files in ``tmp_path``: no engine, no network, never the repo's ``logs/`` or ``data/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import backtest.runner as bt
from backtest.report import NOT_AVAILABLE, FinalSection, assemble_final_report

T = "2026-10-23T{}Z"


def _coin(evaluated: int, opp: int, best: str, last: float | None, why: str = "no lock-in: edge < cost") -> dict[str, Any]:
    return {"stats": {"evaluated": evaluated, "opportunity_ticks": opp, "best_excess_pct": best,
                      "last_opportunity_ts": last}, "why_no_trade": why}


def _hb(ts: str, coins: dict[str, Any], ai_stats: dict[str, int] | None = None, equity: str = "5000000") -> dict[str, Any]:
    extra: dict[str, Any] = {"strategies": {"S1": {"coins": coins}}}
    if ai_stats is not None:
        extra["ai"] = {"stats": ai_stats}
    return {"ts": T.format(ts), "equity_tmn": equity, "extra": extra}


def test_s1_opportunity_ticks_sum_over_engine_processes() -> None:
    health = [
        _hb("00:01:00.000", {"BTC": _coin(30, 1, "0.0004", 1792713660.0)}),
        _hb("00:02:00.000", {"BTC": _coin(60, 2, "0.0009", 1792713700.0)}),
        _hb("00:03:00.000", {"BTC": _coin(10, 0, "-0.0020", None)}),  # a restart: the counters start again
        _hb("00:04:00.000", {"BTC": _coin(40, 3, "0.0002", 1792713800.0, why="no lock-in: x; revert: y")}),
    ]
    got = bt.s1_opportunity_ticks(health)["BTC"]
    assert got["evaluated"] == 100 and got["opportunity_ticks"] == 5
    assert got["best_excess_pct"] == "0.0009"  # the best of both processes
    assert got["last_opportunity"] == "2026-10-23T00:03:20.000Z"
    assert got["why_no_trade"] == "no lock-in: x; revert: y"
    lines = "\n".join(bt.s1_opportunity_markdown({"BTC": got}, []))
    assert "| BTC | 100 | 5 | +0.090% | 2026-10-23T00:03:20.000Z | no lock-in: x |" in lines  # the lock-in clause
    assert "Total: 5 opportunity ticks in 100 coin-ticks evaluated" in lines


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _plan(ts: str, n: int) -> dict[str, Any]:
    return {"decision_id": f"d_plan_{n}", "kind": "plan", "strategy": "AI", "ts": T.format(ts), "mode": "shadow",
            "plan_snapshot": {"enabled_strategies": ["S2"], "prompt_hash": f"h{n}", "regimes": {},
                              "size_multiplier": "1", "ts": 0}}


def test_shadow_report_shows_s1_ticks_and_the_analyst_availability(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    start = {"kind": "start", "ts": T.format("00:00:00.000"), "reason": "engine started",
             "data": {"mode": "shadow", "feed": "synthetic", "synthetic": True, "strategies": ["S1", "S2"],
                      "ai": {"source": "llm"}, "config_hash": "abc", "git_commit": "deadbeef"}}
    events = [
        start,
        {"kind": "ai_degraded", "ts": T.format("00:20:00.000"),
         "reason": "analyst cycle failed: llm unavailable: openrouter: ProviderError: ConnectError"},
        {"kind": "plan_stale", "ts": T.format("00:40:00.000"), "reason": "plan is 1205s old"},
        {"kind": "stop", "ts": T.format("01:00:00.000"), "reason": "ran for 3600 s"},
    ]
    stats = {"cycles": 12, "plans": 4, "failures": 8, "model_calls": 9, "cached_calls": 0, "reviews": 1, "tokens": 999}
    coins = {"ETH": _coin(1800, 3, "0.0011", 1792714000.0), "SOL": _coin(1800, 0, "-0.0012", None)}
    health = [_hb(f"00:{m:02d}:00.000", coins, stats, equity=str(5_000_000 + m)) for m in (10, 30, 59)]
    _write(logs / "events-20261023.jsonl", events)
    _write(logs / "health-20261023.jsonl", health)
    _write(logs / "decisions-20261023.jsonl", [_plan(f"00:{m:02d}:00.000", i) for i, m in enumerate((0, 5, 10, 15))])

    res = bt.report_from_logs(source=logs, out_dir=tmp_path / "rep")
    text = res.report.read_text(encoding="utf-8")
    assert "## S1 opportunity ticks" in text and "| ETH | 1,800 | 3 | +0.110% |" in text
    assert "left S1 out of `enabled_strategies` in 4 of 4 plans" in text
    assert text.index("## S1 opportunity ticks") < text.index("## AI (Regime Analyst)")
    assert "### Analyst availability" in text and "| cycles without a plan | 8 |" in text
    assert text.index("### Analyst availability") < text.index("### Ablation (ai=on vs ai=off)")
    assert ("The LLM analyst gave no plan in 8 of 12 cycles (llm unavailable: provider unreachable from "
            "2026-10-23T00:20:00.000Z); the plan went stale at 2026-10-23T00:40:00.000Z and the strategies ran "
            "rules-only until the end of the window (2026-10-23T01:00:00.000Z, 0.33 h).") in text
    summary = json.loads((tmp_path / "rep" / "summary.json").read_text(encoding="utf-8"))
    assert summary["s1_opportunities"]["ETH"]["opportunity_ticks"] == 3
    assert summary["ai_availability"]["failures"] == 8
    assert summary["ai_availability"]["rules_only_stretches"][0]["until"] == "the end of the window"


def test_no_availability_section_without_ai(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    start = {"kind": "start", "ts": T.format("00:00:00.000"),
             "data": {"mode": "shadow", "feed": "synthetic", "synthetic": True, "strategies": ["S2"],
                      "ai": {"source": "disabled"}}}
    _write(logs / "events-20261023.jsonl", [start])
    _write(logs / "health-20261023.jsonl", [{"ts": T.format(f"00:{m:02d}:00.000"), "equity_tmn": "5000000",
                                              "extra": {}} for m in (1, 2, 3)])
    text = bt.report_from_logs(source=logs, out_dir=tmp_path / "rep").report.read_text(encoding="utf-8")
    assert "### Analyst availability" not in text and "## S1 opportunity ticks" not in text
    assert bt.ai_availability(bt.load_log_run(logs)) is None


def test_a_section_may_say_why_it_is_missing(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "survey_20261022.md").write_text("# Market survey\n\nbody\n", encoding="utf-8")
    live = FinalSection("live", "Live challenge run", ("live_*.md",), status_if_missing="no live run yet",
                        text_if_missing="**No live run yet** (live trading is started only by the operator).")
    text = assemble_final_report(reports, tmp_path / "deliverables" / "REPORT.md",
                                 sections=["survey", live, ("Replay proof", "replay_*.md")]).read_text()
    assert "| 2 | Live challenge run | no live run yet | — |" in text
    assert "**No live run yet** (live trading is started only by the operator)." in text
    assert f"**{NOT_AVAILABLE.capitalize()}:** Replay proof." in text  # only the unexplained gap is listed
    assert "[survey_20261022.md](../reports/survey_20261022.md)" in text
