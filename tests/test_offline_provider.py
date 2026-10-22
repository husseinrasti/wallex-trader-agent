"""Offline rules provider (NOT an LLM): tool round(s) -> plan JSON -> validation -> cache -> replay."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.llm_client import LLMClient, LLMResponse, Route, extract_json
from wta.ai.offline_provider import (
    CANDLE_STATS_BARS,
    OFFLINE_LABEL,
    OFFLINE_MODEL,
    OFFLINE_PROVIDER,
    OFFLINE_STATUS,
    REGIME_SYMBOLS,
    OfflineRulesProvider,
    ai_status_label,
    has_llm_key,
    is_offline_model,
    make_offline_llm,
    use_offline_provider,
)
from wta.ai.tools import (
    INTERNAL_TOOL_SCHEMAS,
    LOCAL_TOOL_SCHEMAS,
    InternalTools,
    ToolDeps,
    feature_view,
    truncate,
)
from wta.config import Config, Settings, apply_changes, canonical_json
from wta.features.engine import FeatureSnapshot
from wta.models import MarketPlan
from wta.strategies.s2_usdtalloc import rule_regime
from wta.utils.timeutil import SimClock

PROMPTS = Path(__file__).resolve().parents[1] / "src" / "wta" / "ai" / "prompts"
ANALYST_SYSTEM = (PROMPTS / "analyst.md").read_text(encoding="utf-8")
REVIEWER_SYSTEM = (PROMPTS / "reviewer.md").read_text(encoding="utf-8")
TOOLS = INTERNAL_TOOL_SCHEMAS + LOCAL_TOOL_SCHEMAS


# ---------------------------------------------------------------- the analyst's expected output shape
# (minimal local copy of PRD-04 §6 PlanOutput; the real one lives in wta/ai/plan_schema.py)


class _RegimeShape(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regime: Literal["range", "trend_up", "trend_down", "chaos"]
    confidence: float = Field(ge=0, le=1)
    evidence: str


class PlanShape(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regimes: dict[str, _RegimeShape]
    enabled_strategies: list[str]
    s1_margin_min_pct: float
    size_multiplier: float = Field(ge=0, le=1)
    usdt_target_share: float = Field(ge=0, le=1)
    risk_flags: list[str]
    notes: str


# ---------------------------------------------------------------- helpers


def feat(trend: int = 0, r24: str = "0.001", gap: str = "0.0005", atr: str = "0.004", med: str = "0.004",
         age: str = "1") -> dict[str, Any]:
    """One symbol's features as ``FeatureSnapshot.compact`` renders them (Decimals as strings)."""
    return {"trend_flag": trend, "ret_24h": r24, "ema_gap_pct": gap, "atr_15m_pct": atr,
            "atr_15m_pct_median_24h": med, "book_age_s": age, "spread_pct": "0.001", "bars_15m": 100}


def calm() -> dict[str, dict[str, Any]]:
    return {s: feat() for s in REGIME_SYMBOLS}


def features_payload(per_symbol: dict[str, dict[str, Any]], t0: float) -> dict[str, Any]:
    return {"ts": t0, "symbols": per_symbol, "cross": {}, "dollar": per_symbol.get("USDTTMN", {})}


def base_messages() -> list[dict[str, Any]]:
    return [{"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": "Compact state: (see tools)\nDecide the plan."}]


def with_tool_results(results: dict[str, Any]) -> list[dict[str, Any]]:
    """base messages + one assistant tool-call turn + one tool message per (name -> result)."""
    msgs = base_messages()
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"offline_{name}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for name in results]})
    for name, result in results.items():
        msgs.append({"role": "tool", "tool_call_id": f"offline_{name}", "name": name,
                     "content": json.dumps(result, sort_keys=True)})
    return msgs


def plan_from(provider: OfflineRulesProvider, results: dict[str, Any]) -> dict[str, Any]:
    resp = provider.respond(with_tool_results(results), TOOLS)
    assert resp.tool_calls == []
    return extract_json(resp.text)


@pytest.fixture
def provider(cfg: Config) -> OfflineRulesProvider:
    return OfflineRulesProvider(cfg)


# ---------------------------------------------------------------- tool round


def test_first_call_requests_features_and_pnl(provider: OfflineRulesProvider) -> None:
    resp = provider.respond(base_messages(), TOOLS)
    assert [c.name for c in resp.tool_calls] == ["get_features", "get_pnl_summary"]
    assert resp.tool_calls[0].arguments == {"symbols": list(REGIME_SYMBOLS)}
    assert resp.tool_calls[1].arguments == {}
    assert resp.text.startswith(OFFLINE_LABEL)
    assert (resp.model, resp.provider, resp.latency_ms, resp.usage) == (OFFLINE_MODEL, "offline", 0, {})


def test_only_offered_tools_are_requested(provider: OfflineRulesProvider) -> None:
    pnl_only = [s for s in TOOLS if s["name"] == "get_pnl_summary"]
    resp = provider.respond(base_messages(), pnl_only)
    assert [c.name for c in resp.tool_calls] == ["get_pnl_summary"]
    # OpenAI-wrapped schemas are understood too
    wrapped = [{"type": "function", "function": s} for s in TOOLS]
    assert [c.name for c in provider.respond(base_messages(), wrapped).tool_calls] == ["get_features",
                                                                                       "get_pnl_summary"]


def test_no_tools_offered_answers_directly_and_conservatively(provider: OfflineRulesProvider) -> None:
    resp = provider.respond(base_messages(), None)
    assert resp.tool_calls == []
    plan = PlanShape.model_validate(extract_json(resp.text))
    assert all(r.regime == "range" and r.confidence == 0.0 for r in plan.regimes.values())
    assert "missing_features" in plan.risk_flags
    assert plan.notes.startswith(OFFLINE_LABEL)


def test_features_in_user_state_are_used_without_tools(provider: OfflineRulesProvider, t0: float) -> None:
    per = calm()
    per["BTCUSDT"] = feat(trend=1, r24="0.02", gap="0.004")
    state = {"features": features_payload(per, t0), "pnl": {"drawdown_pct": "0.001"}}
    msgs = [{"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": "State:\n```json\n" + json.dumps(state) + "\n```\nDecide the plan."}]
    plan = extract_json(provider.respond(msgs, None).text)
    assert plan["regimes"]["BTCUSDT"]["regime"] == "trend_up"
    assert "missing_features" not in plan["risk_flags"]


# ---------------------------------------------------------------- the plan


def test_plan_after_tool_results_has_the_analyst_shape(provider: OfflineRulesProvider, cfg: Config,
                                                       t0: float) -> None:
    per = calm()
    per["BTCUSDT"] = feat(trend=1, r24="0.02", gap="0.004")
    plan = plan_from(provider, {"get_features": features_payload(per, t0),
                                "get_pnl_summary": {"equity_tmn": "5000000", "drawdown_pct": "0.004"}})
    shaped = PlanShape.model_validate(plan)
    assert sorted(shaped.regimes) == sorted(REGIME_SYMBOLS)
    assert shaped.regimes["BTCUSDT"].regime == "trend_up"
    assert shaped.regimes["BTCUSDT"].confidence == pytest.approx(0.71)
    assert shaped.regimes["USDTTMN"].regime == "range"
    assert "[features]" in shaped.regimes["BTCUSDT"].evidence
    assert shaped.enabled_strategies == ["S1", "S2"]
    assert shaped.size_multiplier == 1.0
    assert shaped.usdt_target_share == float(cfg.strategies.s2_usdtalloc.target_share_by_regime["range"])
    assert shaped.s1_margin_min_pct == float(cfg.strategies.s1_crossbook.margin_min_pct)
    assert shaped.risk_flags == []
    # ... and it is a valid engine MarketPlan once the analyst stamps ts/model on it
    mp = MarketPlan.model_validate({"ts": t0, **plan, "model": OFFLINE_MODEL})
    assert mp.s1_margin_min_pct == Decimal("0.0015")
    assert mp.usdt_target_share == Decimal("0.5")


def test_plan_validates_against_real_plan_schema_when_present(provider: OfflineRulesProvider, t0: float) -> None:
    mod = pytest.importorskip("wta.ai.plan_schema")
    plan_output = getattr(mod, "PlanOutput", None)
    if plan_output is None:
        pytest.skip("wta.ai.plan_schema has no PlanOutput")
    plan_output.model_validate(plan_from(provider, {"get_features": features_payload(calm(), t0)}))


def test_notes_are_labelled_and_short(provider: OfflineRulesProvider, t0: float) -> None:
    notes = plan_from(provider, {"get_features": features_payload(calm(), t0)})["notes"]
    assert notes.startswith("[offline rules provider — not an LLM] ")
    sentences = [s for s in notes.removeprefix(OFFLINE_LABEL).split(". ") if s.strip()]
    assert 2 <= len(sentences) <= 4
    assert "not an LLM" in notes


def test_regimes_use_the_s2_rule_regime(provider: OfflineRulesProvider, cfg: Config, t0: float) -> None:
    per = {
        "BTCUSDT": feat(trend=1, r24="0.02", gap="0.004"),
        "ETHUSDT": feat(trend=-1, r24="-0.03", gap="-0.009"),
        "SOLUSDT": feat(trend=1, r24="0.001", gap="0.002"),  # trend flag but small 24h move -> range
        "USDTTMN": feat(atr="0.02", med="0.004"),  # ATR 5x its median -> chaos
    }
    plan = plan_from(provider, {"get_features": features_payload(per, t0)})
    for sym, f in per.items():
        inputs = {k: (Decimal(v) if isinstance(v, str) else v) for k, v in f.items()}
        expected, evidence = rule_regime(inputs, cfg.strategies.s2_usdtalloc)
        assert plan["regimes"][sym]["regime"] == expected
        assert plan["regimes"][sym]["evidence"].startswith(evidence)
    assert [plan["regimes"][s]["regime"] for s in REGIME_SYMBOLS] == ["trend_up", "trend_down", "range", "chaos"]


def test_confidence_grows_with_the_ema_gap(provider: OfflineRulesProvider, t0: float) -> None:
    def conf(gap: str) -> float:
        per = calm()
        per["ETHUSDT"] = feat(trend=-1, r24="-0.03", gap=gap)
        return plan_from(provider, {"get_features": features_payload(per, t0)})["regimes"]["ETHUSDT"]["confidence"]

    assert 0.5 <= conf("-0.002") < conf("-0.005") < conf("-0.02") == 0.9


def test_chaos_sets_size_zero(provider: OfflineRulesProvider, cfg: Config, t0: float) -> None:
    per = calm()
    per["USDTTMN"] = feat(atr="0.012", med="0.004")  # 3x the median > chaos_atr_mult (2x)
    plan = plan_from(provider, {"get_features": features_payload(per, t0),
                                "get_pnl_summary": {"drawdown_pct": "0"}})
    assert plan["regimes"]["USDTTMN"]["regime"] == "chaos"
    assert plan["size_multiplier"] == 0
    assert {"chaos", "high_dollar_volatility"} <= set(plan["risk_flags"])
    assert plan["usdt_target_share"] == float(cfg.strategies.s2_usdtalloc.target_share_by_regime["chaos"])
    assert 0.5 <= plan["regimes"]["USDTTMN"]["confidence"] <= 0.9

    per = calm()
    per["SOLUSDT"] = feat(atr="0.02", med="0.004")  # chaos in any regime symbol -> size 0
    assert plan_from(provider, {"get_features": features_payload(per, t0)})["size_multiplier"] == 0


def test_drawdown_above_two_percent_halves_size(provider: OfflineRulesProvider, t0: float) -> None:
    payload = features_payload(calm(), t0)
    deep = plan_from(provider, {"get_features": payload, "get_pnl_summary": {"drawdown_pct": "0.025"}})
    assert deep["size_multiplier"] == 0.5
    assert "drawdown" in deep["risk_flags"]
    shallow = plan_from(provider, {"get_features": payload, "get_pnl_summary": {"drawdown_pct": "0.015"}})
    assert shallow["size_multiplier"] == 1.0
    assert "drawdown" not in shallow["risk_flags"]


def test_usdt_target_follows_the_dollar_regime(provider: OfflineRulesProvider, cfg: Config, t0: float) -> None:
    table = cfg.strategies.s2_usdtalloc.target_share_by_regime
    for regime, f in (("trend_up", feat(trend=1, r24="0.02", gap="0.003")),
                      ("trend_down", feat(trend=-1, r24="-0.02", gap="-0.003"))):
        per = calm()
        per["USDTTMN"] = f
        plan = plan_from(provider, {"get_features": features_payload(per, t0)})
        assert plan["regimes"]["USDTTMN"]["regime"] == regime
        assert plan["usdt_target_share"] == float(table[regime])


def test_stale_books_and_tool_errors_are_flagged(provider: OfflineRulesProvider, t0: float) -> None:
    per = calm()
    per["BTCUSDT"] = feat(age="120")
    plan = plan_from(provider, {"get_features": features_payload(per, t0),
                                "get_pnl_summary": {"error": "get_pnl_summary failed: RuntimeError: x"}})
    assert {"stale_books", "tool_errors"} <= set(plan["risk_flags"])
    assert "1 tool error(s)" in plan["notes"]


def test_disabled_strategies_are_not_enabled(cfg: Config, t0: float) -> None:
    no_s1 = apply_changes(cfg, {"strategies.s1_crossbook.enabled": False})
    plan = plan_from(OfflineRulesProvider(no_s1), {"get_features": features_payload(calm(), t0)})
    assert plan["enabled_strategies"] == ["S2"]


def test_plan_from_the_curated_feature_view(provider: OfflineRulesProvider, t0: float) -> None:
    """What the engine's get_features returns (short keys, JSON numbers): read through feature_rows."""
    per = calm()
    per["BTCUSDT"] = feat(trend=1, r24="0.03", gap="0.004")
    per["ETHUSDT"] = feat(trend=-1, r24="-0.03", gap="-0.004")
    snap = FeatureSnapshot(ts=t0, symbols={s: {k: Decimal(v) if isinstance(v, str) else v for k, v in f.items()}
                                           for s, f in per.items()})
    view = feature_view(snap, list(REGIME_SYMBOLS))
    assert view["symbols"]["BTCUSDT"]["trend"] == 1 and "trend_flag" not in view["symbols"]["BTCUSDT"]
    plan = plan_from(provider, {"get_features": view, "get_pnl_summary": {"drawdown_pct": "0.001"}})
    assert plan["regimes"]["BTCUSDT"]["regime"] == "trend_up"
    assert plan["regimes"]["ETHUSDT"]["regime"] == "trend_down"
    assert all(r["evidence"].endswith("[features]") for r in plan["regimes"].values())
    assert "missing_features" not in plan["risk_flags"] and "salvaged" not in plan["notes"]


# ---------------------------------------------------------------- size-capped tool results


def _big_payload(per: dict[str, dict[str, Any]], t0: float) -> dict[str, Any]:
    """A get_features result whose cross section pushes it far over the 4 kB tool cap."""
    payload = features_payload(per, t0)
    payload["cross"] = {f"C{i:02d}": {"coin": f"C{i:02d}", "dev_mid_pct": "0.0001234567", "edge_tmn_cheap_pct":
                                      "-0.0012345678", "edge_usdt_cheap_pct": "-0.0023456789", "u_mid": "117146.3192"}
                        for i in range(26)}  # sized so the 4 kB cut lands just after the "dollar" object
    return payload


def test_truncated_features_are_salvaged_then_candle_stats_requested(provider: OfflineRulesProvider,
                                                                     t0: float) -> None:
    big = _big_payload(calm(), t0)
    assert truncate(big, 4096).get("truncated") is True  # the real cap does truncate this payload
    # Salvage logic is tested on a prefix cut in the middle of the first symbol object (BTCUSDT), in
    # the same {"truncated", "json_prefix"} shape truncate() produces (its exact cut depends on escaping).
    text = canonical_json(big).decode()
    cut = {"truncated": True, "json_prefix": text[: text.index('"BTCUSDT":{') + 30]}
    assert '"dollar":{' in cut["json_prefix"] and '"BTCUSDT":{' in cut["json_prefix"]
    msgs =with_tool_results({"get_features": cut, "get_pnl_summary": {"drawdown_pct": "0.001"}})
    resp = provider.respond(msgs, TOOLS)
    # sorted keys: "cross" comes first, so the cut loses "symbols" but keeps the complete "dollar" object
    asked = {c.arguments["symbol"] for c in resp.tool_calls}
    assert {c.name for c in resp.tool_calls} == {"get_candle_stats"}
    assert asked == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert all(c.arguments["n"] == CANDLE_STATS_BARS and c.arguments["resolution"] == "15" for c in resp.tool_calls)

    msgs.append({"role": "assistant", "content": resp.text, "tool_calls": [
        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
        for c in resp.tool_calls]})
    for c in resp.tool_calls:
        sym = c.arguments["symbol"]
        up = sym == "BTCUSDT"
        stats = {"symbol": sym, "resolution": "15", "bars": CANDLE_STATS_BARS, "ret_all": "0.03" if up else "0.001",
                 "atr14_pct": "0.004", "ema_gap_pct": "0.004" if up else "0.0002", "last_close": "100"}
        msgs.append({"role": "tool", "tool_call_id": c.id, "name": c.name, "content": json.dumps(stats)})
    final = provider.respond(msgs, TOOLS)
    assert final.tool_calls == []  # never asks twice
    plan = PlanShape.model_validate(extract_json(final.text))
    assert plan.regimes["BTCUSDT"].regime == "trend_up"
    assert plan.regimes["BTCUSDT"].evidence.endswith("[candle stats]")
    assert plan.regimes["USDTTMN"].evidence.endswith("[features]")
    assert "missing_features" not in plan.risk_flags
    assert "salvaged" in plan.notes


# ---------------------------------------------------------------- determinism


def test_identical_messages_give_identical_responses(cfg: Config, t0: float) -> None:
    per = calm()
    per["ETHUSDT"] = feat(trend=-1, r24="-0.03", gap="-0.004")
    msgs = with_tool_results({"get_features": features_payload(per, t0), "get_pnl_summary": {"drawdown_pct": "0.03"}})
    before = copy.deepcopy(msgs)
    a = OfflineRulesProvider(cfg).respond(msgs, TOOLS)
    b = OfflineRulesProvider(cfg).respond(copy.deepcopy(msgs), TOOLS)
    assert a.text == b.text and a.tool_calls == b.tool_calls
    assert msgs == before  # the provider never mutates the conversation
    first_a = OfflineRulesProvider(cfg).respond(base_messages(), TOOLS)
    first_b = OfflineRulesProvider(cfg).respond(base_messages(), TOOLS)
    assert (first_a.text, first_a.tool_calls) == (first_b.text, first_b.tool_calls)


# ---------------------------------------------------------------- reviewer branch


def test_reviewer_prompt_returns_a_labelled_journal(provider: OfflineRulesProvider) -> None:
    review_input = {
        "decisions": [{"kind": "intent", "strategy": "S1"}, {"kind": "veto", "strategy": "S1"},
                      {"kind": "veto", "strategy": "S2"}, {"kind": "order", "strategy": "S2"}],
        "vetoes_by_code": {"R_FEE_BUDGET": 1, "R_MAX_ORDERS_HOUR": 1},
        "fees_paid_tmn": 1234,
    }
    msgs = [{"role": "system", "content": REVIEWER_SYSTEM},
            {"role": "user", "content": "Last hour:\n" + json.dumps(review_input)}]
    resp = provider.respond(msgs, TOOLS)  # tools offered or not: the reviewer never calls tools
    assert resp.tool_calls == []
    out = extract_json(resp.text)
    assert set(out) == {"journal", "param_suggestions", "alerts"}
    assert out["param_suggestions"] == [] and out["alerts"] == []
    journal = out["journal"]
    assert journal.startswith(OFFLINE_LABEL)
    assert "Decision records: 4 (intent 1, order 1, veto 2)" in journal
    assert "`decisions`: 4 item(s)" in journal
    assert "`vetoes_by_code`: 2 entries" in journal
    assert "`fees_paid_tmn`: 1234" in journal
    assert 5 <= len(journal.splitlines()) <= 15


def test_reviewer_without_structured_input(provider: OfflineRulesProvider) -> None:
    msgs = [{"role": "system", "content": REVIEWER_SYSTEM}, {"role": "user", "content": "quiet hour\nnothing"}]
    out = extract_json(provider.respond(msgs, None).text)
    assert out["journal"].startswith(OFFLINE_LABEL)
    assert "2 line(s)" in out["journal"]


# ---------------------------------------------------------------- through the real LLMClient + LLMCache


def _snapshot(t0: float) -> FeatureSnapshot:
    def f(trend: int, r24: str, gap: str) -> dict[str, Any]:
        return {"trend_flag": trend, "ret_24h": Decimal(r24), "ema_gap_pct": Decimal(gap),
                "atr_15m_pct": Decimal("0.004"), "atr_15m_pct_median_24h": Decimal("0.0038"),
                "book_age_s": Decimal("1"), "spread_pct": Decimal("0.0008")}

    symbols = {"BTCUSDT": f(1, "0.021", "0.0042"), "ETHUSDT": f(0, "0.002", "0.0004"),
               "SOLUSDT": f(-1, "-0.015", "-0.0031"), "USDTTMN": f(0, "0.001", "0.0002")}
    return FeatureSnapshot(ts=t0, symbols=symbols, dollar=symbols["USDTTMN"])


def _tools(cfg: Config, clock: SimClock, snap: FeatureSnapshot | None) -> InternalTools:
    return InternalTools(ToolDeps(
        cfg=cfg, now=clock.now, features=lambda: snap, books=dict, candles_1m=lambda s, a, b: [],
        trades=lambda s, a, b: [], pnl_summary=lambda: {"equity_tmn": "5010000", "drawdown_pct": "0.003"},
        recent_decisions=lambda n: [], health=lambda: {"ok": True}))


async def _run_loop(llm: LLMClient, tools: InternalTools, max_rounds: int = 6
                    ) -> tuple[LLMResponse, list[dict[str, Any]], list[LLMResponse]]:
    """Minimal hand-written analyst loop (PRD-04 §6): call -> run tools -> append -> until final JSON."""
    msgs = base_messages()
    responses: list[LLMResponse] = []
    for _ in range(max_rounds):
        resp = await llm.chat(msgs, tools.schemas, role="analyst")
        assert resp is not None
        responses.append(resp)
        if not resp.tool_calls:
            return resp, msgs, responses
        msgs.append({"role": "assistant", "content": resp.text, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in resp.tool_calls]})
        for c in resp.tool_calls:
            msgs.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                         "content": json.dumps(tools.call(c.name, c.arguments), sort_keys=True)})
    raise AssertionError("tool loop did not finish")


async def test_full_cycle_through_llm_client_and_cache(cfg: Config, sim_clock: SimClock, t0: float,
                                                       tmp_path: Path) -> None:
    tools = _tools(cfg, sim_clock, _snapshot(t0))
    llm = make_offline_llm(cfg, LLMCache(tmp_path), "shadow", sim_clock)
    final, msgs, responses = await _run_loop(llm, tools)
    assert len(responses) == 2  # tool round + plan
    assert [r.cached for r in responses] == [False, False]
    assert all(r.provider == OFFLINE_PROVIDER and r.model == OFFLINE_MODEL for r in responses)
    plan = PlanShape.model_validate(extract_json(final.text))
    assert [plan.regimes[s].regime for s in REGIME_SYMBOLS] == ["trend_up", "range", "trend_down", "range"]
    assert len(llm.cache) == 2

    # structured() (no tools offered): JSON extraction + pydantic validation; a new key, then a cache hit
    obj, last, err = await llm.structured(msgs, PlanShape)
    assert err == "" and obj == plan and last is not None and not last.cached
    obj2, last2, _ = await llm.structured(msgs, PlanShape)
    assert obj2 == plan and last2 is not None and last2.cached
    assert len(llm.cache) == 3

    # second identical cycle: every call is served from the cache
    llm2 = make_offline_llm(cfg, LLMCache(tmp_path), "shadow", sim_clock)
    final2, _, responses2 = await _run_loop(llm2, tools)
    assert [r.cached for r in responses2] == [True, True]
    assert final2.text == final.text
    assert len(llm2.cache) == 3

    # replay (strict): identical plan from the cache alone; anything new is a hard miss
    replay = make_offline_llm(cfg, LLMCache(tmp_path, strict=True), "replay", sim_clock)
    final3, _, _ = await _run_loop(replay, tools)
    assert final3.text == final.text and final3.cached
    with pytest.raises(CacheMissError):
        await replay.chat(base_messages() + [{"role": "user", "content": "new"}], None)

    entry = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert entry["request"]["provider"] == OFFLINE_PROVIDER and entry["request"]["model"] == OFFLINE_MODEL
    assert entry["ts"] == t0  # injected clock, not the wall clock


async def test_cycle_without_any_data_degrades_and_terminates(cfg: Config, sim_clock: SimClock,
                                                              tmp_path: Path) -> None:
    llm = make_offline_llm(cfg, LLMCache(tmp_path), "shadow", sim_clock)
    final, _, responses = await _run_loop(llm, _tools(cfg, sim_clock, None))
    # features unavailable -> candle stats for all four -> (no candles) -> conservative plan
    assert len(responses) == 3
    assert len(responses[1].tool_calls) == len(REGIME_SYMBOLS)
    plan = PlanShape.model_validate(extract_json(final.text))
    assert {"missing_features", "tool_errors"} <= set(plan.risk_flags)
    assert all(r.regime == "range" and r.confidence == 0.0 for r in plan.regimes.values())


def test_make_offline_llm_routes_only_to_the_offline_provider(cfg: Config, sim_clock: SimClock,
                                                               tmp_path: Path) -> None:
    llm = make_offline_llm(cfg, LLMCache(tmp_path), "backtest", sim_clock)
    assert llm.primary == Route("offline", "offline-rules-v1")
    assert llm.fallback is None
    assert set(llm.providers) == {"offline"} and isinstance(llm.providers["offline"], OfflineRulesProvider)
    assert llm.mode == "backtest" and llm.clock is sim_clock


# ---------------------------------------------------------------- engine switch + labels


def test_offline_only_without_keys_and_with_ai_enabled(cfg: Config) -> None:
    none = Settings(_env_file=None, openrouter_api_key="", gemini_api_key="")
    keyed = Settings(_env_file=None, openrouter_api_key="sk-or-test-not-real", gemini_api_key="")
    gem = Settings(_env_file=None, openrouter_api_key="", gemini_api_key="gm-test-not-real")
    assert cfg.ai.enabled
    assert not has_llm_key(none) and has_llm_key(keyed) and has_llm_key(gem)
    assert use_offline_provider(cfg, none)
    assert not use_offline_provider(cfg, keyed) and not use_offline_provider(cfg, gem)
    assert not use_offline_provider(apply_changes(cfg, {"ai.enabled": False}), none)


def test_status_labels() -> None:
    assert is_offline_model("offline-rules-v1") and not is_offline_model("google/gemini-2.5-flash")
    assert ai_status_label(OFFLINE_MODEL) == OFFLINE_STATUS == "AI: offline rules (no LLM key)"
    assert ai_status_label("x/y") == "AI: x/y"
    assert ai_status_label("") == "AI: no plan"
