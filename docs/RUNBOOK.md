# Runbook — operating wallex-trader-agent

**Summary**
1. Two long-running processes: the **recorder** (Wallex books, trades and candles into `data/wta.sqlite`) and the **agent** (`wta run`); an optional third runs a parallel paper **shadow**. Run them with `docker compose` or bare metal (`scripts/run_forever.sh` + `nohup` + `run/*.pid`).
2. Live trading starts only by hand, through [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md): tests green, a clean tagged tree, `CHALLENGE_END_UTC` set, `WTA_LIVE=1` in the process environment and `--i-understand-live` on the command line.
3. Monitoring: one heartbeat per 60 s in `<log-dir>/health.jsonl`, the status summary, the read-only TUI, and a fixed routine every 6 h (§6). The engine flattens to TMN by itself at T−2h / T−30min (§7).
4. Emergencies: `touch KILL` cancels entries, flattens to TMN and leaves the engine monitoring; only `wta reset-kill` clears it. `make flatten` flattens the paper account, `make flatten-live` the real one (§8).
5. Honest status: `api.wallex.ir` is unreachable from the development machine (connect/TLS timeout), so **no live Wallex data has been recorded yet and every run so far used the synthetic feed**. Several commands named in the PRDs are still placeholders at this commit; §0 lists them with workarounds.

Written against commit `b943545` (main). Re-check §0 before the first live start: other work is in progress.

---

## 0. What works at this commit, and what does not yet

| Item | State at `b943545` | What to do instead |
|---|---|---|
| `wta run --mode shadow\|live` | works; **no strategy and no AI layer is plugged in** (`Engine.intent_sources` is empty, the "S0 no-op strategy"). A run exercises data, risk, orders, reconcile, kill and flatten only. | Strategies (`strategies/`) and the Analyst/Reviewer (`ai/`) exist and are unit-tested; wiring them into `build_engine` is pending. |
| `wta status`, `make status` | CLI placeholder, exits 3 | `.venv/bin/python -c "import time; from pathlib import Path; from wta.config import load_config; from wta.status import status_for_config, format_status; print(format_status(status_for_config(load_config(), time.time(), log_dir=Path('logs/shadow'))))"` |
| `wta tui`, `make tui` | CLI placeholder, exits 3 | `.venv/bin/python -m wta.dashboard.tui --log-dir logs/shadow --db data/wta.sqlite` (`--once` prints one frame) |
| `wta alert` (called by `run_forever.sh`) | not a CLI command; the call fails silently | the restart-loop give-up is still written to `logs/run_forever.log` |
| Telegram alerts and `/status /pause /resume /kill` | `wta/utils/telegram.py` implemented and tested, **not wired into the engine** | watch `health.jsonl` / `events.jsonl` |
| `--overlay configs/live_halfsize.yaml` (checklist §5) | **not accepted** by `wta run` (`unrecognized arguments`, exit 2) | `--set risk.max_order_pct_equity=0.125 --set strategies.s1_crossbook.size_pct_equity=0.10` |
| `wta backtest`, `replay`, `report`, `export` | CLI placeholders, exit 3 | libraries exist (`exchange/backtest.py`, `backtest/`); see `docs/REPRODUCE.md` |
| `make replay` | calls `scripts/replay_check.py`, which does not exist yet | — |
| `data.state_sqlite_path` (`data/state_{mode}.sqlite`) | defined in config and read by `status.py`; **the engine still keeps its state in `data.sqlite_path`** (`data/wta.sqlite`, shared with the recorder) | use `--set data.sqlite_path=...` to separate runs (as `make shadow-synth` does) |
| Reviewer overlay `configs/runtime/latest.yaml` | written by `ConfigManager`, **not loaded** on the next start (`load_config` is called without `runtime_overlay`) | — |

---

## 1. Processes

| Process | Command | Writes | Restart policy |
|---|---|---|---|
| recorder | `python -m wta record --symbols-from configs/markets.yaml` (`make record`) | `data/wta.sqlite`: `books`, `trades`, `candles`, `events`; `logs/recorder-YYYYMMDD.jsonl` (one health line per 60 s) | compose `restart: always`, or `run_forever.sh`; every internal task is supervised (restart after 1, 2, 4 … 60 s) |
| agent | `python -m wta run --mode <shadow\|live> --log-dir logs/<mode>` | `<log-dir>/{decisions,orders,events,health}-YYYYMMDD.jsonl`; engine state in `data.sqlite_path` | same; one engine per account (lock file `<sqlite_path>.engine.lock`) |
| shadow (optional) | `python -m wta run --mode shadow --log-dir logs/shadow` | as the agent | only next to a **live** agent, and with its own `--set data.sqlite_path=...` (see §2) |

The engine has its own order-book feed (REST poller + WebSocket); it does not need the recorder for
books. The recorder is the only source of 1-minute candles for a live-feed run (`candles` table, which
S2/S3 features and the AI tools read) and of the recorded books the survey and backtests read.

Modes: `shadow` (default) fills against the books with `PaperExchange` and never sends an order;
`live` sends orders to Wallex. `--feed synthetic` replaces the Wallex feed with the deterministic
offline feed (`data/synthetic.py`); every `start` event, heartbeat, TUI frame and report then says
"synthetic … never evidence of performance". Live refuses `--feed synthetic`.

---

## 2. Running with docker compose

```bash
cp .env.example .env               # fill keys; never commit .env
docker compose build
docker compose up -d recorder      # market data only
docker compose up -d recorder agent    # + agent in WTA_MODE (shadow unless .env says otherwise)
docker compose logs -f agent
docker compose run --rm agent python -m wta logs check --dir logs/shadow    # one-off commands
docker compose down                # SIGTERM; the running tick finishes before the agent stops
```

What the compose file does (`docker-compose.yml`):

* image `python:3.11-slim`, non-root user, `tini` as PID 1; env from `.env`;
* volumes `./data`, `./logs`, `./configs/runtime` are shared by all services;
* agent command: `python -m wta run --mode ${WTA_MODE:-shadow} --log-dir logs/${WTA_MODE:-shadow}`;
* agent healthcheck (every 60 s, 3 retries): unhealthy when the last `logs/*/health-*.jsonl` in name
  order was not modified for 180 s (3 missed heartbeats). Compose only marks the container unhealthy;
  it does not restart it for that.
* `docker compose down`/`stop` sends SIGTERM and waits 10 s (compose default) before SIGKILL; a normal
  tick finishes well within that.

Caveats:

* **Live under compose is not supported as configured.** `WTA_MODE=live` makes the agent run without
  `--i-understand-live`, so the live guard refuses (exit 2) and `restart: always` loops. Live runs are
  bare metal (§3, checklist §5). A containerised live run has not been tested.
* The `KILL` file is `/app/KILL` **inside** the container (not on a volume). Use
  `docker compose exec agent touch KILL`; a `touch KILL` on the host does nothing to the container.
  Likewise `docker compose exec agent python -m wta reset-kill --log-dir logs/<mode> --reason "..."`.
* The `shadow` profile service (`docker compose --profile shadow up -d shadow`) uses the same
  `data/wta.sqlite` as the agent, so it takes the same account lock: next to a shadow-mode agent it
  exits with `EngineLockError` (exit 2) and restarts in a loop. Give it its own store
  (`--set data.sqlite_path=data/shadow.sqlite`) before using it.

---

## 3. Running bare metal

```bash
make install && make test
mkdir -p run logs
# recorder, kept alive by run_forever.sh
nohup scripts/run_forever.sh record --symbols-from configs/markets.yaml > logs/recorder.out 2>&1 &
echo $! > run/recorder.pid
# agent in shadow mode
nohup scripts/run_forever.sh run --mode shadow --log-dir logs/shadow > logs/agent.out 2>&1 &
echo $! > run/agent.pid
bash scripts/ps.sh                      # or: make ps
```

`scripts/run_forever.sh <wta args>` restarts the command 10 s after it exits (`BACKOFF_S`); more than
20 restarts within one hour (`MAX_RESTARTS_PER_HOUR`) stops the loop, because a crash loop must not
hammer the exchange. Every start, exit code and the give-up line go to `logs/run_forever.log`. A
`SIGTERM` to the loop's PID is forwarded to the child, which finishes its tick and exits cleanly:

```bash
kill "$(cat run/agent.pid)"             # stop the agent (loop + child)
```

`scripts/ps.sh` is read-only and never fails on a missing file. It shows matching processes, every
`run/*.pid` (alive or DEAD), the last line of `logs/recorder-*.jsonl` and `logs/health-*.jsonl`, row
counts per table and books per symbol in the last 10 minutes. It only looks at `logs/` itself: for an
agent started with `--log-dir logs/shadow`, read `logs/shadow/health.jsonl` directly.

For live, never run `wta run --mode live` under `run_forever.sh` before the checklist passed; the
checklist starts it with `nohup env WTA_LIVE=1 … & echo $! > run/agent.pid`.

---

## 4. Going live (summary of `GO_LIVE_CHECKLIST.md`)

The checklist is authoritative; every step is a hard gate. In short:

| Step | Gate |
|---|---|
| 0 Preconditions | `curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://api.wallex.ir/v1/markets` prints `200`; `.env` has the **challenge sub-account** key, `WALLEX_SUB_ACCOUNT_CLIENT_ID`, `CHALLENGE_END_UTC`; recorder fresh (`scripts/ps.sh` shows books for every watchlist symbol in the last 10 min) |
| 1 Code | `make test` green; `git status --porcelain` empty (live refuses a dirty tree, untracked files included) |
| 2 Account (read-only) | `scripts/check_account.py`: ≈ 5,000,000 TMN on the sub-account, no routing warnings, fees 0.25/0.30 % (TMN) and 0.10/0.13 % (USDT), no banned watchlist symbol |
| 3 Order probe (operator only) | `scripts/check_account.py --probe-order`: LIMIT and STOP_LIMIT accepted and cancelled; if STOP_LIMIT is rejected set `execution.use_server_side_stops: false`; commit `docs/REPORTS/api_probe.md` |
| 4 Shadow | ≥ 4 h shadow report on the real feed, no exceptions in `logs/shadow/` |
| 5 Tag and start | `git tag live-YYYYMMDD-HHMM && git push origin <tag>`; start at half size (see §0: use the two `--set` flags, not `--overlay`); first heartbeat within 60 s with `paused=false`, `killed=false`; first S2 order's `client_id` looks like `S2_USDTTMN_<ms>_<hex>` in the Wallex UI (read-only) |
| 6 Full size | after 6 h without incidents: stop, tag a new `live-…`, restart without the half-size flags |
| 7–8 | every-6-h routine (§6) and deadline (§7) |

Live start command with the half-size values (the checklist's command, corrected for §0):

```bash
nohup env WTA_LIVE=1 .venv/bin/python -m wta run --mode live --i-understand-live \
  --set risk.max_order_pct_equity=0.125 --set strategies.s1_crossbook.size_pct_equity=0.10 \
  > logs/agent.out 2>&1 & echo $! > run/agent.pid
```

Without `--log-dir` the engine writes to `logs/` directly (so does `make live`). Choose one log
directory for the whole live run and pass the same one to `reset-kill`, the TUI and the status command.

Live guard (`wta/safety.py`, checked in the CLI, in `build_engine` and again in `Engine.run`), in order:
`--i-understand-live` → `WTA_LIVE=1` in the process environment (a `.env` value does not count) →
`WALLEX_API_KEY` set → clean git tree → `CHALLENGE_END_UTC` set and in the future →
`/v1/markets` reachable (live never trades on the offline market snapshot).

---

## 5. Monitoring

**Heartbeat** — `<log-dir>/health.jsonl` (symlink to the current UTC day's `health-YYYYMMDD.jsonl`),
one line per `engine.heartbeat_interval_s` (60 s):

| Field | Meaning |
|---|---|
| `ts`, `mode`, `git_commit`, `config_hash` | when, which mode, which code and parameters |
| `equity_tmn`, `drawdown_pct` | equity at best bids; drawdown from the peak as a fraction (0.05 = 5 %) |
| `open_orders`, `positions` | counts |
| `rest_ok`, `ws_ok`, `api_errors_1m` | fresh REST / WebSocket data within `engine.stale_data_s` (30 s); REST errors in the last minute |
| `llm_ok` | always `null` at this commit (AI layer not wired) |
| `paused`, `killed` | risk state |
| `reasons[]` | everything that is not normal: stale books, `rest: no fresh data (last error …)`, `ws: stopped by the server (close code …)`, `killed: …`, `paused: …`, `flatten phase N`, valuation gaps |
| `extra` | feed, uptime, ticks, book ages, fees used vs budget, USDT share, coin exposure, orders today / last hour, realized PnL, anomalies, last reconcile, task/tick crash counters, synthetic note |

```bash
tail -n 1 logs/shadow/health.jsonl | python3 -m json.tool
tail -n 20 logs/shadow/events.jsonl            # start/stop/restart/pause/resume/kill/flatten/...
.venv/bin/python -m wta logs check --dir logs/shadow --secrets   # schema + secrets scan, exit 0 = ok
```

**Status summary** (`wta/status.py`, read-only, separate process): latest heartbeat, last start
event, last decisions, last plan, engine flags from the state DB, recorder freshness, `run/*.pid`.
Warnings start with `!!`. Command: §0.

**TUI** (`wta/dashboard/tui.py`, read-only, refresh 2 s, sized for 200×55): header (mode, commit,
config hash, deadline, flatten phase, RUNNING/PAUSED/KILLED), equity, markets with S1 cross-book
edges, health, current plan, positions and open orders, last 10 decisions. Command: §0.

**Recorder** — `logs/recorder-YYYYMMDD.jsonl` (`kind: recorder_health`): rows written per table and
errors per source. `scripts/ps.sh` shows books per symbol in the last 10 minutes.

**Telegram** — implemented (rate limit 20 messages / 10 min, overflow as one digest; operational
commands only, no command can name a symbol, side, price or quantity) but not wired yet (§0).

---

## 6. Every-6-hours routine (operator)

1. `bash scripts/ps.sh`: recorder and agent alive; books for every watchlist symbol in the last 10 min.
2. Status summary (§0) or one TUI frame: no `!!` lines; heartbeat < 180 s old; `paused`/`killed` as expected.
3. `tail -n 50 <log-dir>/events.jsonl`: look for `pause`, `kill`, `tick_error`, `reconcile_drift`,
   `foreign_order`, unexpected `restart`.
4. Health `extra.fee_budget_used_pct` (budget = 2 % of initial equity) and `extra.orders_today` (cap 60).
5. `wc -l <log-dir>/decisions.jsonl`; `wta logs check --dir <log-dir>` exits 0.
6. Disk: `df -h .` and `du -sh data logs` (recorder estimate ≈ 150 MB/day, not yet measured on real data).
7. `logs/journal.md` once the Reviewer is wired.
8. Never place, change or cancel an order by hand on the challenge account (manual trades are prohibited).

---

## 7. Deadline timeline (automatic)

With `CHALLENGE_END_UTC` = `end`:

```
flatten_start_ts = end - engine.flatten_start_before_end_s   # 7200 s  (T-2h)
flatten_final_ts = end - engine.flatten_final_before_end_s   # 1800 s  (T-30min)
final_state_ts   = end - engine.final_state_before_end_s     #  300 s  (T-5min)
```

| Time | Engine action | `events.jsonl` |
|---|---|---|
| T−2h | phase 1: entry/rebalance orders cancelled, no new exposure; every non-TMN balance sold with maker-first LIMITs (re-priced up to `execution.flatten_max_reprices` = 3 times, then crossing) | `flatten` with `kind: flatten_start` |
| T−30min | phase 2: only flatten SELLs; MARKET where the market allows it, else LIMIT at `best_bid × (1 − 0.005)` | `flatten` with `kind: flatten_final` |
| when flat | only TMN and dust left (dust = below the market's minimum order; flatten never buys) | `flatten` with `kind: complete` |
| T−5min | final state logged | `final_state`: "100% TMN (dust only)" or "ALERT: not flat yet: …" |
| T | every open order cancelled; no order is placed any more | `flatten` with `kind: challenge_end` |

Phases only move forward; after a restart late in the run the engine applies every missed boundary in
order. The flatten runs **only while the engine process runs**. Operator checks:

* T−2h+2min: `flatten_start` event present. If not (agent down), stop any agent and run `make flatten-live` (§8.3).
* T−30min+2min: `flatten_final` event present.
* T−5min: `final_state` says 100 % TMN; otherwise run `make flatten-live` at once.
* After the end: stop the agent, copy logs to `deliverables/`, commit, tag `final`, push.

---

## 8. Emergency procedures

### 8.1 Kill switch
```bash
touch KILL                  # repo root; under compose: docker compose exec agent touch KILL
```
At the next fast tick (≤ 2 s) the engine persists `killed=true` (committed at once), writes a `kill`
event, cancels its entry/rebalance orders and flattens every non-TMN balance to TMN (phase 1 rules).
The process keeps running: heartbeats continue and only exit/stop/flatten SELLs pass the RiskManager.
A kill also fires automatically on drawdown ≥ 5 % from the peak or on ≥ 3 fast-loop crashes within
600 s (`exception storm`). A kill survives restarts.

### 8.2 Reset after a kill (manual only)
```bash
rm KILL                                         # first; reset-kill refuses while the file exists
.venv/bin/python -m wta reset-kill --log-dir logs/<mode> --reason "why it is safe to resume"
```
It writes a `reset_kill` event and a kv record in `data.sqlite_path`; a running engine applies it on its
next tick (`reset_kill_applied`), otherwise the next start does. The reset moves the peak to the
current equity (the old drawdown must not kill again at once) and cancels the kill's flatten orders
unless a deadline flatten is running.

### 8.3 Flatten now (outside the deadline schedule)
The account lock allows one trading process per store: **stop the agent first** (exit 2 otherwise).
```bash
make flatten                 # paper: the shadow store's kv paper_state; books from the live feed
.venv/bin/python scripts/flatten_now.py --feed synthetic --paper-balances '{"TMN": 1000000, "USDT": 5}'
make flatten-live            # REAL account: WTA_LIVE=1 + --i-understand-live; API key; clean tree
```
`flatten_now.py` runs the engine's own flatten (FlattenManager → RiskManager → OrderManager, decision
records like any order): phase 1 for `--phase1-seconds` (60), then phase 2, until only TMN and dust
remain (exit 0) or `--timeout` (600 s, exit 1, leftovers listed). It prints balances and equity before
and after. It does not need `CHALLENGE_END_UTC`.

### 8.4 API outage
* The REST client retries 429/5xx/timeouts with 1, 2, 4, 8 s backoff (max 60 s); the poller keeps polling.
* No USDTTMN book for `engine.stale_data_s` (30 s), or ≥ 5 consecutive API errors, or TMN/USDT books
  disagreeing by ≥ 5 % → **anomaly pause**: no new entries; working entry orders are cancelled.
  Resume is automatic after the condition is clear for `risk.anomaly_clear_s` (60 s).
* `STALE_DATA` also blocks exits on stale books. Server-side STOP_LIMIT orders stay on the exchange and
  still protect positions while we are blind.
* After the error burst ends, an extra reconcile runs (missed fills, foreign orders, balance drift).
* An order whose send failed ambiguously is never re-sent blindly: it is looked up by `client_id`.

### 8.5 Restarting a process
* **Agent**: stop with SIGTERM (never `kill -9` during trading). On start the engine restores peak
  equity, day-start equity, fees, order counters, realized PnL, kill/pause/flatten flags, positions and
  open orders from the store, waits up to 30 s for the books it needs, then reconciles with the
  exchange. Events: `start` (`reason: restart`) and `restart` with both summaries.
* **Recorder**: safe at any time (`kill "$(cat run/recorder.pid)"`; restart as in §3). Candles are
  upserted, so overlaps are harmless; a gap in `books` stays a gap (the survey reports gaps > 10 s).
  The agent keeps trading on its own feed meanwhile.

---

## 9. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `wta markets` / `check_account.py` fail with `ConnectTimeout`; `feed` prints "waiting for data"; heartbeat `rest_ok=false`, `reasons: rest: no fresh data` | `api.wallex.ir` unreachable. **Observed on the dev machine**: the connection/TLS handshake times out (likely IP filtering); `docs/REPORTS/api_probe.md` | test with the `curl` in §4 step 0; use another network/VPS. Shadow on the real feed is useless until it prints 200; `--feed synthetic` only exercises the engine |
| `wta run` takes long to start, then `markets_source: snapshot` | `/v1/markets` failed after retries; markets come from `configs/markets_snapshot.yaml` | fine for shadow/synthetic; live refuses to start (by design) |
| heartbeat `stale books (> 30s): …` | REST poll failing or a symbol missing from `/v2/depth/all` | check `extra.rest.last_error`; the poller fills missing symbols per symbol |
| `ws: stopped by the server (close code ≥ 3500, not retried)` | Centrifugo "do not reconnect" close | REST polling (2 s) carries the data; restart the agent to try the WS again |
| `paused: anomaly: no USDTTMN book` | dollar book stale > 30 s | see §8.4; resumes by itself |
| `paused: daily_loss …` | daily PnL ≤ −2.5 % | resumes at the next UTC day; nothing to do |
| `killed: drawdown_kill …` / `exception storm …` | §8.1 | read `events.jsonl` and the decision log before §8.2 |
| `error: another engine or flatten_now (pid N) is running on data/wta.sqlite` (exit 2) | two trading processes on one store | stop one; for parallel runs use `--set data.sqlite_path=...` |
| `refusing to start live mode: …` (exit 2) | a live guard (§4) | the message names the failed guard |
| `unrecognized arguments: --overlay …` | §0 | use `--set` |
| `wta status` / `wta tui` print "not implemented yet" (exit 3) | §0 | module commands in §0 |
| LLM provider down, no key, invalid JSON twice | AI layer (once wired) | the Analyst returns no plan; the last plan is used until 20 min old (`ai.plan_stale_s`), then strategies run rules-only; without any key the offline rules provider (not an LLM) is used |
| `run_forever: … restarted N times in 1 h - giving up` in `logs/run_forever.log` | crash loop | read `logs/agent.out` for the traceback; fix, then start again |
| disk filling | recorder books (10 levels per side) and `engine_books` | `wta db vacuum` (stop writers first), move old `logs/*-YYYYMMDD.jsonl` to `deliverables/` |
| "SYNTHETIC" banner in TUI/status/reports | a `--feed synthetic` run | expected; never quote those numbers as performance |

---

## 10. Data, logs and state layout

```
data/                              (gitignored)
  wta.sqlite                       data.sqlite_path: recorder market data AND (at this commit) engine state
  wta.sqlite.engine.lock           one trading process per store (pid inside)
  shadow_synth.sqlite              make shadow-synth (synthetic run, separate store)
  state_{mode}.sqlite              data.state_sqlite_path: intended per-mode engine state (not used by the engine yet, §0)
logs/                              (gitignored)
  recorder-YYYYMMDD.jsonl          recorder health lines
  run_forever.log, agent.out, recorder.out
  journal.md                       Post-trade Reviewer journal (once wired)
  llm_cache/<sha256>.json          LLM request/response cache; llm_cache/mcp_tape/ for MCP results
  <mode>/                          --log-dir logs/<mode> (shadow, live, shadow_synth); make live / checklist: logs/
    decisions-YYYYMMDD.jsonl       one record per decision (intent, veto, order, plan, outcome, no_trade, event)
    orders-YYYYMMDD.jsonl          every order request/response/cancel/fill, raw
    events-YYYYMMDD.jsonl          start, restart, stop, pause, resume, kill, flatten, final_state, reset_kill(_applied),
                                   day_start, tick_error, reconcile, reconcile_drift, foreign_order, config_change
    health-YYYYMMDD.jsonl          heartbeats
    <family>.jsonl                 symlink to the current UTC day's file
run/*.pid                          (gitignored) bare-metal PIDs
configs/runtime/                   (gitignored) reviewer overlays config-<ts>.yaml, latest.yaml
KILL                               (gitignored) kill-switch file
deliverables/                      (committed at the end) logs, reports, journal, llm cache, video link
```

JSONL files are append-only, one canonical JSON object per line (sorted keys, Decimals as strings),
rotated per UTC day, with every configured secret replaced by `***` before the write.

SQLite tables (WAL mode; readers such as the TUI open it read-only):

| Table | Written by | Content |
|---|---|---|
| `books`, `trades` | recorder | order books (≤ 10 levels, identical snapshots deduplicated), public trades |
| `candles` | recorder (`CandlePoller`), `wta backfill`, synthetic feed | 1-minute OHLCV, upsert by `(symbol, resolution, ts)` |
| `engine_books`, `ticks` | engine | replay inputs: every book a tick used, one row per tick with the book timestamps |
| `orders`, `fills`, `positions` | engine | order state, fills, positions (written through) |
| `balances`, `equity` | engine | balance snapshots on change; equity row every 10 s |
| `kv` | engine, `reset-kill` | `portfolio_state`, `engine_flags`, `order_manager_state`, `paper_state` (shadow), `kill_reset` |
| `plans` | engine (once the Analyst is wired) | MarketPlan JSON + `prompt_hash` |
| `events` | recorder, engine, `reset-kill` | mirror of `events.jsonl` plus `recorder_start/stop` |

One fast tick is one SQLite commit: orders, fills, positions and kv state always move together, so a
crash restores the state as of the end of a tick. `wta db vacuum [--path …]` compacts a store.
