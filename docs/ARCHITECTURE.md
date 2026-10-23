# Architecture — wallex-trader-agent

**Summary**
1. Three layers, strictly separated. A deterministic **fast loop** runs every 2 s: data → features → strategies S1/S2/S3 → RiskManager → OrderManager → logs. An LLM **slow loop** (Regime Analyst every 5 min, Post-trade Reviewer every hour) only writes a bounded plan. A deterministic **guardian** (RiskManager, kill switch, flatten scheduler) cannot be overridden by any AI output.
2. Everything is wired into one `Engine` class. The same object graph runs in four modes, and only the exchange, the data source and the clock differ: `live` (Wallex REST), `shadow` (paper fills on live or synthetic books), `backtest` (stored books or candles) and `replay` (recorded inputs and recorded exchange answers).
3. Determinism is designed in: one frozen clock per tick, sorted iteration, ids derived from the tick time, canonical JSON logs, replay inputs in the state DB (`ticks`, `engine_books`, `engine_candles`) and a strict LLM cache. The 4-h shadow run replays 40/40 decisions with 0 network calls.
4. AI is real, bounded and logged. Every plan record carries the model, `prompt_hash`, the tools used and anything that was clamped. The evidence runs used the real OpenRouter LLM (`openai/gpt-5.4-mini`). Without a key, a deterministic offline rules provider fills the slot and is labelled "not an LLM" everywhere.
5. Honest status: `api.wallex.ir` is reachable from the dev machine only intermittently. The real data is about 20 minutes of REST books from 2026-10-22, so the evidence runs used the labelled synthetic feed. No live trading has happened; the operator starts live mode.

Code reference: tag `v0.6-ops` (`v0.5-backtest`, `85acc16`, plus the Phase-6 ops work). Related documents:
[`STRATEGY.md`](STRATEGY.md), [`RISK.md`](RISK.md), [`RUNBOOK.md`](RUNBOOK.md),
[`DECISION_LOG_FORMAT.md`](DECISION_LOG_FORMAT.md), [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md),
[`DECISIONS.md`](DECISIONS.md) (one-line design decisions), [`REPORTS/`](REPORTS/README.md) (evidence).

---

## 1. Overview

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
│ Backtest|    │        fills → Positions → realized PnL)  → DecisionLog (JSONL, one record/decision)
│ Replay       │
└──────────────┘
   Scheduler: reconcile every 5 min · heartbeat 60 s · FLATTEN at T-2h/T-30min · kill-switch file
```

The same picture in module names. One agent process; `engine.py` owns every task:

```
recorder process (wta record)              agent process (wta run)
  data/collector.RestPoller ─┐               data/collector.RestPoller ─┐ (feed live)
  exchange/wallex_ws.WallexWS├─▶ bus ─┐      exchange/wallex_ws.WallexWS├─▶ exchange/base.MarketDataBus
  data/collector.CandlePoller│        │      data/collector.CandleRefresher│     │ snapshot per tick
        ▼                    ▼        │      data/synthetic.SyntheticFeed ─┘     ▼   (feed synthetic)
  data/store.Store (data/wta.sqlite)  │   engine.Engine.tick(now) ─ features/engine.FeatureEngine
   books trades candles events        │        │                         ─ strategies/router (S1, S2, S3)
                                      │        │ risk/manager.RiskManager.evaluate   ◀── intents
  state DB data/state_<account>.sqlite◀────────┤ execution/order_manager.OrderManager.submit / poll
   ticks engine_books engine_candles candles   │ execution/positions · execution/reconcile · risk/flatten
   orders fills positions balances equity      ▼
   plans kv events                  exchange/paper.PaperExchange | exchange/wallex_rest.WallexREST
                                    logging_/decision_log (decisions, orders, events) · logging_/health_log
  slow loop task:  ai/analyst.RegimeAnalyst ─▶ MarketPlan ─▶ Engine.plan (taken once per tick)
  reviewer task:   ai/reviewer.PostTradeReviewer ─▶ ai/config_manager ─▶ configs/runtime/<account>/
  optional:        utils/telegram (alerts; /status /pause /resume /kill)
  read-only side processes: wta status · wta tui · research/survey.py · wta report · replay checks
```

The engine has its own book feed (REST poller + WebSocket) and its own candle task, so it does not
need the recorder to trade. The recorder fills the market DB that the survey and the backtests read.

## 2. Wiring status

Every layer is wired into the engine; nothing below is a placeholder.

| Layer | Modules | Used by |
|---|---|---|
| Market data | `exchange/wallex_rest.py`, `exchange/wallex_ws.py`, `data/collector.py`, `data/synthetic.py` | `wta run`, `wta record`; the backtest and replay runners feed the bus themselves |
| Storage and logs | `data/store.py`, `logging_/*` | every mode |
| Features and strategies | `features/engine.py`, `strategies/*` (S1 and S2 always; S3 when `strategies.s3_momentum.enabled`) | every fast tick, through the `StrategyRouter` |
| Risk, execution, flatten, reconcile | `risk/*`, `execution/*` | every intent, in every mode; `scripts/flatten_now.py` |
| Exchanges | `exchange/paper.py`, `exchange/backtest.py`, `exchange/replay.py`, `exchange/wallex_rest.py` | shadow / backtest / replay / live |
| AI layer | `ai/*` | the `slow_loop` and `reviewer` tasks of `wta run`; `wta plan`; `wta backtest --ai on\|offline` (no reviewer); replays (cache only) |
| Ops | `status.py`, `dashboard/tui.py`, `utils/telegram.py` | `wta status`, `wta tui`, `wta alert`, alerts and commands inside `wta run` (when configured), `scripts/run_forever.sh` |
| Evidence | `backtest/*`, `replay/*`, `research/survey.py` | `wta backtest`, `wta report`, `wta export`, `wta replay`, `scripts/replay_check.py`, `scripts/survey.py` |

Not yet exercised against the real exchange: order placement on Wallex REST (no live order so far),
the WebSocket (it never connected from the dev machine) and the Wallex MCP server (never reached).

## 3. Components

| Package / module | Responsibility |
|---|---|
| `config.py` | `Settings` (env and `.env`: keys, URLs) and `Config` (`configs/default.yaml`, `markets.yaml`, `llm.yaml`, `--overlay` files, `--set`); `config_hash = sha256(canonical_json(config))[:12]`; `account_label` and `state_db_path` (one state DB per mode and feed) |
| `models.py` | every pydantic model: books, orders, fills, positions, `Intent`, `MarketPlan`, `DecisionRecord`, `OutcomeUpdate`, `HealthRecord`, `EventRecord` |
| `safety.py` | live guard (flag, `WTA_LIVE=1` in the process environment, API key, clean tree) and deadline guard |
| `exchange/base.py` | `Exchange` interface, `MarketDataBus` (latest book per symbol and source), errors |
| `exchange/wallex_rest.py` | async REST client: token bucket (6 req/s), retries on 429/5xx/timeouts, Decimal JSON, sub-account header; an order send is never repeated blindly (lookup by `client_id` first) |
| `exchange/wallex_ws.py` | Centrifugo v2 WebSocket (`websockets`): depth and trade channels, ping/pong, reconnect with backoff; close code ≥ 3500 stops it for the process |
| `exchange/paper.py` | `PaperExchange`: Wallex-like validation, balance locks, fills against the tick's books one tick later (pessimistic), STOP orders, serialisable state |
| `exchange/backtest.py` | `BacktestExchange(PaperExchange)`: `books` driver (≤ 80 % of a level, newer snapshot only, no fill across a recording gap) and `candles` driver (synthetic book from each bar) |
| `exchange/replay.py` | `ReplayExchange`: answers every exchange call of a replay from the recorded `orders.jsonl`, balances and fills |
| `exchange/markets.py` | `/v1/markets` + fees, with `configs/markets_snapshot.yaml` / `fees_snapshot.yaml` as the offline fallback (live refuses the fallback) |
| `data/store.py` | SQLite (WAL): batched market data, written-through engine state, one transaction per tick, replay inputs |
| `data/collector.py` | `RestPoller`, `CandlePoller`, `Recorder`, `run_recorder`; the engine's `CandleCache` (26 h of 1-min bars in memory) and `CandleRefresher` |
| `data/candles.py`, `data/synthetic.py` | historical candle backfill; the seeded offline feed (dollar walk, coin walks, TMN-book lag with jumps, optional shock), never evidence |
| `features/engine.py` | spreads, returns, ATR, EMA gap, volume ratio, cross-book deviation, z-score, edges and costs (Decimal) |
| `strategies/` | `base.py` (`Strategy` protocol, `StrategyContext`, plan helpers), `router.py` (one strategy per symbol, cooldowns), `s1_crossbook.py`, `s2_usdtalloc.py`, `s3_momentum.py` |
| `risk/manager.py`, `risk/flatten.py`, `risk/context.py` | 23 pre-trade checks and the rolling kill/pause/resume state; the SELL-only flatten; `RiskContext`, `EngineFlags`, `Deadline` |
| `execution/` | `order_manager.py` (maker-first pricing, re-price/cross/expire, server-side stops, fills by snapshot difference, write-ahead record), `positions.py` (`PortfolioState`, `PositionManager` exits), `reconcile.py` (exchange is the truth; foreign orders untouched) |
| `logging_/` | append-only daily JSONL writers with secret scrubbing; `checker.py` (`wta logs check`) |
| `engine.py` | orchestrator: supervised tasks, fast tick, slow loop, reviewer, scheduler, kill/pause/deadline, Telegram, persistence; `build_engine`, `run_engine`, `plan_once` |
| `ai/` | `wiring.py` (which AI source, cache dir, MCP, runtime overlay), `llm_client.py`, `cache.py`, `mcp_client.py`, `tools.py`, `analyst.py`, `plan_schema.py`, `reviewer.py`, `config_manager.py`, `offline_provider.py`, `replay.py` (analyst-only replay), `prompts/` (§8) |
| `replay/runner.py`, `replay/compare.py` | the engine replay of a recorded run and the decision comparison (§6) |
| `backtest/` | `runner.py` (the same `Engine` over stored data; `wta report` and `wta export` helpers), `fills.py`, `metrics.py`, `report.py`, `walkforward.py` |
| `status.py`, `dashboard/tui.py` | read-only status text (`wta status`, Telegram `/status`) and the `rich` dashboard (`wta tui`) |
| `utils/telegram.py` | rate-limited alerts (20 / 10 min, digest when over) and the four operational commands |
| `research/survey.py` | spreads, depth, gaps, cross-book opportunity rates, S1 parameter recommendation |
| `utils/` | `ids.py` (deterministic ids), `decimalutil.py`, `timeutil.py` (clocks), `gitinfo.py` |
| `scripts/` | `check_account.py` (API/account probe), `flatten_now.py`, `replay_check.py` (`make replay`), `shadow_report.py`, `survey.py`, `run_forever.sh`, `ps.sh` |

## 4. Data flow

### 4.1 Fast tick (`Engine.tick(now)`, every `engine.fast_loop_interval_s` = 2 s)

The whole tick runs with the clock frozen at `now` and inside one SQLite transaction.

1. **Inputs.** The bus snapshot of the watchlist plus every book that flatten, positions and open
   orders need, and the candle snapshot, both taken at the start of the tick. They are recorded
   first (`engine_books`, and one `ticks` row with every book's timestamp and the `candle_seq`), so
   even a tick that crashes later can be replayed. In shadow the PaperExchange matches its open
   orders against exactly these books.
2. **State.** `portfolio.refresh` (equity, peak, UTC day start) → `risk.update_rolling_state` → kill,
   pause and resume events. The `KILL` file is checked, a pending `wta reset-kill` is applied, and
   queued Telegram `/pause`, `/resume` and `/kill` requests are applied.
3. **Deadline.** Flatten phases by clock (T−2h, T−30min), `final_state` at T−5min, every order
   cancelled at the end.
4. **Orders.** `order_manager.poll` (fills, re-prices, crosses, protective stops). Balances are
   re-read from the exchange after any fill, placement or cancel.
5. **Intents**, each through `risk.evaluate` → `order_manager.submit` (vetoes are logged too):
   * position exits (software stops, targets, time stops) and flatten SELLs;
   * strategy hooks first: every fill goes to the strategy that owns the order (`on_fill`), every
     finished order to `on_order_done`. Hook intents (S1 leg 2, or its degrade stop) are evaluated
     and sent in the same tick;
   * `FeatureEngine.compute` and `router.tick(ctx, exits)`: S1 → S2 → S3 (S3 only when
     `strategies.s3_momentum.enabled`; it is off by default). The enabled set is config `enabled` ∧
     the fresh plan's `enabled_strategies` ∧ not flattening. One strategy per symbol, one open order
     per (strategy, symbol), and a 30 s symbol cooldown after a fill. A dropped intent gets a
     `no_trade` record;
   * `intent_sources` (tests and tools) and acceptance-test intents injected from a JSONL file
     (shadow only).
   Strategies are not asked while killed, in a flatten phase, during `flatten_now`, or before a
   deferred startup reconcile.
6. **Scheduler.** Reconcile every 300 s and after an API error burst; an equity row every 10 s; kv
   state (portfolio, flags, orders, paper account, strategy and feature state) whenever it changed.

### 4.2 Other tasks of the same process (each one supervised and restarted on a crash)

| Task | Period | Work |
|---|---|---|
| `rest` | 2 s | `RestPoller` → bus (`source="rest"`), live feed |
| `ws` | push | `WallexWS` → bus (`source="ws"`), live feed |
| `candles` | 60 s | `CandleRefresher`: backfills 26 h in one-day requests, then refreshes; every new or changed bar goes into `candles` and the append-only `engine_candles` |
| `synthetic` | 1 s | `SyntheticFeed` books and candles, only with `--feed synthetic` |
| `fast_loop` | 2 s | `safe_tick(now)` (§4.1) |
| `slow_loop` | polled every 5 s | `analyst.should_run` → one Regime Analyst cycle → plan swap (§8.1); absent with `ai.enabled: false` |
| `reviewer` | `engine.reviewer_interval_s` (3600 s) | Post-trade Reviewer → journal, ≤ 2 bounded config changes (§8.2) |
| `heartbeat` | 60 s | `HealthRecord` → `health.jsonl`; Telegram stale-data, API-error and hourly equity alerts |
| `flush` | 1 s | batched store writes |
| `telegram` | long poll | `/status`, `/pause`, `/resume`, `/kill` (only with Telegram configured and `alerts.telegram_commands_enabled`; one engine per bot) |

The fast loop never awaits the slow loop, the reviewer or Telegram.

### 4.3 Outputs

* `<log dir>/decisions-YYYYMMDD.jsonl`: one record per order, veto, `no_trade` drop, plan, and
  later outcome. Each carries `mode`, `git_commit` and `config_hash`. Outcomes are appended, never
  rewritten. Fields: [`DECISION_LOG_FORMAT.md`](DECISION_LOG_FORMAT.md).
* `orders` (exchange traffic), `events` (start, kill, flatten, plan failures, config changes, ...),
  `health` (heartbeats), and `journal.md` (one section per review).
* `<log dir>/llm_cache/` (every LLM request and answer, plus `mcp_tape/`).
* The state DB (`data/state_<account>.sqlite`): engine state and the replay inputs.

## 5. Modes

| | `live` | `shadow` | `backtest` | `replay` |
|---|---|---|---|---|
| Entry point | `WTA_LIVE=1 wta run --mode live --i-understand-live` | `wta run` (shadow is the default) | `wta backtest` (`backtest/runner.py`) | `wta replay`, `scripts/replay_check.py` (`make replay`) |
| Exchange | `WallexREST` (real orders) | `PaperExchange` on the tick's books | `BacktestExchange` (`books` or `candles` driver) | `ReplayExchange`: recorded answers |
| Market data | REST poller + WebSocket + candle refresher | the same, or `--feed synthetic` | market DB (recorded books or 1-min candles, read-only) or `--synthetic-db` | the run's state DB: `ticks`, `engine_books`, `engine_candles` |
| Markets and fees | `/v1/markets` + `/v1/account/fee` only | API, else the snapshot | the snapshot + `fees_snapshot.yaml` | the snapshot |
| Clock | system clock in a `TickClock` | the same | `SimClock`, one step per tick | `SimClock` at the recorded tick times |
| LLM | network, every answer cached | the same | `--ai off\|on\|offline`, cached in `<out>/llm_cache`, at most `--ai-max-calls` cycles, no reviewer | cache only; a miss fails the replay |
| Wallex MCP | live feed only, results taped | live feed only; synthetic feed → `*_local` tools | off (`*_local` tools) | tape only |
| Guards | live guard, deadline guard, markets from the API | none | none | network guard (every socket connect and DNS lookup refused and counted) |
| `mode` in records | `live` | `shadow` | `backtest` | `replay` |

`--feed synthetic` (shadow only) replaces the Wallex feed with the seeded offline feed. Every `start`
event, heartbeat, TUI frame and report then says "synthetic … never evidence of performance". Live
refuses it. Everything between the books and the exchange call is the same object graph in every
mode; the backtest runner and the replay runner build the same `Engine` class that `wta run` builds.

**State per account.** An *account* is `mode` on the live feed and `mode_feed` otherwise (`live`,
`shadow`, `shadow_synthetic`). Each account has its own state DB (`data/state_<account>.sqlite`), its
own lock (`<state db>.engine.lock`) and its own reviewer overlay directory
(`configs/runtime/<account>/`). The LLM cache lives next to the run's logs (`<log dir>/llm_cache`).
A synthetic exercise therefore never touches the real-feed paper account, and nothing learned on
paper or synthetic fills tunes the live engine.

---

## 6. Determinism and replay

| Mechanism | Where | Effect |
|---|---|---|
| Clock injection | `utils/timeutil.Clock`; `engine.TickClock.frozen(now)` | every component reads the tick's `now`; backtests and replays drive `tick(now)` with recorded or simulated times; data feeds keep the real clock (books carry their receive time) |
| Sorted iteration | markets, books, positions, open orders, router output | the same inputs give intents and orders in the same order |
| Deterministic ids | `utils/ids.py` | formulas below; a replay regenerates the same ids and asks the `ReplayExchange` for exactly the recorded client ids |
| Canonical JSON | `config.canonical_json`, `logging_/jsonl.py` | sorted keys, Decimals as strings: the same record is the same bytes |
| Decimal money | prices, quantities, balances, fees | no float rounding in fills or PnL |
| Book inputs | `engine_books` (every book a tick used, all levels, deduplicated by `(symbol, ts)`) and `ticks` (one row per tick: its time, each book's `ts`, its `candle_seq`) | a replay puts exactly the books of each tick on the bus |
| Candle inputs | `engine_candles` (append-only, sequence number per arrival) + `ticks.candle_seq` | a replay applies the arrivals with `seq <= candle_seq` before each tick and gets exactly the candles that tick's features used |
| Pessimistic, deterministic fills | `exchange/paper.py`, `backtest/fills.py` | no randomness; matching one tick (or bar) after placement |
| Strict LLM cache | `ai/cache.py` | key = `sha256(provider, model, messages, tools, temperature)`; a replay serves every answer from the cache, and a miss is a failure, not a silent fallback |
| MCP and tool tape | `<cache dir>/mcp_tape/` | MCP tool lists and results, every internal tool result, and per plan the cycle's input (time, trigger, state, config) |
| Seeds | `engine.rng_seed` = 42 | the synthetic feed and the strategies' only randomness |

```
decision_id   = f"d_{ts_ms}_{strategy}_{symbol}_{seq}"            # plans: d_<ms>_AI_PLAN_<n>
client_id     = f"{prefix}_{SYMBOL}_{ts_ms:013d}_{sha1(f'{decision_id}|{attempt}')[:4].upper()}"   # ≤ 40 chars, [A-Z0-9_]
position_id   = f"p_{strategy}_{symbol}_{ts_ms}_{seq}"
prefix ∈ {S1, S2, S3, FL (flatten), EX (exit), PR (probe)}; anything else on the account is "foreign"
config_hash   = sha256(canonical_json(resolved config))[:12]
llm_cache_key = sha256(provider ‖ model ‖ canonical_json(messages) ‖ canonical_json(tools) ‖ repr(temperature))
prompt_hash   = the cache key of a cycle's first model request
```

**The replay runner** (`replay/runner.py`; `wta replay` and `scripts/replay_check.py`):

1. Rebuilds the engine of the recorded run. The config must give the recorded `config_hash`, with
   the start event's runtime overlay applied. The AI source and routes are taken from the start event.
2. Builds a fresh state DB and log directory (mode `replay`) and a `ReplayExchange` over the recorded
   orders, balances rows and fills. It has no REST, WebSocket, MCP or Telegram client at all.
3. For every `ticks` row: sets the clock to the tick time, puts that tick's `engine_books` on the bus,
   feeds the `engine_candles` arrivals up to its `candle_seq`, and runs `Engine.safe_tick`.
4. Re-derives every recorded plan with `RegimeAnalyst.replay_cycle`: its input from the tape and every
   model answer from the strict cache, with the providers replaced by sentinels that count and refuse
   any call. Each plan is swapped in before the first tick after the recorded swap. Reviewer config
   changes are re-applied from their `config_change` events.
5. Compares the new decision log with the recorded one (`replay/compare.py`): the PRD-05 fields and
   the plan's content, then a strict pass over every field, time stamps, fees, fills and outcome PnL
   included.

During the replay every `AF_INET`/`AF_INET6` connect and every `getaddrinfo` is refused and counted.
The verdict needs zero socket attempts and zero provider calls.

**Result on the 4-h shadow run** (`logs/shadow_synth_4h` + `data/state_shadow4h_shadow_synthetic.sqlite`;
SYNTHETIC feed, real LLM; [`REPORTS/replay_20261023.md`](REPORTS/replay_20261023.md)): 7,292 of 7,292
ticks, **40/40 decisions reproduced** (38 plans + 2 S2 orders) in the comparison and in the strict
pass, 38/38 plans re-derived from the cache (81 hits, 0 misses), 0 socket attempts, 0 LLM provider
calls. Re-run at `85acc16` on 2026-10-23 with the same result (about 2 minutes; `--reports-dir ''`
writes no report, and the replayed logs go to a temporary directory). `logs/` and `data/` are not
in git, so the run is also shipped as a replay bundle (`logs.tar.xz`, `state.sqlite.xz`,
`SHA256SUMS`) that `make replay-deliverable` verifies, unpacks to a temporary directory and replays:

```bash
make replay-deliverable                                                          # from deliverables/replay/shadow_synth_4h
.venv/bin/python scripts/replay_check.py --log-dir logs/shadow_synth_4h --reports-dir ''   # from the local run
```

Replay limits (also written into every replay report when they apply): one process segment is
replayed (a recorded restart is not reproduced); heartbeats, Telegram commands and `wta reset-kill`
are not replayed; markets come from the snapshot; the REST error counters of a live run are not
recorded. No live run exists to replay yet.

---

## 7. Failure handling

| Failure | Handling |
|---|---|
| A task crashes or returns | `_supervise` restarts it after `min(1 s × 2^(n−1), 60 s)`; counted in `health.extra.task_crashes` |
| A fast tick raises (a strategy bug included) | `safe_tick` logs a `tick_error` event and continues; ≥ 3 crashes in 600 s → kill switch (`exception storm`) |
| WebSocket drops | reconnect with backoff; close code ≥ 3500 stops the WebSocket for the process; REST polling (2 s) carries the data |
| REST errors | retries with 1, 2, 4, 8 s backoff (max 60 s, honours `Retry-After`); an ambiguous order send is resolved by `get_order(client_id)`, never sent again blindly |
| API unreachable (seen on 2026-10-22, `RUNBOOK.md` §10) | the feeds retry quietly with capped backoff; no USDTTMN book for 30 s or ≥ 5 consecutive API errors → anomaly **pause** (no entries, entry orders cancelled); resume after 60 s clear. `STALE_DATA` vetoes any decision on a book older than 30 s |
| TMN vs USDT books ≥ 5 % apart, or a held asset without a price | anomaly pause, as above |
| Daily loss ≤ −2.5 % | pause until the next UTC day |
| Drawdown ≥ 5 % from the peak, `KILL` file, or Telegram `/kill` | **kill**: persisted at once, entries cancelled, flatten to TMN, the process keeps monitoring; only `wta reset-kill` clears it |
| Process restart | `startup_restore` reloads peak, day-start equity, fees, counters, realized PnL, flags, positions, orders, strategy and feature state; startup reconcile against the exchange; nothing resets |
| Crash in the middle of a tick | one transaction per tick; the order write-ahead record is committed before the send; reconcile adopts our orders that the store missed |
| Two processes on one account | exclusive lock `<state db>.engine.lock`; the second exits with `EngineLockError` (exit 2) |
| Storage error | the failed commit stays pending and is retried on the next tick; it never counts as a tick crash |
| LLM provider down, timeout, or invalid JSON twice | no new plan; `llm_ok=false`, one `ai_degraded` event, Telegram alert at 3 in a row; the last plan stays in force until it is `ai.plan_stale_s` (1200 s) old (`plan_stale` event), then strategies run rules-only; `ai_recovered` when a plan arrives again. Seen in the 4-h run: a provider outage from 02:10Z, rules-only for the last 0.56 h, no exception |
| Gemini fallback | tried once after an OpenRouter failure; the configured Gemini key is quota-limited (HTTP 429), so in practice the fallback fails too and the engine runs rules-only |
| MCP unavailable | the Analyst uses the `*_local` tools for the cycle; `initialize` is retried after 600 s |
| Telegram unreachable or HTTP 409 | alerts join a digest and never block a tick; a command poller that sees "another getUpdates" conflicts stops and says so in the heartbeat |
| Deadline | flatten phases by clock; a restart late in the run applies every missed boundary in order |

---

## 8. How AI is used

The AI decides **parameters within bounds, never orders**. Two LLM roles run in the slow loop; both
are cached, replayable and logged.

### 8.0 Engine integration (`engine.py`, `ai/wiring.py`)

**Which AI source.** `wiring.build_llm` decides once at startup. The choice is named in the `start`
event and in every heartbeat as `health.extra.ai_source`.

| Condition | `ai_source` | Analyst / Reviewer client |
|---|---|---|
| `ai.enabled: false` | `disabled` | none: no analyst, no reviewer, `plan` always None (the ablation) |
| `OPENROUTER_API_KEY` and/or `GEMINI_API_KEY` set | `llm` | one `LLMClient` per role: OpenRouter primary (`llm.analyst.model` / `llm.reviewer.model` in `configs/llm.yaml`, filled from `OPENROUTER_MODEL_FAST` / `OPENROUTER_MODEL_STRONG`), Gemini fallback (`GEMINI_MODEL`); with only a Gemini key, Gemini is primary; daily budgets `ai.max_analyst_calls_per_day` (400 cycles) and `ai.max_reviewer_calls_per_day` (30 reviews) |
| `ai.enabled` and no key | `offline-rules` | `offline_provider.make_offline_llm`: deterministic rules, **not an LLM** (§8.6) |

With a key set the offline provider is never used. A broken key, a timeout or invalid JSON shows as
`llm_ok=false` and rules-only (or the last fresh plan), never as rules dressed up as a plan. The
evidence runs had `ai_source: llm` with routes `openrouter/openai/gpt-5.4-mini` (analyst),
`openrouter/openai/gpt-6-luna-pro` (reviewer) and `gemini/gemini-4-argon` (fallback).

`LLMCache` directory (`wiring.llm_cache_dir`): `ai.cache_dir` at its default (`logs/llm_cache`) means
`<log dir>/llm_cache` of the run: `logs/shadow/llm_cache` for `wta run`, `logs/llm_cache` for live,
the `--log-dir` (or backtest `--out`) of any other run. Any other value is used as given. The Wallex
MCP client (`wiring.build_mcp`: `WALLEX_MCP_URL`, the API key in `WALLEX_MCP_AUTH_HEADER`, the
read-only allowlist) exists on the live feed only. On the synthetic feed, in backtests and replays,
and on any MCP failure the analyst uses the `*_local` tools.

**Slow loop** (supervised task `slow_loop`). Every 5 s it asks
`analyst.should_run(now, last_run_ts, features, events)`. A due cycle first captures a view: the
latest `FeatureSnapshot`, the bus books, the in-memory 1-minute candles, recent market trades, the
PnL summary, a ring buffer of the last 50 decision records with outcomes, and compact health. Every
tool answer of the cycle comes from that one moment. Then `analyst.run` runs with a whole-cycle
timeout. A plan goes to the `plans` table and to `decisions.jsonl` as a record `kind="plan"`
(decision id `d_<ms>_AI_PLAN_<n>`, stamped with the cycle time; `features` holds the analyst record:
`prompt_hash, model, cached, calls, tools_used, tools_source, latency_ms, clamped, reason,
tool_rounds, llm_calls, json_retry, usage, ai_source`; `plan_snapshot` is the plan; `notes` its
notes). Then `engine.plan` is swapped atomically. Each tick takes the plan once at its start, so a
plan never changes half-way through a tick.

Heartbeat: `llm_ok` is None without an LLM (disabled, offline rules, or no cycle yet) and False after
a failed cycle. `health.extra.ai` holds `ai_source, plan_age_s, plan_fresh, plan_model, plan_stale,
tools_source, llm_consecutive_failures, mcp_ok, last_error, stats, routes, last_review`.

**Reviewer** (supervised task `reviewer`, every `engine.reviewer_interval_s`, the first one interval
after the start). Input: the last hour of this run's `decisions` (outcome updates included), `health`
and `events` files, and the PnL summary. `reviewer.run` → `ConfigManager.apply` (≤ 2 bounded changes;
the operator's `--set` / `--overlay` keys are neither offered nor accepted) → `engine.reload_config`
at the next tick boundary. The reload rebuilds the S1/S2/S3 configs, the router, the FeatureEngine
and the analyst; `risk.*` and `execution.*` differences are refused. Then
`DecisionLog.set_config_hash` stamps the new hash, an event `config_change {old_hash, new_hash, diff,
rejected, overlay_path, source: "reviewer"}` is written, and one section goes into
`<log dir>/journal.md`.

**Runtime overlay at startup.** `build_engine` re-applies `<runtime dir>/latest.yaml`. The default
runtime dir is the account's own (`configs/runtime/live`, `configs/runtime/shadow`,
`configs/runtime/shadow_synthetic`; `wta run --runtime-dir DIR` overrides it). Only
reviewer-editable paths within bounds are applied, only from an overlay whose `# meta` header names
this account, and never with `ai.enabled: false` (the ablation runs on the plain config). The
operator's `--set` / `--overlay` keys win. The `start` event names the result (`runtime_overlay:
{path, sha256, account, applied_ts, applied, pinned, rejected, config_hash_before,
config_hash_after}`, or `skipped`). `wta run --no-runtime-overlay` skips it.

**One cycle by hand.** `wta plan [--feed live|synthetic]` builds the shadow engine of that feed,
restores its candles and feature state, waits for the regime books and runs ONE analyst cycle (reason
`manual`). It prints the plan JSON and stores nothing but the LLM cache and tape: no tick, no order,
no decision record. It takes the shadow account's lock, so it refuses to run next to a shadow engine
on the same account.

### 8.1 Regime Analyst (`ai/analyst.py`, `ai/plan_schema.py`, `ai/prompts/analyst.md`)

**When** (`should_run`):

| Trigger | Condition | `reason` |
|---|---|---|
| startup | first cycle | `startup` |
| rate cap | < 60 s since the last cycle → never | `min_interval` |
| event | `kill`, `pause` or a flatten start since the last cycle | `event:<kind>` |
| fast move | abs(`ret_15m`) of USDTTMN > 1 % or of BTCUSDT > 2 %; once per 15 min per symbol | `move:<symbol>` |
| schedule | ≥ `engine.slow_loop_interval_s` (300 s) since the last cycle | `scheduled` |

**One cycle** (a hand-written loop, no agent framework):

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
clamp_plan → MarketPlan + audit fields → on_plan (plans table + decision kind="plan", §8.0)
```

Every intermediate message is part of the next call's cache key, so the whole trajectory replays.

**Tools**

| Tool | Source | Returns |
|---|---|---|
| `get_features` | engine | the curated `tools.feature_view` (below) |
| `get_pnl_summary` | engine | equity, drawdown, daily PnL, realized PnL by strategy, fees vs budget, positions |
| `get_recent_decisions` | engine | the last n (≤ 50) compact decisions |
| `get_health` | engine | data freshness, API errors, paused/killed, flatten phase, time to deadline |
| `get_candle_stats` | engine candles | returns, ATR %, EMA20/60, volume ratio for 1/15/60-min bars (a clear error during warm-up) |
| `wallex_basic_get_market_depth`, `…_candle_history`, `…_get_latest_trades`, `…_get_markets`, `…_get_account_balances`, `…_get_account_fees`, `wallex_spot_get_open_order`, `wallex_spot_get_orders` | Wallex MCP | read-only market and account views |
| `wallex_basic_get_market_depth_local`, `…_candle_history_local`, `…_get_latest_trades_local` | bus/store | the same data from our own feed (MCP outage, synthetic feed, backtest) |

The plan records `tools_source` = `mcp`, `local` or `none`. Every plan so far has `local`: the MCP
server has never been reached from the dev machine.

**`get_features` view** (`tools.feature_view`). The full `FeatureSnapshot.compact()` of the
21-symbol watchlist is about 18 kB, several times the 4 kB tool cap, and a cut JSON is useless to a
model. The view keeps key fields only, under short names explained in the tool description (`mid,
spread, ret_15m, ret_1h, ret_24h, atr, atr_med, ema_gap, trend, vol_x, age_s`), numbers rounded to 6
significant digits, None values dropped. Without `symbols` the regime symbols get every key and the
other books `mid, spread, ret_1h, ret_24h`. Per S1 coin it adds `dev`, `z` and the lock-in edges
**net of fees** (`net_tmn_cheap`, `net_usdt_cheap`), with the fees once under `lockin_cost`. The
default watchlist with every value present is about 3.8 kB (asserted < 4096 in
`tests/test_ai_tools.py`). When something does not fit, rows become brief, then digits drop, then
rows are omitted and named, never cut.

**Output and clamping** (`clamp_plan`, deterministic, never raises):

| Field | Bound / rule |
|---|---|
| `regimes` | exactly USDTTMN, BTCUSDT, ETHUSDT, SOLUSDT (names normalised, extras dropped and recorded as `regimes.dropped`; missing → `range`, confidence 0.0); regime ∈ range, trend_up, trend_down, chaos |
| `confidence` | [0, 1] |
| `enabled_strategies` | subset of the strategies enabled in config (S3 is disabled by default, so it is removed) |
| `s1_margin_min_pct` | [0.0008, 0.01] |
| `size_multiplier` | [0, 1] |
| `usdt_target_share` | S2 bounds [0.25, 0.70] |
| `risk_flags` | ≤ 12 tags of ≤ 64 chars, de-duplicated |
| `notes` / `evidence` | ≤ 2000 / ≤ 500 chars |

Every value that changed is kept in `MarketPlan.clamped` as `{field: original value}`. Floats from the
model become `Decimal` through their shortest repr, so clamping is reproducible. A plan older than
`ai.plan_stale_s` (1200 s) is ignored and strategies fall back to rules-only defaults.

**How the plan reaches the strategies** (`strategies/base.py` and each strategy):

| Plan field | Consumer | Effect |
|---|---|---|
| `enabled_strategies` | `plan_enabled` | a strategy not listed opens nothing (open cycles are still tended); no fresh plan → allowed (rules-only) |
| `size_multiplier` | `plan_size_multiplier` (S1, S3) | entry size × multiplier; no plan → 1 |
| `s1_margin_min_pct` | S1 | `margin_min = max(config, plan)`: the plan can only make S1 stricter |
| `regimes[XUSDT]`, `regimes[USDTTMN]` | S1 | no revert trades when either is `chaos` |
| `regimes[USDTTMN]`, `usdt_target_share` | S2 | target share from the plan, clamped to the bounds; no plan → `target_share_by_regime[rule_regime(...)]` |
| `regimes[sym]` | S3 | entry only on `trend_up` with confidence ≥ 0.6 (a rules-only entry only with `allow_rules_only`) |

The RiskManager evaluates every resulting intent exactly as it would without a plan; it does not read
the plan at all.

### 8.2 Post-trade Reviewer (`ai/reviewer.py`, `ai/config_manager.py`, `ai/prompts/reviewer.md`)

* **Cadence:** every `engine.reviewer_interval_s` (3600 s), and once at the end (`run_final`, config
  frozen). Not run in backtests.
* **Input** (compact, sorted JSON): the last hour's decisions with merged outcomes (≤ 40 shown,
  aggregates over all), PnL by strategy, fee use vs budget, vetoes by risk code, plan history (≤ 12),
  events and health incidents.
* **Output** (`ReviewOutput`, extra keys forbidden): `journal` (markdown), `param_suggestions`
  `[{path, value, rationale}]`, `alerts`. Temperature 0, cached, one "fix the JSON" retry.
* **Bounded apply:** reviewer pre-filter (forbidden prefixes, no-op values) → `ConfigManager.apply`:
  cooldown `cooldown_s` (3600 s) since the last applied change, ≤ `max_changes_per_review` (2), path
  in the allowlist, value within bounds, the field's own type still validates, value must differ.

| Editable path (`configs/llm.yaml`) | Bounds |
|---|---|
| `strategies.s1_crossbook.margin_min_pct` | [0.0008, 0.005] |
| `strategies.s1_crossbook.leg1_taker` | true / false |
| `strategies.s2_usdtalloc.rebalance_threshold_pp` | [0.10, 0.25] |
| `strategies.s3_momentum.min_regime_confidence` | [0.5, 0.9] |
| `engine.slow_loop_interval_s` | [180, 900] (PRD-04 names it `ai.slow_loop_interval_s`; see `DECISIONS.md`) |

Never editable, enforced twice (the allowlist and a hard prefix check): `risk.`, `execution.`,
`markets.`, `engine.initial_equity`, `engine.challenge`, `engine.flatten`, `ai.replay_strict`, `llm.`.

* **Effects of an accepted change:** a new config object and `config_hash`; overlay files
  `<runtime dir>/config-<UTC stamp>.yaml` and `latest.yaml`, written atomically (header with old →
  new hash and a `# meta` line: account, mode, feed, git commit, `applied_ts`); the `config_change`
  event; a live reload at the next tick and re-application at the next start (§8.0).
* **Journal:** one section per review in `<log dir>/journal.md` (hash before → after, the model's
  journal, alerts, applied changes, every rejected suggestion with its reason). An unavailable or
  invalid LLM writes `reviewer skipped: <reason>` and changes nothing.
* **Seen so far:** the 4-h shadow run has 4 reviews by `gpt-6-luna-pro` and 0 config changes; the
  fourth was skipped because the provider was unreachable.

### 8.3 Wallex MCP client (`ai/mcp_client.py`)

JSON-RPC 2.0 over streamable HTTP to `WALLEX_MCP_URL`: `initialize` (protocol `2025-06-18`) +
`notifications/initialized`, `Mcp-Session-Id` kept (one re-initialise on HTTP 404), plain JSON or SSE
answers, fractional numbers decoded as `Decimal`, the API key only in the auth header and scrubbed
from every error. **Read-only by construction:** `call_tool` accepts only the 8 allowlisted tools
above and refuses any name containing a state-changing word (post, delete, cancel, place, transfer,
withdraw, buy, sell, …; "order" unless preceded by get/list), even if a caller allowlists it. The MCP
server has never been reached from the dev machine (`docs/REPORTS/api_probe.md`).

### 8.4 LLM client, cache and replay (`ai/llm_client.py`, `ai/cache.py`, `ai/replay.py`)

```
chat(messages, tools) = cache lookup (all modes)
                        → replay: stop (miss = CacheMissError)
                        → per-role daily budget (analyst cycles, reviews)
                        → primary route (OpenRouter, timeout) → fallback route once (Gemini) → None
cache file: <cache dir>/<sha256>.json = {request, response, ts, latency_ms, usage}; atomic write
```

Keys travel only in HTTP headers and never reach logs or cache files. `CacheMissError` is re-raised by
the Analyst and the Reviewer: a replay must prove determinism, not degrade silently. Two replays use
the cache: `python -m wta.ai.replay --log-dir DIR` re-derives every `kind="plan"` record of a run from
the cache and the tape alone (analyst only), and the engine replay of §6 re-runs every tick with the
plans re-derived the same way.

### 8.5 Ablation (`ai.enabled=false` vs `true`)

`ai.enabled=false` → no analyst, so strategies always run rules-only: S2 uses `rule_regime`, S1 uses
the config margins, S3 does not trade unless `allow_rules_only`. The runtime overlay is not applied
either. `wta backtest --ai off|on|offline` and `backtest/metrics.ablation_delta` put the runs side by
side ([`REPORTS/ablation_20261023.md`](REPORTS/ablation_20261023.md)):

* recorded real books (0.32 h), AI on vs off: identical, 0 trades either way;
* SYNTHETIC books, a 6-h window, real LLM: +0.09 pp, because the plans raised S1's margin to 0.18 %
  and skipped two marginal signals;
* SYNTHETIC books, offline rules vs off: identical by design.

None of this is evidence that the AI adds return.

### 8.6 Offline rules provider (`ai/offline_provider.py`), NOT an LLM

Used **only** when `ai.enabled` is true and neither `OPENROUTER_API_KEY` nor `GEMINI_API_KEY` is set.
It implements the provider interface with fixed rules, so the whole pipeline (prompt → tool calls →
JSON → validation → clamp → cache → replay) runs without a key:

* first turn: asks for `get_features` (the four regime symbols) and `get_pnl_summary`; asks
  `get_candle_stats` for any symbol still missing features;
* plan: each regime from S2's `rule_regime` on that symbol's features, confidence from the EMA-gap
  size; `enabled_strategies` = S1 and S2 when enabled in config (never S3); `size_multiplier` 1.0, 0.5 when drawdown > 2 %, 0
  when any regime is `chaos`; `usdt_target_share` from the S2 regime table; `s1_margin_min_pct` =
  config;
* reviewer: a journal that only counts what it was given, no parameter suggestions.

Its records carry `ai_source: "offline-rules"`, `ai_label: "AI: offline rules (no LLM key)"`, model
`offline-rules-v1`, latency 0 and no usage. Notes and journals start with `[offline rules provider —
not an LLM]`; status and TUI show `AI: offline rules (no LLM key)`; `llm_ok` stays None. Its plans are
**not** evidence of AI use. Committed examples: `docs/REPORTS/bt_books_synth24h_ai_offline/`.

### 8.7 What the AI layer cannot do

Place, modify or cancel orders; change `risk.*`, `execution.*`, `markets.*` or `llm.*`; push a plan
value past its clamp; enable a strategy that config disables; lift a pause or a kill; delay the
flatten. Every order is a strategy or flatten intent that passed the RiskManager.

### 8.8 Real plans

Both plans below are **real LLM answers from OpenRouter (`openai/gpt-5.4-mini`), not offline-rules
plans** (`ai_source: "llm"`, model `openai/gpt-5.4-mini`, OpenRouter usage in the cache files). Both
ran on the **SYNTHETIC feed**, because `api.wallex.ir` was unreachable: the books the model reasoned
about are generated, and the Wallex MCP tools could not be offered (`tools_source: "local"`).

**(a) From the 4-h shadow run** (`logs/shadow_synth_4h`, committed copy in
`deliverables/shadow_synth_4h/`; commit `7e8f2f5`, clean tree, `config_hash 481cc6dc3ce2`; 38 plans
in 4.08 h, all reproduced by the replay of §6). An excerpt of one `kind="plan"` line of
`decisions-20261023.jsonl`; `/* … */` marks removed keys, the values are verbatim:

```jsonc
{
  "kind": "plan", "decision_id": "d_1792718394646_AI_PLAN_30", "ts": "2026-10-23T01:19:54.646Z",
  "mode": "shadow", "strategy": "AI", "symbol": "PLAN", "config_hash": "481cc6dc3ce2",
  "features": {
    "ai_source": "llm", "model": "openai/gpt-5.4-mini", "reason": "scheduled", "cached": false,
    "prompt_hash": "214230b3149835eb82e05614ed4a5a1b1b9126f64bfc2a58261ffc0a4eaf95a4",
    "llm_calls": 2, "tool_rounds": 1, "tools_used": ["get_features"], "tools_source": "local",
    "json_retry": false, "clamped": {}, "latency_ms": 5134,
    "usage": {"completion_tokens": 390, "prompt_tokens": 10659, "total_tokens": 11049} /* calls */ },
  "plan_snapshot": {
    "enabled_strategies": ["S2"], "size_multiplier": "0.35", "s1_margin_min_pct": "0.0018",
    "usdt_target_share": "0.55",
    "regimes": {
      "USDTTMN": {"regime": "trend_up", "confidence": 0.68,
                  "evidence": "ret_1h +0.467%, ret_15m +0.144%, spread 0.0146%, trend 0; dollar bid is firm but not explosive."},
      "SOLUSDT": {"regime": "trend_down", "confidence": 0.97,
                  "evidence": "ret_1h -2.091%, ret_15m -0.562%, spread 0.0862%, trend 0; strongest selloff of the group."}
      /* BTCUSDT trend_down 0.9, ETHUSDT trend_down 0.72 */ },
    "risk_flags": ["crossbook_unattractive", "crypto_weakness", "sol_trend_down", "drawdown_control"],
    "notes": "USDTTMN is still orderly and slightly stronger, so I nudge the USDT target above neutral to 55%. Cross-book edges remain negative after fees for BTC, ETH, and SOL, so S1 stays disabled. Crypto momentum is broadly weak, especially SOL, so I keep size reduced rather than forcing risk."
    /* model, prompt_hash, ts, tools_*, latency_ms, cached, clamped: as in features */ }
}
```

How to check it: `deliverables/shadow_synth_4h/llm_cache/214230b3149835eb….json` (the `prompt_hash` is the
cache key of the cycle's first request) holds the request (system and user messages, the tool list,
provider `openrouter`, model `openai/gpt-5.4-mini`, temperature 0) and OpenRouter's answer: a
`get_features` call for USDTTMN, BTCUSDT, ETHUSDT and SOLUSDT, with OpenRouter's token and cost
usage. The second request (the JSON plan) is listed in `features.calls`. What the plan changed: S2's
target moved from 50 % to 55 %, but the USDT share was already about 49.8 %, a 5 pp gap below the
15 pp rebalance threshold, so no order followed. S1 stayed off, as in all 38 plans of the run.

**(b) From the Phase-4 acceptance run (a)** ([`REPORTS/phase4_acceptance.md`](REPORTS/phase4_acceptance.md)):
`wta run --mode shadow --feed synthetic --seconds 900` with `engine.slow_loop_interval_s=60`, git
`8130132` plus the uncommitted Phase-4 working tree, `config_hash 113103719ae4`. Its logs are in the
session scratchpad, not in the repo. 15 plans in 15 minutes, all 15 reproduced from the LLM cache with
zero provider calls (`python -m wta.ai.replay`). The state DB was fresh, so an hour of candles did
not exist yet: `ret_1h`, `ret_24h` and the EMA features were absent, and the model said so in its
evidence. The plan narrows the envelope (S1 off, `s1_margin_min_pct` 0.002 > config 0.0015). This
record predates the `features.calls` field. One `kind="plan"` line, verbatim:

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

**For contrast, an offline-rules plan** (not an LLM), from the committed
`docs/REPORTS/bt_books_synth24h_ai_offline/decisions-20261001.jsonl`: `"ai_source":
"offline-rules"`, `"ai_label": "AI: offline rules (no LLM key)"`, `"model": "offline-rules-v1"`,
`"latency_ms": 0`, `"usage": {}`, and notes that start with `[offline rules provider — not an LLM]
Rules-only regimes from features: …`.

Still open: a plan from a shadow run on the **real** Wallex feed with the MCP server reachable
(`tools_source: "mcp"`).

---

## 9. Known limitations

* **Almost no real market data.** `api.wallex.ir` is reachable from the dev machine only
  intermittently. The recorder has about 20 minutes of REST books (2026-10-22 14:20–14:40Z, 6,103
  snapshots), a 1-second burst of 28 snapshots at 20:16Z and 2.2 h of 1-minute candles. The WebSocket
  never connected, and the MCP server was never reached. Payload parsers follow the documented shapes
  (`WALLEX_API_NOTES.md`) and the short real recording.
* **Evidence runs are synthetic.** The 4-h shadow run, the 24-h book backtests and the 30-day candle
  backtest ran on generated data (labelled SYNTHETIC everywhere). They prove the mechanics, not
  returns. See [`REPORTS/final_report_summary.md`](REPORTS/final_report_summary.md).
* **No live run.** No live order has been placed; live mode is started only by the operator after
  "go live" ([`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md)). `STOP_LIMIT` acceptance and the
  `stop_Price` field on Wallex REST are unverified (the order probe has not run).
* **LLM availability.** OpenRouter was unreachable for the last 48 minutes of the 4-h run, and the
  Gemini fallback key is quota-limited (HTTP 429), so a provider outage means rules-only after 20
  minutes.
* **Replay scope.** One process segment per replay; heartbeats, Telegram commands and `reset-kill`
  are not replayed; markets come from the snapshot.
* **Operations not exercised for real.** Telegram needs `TELEGRAM_CHAT_ID` (empty in `.env`) and
  `alerts.telegram_enabled=true`; `CHALLENGE_END_UTC` and `WALLEX_SUB_ACCOUNT_CLIENT_ID` are empty in
  `.env` too. Live under docker compose is not supported as configured ([`RUNBOOK.md`](RUNBOOK.md) §3).
