# Architecture — wallex-trader-agent (DRAFT)

**Summary**
1. Three layers with strict separation: a deterministic **fast loop** (data → features → strategy intents → RiskManager → OrderManager → logs, every 2 s), an LLM **slow loop** (Regime Analyst every 5 min, Post-trade Reviewer hourly) that only writes a bounded plan, and a deterministic **guardian** (RiskManager, kill switch, flatten scheduler) that no AI output can override.
2. One code path for four modes; only the exchange object, the data source and the clock differ: `live` (Wallex REST), `shadow` (PaperExchange on live books), `backtest` (BacktestExchange on recorded books or candles), `replay` (recorded inputs + cached LLM answers).
3. Determinism is designed in: an injected clock frozen per tick, sorted iteration, ids derived from the tick time, canonical JSON logs, recorded replay inputs (`engine_books`, `ticks`) and an LLM cache keyed by `sha256(provider, model, messages, tools, temperature)`.
4. AI is used in a measured, logged way: every plan carries `prompt_hash`, `model`, `tools_used`, `tools_source`, `latency_ms` and the list of clamped values; without an LLM key a deterministic offline rules provider (not an LLM) keeps the plumbing running.
5. Status at commit `b943545`: data, risk, execution, flatten, reconcile and the shadow engine run end to end; strategies and the AI layer are implemented and unit-tested but **not yet plugged into `wta run`**. `api.wallex.ir` is unreachable from the dev machine, so every run so far used the synthetic feed.

Related: [`RUNBOOK.md`](RUNBOOK.md) (operations), `STRATEGY.md`, `RISK.md`, `REPRODUCE.md`,
`DECISION_LOG_FORMAT.md`, [`DECISIONS.md`](DECISIONS.md) (one-line design decisions), PRD-01…06.

---

## 1. Overview diagram (Overview §3)

```
                ┌──────────────────────────── SLOW LOOP (every 5 min) ───────────────────────────┐
                │  Regime Analyst (LLM via OpenRouter, tool-calling over Wallex MCP + internal)   │
                │  → MarketPlan JSON (regime per market, enabled strategies, margins, size mult,  │
                │    USDT target share, rationale)  → validated + clamped → stored + cached        │
                │  Post-trade Reviewer (LLM, hourly) → journal + bounded param suggestions         │
                └───────────────────────────────────────┬──────────────────────────────────────────┘
                                                        │ MarketPlan
┌──────────────┐   book/trades/candles   ┌──────────────▼─────────────┐   intents   ┌──────────────┐
│ Wallex REST  │────────────────────────▶│  FAST LOOP (every 2 s)      │────────────▶│ RiskManager  │
│ + WebSocket  │                         │  FeatureEngine → Strategies │             │ (hard rules) │
└──────────────┘                         │  S1 CrossBook, S2 USDTAlloc,│◀────────────│ approve/veto │
       ▲                                 │  S3 RegimeMomentum          │  decisions  └──────┬───────┘
       │ orders / fills                  └──────────────┬─────────────┘                    │
┌──────┴───────┐                                        │ approved intents                 │
│ Exchange     │◀───────────────────────────────────────┴──────────────────────────────────┘
│ Live|Paper|  │        OrderManager (idempotent client_id, reprice/cancel, server-side stops,
│ Backtest     │        fills → Positions → realized PnL)  → DecisionLog (JSONL, one record/decision)
└──────────────┘
   Scheduler: reconcile every 5 min · heartbeat 60 s · FLATTEN at T-2h/T-30min · kill-switch file
```

The same picture in module names (one process; `engine.py` owns every task):

```
recorder process (wta record)          agent process (wta run)
  data/collector.RestPoller ─┐           data/collector.RestPoller ┐
  exchange/wallex_ws.WallexWS├─▶ bus ─▶  exchange/wallex_ws.WallexWS├─▶ exchange/base.MarketDataBus
  data/collector.CandlePoller│           data/synthetic.SyntheticFeed┘        │ snapshot per tick
        ▼                    ▼                                                ▼
  data/store.Store (data/wta.sqlite) ◀──── engine.Engine.tick(now) ── features/engine ─▶ strategies/router
   books trades candles                        │                                   (S1, S2, S3) │ intents
   engine_books ticks orders fills             │  risk/manager.RiskManager.evaluate ◀──────────┘
   positions equity kv plans events            │  execution/order_manager.OrderManager.submit/poll
                                               │  execution/positions · execution/reconcile · risk/flatten
                                               ▼
                         exchange/paper.PaperExchange | exchange/wallex_rest.WallexREST | exchange/backtest
                         logging_/decision_log (decisions, orders, events) · logging_/health_log
  slow loop (designed): ai/analyst.RegimeAnalyst ─▶ MarketPlan ─▶ StrategyContext.plan
                        ai/reviewer.PostTradeReviewer ─▶ ai/config_manager ─▶ configs/runtime/*.yaml
  read-only side processes: status.py · dashboard/tui.py · research/survey.py · backtest/report.py
```

## 2. Wiring status at `b943545`

| Layer | Modules | Implemented and unit-tested | Used by `wta run` |
|---|---|---|---|
| Market data | `exchange/wallex_rest.py`, `exchange/wallex_ws.py`, `data/collector.py`, `data/synthetic.py` | yes | yes |
| Storage and logs | `data/store.py`, `logging_/*` | yes | yes |
| Risk, execution, flatten, reconcile | `risk/*`, `execution/*` | yes | yes |
| Paper exchange | `exchange/paper.py` | yes | yes (shadow) |
| Features | `features/engine.py` | yes | no (strategies, AI tools and survey use it) |
| Strategies | `strategies/*` | yes | **no**: `Engine.intent_sources` is empty ("S0 no-op") |
| AI layer | `ai/*` | yes | **no**: heartbeat `llm_ok` is always `null` |
| Backtest | `exchange/backtest.py`, `backtest/fills.py`, `backtest/metrics.py`, `backtest/report.py` | yes | no runner; `wta backtest` is a placeholder |
| Replay | inputs recorded (`engine_books`, `ticks`); LLM cache `replay` mode | partly | `wta replay` is a placeholder |
| Ops | `status.py`, `dashboard/tui.py`, `utils/telegram.py` | yes | module entry points only (see RUNBOOK §0) |
| Research | `research/survey.py`, `scripts/survey.py` | yes | standalone |

Sections 3–8 describe the implemented design; where the engine wiring is still missing it says so.

---

## 3. Components

| Package / module | Responsibility |
|---|---|
| `config.py` | `Settings` (env/.env: keys, URLs) and `Config` (`configs/default.yaml`, `markets.yaml`, `llm.yaml`, `--set`); `config_hash = sha256(canonical_json(config))[:12]` |
| `models.py` | every pydantic model: books, orders, fills, positions, `Intent`, `MarketPlan`, `DecisionRecord`, `HealthRecord`, `EventRecord` |
| `safety.py` | live guard (flag, `WTA_LIVE=1` in the process env, API key, clean tree) and deadline guard |
| `exchange/base.py` | `Exchange` interface, `MarketDataBus` (latest book per symbol and per source), errors |
| `exchange/wallex_rest.py` | async REST client: token bucket (6 req/s), retries on 429/5xx/timeouts, Decimal JSON, `place_order` never re-sent blindly (lookup by `client_id` first), sub-account header |
| `exchange/wallex_ws.py` | Centrifugo v2 WebSocket over `websockets`: depth and trade channels, ping/pong, reconnect with backoff, close code ≥ 3500 stops it for good |
| `exchange/paper.py` | `PaperExchange`: Wallex-like validation, balance locks, fills against the tick's books one tick later (pessimistic), STOP orders, serialisable state |
| `exchange/backtest.py` | `BacktestExchange(PaperExchange)`: `books` driver (≤ 80 % of a level, newer snapshot only) and `candles` driver (synthetic book from bars) |
| `exchange/markets.py` | `/v1/markets` + fees, with `configs/markets_snapshot.yaml` / `fees_snapshot.yaml` as offline fallback (live refuses the fallback) |
| `data/store.py` | SQLite (WAL), batched market data, written-through engine state, one transaction per tick, replay inputs |
| `data/collector.py` | `RestPoller` (`/v2/depth/all` + per-symbol fill-in), `CandlePoller`, `Recorder`, `run_recorder` |
| `data/candles.py` | historical backfill from `/v1/udf/history` |
| `data/synthetic.py` | seeded offline feed (dollar random walk, coin walks, TMN-book lag with jumps, optional shock); never evidence |
| `features/engine.py` | spreads, returns, ATR, EMA gap, volume ratio, cross-book deviation/z-score/edges/costs (Decimal) |
| `strategies/` | `base.py` (pure `Strategy` protocol, `StrategyContext`), `router.py`, `s1_crossbook.py`, `s2_usdtalloc.py`, `s3_momentum.py` |
| `risk/manager.py` | `RiskManager.evaluate`: ordered checklist (KILLED … DUPLICATE), hard vetoes and size adjustments; `update_rolling_state` for kill/pause/resume |
| `risk/flatten.py` | `FlattenManager`: SELL-only routine to TMN, phases, dust handling |
| `risk/context.py` | `RiskContext`, `EngineFlags`, `Deadline` |
| `execution/order_manager.py` | intent → exchange order(s): maker-first pricing, re-price/cross/expire, server-side stops, fills by snapshot difference, write-ahead record |
| `execution/positions.py` | `PortfolioState` (balances, equity in TMN, peak, day start, fees, counters, realized PnL), `PositionManager` (exits) |
| `execution/reconcile.py` | startup/periodic reconcile with the exchange (exchange is the truth), foreign orders left untouched |
| `logging_/` | append-only daily JSONL writers with secret scrubbing; `checker.py` (`wta logs check`) |
| `engine.py` | orchestrator: tasks, fast tick, scheduler, kill/pause/deadline, persistence, `build_engine`/`run_engine` |
| `ai/` | `llm_client.py`, `cache.py`, `mcp_client.py`, `tools.py`, `analyst.py`, `plan_schema.py`, `reviewer.py`, `config_manager.py`, `offline_provider.py`, `prompts/` (§8) |
| `status.py`, `dashboard/tui.py` | read-only status text and `rich` dashboard over logs and the store |
| `research/survey.py` | measured spreads, depth, gaps, cross-book opportunity rates, computed S1 parameter recommendation |
| `utils/` | `ids.py` (deterministic ids), `decimalutil.py`, `timeutil.py` (clocks), `gitinfo.py`, `telegram.py` |

## 4. Data flow

### 4.1 Fast tick (`Engine.tick(now)`, every `engine.fast_loop_interval_s` = 2 s)

The whole tick runs with the clock frozen at `now` and inside one SQLite transaction.

1. **Books.** Snapshot of the bus for the watchlist plus every symbol that flatten, positions and open
   orders need. They are recorded first (`engine_books`, `ticks`), so even a tick that crashes later is
   replayable. In shadow the PaperExchange matches its open orders against exactly these books.
2. **State.** `portfolio.refresh` (equity, peak, UTC day start) → `risk.update_rolling_state` → kill /
   pause / resume events; the `KILL` file is checked; a pending `wta reset-kill` is applied.
3. **Deadline.** Flatten phase transitions by clock (T−2h, T−30min), `final_state` at T−5min, all orders
   cancelled at the end.
4. **Orders.** `order_manager.poll` (fills, re-prices, crosses, stops); balances are re-read from the
   exchange after any fill, placement or cancel.
5. **Intents** in this order, each through `risk.evaluate` → `order_manager.submit` (vetoes are logged
   too): position exits (software stops, targets, time stops), flatten SELLs, strategy intents
   (`intent_sources`), acceptance-test intents injected from a JSONL file (shadow only).
6. **Scheduler.** Reconcile every 300 s and after an API error burst; equity row every 10 s; kv state
   whenever it changed.

Designed strategy step (PRD-03, router implemented): exits → S1 → S2 → S3; enabled set =
config `enabled` ∧ fresh `plan.enabled_strategies` ∧ not flattening; one strategy per symbol; per-symbol
cooldown 30 s after a fill.

### 4.2 Other loops (supervised tasks of the same process)

| Task | Period | Work |
|---|---|---|
| `rest` | 2 s | `RestPoller` → bus (`source="rest"`) |
| `ws` | push | `WallexWS` → bus (`source="ws"`) |
| `synthetic` | 1 s | only with `--feed synthetic` |
| `heartbeat` | 60 s | `HealthRecord` → `health.jsonl` |
| `flush` | 1 s | batched store writes |
| slow loop (designed, PRD-04 §8) | trigger-based, ≥ 60 s | `RegimeAnalyst.run` → plan swap; hourly `PostTradeReviewer.run` |

### 4.3 Outputs

`decisions.jsonl` (one record per intent/veto/order/plan/outcome; outcomes are appended later, never
rewritten; each carries `mode`, `git_commit`, `config_hash`), `orders.jsonl` (raw exchange traffic),
`events.jsonl`, `health.jsonl`; store tables listed in RUNBOOK §10.

---

## 5. Modes

| | `live` | `shadow` | `backtest` | `replay` |
|---|---|---|---|---|
| Exchange | `WallexREST` (real orders) | `PaperExchange` | `BacktestExchange` (`books` or `candles` driver) | Paper/Backtest exchange over recorded inputs |
| Market data | REST poller + WebSocket | same, or `--feed synthetic` | store: recorded books or 1-min candles | store: `engine_books` + `ticks` |
| Markets/fees | `/v1/markets` + `/v1/account/fee` only | API, else snapshot | `fees_snapshot.yaml` | as recorded |
| Clock | system clock in a `TickClock` | same | simulated `tick(now)` | recorded tick times |
| LLM | network, every answer cached | same | cache; a miss calls once and is stored | cache only; a miss raises `CacheMissError` (`ai.replay_strict`) |
| Wallex MCP | live, results taped | same | `*_local` tools | tape only |
| Guards | live guard + deadline guard | none | none | none |
| Entry point | `wta run --mode live --i-understand-live` | `wta run --mode shadow` | library; `wta backtest` placeholder | `wta replay` placeholder |

Everything between the books and the exchange call is the same object graph in every mode.

---

## 6. Determinism and replay

| Mechanism | Where | Effect |
|---|---|---|
| Clock injection | `utils/timeutil.Clock`; `engine.TickClock.frozen(now)` | every component reads the tick's `now` during a tick; tests, backtests and replays pass `now` explicitly. Data feeds keep the real clock (books carry their receive time) |
| Sorted iteration | markets, books, positions, open orders, router output | the same inputs produce intents and orders in the same order |
| Deterministic ids | `utils/ids.py` | see formulas below; a replay regenerates the same ids |
| Canonical JSON | `config.canonical_json`, `logging_/jsonl.py` | sorted keys, Decimals as strings: the same record is the same bytes |
| Decimal money | everywhere for prices, quantities, balances | no float rounding in fills or PnL |
| Replay inputs | `engine_books` (every book a tick used, all levels, deduplicated by `(symbol, ts)`), `ticks` (one row per tick with the book timestamps) | a replay feeds back exactly what each tick saw |
| Pessimistic, deterministic fills | `exchange/paper.py`, `backtest/fills.py` | no randomness; matching one tick (or bar) after placement |
| LLM cache | `ai/cache.py` | identical request → identical response, zero network in replay |
| MCP tape | `analyst.py`, `<cache_dir>/mcp_tape/` | MCP tool lists and results replay without the server |
| Seeds | `engine.rng_seed` = 42 | synthetic feed and strategies' only randomness source |

```
decision_id = f"d_{ts_ms}_{strategy}_{symbol}_{seq}"
client_id   = f"{prefix}_{SYMBOL}_{ts_ms:013d}_{sha1(f'{decision_id}|{attempt}')[:4].upper()}"   # ≤ 40 chars, [A-Z0-9_]
position_id = f"p_{strategy}_{symbol}_{ts_ms}_{seq}"
prefix ∈ {S1, S2, S3, FL (flatten), EX (exit), PR (probe)}; anything else on the account is "foreign"
config_hash = sha256(canonical_json(resolved config))[:12]
llm_cache_key = sha256(provider ‖ model ‖ canonical_json(messages) ‖ canonical_json(tools) ‖ repr(temperature))
```

Replay target (PRD-05): re-running the recorded inputs with the cached LLM answers must reproduce
`decisions.jsonl` byte for byte on the deterministic fields. Not demonstrated yet: the replay runner
does not exist at this commit.

---

## 7. Failure handling

| Failure | Handling |
|---|---|
| A task crashes or returns | `_supervise` restarts it after `min(1 s × 2^(n−1), 60 s)`; counted in `health.extra.task_crashes` |
| A fast tick raises | `safe_tick` logs a `tick_error` event and continues; ≥ 3 crashes in 600 s → kill switch (`exception storm`) |
| WebSocket drops | reconnect with backoff; close code ≥ 3500 stops the WS for the process; REST polling (2 s) covers the data |
| REST errors | retries with 1, 2, 4, 8 s backoff (max 60 s, honours `Retry-After`); an ambiguous order send is resolved by `get_order(client_id)`, never re-sent blindly |
| Stale or inconsistent data | no USDTTMN book for 30 s, ≥ 5 consecutive API errors, TMN vs USDT books ≥ 5 % apart, or a held asset without price → anomaly **pause** (no entries, entry orders cancelled); resume after 60 s clear. `STALE_DATA` vetoes any decision on a book older than 30 s |
| Daily loss ≤ −2.5 % | pause until the next UTC day |
| Drawdown ≥ 5 % from peak, `KILL` file | **kill**: persisted at once, entries cancelled, flatten to TMN, process keeps monitoring; only `wta reset-kill` clears it |
| Process restart | `startup_restore` reloads peak, day-start equity, fees, counters, realized PnL, flags, positions and orders; startup reconcile against the exchange; nothing resets |
| Crash in the middle of a tick | one transaction per tick; the order write-ahead record is committed before the send; reconcile adopts our orders the store missed |
| Two processes on one account | exclusive lock `<sqlite_path>.engine.lock`; the second exits with `EngineLockError` |
| Storage error | the failed commit stays pending and is retried next tick; it never counts as a tick crash |
| LLM unavailable / invalid twice | no new plan; the last plan is used until `ai.plan_stale_s` (1200 s), then strategies run rules-only |
| MCP unavailable | the Analyst switches to `*_local` tools for the cycle; `initialize` retried after 600 s |
| Deadline | flatten phases by clock; a restart late in the run applies every missed boundary in order |

---

## 8. How AI is used

AI decides **parameters within bounds**, never orders. Two LLM roles run in the slow loop; both are
cached, replayable and logged. Both are wired into `wta run` (`Engine.enable_ai`, attached by
`build_engine`; §8.0) — the wiring table in §2 predates it.

### 8.0 Engine integration (`engine.py`, `ai/wiring.py`)

**Which AI source** (`wiring.build_llm`, decided once at startup, named in the `start` event and in
every heartbeat as `health.extra.ai_source`):

| Condition | `ai_source` | Analyst / Reviewer client |
|---|---|---|
| `ai.enabled: false` | `disabled` | none: no analyst, no reviewer, `plan` always None (ablation) |
| `OPENROUTER_API_KEY` and/or `GEMINI_API_KEY` set | `llm` | `LLMClient` per role: OpenRouter primary (`llm.analyst.model` / `llm.reviewer.model` from `configs/llm.yaml`, which fill from `OPENROUTER_MODEL_FAST` / `OPENROUTER_MODEL_STRONG`), Gemini fallback (`GEMINI_MODEL`); only a Gemini key → Gemini primary; daily budgets `ai.max_analyst_calls_per_day` / `ai.max_reviewer_calls_per_day` |
| `ai.enabled` and no key | `offline-rules` | `offline_provider.make_offline_llm` — deterministic rules, **not an LLM** (§8.6) |

With a key set the offline provider is never used: a broken key, a timeout or invalid JSON shows as
`llm_ok=false` and rules-only (or the last fresh plan), never as rules dressed up as a plan.
`LLMCache` directory (`wiring.llm_cache_dir`): `ai.cache_dir` at its default (`logs/llm_cache`) means
`<log dir>/llm_cache` of the run — `logs/shadow/llm_cache` for `wta run --mode shadow`,
`logs/llm_cache` for live, the `--log-dir` of any other run; any other value is used as given. It is
strict (a miss raises) only in `replay` mode with `ai.replay_strict`. The Wallex MCP client
(`wiring.build_mcp`: `WALLEX_MCP_URL`, `WALLEX_API_KEY` in `WALLEX_MCP_AUTH_HEADER`,
`READ_ONLY_ALLOWLIST`) exists on the live feed only; on the synthetic feed, in replay and on any MCP
failure the analyst uses the `*_local` tools.

**Slow loop** (supervised task `slow_loop`; the fast loop never awaits it): every 5 s
`analyst.should_run(now, last_run_ts, features, events)` (triggers below); a due cycle captures a
view (latest `FeatureSnapshot`, bus books, the in-memory 1-minute candles, recent market trades, the
PnL summary, a ring buffer of the last 50 decision records with outcomes, compact health) so every
tool answer of the cycle comes from one moment, then `analyst.run` with a whole-cycle timeout.
A plan → `plans` table + `decisions.jsonl` record `kind="plan"` (`decision_id`
`d_<ms>_AI_PLAN_<seq>`, stamped with the cycle time; `features` = the analyst record:
`prompt_hash, model, cached, tools_used, tools_source, latency_ms, clamped, reason, tool_rounds,
llm_calls, json_retry, usage, ai_source`; `plan_snapshot` = the plan; `notes` = its notes) → atomic
swap of `engine.plan`; each tick takes the plan once at its start, so a plan never changes half-way
through a tick. No plan → the last plan stays until it is stale, `llm_consecutive_failures` grows,
one `ai_degraded` event per outage (`ai_recovered` after it), a Telegram alert at 3 in a row.
Heartbeat: `llm_ok` (None without an LLM — disabled, offline rules or no cycle yet; False after a
failed cycle), `health.extra.ai = {ai_source, plan_age_s, plan_model, plan_stale, tools_source,
llm_consecutive_failures, mcp_ok, last_error, stats, routes, last_review}`.

**Reviewer** (supervised task `reviewer`, every `engine.reviewer_interval_s`, the first one interval
after the start): the last hour of this run's `decisions` (outcome updates included), `health` and
`events` files (read in a worker thread) + the PnL summary → `reviewer.run` → `ConfigManager.apply`
(≤ 2 bounded changes; the operator's pinned `--set` / `--overlay` keys are neither offered nor
accepted) → `engine.reload_config` at the next tick boundary (S1/S2/S3 config objects, router,
FeatureEngine, analyst; `risk.*` / `execution.*` differences are refused; a tick suspended on an
exchange await finishes on the old config, a running analyst cycle keeps the config it started with)
→ `DecisionLog.set_config_hash`; event `config_change {old_hash, new_hash, diff, rejected,
overlay_path, source: "reviewer"}`; one section in `<log dir>/journal.md` per review. At startup
`build_engine` re-applies `<runtime dir>/latest.yaml` (default: the account's own
`configs/runtime/<mode>` on the live feed, `configs/runtime/<mode>_<feed>` otherwise, like the state
DB; `wta run --runtime-dir`) — only reviewer-editable paths within bounds, only an overlay whose
`# meta` header names this account, never with `ai.enabled: false` (the ablation runs on the plain
config); the operator's `--set` / `--overlay` keys win and their overlay entries are kept in
`latest.yaml` (`pinned`); the cooldown restarts from the header's `applied_ts` — and the `start`
event names it (`runtime_overlay: {path, sha256, account, applied_ts, applied, pinned, rejected,
config_hash_before, config_hash_after}`, `skipped` when not applied); `wta run --no-runtime-overlay`
skips it.

**One cycle by hand:** `wta plan [--feed live|synthetic]` builds the shadow engine of that feed,
restores its candles and feature state, waits for the regime books and runs ONE analyst cycle
(reason `manual`); it prints the plan JSON and stores nothing but the LLM cache and tape (no tick,
no order, no decision record).

### 8.1 Regime Analyst (`ai/analyst.py`, `ai/plan_schema.py`, `ai/prompts/analyst.md`)

**When** (`should_run`):

| Trigger | Condition | `reason` |
|---|---|---|
| startup | first cycle | `startup` |
| rate cap | < 60 s since the last cycle → never | `min_interval` |
| event | `kill`, `pause` or a flatten start since the last cycle | `event:<kind>` |
| fast move | abs(`ret_15m`) of USDTTMN > 1 % or of BTCUSDT > 2 %; once per 15 min per symbol | `move:<symbol>` |
| schedule | ≥ `engine.slow_loop_interval_s` (300 s) since the last cycle | `scheduled` |

**One cycle** (hand-written loop, no agent framework):

```
system = prompts/analyst.md with config values (fees, strategy parameters, bounds, time to deadline)
         + the exact JSON schema + one illustrative example
user   = canonical JSON {reason, now_utc, time_to_deadline_s, features (compact), pnl_summary,
         health, last_plan, recent_events} + "Decide the plan ..."
tools  = internal tools + Wallex MCP read-only tools        (MCP initialize ok)
       | internal tools + *_local mirrors                   (no MCP, or MCP failed this cycle)
repeat ≤ ai.analyst_max_tool_rounds (6):
    resp = llm.chat(messages, tools, temperature=0, json_mode, timeout=ai.analyst_timeout_s)
    tool calls (≤ 8 per round) → results truncated to ai.tool_result_max_bytes (4096)
    → appended as assistant tool_calls + role:"tool" messages → next round
at the cap: "Tool-call limit reached … return only the final JSON plan" (tools off)
parse PlanOutput (strict, extra keys forbidden); invalid → one "fix the JSON" retry; invalid again → None
clamp_plan → MarketPlan + audit fields → on_plan (engine: `plans` table + decision kind="plan", §8.0)
```

Every intermediate message is part of the next call's cache key, so the whole trajectory replays.

**Tools**

| Tool | Source | Returns |
|---|---|---|
| `get_features` | engine | the curated `tools.feature_view` (below) |
| `get_pnl_summary` | engine | equity, drawdown, daily PnL, realized PnL by strategy, fees vs budget, positions |
| `get_recent_decisions` | engine | last n (≤ 50) compact decisions |
| `get_health` | engine | data freshness, API errors, paused/killed, flatten phase, time to deadline |
| `get_candle_stats` | store | returns, ATR %, EMA20/60, volume ratio for 1/15/60-min bars |
| `wallex_basic_get_market_depth`, `…_candle_history`, `…_get_latest_trades`, `…_get_markets`, `…_get_account_balances`, `…_get_account_fees`, `wallex_spot_get_open_order`, `wallex_spot_get_orders` | Wallex MCP | read-only market and account views |
| `wallex_basic_get_market_depth_local`, `…_candle_history_local`, `…_get_latest_trades_local` | bus/store | the same data from our own feed (MCP outage, backtest) |

The plan records `tools_source` = `mcp`, `local` or `none`.

**`get_features` view** (`tools.feature_view`): the full `FeatureSnapshot.compact()` of the
21-symbol watchlist is ~18 kB, several times the 4 kB tool cap, and a cut JSON is useless to a
model. The view keeps key fields only, under short names explained in the tool description
(`mid, spread, ret_15m, ret_1h, ret_24h, atr, atr_med, ema_gap, trend, vol_x, age_s`), JSON numbers
rounded to 6 significant digits, None values dropped. Without `symbols` the regime symbols get every
key and the other books `mid, spread, ret_1h, ret_24h` (`brief_rows`); per S1 coin `dev`, `z` and the
lock-in edges **net of fees** (`net_tmn_cheap`, `net_usdt_cheap`), the fees once under
`lockin_cost`. The default watchlist with every value present is ~3.8 kB (asserted < 4096 in
`tests/test_ai_tools.py`); when something does not fit, rows become brief, then digits drop, then rows
are omitted and named — never a cut. The analyst's first message carries the same view for the whole
watchlist with every key and the key legend (cap 4 × 4 kB).

**Output and clamping** (`clamp_plan`, deterministic, never raises):

| Field | Bound / rule |
|---|---|
| `regimes` | exactly USDTTMN, BTCUSDT, ETHUSDT, SOLUSDT (names normalised, extras dropped, missing → `range`, confidence 0.0, "missing in model output"); regime ∈ range, trend_up, trend_down, chaos |
| `confidence` | [0, 1] |
| `enabled_strategies` | subset of the strategies enabled in config (S3 is disabled by default, so it is removed) |
| `s1_margin_min_pct` | [0.0008, 0.01] |
| `size_multiplier` | [0, 1] |
| `usdt_target_share` | S2 bounds [0.25, 0.70] |
| `risk_flags` | ≤ 12 tags of ≤ 64 chars, de-duplicated |
| `notes` / `evidence` | ≤ 2000 / ≤ 500 chars |

Every value that changed is kept in `MarketPlan.clamped` as `{field: original value}` (PRD-04 says
`clamped: true` plus the original; the implementation keeps the per-field map). Floats from the model
become `Decimal` through their shortest repr, so clamping is reproducible.

**Staleness.** A plan older than `ai.plan_stale_s` (1200 s) is ignored (`plan=None`, logged once per
plan); strategies fall back to rules-only defaults.

**How the plan reaches the strategies** (`strategies/base.py` and each strategy):

| Plan field | Consumer | Effect |
|---|---|---|
| `enabled_strategies` | `plan_enabled` | a strategy not listed opens nothing; no fresh plan → allowed (rules-only) |
| `size_multiplier` | `plan_size_multiplier` (S1, S3) | entry size × multiplier; no plan → 1 |
| `s1_margin_min_pct` | S1 | `margin_min = max(config, plan)`: the plan can only make S1 stricter |
| `regimes[XUSDT]`, `regimes[USDTTMN]` | S1 | no revert trades when either is `chaos` |
| `regimes[USDTTMN]`, `usdt_target_share` | S2 | target share from the plan, clamped to bounds; no plan → `target_share_by_regime[rule_regime(...)]` |
| `regimes[sym]` | S3 | entry only on `trend_up` with confidence ≥ 0.6 (rules-only entry only with `allow_rules_only`) |

The RiskManager evaluates every resulting intent exactly as it would without a plan.

### 8.2 Post-trade Reviewer (`ai/reviewer.py`, `ai/config_manager.py`, `ai/prompts/reviewer.md`)

* **Cadence:** every `engine.reviewer_interval_s` (3600 s) and once at the end (`run_final`, config frozen).
* **Input** (compact, sorted JSON): the last hour's decisions with merged outcomes (≤ 40 shown,
  aggregates over all), PnL by strategy, fee usage vs budget, vetoes by risk code, plan history (≤ 12),
  events and health incidents.
* **Output** (`ReviewOutput`, extra keys forbidden): `journal` (markdown), `param_suggestions`
  `[{path, value, rationale}]`, `alerts`. Temperature 0, cached, one "fix the JSON" retry.
* **Bounded apply:** reviewer pre-filter (forbidden prefixes, no-op values) → `ConfigManager.apply`:
  cooldown `cooldown_s` (3600 s) since the last applied change, ≤ `max_changes_per_review` (2), path in
  the allowlist, value within bounds, the field's own type still validates, value must differ.

| Editable path (`configs/llm.yaml`) | Bounds |
|---|---|
| `strategies.s1_crossbook.margin_min_pct` | [0.0008, 0.005] |
| `strategies.s1_crossbook.leg1_taker` | true / false |
| `strategies.s2_usdtalloc.rebalance_threshold_pp` | [0.10, 0.25] |
| `strategies.s3_momentum.min_regime_confidence` | [0.5, 0.9] |
| `engine.slow_loop_interval_s` | [180, 900] (PRD names it `ai.slow_loop_interval_s`; see DECISIONS.md) |

Never editable, enforced twice (allowlist and a hard prefix check): `risk.`, `execution.`, `markets.`,
`engine.initial_equity`, `engine.challenge`, `engine.flatten`, `ai.replay_strict`, `llm.`.

* **Effects of an accepted change:** a new config object and `config_hash`; overlay files
  `<runtime dir>/config-<UTC stamp>.yaml` and `latest.yaml`, written atomically (header with old →
  new hash and a `# meta` line: account, mode, feed, git commit, `applied_ts`); event
  `config_change {old_hash, new_hash, diff, rejected, overlay_path, source: "reviewer"}`.
* **Journal:** one section per review appended to `logs/journal.md` (hash before → after, the model's
  journal, alerts, applied changes, every rejected suggestion with its reason). An unavailable or
  invalid LLM writes `reviewer skipped: <reason>` and changes nothing.
* **Engine:** accepted changes are reloaded live (`reload_config`) and re-applied at the next start
  from `latest.yaml` (§8.0).

### 8.3 Wallex MCP client (`ai/mcp_client.py`)

JSON-RPC 2.0 over streamable HTTP to `WALLEX_MCP_URL`: `initialize` (protocol `2025-06-18`) +
`notifications/initialized`, `Mcp-Session-Id` kept (one re-initialise on HTTP 404), plain JSON or SSE
answers, fractional numbers decoded as `Decimal`, the API key only in the auth header and scrubbed from
every error. **Read-only by construction:** `call_tool` accepts only the 8 allowlisted tools above, and
refuses any name containing a state-changing word (post, delete, cancel, place, transfer, withdraw,
buy, sell, …; "order" unless preceded by get/list) even if a caller allowlists it. The MCP server has
never been reached from the dev machine (`docs/REPORTS/api_probe.md`).

### 8.4 LLM client, cache and replay (`ai/llm_client.py`, `ai/cache.py`)

```
chat(messages, tools) = cache lookup (all modes)
                        → replay: stop (miss = CacheMissError)
                        → per-role daily call budget (config: ai.max_analyst_calls_per_day 400,
                          ai.max_reviewer_calls_per_day 30; passed in by whoever builds the client)
                        → primary route (OpenRouter, timeout) → fallback route once (Gemini) → None
cache file: <ai.cache_dir>/<sha256>.json = {request, response, ts, latency_ms, usage}; atomic write
```

Keys travel only in HTTP headers and never reach logs or cache files. `CacheMissError` is re-raised
by the Analyst and the Reviewer: a replay must prove determinism, not degrade silently. MCP tool lists
and results are taped under `<cache_dir>/mcp_tape/`; in replay the Analyst never contacts MCP. The
same tape holds every internal tool result and, per plan, the cycle's input (`now`, trigger, the JSON
state, `config_hash`), so `RegimeAnalyst.replay_cycle(prompt_hash)` re-runs a recorded cycle from the
cache and the tape alone. `python -m wta.ai.replay --log-dir DIR` does that for every `kind="plan"`
record of a run (strict cache, no provider object at all — a sentinel counts and refuses any call —
no MCP) and compares each replayed plan with the recorded `plan_snapshot`. Re-running the recorded
ticks through the engine (`wta replay`) is the Phase-5 runner (PRD-05).

### 8.5 Ablation

`ai.enabled=false` → the Analyst returns no plan (`last_error = "ai.enabled is false"`), so strategies
always run rules-only: S2 uses `rule_regime`, S1 uses config margins, S3 does not trade unless
`allow_rules_only`. `backtest/metrics.ablation_delta` and `backtest/report.py` put AI-on vs AI-off
side by side (return, max drawdown, trades, vetoes, fees). No ablation result exists yet: there is no
recorded Wallex data to run it on.

### 8.6 Offline rules provider (`ai/offline_provider.py`) — NOT an LLM

Used **only** when `ai.enabled` is true and neither `OPENROUTER_API_KEY` nor `GEMINI_API_KEY` is set
(`use_offline_provider`). It implements the provider interface with fixed rules so the whole pipeline
(prompt → tool calls → JSON → validation → clamp → cache → replay) runs without a key:

* first turn: asks for `get_features` (the four regime symbols) and `get_pnl_summary`; asks
  `get_candle_stats` (97 × 15-min bars = 24 h) for any symbol still missing features;
* plan: each regime from S2's `rule_regime` on that symbol's features, confidence from the EMA-gap size;
  `enabled_strategies` = the configured ones; `size_multiplier` 1.0, 0.5 when drawdown > 2 %, 0 when
  any regime is `chaos`; `usdt_target_share` from the S2 regime table; `s1_margin_min_pct` = config;
* reviewer: a journal that only counts what it was given, no parameter suggestions.

In the engine `ai_source` is `offline-rules`, `llm_ok` stays None (there is no LLM), plan records carry
`ai_label: "AI: offline rules (no LLM key)"`. Model name `offline-rules-v1`, latency 0, no usage; notes
and journals start with
`[offline rules provider — not an LLM]`; status and TUI show `AI: offline rules (no LLM key)`. Output
depends only on the messages, so it caches and replays exactly. Its plans are **not** evidence of AI
usage and must not be presented as such.

### 8.7 What the AI layer cannot do

Place, modify or cancel orders; change `risk.*`, `execution.*`, `markets.*` or `llm.*`; push a plan
value past its clamp; enable a strategy that config disables; lift a pause or a kill; delay the
flatten. Every order is a strategy or flatten intent that passed the RiskManager.

### 8.8 Real plan example

**Real LLM plan — `openai/gpt-5.4-mini` via OpenRouter — on the SYNTHETIC feed** (not real market
data: `api.wallex.ir` was unreachable, so the Wallex MCP tools could not be offered; `tools_source`
is `local`). One `kind="plan"` line of `decisions.jsonl`, verbatim, from the Phase-4 acceptance run
(a) (`docs/REPORTS/phase4_acceptance.md`): `wta run --mode shadow --feed synthetic --seconds 900`
with `engine.slow_loop_interval_s=60`, git `8130132` + the uncommitted Phase-4 working tree,
`config_hash 113103719ae4`; 15 plans in 15 minutes, all 15 reproduced from the LLM cache with zero
provider calls (`python -m wta.ai.replay`). The state DB was fresh, so an hour of candles did not
exist yet: `ret_1h`, `ret_24h` and the EMA features were absent, and the model said so in its
evidence. The plan narrows the envelope (S1 off, `s1_margin_min_pct` 0.002 > config 0.0015); the
RiskManager still checks every intent.

```json
{
  "config_hash": "113103719ae4",
  "decision_id": "d_1792705552866_AI_PLAN_14",
  "features": {
    "ai_source": "llm",
    "cached": false,
    "clamped": {},
    "json_retry": false,
    "latency_ms": 5271,
    "llm_calls": 2,
    "model": "openai/gpt-5.4-mini",
    "prompt_hash": "49f5610a19280633ecf84033e6187f31c713ff15882a6c9a631ffc33f65daba1",
    "reason": "scheduled",
    "tool_rounds": 1,
    "tools_source": "local",
    "tools_used": [
      "get_features",
      "get_health",
      "get_pnl_summary"
    ],
    "usage": {
      "completion_tokens": 445,
      "prompt_tokens": 9847,
      "total_tokens": 10292
    }
  },
  "git_commit": "8130132b4afb648aaeea6cd47eaf4494ecdcee85",
  "intent": null,
  "kind": "plan",
  "mode": "shadow",
  "notes": "Books are fresh and healthy, with no API errors or stale symbols. Cross-book lock-in remains unattractive after fees: all net edges are negative, with BTC/ETH/SOL around -0.47% to -0.67% versus a 0.43% round-trip cost floor. The dollar book is flat and the current USDT share is 0.249, so S2 stays at the normal 0.5 target with full size. Drawdown is still tiny, so I keep the plan stable and avoid overreacting.",
  "order": null,
  "outcome": null,
  "plan_snapshot": {
    "cached": false,
    "clamped": {},
    "enabled_strategies": [
      "S2"
    ],
    "latency_ms": 5271,
    "model": "openai/gpt-5.4-mini",
    "notes": "Books are fresh and healthy, with no API errors or stale symbols. Cross-book lock-in remains unattractive after fees: all net edges are negative, with BTC/ETH/SOL around -0.47% to -0.67% versus a 0.43% round-trip cost floor. The dollar book is flat and the current USDT share is 0.249, so S2 stays at the normal 0.5 target with full size. Drawdown is still tiny, so I keep the plan stable and avoid overreacting.",
    "prompt_hash": "49f5610a19280633ecf84033e6187f31c713ff15882a6c9a631ffc33f65daba1",
    "regimes": {
      "BTCUSDT": {
        "confidence": 0.87,
        "evidence": "trend 0, spread 0.0770%, z -0.162, cross dev +0.0050%",
        "regime": "range"
      },
      "ETHUSDT": {
        "confidence": 0.92,
        "evidence": "trend 0, spread 0.0505%, z -0.537, cross dev -0.0088%",
        "regime": "range"
      },
      "SOLUSDT": {
        "confidence": 0.84,
        "evidence": "trend 0, spread 0.0951%, z -0.549, cross dev -0.0024%",
        "regime": "range"
      },
      "USDTTMN": {
        "confidence": 0.98,
        "evidence": "ret_1h not provided; trend 0, spread 0.0143%, age 0.667s, usdt_share 0.249 vs target 0.5",
        "regime": "range"
      }
    },
    "risk_flags": [
      "fresh_books",
      "s1_unfavorable_lockin",
      "flat_regime",
      "small_drawdown"
    ],
    "s1_margin_min_pct": "0.002",
    "size_multiplier": "1",
    "tools_source": "local",
    "tools_used": [
      "get_features",
      "get_health",
      "get_pnl_summary"
    ],
    "ts": 1792705552.865914,
    "usdt_target_share": "0.5"
  },
  "risk": {},
  "strategy": "AI",
  "symbol": "PLAN",
  "ts": "2026-10-22T21:45:52.865Z"
}
```

TODO (still open): replace it with a plan from a shadow run on the **real** Wallex feed with the MCP
server reachable (`tools_source: "mcp"`), naming the run (mode, git tag, config_hash).

---

## 9. Known limitations (at `b943545`)

* No live Wallex data yet: `api.wallex.ir` times out from the dev machine; all engine runs used the
  synthetic feed and the offline market snapshot. Payload parsers follow the documented shapes
  (`WALLEX_API_NOTES.md`) and are unverified against the real API.
* Strategies, Analyst and Reviewer are not wired into `wta run`; heartbeat `llm_ok` is always `null`.
* `wta backtest`, `wta replay`, `wta report`, `wta status`, `wta tui` are CLI placeholders; replay
  determinism of `decisions.jsonl` is designed but not demonstrated.
* The engine keeps its state in `data.sqlite_path`, shared with the recorder; `data.state_sqlite_path`
  is not used by the engine yet.
* Reviewer overlays are written but not reloaded live or at restart.
* Telegram alerts are implemented but not connected to engine events.
