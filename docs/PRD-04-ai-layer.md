# PRD-04 — AI Layer: Regime Analyst, Post-trade Reviewer, Wallex MCP Client, LLM Cache & Replay

**Depends on:** PRD-01…03. **Related:** PRD-05 (ablation, replay).
**Goal:** AI that demonstrably changes trading decisions, is auditable line-by-line, is reproducible offline, and cannot bypass risk.

Scoring targets: real AI usage 35 pts, strategy logic 40 pts (explanations), re-runnability 25 pts.

---

## 1. Principles

1. **LLM in the slow loop only** (5-min analyst, hourly reviewer). The 2-s loop never waits on a model.
2. **LLM outputs are plans, not orders.** A plan is a bounded envelope the deterministic strategies operate inside. RiskManager treats plan values as untrusted input (clamped).
3. **Every call is cached and replayable.** `replay` mode must produce identical plans with zero network.
4. **Ablation is first-class.** `ai.enabled=false` ⇒ rules-only defaults; backtests report both.
5. **Explain for a judge.** Plan `notes` are written to be read aloud in the demo video.

---

## 2. LLM client (`wta/ai/llm_client.py`)

- Providers: `openrouter` (OpenAI-compatible `/v1/chat/completions`, header `Authorization: Bearer`, `HTTP-Referer`/`X-Title` optional) and `gemini` (`generateContent` with `responseMimeType: application/json` + `responseSchema`). Abstraction: `chat(messages, tools=None, response_schema=None, model=..., temperature=0.0, max_tokens=...) -> LLMResponse(text, tool_calls, usage, model, latency_ms)`.
- **Structured output:** request JSON mode / schema where supported; always validate with pydantic; on validation failure retry once with the error appended ("fix the JSON"); on second failure return `None` (engine uses the last valid plan, or rules-only if none; log `llm_invalid`).
- **Timeouts:** 40 s analyst, 90 s reviewer. **Failover:** on provider error/timeout → fallback provider once → else `None`.
- **Cost guard:** max 400 analyst calls/day (every 5 min = 288) and 30 reviewer calls/day; token usage logged.
- Models are configured in `configs/llm.yaml` (`analyst.model`, `reviewer.model`, `fallback.provider/model`). Keep temperature 0; set `seed` if the provider supports it.

## 3. Cache (`wta/ai/cache.py`)

- Key: `sha256(provider + model + canonical_json(messages) + canonical_json(tools) + str(temperature))`.
- Value file `logs/llm_cache/<key>.json`: `{request, response, ts, latency_ms, usage}`.
- Lookup before every call in all modes. In `replay`, a cache miss is a **hard error** when `ai.replay_strict=true` (prove determinism); in `backtest`, misses call the model and populate the cache (so a backtest becomes reproducible after its first run).
- Tool-call loops: every intermediate message (tool results included) is part of the key for the next call, so the whole agentic trajectory replays.
- `deliverables/llm_cache/` is committed at the end (small JSON files; redact nothing — there are no secrets in prompts; verify with a test that no `sk-`/API key patterns appear).

## 4. Wallex MCP client (`wta/ai/mcp_client.py`)

- Streamable-HTTP MCP client (JSON-RPC 2.0 over `POST https://api.wallex.ir/mcp`; `Accept: application/json, text/event-stream`; handle SSE-framed responses). `initialize` → `tools/list` → `tools/call`. Auth: `X-API-Key` header (verify in `check_account.py`; if the server expects a different header, make it configurable `WALLEX_MCP_AUTH_HEADER`).
- Expose a **whitelisted, read-only** subset to the analyst:
  `wallex_basic_get_markets, wallex_basic_get_market_depth, wallex_basic_candle_history, wallex_basic_get_latest_trades, wallex_basic_get_account_balances, wallex_basic_get_account_fees, wallex_spot_get_open_order, wallex_spot_get_orders`.
  **Never** expose state-changing tools (`wallex_spot_post_account_orders`, `*delete*`, `wallex_credit_*`, `wallex_margin_*`, `wallex_otc_*`, IBAN/card tools) to the model. Enforce by an allowlist in code + a test.
- Tool results are truncated to ≤ 4 kB each (top 10 levels, last 50 candles) before being fed to the model.
- If MCP is unreachable, the analyst falls back to **internal tools** that read the same data from our bus/store (same tool names, suffix `_local`), so the slow loop never depends on MCP uptime. The plan records `tools_source: mcp|local`.

## 5. Internal tools (`wta/ai/tools.py`)

Read-only functions exposed to the model with JSON schemas:
- `get_features(symbols)` → FeatureSets (§2 of PRD-03), including cross-book edges and costs.
- `get_pnl_summary()` → equity, drawdown, daily PnL, realized by strategy, fees paid vs budget, open positions.
- `get_recent_decisions(n=20)` → compact decision records (intent reason, risk result, outcome).
- `get_health()` → data freshness, API errors, paused/killed flags, flatten phase, time to deadline.
- `get_candle_stats(symbol, resolution, n)` → returns, ATR%, EMA state, volume ratio (computed, not raw).

## 6. Regime Analyst (`wta/ai/analyst.py`)

**Cadence:** every `slow_loop_interval_s` (300 s) and on triggers: USDTTMN 15-min move > 1%, BTCUSDT 15-min move > 2%, kill/pause events (for a post-mortem note), flatten start.

**Agent loop (hand-written, ≤ 6 tool rounds):**
```
system: role, rules, output schema, bounds, what each strategy does, current config values
user:   compact state (features for watchlist, pnl summary, health, last plan) + "Decide the plan."
loop:   model may call tools (MCP/internal) → append results → until it returns final JSON
```
**Output schema (`MarketPlan`)** — strict, additionalProperties=false:
```json
{
  "regimes": {
    "USDTTMN": {"regime": "range|trend_up|trend_down|chaos", "confidence": 0.0, "evidence": "..."},
    "BTCUSDT": {...}, "ETHUSDT": {...}, "SOLUSDT": {...}
  },
  "enabled_strategies": ["S1","S2","S3"],
  "s1_margin_min_pct": 0.0015,
  "size_multiplier": 0.8,
  "usdt_target_share": 0.55,
  "risk_flags": ["high_dollar_volatility"],
  "notes": "2-4 sentences a judge can read: what you saw, what you decided, why."
}
```
**Validation & clamping (deterministic, in code):** `s1_margin_min_pct ∈ [0.0008, 0.01]`, `size_multiplier ∈ [0, 1]`, `usdt_target_share ∈ bounds [0.25, 0.70]`, `enabled_strategies ⊆ configured-enabled`, confidence ∈ [0,1]. Clamped values are logged with `clamped: true` and the original value.
**Persistence:** `plans` table + `decisions.jsonl` record `kind="plan"` with `prompt_hash`, `model`, `cached`, `tools_used[]`, `latency_ms`, full `notes`.
**Staleness:** a plan older than 20 min is ignored (strategies fall back to rules-only defaults with `plan=None`), logged once.
**Prompt content (`wta/ai/prompts/analyst.md`)** must include: challenge rules summary (TMN valuation, deadline), fee table, strategy descriptions with their parameters and bounds, the exact JSON schema, "do not invent prices — use tools", "prefer size_multiplier 0 in chaos", "explain in notes", and the current time-to-deadline.

## 7. Post-trade Reviewer (`wta/ai/reviewer.py`)

**Cadence:** hourly (and at the end of the challenge for the final report).
**Input:** last hour's decisions with outcomes, PnL by strategy, fee usage, vetoes by code, plan history, health incidents.
**Output schema:**
```json
{
  "journal": "markdown, 5-15 lines: what worked, what didn't, anomalies",
  "param_suggestions": [
    {"path": "strategies.s1_crossbook.margin_min_pct", "value": 0.0012, "rationale": "..."},
    {"path": "strategies.s2_usdtalloc.rebalance_threshold_pp", "value": 0.12, "rationale": "..."}
  ],
  "alerts": ["string"]
}
```
**Bounded auto-apply:** `configs/llm.yaml` declares `reviewer.editable` with bounds, e.g.
```yaml
reviewer:
  editable:
    strategies.s1_crossbook.margin_min_pct: [0.0008, 0.005]
    strategies.s1_crossbook.leg1_taker: [true, false]
    strategies.s2_usdtalloc.rebalance_threshold_pp: [0.10, 0.25]
    strategies.s3_momentum.min_regime_confidence: [0.5, 0.9]
    ai.slow_loop_interval_s: [180, 900]
  max_changes_per_review: 2
  cooldown_s: 3600
```
Suggestions outside the list/bounds are logged and ignored. Accepted ones produce a new config version: write `configs/runtime/config-<ts>.yaml`, recompute `config_hash`, log `events: config_change {old_hash, new_hash, diff, source: reviewer}`. Risk parameters are **never** editable by the reviewer.
Journal entries are appended to `logs/journal.md` and later copied to `deliverables/`.

## 8. Engine integration

- Slow loop task: `plan = await analyst.run(ctx)`; `engine.plan = plan` (atomic swap); strategies read `ctx.plan` on the next fast tick.
- Reviewer task hourly → `config_manager.apply(suggestions)` → `engine.reload_config()` (strategies pick up new values; RiskManager unaffected).
- All AI failures degrade gracefully to rules-only; the health line shows `llm_ok=false` and `plan_age_s`.

## 9. Ablation support (used by PRD-05)

- `ai.enabled=false` ⇒ `plan=None` always; S2 uses `rule_regime()`; S3 disabled (needs regime) unless `s3.allow_rules_only=true` (then uses `rule_regime` on BTCUSDT); S1 uses config margins.
- Backtest runner can run the analyst on historical windows (tools → `_local` reading the store at the simulated time), caching each call. Report compares `ai=on` vs `ai=off`: return, max DD, number of trades, vetoes, fee paid.

## 10. Tests

- `test_llm_client.py`: provider abstraction with mocked HTTP; failover; invalid JSON retry → None.
- `test_cache.py`: key stability, hit/miss, replay-strict error, no-secret scan.
- `test_mcp_client.py`: JSON-RPC framing (mock server), allowlist enforcement (state-changing tool names rejected even if the model requests them), truncation.
- `test_analyst.py`: schema validation, clamping, staleness, tool loop cap, plan persisted to decisions.
- `test_reviewer.py`: bounded apply, cooldown, ignored out-of-bounds, config version + hash change event.

## 11. Acceptance checklist

- [ ] In shadow: a plan every 5 min with non-empty `notes`; `tools_used` includes at least one Wallex MCP tool when MCP is reachable.
- [ ] Kill network → next analyst cycle degrades to rules-only without exceptions; health shows it.
- [ ] `python -m wta replay` over a 2-h window reproduces every plan from cache (0 network calls, asserted).
- [ ] One reviewer cycle applies ≤ 2 bounded changes and the `config_hash` in subsequent decisions changes accordingly.
- [ ] `docs/ARCHITECTURE.md` section "How AI is used" written from this PRD with one real plan example pasted in.
- [ ] Tag `v0.4-ai`.
