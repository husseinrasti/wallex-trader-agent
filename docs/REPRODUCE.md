# Reproducing the results

**Summary**
1. Everything below runs from a clean clone with Python ≥ 3.11 or Docker. The offline checks need no API key and no access to Wallex.
2. The key check is `make replay-deliverable`. It unpacks the committed bundle of the 4-hour shadow run (synthetic feed, real LLM) and re-runs it with all network access refused. Result: 40/40 decisions reproduced, 0 socket attempts, 0 LLM provider calls.
3. Backtests and shadow runs work offline on SYNTHETIC data, labelled as such. The recorded real data ships in git (`deliverables/market/`, `make market-db`), so the real-data reports rerun offline too, and `--ai cache` reruns the real-LLM backtests from their committed caches without a key.
4. Every decision line carries `git_commit` and `config_hash`. §6 shows how to check them against the git tags and `python -m wta config hash`.
5. Real Wallex data is about 20 minutes of order books and 2.2 h of candles, too little to judge the strategies. No live run exists yet, so there is no live log to replay.

Every command in this file was run on 2026-10-23 on macOS arm64 and in the Docker image. Most ran on
fresh clones of tag `v0.5-backtest`; the Phase-6 commands (`make replay-deliverable`, `--bundle`,
`make backtest-cache`, `make market-db` and §4.2) ran on tag `v0.6-ops`, which also regenerated every
report in `docs/REPORTS` on a clean tree. The times are wall-clock times on that machine. Run commands from
the repo root. `python` means the venv's interpreter: run `source .venv/bin/activate` first, or write
`.venv/bin/python`. `make` targets use `.venv` on their own.

## 0. What you can check

| Claim | Command | Needs | Time | Expected |
|---|---|---|---|---|
| Code and tests are sound | `make test` | install only | ~2 min | `All checks passed!` (ruff), then pytest dots and `1953 passed, 1 skipped`, exit 0 |
| The 4-h shadow run is reproducible | `make replay-deliverable` | nothing | ~2–3 min | `result:   40/40 decisions reproduced -> PASS`, exit 0 (§3.1) |
| Backtest path, offline | `make backtest-synth` | nothing | ~30 s | `return -0.236%  max drawdown +0.770%  trades 2  orders 19  vetoes 18  fees 16,063 TMN` (§4.1) |
| The AI ablation's real-LLM run reproduces without a key | `make backtest-cache` | nothing | ~3 min | `ai=cache`, `return +0.121%`, `model calls 132 (132 cached)`, exit 0; a cache miss would be exit 1 (§4.1) |
| Engine, risk, AI plumbing, offline | `make shadow-synth` | nothing | until Ctrl-C | heartbeats with `fresh_books=21/21`, an S2 order, `logs check` OK (§5.1) |
| Your own run replays | `make replay` | a run under `logs/` | seconds to minutes | `N/N decisions reproduced -> PASS` (§3.2) |
| Logs belong to a tagged commit and config | §6 commands | git history | instant | the commit is in a tag; `config hash` prints the logged hash |
| The real-data reports reproduce | `make market-db`, then the §4.2 commands | nothing | seconds | the shipped numbers: survey "insufficient data"; recorded books −0.055 %, 3 orders, AI off = AI on (§4.2) |
| Strategies on enough real data | `make backtest`, `make backtest-books`, `make survey` | more recorded Wallex data | — | not possible yet: 20 minutes of books are too short to judge the strategies (§2, §8) |

## 1. Environment

* **Python ≥ 3.11.** Tested with 3.11 (the Docker image `python:3.11-slim` and CI), 3.12.14 (the
  development venv) and 3.14.7 (`make install` with the system `python3` on a fresh clone).
* **venv:** `make install` runs `python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"`.
  It needs PyPI access and takes about 30–60 s. Choose the interpreter with `make install PYTHON=python3.12`.
* **uv (alternative):** either of these works.

  ```bash
  make install PYTHON="$(uv python find 3.12)"
  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
  ```
* **Docker:** `cp .env.example .env && docker compose build` builds `wallex-trader-agent:latest`
  (Python 3.11, non-root user `wta`, `tini`, about 100 s). `.dockerignore` keeps `.env` (and any
  `.env.*` copy except `.env.example`), `.venv`, `data/`, `logs/`, `run/` and `video/` out of the image. The image holds the code, `configs/`,
  `docs/`, `deliverables/` and `.git` (for the provenance fields). Compose passes `.env` in at run time
  (`env_file`); without the file every compose command fails with `env file .../.env not found`. Run
  one-off commands with `--no-deps`, or compose also starts the recorder, which `agent` depends on:

  ```bash
  docker compose run --rm --no-deps agent python -m pytest             # ~100 s, exit 0
  docker compose run --rm --no-deps agent python -m wta config hash
  ```
  Only `./data`, `./logs` and `./configs/runtime` are mounted, so give `--out logs/...` to anything
  whose output you want to keep. The synthetic book backtest of §4.1 printed the same numbers in the
  3.11 container as on the 3.12 host.
* **`.env`:** `cp .env.example .env`. Every key is optional for this file.

  | Variable | Used for | Without it |
  |---|---|---|
  | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL_FAST`, `OPENROUTER_MODEL_STRONG` | the Regime Analyst and Reviewer (real LLM) | the offline rules provider `offline-rules-v1`, labelled "NOT an LLM" everywhere |
  | `GEMINI_API_KEY`, `GEMINI_MODEL` | LLM fallback | no fallback (the developer's key is quota-limited, HTTP 429) |
  | `WALLEX_API_KEY`, `WALLEX_SUB_ACCOUNT_CLIENT_ID` | account endpoints and live orders; MCP tools for the analyst | public market data still works; the analyst uses local tools only |
  | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts | no alerts |
  | `CHALLENGE_START_UTC`, `CHALLENGE_END_UTC` | deadline flatten; live refuses to start without the end | no deadline (shadow and backtest ignore it) |

  `CHALLENGE_START_UTC`, `CHALLENGE_END_UTC`, `OPENROUTER_MODEL_FAST`, `OPENROUTER_MODEL_STRONG` and
  `GEMINI_MODEL` enter the config, so they change `config_hash` (§6). API keys are settings, not
  config, and are never logged. `python -m wta config show` prints them masked.
* **Network:** apart from `pip install`, only these touch the internet: `make shadow`, `make record`,
  `python -m wta backfill|markets|feed|account|plan`, `scripts/check_account.py`, and the LLM when a
  key is set. Backtests never call Wallex. A replay refuses every connection.

## 2. Data: what is needed and what is shipped

| Data | Where | In git | Notes |
|---|---|---|---|
| Real Wallex market data | `deliverables/market/wta.sqlite.xz` (257 KB, `SHA256SUMS`); `make market-db` writes `data/wta.sqlite` (3.4 MB) | yes (the archive; `/data/` itself is ignored) | 6,131 REST order-book snapshots in 21 markets: one window 2026-10-22 14:20:05–14:40:15Z (6,103 snapshots) and a 1-second burst of 28 at 20:16:12Z. Plus 2.2 h of 1-minute candles (12:25–14:39Z) and 300 trades. Account and engine tables are empty. No 30-day backfill. `api.wallex.ir` answers the development machine only intermittently, and the recorder has stored nothing since |
| The 4-h shadow run (synthetic feed, real LLM `openai/gpt-5.4-mini`) | `deliverables/replay/shadow_synth_4h/` (bundle) and `deliverables/shadow_synth_4h/` (readable copy) | yes | from `logs/shadow_synth_4h` + `data/state_shadow4h_shadow_synthetic.sqlite` (108 MB, compressed in the bundle) |
| Reports, with each run's decision logs and, for `ai=on` runs, the LLM cache | `docs/REPORTS/*/` | yes | index in `docs/REPORTS/README.md`; the final report is `deliverables/REPORT.md` |
| Offline market rules and fees | `configs/markets_snapshot.yaml`, `configs/fees_snapshot.yaml` | yes | used when `/v1/markets` or `/v1/account/fee` cannot be reached |
| Synthetic market DBs | made on demand by `--synthetic-db PATH` | no | deterministic (seed 42). Every book is tagged `source="synthetic"`, and the generator never overwrites a database without its synthetic marker |

A fresh clone holds the recorded real market data as a compressed archive. `make market-db` checks
its `SHA256SUMS` and unpacks it to `data/wta.sqlite` (it refuses to overwrite an existing file); after
that the real-data reports in `docs/REPORTS/` (survey, `bt_*_recorded_*`) rerun offline (§4.2). Every
longer run is SYNTHETIC: 20 minutes of real books are all there is (§8 records more).

## 3. Replay: the key reproducibility check

### 3.1 `make replay-deliverable` (the shipped 4-h run)

```bash
make replay-deliverable        # = python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h
```

The script checks `SHA256SUMS` against `logs.tar.xz` and `state.sqlite.xz` and unpacks both into a
temporary directory. It then rebuilds the engine from the recorded config (the hash is checked) and
drives it tick by tick over the recorded books, candles, exchange answers and cached LLM answers. At
the end it compares every decision and deletes the temporary directory. It writes no report and
changes no file in the repo. Expected output (2 min 17 s on the development machine):

```
bundle:   deliverables/replay/shadow_synth_4h: SHA256SUMS verified (logs.tar.xz, state.sqlite.xz); unpacked into a temporary directory
replay: shadow_synth_4h (unpacked, from bundle deliverables/replay/shadow_synth_4h)
recorded: mode shadow, feed synthetic — SYNTHETIC, not evidence of performance; segment 1 of 1
window:   2026-10-22T22:53:39.782Z -> 2026-10-23T02:58:38.810Z (4.08 h, 7292 ticks); plans 38/38 re-derived identically; config 481cc6dc3ce2 (= recorded)
network:  0 socket attempts, 0 LLM provider calls; LLM cache 81 hits / 0 misses
exchange: recorded 160, repeat 22; divergences 0
strict:   40/40 decisions identical (every field, time stamps included)
end:      16/16 final-heartbeat values equal (equity, fees, plan freshness, S1 tick counters; informational)
result:   40/40 decisions reproduced -> PASS
report:   not written (--bundle without --reports-dir)  (replay output removed with the temporary directory; --out DIR keeps it)
```

How to read it:

* **40 decisions** = 38 analyst plans + 2 S2 orders. The 2 outcome lines are joined to their orders.
* **The verdict needs all of these:** the PRD-05 fields equal; the strict pass (every field,
  including time stamps, fees, fills and features); every plan re-derived identically from the LLM
  cache; zero exchange divergences; a complete recording; and zero network.
* **Zero network is enforced, not assumed.** Every `AF_INET`/`AF_INET6` connect and every
  `getaddrinfo` is refused and counted, and the LLM providers are replaced by sentinels that count and
  refuse calls. Both counters must be 0.
* **`end:` is informational.** It compares the final heartbeat: equity 5,002,180.5056 TMN, fees
  6,239.79 TMN, and S1's per-coin tick counters, which change on every tick.
* **The data is SYNTHETIC.** The run proves that the code is deterministic and replayable. It proves
  nothing about returns. The full proof is in [`REPORTS/replay_20261023.md`](REPORTS/replay_20261023.md).

Exit codes: `0` PASS. `1` any mismatch, a missing or extra decision, a plan re-derived differently,
an exchange divergence, an LLM cache miss, a partial recording, or a network call. `2` nothing to
replay or bad input, for example a bundle that fails its `SHA256SUMS` or an output directory that is
not empty. Add `--out DIR` (a new, empty directory) to keep the replayed logs, state DB and
`replay_report.md`, and `--json` to print the full result.

In Docker, with Python 3.11, the output is the same (2 min 39 s):

```bash
docker compose run --rm --no-deps agent python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h
```

### 3.2 `make replay` (a run of your own)

```bash
make replay                                              # newest live run under logs/, else the newest shadow run; report in logs/
python scripts/replay_check.py --log-dir logs/shadow_synth --reports-dir '' --no-index   # a given run, no report at all
python scripts/replay_check.py --log-dir logs/shadow_synth --out /tmp/replay1 --json      # keep the output, print JSON
make replay-report                                       # like make replay, but writes docs/REPORTS/replay_<day>.md + the index
```

After `make shadow-synth` (§5.1), `make replay` printed on a fresh clone:

```
replay: logs/shadow_synth (logs/decisions.jsonl does not exist (no live run recorded there); no live run; newest shadow run under logs)
recorded: mode shadow, feed synthetic — SYNTHETIC, not evidence of performance; segment 1 of 2
window:   2026-10-23T07:08:43.432Z -> 2026-10-23T07:09:21.506Z (0.01 h, 20 ticks); plans 1/1 re-derived identically; config 73958c6765c6 (= recorded)
network:  0 socket attempts, 0 LLM provider calls; LLM cache 2 hits / 0 misses
...
result:   2/2 decisions reproduced -> PASS
report:   logs/replay_20261023.md  (replay output: /var/folders/.../wta-replay-...)
```

* **It never writes into the repo.** `make replay` writes its report to `logs/replay_<YYYYMMDD>.md`
  (named after the UTC day of the last replayed tick; `logs/` is gitignored) and leaves the reports
  index alone, so the committed proof `docs/REPORTS/replay_20261023.md` stays as it is. Only
  `make replay-report` (or `scripts/replay_check.py` without `--reports-dir`) writes
  `docs/REPORTS/replay_<YYYYMMDD>.md` and regenerates `docs/REPORTS/README.md`; restore them with
  `git checkout -- docs/REPORTS`.
* **One segment at a time.** Each process start in a log directory is a segment. By default the
  latest fresh start is replayed; choose another with `--segment N`.
* **A run still being written** is a failure unless you pass `--allow-partial`.
* **A fresh clone has no runs**, so `make replay` exits 2 with `nothing to replay`. Use
  `make replay-deliverable`.

## 4. Backtests

Every backtest writes into its `--out` folder: `report.md`, `summary.json`, `equity.csv`, `trades.csv`,
`equity.png`, `drawdown.png`, `regimes.png`, `vetoes.png`, and the engine's own
`decisions/orders/events/health` JSONL, in the same schema as live. The state DB goes to `--state-db`.
A run on generated data carries the SYNTHETIC banner in its report and console output.

### 4.1 Offline (SYNTHETIC data)

```bash
make backtest-synth
```
```
synthetic market DB data/synthetic_books.sqlite (generated): 2026-10-01T00:00:00.000Z -> 2026-10-01T06:00:00.000Z, 75607 books, 2520 candles (SYNTHETIC — not evidence of performance)
SYNTHETIC — not evidence of performance (generated market data)
backtest books S1,S2 ai=off (disabled): 2026-10-01T00:00:00.000Z -> 2026-10-01T06:00:00.000Z, data 6.0006 h, 10801 ticks
  return -0.236%  max drawdown +0.770%  trades 2  orders 19  vetoes 18  fees 16,063 TMN
  report: logs/synthetic_bt_books/report.md
```

This is a 6-h order-book run of S1+S2 with the AI off, and it takes about 30 s. The variants below
were run the same way, and the times are wall-clock:

```bash
# the same 6 h with AI plans: offline = the rules provider (NOT an LLM);
# on = the LLM in .env (OPENROUTER_API_KEY + OPENROUTER_MODEL_FAST); with no key, --ai on falls back
# to the offline rules and the report says so. To rerun a shipped real-LLM run without a key, use
# --ai cache (below)
python -m wta backtest --driver books --synthetic-db data/synthetic_books.sqlite --synthetic-hours 6 \
  --ai offline --strategies S1,S2 --out /tmp/bt_synth_offline --state-db /tmp/bt_synth_offline/state.sqlite
#   ~26 s: the same numbers as ai=off, plus "analyst cycles 73 (cap 150), plans 73, model calls 146 (0 cached)"

# 1-minute candles, S2+S3, 72 h (the candle default), ~2 min:
#   return -0.798%  max drawdown +1.730%  trades 0  orders 8  vetoes 0  fees 17,334 TMN
python -m wta backtest --driver candles --synthetic-db /tmp/synth_candles_72h.sqlite --strategies S2,S3 \
  --out /tmp/bt_candles_synth72h --state-db /tmp/bt_candles_synth72h/state.sqlite

# the shipped 30-day SYNTHETIC candle report (docs/REPORTS/bt_candles_30d), ~17 min with the DB generation;
# prints the shipped numbers: return -4.459%  max drawdown +5.200%  trades 3  orders 68  vetoes 79  fees 104,683 TMN
python -m wta backtest --driver candles --synthetic-db /tmp/synth_candles_30d.sqlite --synthetic-hours 720 \
  --ai off --strategies S2,S3 --set strategies.s3_momentum.allow_rules_only=true \
  --out /tmp/bt_candles_30d --state-db /tmp/bt_candles_30d/state.sqlite
```

**The real-LLM runs, again, without a key: `--ai cache`.** The only real-LLM evidence of AI value is the
6-h ablation pair (`bt_books_synth24h_6h_ai_{off,on}`, `ablation_20261023.md` §2). Its LLM cache is
committed (`docs/REPORTS/bt_books_synth24h_6h_ai_on/llm_cache/`, 132 answers). `--ai cache` reruns it
from that cache alone: the analyst's routes are read from the cache entries, the providers are
stand-ins that refuse every call, the cache is strict (a miss stops the run with exit 1, it never
calls a provider) and read-only (nothing is written into it). No key and no model name are needed.

```bash
make backtest-cache     # = the command below; ~1 min to build the SYNTHETIC 24-h DB, then ~30 s
python -m wta backtest --driver books --synthetic-db data/synth_books_24h.sqlite --synthetic-hours 24 \
  --from 2026-10-01T12:00Z --to 2026-10-01T18:00Z --ai cache --strategies S1,S2 \
  --set ai.cache_dir=docs/REPORTS/bt_books_synth24h_6h_ai_on/llm_cache \
  --out logs/bt_cache_synth24h_6h --state-db data/backtest_state/bt_cache_synth24h_6h.sqlite
```
```
SYNTHETIC — not evidence of performance (generated market data)
backtest books S1,S2 ai=cache (llm): 2026-10-01T12:00:00.000Z -> 2026-10-01T18:00:00.000Z, data 6.0006 h, 10801 ticks
  return +0.121%  max drawdown +0.380%  trades 0  orders 6  vetoes 0  fees 6,234 TMN
  analyst cycles 73 (cap 150), plans 73, model calls 132 (132 cached)
```

Those are the shipped `ai=on` numbers. The `ai=off` side is the same command with `--ai off`
(`return +0.031%`, 12 orders), so the +0.09 pp of the ablation is checkable on a fresh clone. The run's
report says `AI | cache (recorded LLM answers, no provider call)`, and its `summary.json` has
`provider_calls: 0` and `ai_cache_routes: ["openrouter/openai/gpt-5.4-mini"]`. The recorded-books pair
works the same way after `make market-db` (§4.2). A different window, strategy set, config or code
asks questions the cache never saw: that is `error: --ai cache: LLM cache miss ...` and exit 1.

Without `--out`, a backtest writes to `docs/REPORTS/bt_<driver>_<from>_<to>`, so pass `--out` to keep
the committed reports as they are. Every command behind the shipped reports, ablation and walk-forward
(`--param-grid`) included, is listed in
[`REPORTS/phase5_acceptance.md`](REPORTS/phase5_acceptance.md) §2.

### 4.2 On real data (the shipped market DB)

The recorded real data is in git as `deliverables/market/wta.sqlite.xz`. Unpack it first (it never
overwrites an existing `data/wta.sqlite`):

```bash
make market-db         # = python scripts/market_db.py
```
```
deliverables/market/wta.sqlite.xz: SHA256SUMS verified; wrote data/wta.sqlite (REAL recorded Wallex market data)
  books      6,131 rows  2026-10-22T14:20:05Z -> 2026-10-22T20:16:13Z
  trades       300 rows  2026-10-22T14:19:36Z -> 2026-10-22T14:40:24Z
  candles    1,476 rows  2026-10-22T12:25:00Z -> 2026-10-22T14:39:00Z
  events         5 rows  2026-10-22T10:42:48Z -> 2026-10-22T20:56:03Z
```

Then every real-data report reruns offline, in seconds. These are the commands behind the shipped
reports, with `--out` outside the repo (the expected lines are the shipped numbers):

```bash
# the survey (docs/REPORTS/survey_20261022.md): "insufficient data", defaults kept
python scripts/survey.py --since 72h --png --json --out /tmp/survey
#   Recommendation (insufficient_data): margin_min_pct=0.0015 leg1_taker=true S1 lock-in viable: unknown

# all recorded books, S1+S2, AI off (bt_books_recorded_ai_off) and AI on from its committed cache (bt_books_recorded_ai_on)
python -m wta backtest --driver books --from 2026-10-22T14:20Z --to 2026-10-22T20:17Z --ai off --strategies S1,S2 \
  --out /tmp/bt_books_recorded_ai_off --state-db /tmp/bt_books_recorded_ai_off.sqlite
#   backtest books S1,S2 ai=off (disabled): 2026-10-22T14:20:00.000Z -> 2026-10-22T20:17:00.000Z, data 0.3239 h, 583 ticks
#     return -0.055%  max drawdown +0.063%  trades 0  orders 3  vetoes 0  fees 3,123 TMN
python -m wta backtest --driver books --from 2026-10-22T14:20Z --to 2026-10-22T20:17Z --ai cache --strategies S1,S2 \
  --set ai.cache_dir=docs/REPORTS/bt_books_recorded_ai_on/llm_cache \
  --out /tmp/bt_books_recorded_ai_on --state-db /tmp/bt_books_recorded_ai_on.sqlite
#     return -0.055%  max drawdown +0.063%  trades 0  orders 3  vetoes 0  fees 3,123 TMN
#     analyst cycles 6 (cap 150), plans 6, model calls 11 (11 cached)

# all recorded 1-minute candles, S2+S3 (S3 rules-only), AI off (bt_candles_recorded_ai_off)
python -m wta backtest --driver candles --from 2026-10-22T12:25Z --to 2026-10-22T14:40Z --ai off --strategies S2,S3 \
  --set strategies.s3_momentum.allow_rules_only=true \
  --out /tmp/bt_candles_recorded_ai_off --state-db /tmp/bt_candles_recorded_ai_off.sqlite
#     return +0.092%  max drawdown +0.091%  trades 0  orders 4  vetoes 0  fees 6,242 TMN
```

Each report states how many hours the data really covers (0.32 h of books, 2.25 h of candles), not
the requested window. The longer real-data targets need more data than was ever recorded:

```bash
make backtest          # 30 days of REAL 1-min candles, S2+S3 (allow_rules_only, like the shipped candle reports) -> docs/REPORTS/bt_candles_30d_real/
make backtest-books    # recorded order books, S1+S2 (BOOKS_FROM / BOOKS_TO; AI=off|on|offline) -> docs/REPORTS/bt_books/
```

Without `data/wta.sqlite` both stop at once with `error: market database data/wta.sqlite does not
exist` (exit 2). With the shipped DB, the default `make backtest` window (`FROM=2026-09-22`, `TO=2026-10-22`,
midnight) ends before the first recorded candle (12:25Z), so its report says INSUFFICIENT DATA; a
real 30-day run needs `python -m wta backfill --days 30 --resolution 1` and
`make record` first, and both need `api.wallex.ir` (§8).

## 5. Shadow runs

### 5.1 Offline: the synthetic feed

```bash
make shadow-synth      # Ctrl-C to stop
# or a fixed length:
python -m wta run --mode shadow --feed synthetic --log-dir logs/shadow_synth \
  --set data.sqlite_path=data/shadow_synth.sqlite --seconds 120
```

Expected without an LLM key:

```
WARNING wta.engine: engine: AI: offline rules (no LLM key): plans come from deterministic rules, NOT an LLM
INFO wta.engine: engine: shadow mode, feed synthetic, commit <sha12>, config <hash>, 21 symbols
INFO wta.engine: analyst: plan <hash> (offline-rules-v1, cached=False, tools get_features,get_pnl_summary via local, 0 ms): [offline rules provider — not an LLM] ...
INFO wta.engine: heartbeat: feed=synthetic rest_ok=False ws_ok=False fresh_books=21/21 equity=... killed=False paused=False ai=offline-rules llm_ok=None ...
INFO wta.engine: engine: stopped (signal SIGINT)
```

With an OpenRouter key the plan lines name the model, for example `openai/gpt-5.4-mini`. A second
start on the same state DB resumes it: the `start` event has reason `restart`, and peak equity, daily
PnL, fees and positions are kept. Inspect the run with:

```bash
python -m wta status --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite
python -m wta tui    --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite   # --once: one frame
python -m wta logs check --dir logs/shadow_synth                                                    # prints OK, exit 0
python -m wta report --source logs/shadow_synth --out /tmp/report_shadow_synth                       # report.md + PNGs
```

`status` and `tui` need the same `--set data.sqlite_path` as the run, so that they find its state DB
(here `data/shadow_synth.state_shadow_synthetic.sqlite`). Without it, `status` still shows the
heartbeat and decisions, and says `details need the engine state db`.

### 5.2 Real feed

```bash
make shadow            # = python -m wta run --mode shadow --log-dir logs/shadow; then make status / make tui
docker compose up -d recorder agent && docker compose logs -f agent      # the same, kept alive (restart: always)
scripts/run_forever.sh run --mode shadow --log-dir logs/shadow           # bare metal, 10 s back-off, max 20 restarts/h
```

These need `api.wallex.ir`. When it cannot be reached, the engine starts anyway. It logs
`markets fetch failed ... using configs/markets_snapshot.yaml` and WebSocket handshake timeouts, its
heartbeats show `rest_ok=False ws_ok=False fresh_books=0/21`, and it trades nothing, because stale
books pause entries. Start-up takes about a minute longer while the markets fetch retries. The
shipped shadow report (`docs/REPORTS/shadow_20261023_synth4h/`) is a synthetic-feed run. No
live-feed shadow of 4 h or more exists yet.

## 6. Verify git tags against `git_commit` and `config_hash`

Every decision line carries the full commit SHA of the process (`git_commit`) and
`config_hash = sha256(canonical JSON of the resolved config)[:12]`. The `start` event adds
`git_dirty`, `git_tag`, the overlays, the state DB and the AI routes. Live mode refuses a dirty tree.
Live runs are tagged `live-YYYYMMDD-HHMM` ([`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md) §5); none
exists yet. Checked on the shipped 4-h run:

```bash
RUN=deliverables/shadow_synth_4h          # or logs/<run> for a run of your own

# 1. distinct (git_commit, config_hash, mode) in the decision log
cat $RUN/decisions-*.jsonl | python -c 'import sys, json
rows = {(r["git_commit"], r["config_hash"], r["mode"]) for r in map(json.loads, sys.stdin) if r["kind"] != "outcome"}
print(*sorted(rows), sep="\n")'
#   ('7e8f2f5af1c55c3d744c6c8de22a454040b072f9', '481cc6dc3ce2', 'shadow')

# 2. the start event: clean tree? which tag? which models?
grep -h '"kind":"start"' $RUN/events-*.jsonl | python -c 'import sys, json
for line in sys.stdin:
    d = json.loads(line)["data"]
    print({k: d.get(k) for k in ("git_commit", "git_dirty", "git_tag", "config_hash", "mode", "feed", "overlays", "state_db", "reason")})
    print("ai routes:", d["ai"]["routes"])'
#   {'git_commit': '7e8f2f5a...', 'git_dirty': False, 'git_tag': None, 'config_hash': '481cc6dc3ce2', 'mode': 'shadow',
#    'feed': 'synthetic', 'overlays': [], 'state_db': 'data/state_shadow4h_shadow_synthetic.sqlite', 'reason': 'fresh'}
#   ai routes: {'analyst': 'openrouter/openai/gpt-5.4-mini', 'fallback': 'gemini/gemini-4-argon', 'reviewer': 'openrouter/openai/gpt-6-luna-pro'}

# 3. the commit is in the history and in a tag
git tag --contains 7e8f2f5              # v0.5-backtest   (a live run: git tag --points-at <sha> prints its live-* tag)
git describe --tags --contains 7e8f2f5  # v0.5-backtest~2
for t in $(git tag); do echo "$t $(git rev-parse "$t^{commit}")"; done

# 4. the config hash at that commit (same --set flags, same ${VAR} values as the run)
git checkout -q 7e8f2f5
OPENROUTER_MODEL_FAST=openai/gpt-5.4-mini OPENROUTER_MODEL_STRONG=openai/gpt-6-luna-pro GEMINI_MODEL=gemini-4-argon \
  python -m wta config hash --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'
#   481cc6dc3ce2
git checkout -q main                    # the same command on main prints 481cc6dc3ce2 too: no config change since
```

* **Recompute the hash with the run's inputs.** These are the run's `--set` and `--overlay` flags
  (`data.*` paths included) and the `${VAR}` fields: the model names, read here from the start
  event's `ai.routes`, and `CHALLENGE_*_UTC`. The environment wins over `.env`, and an empty value
  counts as `null`.
* **Where the `--set` came from.** The 4-h run was started with
  `--set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'`, so its state DB is
  `state_shadow4h_shadow_synthetic.sqlite`. The replay recovers this pattern from the `start` event
  by itself.
* **Reviewer edits.** An accepted Reviewer change writes a `config_change` event (`old_hash`,
  `new_hash`, the overlay file), and later decisions carry the new hash. That run made 0 changes.

More detail: [`DECISION_LOG_FORMAT.md`](DECISION_LOG_FORMAT.md) §8. Every tag with its commit, and the
commit behind each evidence run, is listed in [`deliverables/TAGS.md`](../deliverables/TAGS.md).

## 7. Reading `decisions.jsonl`

[`DECISION_LOG_FORMAT.md`](DECISION_LOG_FORMAT.md) describes every field, with annotated examples.
In short:

* **The files.** One line per decision goes in `decisions-YYYYMMDD.jsonl`, and `decisions.jsonl` is a
  link to the current UTC day's file.
* **`kind`.** It is `plan` (an analyst plan, `strategy: AI`, `symbol: PLAN`, the plan in
  `plan_snapshot` and its rationale in `notes`), `order`, `veto` or `outcome`. An `outcome` line is
  written later and points back by `decision_id`. Earlier lines are never rewritten.
* **An order or veto line holds:** `intent` (side, qty, price, purpose, `reason` in plain words),
  `risk` (`approved`, `adjusted_qty`, `veto_reason` and all `checks` with code, passed and detail),
  `order` (request and exchange answer), `features` (the market view at that tick), `plan_snapshot`
  (the plan in force), and `mode`, `git_commit`, `config_hash`.

A quick listing:

```bash
RUN=deliverables/shadow_synth_4h python - <<'EOF'
import glob, json, os
run = os.environ["RUN"]
for f in sorted(glob.glob(f"{run}/decisions-*.jsonl")):
    for r in map(json.loads, open(f)):
        if r["kind"] == "plan":
            p = r["plan_snapshot"]
            print(r["ts"], "plan   ", p["model"], "enabled", p["enabled_strategies"], "usdt", p["usdt_target_share"])
        elif r["kind"] == "outcome":
            o = r["outcome"]
            print(r["ts"], "outcome", r["decision_id"], o["status"], "filled", o["filled_qty"], "fee", o["fee_paid"])
        else:
            i, k = r["intent"], r["risk"]
            verdict = f"approved qty {k['adjusted_qty'] or i['qty']}" if k["approved"] else f"VETO {k['veto_reason']}"
            print(r["ts"], r["kind"].ljust(7), r["strategy"], r["symbol"], i["side"], verdict, "|", i["reason"][:70])
EOF
```
```
2026-10-22T22:53:39.782Z order   S2 USDTTMN BUY approved qty 4.68 | USDTTMN regime=range [rules] (ema20/ema60 n/a, 24h n/a, ATR15m n/a vs
2026-10-22T22:53:39.790Z plan    openai/gpt-5.4-mini enabled ['S2'] usdt 0.5
2026-10-22T22:54:21.931Z outcome d_1792709619783_S2_USDTTMN_1 FILLED filled 4.68 fee 0.011700
...
```

`python -m wta logs check --dir <logdir>` validates the schema and the links between all four log
families. `--secrets` also scans the logs, `deliverables/` and `docs/REPORTS` for secrets. In code,
`wta.logging_.decision_log.merge_decisions` joins the outcome lines to their decisions.

## 8. Re-running the survey on new data

The survey (`scripts/survey.py`, PRD-05 §2) counts S1 cross-book opportunities in the recorded books.
It then recommends `margin_min_pct` and `leg1_taker`, or says "insufficient data" when no coin has at
least 1 h of aligned TMN, USDT and USDTTMN books. The recommendation is computed, never written by an
LLM.

```bash
curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://api.wallex.ir/v1/markets   # 200 = reachable
make record                                    # books, trades, candles into data/wta.sqlite (Ctrl-C to stop)
#   or: python -m wta record --symbols-from configs/markets.yaml --seconds 7200
#   or: docker compose up -d recorder
python -m wta backfill --days 30 --resolution 1   # 30 days of 1-min candles for make backtest
make survey                                    # = python scripts/survey.py --since 24h -> docs/REPORTS/survey_<YYYYMMDD>.md
python scripts/survey.py --since 72h --png --json --out /tmp/survey    # longer window, chart + JSON, outside the repo
```

On `data/wta.sqlite` (the recorded data, shipped as `deliverables/market/`), the second command took
about 1.5 s and printed:

```
Market survey 20261022: window 2026-10-19T20:16:13.129Z -> 2026-10-22T20:16:13.129Z
6,131 snapshots in 21 markets; sources: rest: 6,131
  BTC: 0.33 h aligned (not in totals)
  ...
Recommendation (insufficient_data): margin_min_pct=0.0015 leg1_taker=true S1 lock-in viable: unknown
```

* **The file name** comes from the UTC day of the newest recorded book, and the window ends there.
  Choose it with `--until ISO|now`.
* **A real verdict** needs at least 1 h of aligned books per coin, and ideally 24 h or more.
* **A changed parameter** passes the walk-forward guard first (`wta backtest --param-grid ...`,
  70/30 split). Then rerun the commands of §4.2 on the new window, and the AI ablation with
  `--ai off` and `--ai on`.
* **Then rebuild the reports index and the final report:** `make final-report`
  (`scripts/assemble_report.py`) regenerates `docs/REPORTS/README.md`, `deliverables/REPORT.md` and
  its deliverables index from the reports. `phase5_acceptance.md` §7 explains the sections.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make install` fails building a wheel | the Python is too new or too old for a dependency | `make install PYTHON=python3.12`, or the uv lines in §1 |
| `python: command not found`, or the wrong `wta` | the venv is not active | `source .venv/bin/activate` or `.venv/bin/python -m wta ...` |
| `error: market database data/wta.sqlite does not exist` | a real-data backtest on a fresh clone | record or backfill first (§8), or use the synthetic variants (§4.1) |
| `markets fetch failed`, `ws: connect failed: TimeoutError`, `fresh_books=0/21` | `api.wallex.ir` cannot be reached (it answers the development machine only now and then) | check with `curl` (§8). Use `--feed synthetic` for offline runs |
| `make replay` exits 2, `nothing to replay` | no run under `logs/` | `make replay-deliverable`, or make a run first (§5.1) |
| `error: --ai cache: LLM cache miss ...` (exit 1) | the run asks something the cached run did not: another window, `--strategies`, `--set`, seed or code | use the shipped run's exact command (§4.1, §4.2); with a key, `--ai on` would call the model instead |
| `make market-db`: `data/wta.sqlite exists` (exit 2) | a market DB is already there (the recorder's, or an earlier unpack) | keep it, or move it away and unpack again |
| replay exits 2, output directory not empty | `--out` points at a used folder | give a new folder, or drop `--out` (a temporary one is used) |
| replay FAIL with an LLM cache miss, or a config hash not matched | the code or config differs from the recording, or a `--set` is missing | replay on the recorded commit's config; `--set`/`--overlay` as recorded; see the report's mismatch list |
| `git status` shows `docs/REPORTS/replay_*.md` or `README.md` modified | `make replay-report` (or `replay_check.py` without `--reports-dir`) writes its report there | `git checkout -- docs/REPORTS`; `make replay` writes to `logs/` instead |
| `config hash` differs from the logged one | different `--set` flags or `${VAR}` values | §6 step 4: the same flags and model names; check `python -m wta config show` |
| `status`: `details need the engine state db` | the run used another `data.sqlite_path` | pass the run's `--set data.sqlite_path=...` (§5.1) |
| `AI: offline rules (no LLM key)` | no `OPENROUTER_API_KEY` | expected without keys. Set the key and `OPENROUTER_MODEL_FAST` for the real LLM |
| `llm_ok=false`, `plan stale ... rules-only` | the LLM provider failed (the Gemini fallback key is quota-limited, HTTP 429) | by design the engine keeps the last plan for 20 min, then runs rules-only. No action needed for shadow or backtest |
| `env file .../.env not found` (compose) | no `.env` | `cp .env.example .env` |
| a recorder container keeps running after `docker compose run` | `agent` depends on `recorder` | add `--no-deps`. Stop it with `docker compose down` |
| a Docker output is gone after the run | it was written outside the mounted folders | write under `logs/` or `data/` |
| permission denied on `./data` or `./logs` (Docker on Linux) | the container runs as uid 1000 | make both folders writable for uid 1000, e.g. `sudo chown -R 1000:1000 data logs` |
| `live mode needs ...` / refuses to start | intended: live needs `WTA_LIVE=1`, `--i-understand-live`, a clean tree and `CHALLENGE_END_UTC` | only the operator starts live, via [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md) |
