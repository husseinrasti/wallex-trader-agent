"""compare_decisions / ReplayReport (PRD-05 §6): decision-by-decision replay verdict.

Both sides are written by the real ``DecisionLog``. The replayed side runs on a clock half a second
later (every ``ts`` differs), in mode ``replay`` and with cached plans, none of which may count as a
difference. Mutations of the deterministic fields must come back as exact rows.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wta.logging_.decision_log import DecisionLog, merge_decisions
from wta.logging_.jsonl import iter_family
from wta.models import Intent, MarketPlan, OrderRequest, RegimeCall, RiskCheck, RiskDecision
from wta.replay import FIELDS, IGNORE, ReplayReport, compare_decisions, report_path
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock

from .conftest import T0

ORDER_ID = "d_1792663200000_S3_BTCUSDT_1"
VETO_ID = "d_1792663202000_S3_ETHUSDT_2"
PLAN_ID = "plan_1792663200000"
GIT = "b943545cdad0bcd2be5caf048f059ae6b2c89df4"

Spec = dict[str, Any]


def specs(now: float) -> list[Spec]:
    """A plan, an accepted order (plus its outcome) and a veto, as DecisionLog calls."""
    plan = MarketPlan(
        ts=now,
        regimes={"BTCUSDT": RegimeCall(regime="trend_up", confidence=0.7, evidence="higher highs")},
        enabled_strategies=["S2", "S3"],
        prompt_hash="ph_0123456789ab",
        model="test-model",
        latency_ms=1234,
    ).model_dump(mode="json")
    intent = Intent(
        id=ORDER_ID,
        ts=now,
        strategy="S3",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=D("0.5"),
        stop_price=D("95"),
        purpose="entry",
        reason="S3 breakout: close above the 20-bar high",
        features={"atr_pct": "0.004", "seen_ts": now},
        expected_edge_pct=D("0.05"),
    )
    risk = RiskDecision(
        approved=True,
        adjusted_qty=D("0.500000"),
        checks=[
            RiskCheck(
                code="MIN_NOTIONAL", passed=True, detail="qty 0.500000 x 100.01 = 50.0050 USDT >= min 1 USDT"
            ),
            RiskCheck(code="EDGE_VS_COST", passed=True, detail="edge 5.000% vs cost 0.337% = 0.337%"),
            RiskCheck(code="BALANCE", passed=True, detail="needs 50.1000 USDT <= free 497.5000 USDT"),
            RiskCheck(
                code="MIN_NOTIONAL", passed=True, detail="after adjustments: 50.0050 USDT >= min 1 USDT"
            ),
        ],
    )
    req = OrderRequest(
        client_id="S3_BTCUSDT_1792663200000_B483",
        symbol="BTCUSDT",
        side="BUY",
        type="LIMIT",
        qty=D("0.500000"),
        price=D("100.01"),
        strategy="S3",
        purpose="entry",
        decision_id=ORDER_ID,
    )
    order = {
        "request": req.model_dump(mode="json"),
        "accepted": True,
        "response": {
            "exchange_id": "paper-o-1",
            "status": "NEW",
            "filled_qty": "0",
            "avg_price": None,
            "fee_paid": "0",
            "fee_asset": None,
            "created_ts": now,
            "updated_ts": now,
            "reprices": 0,
        },
    }
    veto_intent = Intent(
        id=VETO_ID,
        ts=now + 2,
        strategy="S3",
        symbol="ETHUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=D("0.01"),
        purpose="entry",
        reason="S3 breakout on ETH",
        expected_edge_pct=D("0.001"),
    )
    veto_risk = RiskDecision(
        approved=False,
        adjusted_qty=D("0.0100"),
        veto_reason="MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1",
        checks=[
            RiskCheck(
                code="MAX_POSITIONS",
                passed=False,
                detail="S3 already has 1 positions (incl. pending) >= its cap 1",
            ),
            RiskCheck(
                code="EDGE_VS_COST", passed=False, detail="edge 0.100% vs cost 0.310% | edge too small"
            ),
        ],
    )
    return [
        {
            "fn": "record",
            "at": 0,
            "kind": "plan",
            "decision_id": PLAN_ID,
            "strategy": "AI",
            "symbol": "*",
            "plan_snapshot": plan,
        },
        {
            "fn": "record",
            "at": 0,
            "kind": "order",
            "decision_id": ORDER_ID,
            "strategy": "S3",
            "symbol": "BTCUSDT",
            "features": dict(intent.features),
            "plan_snapshot": plan,
            "intent": intent.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "order": order,
            "notes": "",
        },
        {
            "fn": "record",
            "at": 2,
            "kind": "veto",
            "decision_id": VETO_ID,
            "strategy": "S3",
            "symbol": "ETHUSDT",
            "intent": veto_intent.model_dump(mode="json"),
            "risk": veto_risk.model_dump(mode="json"),
            "notes": veto_risk.veto_reason,
        },
        {
            "fn": "outcome",
            "at": 6,
            "decision_id": ORDER_ID,
            "outcome": {
                "kind": "order",
                "status": "FILLED",
                "done_reason": "FILLED",
                "filled_qty": "0.500000",
                "avg_price": "100.01",
                "client_ids": [req.client_id],
                "done_ts": now + 6,
            },
        },
    ]


def write_run(
    log_dir: Path,
    *,
    offset: float = 0.0,
    mode: str = "shadow",
    git: str = GIT,
    mutate: Callable[[list[Spec]], None] | None = None,
    latency_ms: int | None = 3,
) -> Path:
    start = T0 + offset
    clock = SimClock(start)
    dlog = DecisionLog(log_dir, mode, git, "01d6ebc6184d", clock)
    items = specs(start)
    if mode == "replay":  # served by the LLM cache
        for s in items:
            if s.get("plan_snapshot"):
                s["plan_snapshot"] = {**s["plan_snapshot"], "cached": True}
                if latency_ms is not None:
                    s["plan_snapshot"]["latency_ms"] = latency_ms
    if mutate is not None:
        mutate(items)
    for s in items:
        s = copy.deepcopy(s)
        clock.set(start + s.pop("at"))
        if s.pop("fn") == "outcome":
            dlog.outcome(s["decision_id"], s["outcome"])
        else:
            dlog.record(**s)
    dlog.close()
    return log_dir


def by_id(items: list[Spec], decision_id: str, fn: str = "record") -> Spec:
    return next(s for s in items if s["decision_id"] == decision_id and s["fn"] == fn)


def compare(tmp_path: Path, mutate: Callable[[list[Spec]], None] | None = None, **kw: Any) -> ReplayReport:
    ref = write_run(tmp_path / "ref")
    rep = write_run(tmp_path / "rep", offset=0.5, mode="replay", mutate=mutate)
    return compare_decisions(ref, rep, **kw)


def rows(report: ReplayReport) -> list[tuple[str, str, Any, Any]]:
    return [(m.decision_id, m.field, m.reference, m.replayed) for m in report.mismatches]


# ---------------------------------------------------------------- identical runs


def test_identical_runs_reproduce_every_decision_and_ignore_time_mode_and_cache(tmp_path: Path) -> None:
    report = compare(tmp_path)  # the replay runs 0.5 s later: the PRD field list has no time stamp
    assert report.ok and rows(report) == []
    assert (report.n_reference, report.n_replayed, report.n_matched) == (3, 3, 3)
    assert report.summary == "3/3 decisions reproduced"
    assert report.missing == [] and report.extra == [] and report.aligned_by_order == []
    assert report.provenance["git_commit"] == {"reference": [GIT], "replayed": [GIT], "equal": True}
    assert report.provenance["config_hash"]["equal"] is True
    assert report.provenance["mode"] == {"reference": ["shadow"], "replayed": ["replay"], "equal": False}
    # the strict pass compares time stamps and durations too: the replay clock is set to the recorded
    # tick times, so a decision, fill or close at another time is a difference
    ref = write_run(tmp_path / "s_ref")
    same = write_run(tmp_path / "s_same", mode="replay", latency_ms=None)  # only mode + cached differ
    assert compare_decisions(ref, same, strict=True).ok
    shifted = compare_decisions(ref, write_run(tmp_path / "s_late", offset=0.5, mode="replay"), strict=True)
    fields = {m.field for m in shifted.mismatches}
    assert not shifted.ok
    assert {"ts", "intent.ts", "outcome.done_ts", "plan_snapshot.latency_ms"} <= fields, sorted(fields)
    assert not any(f in ("mode", "plan_snapshot.cached") for f in fields)


def test_inputs_may_be_a_directory_raw_records_or_merged_records(tmp_path: Path) -> None:
    ref = write_run(tmp_path / "ref")
    rep = write_run(tmp_path / "rep", offset=0.5, mode="replay")
    raw = list(iter_family(rep, "decisions"))
    merged = merge_decisions(iter_family(ref, "decisions"))
    for a, b in ((ref, rep), (ref, raw), (merged, raw), (dict(merged), rep)):
        assert compare_decisions(a, b).summary == "3/3 decisions reproduced"


# ---------------------------------------------------------------- mutations -> exact rows


def test_a_changed_reason_is_one_exact_row(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        by_id(items, ORDER_ID)["intent"]["reason"] = "S3 breakout: close above the 50-bar high"

    report = compare(tmp_path, mutate)
    assert not report.ok and report.summary == "2/3 decisions reproduced"
    assert rows(report) == [
        (
            ORDER_ID,
            "intent.reason",
            "S3 breakout: close above the 20-bar high",
            "S3 breakout: close above the 50-bar high",
        )
    ]


def test_quantities_compare_as_decimals(tmp_path: Path) -> None:
    def same_value(items: list[Spec]) -> None:
        by_id(items, ORDER_ID)["risk"]["adjusted_qty"] = "0.5"  # "0.500000" in the reference

    assert compare(tmp_path / "a", same_value).ok

    def smaller(items: list[Spec]) -> None:
        spec = by_id(items, ORDER_ID)
        spec["risk"]["adjusted_qty"] = "0.400000"
        spec["order"]["request"]["qty"] = "0.400000"

    assert rows(compare(tmp_path / "b", smaller)) == [
        (ORDER_ID, "risk.adjusted_qty", "0.500000", "0.400000"),
        (ORDER_ID, "order.request.qty", "0.500000", "0.400000"),
    ]


def test_risk_checks_are_compared_by_code_passed_and_detail(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        checks = by_id(items, ORDER_ID)["risk"]["checks"]
        checks[1]["detail"] = "edge 5.000% vs cost 0.400% = 0.400%"
        checks[3]["detail"] = "after adjustments: 40.0040 USDT >= min 1 USDT"
        veto_checks = by_id(items, VETO_ID)["risk"]["checks"]
        veto_checks[1]["passed"] = True
        del by_id(items, ORDER_ID)["risk"]["checks"][2]  # BALANCE

    assert rows(compare(tmp_path, mutate)) == [
        (
            ORDER_ID,
            "risk.checks[EDGE_VS_COST].detail",
            "edge 5.000% vs cost 0.337% = 0.337%",
            "edge 5.000% vs cost 0.400% = 0.400%",
        ),
        (
            ORDER_ID,
            "risk.checks[BALANCE]",
            ["BALANCE", True, "needs 50.1000 USDT <= free 497.5000 USDT"],
            None,
        ),
        (
            ORDER_ID,
            "risk.checks[MIN_NOTIONAL#2].detail",
            "after adjustments: 50.0050 USDT >= min 1 USDT",
            "after adjustments: 40.0040 USDT >= min 1 USDT",
        ),
        (VETO_ID, "risk.checks[EDGE_VS_COST].passed", False, True),
    ]


def test_a_changed_plan_prompt_hash_is_reported_on_every_record_that_carries_it(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        for s in items:
            if s.get("plan_snapshot"):
                s["plan_snapshot"]["prompt_hash"] = "ph_ffffffffffff"

    assert rows(compare(tmp_path, mutate)) == [
        (PLAN_ID, "plan.prompt_hash", "ph_0123456789ab", "ph_ffffffffffff"),
        (ORDER_ID, "plan.prompt_hash", "ph_0123456789ab", "ph_ffffffffffff"),
    ]


def test_order_status_answer_and_outcome(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        by_id(items, ORDER_ID)["order"]["response"]["status"] = "PARTIALLY_FILLED"
        out = by_id(items, ORDER_ID, "outcome")["outcome"]
        out["status"], out["done_reason"] = "UNFILLED", "EXPIRED_UNFILLED"
        by_id(items, VETO_ID)["notes"] = "REJECTED: replay_divergence S3_ETHUSDT_... was never placed"

    assert rows(compare(tmp_path, mutate)) == [
        (ORDER_ID, "order.response.status", "NEW", "PARTIALLY_FILLED"),
        (ORDER_ID, "outcome.status", "FILLED", "UNFILLED"),
        (ORDER_ID, "outcome.done_reason", "FILLED", "EXPIRED_UNFILLED"),
        (
            VETO_ID,
            "notes",
            "MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1",
            "REJECTED: replay_divergence S3_ETHUSDT_... was never placed",
        ),
    ]


def test_strict_mode_compares_every_other_field(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        spec = by_id(items, ORDER_ID)
        spec["features"]["atr_pct"] = "0.005"
        spec["intent"]["features"]["atr_pct"] = "0.005"

    assert compare(tmp_path / "default", mutate).ok  # features are not a PRD-05 field
    ref = write_run(tmp_path / "strict" / "ref")  # same clock: only the mutated fields differ
    strict = compare_decisions(ref, write_run(tmp_path / "strict" / "rep", mode="replay", mutate=mutate,
                                              latency_ms=None), strict=True)
    assert rows(strict) == [
        (ORDER_ID, "features.atr_pct", "0.004", "0.005"),
        (ORDER_ID, "intent.features.atr_pct", "0.004", "0.005"),
    ]


# ---------------------------------------------------------------- alignment


def test_missing_extra_and_alignment_by_order(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        veto = by_id(items, VETO_ID)
        veto["decision_id"] = "d_1792663204000_S3_ETHUSDT_2"  # same kind/strategy/symbol, other tick
        veto["intent"]["id"] = veto["decision_id"]
        items.remove(by_id(items, PLAN_ID))
        items.append(
            {
                "fn": "record",
                "at": 8,
                "kind": "order",
                "decision_id": "d_1792663208000_S2_USDTTMN_3",
                "strategy": "S2",
                "symbol": "USDTTMN",
                "notes": "REFUSED: no USDTTMN book to price the order",
            }
        )

    report = compare(tmp_path, mutate)
    assert not report.ok and report.summary == "1/3 decisions reproduced"
    assert report.missing == [{"decision_id": PLAN_ID, "kind": "plan", "strategy": "AI", "symbol": "*"}]
    assert report.extra == [
        {
            "decision_id": "d_1792663208000_S2_USDTTMN_3",
            "kind": "order",
            "strategy": "S2",
            "symbol": "USDTTMN",
        }
    ]
    assert report.aligned_by_order == [(VETO_ID, "d_1792663204000_S3_ETHUSDT_2")]
    assert rows(report) == [(VETO_ID, "decision_id", VETO_ID, "d_1792663204000_S3_ETHUSDT_2")]


def test_provenance_differences_and_exchange_divergences(tmp_path: Path) -> None:
    ref = write_run(tmp_path / "ref")
    rep = write_run(tmp_path / "rep", offset=0.5, mode="replay", git="c" * 40)
    report = compare_decisions(ref, rep)
    assert report.ok and report.provenance["git_commit"]["equal"] is False  # provenance, not a mismatch
    div = {
        "kind": "not_replayed",
        "client_id": "S3_BTCUSDT_1792663216000_E735",
        "decision_id": ORDER_ID,
        "message": "recorded placement S3_BTCUSDT_1792663216000_E735 (BUY 0.3 BTCUSDT) was not replayed",
    }
    failed = compare_decisions(ref, rep, divergences=[div])
    assert not failed.ok and failed.summary == "3/3 decisions reproduced"
    assert failed.to_dict()["divergences"] == [div]


def test_orphan_outcomes_are_counted(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        items.append({"fn": "outcome", "at": 9, "decision_id": "d_unknown", "outcome": {"status": "FILLED"}})

    report = compare(tmp_path, mutate)
    assert report.orphans == {"reference": 0, "replayed": 1} and report.ok


# ---------------------------------------------------------------- markdown


def test_markdown_report(tmp_path: Path) -> None:
    def mutate(items: list[Spec]) -> None:
        by_id(items, VETO_ID)["risk"]["checks"][1]["detail"] = "edge 0.100% vs cost 0.999% | edge too small"

    report = compare(tmp_path, mutate)
    md = report.to_markdown(date="20261022")
    assert md == report.to_markdown(date="20261022")  # deterministic
    assert md.startswith("# Replay report 20261022\n\n**2/3 decisions reproduced** (FAIL)\n")
    assert (
        f"| `{VETO_ID}` | `risk.checks[EDGE_VS_COST].detail` | edge 0.100% vs cost 0.310% \\| edge too small"
        " | edge 0.100% vs cost 0.999% \\| edge too small |"
    ) in md
    assert f"| `git_commit` | {GIT} | {GIT} | yes |" in md
    assert "## Missing (recorded, not replayed)\n\nNone." in md
    assert "None: every exchange answer came from the record." in md
    for f in ("intent.reason", "risk.adjusted_qty", "plan.prompt_hash"):
        assert f"`{f}`" in md
    path = report_path(tmp_path / "docs" / "REPORTS", "20261022")
    assert path.name == "replay_20261022.md"
    assert report.write_markdown(path, date="20261022").read_text(encoding="utf-8") == md
    ok = compare(tmp_path / "ok").to_markdown()
    assert (
        ok.startswith("# Replay report\n\n**3/3 decisions reproduced** (PASS)")
        and "## Mismatches\n\nNone." in ok
    )


def test_documented_field_and_ignore_sets() -> None:
    for name in (
        "kind",
        "strategy",
        "symbol",
        "intent.side",
        "intent.purpose",
        "risk.adjusted_qty",
        "order.request.price",
        "intent.reason",
        "risk.checks",
        "plan.prompt_hash",
        "order.response.status",
    ):
        assert name in FIELDS
    assert {"mode", "git_commit", "config_hash", "plan_snapshot.cached"} <= IGNORE
    assert not any("ts" == f.rsplit(".", 1)[-1] for f in FIELDS)
