# Decision log format

**Summary**
1. Every decision is one JSON line in `<log dir>/decisions-YYYYMMDD.jsonl`: an order, a risk veto, a router drop (`no_trade`) or an AI plan (`plan`). `<log dir>/decisions.jsonl` is a symlink to the current UTC day's file.
2. Results come later as separate `kind: "outcome"` lines that point back by `decision_id`. Earlier lines are never rewritten; `merge_decisions` joins them.
3. An order or veto record holds the intent and its reason, the full 23-check risk list, the order request and the exchange's answer, the features the strategy saw and the plan in force, plus `mode`, `git_commit` and `config_hash`. A plan record holds the model, `prompt_hash`, the tools used and the whole plan.
4. `orders.jsonl` (order traffic), `events.jsonl` (engine events) and `health.jsonl` (heartbeats) complete the trail; `wta logs check` validates all four. The state DB keeps the replay inputs (`ticks`, `engine_books`, `engine_candles`).
5. The examples are real records of the 4-h shadow run (SYNTHETIC feed, real OpenRouter LLM) and of runs at `v0.5-backtest` on the synthetic feed and on crafted test books. None is live: no live order has been placed yet, and `api.wallex.ir` is reachable only intermittently.

This file describes the code at tag `v0.5-backtest` (`85acc16`): `src/wta/models.py`,
`src/wta/logging_/`, `src/wta/execution/order_manager.py`, `src/wta/engine.py`. Where the code
differs from the PRDs, this file follows the code and §10 lists the differences.

## 1. Files and how they are written

| Family | Model (`src/wta/models.py`) | Writer | One line per |
|---|---|---|---|
| `decisions` | `DecisionRecord`, `OutcomeUpdate` | `DecisionLog` (`logging_/decision_log.py`) | decision, or outcome update |
| `orders` | `OrderLogRecord` | `OrdersLog` (same file) | placement, answer, cancel, status change, fill, error |
| `events` | `EventRecord` | `EventsLog` (same file) | engine event (start, kill, flatten, plan failure, ...) |
| `health` | `HealthRecord` | `HealthLog` (`logging_/health_log.py`) | heartbeat (every `engine.heartbeat_interval_s` = 60 s, and one at shutdown) |

Where they go: `wta run --mode live` writes to `logs/`, `wta run` (shadow) to `logs/shadow/`, any
run to its `--log-dir`; a backtest to its `--out`; a replay to its `--out` (default a temporary
directory). Each family is a set of files `<family>-YYYYMMDD.jsonl` plus the relative symlink
`<family>.jsonl -> <family>-YYYYMMDD.jsonl`. All four share one writer, `JsonlWriter`
(`logging_/jsonl.py`):

| Property | Implementation |
|---|---|
| Append-only | Files are opened in `"a"` mode, line-buffered and flushed after every line. Nothing is ever truncated or rewritten; a correction or result is a new line. |
| Daily rotation | The file is chosen at each write from the UTC day of the engine's clock, so a backtest or replay names files by simulated date. |
| Current-day link | When a new day's file is opened, `<family>.jsonl` is re-pointed atomically. A real file with that name (e.g. a copied deliverable) is never overwritten. |
| Encoding | Every line is built through its pydantic model (`extra="forbid"`), then written as canonical JSON: sorted keys, no whitespace, UTF-8, one object per line. The same record always gives the same bytes (replay compares lines). |
| Secrets | Each configured secret (Wallex, OpenRouter, Gemini keys, Telegram token; ≥ 4 chars), raw and JSON-escaped, is replaced by `***` before the line reaches the disk. |
| Time | `ts` is ISO-8601 UTC with milliseconds (`2026-10-22T23:24:23.730Z`). During a tick the clock is frozen at the tick's start, so all lines of one tick share one `ts`. A plan record is stamped with its analyst cycle's time. |
| Reading | `family_files(log_dir, family)`: a plain `<family>.jsonl` first if it is a regular file, then the dated files in date order. The symlink is skipped, so nothing is read twice. |

Value conventions: money and quantities are **strings** (Python `Decimal`), sometimes in exponent
form (a fee of `"2.73E-7"` BTC); parse them as decimals, never as floats. `*_pct` fields and
`usdt_share` are **fractions** (`0.008` = 0.8 %); only risk-check `detail` texts print percentages.
Only the top-level `ts` of a line is an ISO string; `intent.ts`, `*_ts` fields inside objects and
fill `ts` are Unix seconds (floats). Plan confidences are JSON numbers; plan sizes and shares are
Decimal strings.

## 2. `decisions.jsonl`

### 2.1 Record kinds

| `kind` | Written by | Meaning |
|---|---|---|
| `order` | `OrderManager._record` | Risk approved the intent. `order.accepted` says whether the exchange took it. When the manager refused before sending (unknown market, no book, below minimum, self-cross guard), `order` is `null` and `notes` starts with `REFUSED:` or `SKIPPED:`. Auto-placed server stops are `order` records too. |
| `veto` | `OrderManager.submit` | The RiskManager said no. Nothing was sent. |
| `no_trade` | `Engine._log_router_drops` | The strategy router dropped the intent (one strategy per symbol, one open order per strategy and symbol, symbol cooldown). `notes`: `dropped by the strategy router: <why>; not sent to the RiskManager`. |
| `plan` | `Engine._on_plan` | One Regime Analyst plan (§2.4). `strategy: "AI"`, `symbol: "PLAN"`. |
| `outcome` | `DecisionLog.outcome` | An `OutcomeUpdate` (§2.5), not a `DecisionRecord`. |
| `intent`, `event` | nothing | Defined in `DecisionKind`, not written. |

Repeats: an identical veto or router drop of the same intent within 60 s (`VETO_REPEAT_S`) is not
logged again; the next logged one carries `features.veto_repeats_suppressed`.

### 2.2 `DecisionRecord` fields

| Field | Type | Content |
|---|---|---|
| `kind` | string | See §2.1. |
| `decision_id` | string | Equals `intent.id`. Links the record to its outcome and to `orders.jsonl` (§2.6). |
| `ts` | ISO string | Tick time (cycle time for a plan). |
| `mode` | string | `shadow`, `live`, `backtest` or `replay`. |
| `git_commit` | 40 hex chars | `git rev-parse HEAD` at process start, or `unknown`. See §8. |
| `config_hash` | 12 hex chars | `sha256(canonical_json(resolved config))[:12]` of the config in force; it changes after an accepted reviewer change. See §8. |
| `strategy`, `symbol` | string | `S1`/`S2`/`S3`/`FLATTEN`/`AI`, or the name in an injected intent; e.g. `BTCUSDT`, `PLAN`. |
| `features` | object | Strategy records: `FeatureSnapshot.compact` of the books the intent used (`cross` per S1 coin, `dollar` = USDTTMN, `symbols`, `ts`). Injected intents: their own features (`inject_line`, `injected`). Plans: the analyst record (§2.4). |
| `plan_snapshot` | object or null | The `MarketPlan` in force, if it is fresh (≤ `ai.plan_stale_s`); `null` without a plan (rules-only, `ai.enabled: false`, or a stale plan). On a plan record: the plan itself. |
| `intent`, `risk` | object | The full `Intent` and the `RiskDecision` (§2.3). `intent: null` and `risk: {}` on plan records. |
| `order` | object or null | Request and exchange answer (§2.3). `null` for vetoes, drops, plans and refusals. |
| `outcome` | null | Always `null` in a decision line; results arrive as outcome lines. |
| `notes` | string | Veto reason, `REFUSED`/`SKIPPED`/`REJECTED` reason, server-stop placement, `server stop ... canceled first`, the router's drop reason, or the plan's notes. |

### 2.3 Sub-objects: `intent`, `risk`, `order`

`intent` (model `Intent`): `id`, `ts`, `strategy`, `symbol`, `side` (`BUY`/`SELL`), `order_type`
(`LIMIT`/`MARKET`/`STOP_LIMIT`/`STOP_MARKET`), `qty` or `notional` (in the quote currency),
`price` (`null` = the OrderManager prices it from the book), `stop_price`, `purpose` (`entry`,
`exit`, `stop`, `rebalance`, `flatten`, `leg2`, `probe`), `reason` (the strategy's own explanation
with its numbers, copied verbatim), `features` (the strategy's inputs and sizing: for S1 the edges,
costs, `need_pct`, `cost_model`, `leg2_symbol`, `sizing`; for S2 the share, target, regime and its
source), `expected_edge_pct`, `linked_position_id` (exits, stops, leg 2) and `taker` (true = cross
the spread).

`risk` (model `RiskDecision`):

| Key | Content |
|---|---|
| `approved` | True only when no check failed. |
| `checks` | Every check, passed or not, in a fixed order: `{code, passed, detail}`. An evaluated intent has 23: `KILLED, PAUSED, DEADLINE, BANNED_MARKET, MARKET_ORDER_DISABLED, MIN_NOTIONAL, MAX_ORDER_PCT, BEST_LEVEL_QTY, COIN_EXPOSURE, USDT_BOUNDS, MAX_POSITIONS, MAX_OPEN_ORDERS, RATE_HOUR, RATE_DAY, DAILY_LOSS, DRAWDOWN_KILL, FEE_BUDGET, EDGE_VS_COST, STALE_DATA, PRICE_SANITY, SELF_CROSS, BALANCE, DUPLICATE`. A second `MIN_NOTIONAL` ("after adjustments", or "lock-in leg 2 on …") is appended when sizing made the order (or its leg 2) too small. Rules that do not apply say `n/a (...)` and pass. |
| `adjusted_qty` | Quantity after risk sizing, rounded down to the market step. Present on vetoes too; `null` when the intent could not be sized and on the fixed-check records below. |
| `veto_reason` | `"<first failed code>: <its detail>"`, or `null`. |

Two kinds of order skip the RiskManager and carry one fixed check: `PROTECTIVE_STOP` (the server
stop after a filled, risk-approved entry; it only reduces exposure) and `ADOPTED` (our order found
on the exchange without a stored request). The rules are in [`RISK.md`](RISK.md).

`order` (built by `OrderManager._record`):

| Key | Content |
|---|---|
| `request` | The `OrderRequest` sent: `client_id`, `decision_id`, `symbol`, `side`, `type`, `qty` (rounded down to the step), `price`, `stop_price`, `strategy`, `purpose`. |
| `accepted` | `true` if the exchange accepted it, else `false` with `notes: "REJECTED: <code> <message>"`. |
| `response` | Only when accepted: the exchange's answer to the placement (`exchange_id`, `status`, `filled_qty`, `avg_price`, `fee_paid`, `fee_asset`, `created_ts`, `updated_ts`, `reprices`). |

Re-prices and crosses do not write new decision lines. Each new placement gets a new `client_id` and
shows up in `orders.jsonl` and in the outcome's `client_ids`.

### 2.4 Plan records (`kind: "plan"`)

Written for every analyst cycle that produced a plan (a failed cycle writes no plan record, only
events; §5). `decision_id` = `d_<cycle ms>_AI_PLAN_<n>`.

| Key | Content |
|---|---|
| `features.ai_source` | `llm` (a real model), `offline-rules` (the deterministic rules provider, NOT an LLM; then also `features.ai_label: "AI: offline rules (no LLM key)"`) |
| `features.model` | e.g. `openai/gpt-5.4-mini`; `offline-rules-v1` for the rules provider |
| `features.prompt_hash` | the LLM cache key of the cycle's first request (`<log dir>/llm_cache/<prompt_hash>.json`) |
| `features.calls` | every model request of the cycle: `{cache_key, provider, model}` |
| `features.reason` | the trigger: `startup`, `scheduled`, `event:<kind>`, `move:<symbol>`, `manual` |
| `features.tools_used`, `tools_source`, `tool_rounds` | tool names called; `mcp`, `local` or `none`; tool rounds |
| `features.llm_calls`, `json_retry`, `latency_ms`, `usage`, `cached` | request count; whether the "fix the JSON" retry was needed; wall time; tokens; served from the cache |
| `features.clamped` | `{field: original value}` for every value `clamp_plan` changed |
| `plan_snapshot` | the `MarketPlan`: `regimes` (USDTTMN, BTCUSDT, ETHUSDT, SOLUSDT: `regime`, `confidence`, `evidence`), `enabled_strategies`, `s1_margin_min_pct`, `size_multiplier`, `usdt_target_share`, `risk_flags`, `notes`, plus the audit fields above and `ts` |
| `notes` | the plan's notes |

The same plan also goes into the state DB's `plans` table. Every later strategy record carries it
in `plan_snapshot` while it is fresh.

### 2.5 Outcome updates (`OutcomeUpdate`)

An update is `{"kind": "outcome", "decision_id": "<earlier decision>", "ts": "<ISO>", "outcome": {...}}`.

**Position outcome:** on the *entry* decision id, written when the position closes (by a fill or by
the Reconciler). Keys: `pnl_tmn`, `pnl_pct`, `hold_s`, `closed_ts`, `exit_reason` (`target`, `stop`,
`time_stop`, `lockin`, `flatten`, `kill`, ...), `position_id`, `strategy`, `symbol`. Optional:
`dust_qty` (unsellable remainder left on the account), and `pnl_complete: false` with a `note` when a
leg could not be valued in TMN. Such a PnL is not added to the realized total.

```
value_in_tmn  = Σ entry fills   qty × price, converted to TMN at the USDTTMN bid at fill time
value_out_tmn = Σ closing fills qty × price (same conversion) + dust_qty × closing price
fees_tmn      = Σ fees in TMN − fees already taken out of the coin quantity
pnl_tmn       = value_out_tmn − value_in_tmn − fees_tmn
pnl_pct       = pnl_tmn / value_in_tmn                      (fraction)
hold_s        = closed_ts − time of the first entry fill
```

**Order outcome** (`outcome.kind = "order"`): on the decision id of every other logical order when it
ends: exits, stops, `leg2`, flatten, rebalances, and entries that never filled. A filled entry gets
no order outcome; its position outcome reports instead. Keys: `status` (`FILLED`, `PARTIALLY_FILLED`,
`UNFILLED`), `done_reason`, `filled_qty`, `qty`, `avg_price` (volume-weighted over all placements),
`fee_paid`, `fee_asset`, `symbol`, `side`, `purpose`, `reprices`, `crossed`, `client_ids` (every
placement), `done_ts`. Optional: `position_id`, `detail`. `done_reason` is one of `FILLED`;
`EXPIRED_UNFILLED` (entry, rebalance or leg 2 given up: out of re-prices, `limit_cancel_after_s`, or
a lock-in leg that never re-prices); `CANCELED_FOR_EXIT` (a server stop canceled so a closing SELL
could take its coins); `POSITION_CLOSED`; `DUST_REMAINDER`; `NOT_FOUND` (unknown to the exchange
after 3 polls); `REPLACE_REJECTED`; `SELF_CROSS_GUARD`; `ADOPTED_UNKNOWN`; `EXCHANGE_<status>`.

### 2.6 Identifiers and how records link

| Id | Format | Example (§3) |
|---|---|---|
| Strategy / exit / flatten decision | `d_<tick ms>_<strategy>_<symbol>_<seq>` | `d_1792711463731_S2_USDTTMN_1` |
| Plan | `d_<cycle ms>_AI_PLAN_<n>` | `d_1792712035450_AI_PLAN_9` |
| Injected decision (tests) | `d_<ms>_<strategy>_<symbol>_inj<line>` | `d_1792739460254_S3_ETHUSDT_inj4` |
| Protective server stop | `<entry decision id>_stop<n>` | `d_1792739450239_S3_BTCUSDT_inj3_stop1` |
| `client_id` | `<S1/S2/S3/FL/EX/PR>_<SYMBOL>_<13-digit ms>_<4 hex>` | `S1_BTCTMN_1792663212000_DC11` |
| Position | `p_<strategy>_<symbol>_<ms>_<seq>` | `p_S1_BTCTMN_1792663214000_1` |

The 4-hex nonce is `sha1(decision_id | attempt)[:4]`, so a replay produces the same client ids
(Wallex allows only letters, digits and `_`). Links: `decision_id` = `order.request.decision_id` =
`data.decision_id` of the fills in `orders.jsonl`. The outcome's `position_id` =
`intent.linked_position_id` of the position's exits, stops and leg 2. An S1 leg 2 also names its
cycle in `intent.features.cycle` (the leg-1 decision id).

### 2.7 Joining decisions and outcomes: `merge_decisions`

```python
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
merged = merge_decisions(iter_family("deliverables/shadow_synth_4h", "decisions"))   # decision_id -> merged record
merged["d_1792711463731_S2_USDTTMN_1"]["outcome"]["done_reason"]; merged.orphans
```

Several decision records with the same id: later non-empty fields overlay earlier ones. An outcome
replaces `outcome` as a whole; the last one wins. Outcomes whose id never appeared are kept,
unchanged, in `merged.orphans`. Input dicts are not modified.

## 3. Annotated examples

**Read this first.** No example comes from a live run or from real Wallex books. Examples 1 and 2
are real records of the 4-h shadow run: the **SYNTHETIC feed** with the **real OpenRouter LLM**
(`logs/shadow_synth_4h`, commit `7e8f2f5`, clean tree, `config_hash 481cc6dc3ce2`; replayed 40/40;
the decision, order and event files, the journal and the LLM cache are committed in
`deliverables/shadow_synth_4h/`).
Example 3 runs the real engine on **crafted books in the test rig**. Example 4 uses an **injected
test intent** on the synthetic feed. They show the format, not strategy performance.

For reading, keys are grouped (the files sort them alphabetically), small objects are put on one
line, `//` comments are added and `/* ... */` marks removed parts. Values are unchanged.

What the 4-h run wrote: 38 `plan` records, 2 S2 `order` records and their 2 outcomes; no veto, no S1
or S3 record (the plans kept S1 off in all 38 cycles; S3 is disabled in config).

### 3.1 Example 1: a real LLM plan (4-h run, line 13 of `decisions-20261022.jsonl`)

```jsonc
{
  "kind": "plan", "decision_id": "d_1792712035450_AI_PLAN_9",
  "ts": "2026-10-22T23:33:55.449Z",                    // the analyst cycle's time
  "mode": "shadow", "strategy": "AI", "symbol": "PLAN",
  "git_commit": "7e8f2f5af1c55c3d744c6c8de22a454040b072f9", "config_hash": "481cc6dc3ce2",
  "features": {
    "ai_source": "llm",                                // a real model, not the offline rules provider
    "model": "openai/gpt-5.4-mini", "reason": "scheduled", "cached": false,
    "prompt_hash": "e1434de491e1f0c1907197fd0c664e4688d1d8ee932f2840af8942834c088b73",
    "calls": [                                         // 3 requests: tool round, answer, JSON fix
      {"cache_key": "e1434de491e1f0c1907197fd0c664e4688d1d8ee932f2840af8942834c088b73", "model": "openai/gpt-5.4-mini", "provider": "openrouter"},
      {"cache_key": "eb2cf179fab7d5923eeffab83ce69a507f5028ac7663c98306b4e02823f68761", "model": "openai/gpt-5.4-mini", "provider": "openrouter"},
      {"cache_key": "9a02ba47027365ca35ad50c8026cdf8d1a5f7dd6e1b41c5a780091c4489f11e2", "model": "openai/gpt-5.4-mini", "provider": "openrouter"}],
    "llm_calls": 3, "tool_rounds": 1,
    "tools_used": ["get_candle_stats", "get_features", "get_health", "get_pnl_summary"],
    "tools_source": "local",                           // synthetic feed: the *_local tools, no Wallex MCP
    "json_retry": true,                                // the first answer did not validate; the retry did
    "clamped": {},                                     // nothing had to be clamped
    "latency_ms": 8996,
    "usage": {"completion_tokens": 926, "prompt_tokens": 16931, "total_tokens": 17857} },
  "plan_snapshot": {
    "enabled_strategies": ["S2"],                      // S1 off: no new S1 entries while this plan is fresh
    "size_multiplier": "0.35",                         // entry size x 0.35 (S1, S3); was 0 in plans 1-8
    "s1_margin_min_pct": "0.0018",                     // 0.18 % > config 0.15 %: S1 would be stricter
    "usdt_target_share": "0.5",                        // S2 target, inside the [0.25, 0.70] bounds
    "regimes": {
      "USDTTMN": {"regime": "range", "confidence": 0.76, "evidence": "ret_15m -0.12%, 15m ret_all -0.18%, spread 0.0146%, books fresh"},
      "BTCUSDT": {"regime": "range", "confidence": 0.66, "evidence": "ret_15m +0.39%, 15m ret_all +0.45%, spread 0.0770%, no EMA/ATR signal from only 3 bars"},
      "ETHUSDT": {"regime": "trend_down", "confidence": 0.78, "evidence": "ret_15m -0.30%, 15m ret_all -0.89%, spread 0.0502%, selling pressure on last 15m"},
      "SOLUSDT": {"regime": "trend_up", "confidence": 0.72, "evidence": "ret_15m +0.65%, 15m ret_all +0.91%, spread 0.0940%, momentum still positive"} },
    "risk_flags": ["crossbook_unattractive", "heartbeat_unhealthy", "small_drawdown", "limited_candle_history"],
    "notes": "Cross-book lock-ins remain unattractive on all observed pairs: net edges are about -0.47% to -0.69% after fees, so S1 stays disabled. USDTTMN is still range-like and the current USDT share is 49.84%, so S2 stays neutral at 50%. Drawdown is small at 0.17%, but the heartbeat is still unhealthy and candle history is thin, so size stays reduced rather than full size.",
    "ts": 1792712035.449502,
    /* cached, clamped, latency_ms, model, prompt_hash, tools_source, tools_used: as in features */ },
  "intent": null, "risk": {}, "order": null, "outcome": null,
  "notes": "Cross-book lock-ins remain unattractive … /* the plan's notes, verbatim */"
}
```

* The model first asked for four tools, then returned the plan; its first plan did not validate, and
  the "fix the JSON" retry did (`json_retry: true`, 3 calls). All three answers are in
  `deliverables/shadow_synth_4h/llm_cache/<cache_key>.json`, with OpenRouter's usage.
* "heartbeat unhealthy" is the model reading `rest_ok=false, ws_ok=false`: on the synthetic feed
  neither real source is used.
* The replay re-derived this plan from the cache with no provider call (`REPORTS/replay_20261023.md`).

### 3.2 Example 2: S2 rebalance under that run's plan, and its outcome

The second S2 order of the 4-h run (line 10 of `decisions-20261022.jsonl`). The first one, at
22:53:39Z, was decided by the rules before the first plan arrived.

```jsonc
{
  "kind": "order", "decision_id": "d_1792711463731_S2_USDTTMN_1", "ts": "2026-10-22T23:24:23.730Z",
  "mode": "shadow", "strategy": "S2", "symbol": "USDTTMN",
  "git_commit": "7e8f2f5af1c55c3d744c6c8de22a454040b072f9", "config_hash": "481cc6dc3ce2",
  "features": {"dollar": {"bid": "266603", "ask": "266641", "mid": "266622", "spread_pct": "0.0001425239",
                          "ret_15m": "0.0004990825", "ret_1h": null, "bars_15m": 2, /* ... */},
               "symbols": {"USDTTMN": { /* the same row */ }}, "cross": {}, "ts": 1792711463.730858},
  "intent": {
    "id": "d_1792711463731_S2_USDTTMN_1", "side": "BUY", "order_type": "LIMIT", "purpose": "rebalance",
    "qty": "4.70", "notional": "1253038.80", "price": "266604", // maker: best bid 266,603 + 1 tick
    "taker": false, "expected_edge_pct": null,       // a rebalance needs no edge
    "features": {"usdt_share": "0.2491235056249696717936460778", "target_share": "0.5",
                 "target_origin": "plan", "regime": "range", "regime_source": "plan, confidence 0.93",
                 "rule_regime": "range", "gap_pp": "0.2508764943750303282063539222", "threshold_pp": "0.15",
                 "plan_fresh": true, "plan_ts": 1792711434.577423, "books_used": ["USDTTMN"], /* ... */},
    "reason": "USDTTMN regime=range [plan, confidence 0.93] (analyst: ret_15m +0.10%, spread 0.0146%, no anomaly; prior USDT share 24.92% and books fresh; rules would say range: ema20/ema60 n/a, 24h n/a, ATR15m n/a vs 24h median n/a; no trend rule (|24h| > 0.80% with trend_flag) and no chaos rule fired); target USDT share 50% (plan) vs current 24.9% -> gap +25.1 pp >= threshold 15 pp; last rebalance 30 min ago (min 30 min); buy 1,253,340 TMN of USDT (4.70 USDT, 25.1% of equity 4,995,846 TMN) as maker at 266,604 (bid 266,603 / ask 266,641); the RiskManager caps one order at 25% of equity (1,248,962 TMN), the rest of the gap waits for the next rebalance.",
    /* ts, strategy, symbol, stop_price, linked_position_id */ },
  "risk": {
    "approved": true, "veto_reason": null,
    "adjusted_qty": "4.68",                          // 4.70 asked, 4.68 sent
    "checks": [
      /* KILLED, PAUSED, DEADLINE, BANNED_MARKET, MARKET_ORDER_DISABLED: passed */
      {"code": "MIN_NOTIONAL", "passed": true, "detail": "qty 4.70 x 266604 = 1,253,039 TMN >= min 50,000 TMN"},
      {"code": "MAX_ORDER_PCT", "passed": true,      // an adjust check: it passes and shrinks the qty
       "detail": "adjusted qty 4.70 -> 4.68: notional 1,253,039 TMN > 25% of equity = 1,248,962 TMN"},
      {"code": "BEST_LEVEL_QTY", "passed": true, "detail": "n/a (maker order)"},
      {"code": "USDT_BOUNDS", "passed": true, "detail": "USDT share 24.91% -> 49.91% within [25%, 70%] (moves toward the band)"},
      {"code": "RATE_HOUR", "passed": true, "detail": "3 placements in the last 60 min + 0 re-placements reserved by our open entries + 3 for this one (1 + 2 re-prices) = 6 <= max 10"},
      {"code": "FEE_BUDGET", "passed": true, "detail": "fees paid 3,121 TMN + expected 3,122 TMN (0.250% maker) = 6,243 TMN <= budget 100,000 TMN (2.0% of initial)"},
      {"code": "EDGE_VS_COST", "passed": true, "detail": "n/a (rebalance: no edge required)"},
      {"code": "STALE_DATA", "passed": true, "detail": "books fresh (<= 30s): USDTTMN 1.0s"},
      {"code": "BALANCE", "passed": true, "detail": "needs 1,252,084 TMN incl. 0.25% fee <= free 3,751,264 TMN less 0.5% buffer = 3,732,507 TMN"},
      /* 13 more passed checks: 23 in total */ ] },
  "order": {"accepted": true,
            "request": {"client_id": "S2_USDTTMN_1792711463731_9D1B", "type": "LIMIT", "side": "BUY",
                        "qty": "4.68", "price": "266604", "purpose": "rebalance", /* ... */},
            "response": {"exchange_id": "paper-o-4", "status": "NEW", "filled_qty": "0", "reprices": 0, /* ... */}},
  "plan_snapshot": {"model": "openai/gpt-5.4-mini", "prompt_hash": "78bef8d57dd8436b5a5c047d44f6fd70e0dc185124f295172811e2ad57ea6a59",
                    "enabled_strategies": ["S2"], "usdt_target_share": "0.5", "size_multiplier": "0",
                    "ts": 1792711434.577423, /* the whole plan in force: plan 7 of the run */},
  "outcome": null, "notes": ""
}
```

Six seconds later (line 11):

```jsonc
{"kind": "outcome", "decision_id": "d_1792711463731_S2_USDTTMN_1", "ts": "2026-10-22T23:24:29.751Z",
 "outcome": {"kind": "order", "status": "FILLED", "done_reason": "FILLED",
             "side": "BUY", "symbol": "USDTTMN", "purpose": "rebalance", "qty": "4.68", "filled_qty": "4.68",
             "avg_price": "266604", "fee_paid": "0.011700", "fee_asset": "USDT",   // 0.25 % maker, paid in USDT
             "reprices": 0, "crossed": false, "done_ts": 1792711469.751961,
             "client_ids": ["S2_USDTTMN_1792711463731_9D1B"]}}
```

* The target came from the plan (`target_origin: "plan"`), and the plan's `size_multiplier` 0 did
  not matter: S2 does not use it. S1 and S3 do.
* The USDT share ended at about 49.8 %; no later rebalance was needed in the run.

### 3.3 Example 3: a full S1 lock-in cycle (crafted books, test rig)

**Not market data.** The rig of `tests/test_strategies_engine.py` (`make_rig`, `CrossFeed`) drives
the real engine, RiskManager, OrderManager, PaperExchange and logs with a `SimClock` (2-s ticks) over
hand-made books: BTCTMN = BTCUSDT × USDTTMN × (1 + lag), with a −0.9 % TMN-book lag on ticks 5–7 and
a 10,000,000 TMN paper account. Re-run at `85acc16` (tag `v0.5-backtest`, clean tree) on 2026-10-23;
`feed: "test"`, `ai.source: "off"` (no plan), `config_hash b9b692189867`. The test
`test_two_engines_on_identical_books_write_identical_decision_logs` runs the same scenario. Lines:
S2 initial allocation, its outcome, **leg 1**, **leg 2**, the position outcome, the leg-2 outcome.

Leg 1 (line 3):

```jsonc
{
  "kind": "order", "decision_id": "d_1792663212000_S1_BTCTMN_1", "ts": "2026-10-22T10:00:12.000Z",
  "mode": "shadow", "strategy": "S1", "symbol": "BTCTMN",
  "git_commit": "85acc16b6778f259bcdfb8fda51c44bd80abf944", "config_hash": "b9b692189867",
  "intent": {
    "side": "BUY", "purpose": "entry", "order_type": "LIMIT", "taker": true,
    "qty": "0.000091", "price": "21911480434",       // the BTCTMN best ask
    "expected_edge_pct": "0.007870327467805820463623357",   // 0.787 % before fees
    "features": {"cost_model": "lockin", "mode": "lockin", "side": "TMN_CHEAP", "leg2_symbol": "BTCUSDT",
                 "edge_tmn_cheap_pct": "0.007870327467805820463623357", "cost_pct": "0.0043",
                 "margin_min_pct": "0.0015", "need_pct": "0.006039670329670329670329670330",
                 "unhedged_stress_pct": "0.0002396703296703296703296703297", "size_multiplier": "1",
                 "sizing": {"binding_limit": "budget", "qty": "0.000091", "held_after_fee": "0.000090727",
                            "leg2_sell": "0.000090", "unhedged": "0.000000727", /* ... */},
                 "books_used": ["BTCTMN", "BTCUSDT", "USDTTMN"], /* ... */},
    "reason": "BTC: BTCTMN ask 21,911,480,434 TMN / USDTTMN bid 266,573 = 82,196.92 USDT vs BTCUSDT bid 82,843.84 -> TMN book 0.787% cheap; cost 0.430% (0.300% taker BTCTMN + 0.130% taker BTCUSDT); edge - cost = 0.357% >= margin min 0.150% (config; no fresh plan); unhedged remainder: buying 0.000091 BTC leaves 0.000090727 after the 0.300% leg-1 fee (paid in BTC) and leg 2 on BTCUSDT (step 0.000001) can sell 0.000090: the unhedged 0.000000727 BTC (0.799% of the cycle) x a 3% adverse move = 0.024%, so edge 0.787% >= need 0.604%; lock-in: buy 0.000091 BTC on BTCTMN at 21,911,480,434 (1,993,944.72 TMN) as taker, then sell the 0.000090 BTC left after the leg-1 fee on BTCUSDT at its bid 82,843.84 (leg 2); size 0.000091 BTC limited by budget (budget 0.000091, free_quote 0.000339, leg1_liquidity 0.040000, leg2_liquidity 0.040000); no fresh plan (rules-only, size mult 1).",
    /* id, ts, notional, stop_price, linked_position_id */ },
  "risk": {"approved": true, "adjusted_qty": "0.000091", "veto_reason": null,
    "checks": [
      {"code": "USDT_BOUNDS", "passed": true, "detail": "USDT share 24.93% -> 45.05% (lock-in cycle incl. leg 2 SELL on BTCUSDT) within [25%, 70%] (moves toward the band)"},
      {"code": "FEE_BUDGET", "passed": true, "detail": "fees paid 6,244 TMN + expected 8,574 TMN (0.300% taker + 0.130% taker leg 2 on BTCUSDT) = 14,818 TMN <= budget 100,000 TMN (2.0% of initial)"},
      {"code": "EDGE_VS_COST", "passed": true, "detail": "edge 0.787% vs cost 0.430% (lock-in: 0.300% taker BTCTMN + 0.130% taker BTCUSDT) + margin 0.150% = 0.580%"},
      {"code": "STALE_DATA", "passed": true, "detail": "books fresh (<= 30s): BTCTMN 0.0s, BTCUSDT 0.0s, USDTTMN 0.0s"},
      {"code": "PRICE_SANITY", "passed": true, "detail": "limit 21911480434 is 0.07% from mid 21896153126.5 (band +-3.0%); cross-book deviation -0.90% (anomaly at 5%)"},
      /* 18 more passed checks: 23 in total */ ] },
  "order": {"accepted": true, "request": {"client_id": "S1_BTCTMN_1792663212000_DC11", "qty": "0.000091",
            "price": "21911480434", "side": "BUY", "type": "LIMIT", "purpose": "entry", /* ... */},
            "response": {"exchange_id": "paper-o-2", "status": "NEW", /* ... */}},
  "features": {"cross": {"BTC": { /* edges, costs, deviation */ }}, "dollar": { /* USDTTMN */ },
               "symbols": {"BTCTMN": { }, "BTCUSDT": { }, "USDTTMN": { }}, "ts": 1792663212.0},
  "plan_snapshot": null, "outcome": null, "notes": ""   // the rig runs rules-only
}
```

Leg 1 filled on the next tick (`orders.jsonl`: `fill` of 0.000091 BTC at 21,911,480,434 with a fee
of `2.73E-7` BTC, so 0.000090727 BTC are held). In that same tick S1's `on_fill` hook emitted leg 2
(line 4):

```jsonc
{
  "kind": "order", "decision_id": "d_1792663214000_S1_BTCUSDT_1", "ts": "2026-10-22T10:00:14.000Z",
  "strategy": "S1", "symbol": "BTCUSDT",
  "intent": {"side": "SELL", "purpose": "leg2", "taker": true, "qty": "0.000090", "price": "82843.84",
             "linked_position_id": "p_S1_BTCTMN_1792663214000_1",
             "features": {"cycle": "d_1792663212000_S1_BTCTMN_1", "trigger": "fill", "exit_reason": "lockin",
                          "entry_edge_pct": "0.007870327467805820463623357",
                          "edge_now_pct": "0.003570327467805820463623357",   // after the 0.43 % fees
                          "leg2_max_loss_pct": "0.003", "position_qty": "0.000090727", /* ... */},
             "reason": "BTC: lock-in leg 2 on the leg-1 fill of 0.000091 at 21,911,480,434: 0.000090727 BTC held from leg 1 on BTCTMN at avg 21,911,480,434, 0 already in open leg-2 orders; sell 0.000090 BTC on BTCUSDT at bid 82,843.84 (7.46 USDT, crossing, taker 0.130%); cycle edge now 0.357% after 0.430% fees (entry edge 0.787% before fees) is above the -0.30% leg-2 loss limit.",
             /* ... */},
  "risk": {"approved": true, "adjusted_qty": "0.000090",
    "checks": [
      {"code": "MAX_ORDER_PCT", "passed": true, "detail": "n/a (leg2)"},    // leg 2 reduces exposure
      {"code": "RATE_HOUR", "passed": true, "detail": "n/a (leg2 is exempt from rate limits)"},
      {"code": "PRICE_SANITY", "passed": true, "detail": "sell limit 82843.84 is 0.00% from the best bid 82843.84 (floor -3.0%)"},
      {"code": "BALANCE", "passed": true, "detail": "sells 0.000090 BTC <= free 0.000090727 BTC"},
      /* 19 more: 23 in total */ ] },
  "order": {"accepted": true, "request": {"client_id": "S1_BTCUSDT_1792663214000_5C6D", "qty": "0.000090", /* ... */}},
  /* mode, git_commit, config_hash, features as in leg 1; plan_snapshot null; notes "" */
}
```

When leg 2 filled, the position outcome was appended on the **leg-1** id, and an order outcome on the
leg-2 id (lines 5 and 6):

```jsonc
{"kind": "outcome", "decision_id": "d_1792663212000_S1_BTCTMN_1", "ts": "2026-10-22T10:00:16.000Z",
 "outcome": {"position_id": "p_S1_BTCTMN_1792663214000_1", "strategy": "S1", "symbol": "BTCTMN",
             "exit_reason": "lockin", "hold_s": 2.0, "closed_ts": 1792663216.0,
             "pnl_tmn": "6932.607382855560", "pnl_pct": "0.003476830282744667125510280729",   // +0.348 %
             "dust_qty": "0.000000727"}}          // the sub-step BTC leg 2 could not sell
{"kind": "outcome", "decision_id": "d_1792663214000_S1_BTCUSDT_1", "ts": "2026-10-22T10:00:16.000Z",
 "outcome": {"kind": "order", "status": "FILLED", "done_reason": "FILLED", "purpose": "leg2",
             "filled_qty": "0.000090", "avg_price": "82843.84", "fee_paid": "0.009692729280", "fee_asset": "USDT",
             "crossed": true, "reprices": 0, "position_id": "p_S1_BTCTMN_1792663214000_1", /* ... */}}
```

The +0.35 % comes from the crafted 0.9 % lag minus 0.43 % fees. It says nothing about real gaps.

### 3.4 Example 4: a veto with two failed checks

Run at `85acc16` (tag `v0.5-backtest`, clean tree) on 2026-10-23, synthetic feed, AI off, three
injected test intents (marked `features.injected: true`, `reason` prefixed `[injected for acceptance
testing]`), in a scratch directory outside the repo:

```bash
python -m wta run --mode shadow --feed synthetic --seconds 150 --inject-intents $T/inject.jsonl \
  --paper-balances '{"TMN": 3000000, "USDT": 8}' --log-dir $T/logs \
  --set data.state_sqlite_path=$T/run.sqlite --set ai.enabled=false --runtime-dir $T/runtime
python -m wta logs check --dir $T/logs --secrets      # 9 decision lines ok, OK, secrets scan: clean
```

Intent 3 is a BTCUSDT S3 entry with `stop_price` 81600 (filled; the engine then placed its
`STOP_LIMIT` protective stop, `..._inj3_stop1`, and 60 s later a time-stop exit that canceled the stop
first). Intent 4, ten seconds after intent 3, is a second S3 entry while that S3 position is open:

```jsonc
{
  "kind": "veto", "decision_id": "d_1792739460254_S3_ETHUSDT_inj4", "ts": "2026-10-23T07:11:00.254Z",
  "mode": "shadow", "strategy": "S3", "symbol": "ETHUSDT",
  "git_commit": "85acc16b6778f259bcdfb8fda51c44bd80abf944", "config_hash": "abdc8bb77b99",
  "features": {"inject_line": 4, "injected": true},
  "intent": {"side": "BUY", "order_type": "LIMIT", "purpose": "entry", "qty": "0.001", "price": null,
             "taker": true, "expected_edge_pct": "0.001",
             "reason": "[injected for acceptance testing] S3 example: second S3 entry while one S3 position is open (expected veto)",
             /* ... */},
  "risk": {
    "approved": false, "adjusted_qty": "0.00100",    // computed on a veto too
    "veto_reason": "MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1",
    "checks": [
      /* 8 passed checks */
      {"code": "COIN_EXPOSURE", "passed": true, "detail": "post-trade coins 1,312,416 TMN (held 661,763 TMN + open BUYs 0.0000 TMN + this 650,652 TMN) = 25.57% <= 40% of equity"},
      {"code": "USDT_BOUNDS", "passed": true, "detail": "USDT share incl. our open orders (533,654 TMN) 39.06% -> 26.39% within [25%, 70%]"},
      {"code": "MAX_POSITIONS", "passed": false,     // first failure -> veto_reason
       "detail": "S3 already has 1 positions (incl. pending) >= its cap 1"},
      /* 6 passed checks */
      {"code": "EDGE_VS_COST", "passed": false,      // second failure, also recorded
       "detail": "edge 0.100% vs cost 0.310% (round trip: 0.130% taker ETHUSDT + 0.130% taker exit + spread 0.050%) + margin 0.000% = 0.310%: edge too small"},
      /* 5 passed checks: 23 in total */ ] },
  "order": null,                                     // nothing was sent
  "plan_snapshot": null,                             // ai.enabled false: no plan
  "outcome": null,
  "notes": "MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1"
}
```

A veto gets no outcome line. The position of intent 3 closed with `exit_reason: "time_stop"`,
`pnl_tmn: "-2151.326008215280"`: taker fees on both sides plus the spread, on synthetic books.

## 4. `orders.jsonl`

`OrderLogRecord`: `ts`, `action`, `client_id`, `symbol`, `data`. Written by the OrderManager.

| `action` | `data` |
|---|---|
| `place`, `response` | `source`, `why` (`submit`, `reprice`, `cross`, `server stop`), `request` (the `OrderRequest`); the answer: `source`, `status`, `exchange_id` |
| `error` | `code`, `status`, `message`; `ambiguous: true` when the order may exist (tracked as `UNKNOWN`, resolved by `get_order`, never sent again) |
| `cancel` | `source`, `why` (`cross`, `reprice`, or a done reason) |
| `status` | End of a logical order (`done`, `done_reason`, `filled_qty`, `reprices`); not found after polls; a reconcile finding (`source: "reconcile"`); an adopted order; a lagging exchange snapshot ignored |
| `fill` | The `Fill` (`order_client_id`, `price`, `qty`, `fee`, `fee_asset`, `side`, `symbol`, `trade_id`, `ts`, ...) plus `decision_id` and `purpose` |

`cancel_response` and `timeout` exist in the schema but are not written. The rig run of §3.3 (three
orders: the S2 allocation, leg 1, leg 2) wrote 12 lines: per order one `place`, one `response`, one
`fill` and one `status`. A replay answers every
exchange call from these lines (`ReplayExchange`), so they are a replay input too.

## 5. `events.jsonl`

`EventRecord`: `ts`, `kind`, `reason` (a human sentence), `data` (object). Kinds the engine writes:

| `kind` | When; notable `data` |
|---|---|
| `start` | Process start: `mode`, `feed`, `reason` (`fresh`/`restart`), `git_commit`, `git_dirty`, `git_tag`, `config_hash`, `overlays`, `runtime_overlay`, `state_db`, `strategies`, `rules_only`, `ai` (`source`, `routes`, `cache_dir`, `mcp`, intervals), `markets_source`, `exchange`, `watchlist`, `pid`, deadline times; `synthetic: true` and a `note` on the synthetic feed; `injected_intents` |
| `restart`, `stop` | State restored from the store (restore and startup-reconcile summaries); shutdown (`uptime_s`, `ticks`, `heartbeats`) |
| `pause`, `resume` | Daily-loss, anomaly or Telegram pause; `reason` names the rule |
| `kill`, `flatten`, `final_state` | Kill switch (`source`: drawdown, `kill_file`, `exception_storm`, `telegram`); flatten phases (`kind`: `flatten_start`, `flatten_final`, `complete`, `challenge_end`, kill flatten); the state at T−5 min |
| `reset_kill`, `reset_kill_applied` | Manual reset (`wta reset-kill --reason ...`) and when the engine applied it |
| `ai_degraded`, `ai_recovered`, `plan_stale` | An analyst cycle without a plan (the error, `consecutive_failures`); the first plan after an outage; the last plan passed `ai.plan_stale_s` (`age_s`, `prompt_hash`) |
| `config_change` | An accepted reviewer change: `old_hash`, `new_hash`, `diff`, `rejected`, `overlay_path`, `source: "reviewer"` |
| `operator_command`, `operator_command_ignored` | A Telegram command (`command`, `args`, `source: "telegram"`), and one that had no effect (e.g. `/resume` of a risk pause) |
| `day_start`, `tick_error`, `reconcile`, `reconcile_drift`, `foreign_order` | New UTC day; a crashing tick; a reconcile that found something; balance drift; an order without our prefix |

The 4-h run wrote `start`, `day_start`, `ai_degraded` (01:29:58Z, invalid JSON twice; 02:10:18Z,
provider unreachable), `ai_recovered`, `plan_stale` (02:25:19Z) and `stop`.

## 6. `health.jsonl`

`HealthRecord`, built by `Engine.health_record`:

| Field | Content |
|---|---|
| `ts`, `mode`, `git_commit`, `config_hash` | As in decision records |
| `equity_tmn`, `drawdown_pct` | Equity in TMN; drawdown from the peak as a fraction (`null` when not trading) |
| `open_orders`, `positions`, `paused`, `killed` | Counts and engine flags |
| `ws_ok`, `rest_ok` | Fresh data from that source within `engine.stale_data_s` (30 s). Both `false` on the synthetic feed, which uses neither. |
| `api_errors_1m` | Failed API attempts in the last 60 s |
| `llm_ok` | `true` after a good analyst cycle, `false` after a failed one, `null` without an LLM (AI disabled, offline rules, or no cycle yet) |
| `reasons` | Why the engine is not healthy, e.g. `rest: no fresh data`, `stale books (> 30s): ...`, `llm: 10 failed analyst cycle(s) in a row (...)`, `plan stale (3205s > 1200s): rules-only`, `killed: ...`, `paused: ...`, `flatten phase 2`. Empty = healthy. |
| `extra` | Object, keys below |

`extra` always has `feed`, `uptime_s`, `ticks`, `book_age_s` (per symbol), `rest`, `rest_poller`, `ws`,
`task_crashes`, `tick_crashes`, `git_dirty`, and a `note` on the synthetic feed. When trading it adds
`markets_source`, `flatten_phase`, `daily_pnl_pct`, `fees_paid_tmn`, `fee_budget_used_pct`,
`usdt_share`, `coin_exposure_pct`, `orders_today`, `orders_last_hour`, `peak_equity_tmn`,
`realized_pnl_tmn`, `positions`, `anomalies`, `last_reconcile`, `candles`, `paper_tick` (shadow),
`ai_source`, `ai` (`ai_source`, `plan_age_s`, `plan_fresh`, `plan_model`, `plan_stale`,
`tools_source`, `llm_consecutive_failures`, `llm_ok`, `mcp_ok`, `last_error`, `last_review`, `routes`,
`stats`) and `strategies` (S1 per coin: edges, costs, needs, deviation, z-score, `why_no_trade`,
opportunity-tick counters; S2 target, share and status; S3 status). The 4-h run's last heartbeat:
`equity_tmn` 5,002,181, `llm_ok: false`, `ai.stats` 49 cycles, 38 plans, 11 failures, 4 reviews.

## 7. `python -m wta logs check`

```
python -m wta logs check [--dir logs] [--secrets]      # exit 0 = OK, 1 = a check failed
```

What `src/wta/logging_/checker.py` checks, for the files `family_files` finds (a missing or empty
directory is OK):

1. Every non-blank line is valid JSON, an object, and passes its model: `decisions` lines with
   `kind: "outcome"` → `OutcomeUpdate`, other `decisions` lines → `DecisionRecord`, then
   `HealthRecord`, `EventRecord`, `OrderLogRecord`. Unknown fields, wrong types and invalid
   `kind`/`action` values fail. `ts` must parse as ISO-8601.
2. After all files are read, every outcome's `decision_id` exists as a decision record in some
   `decisions` file (possibly the previous day's). Otherwise:
   `line N: outcome references unknown decision_id '...'`.
3. Errors name the file, line and field path, never the values, so a bad line cannot leak a key into
   the report. At most 50 errors per file are listed.

`--secrets` also scans the log directory, `deliverables/` and `docs/REPORTS/` for each configured
secret (raw and JSON-escaped) and for key patterns (OpenRouter `sk-or-v1-`, `sk-`, Google `AIza`,
Telegram bot tokens, `X-API-Key` values). zip/gzip/bzip2/xz files are decompressed first; a file it
cannot search is reported as a finding, so the scan fails closed. `wta logs check --dir
logs/shadow_synth_4h` passes (42 decision lines, 245 heartbeats, 7 events, 14 order lines).

It does **not** check PnL arithmetic, timestamp order, duplicate decision ids, or that
`git_commit`/`config_hash` match a tag. §8 covers the last point; the replay (§9) checks the rest by
re-deriving every record.

## 8. Verifying `git_commit` and `config_hash`

`git_commit` is the full SHA of `HEAD` at process start. Live mode refuses a dirty tree (untracked
files count) unless started with `--allow-dirty`; the `start` event records `git_dirty` and
`git_tag`. Live runs start from tags `live-YYYYMMDD-HHMM` (`GO_LIVE_CHECKLIST.md` §5).

```bash
# 1. distinct (git_commit, config_hash, mode) in the delivered decision files
cat deliverables/<run>/decisions-*.jsonl | python -c 'import sys, json
rows = {(r["git_commit"], r["config_hash"], r["mode"]) for r in map(json.loads, sys.stdin) if r["kind"] != "outcome"}
print(*sorted(rows), sep="\n")'
# 2. each live commit must be the commit of a live tag
git tag --points-at <git_commit>              # prints live-YYYYMMDD-HHMM
git rev-parse 'live-YYYYMMDD-HHMM^{commit}'   # must print exactly <git_commit>
# 3. the run's start event: clean tree, same tag, the overlays and the runtime overlay it used
grep '"kind":"start"' deliverables/<run>/events-*.jsonl
# 4. the config hash at that tag, with the run's --overlay and --set flags
git checkout live-YYYYMMDD-HHMM
python -m wta config hash --overlay configs/live_halfsize.yaml [the run's --set flags]
```

The hash is `sha256` over the canonical JSON of the resolved config: `configs/default.yaml`,
`markets.yaml` and `llm.yaml`, then the `--overlay` files in order, then the `--set` overrides, then
the `${VAR}` fields. To get the same hash use the same overlays and `--set` flags (every override
counts, including paths such as `data.state_sqlite_path`) and the same values for the `${VAR}` fields
`CHALLENGE_START_UTC`, `CHALLENGE_END_UTC`, `OPENROUTER_MODEL_FAST`, `OPENROUTER_MODEL_STRONG`,
`GEMINI_MODEL` (read from the environment, else from the repo's `.env`; empty becomes `null`). API
keys are settings, not config, so they are not in the hash. Checked on 2026-10-23 at `85acc16`:
`wta config hash --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'` prints
`481cc6dc3ce2`, the hash of every line of the 4-h run.

A reviewer-applied runtime overlay (at startup, or live after a `config_change`) changes the hash
too. The `start` event's `runtime_overlay` names its file, sha256 and `config_hash_before/after`,
and each `config_change` event names `old_hash`, `new_hash` and the overlay file; later decisions
carry `new_hash`, and reproducing it needs that overlay.

## 9. Replay inputs outside the logs

A replay (`wta replay`, `scripts/replay_check.py`) needs the run's log directory (decisions, orders,
events, `llm_cache/`) and its state DB, which the `start` event names (`data.state_db`). From the
state DB it reads:

| Table | Content |
|---|---|
| `ticks` | one row per fast tick: its time and mode, the `ts` of every book it used, its `candle_seq` |
| `engine_books` | every book a tick used, all stored levels, deduplicated by `(symbol, ts)` |
| `engine_candles` | every candle arrival, append-only, with a sequence number; a tick used the arrivals with `seq <= candle_seq` |
| `balances`, `fills` | the exchange-side balances and fills of the recorded run |

The 4-h run's state DB holds 7,292 ticks, 153,132 engine books and 306,200 candle arrivals (108 MB);
the replay used all 7,292 ticks and reproduced 40/40 decisions with 0 network calls. `logs/` and
`data/` are not in git: `deliverables/replay/shadow_synth_4h/` ships the log directory and the
state DB as `logs.tar.xz` and `state.sqlite.xz` with `SHA256SUMS`, and `make replay-deliverable`
replays them.

## 10. Differences from the PRDs, and limitations

| Topic | PRD | Implementation |
|---|---|---|
| File name | `logs/decisions.jsonl` (rotation allowed) | Dated files plus a symlink (§1). Deliver the dated files. |
| `orders.jsonl` "raw" | PRD-01 §8 | Normalized fields only. The exchange payload (`Order.raw`) is kept in the state DB's `orders` table. |
| `client_id` | Overview §8: `-` separators | `_` separators (Wallex accepts only letters, digits, `_`; `docs/DECISIONS.md`). |
| Plan `clamped` | PRD-04: `clamped: true` plus the original | A per-field map `{field: original value}`. |
| Health | PRD-01 §8 field list | Same fields plus `extra`. |
| `intent` / `event` kinds | defined | Not written; router drops are `no_trade`. |

**Limitations.** No live data yet: every example and every engine run so far used the synthetic feed,
crafted test books or the paper exchange. Prices, fills and PnL in these logs are not market
evidence. Paper fills are simulated against those books with fees from `configs/fees_snapshot.yaml`;
order behaviour on Wallex, including `STOP_LIMIT` acceptance, is still unverified
(`GO_LIVE_CHECKLIST.md` §3). `logs check` validates structure and references, not the meaning of the
numbers.
