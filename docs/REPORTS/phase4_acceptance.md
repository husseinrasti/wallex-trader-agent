# Phase 4 acceptance — AI layer wired into the engine (PRD-04 §8, PRD-06 §2)

Date: 2026-10-22/23 (UTC 21:30–21:53). Code: git `8130132` (`v0.3-strategies-5-g8130132`) plus the
uncommitted Phase-4 working tree. Every log and database of these runs is in the session scratchpad
(`<scratch>/acc/...`), never in the repo's `logs/` or `data/`.

## 0. Read this first

* **The brief said there are no LLM keys on this machine. That is no longer true.** `.env` now holds
  `OPENROUTER_API_KEY` (models `OPENROUTER_MODEL_FAST=openai/gpt-5.4-mini`,
  `OPENROUTER_MODEL_STRONG=openai/gpt-6-luna-pro`), `GEMINI_API_KEY` (`GEMINI_MODEL=gemini-4-argon`)
  and a `TELEGRAM_BOT_TOKEN` without `TELEGRAM_CHAT_ID` (so Telegram stays off). With a key set the
  engine uses the LLM by design, so run (a) produced **real LLM plans (OpenRouter, gpt-5.4-mini)**, not
  offline-rules plans. The offline rules provider was exercised in a separate run with the keys
  blanked (a′). The plan pasted into `docs/ARCHITECTURE.md` §8.8 is therefore labelled as what it
  is: a real LLM plan **on the synthetic feed**, not the "offline rules provider" label the brief
  anticipated.
* Real calls made while doing this work: OpenRouter 3 + 31 + 2 analyst calls (gpt-5.4-mini, ~176 k
  tokens) and 1 + 2 reviewer calls (gpt-6-luna-pro, ~33 k tokens); Gemini 6 calls (all refused, §4).
* `api.wallex.ir` and its MCP endpoint were unreachable (ConnectTimeout) during these runs: every run
  used `--feed synthetic` (an offline exercise, never evidence of performance) and the analyst always
  used the `*_local` tools (`tools_source: "local"`). No plan with `tools_source: "mcp"` exists yet.

## 1. Results

| Check | Result |
|---|---|
| (a) shadow, synthetic, 900 s, `slow_loop_interval_s=60`, `reviewer_interval_s=300` | **pass** — 15 plans, one every 60–64 s, every one with notes (273–412 chars), model `openai/gpt-5.4-mini`, `tools_source` `local` (tools used in 11 of 15 cycles: `get_features`, `get_health`, `get_pnl_summary`, `get_recent_decisions`), 0 failed cycles, `llm_ok=true` in all 16 heartbeats; 2 reviewer journal entries (`gpt-6-luna-pro`, no change suggested); `wta logs check` OK, secrets scan clean |
| (a′) same with no key (`OPENROUTER_API_KEY= GEMINI_API_KEY=`), 330 s | **pass** — `ai_source: offline-rules`, 6 plans every 60 s, model `offline-rules-v1`, notes start with `[offline rules provider — not an LLM]`, records carry `ai_label: "AI: offline rules (no LLM key)"`, `llm_ok` null (no LLM), 1 offline journal entry |
| (b) invalid `OPENROUTER_API_KEY` in the environment, no Gemini key, 200 s | **pass** — every cycle fails with OpenRouter HTTP 401, no exception, `ai_source` stays `llm` (the offline provider is **not** used while a key is set), `llm_ok=false`, `llm_consecutive_failures` 1→4, one `ai_degraded` event, reason "strategies run rules-only" in every heartbeat, no plan record, reviewer "skipped: … 401"; the key appears nowhere in logs, events, journal or console |
| (b2) invalid OpenRouter key + the real Gemini key from `.env` (fallback), 140 s | **fallback reached, not proven end to end** — 1st run: Gemini HTTP 400 on our request (bug, fixed, §4); 2nd run: Gemini HTTP 429 "You exceeded your current quota" (account quota). Engine behaviour as in (b): rules-only, `llm_ok=false` |
| (c) replay of (a)'s plans from the cache, zero network | **pass** — `python -m wta.ai.replay`: 15/15 plans identical (prompt_hash and every plan field but `cached`), provider calls 0 (no provider object exists in the replay; a sentinel counts), cache hits 31, misses 0; (a′) 6/6 identical |
| (d) one plan record of (a) in `docs/ARCHITECTURE.md` §8.8 | **done** — `d_1792705552866_AI_PLAN_14`, labelled "Real LLM plan … on the SYNTHETIC feed"; the real-feed TODO stays open |
| `wta plan --feed live` (one cycle, extra) | ran: markets/books unreachable, MCP initialize timed out → local tools; the LLM saw "7 consecutive API errors, zero fresh books", disabled S1 and set `size_multiplier` 0 — nothing stored |
| `ruff check src tests scripts backtest` | All checks passed |
| `pytest` | **1765 passed** in 62 s at integration; **1796 passed** after the review fixes (§6) |

## 2. What was built (summary)

* **Construction** (`ai/wiring.py`): `build_llm` → `llm` (OpenRouter primary + Gemini fallback per
  `configs/llm.yaml` and the env models; per-role daily budgets; analyst/reviewer timeouts) /
  `offline-rules` (no key) / `disabled` (`ai.enabled: false`). One `LLMCache` per run:
  `ai.cache_dir` at its default resolves to `<log dir>/llm_cache` (shadow `logs/shadow/llm_cache`,
  live `logs/llm_cache`, any `--log-dir`), strict only in replay. `build_mcp` (live feed only).
  `load_runtime_overlay` (startup re-application of `configs/runtime/latest.yaml`, bounded).
* **Tools** (`ai/tools.py`): `get_features` returns the curated `feature_view` (short keys, 6
  significant digits, None dropped, regime symbols full / other books brief, S1 edges net of fees);
  the default 21-symbol watchlist + 10 S1 coins with every value present is ~3.8 kB < 4096
  (asserted). Engine deps: features, bus books, in-memory candles, market trades, PnL summary, a ring
  buffer of the last 50 decisions (`DecisionLog.listeners`), compact health.
* **Engine** (`engine.py`): supervised `slow_loop` and `reviewer` tasks (the fast loop never awaits
  them), plan persistence (plans table + `kind="plan"` record), atomic plan swap taken once per tick,
  staleness, failure accounting, health `llm_ok` + `extra.ai`, `reload_config`, Telegram alerts and
  commands, `plan_once`. `build_engine` wires AI, overlay and Telegram.
* **CLI**: `wta status`, `wta tui`, `wta plan`, `wta alert`; `wta run --no-runtime-overlay
  --runtime-dir`. `python -m wta.ai.replay` (analyst replay).
* **Tests**: `tests/test_ai_engine.py` (plan on schedule via offline and via a fake provider through
  the real `LLMClient`; persistence; S2 uses the plan target; stale plan ignored; 401 → rules-only +
  `llm_ok=false`; last plan kept until stale; fast loop not delayed by a hanging and by a sleeping LLM
  call, the latter through `engine.run`; reviewer ≤ 2 bounded changes → new `config_hash` on later
  records; runtime overlay at startup; ablation; strict replay incl. misses; Telegram commands →
  engine actions + `source: "telegram"` events, rate-limited digest, LLM-failure and hourly equity
  alerts), `tests/test_ai_wiring.py`, `tests/test_cli_ai.py` (status/tui/plan/alert/run options),
  plus updated `test_ai_tools.py`, `test_analyst.py`, `test_offline_provider.py`, `test_llm_client.py`,
  `test_reviewer.py`.

## 3. Run details

### (a) LLM plans every 60 s

```
python -m wta run --mode shadow --feed synthetic --seconds 900 \
  --set engine.slow_loop_interval_s=60 --set engine.reviewer_interval_s=300 \
  --log-dir <scratch>/acc/logs4 --set data.state_sqlite_path=<scratch>/acc/state4.sqlite \
  --runtime-dir <scratch>/acc/runtime4
```

Start event: `ai: {source: llm, routes: {analyst: openrouter/openai/gpt-5.4-mini, reviewer:
openrouter/openai/gpt-6-luna-pro, fallback: gemini/gemini-4-argon}, mcp: null}`, `config_hash
113103719ae4`, no runtime overlay. Plans (all `tools_source: local`, `cached: false`, `clamped: {}`):

Every plan: `enabled_strategies ["S2"]`, `s1_margin_min_pct 0.002`, `size_multiplier 1`,
`usdt_target_share 0.5`.

| # | reason | tools used | LLM calls | JSON retry | regimes (BTC / ETH / SOL / USDTTMN) |
|---|---|---|---|---|---|
| 1 | startup | – | 2 | yes | range ×4 (0.88–0.93) |
| 2–10 | scheduled | `get_features` (in 7 of 9 also `get_health`, `get_pnl_summary`) | 2 | no | range ×4 (0.84–0.97) |
| 11, 13 | scheduled | – | 2 | yes | range ×4 |
| 12 | scheduled | `get_features`, `get_health`, `get_pnl_summary`, `get_recent_decisions` | 3 | yes | range ×4 |
| 14 | scheduled | `get_features`, `get_health`, `get_pnl_summary` | 2 | no | range ×4 — pasted in ARCHITECTURE §8.8 |
| 15 | scheduled | – | 2 | yes | range / **trend_up 0.71** / range / range |

The model kept S1 off in every plan ("cross-book lock-in remains unattractive after fees: all net
edges negative … versus a 0.43 % round-trip cost floor") and kept S2 at the neutral 0.5 target; with
no candle history in the fresh state DB it reported `ret_1h`/`ret_24h` as unavailable instead of
inventing them. **JSON retries:** 5 of 15 first answers were invalid JSON, always the same slip —
`regimes` not closed before `enabled_strategies`. The "fix the JSON" retry now names that slip
(`analyst._parse` / `FIX_JSON`, added after the first smoke run in which two identical bad answers in
a row cost a cycle); all 5 retries succeeded. Reviewer journal (`logs4/journal.md`, 2 entries,
`gpt-6-luna-pro`): counts and fees right, no parameter change suggested; it flagged "5/10 stale health
records" — a misreading caused by our input (on the synthetic feed `rest_ok`/`ws_ok` are always false),
fixed in `reviewer.build_input` after the run (§4) — and "1 order decision vs `orders_last_hour` 3"
(one S2 rebalance re-priced twice: three placements of one decision; expected).

### (a′) offline rules provider (no key)

Same command with `OPENROUTER_API_KEY= GEMINI_API_KEY=` and 330 s. Every plan:
`[offline rules provider — not an LLM] Rules-only regimes from features: BTCUSDT range (conf 0.30) …
Size multiplier 1 because normal conditions; USDT target 50% from the S2 table for a range dollar; S1
margin kept at the config 0.15%.` (0.30 = no EMA gap yet). The heartbeat says `ai_source:
offline-rules`, `llm_ok: null`; `wta status` prints `llm none: offline rules (no LLM key)`.

### (b) provider failure → rules-only

`OPENROUTER_API_KEY=<invalid> GEMINI_API_KEY= python -m wta run … --seconds 200`. Behaviour with a
key set: the engine stays on the LLM path and shows the outage — it never substitutes the offline
provider (a broken key must not be papered over by rules that look like a plan). If an earlier plan
were still fresh it would stay in force until `ai.plan_stale_s` (1200 s); here there was none, so S1/S2
ran on their rules-only defaults from the first tick. One `ai_degraded` event per outage
(`ai_recovered` when it ends); with Telegram configured an alert goes out at 3 failures in a row
(tested).

### (c) replay

```
python -m wta.ai.replay --log-dir <scratch>/acc/logs4 \
  --set engine.slow_loop_interval_s=60 --set engine.reviewer_interval_s=300 \
  --set data.state_sqlite_path=<scratch>/acc/state4.sqlite
plans recorded 15, reproduced 15, provider calls 0, cache hits 31, cache misses 0
OK: every plan reproduced with zero network calls
```

How: each plan's cycle input (`now`, trigger, the JSON state the engine passed, `config_hash`) and
every tool result were taped (`llm_cache/mcp_tape`) during the run; the replay re-runs
`RegimeAnalyst.replay_cycle` with a strict cache in `replay` mode, the route read from the cached
request, no provider and no MCP client. What remains for the Phase-5 runner: replaying the recorded
**ticks** through the engine (`wta replay`: books/candles → features → strategies → decisions, with
the plan in force at each tick — the new `exchange/replay.py` / `replay/compare.py` are its parts).
Replaying cycles across a reviewer config change and cycles that failed over part-way works since the
review fixes (§6): the tape holds each cycle's config, the plan record every call's route.

## 4. Bugs found by the real runs (fixed, with tests)

1. **Gemini fallback always failed with HTTP 400**: "Unknown name `additionalProperties` at
   `tools[0].function_declarations[0].parameters`". Gemini accepts only an OpenAPI subset and no OBJECT
   without properties. `llm_client.gemini_function` / `gemini_schema` now send only supported keywords
   (and no `parameters` for argument-less tools). After the fix Gemini answered 429 (quota), so the
   fallback is verified against the documented rules in a test, not yet by a successful real call.
2. **Malformed final JSON** (above): targeted retry message; strict validation unchanged (nothing is
   repaired silently).
3. **Reviewer misread synthetic heartbeats as stale** (`rest_ok`/`ws_ok` false on the synthetic feed):
   only the engine's own reasons count there now.
4. `wta status` showed `rest DOWN | ws DOWN` on the synthetic feed and two different plan ages; now
   `rest/ws n/a (synthetic feed)` and one age. The heartbeat counter `llm_calls` was renamed
   `model_calls` (the offline provider is not an LLM); run (a)'s logs still carry the old name.

## 5. Known gaps

* No real-feed plan and no `tools_source: "mcp"` plan yet (API unreachable); the §8.8 TODO stays open.
* Gemini fallback blocked by the account's quota (429).
* `wta plan` takes the account lock, so it refuses to run next to a shadow engine of the same
  account (use `wta status` for the running engine's latest plan).
* Daily analyst/reviewer budgets (now counted per cycle / review, §6) are kept in memory: a restart
  starts a new count. The reviewer cooldown does survive a restart (overlay `applied_ts`).
* `docs/ARCHITECTURE.md` §2 (wiring table), the summary and §9 (known limitations) still describe the
  pre-Phase-4 state; `docs/PROGRESS.md` and `docs/RUNBOOK.md` are not updated (outside this step).
* Nothing is committed or tagged (`v0.4-ai` pending review).

## 6. Review fixes (after the Phase-4 review)

25 reviewer findings were checked against the code; all 25 were real (three pairs were the same defect
reported twice: replay routes, the reload in the middle of a tick, and `run_log_dir`), so 22 fixes. Each fix has a regression test
in `tests/test_phase4_fixes.py` (engine/AI) or `tests/test_phase4_ops.py` (ops), or an updated test.

| Finding | Fix |
|---|---|
| MCP `call_tool` result not masked (key echoed in `isError` / `structuredContent` reached the model, the LLM cache and the tape) | `MCPClient._mask_obj` masks every string of the result (numbers stay `Decimal`); second layer: `RegimeAnalyst(secrets=...)` masks the MCP text before tape, cache and model |
| One `configs/runtime/latest.yaml` for every account (synthetic/paper reviews tuned live) | default runtime dir per account, `configs/runtime/<mode>[_<feed>]` (`wiring.runtime_dir_for`, `config.account_label`); overlay header `# meta {account, mode, feed, git_commit, applied_ts, ...}`; an overlay of another account is refused (`skipped`) |
| Overlay applied with `ai.enabled: false` (ablation contaminated) | not applied; start event `runtime_overlay.skipped: "ai.enabled is false"`, config hash = baseline |
| Replay with one route, no fallback (mid-cycle failover never reproduced) — reported twice | plan record `features.calls` = provider/model/cache_key per call; replay builds the client with every recorded route (older records: first call + start-event routes), a `_NoNetwork` sentinel per provider |
| Replay with the CLI config only (a reviewer change between cycles never replays) | tape `analyst-cycle-*` holds the cycle's whole config; replay rebuilds it per cycle; an older tape that misses under another hash is reported `config mismatch: recorded X, replay Y` |
| Reviewer `reload_config` in the middle of a tick — reported twice | `review_cycle` stores `_pending_cfg` while a tick runs; `tick()` swaps it in first thing; immediate only between ticks |
| Analyst cycle saw a reload half-way (prompt old, clamp/tape/record new) | `RegimeAnalyst.cycle_cfg`: one config per cycle; the plan record is stamped with the cycle's `config_hash` (`DecisionLog.record(config_hash=)`) |
| Pinned `--set`/`--overlay` keys reach only the startup overlay | `ConfigManager(pinned=)` rejects them (`pinned by the operator`); the reviewer prompt never offers them |
| Reviewer write erased an overlay entry pinned at startup | `load_runtime_overlay` returns valid pinned entries in `pinned`; the ConfigManager overlay is seeded with them, so `latest.yaml` keeps them |
| Cooldown in memory only | `applied_ts` in the overlay header (mtime fallback) seeds `ConfigManager.last_apply_ts` at startup |
| `latest.yaml` written non-atomically | `config_manager.atomic_write_text`: temp file, fsync, `os.replace` |
| Analyst budget counted per HTTP request (budget out by ~16:40 UTC every day at the defaults) | budget counts cycles (`RegimeAnalyst(daily_cycle_budget=)`, "daily analyst cycle budget exhausted"); reviews likewise; the client keeps only a ceiling of cycles × (tool rounds + 2) × routes |
| Whole-cycle cap ignored the fallback and MCP calls | cap = `analyst_timeout_s × (rounds + 2) × routes` (+ `MCP_TIMEOUT_S × 8 × rounds + 30` with MCP) |
| Review window without upper bound (outcomes after `now` leaked into the prompt) | `read_log_window(..., until=now)` |
| `run_log_dir` called with one argument: `wta status` / `tui` / `plan` crashed without `--log-dir` (reported twice) | `given` defaults to None; both calls pass `args.log_dir`; CLI tests without `--log-dir` |
| `wta status` never found a live engine by default | without `--mode`/`--log-dir`: the newer heartbeat of `logs` and `logs/shadow` wins |
| Synthetic status fell back to the real-feed state DB / the market DB | legacy fallback on the live feed only; `sources.state_db_expected` names the missing file |
| Docker healthcheck sorted `logs/*/health-*` by path | checks `logs/$WTA_MODE` only, newest mtime, `start_period: 120s` |
| Every engine polled Telegram `getUpdates` (a `/kill` could land in the shadow or flatten engine) | `alerts.telegram_commands_enabled` (default true; false for the compose `shadow` service) and `build_engine(telegram_commands=False)` in `flatten_now`; 3 × HTTP 409 in a row stops the poller and adds a heartbeat reason |
| `run_forever.sh` exited before the child on SIGTERM | waits until the child has exited, then logs `stopped` |
| Crash-loop alert ignored the wrapped command's `--set`/`--overlay` | `run_forever.sh` forwards them; `wta alert` takes `--overlay` |
| `bot_username` never set (`/kill@other_bot` executed) | resolved with `getMe`; while unknown, every `@`-addressed command is ignored |

Re-checked after the fixes (no network, no LLM call):

* `ruff check src tests scripts backtest`: all checks passed. `pytest`: **1796 passed**.
* `python -m wta.ai.replay` on the recorded runs of §3 (records without `calls`, tapes without the
  cycle config): (a) 15/15 identical, 0 provider calls, 0 cache misses; (a′) 6/6 identical. The
  current config hash differs from the recorded one (new field `alerts.telegram_commands_enabled`);
  the plans still reproduce because no prompt value changed.
* A new 130 s offline-rules run (synthetic feed, keys blanked, `reviewer_interval_s=60`): 3 plans,
  1 review, `wta logs check --secrets` OK and clean, replay 3/3 identical with the cycle config read
  from the tape (`config_source: tape`), every plan record carrying `calls`.
* `wta status` and `wta tui --once` without `--log-dir` in the repo work again;
  `wta status --log-dir logs/soak_noop` now shows no state DB (`state_db_expected:
  data/state_shadow_synthetic.sqlite`) instead of the recorder's `data/wta.sqlite`.

Behaviour changes to know: the default reviewer overlay moved from `configs/runtime/latest.yaml` to
`configs/runtime/<account>/latest.yaml` (an existing file at the old place is no longer read; move it
into the account's directory if it should still apply); `config_hash` values changed (new config
field); a lone `@`-addressed Telegram command is ignored until `getMe` has answered.
