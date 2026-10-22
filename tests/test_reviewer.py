"""Post-trade Reviewer (PRD-04 §7).

A fake provider runs through the real ``LLMClient`` + ``LLMCache`` (tmp dir), and the real
``ConfigManager`` writes its overlays to a tmp runtime dir. Covered: bounded apply (in-bounds
accepted; out-of-bounds, unlisted and risk.* paths ignored), max 2 changes, cooldown, config_hash
change + ``config_change`` event, append-only journal, invalid JSON -> retry -> None, llm None,
deterministic input building, and replay from the cache with zero provider calls.
No network, no wall clock.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta.ai.cache import CacheMissError, LLMCache
from wta.ai.config_manager import ConfigManager
from wta.ai.llm_client import LLMClient, LLMResponse, ProviderError, Route
from wta.ai.reviewer import (
    JOURNAL_HEADER,
    PostTradeReviewer,
    ReviewOutput,
    editable_paths,
    render_template,
    reviewer_route,
)
from wta.config import Config, apply_changes, canonical_json, config_hash, load_config
from wta.utils.timeutil import SimClock, iso

MODEL = "fake/reviewer-model"
MARGIN = "strategies.s1_crossbook.margin_min_pct"
REBAL = "strategies.s2_usdtalloc.rebalance_threshold_pp"
S3CONF = "strategies.s3_momentum.min_regime_confidence"
SLOW = "engine.slow_loop_interval_s"
SECRET = "fake-provider-key-NOT-REAL-0123456789"  # deliberately not shaped like a real provider key


# ---------------------------------------------------------------- fakes / helpers


class FakeProvider:
    """Scripted provider: each call pops the next reply (a text, or an exception to raise)."""

    name = "openrouter"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, model: str,
                   temperature: float, max_tokens: int, json_mode: bool) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model,
                           "temperature": temperature, "max_tokens": max_tokens, "json_mode": json_mode})
        if not self.replies:
            raise ProviderError("fake provider: no scripted reply left")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r, usage={"prompt_tokens": 100, "completion_tokens": 40}, model=model,
                           provider=self.name, latency_ms=7)


def sug(path: str, value: Any, rationale: str = "quoted evidence from this hour") -> dict[str, Any]:
    return {"path": path, "value": value, "rationale": rationale}


def reply(journal: str = "S1 locked in 2 trades (+1,250 TMN); 3 vetoes, mostly FEE_BUDGET.",
          suggestions: list[dict[str, Any]] | None = None, alerts: list[str] | None = None) -> str:
    return json.dumps({"journal": journal, "param_suggestions": suggestions or [], "alerts": alerts or []})


@dataclass
class Rig:
    reviewer: PostTradeReviewer
    provider: FakeProvider
    llm: LLMClient
    cm: ConfigManager
    clock: SimClock
    journal: Path
    runtime_dir: Path
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


def make_rig(tmp_path: Path, cfg: Config, clock: SimClock, replies: list[str | Exception], *, sub: str = "a",
             mode: str = "shadow", strict: bool = False, secrets: list[str] | None = None) -> Rig:
    provider = FakeProvider(replies)
    cache = LLMCache(tmp_path / "llm_cache", strict=strict)  # shared by every rig of a test (replay)
    llm = LLMClient({"openrouter": provider}, Route("openrouter", MODEL), None, cache, mode=mode, clock=clock,
                    daily_budget={"reviewer": cfg.ai.max_reviewer_calls_per_day})
    runtime_dir = tmp_path / sub / "runtime"
    cm = ConfigManager(cfg, runtime_dir=runtime_dir)
    events: list[tuple[str, str, dict[str, Any]]] = []
    journal = tmp_path / sub / "logs" / "journal.md"
    reviewer = PostTradeReviewer(cfg, llm, cm, clock, journal_path=journal,
                                 on_event=lambda k, r, d: events.append((k, r, d)), secrets=secrets)
    return Rig(reviewer, provider, llm, cm, clock, journal, runtime_dir, events)


def _rec(kind: str, did: str, ts: float, strategy: str, symbol: str, **extra: Any) -> dict[str, Any]:
    """A decisions.jsonl line (DecisionRecord shape)."""
    return {"kind": kind, "decision_id": did, "ts": iso(ts), "mode": "shadow", "git_commit": "abc1234",
            "config_hash": "h0", "strategy": strategy, "symbol": symbol, "features": {}, "plan_snapshot": None,
            "intent": None, "risk": {}, "order": None, "outcome": None, "notes": "", **extra}


def _intent(did: str, ts: float, strategy: str, symbol: str, reason: str, side: str = "BUY") -> dict[str, Any]:
    return {"id": did, "ts": ts, "strategy": strategy, "symbol": symbol, "side": side, "order_type": "LIMIT",
            "purpose": "entry", "reason": reason, "features": {}, "taker": False}


def _veto_risk(code: str, detail: str) -> dict[str, Any]:
    return {"approved": False, "veto_reason": f"{code}: {detail}", "adjusted_qty": None,
            "checks": [{"code": "MAX_ORDER", "passed": True, "detail": "ok"},
                       {"code": code, "passed": False, "detail": detail}]}


def _outcome(did: str, ts: float, outcome: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "outcome", "decision_id": did, "ts": iso(ts), "outcome": outcome}


def _plan(ts: float, regime: str, conf: float, size: str, share: str) -> dict[str, Any]:
    return {"ts": ts, "regimes": {"USDTTMN": {"regime": regime, "confidence": conf, "evidence": "ema gap"},
                                  "BTCUSDT": {"regime": "range", "confidence": 0.55, "evidence": "flat"}},
            "enabled_strategies": ["S2", "S1"], "s1_margin_min_pct": "0.0015", "size_multiplier": size,
            "usdt_target_share": share, "risk_flags": [], "notes": "USDT drifting up; keep S1 on.",
            "model": MODEL, "cached": False, "tools_used": [], "tools_source": "local", "clamped": {}}


def _health(ts: float, **kw: Any) -> dict[str, Any]:
    base = {"ts": iso(ts), "mode": "shadow", "git_commit": "abc1234", "config_hash": "h0", "equity_tmn": "5000000",
            "open_orders": 0, "positions": 0, "ws_ok": True, "rest_ok": True, "api_errors_1m": 0, "llm_ok": True,
            "paused": False, "killed": False, "reasons": [], "extra": {}}
    return {**base, **kw}


def sample(t0: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """One hour [t0, t0+3600] of decisions/plans/health, plus records outside it that must be dropped."""
    long_reason = "S1 lock-in: BTCTMN ask below BTCUSDT bid x USDTTMN by 0.41% > fees+margin. " * 5
    decisions = [
        _rec("no_trade", "nt-1", t0 + 100, "S1", "BTCTMN", notes="no edge"),
        _rec("intent", "d1", t0 + 600, "S1", "BTCTMN", intent=_intent("d1", t0 + 600, "S1", "BTCTMN", long_reason)),
        _rec("order", "d1", t0 + 601, "S1", "BTCTMN", intent=_intent("d1", t0 + 600, "S1", "BTCTMN", long_reason),
             risk={"approved": True, "checks": [{"code": "MAX_ORDER", "passed": True, "detail": "ok"}]},
             order={"accepted": True, "response": {"status": "FILLED"}}),
        _outcome("d1", t0 + 1500, {"pnl_tmn": "1200.50", "pnl_pct": "0.0024", "hold_s": 899.0,
                                   "closed_ts": t0 + 1500, "exit_reason": "lockin", "strategy": "S1",
                                   "symbol": "BTCTMN", "position_id": "p1"}),
        _rec("veto", "d2", t0 + 900, "S1", "ETHTMN", intent=_intent("d2", t0 + 900, "S1", "ETHTMN", "S1 revert"),
             risk=_veto_risk("FEE_BUDGET", "fees paid 99,000 TMN + expected 2,000 TMN > budget 100,000 TMN"),
             notes="vetoed: FEE_BUDGET"),
        _rec("veto", "d3", t0 + 1200, "S3", "BTCUSDT", intent=_intent("d3", t0 + 1200, "S3", "BTCUSDT", "breakout"),
             risk=_veto_risk("FEE_BUDGET", "fees paid 99,500 TMN + expected 900 TMN > budget"), notes="vetoed"),
        _rec("veto", "d4", t0 + 1300, "S2", "USDTTMN", intent=_intent("d4", t0 + 1300, "S2", "USDTTMN", "rebalance"),
             risk=_veto_risk("PRICE_SANITY", "limit 3.4% away from mid"), notes="vetoed"),
        # entered before the window, closed inside it -> its PnL belongs to this hour
        _rec("order", "d5", t0 - 1800, "S3", "ETHUSDT", intent=_intent("d5", t0 - 1800, "S3", "ETHUSDT", "breakout"),
             risk={"approved": True, "checks": []}, order={"accepted": True}),
        _outcome("d5", t0 + 2000, {"pnl_tmn": "-300", "pnl_pct": "-0.001", "closed_ts": t0 + 2000,
                                   "exit_reason": "stop", "strategy": "S3", "symbol": "ETHUSDT"}),
        # entered and closed before the window -> dropped
        _rec("order", "d6", t0 - 7200, "S1", "SOLTMN", intent=_intent("d6", t0 - 7200, "S1", "SOLTMN", "old")),
        _outcome("d6", t0 - 3600, {"pnl_tmn": "999", "pnl_pct": "0.01", "closed_ts": t0 - 3600, "strategy": "S1"}),
        # S2 rebalance order with an order-level outcome (fees in USDT)
        _rec("order", "d7", t0 + 1800, "S2", "USDTTMN", intent=_intent("d7", t0 + 1800, "S2", "USDTTMN", "rebalance"),
             risk={"approved": True, "checks": []}, order={"accepted": True}),
        _outcome("d7", t0 + 1850, {"kind": "order", "status": "FILLED", "fee_paid": "0.12", "fee_asset": "USDT",
                                   "done_ts": t0 + 1850, "symbol": "USDTTMN", "purpose": "rebalance"}),
        # outcome of an entry that is not among the given records (orphan) -> still realized this hour
        _outcome("old-entry", t0 + 2500, {"pnl_tmn": "50", "pnl_pct": "0.0001", "closed_ts": t0 + 2500,
                                          "exit_reason": "target", "strategy": "S1", "symbol": "XRPTMN"}),
    ]
    plans = [
        _plan(t0 - 600, "range", 0.5, "1", "0.5"),  # before the window
        _plan(t0 + 300, "range", 0.7, "1", "0.5"),
        _plan(t0 + 600, "trend_up", 0.65, "0.8", "0.6"),
        # a kind="plan" decision record carrying the plan in plan_snapshot
        _rec("plan", "plan-3", t0 + 900, "AI", "*", plan_snapshot=_plan(t0 + 900, "trend_up", 0.8, "0.8", "0.62")),
    ]
    health = [
        _health(t0 - 60, killed=True, reasons=["killed: drawdown"]),  # before the window
        _health(t0 + 60),
        _health(t0 + 120, paused=True, api_errors_1m=3, reasons=["paused: price disagreement USDTTMN"]),
        _health(t0 + 180, rest_ok=False, ws_ok=False, reasons=["stale data: BTCTMN 45s"]),
        {"ts": iso(t0 + 120), "kind": "pause", "reason": "price disagreement USDTTMN 5.2%"},
    ]
    portfolio = {"equity_tmn": "5012000", "initial_equity_tmn": "5000000", "fees_paid_tmn": "25000",
                 "drawdown_pct": "0.001", "realized_pnl_tmn": {"S3": "-300", "S1": "1250.5"}}
    return decisions, plans, health, portfolio


def _schema_from_prompt(prompt: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(\{.*?\})\s*```", prompt, re.S)
    assert m, "schema block missing from the prompt"
    return json.loads(m.group(1))


# ---------------------------------------------------------------- prompt


def test_render_template_replaces_only_known_keys() -> None:
    t = 'a {editable} b {"path", "value"} c {unknown} d {schema} e {"journal": "x"}'
    out = render_template(t, {"editable": "X", "schema": "{max_changes}", "max_changes": 2})
    # JSON braces and unknown keys survive; replacement text is not re-scanned
    assert out == 'a X b {"path", "value"} c {unknown} d {max_changes} e {"journal": "x"}'


def test_system_prompt_lists_bounds_current_values_and_strict_schema(tmp_path: Path, cfg: Config,
                                                                     sim_clock: SimClock) -> None:
    prompt = make_rig(tmp_path, cfg, sim_clock, []).reviewer.system_prompt()
    assert f"- `{MARGIN}`: [0.0008, 0.005] (inclusive) (current: 0.0015)" in prompt
    assert "- `strategies.s1_crossbook.leg1_taker`: true or false (current: true)" in prompt
    assert f"- `{SLOW}`: [180, 900] (inclusive) (current: 300)" in prompt
    assert "At most 2 suggestions per review" in prompt
    assert '{"path", "value", "rationale"}' in prompt  # literal JSON braces of the template kept
    assert not re.search(r"\{(editable|schema|max_changes)\}", prompt)
    schema = _schema_from_prompt(prompt)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"journal", "param_suggestions", "alerts"}
    items = schema["properties"]["param_suggestions"]["items"]
    assert items["additionalProperties"] is False
    assert items["properties"]["path"]["enum"] == sorted(cfg.llm.reviewer.editable)
    assert items["properties"]["value"]["type"] == ["boolean", "number"]
    assert schema["properties"]["param_suggestions"]["maxItems"] == 2
    assert "risk." not in prompt.split("Return **only**")[0]  # no risk path is offered
    assert reviewer_route(cfg) == Route(cfg.llm.reviewer.provider, cfg.llm.reviewer.model or "")


def test_review_output_is_strict() -> None:
    ok = ReviewOutput.model_validate({"journal": "j", "param_suggestions": [sug(MARGIN, 0.001)], "alerts": []})
    assert ok.param_suggestions[0].value == 0.001
    assert ReviewOutput.model_validate({"journal": "j", "param_suggestions": [
        sug("strategies.s1_crossbook.leg1_taker", False)]}).param_suggestions[0].value is False
    with pytest.raises(ValueError):
        ReviewOutput.model_validate({"journal": "j", "param_suggestions": [], "alerts": [], "extra": 1})
    with pytest.raises(ValueError):
        ReviewOutput.model_validate({"journal": "j", "param_suggestions": [{**sug(MARGIN, 0.001), "why": "x"}]})


# ---------------------------------------------------------------- input


def test_build_input_is_deterministic_compact_and_windowed(tmp_path: Path, cfg: Config, sim_clock: SimClock,
                                                           t0: float) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [])
    sim_clock.set(t0 + 3600)
    decisions, plans, health, portfolio = sample(t0)
    a = rig.reviewer.build_input(decisions, plans, health, portfolio)

    rng = random.Random(7)
    d2, p2, h2 = list(decisions), list(plans), list(health)
    for lst in (d2, p2, h2):
        rng.shuffle(lst)
    b = rig.reviewer.build_input(d2, p2, h2, dict(reversed(list(portfolio.items()))))
    assert canonical_json(a) == canonical_json(b)
    assert json.loads(json.dumps(a)) == a  # plain JSON

    assert a["window"] == {"from": iso(t0), "to": iso(t0 + 3600), "seconds": 3600.0}
    assert a["config_hash"] == config_hash(cfg)
    ids = [d["id"] for d in a["decisions"]]
    assert ids == ["d5", "d1", "d2", "d3", "d4", "d7", "old-entry"]  # d6 (closed before) and no_trade dropped
    assert a["decision_counts"] == {"no_trade": 1, "order": 3, "outcome": 1, "veto": 3}
    assert a["orphan_outcomes"] == 1
    d1 = a["decisions"][1]
    assert d1["kind"] == "order" and d1["approved"] is True and d1["order_status"] == "FILLED"
    assert d1["outcome"]["pnl_tmn"] == "1200.50" and d1["outcome"]["exit_reason"] == "lockin"
    assert len(d1["reason"]) <= 160
    assert a["decisions"][2]["veto_codes"] == ["FEE_BUDGET"]

    assert a["vetoes_total"] == 3
    assert list(a["vetoes_by_code"]) == ["FEE_BUDGET", "PRICE_SANITY"]
    fee = a["vetoes_by_code"]["FEE_BUDGET"]
    assert fee["count"] == 2 and fee["example"].startswith("S1 ETHTMN: fees paid 99,000 TMN")
    assert a["vetoes_by_code"]["PRICE_SANITY"]["count"] == 1

    assert a["pnl_by_strategy"] == {
        "S1": {"trades": 2, "wins": 2, "losses": 0, "pnl_tmn": "1250.5", "avg_pnl_pct": "0.00125"},
        "S3": {"trades": 1, "wins": 0, "losses": 1, "pnl_tmn": "-300", "avg_pnl_pct": "-0.001"},
    }
    assert a["pnl_tmn_window_total"] == "950.5"
    assert a["fees"] == {"budget_pct_of_initial": "0.02", "budget_tmn": "100000", "paid_tmn_total": "25000",
                         "used_pct_of_budget": "0.25", "window_by_asset": {"USDT": "0.12"}}

    assert a["plans_total"] == 3
    assert [p["ts"] for p in a["plans"]] == [iso(t0 + 300), iso(t0 + 600), iso(t0 + 900)]
    assert a["plans"][0]["regimes"] == {"BTCUSDT": "range@0.55", "USDTTMN": "range@0.7"}
    assert a["plans"][2]["usdt_target_share"] == "0.62"
    assert a["plans"][1]["enabled_strategies"] == ["S1", "S2"]

    h = a["health"]
    assert (h["records"], h["paused"], h["killed"], h["stale"]) == (3, 1, 0, 1)
    assert (h["api_error_records"], h["api_errors_max_1m"], h["llm_down"]) == (1, 3, 0)
    assert [r["reason"] for r in h["reasons"]] == ["paused: price disagreement USDTTMN", "stale data: BTCTMN 45s"]
    assert h["events"] == [{"ts": iso(t0 + 120), "kind": "pause", "reason": "price disagreement USDTTMN 5.2%"}]
    assert a["portfolio"]["realized_pnl_tmn"] == {"S1": "1250.5", "S3": "-300"}


def test_synthetic_heartbeats_are_not_stale_by_their_source_flags(tmp_path: Path, cfg: Config,
                                                                  sim_clock: SimClock) -> None:
    """On the synthetic feed rest_ok/ws_ok are always False; only the engine's reasons count (a real
    LLM review once flagged every synthetic heartbeat as stale)."""
    t = sim_clock.now()
    synth = {"feed": "synthetic"}
    health = [_health(t - 120, rest_ok=False, ws_ok=False, extra=synth),
              _health(t - 60, rest_ok=False, ws_ok=False, extra=synth, reasons=["synthetic feed: no fresh data"]),
              _health(t - 30, rest_ok=False, ws_ok=False)]  # a live heartbeat without any source: stale
    h = make_rig(tmp_path, cfg, sim_clock, []).reviewer.build_input([], [], health, {})["health"]
    assert (h["records"], h["stale"]) == (3, 2)


def test_build_input_empty_is_valid(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    a = make_rig(tmp_path, cfg, sim_clock, []).reviewer.build_input([], [], [], {})
    assert a["decisions"] == [] and a["pnl_by_strategy"] == {} and a["vetoes_by_code"] == {}
    assert a["fees"]["paid_tmn_total"] is None and a["fees"]["budget_tmn"] == "100000"


# ---------------------------------------------------------------- bounded apply


async def test_bounded_apply_accepts_in_bounds_and_ignores_the_rest(tmp_path: Path, cfg: Config,
                                                                    sim_clock: SimClock, t0: float) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(suggestions=[
        sug(MARGIN, 0.0012, "6 S1 opportunities at 0.13-0.15% margin reverted profitably"),
        sug(REBAL, 0.5),  # out of bounds [0.10, 0.25]
        sug("risk.max_order_pct_equity", 0.5),  # risk is never editable
        sug("strategies.s1_crossbook.size_pct_equity", 0.3),  # not listed as editable
        sug("strategies.s1_crossbook.leg1_taker", "yes"),  # not a boolean
    ], alerts=["FEE_BUDGET vetoes: budget 25% used"])])
    sim_clock.set(t0 + 3600)
    review_input = rig.reviewer.build_input(*sample(t0))
    res = await rig.reviewer.run(review_input)

    assert res.raw_ok and res.change is not None
    assert [(a["path"], a["new"]) for a in res.change.accepted] == [(MARGIN, "0.0012")]
    why = {r["path"]: r["why"] for r in res.change.rejected}
    assert why[REBAL].startswith("outside bounds")
    assert why["risk.max_order_pct_equity"].startswith("forbidden path")
    assert why["strategies.s1_crossbook.size_pct_equity"] == "path not editable"
    assert why["strategies.s1_crossbook.leg1_taker"] == "expected a boolean"
    assert rig.cm.cfg.strategies.s1_crossbook.margin_min_pct == Decimal("0.0012")
    assert rig.reviewer.cfg is rig.cm.cfg
    assert rig.cm.cfg.risk == cfg.risk and rig.cm.cfg.strategies.s2_usdtalloc == cfg.strategies.s2_usdtalloc
    assert res.alerts == ["FEE_BUDGET vetoes: budget 25% used"]

    # one model call: role reviewer, temperature 0, JSON mode, no tools, the compact input as JSON
    (call,) = rig.provider.calls
    assert call["temperature"] == 0.0 and call["json_mode"] is True and call["tools"] is None
    assert call["model"] == MODEL and call["max_tokens"] == cfg.llm.reviewer.max_tokens
    assert canonical_json(review_input).decode() in call["messages"][1]["content"]

    text = rig.journal.read_text()
    assert "**Config changes applied**" in text
    assert f"- `{MARGIN}`: 0.0015 -> 0.0012 — 6 S1 opportunities" in text
    assert "**Suggestions rejected/ignored**" in text
    assert "- `risk.max_order_pct_equity` = 0.5: forbidden path" in text
    assert f"- `{REBAL}` = 0.5: outside bounds" in text
    overlay = Path(res.change.overlay_path or "")
    assert overlay.parent == rig.runtime_dir and overlay.exists()
    assert "margin_min_pct: 0.0012" in overlay.read_text() and "risk" not in overlay.read_text()


async def test_risk_paths_never_apply_even_if_declared_editable(tmp_path: Path, cfg: Config,
                                                               sim_clock: SimClock) -> None:
    editable = {**cfg.llm.reviewer.editable, "risk.max_order_pct_equity": [0.1, 0.3]}
    loose = apply_changes(cfg, {"llm.reviewer.editable": editable})
    rig = make_rig(tmp_path, loose, sim_clock, [reply(suggestions=[sug("risk.max_order_pct_equity", 0.2)])])
    assert "risk.max_order_pct_equity" not in editable_paths(loose)
    assert "risk.max_order_pct_equity" not in rig.reviewer.system_prompt()
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res.change is not None and res.change.accepted == []
    assert res.change.rejected[0]["why"].startswith("forbidden path")
    assert rig.cm.cfg.risk.max_order_pct_equity == Decimal("0.25")
    assert config_hash(rig.cm.cfg) == config_hash(loose) and rig.events == []


async def test_at_most_two_changes_per_review(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(suggestions=[sug(MARGIN, 0.0012), sug(REBAL, 0.12),
                                                                 sug(S3CONF, 0.7)])])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res.change is not None
    assert [a["path"] for a in res.change.accepted] == [MARGIN, REBAL]
    assert [(r["path"], r["why"]) for r in res.change.rejected] == [(S3CONF, "max_changes_per_review reached")]
    assert rig.cm.cfg.strategies.s3_momentum.min_regime_confidence == Decimal("0.6")
    assert len(rig.events) == 1 and [d["path"] for d in rig.events[0][2]["diff"]] == [MARGIN, REBAL]


async def test_numeric_noop_is_not_a_change(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    # 300 == 300.0: must not burn a change slot, the cooldown, an overlay file or a config_change event
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(suggestions=[sug(SLOW, 300), sug(MARGIN, 0.0015)])])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res.change is not None and res.change.accepted == []
    assert {r["why"] for r in res.change.rejected} == {"no change"}
    assert rig.events == [] and rig.cm.last_apply_ts is None and not rig.runtime_dir.exists()


async def test_cooldown_blocks_a_second_change_within_the_hour(tmp_path: Path, cfg: Config,
                                                              sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(suggestions=[sug(MARGIN, 0.0012)]),
                                              reply(suggestions=[sug(REBAL, 0.12)]),
                                              reply(suggestions=[sug(REBAL, 0.12)])])
    r1 = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert r1.change is not None and len(r1.change.accepted) == 1

    sim_clock.advance(600)
    h1 = config_hash(rig.cm.cfg)
    r2 = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert r2.change is not None and r2.change.accepted == []
    assert r2.change.rejected[0]["why"] == "cooldown 3600s"
    assert config_hash(rig.cm.cfg) == h1 and len(rig.events) == 1
    assert rig.cm.cfg.strategies.s2_usdtalloc.rebalance_threshold_pp == Decimal("0.15")

    sim_clock.advance(3000)  # exactly cooldown_s after the first change
    r3 = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert r3.change is not None and [a["path"] for a in r3.change.accepted] == [REBAL]
    assert len(rig.events) == 2 and len(rig.provider.calls) == 3
    assert rig.cm.cfg.strategies.s2_usdtalloc.rebalance_threshold_pp == Decimal("0.12")


async def test_config_hash_changes_and_event_carries_hashes_and_diff(tmp_path: Path, cfg: Config,
                                                                     sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(suggestions=[sug(MARGIN, 0.0012), sug(REBAL, 0.9)])])
    h0 = config_hash(cfg)
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res.change is not None
    h1 = config_hash(rig.cm.cfg)
    assert h1 != h0 and res.change.old_hash == h0 and res.change.new_hash == h1

    ((kind, reason, data),) = rig.events
    assert (kind, reason) == ("config_change", "reviewer")
    assert data["old_hash"] == h0 and data["new_hash"] == h1 and data["source"] == "reviewer"
    assert data["diff"] == [{"path": MARGIN, "old": "0.0015", "new": "0.0012",
                             "rationale": "quoted evidence from this hour"}]
    assert [r["path"] for r in data["rejected"]] == [REBAL]
    assert data["overlay_path"] == res.change.overlay_path and Path(data["overlay_path"]).exists()
    json.dumps(data)  # JSON-safe for events.jsonl

    # the overlay reproduces the new config version (same hash) when loaded on top of configs/
    reloaded = load_config(env={}, runtime_overlay=Path(data["overlay_path"]))
    assert config_hash(reloaded) == h1
    assert f"- config_hash: `{h0}` -> `{h1}`\n" in rig.journal.read_text()


# ---------------------------------------------------------------- journal


async def test_journal_is_appended_never_overwritten(tmp_path: Path, cfg: Config, sim_clock: SimClock,
                                                     t0: float) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply("## What worked\nS1 lock-ins.", [sug(MARGIN, 0.0012)]),
                                              reply("Quiet hour, no change.")])
    rig.journal.parent.mkdir(parents=True)
    previous = "# Trading journal\n\n## Review 2026-10-22T08:00:00.000Z\n\nearlier entry\n"
    rig.journal.write_text(previous)
    h0 = config_hash(cfg)

    await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    h1 = config_hash(rig.cm.cfg)
    sim_clock.advance(3600)
    await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))

    text = rig.journal.read_text()
    assert text.startswith(previous)
    headers = re.findall(r"^## (Review .*)$", text, re.M)
    assert headers == ["Review 2026-10-22T08:00:00.000Z", f"Review {iso(t0)}", f"Review {iso(t0 + 3600)}"]
    assert f"- config_hash: `{h0}` -> `{h1}`\n" in text
    assert f"- config_hash: `{h1}` -> `{h1}` (unchanged)" in text
    assert "### What worked" in text  # the model's headings are nested under our section
    assert f"- window: {iso(t0 - 3600)} .. {iso(t0)}" in text
    assert f"- llm: openrouter/{MODEL}, cache key `" in text


async def test_fresh_journal_gets_a_header_once(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [reply(), reply("second")])
    await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    sim_clock.advance(10)
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    text = rig.journal.read_text()
    assert text.startswith(JOURNAL_HEADER) and text.count("# Trading journal") == 1
    assert text.endswith(res.entry) and res.journal == "second"


# ---------------------------------------------------------------- failures


async def test_invalid_json_retries_once_then_skips(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    bad_extra = json.dumps({"journal": "x", "param_suggestions": [sug(MARGIN, 0.001)], "alerts": [], "extra": 1})
    rig = make_rig(tmp_path, cfg, sim_clock, ["Sure! Here is my review: not json", bad_extra])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))

    assert len(rig.provider.calls) == 2
    retry_msgs = rig.provider.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "Sure! Here is my review: not json"}
    assert "Fix the JSON" in retry_msgs[-1]["content"]
    assert not res.raw_ok and res.change is None and res.suggestions == []
    assert res.journal.startswith("reviewer skipped: llm_invalid")
    assert "reviewer skipped: llm_invalid" in rig.journal.read_text()
    assert config_hash(rig.cm.cfg) == config_hash(cfg) and rig.events == [] and rig.cm.last_apply_ts is None


async def test_invalid_json_then_valid_retry_is_applied(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, ['```json\n{"journal": "oops", ', reply(suggestions=[sug(MARGIN, 0.0012)])])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res.raw_ok and res.change is not None and len(res.change.accepted) == 1
    assert len(rig.provider.calls) == 2 and len(rig.events) == 1


async def test_llm_none_writes_skip_entry(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    cm = ConfigManager(cfg, runtime_dir=tmp_path / "runtime")
    events: list[Any] = []
    journal = tmp_path / "journal.md"
    reviewer = PostTradeReviewer(cfg, None, cm, sim_clock, journal_path=journal,
                                 on_event=lambda *a: events.append(a))
    res = await reviewer.run(reviewer.build_input([], [], [], {}))
    assert res.journal == "reviewer skipped: llm not configured"
    assert res.change is None and not res.raw_ok and res.skipped_reason == "llm not configured"
    text = journal.read_text()
    assert "reviewer skipped: llm not configured" in text and f"## Review {iso(sim_clock.now())}" in text
    assert events == [] and cm.cfg is cfg

    off = apply_changes(cfg, {"ai.enabled": False})
    rig = make_rig(tmp_path, off, sim_clock, [reply(suggestions=[sug(MARGIN, 0.0012)])], sub="off")
    res2 = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert res2.journal == "reviewer skipped: ai disabled (ai.enabled=false)" and rig.provider.calls == []


async def test_provider_down_skips_and_never_leaks_keys(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    # a (hypothetically) leaky provider error must not put a key into the journal
    rig = make_rig(tmp_path, cfg, sim_clock, [ProviderError(f"HTTP 401: bad key {SECRET}")], secrets=[SECRET])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert not res.raw_ok and res.change is None
    assert res.journal.startswith("reviewer skipped: openrouter: ProviderError")
    text = rig.journal.read_text()
    assert SECRET not in text and SECRET not in res.journal and "***" in text
    assert rig.events == [] and len(rig.llm.cache) == 0


async def test_unexpected_provider_exception_degrades(tmp_path: Path, cfg: Config, sim_clock: SimClock) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [KeyError("choices")])
    res = await rig.reviewer.run(rig.reviewer.build_input([], [], [], {}))
    assert not res.raw_ok and res.journal.startswith("reviewer skipped: KeyError")


# ---------------------------------------------------------------- replay


async def test_replay_reproduces_review_from_cache_with_zero_calls(tmp_path: Path, cfg: Config, t0: float) -> None:
    decisions, plans, health, portfolio = sample(t0)
    live = make_rig(tmp_path, cfg, SimClock(t0 + 3600), [
        reply(suggestions=[sug(MARGIN, 0.0012), sug(REBAL, 0.5)], alerts=["2 FEE_BUDGET vetoes"])])
    r1 = await live.reviewer.run(live.reviewer.build_input(decisions, plans, health, portfolio))
    assert r1.raw_ok and not r1.cached and len(live.llm.cache) == 1

    replay = make_rig(tmp_path, cfg, SimClock(t0 + 3600), [], sub="replay", mode="replay", strict=True)
    r2 = await replay.reviewer.run(replay.reviewer.build_input(decisions, plans, health, portfolio))
    assert replay.provider.calls == [] and r2.cached
    assert r2.entry == r1.entry
    assert replay.journal.read_text() == live.journal.read_text()
    assert r1.change is not None and r2.change is not None
    assert r2.change.accepted == r1.change.accepted and r2.change.new_hash == r1.change.new_hash
    assert [e[2]["new_hash"] for e in replay.events] == [e[2]["new_hash"] for e in live.events]

    # a request that was never recorded is a hard error in strict replay (determinism, not an outage)
    replay.clock.advance(60)
    with pytest.raises(CacheMissError):
        await replay.reviewer.run(replay.reviewer.build_input(decisions, plans, health, portfolio))


# ---------------------------------------------------------------- final


async def test_run_final_writes_summary_without_config_changes(tmp_path: Path, cfg: Config, sim_clock: SimClock,
                                                               t0: float) -> None:
    rig = make_rig(tmp_path, cfg, sim_clock, [
        reply("Final: +0.25% in TMN. S1 carried the result; S3 lost one stop.", [sug(MARGIN, 0.0012)])])
    sim_clock.set(t0 + 3600)
    res = await rig.reviewer.run_final(rig.reviewer.build_input(*sample(t0), window_s=86400))
    assert res.final and res.raw_ok and res.change is None
    assert config_hash(rig.cm.cfg) == config_hash(cfg) and rig.events == [] and rig.cm.last_apply_ts is None
    system = rig.provider.calls[0]["messages"][0]["content"]
    assert "Final review (end of the challenge)" in system and f"`{MARGIN}`" not in system
    assert _schema_from_prompt(system)["properties"]["param_suggestions"]["maxItems"] == 0
    text = rig.journal.read_text()
    assert f"## Final summary {iso(t0 + 3600)}" in text and "S1 carried the result" in text
    assert f"- `{MARGIN}` = 0.0012: final review: configuration is frozen" in text


async def test_run_final_without_llm_writes_deterministic_facts(tmp_path: Path, cfg: Config, sim_clock: SimClock,
                                                                t0: float) -> None:
    reviewer = PostTradeReviewer(cfg, None, ConfigManager(cfg, runtime_dir=tmp_path / "rt"), sim_clock,
                                 journal_path=tmp_path / "journal.md")
    sim_clock.set(t0 + 3600)
    res = await reviewer.run_final(reviewer.build_input(*sample(t0)))
    assert res.final and res.change is None
    assert res.journal.startswith("reviewer skipped: llm not configured")
    assert "- S1: 2 trades (2 won, 0 lost), pnl 1250.5 TMN" in res.journal
    assert "- fees: paid 25000 TMN of budget 100000 TMN (used 0.25)" in res.journal
    assert "- vetoes: 3 (FEE_BUDGET×2, PRICE_SANITY×1)" in res.journal
    assert "## Final summary" in (tmp_path / "journal.md").read_text()
