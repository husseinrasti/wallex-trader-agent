# Runbook — operating wallex-trader-agent

**Summary**
1. Two long-running processes: the **recorder** (`wta record`: Wallex books, trades and candles into the market DB `data/wta.sqlite`) and the **agent** (`wta run`). Each engine account (`live`, `shadow`, `shadow_synthetic`) has its own state DB, lock, log directory, LLM cache and reviewer overlay directory, so a paper engine can run next to the live one.
2. Live trading starts only by hand, after the operator says "go live", through [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md): green tests, a clean tagged tree, `CHALLENGE_END_UTC` set, `WTA_LIVE=1` in the process environment, `--i-understand-live`, and `--overlay configs/live_halfsize.yaml` for the first 6 h.
3. Monitoring: a heartbeat per 60 s in `<log dir>/health.jsonl`, `wta status`, the read-only `wta tui`, optional Telegram alerts, and a routine every 6 h (§7). The engine flattens to TMN by itself at T−2h / T−30min (§8).
4. Emergencies: `touch KILL` (or Telegram `/kill`) cancels entries, flattens to TMN and leaves the engine monitoring; only `wta reset-kill` clears it. `make flatten-live` flattens the real account with the agent stopped (§9).
5. Honest status: `api.wallex.ir` is reachable from the dev machine only intermittently (§10); the real data is about 20 minutes of REST books from 2026-10-22. The evidence runs used the labelled synthetic feed with the real OpenRouter LLM. No live trading has happened.

The offline commands below were run on 2026-10-23 at `v0.5-backtest` (`85acc16`) plus the Phase-6
working tree (`status`, `tui --once`, `plan` with the offline provider, `alert`, `logs check`,
`config hash`, `report`, `export`, a synthetic `backtest`, `replay`, `make replay-deliverable`,
`reset-kill`, a synthetic `flatten_now.py`, `docker compose config`). Commands that need the Wallex
API, an API key, the real account or a live run are marked; they were exercised against fixtures and
in the acceptance runs (`docs/REPORTS/phase*_acceptance.md`). `wta` means `.venv/bin/python -m wta`
(`python -m wta` inside the container).

---

## 1. Command reference

### 1.1 `wta` subcommands (`src/wta/cli.py`)

Every subcommand takes `--set KEY=VALUE` (repeatable) and `--log-level`. Exit codes: 0 ok, 1 a check
failed, 2 cannot run (bad arguments, a guard, the account lock), 130 interrupted.

| Command | What it does | Notes |
|---|---|---|
| `wta run [--mode shadow\|live]` | runs the engine (shadow by default) | flags in §4.1; live needs the live guard (`GO_LIVE_CHECKLIST.md`) |
| `wta record --symbols-from configs/markets.yaml` | recorder: books, trades, candles into `data.sqlite_path` | `--no-ws` for REST only; `--seconds N` |
| `wta status [--mode] [--feed] [--log-dir] [--json]` | one-screen summary: heartbeat, state DB, flags, plan, AI, processes, recorder | read-only; without `--log-dir`/`--mode` it picks `logs` or `logs/shadow`, whichever has the newer heartbeat |
| `wta tui [--once] [--width 200] [--refresh 2]` | `rich` dashboard over the same data | read-only, separate process; `--once` prints one frame |
| `wta plan [--feed live\|synthetic]` | ONE analyst cycle on the shadow account's data; prints the plan JSON | stores only the LLM cache and tape; refuses (exit 2) while a shadow engine holds that account's lock; uses the LLM when a key is set |
| `wta alert TEXT` | one Telegram message | no-op (exit 0) without `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `alerts.telegram_enabled` |
| `wta reset-kill --reason "..." [--mode] [--feed] [--log-dir]` | clears the kill-switch by hand | §9.2 |
| `wta logs check [--dir DIR] [--secrets]` | validates the JSONL logs; `--secrets` also scans `deliverables/` and `docs/REPORTS/` | exit 1 on any finding |
| `wta config show [--full] [--overlay F]` / `wta config hash [--overlay F]` | resolved config and its `config_hash` | the hash depends on `--set`, overlays and the `${VAR}` values in `.env` |
| `wta markets` | watchlist market rules from `/v1/markets`; exit 1 if a symbol is banned | needs the API |
| `wta account [--all]` | balances and fee rates | needs the API and `WALLEX_API_KEY` |
| `wta feed SYMBOL [--seconds N] [--no-ws]` | best bid/ask once per second | needs the API |
| `wta backfill [--days 30] [--resolution 1]` | historical candles into the market DB | needs the API |
| `wta backtest --driver books\|candles --from ISO --to ISO [--ai off\|on\|offline]` | the engine over stored data; report, PNGs, `summary.json` | `--synthetic-db PATH` generates SYNTHETIC data; `--param-grid` runs the walk-forward |
| `wta report --source LOGDIR --out DIR [--compare LOGDIR]` | report of a shadow/live run from its logs and state DB | `--update-index` regenerates `docs/REPORTS/README.md` |
| `wta export [--parquet DIR] [--db PATH]` | market tables to Parquet | writes CSV when `pyarrow` is missing (it is not installed in `.venv`) |
| `wta replay [--log-dir DIR] [--state-db PATH] [--out DIR] [--reports-dir DIR]` | re-runs a recorded run and compares every decision | default `--log-dir logs`; writes `docs/REPORTS/replay_<date>.md` unless `--reports-dir ''` |
| `wta db vacuum [--path PATH]` | compacts a SQLite DB | stop its writers first |

### 1.2 `make` targets (`Makefile`)

| Target | Runs |
|---|---|
| `make install` | venv + `pip install -e ".[dev]"` |
| `make test` | `ruff check src tests backtest scripts`, then `pytest` (quiet through `addopts` in `pyproject.toml`) |
| `make record` | `wta record --symbols-from configs/markets.yaml` (foreground) |
| `make shadow` | `wta run --mode shadow --log-dir logs/shadow` (real feed, paper account `shadow`) |
| `make shadow-synth` | `wta run --mode shadow --feed synthetic --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite` (state DB `data/shadow_synth.state_shadow_synthetic.sqlite`); never evidence |
| `make live` | `WTA_LIVE=1 wta run --mode live --i-understand-live`: **full size**, foreground. The checklist uses its own command with the half-size overlay instead |
| `make backtest` | 30-day candle backtest of real candles, S2+S3 with `allow_rules_only` (as the shipped candle reports), `AI=off` (override `FROM`, `TO`, `AI`); writes `docs/REPORTS/bt_candles_30d_real/`; needs a candle backfill |
| `make backtest-books` | book backtest, S1+S2, over the recorded books (`BOOKS_FROM`, `BOOKS_TO`, `AI`) |
| `make backtest-synth` | the book backtest on a generated 6-h SYNTHETIC DB (writes `logs/synthetic_bt_books`, gitignored) |
| `make backtest-cache` | the AI ablation's real-LLM run (6-h SYNTHETIC books) from its committed LLM cache with `--ai cache`: no key, no provider call, a miss is exit 1 (writes `logs/bt_cache_synth24h_6h`) |
| `make market-db` | `scripts/market_db.py`: unpacks the committed real market data `deliverables/market/wta.sqlite.xz` into `data/wta.sqlite` (checksum verified; never overwrites) |
| `make replay` | `scripts/replay_check.py --decisions logs/decisions.jsonl --reports-dir logs --no-index`: the live run if one exists, else the newest shadow run under `logs/` (today: `logs/shadow_synth_4h`); report in `logs/replay_<date>.md`; exit 1 on any mismatch |
| `make replay-report` | the same, but writes `docs/REPORTS/replay_<date>.md` and the reports index (to regenerate the committed proof) |
| `make final-report` | `scripts/assemble_report.py`: the reports index, `deliverables/REPORT.md` and its deliverables index, from `docs/REPORTS` |
| `make replay-deliverable` | `scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h`: the same replay from the committed bundle (checksums verified, unpacked to a temporary directory, replayed, removed); needs no `logs/` or `data/` |
| `make deliverables` | `scripts/make_deliverables.py --run-dir $(DELIVER_RUN) --bundle --replay`: copies a recorded run (default `logs/shadow_synth_4h`) into `deliverables/` with its replay bundle, secrets-scanned |
| `make report` | `wta report --source $(REPORT_SOURCE) --out $(REPORT_OUT)` (default `logs/shadow` → `docs/REPORTS/shadow_report`) |
| `make export` | `wta export --parquet data/export` |
| `make survey` | `scripts/survey.py --since 24h` (writes the survey into `docs/REPORTS/`) |
| `make flatten` | `scripts/flatten_now.py`: flattens the **paper** account `shadow` (its records go to `logs/`, see §9.3) |
| `make flatten-live` | `WTA_LIVE=1 scripts/flatten_now.py --i-understand-live`: flattens the **real** account |
| `make status` / `make tui` | `wta status` / `wta tui` |
| `make ps` | `bash scripts/ps.sh` (processes, pid files, last recorder line, market DB row counts) |
| `make docker` | `docker build -t wallex-trader-agent .` |

### 1.3 Scripts

| Script | Purpose |
|---|---|
| `scripts/check_account.py [--probe-order]` | API and account probe; writes `logs/api_probe.md` (gitignored: it holds raw balance payloads; account ids appear only as their last 4 characters). `--probe-order` places and cancels two far-from-market orders: **operator only**, after typing `PROBE` |
| `scripts/flatten_now.py [--i-understand-live] [--feed] [--log-dir] [--phase1-seconds 60] [--timeout 600]` | the engine's own flatten, now (§9.3) |
| `scripts/replay_check.py [--log-dir DIR \| --decisions FILE \| --bundle DIR] [--reports-dir ''] [--json]` | the judges' replay check (`make replay`, `make replay-deliverable`) |
| `scripts/run_forever.sh <wta args>` | restart loop for bare metal (§4) |
| `scripts/ps.sh` | read-only process and data overview |
| `scripts/survey.py`, `scripts/shadow_report.py` | market survey; the older shadow report (`wta report` replaces it) |
| `python -m wta.ai.replay --log-dir DIR` | re-derives every plan of a run from its LLM cache alone (analyst only, zero network) |

---

## 2. Processes and layout

| Process | Command | Account | State DB | Log dir | Reviewer overlays | Telegram commands |
|---|---|---|---|---|---|---|
| recorder | `wta record --symbols-from configs/markets.yaml` | — | market DB `data/wta.sqlite` | `logs/recorder-YYYYMMDD.jsonl` | — | — |
| live agent | `WTA_LIVE=1 wta run --mode live --i-understand-live --overlay configs/live_halfsize.yaml` | `live` | `data/state_live.sqlite` | `logs/` | `configs/runtime/live/` | yes (the bot's one poller) |
| paper agent | `wta run --mode shadow` (`make shadow`) | `shadow` | `data/state_shadow.sqlite` | `logs/shadow/` | `configs/runtime/shadow/` | only when no other engine polls the bot; next to a live agent add `--set alerts.telegram_commands_enabled=false` |
| synthetic exercise | `wta run --mode shadow --feed synthetic --log-dir logs/<name> --set 'data.state_sqlite_path=data/state_<name>_{mode}.sqlite'` | `shadow_synthetic` | as set (default `data/state_shadow_synthetic.sqlite`) | as given | `configs/runtime/shadow_synthetic/` | Telegram is off by default; if it is on, add `--set alerts.telegram_commands_enabled=false` |

* **Lock.** Each state DB has a lock file `<state db>.engine.lock` with the pid inside. A second
  `wta run`, `wta plan` or `flatten_now.py` on the same account exits with `EngineLockError` (exit 2).
  Two synthetic exercises at once need different `data.state_sqlite_path` values.
* **LLM cache.** `<log dir>/llm_cache/` (live `logs/llm_cache`, paper `logs/shadow/llm_cache`), with
  `mcp_tape/` inside. Keep it with the logs: replays need it.
* **Reviewer overlays.** The reviewer writes `configs/runtime/<account>/config-<ts>.yaml` and
  `latest.yaml`; the next start of the same account re-applies `latest.yaml` (bounded, never with
  `ai.enabled: false`). `--runtime-dir DIR` moves it, `--no-runtime-overlay` skips it.
* The engine has its own book feed (REST poller + WebSocket) and candle task; it does not need the
  recorder to trade. The recorder fills the market DB that the survey and the backtests read.
* `WTA_MODE` in `.env` is read only by `docker compose`. `wta run` is shadow unless `--mode live` is
  given.

---

## 3. Running with docker compose

```bash
cp .env.example .env                    # fill keys; never commit .env
docker compose build
docker compose up -d recorder           # market data only
docker compose up -d recorder agent     # + agent in WTA_MODE (shadow unless .env says otherwise)
docker compose logs -f agent
docker compose run --rm --no-deps agent python -m wta logs check --dir logs/shadow    # one-off commands (--no-deps: no recorder)
docker compose down                     # SIGTERM; the running tick finishes before the agent stops
```

What `docker-compose.yml` does:

* image `python:3.11-slim`, non-root user, `tini` as PID 1, env from `.env`, except `WTA_LIVE`: each
  service pins `WTA_LIVE: "0"` in its own `environment`, so a `WTA_LIVE=1` left in `.env` never arms
  the live guard inside a container;
* volumes `./data`, `./logs` and `./configs/runtime` are shared by all services;
* agent: `python -m wta run --mode ${WTA_MODE:-shadow} --log-dir logs/${WTA_MODE:-shadow}`,
  `restart: always`; healthcheck every 60 s: unhealthy when no `logs/$WTA_MODE/health-*.jsonl` was
  modified in the last 180 s. **Compose only marks the container unhealthy; it does not restart it**
  (plain Docker has no restart-on-unhealthy). A hung agent under compose therefore needs
  `docker compose restart agent` by hand, an autoheal sidecar, or the bare-metal
  `scripts/run_forever.sh`, whose watchdog does restart it (§4);
* optional `shadow` profile (`docker compose --profile shadow up -d shadow`): a second engine with
  `--set alerts.telegram_commands_enabled=false`, meant to run next to a live agent.

Caveats:

* **Live under compose is not supported as configured.** `WTA_MODE=live` runs the agent without
  `--i-understand-live`, so the live guard refuses (exit 2) and `restart: always` loops. Live runs
  bare metal (checklist §5).
* The `shadow` profile uses the `shadow` account (`data/state_shadow.sqlite`). Next to an agent in
  `WTA_MODE=shadow` it takes the same lock, exits with exit 2 and restarts in a loop.
* The `KILL` file is `/app/KILL` **inside** the container (not on a volume): use
  `docker compose exec agent touch KILL`. Likewise
  `docker compose exec agent python -m wta reset-kill --log-dir logs/<mode> --reason "..."`.

---

## 4. Running bare metal

```bash
make install && make test
mkdir -p run logs
# recorder, kept alive by run_forever.sh
nohup scripts/run_forever.sh record --symbols-from configs/markets.yaml > logs/recorder.out 2>&1 &
echo $! > run/recorder.pid
# paper agent on the real feed
nohup scripts/run_forever.sh run --mode shadow --log-dir logs/shadow > logs/agent_shadow.out 2>&1 &
echo $! > run/shadow.pid
bash scripts/ps.sh                       # or: make ps
```

`scripts/run_forever.sh <wta args>` restarts the command 10 s after it exits (`BACKOFF_S`). More than
20 restarts within one hour (`MAX_RESTARTS_PER_HOUR`) stops the loop and sends `wta alert` (with the
wrapped command's `--set` / `--overlay` arguments, so a Telegram enabled by `--set` reaches the alert
too). Every start, exit code and the give-up line go to `logs/run_forever.log`.

**Watchdog (PRD-06 §2).** A child that is alive but silent is restarted too. For `run` the heartbeat
is the newest `<--log-dir>/health-*.jsonl`, for `record` the newest `logs/recorder-*.jsonl`; both are
written every 60 s. When the newest one is more than `WATCHDOG_S` (180 s) old, checked every
`WATCHDOG_POLL_S` (15 s) once `WATCHDOG_GRACE_S` (300 s) have passed since the start (start-up with an
unreachable API takes about 2.5 min), the loop logs `watchdog: no heartbeat in ... for Ns`, sends
`wta alert`, SIGTERMs the child (SIGKILL after `WATCHDOG_KILL_S`, 30 s) and restarts it like after a
crash: the back-off and the hourly cap apply. A heartbeat file older than the child's start never
counts. `WATCHDOG_S=0` turns the watchdog off. A `SIGTERM` to the
loop's pid is forwarded to the child, and the loop waits until the child has finished its tick:

```bash
kill "$(cat run/shadow.pid)"             # stop the paper agent (loop + child)
```

The live agent is started by the checklist with `nohup env WTA_LIVE=1 … & echo $! > run/agent.pid`,
not under `run_forever.sh` (a crash loop on the real account should stop, not retry for an hour).

### 4.1 `wta run` flags

| Flag | Meaning |
|---|---|
| `--mode shadow\|live` | paper (default) or real orders (live guard) |
| `--i-understand-live`, `--allow-dirty` | live flag; dirty tree allowed (dev only, never for the challenge) |
| `--overlay FILE` | YAML overlay merged over `configs/*.yaml`, repeatable (e.g. `configs/live_halfsize.yaml`); its keys are pinned against the reviewer; named in the `start` event |
| `--set KEY=VALUE` | config override, repeatable; wins over overlays; pinned against the reviewer |
| `--no-runtime-overlay` | do not re-apply the reviewer's `latest.yaml` at startup |
| `--runtime-dir DIR` | where the reviewer writes (and startup reads) its overlays; default `configs/runtime/<account>` |
| `--log-dir DIR` | JSONL logs; default `logs` for live, `logs/shadow` for shadow |
| `--feed live\|synthetic` | Wallex REST/WS (default) or the seeded offline feed (shadow only, never evidence) |
| `--seconds N` | stop after N seconds |
| `--inject-intents FILE` | JSONL intents for acceptance tests (shadow only; marked `injected` in the log) |
| `--paper-balances JSON` | starting paper account for a state DB without one, e.g. `'{"TMN": 3000000, "USDT": 10}'` |
| `--synthetic-shock-at S --synthetic-shock-pct P` | a synthetic shock (e.g. `-0.08`) S seconds after start (`--feed synthetic` only) |

Useful `--set` keys: `alerts.telegram_enabled=true` (Telegram is off by default),
`alerts.telegram_commands_enabled=false` (any engine that must not own the bot's commands),
`ai.enabled=false` (rules only), `data.state_sqlite_path=...` (a separate account file),
`engine.slow_loop_interval_s=60` (faster plans for a demo; changes `config_hash`).

---

## 5. Going live (summary of `GO_LIVE_CHECKLIST.md`)

The checklist is authoritative; every step is a hard gate.

| Step | Gate |
|---|---|
| 0 Preconditions | `api.wallex.ir` answers 200 (several times, it is intermittent); `.env` has the challenge account key, `CHALLENGE_END_UTC` and `WALLEX_SUB_ACCOUNT_CLIENT_ID`; recorder fresh |
| 1 Code | `make test` green; `git status --porcelain` empty |
| 2 Account | `scripts/check_account.py`: ≈ 5,000,000 TMN on the sub-account, no routing warning, fees as expected |
| 3 Order probe | `scripts/check_account.py --probe-order` (operator only) |
| 4 Shadow | ≥ 4 h paper run on the real feed, report written |
| 5 Tag and start | `git tag live-YYYYMMDD-HHMM`, push the tag; start with `--overlay configs/live_halfsize.yaml` |
| 6 Full size | after 6 h without incidents: stop, new tag, restart without the overlay |
| 7–8 | every-6-h routine (§7) and the deadline (§8) |

---

## 6. Monitoring

**Heartbeat** — `<log dir>/health.jsonl` (symlink to the current UTC day's file), one line per
`engine.heartbeat_interval_s` (60 s) and one at shutdown:

| Field | Meaning |
|---|---|
| `ts`, `mode`, `git_commit`, `config_hash` | when, which mode, which code and parameters |
| `equity_tmn`, `drawdown_pct` | equity at best bids; drawdown from the peak as a fraction (0.05 = 5 %) |
| `open_orders`, `positions`, `paused`, `killed` | counts and risk state |
| `rest_ok`, `ws_ok`, `api_errors_1m` | fresh REST / WebSocket data within 30 s (both `false` on the synthetic feed); failed API attempts in the last minute |
| `llm_ok` | `true` after a good analyst cycle, `false` after a failed one, `null` without an LLM (disabled, offline rules, or no cycle yet) |
| `reasons[]` | everything that is not normal: stale books, `rest: no fresh data (last error …)`, `ws: stopped by the server (close code …)`, `llm: N failed analyst cycle(s) in a row …`, `plan stale …`, `killed: …`, `paused: …`, `flatten phase N`, Telegram poller stopped, valuation gaps |
| `extra` | feed, uptime, ticks, book ages, fees used vs budget, USDT share, coin exposure, orders today / last hour, realized PnL, anomalies, last reconcile, crash counters; `ai_source` and `ai` (plan age, model, failures, routes, last review); `strategies` (S1 per coin: edge, cost, need, z-score, why no trade, opportunity-tick counters; S2 target and status; S3 status) |

```bash
tail -n 1 logs/health.jsonl | python3 -m json.tool              # live; logs/shadow/... for paper
tail -n 20 logs/events.jsonl
.venv/bin/python -m wta logs check --dir logs --secrets          # schema + secrets scan, exit 0 = ok
```

**Status.** `wta status --mode live` (or `--mode shadow`, or `--log-dir DIR`): the latest heartbeat,
the last start event, the last decisions, the plan in force (model, age, size, USDT target, regimes,
notes), the engine flags from the state DB, recorder freshness and `run/*.pid`. Warnings start with
`!!`. For a run with a custom state DB pass the same `--set`, e.g.
`wta status --log-dir logs/shadow_synth_4h --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'`.

**TUI.** `wta tui --mode live` (read-only, refresh 2 s, sized for 200×55): header (mode, feed, commit,
config hash, deadline, flatten phase, RUNNING/PAUSED/KILLED), equity, markets with S1 cross-book
edges, health, current plan, positions and open orders, last decisions. A synthetic run shows a
SYNTHETIC banner.

**Recorder.** `logs/recorder-YYYYMMDD.jsonl` (`kind: recorder_health`, one per minute): rows written
per table (since start and in the last minute), errors per source, `rest_ok`, `ws_ok`, missing
symbols. `scripts/ps.sh` shows books per symbol in the last 10 minutes.

**Telegram** (optional). Needs `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (empty in `.env` today) and
`--set alerts.telegram_enabled=true`. Alerts: start/stop/restart, every fill, pause/resume/kill,
flatten phases, config changes, `final_state`, tick errors, 3 failed analyst cycles in a row, a book
older than 120 s, API error bursts, equity every hour; at most 20 messages per 10 minutes, the rest
as one digest. Commands from the configured chat only: `/status`, `/pause`, `/resume`, `/kill`
(operational only, `RISK.md` §6).

---

## 7. Every-6-hours routine (operator)

1. `bash scripts/ps.sh`: recorder and agent alive; books for every watchlist symbol in the last 10 min.
2. `wta status --mode live` (or one `wta tui --once --width 200` frame): no `!!` lines; heartbeat
   < 180 s old; `paused`/`killed` as expected; `llm_ok` true or the reason known.
3. `tail -n 50 logs/events.jsonl`: look for `pause`, `kill`, `tick_error`, `reconcile_drift`,
   `foreign_order`, unexpected `restart`, `ai_degraded`, `plan_stale`, `config_change`.
4. Heartbeat `extra.fee_budget_used_pct` (budget = 2 % of initial equity) and `extra.orders_today`
   (cap 60).
5. `wta logs check --dir logs --secrets` exits 0.
6. `logs/journal.md`: the Reviewer's latest section; any applied change matches a `config_change`
   event.
7. Disk: `df -h .` and `du -sh data logs` (the state DB grows about 26 MB per hour of ticks: the
   4-h run's DB is 108 MB).
8. Never place, change or cancel an order by hand on the challenge account (manual trades are
   prohibited); the web UI is read-only for us.

---

## 8. Deadline timeline (automatic)

With `CHALLENGE_END_UTC` = `end`:

```
flatten_start_ts = end - engine.flatten_start_before_end_s   # 7200 s  (T-2h)
flatten_final_ts = end - engine.flatten_final_before_end_s   # 1800 s  (T-30min)
final_state_ts   = end - engine.final_state_before_end_s     #  300 s  (T-5min)
```

| Time | Engine action | `events.jsonl` |
|---|---|---|
| T−2h | phase 1: entry/rebalance orders cancelled, no new exposure; every non-TMN balance sold with maker-first LIMITs (re-priced up to 3 times, then crossing) | `flatten` with `kind: flatten_start` |
| T−30min | phase 2: only flatten SELLs; MARKET where allowed, else LIMIT at `best_bid × (1 − 0.005)` | `flatten` with `kind: flatten_final` |
| when flat | only TMN and dust left (dust = below the market's minimum order; flatten never buys) | `flatten` with `kind: complete` |
| T−5min | final state logged | `final_state`: "100% TMN (dust only)" or "ALERT: not flat yet: …" |
| T | every open order cancelled; no order is placed any more | `flatten` with `kind: challenge_end` |

Phases only move forward; after a restart late in the run the engine applies every missed boundary
in order. The flatten runs **only while the engine process runs**. Operator checks:

* T−2h+2min: `flatten_start` event present. If not (agent down), run `make flatten-live` (§9.3).
* T−30min+2min: `flatten_final` event present.
* T−5min: `final_state` says 100 % TMN; otherwise stop the agent and run `make flatten-live` at once.
* After the end: stop the agent, copy the logs to `deliverables/`, commit, tag `final`, push.

---

## 9. Emergency procedures

### 9.1 Kill switch
```bash
touch KILL                  # repo root; under compose: docker compose exec agent touch KILL
```
Or Telegram `/kill` (when configured). At the next fast tick (≤ 2 s) the engine persists
`killed=true` (committed at once), writes a `kill` event, cancels its entry/rebalance orders and
flattens every non-TMN balance to TMN (phase 1 rules). The process keeps running: heartbeats continue
and only exit/stop/flatten SELLs pass the RiskManager. A kill also fires automatically on a drawdown
≥ 5 % from the peak or on ≥ 3 fast-loop crashes within 600 s (`exception storm`). A kill survives
restarts.

### 9.2 Reset after a kill (manual only)
```bash
rm KILL                                                     # first; reset-kill refuses while it exists
.venv/bin/python -m wta reset-kill --mode live --log-dir logs --reason "why it is safe to resume"
.venv/bin/python -m wta reset-kill --mode shadow --log-dir logs/shadow --reason "..."      # paper
```
Without `--mode`/`--feed` it resets the account that is killed (it refuses to guess between two).
It writes a `reset_kill` event into `<log dir>/events.jsonl` (default `logs`: pass the engine's own
log dir) and a kv record into that account's state DB. A running engine applies it on its next tick
(`reset_kill_applied`), otherwise the next start does. The reset moves the peak to the current
equity (the old drawdown must not kill again at once) and cancels the kill's flatten orders unless a
deadline flatten is running.

### 9.3 Flatten now (outside the deadline schedule)
The account lock allows one trading process per account: **stop the agent first** (exit 2 otherwise).
```bash
make flatten-live            # REAL account: WTA_LIVE=1 + --i-understand-live, API key, clean tree
.venv/bin/python scripts/flatten_now.py --log-dir logs/shadow          # paper account "shadow"
.venv/bin/python scripts/flatten_now.py --feed synthetic --log-dir logs/shadow_synth \
    --paper-balances '{"TMN": 1000000, "USDT": 5}'                       # synthetic account, offline
```
`flatten_now.py` runs the engine's own flatten (FlattenManager → RiskManager → OrderManager, decision
records like any order): phase 1 for `--phase1-seconds` (60), then phase 2, until only TMN and dust
remain (exit 0) or `--timeout` (600 s, exit 1, leftovers listed). It prints balances and equity
before and after, does not need `CHALLENGE_END_UTC`, and sends Telegram alerts but never polls the
commands. Its `--log-dir` defaults to `logs` (the live log dir): `make flatten` (paper) writes its
records there too, so prefer the explicit paper command above.

### 9.4 API outage
* The REST client retries 429/5xx/timeouts with 1, 2, 4, 8 s backoff (max 60 s); the poller keeps
  polling with its own capped backoff.
* No USDTTMN book for `engine.stale_data_s` (30 s), ≥ 5 consecutive API errors, or TMN/USDT books
  disagreeing by ≥ 5 % → **anomaly pause**: no new entries; working entry orders are cancelled.
  Resume is automatic after the condition is clear for 60 s.
* `STALE_DATA` also blocks exits on stale books. Server-side STOP_LIMIT orders stay on the exchange
  and still protect positions while we are blind.
* After the error burst ends, an extra reconcile runs (missed fills, foreign orders, balance drift).
* An order whose send failed ambiguously is never sent again blindly: it is looked up by `client_id`.

### 9.5 LLM outage
Nothing to do by hand. A failed cycle keeps the last plan until it is 20 minutes old
(`ai.plan_stale_s`), then the strategies run rules-only; heartbeat `llm_ok=false`, events
`ai_degraded`, `plan_stale` and later `ai_recovered`, a Telegram alert after 3 failures in a row. The
Gemini fallback key is quota-limited (HTTP 429), so do not count on it. To run without the LLM on
purpose: `--set ai.enabled=false` (changes `config_hash`).

### 9.6 Restarting a process
* **Agent**: stop with SIGTERM (never `kill -9` during trading). On start the engine restores peak
  equity, day-start equity, fees, order counters, realized PnL, kill/pause/flatten flags, positions,
  open orders, strategy and feature state and candles from its state DB, re-applies the reviewer's
  `latest.yaml`, waits up to 30 s for the books it needs, then reconciles with the exchange. Events:
  `start` (`reason: restart`) and `restart` with both summaries. Restart with the same `--overlay` /
  `--set` flags, or the `config_hash` changes.
* **Recorder**: safe at any time. Candles are upserted, so overlaps are harmless; a gap in `books`
  stays a gap (the survey and the book backtests report only the hours really covered). The agent
  keeps trading on its own feed meanwhile.

---

## 10. API connectivity seen on 2026-10-22, and what the recorder does

`api.wallex.ir` is reachable from the development machine only intermittently (connect/TLS timeouts,
likely IP filtering; `docs/REPORTS/api_probe.md` at 10:43Z: `ConnectTimeout`). From the recorder's
health lines and the market DB:

| Time (UTC) | What happened |
|---|---|
| 2026-10-22 10:42 | recorder started (11 symbols); REST, WebSocket and candles all failing |
| 14:20:05–14:40:15 | the API answered: **6,103 REST book snapshots** (~20 min), 300 trades, candles backfilled (1-min bars 12:25–14:39Z, 2.2 h) |
| 14:41 onwards | unreachable again |
| 18:29 | recorder restarted with the 21-symbol watchlist |
| 20:16:12 | a 1-second burst: **28 snapshots** |
| 20:56 → now | recorder running (pid in `run/recorder.pid`), no rows; at 2026-10-23 07:09Z it had counted 362 REST, 108 candle and 556 WebSocket errors since that start |

The WebSocket never connected and the MCP endpoint never answered. The market DB holds 6,131 book
snapshots and 1,476 candles in total; only 0.33 h of books align across the books S1 needs
(`docs/REPORTS/survey_20261022.md`).

What the recorder does during such an outage (`data/collector.py`):

* every task (REST poller, candle poller, WebSocket, store flush, health) is supervised: a crash is
  logged and the task restarted after 1 s doubling to 60 s;
* the REST poller retries every 2 s, backing off to 30 s after consecutive failures; the candle
  poller backs off to 300 s; the WebSocket reconnects with backoff up to 60 s (a close code ≥ 3500
  stops it until the next restart);
* nothing is invented: a gap stays a gap, identical consecutive snapshots are deduplicated, candles
  are upserted (a later backfill fills candle gaps, never book gaps);
* one health line per minute says how many rows were written and which symbols are missing, so the
  outage is visible in the log;
* when the API comes back, rows start flowing without a restart (as at 14:20Z and 20:16Z).

What it means for operations: check reachability several times before going live
(`curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://api.wallex.ir/v1/markets`), and run the
trading machine on a network where the API answers consistently. A live engine on a flaky network
spends its time in anomaly pauses (no new entries) and keeps exits and server stops working.

---

## 11. Evidence commands (backtest, report, replay)

```bash
# backtests (SYNTHETIC unless the market DB holds recorded data; every report says which)
.venv/bin/python -m wta backtest --driver books --from 2026-10-22T14:20Z --to 2026-10-22T20:17Z \
    --strategies S1,S2 --ai off --out /tmp/bt_books_recorded_ai_off      # all recorded real books (0.32 h)
.venv/bin/python -m wta backtest --driver books --synthetic-db data/synthetic_books.sqlite \
    --synthetic-hours 6 --strategies S1,S2 --ai on --out /tmp/bt_synth_ai_on
# a run's report (equity from the state DB its start event names)
.venv/bin/python -m wta report --source logs/shadow_synth_4h --out /tmp/shadow_report
# replay the 4-h shadow run: 40/40 decisions, 0 network (about 2 minutes)
.venv/bin/python scripts/replay_check.py --log-dir logs/shadow_synth_4h --reports-dir ''
make replay-deliverable                     # the same from the committed bundle
# plans only, from the cache
.venv/bin/python -m wta.ai.replay --log-dir logs/shadow_synth_4h
```

`--ai on` uses the configured LLM (real calls, cached under `<out>/llm_cache`, at most
`--ai-max-calls` cycles); a rerun is served from the cache. `make replay` and `wta replay` write
`docs/REPORTS/replay_<date>.md` unless `--reports-dir ''` is given. Replay exit codes: 0 every
decision reproduced, 1 a mismatch or a partial recording, 2 nothing to replay.

The 4-h shadow run (`logs/shadow_synth_4h`, state DB `data/state_shadow4h_shadow_synthetic.sqlite`)
was a `wta run --mode shadow --feed synthetic` with
`--set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'`, AI on (OpenRouter), at commit
`7e8f2f5`. `wta config hash --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'` prints
its `config_hash` `481cc6dc3ce2` (with the current `.env`). `logs/` and `data/` are not in git.

---

## 12. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `wta markets` / `check_account.py` fail with `ConnectTimeout`; heartbeat `rest_ok=false`, `reasons: rest: no fresh data` | `api.wallex.ir` unreachable (§10) | test with `curl`; use another network; a shadow run on the real feed is useless until it answers, `--feed synthetic` only exercises the engine |
| `wta run` starts slowly, then `markets_source: snapshot` | `/v1/markets` failed after retries; markets from `configs/markets_snapshot.yaml` | fine for shadow/synthetic; live refuses to start (by design) |
| heartbeat `stale books (> 30s): …` | REST poll failing or a symbol missing from `/v2/depth/all` | check `extra.rest.last_error`; the poller fills missing symbols per symbol |
| `ws: stopped by the server (close code ≥ 3500, not retried)` | Centrifugo "do not reconnect" | REST polling (2 s) carries the data; restart the agent to try the WebSocket again |
| `paused: anomaly: no USDTTMN book` | dollar book stale > 30 s | §9.4; resumes by itself |
| `paused: daily_loss …` | daily PnL ≤ −2.5 % | resumes at the next UTC day |
| `paused: telegram: …` | an operator `/pause` | `/resume` from the same chat |
| `killed: drawdown_kill …` / `exception storm …` / `telegram /kill …` | §9.1 | read `events.jsonl` and the decision log before §9.2 |
| `llm: N failed analyst cycle(s) in a row (…)`, `plan stale` | provider unreachable, bad key, or invalid JSON twice | §9.5; `last_error` in `extra.ai` names it; the key never appears in logs |
| `AI: offline rules (no LLM key)` in status/TUI | no `OPENROUTER_API_KEY` / `GEMINI_API_KEY` in the environment | expected without keys; those plans are not LLM plans |
| `error: another engine or flatten_now (pid N) is running on data/state_<account>.sqlite` (exit 2) | two trading processes on one account (also `wta plan` next to a shadow engine) | stop one; parallel runs need different `data.state_sqlite_path` |
| `refusing to start live mode: …` (exit 2) | a live guard (`RISK.md` §14) | the message names the failed guard |
| Telegram commands never answer; heartbeat says the poller stopped | another engine polls the same bot (HTTP 409) | only one engine keeps `alerts.telegram_commands_enabled` true |
| `wta alert` prints `telegram: not configured …` | token, chat id or `alerts.telegram_enabled` missing | fill `TELEGRAM_CHAT_ID`; add `--set alerts.telegram_enabled=true` |
| `run_forever: … restarted N times in 1 h - giving up` in `logs/run_forever.log` | crash loop | read the `*.out` file for the traceback; fix, then start again |
| replay fails on the config hash (`DIFFERS from recorded`) | the run used `--set`/`--overlay` values the replay cannot recover | pass the run's `--set` / `--overlay` to `scripts/replay_check.py` |
| replay exit 1 `PARTIAL` | the run is still being written or crashed | stop it, or `--allow-partial` to replay the committed prefix |
| disk filling | recorder books and the state DB's `engine_books` / `engine_candles` | `wta db vacuum --path …` (stop writers first); move old dated logs to `deliverables/` |
| "SYNTHETIC" banner in TUI/status/reports | a `--feed synthetic` run or a synthetic market DB | expected; never quote those numbers as performance |

---

## 13. Data, logs and state layout

```
data/                                  (gitignored)
  wta.sqlite                           market DB (data.sqlite_path): recorder books, trades, candles, events
  state_live.sqlite                    live account state DB (data.state_sqlite_path = data/state_{mode}.sqlite)
  state_shadow.sqlite                  paper account on the real feed
  state_shadow_synthetic.sqlite        paper account on the synthetic feed
  state_shadow4h_shadow_synthetic.sqlite   the 4-h shadow run (custom state path)
  <state db>.engine.lock               one trading process per account (pid inside)
  backtest_state/                      state DBs of make backtest*
logs/                                  (gitignored)
  recorder-YYYYMMDD.jsonl, recorder.out, run_forever.log, *.out
  decisions|orders|events|health-YYYYMMDD.jsonl   live engine (wta run --mode live writes to logs/)
  <family>.jsonl                       symlink to the current UTC day's file
  journal.md                           Post-trade Reviewer journal of that log dir
  llm_cache/<sha256>.json, llm_cache/mcp_tape/    LLM requests/answers and tool tapes of that log dir
  shadow/                              paper engine: the same files
  shadow_synth_4h/                     the 4-h shadow run (synthetic feed, real LLM)
configs/runtime/<account>/             (gitignored) reviewer overlays config-<ts>.yaml, latest.yaml
run/*.pid                              (gitignored) bare-metal pids
KILL                                   (gitignored) kill-switch file
deliverables/                          (committed) REPORT.md, TAGS.md
  shadow_synth_4h/                     the 4-h run's decisions, orders, events, hourly heartbeats, journal, llm_cache
  replay/shadow_synth_4h/              its replay bundle: logs.tar.xz, state.sqlite.xz, SHA256SUMS (make replay-deliverable)
docs/REPORTS/                          (committed) survey, backtests, ablation, shadow, replay reports
```

JSONL files are append-only, one canonical JSON object per line (sorted keys, Decimals as strings),
rotated per UTC day, with every configured secret replaced by `***` before the write.

SQLite tables (WAL mode; readers such as the TUI open them read-only):

| Table | Written by | Content |
|---|---|---|
| `books`, `trades` | recorder (market DB) | order books (≤ 10 levels, identical snapshots deduplicated), public trades |
| `candles` | recorder `CandlePoller`, `wta backfill`; engine (state DB) | 1-minute OHLCV, upsert by `(symbol, resolution, ts)` |
| `ticks`, `engine_books`, `engine_candles` | engine | replay inputs: one row per tick (time, each book's `ts`, `candle_seq`), every book a tick used, every candle arrival in order |
| `orders`, `fills`, `positions` | engine | order state, fills, positions (written through) |
| `balances`, `equity` | engine | balance snapshots on change; equity row every 10 s |
| `plans` | engine | every MarketPlan with its `prompt_hash` |
| `kv` | engine, `reset-kill` | `portfolio_state`, `engine_flags`, `order_manager_state`, `paper_state` (shadow), `strategy_state`, `feature_state`, `kill_reset` |
| `events` | recorder, engine, `reset-kill` | mirror of `events.jsonl` plus `recorder_start/stop` |

One fast tick is one SQLite commit: orders, fills, positions and kv state move together, so a crash
restores the state as of the end of a tick.
