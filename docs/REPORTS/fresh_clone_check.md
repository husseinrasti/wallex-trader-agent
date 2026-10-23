# Fresh-clone check of the README quick start, 2026-10-23

**Summary**
1. A fresh `git clone` of `main` (commit `391783c`, then `b8e0b44` after one fix) was set up and tested on macOS with Python 3.12 and in the Docker image (Python 3.11). Every documented command exists and gave the documented result.
2. `make test`: ruff clean, `1939 passed, 1 skipped`, exit 0, on the host and in the container. `make replay-deliverable`: `40/40 decisions reproduced -> PASS`, 0 socket attempts, 0 LLM provider calls, on the host and through `docker compose run`.
3. One defect was found and fixed (`b8e0b44`). `make test` printed no pytest summary line, because the Makefile passed `-q` on top of `addopts = "-q"`. The docs also still said 1,913 tests; the suite now has 1,939.
4. This check placed no real order. `make shadow` ran on paper and `api.wallex.ir` did not answer, so it showed `fresh_books=0/21` and traded nothing. All other runs used the offline SYNTHETIC feed, synthetic DBs or the committed bundle. No LLM key was set, so the AI layer was the offline rules provider, which is not an LLM.
5. Two observations were not fixed because they are outside this check's scope (§4). `make backtest-synth` leaves an untracked folder, so the tree is "dirty" afterwards. A shadow run stopped before its first tick logs `fresh_books=21/21`.

## 1. Setup

| | |
|---|---|
| Date | 2026-10-23, 07:54–08:19 UTC |
| Machine | macOS 27.0.1, arm64; GNU Make 3.81; git; uv 0.12.5 |
| Python (host) | 3.12.14 (the uv-managed CPython 3.12; `python3.12` is not on `PATH` here, so it was passed by full path as `PYTHON=`) |
| Docker | Docker 29.7.2, Compose 2.40.1; image `python:3.11-slim` → Python 3.11.17, user `wta` (uid 1000) |
| Source | `git clone /Users/hussein/Code/hackathon/wallex-trader-agent fresh`: a local clone of the committed `main`, identical to `origin/main` (`391783c` pushed before the clone, `b8e0b44` pushed after the fix). No `logs/`, `data/` or `.env` in the clone |
| `.env` | `cp .env.example .env`, as the README says: every key empty, so no LLM, Wallex or Telegram key |

Before the clone, the same checks ran on the working tree: `ruff check src tests scripts backtest` gave "All checks
passed!" and `pytest` gave `1939 passed, 1 skipped in 102.88s`. Then everything was committed (`391783c`, 252 files)
and pushed. The commit holds no `.env`, `data/`, `logs/`, `run/`, `.claude/` or `video/`, and its largest file is
`deliverables/replay/shadow_synth_4h/state.sqlite.xz` at 16.5 MB (limit 50 MB). Before committing, the 252 files
were scanned with `scan_for_secrets` against the 4 configured key values and the key patterns, with the `.xz`
files decompressed: 0 findings. After the commit, the history scans of
[`SUBMISSION_CHECKLIST.md`](../SUBMISSION_CHECKLIST.md) §2 gave: `.env` in no commit; `history 0  tree 0` for each
of the 4 values; `0` key-pattern matches; `wta logs check --secrets` printed `secrets scan: clean`. No key value was
printed at any step. GitHub Actions `ci` (ruff, pytest on Python 3.11, `docker build` and a `wta config hash` smoke
test in the image) passed on `391783c` (run 39920959759, 5 min).

## 2. Commands, exit codes and key output

Run in the clone in this order. Times are wall-clock. The two Ctrl-C runs got SIGINT sent to their whole process
group after N seconds, as a terminal does. `make` then exits by the signal, and the engine logs
`engine: stopped (signal SIGINT)`.

### 2.1 Install and tests (task a)

| # | Command | Exit | Time | Key output |
|---|---|---|---|---|
| 1 | `cp .env.example .env` | 0 | — | — |
| 2 | `make install PYTHON=<uv cpython 3.12>/bin/python3.12` | 0 | 23 s | `Successfully installed ... wta-0.1.0`; `.venv/bin/python --version` → `Python 3.12.14` |
| 3 | `make test` (at `391783c`) | 0 | 122 s | `All checks passed!`, then pytest dots only: **no summary line** (1,940 result characters: 1,939 `.` and 1 `s`). Defect, §3 |
| 3' | `make test` (at `b8e0b44`, after the fix) | 0 | 101 s | `All checks passed!` … `1939 passed, 1 skipped in 100.41s (0:01:40)` |

### 2.2 Docker (task b)

| # | Command | Exit | Time | Key output |
|---|---|---|---|---|
| 4 | `docker build -t wta-fresh .` | 0 | 167 s (cold); 78 s after the fix (pip layer cached) | `naming to docker.io/library/wta-fresh:latest` |
| 5 | `docker run --rm wta-fresh python -m wta config hash` | 0 | 2 s | `de294ab3c652`, the same as `python -m wta config hash` on the host, with or without the `.env` copied from `.env.example` |
| 6 | `docker run --rm wta-fresh python -m pytest -q -x -p no:cacheprovider` (at `391783c`) | 0 | ~2 min | the **full suite**, not a subset: 1,939 `.` and 1 `s`, no failure (the extra `-q` hid the summary here too) |
| 6' | `docker run --rm wta-fresh python -m pytest -x -p no:cacheprovider` (at `b8e0b44`) | 0 | 112 s | `1939 passed, 1 skipped in 110.54s (0:01:50)` |
| 7 | `docker compose build` | 0 | 9 s (layers cached from #4) | `naming to docker.io/library/wallex-trader-agent:latest` |
| 8 | `docker compose run --rm --no-deps agent python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h` | 0 | 144 s | the same lines as #9 below, ending `result:   40/40 decisions reproduced -> PASS` |

`docker compose down` afterwards removed the `fresh_default` network. No container was left running.

### 2.3 The judges' quick start, README steps 1–6 (task c)

| # | README step / command | Exit | Time | Key output |
|---|---|---|---|---|
| 9 | step 5: `make replay-deliverable` | 0 | 123 s | see the block below; `git status` clean afterwards (nothing written into the repo) |
| 10 | step 1: `make shadow-synth`, Ctrl-C after 65 s | SIGINT | 65 s | `AI: offline rules (no LLM key): AI plans come from offline-rules-v1 (deterministic rules, NOT an LLM)`; `engine: shadow mode, feed synthetic, commit 391783cc3e8f, config 73958c6765c6, 21 symbols`; an S2 `place` of `USDTTMN BUY 4.68 @ 266721` in the first tick; `heartbeat: feed=synthetic ... fresh_books=21/21 equity=4996663.417400 ...`; `engine: stopped (signal SIGINT)` |
| 11 | `python -m wta logs check --dir logs/shadow_synth` | 0 | 1 s | `decisions`/`orders`/`events`/`health` files `ok`, then `OK` |
| 12 | step 3: `make backtest-synth` | 0 | 28 s | `return -0.236%  max drawdown +0.770%  trades 2  orders 19  vetoes 18  fees 16,063 TMN`, `report: docs/REPORTS/synthetic_bt_books/report.md` (equal to the README) |
| 13 | step 4: `make backtest` | 2 | 1 s | `error: market database data/wta.sqlite does not exist` (as documented for a fresh clone) |
| 14 | `make backtest-books` | 2 | 1 s | the same error (as documented in REPRODUCE §4.2) |
| 15 | REPRODUCE §4.1: 72-h synthetic candle backtest (`--out`/`--state-db`/`--synthetic-db` under a scratch dir instead of `/tmp`) | 0 | 73 s | `return -0.798%  max drawdown +1.730%  trades 0  orders 8  vetoes 0  fees 17,334 TMN` (equal to REPRODUCE) |
| 16 | step 6: `python -m wta status --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite` | 0 | 1 s | `!! SYNTHETIC market data ...`; `mode shadow \| feed SYNTHETIC`; `commit 391783cc3e8f \| no tag \| config 73958c6765c6`; `Equity 4,996,635 TMN \| return -0.07%`; last decisions `plan` and `order S2 USDTTMN BUY approved, order sent` |
| 17 | step 6: `python -m wta tui ... --once` (same flags) | 0 | 1 s | one 66-line frame with the `SYNTHETIC FEED — not real market data` banner, header, equity, markets and health panels |
| 18 | `make status` (reads `logs/shadow`) | 0 | 1 s | before any `make shadow`: `!! no heartbeat in logs/shadow/health*.jsonl: the agent has not run with these logs` (as the README says, use the flags of #16 for the synthetic run) |
| 19 | step 2: `make shadow`, Ctrl-C after 170 s | SIGINT | 170 s | `markets fetch failed (... GET /v1/markets: ConnectTimeout (gave up after 4 retries)); using configs/markets_snapshot.yaml` (after about 56 s); 6 × `ws: connect failed: TimeoutError`; `heartbeat: feed=live rest_ok=False ws_ok=False fresh_books=0/21 equity=5000000 killed=False paused=True ...`; no `orders` file written, so no paper order; `engine: stopped (signal SIGINT)` |

Output of #9 (`make replay-deliverable`), identical in the container (#8):

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

The replayed run is the 4-h shadow run on the SYNTHETIC feed with the real OpenRouter LLM (`openai/gpt-5.4-mini`).
Its answers come from the committed LLM cache, which is why the replay needs no key and makes no network call.

### 2.4 Compose file (task d)

| # | Command | Exit | Key output |
|---|---|---|---|
| 20 | `docker compose config -q` (with `.env`) | 0 | — |
| 21 | `docker compose --profile shadow config --services` | 0 | `recorder`, `agent`, `shadow` |
| 22 | `docker compose config -q` without `.env` | 1 | `env file .../.env not found` (as the README and REPRODUCE §1 say: `cp .env.example .env` first) |

## 3. Defect found and fixed

| | |
|---|---|
| Symptom | `make test` and `docker compose run --rm --no-deps agent python -m pytest -q` print no `N passed, M skipped` line. The README promised `pytest (1,913 passed, 1 skipped)` |
| Cause | `pyproject.toml` sets `addopts = "-q"`. The Makefile (`$(PY) -m pytest -q`) and the docs added a second `-q`, and `-qq` drops the summary. Separately, the suite grew from 1,913 to 1,939 tests (`tests/test_replay_bundle.py`, 26 tests, in `391783c`) |
| Fix (`b8e0b44`) | `Makefile`: `test` runs `$(PY) -m pytest`, with a comment explaining why. `README.md`, `README.fa.md`, `docs/REPRODUCE.md`: the docker pytest line without `-q`, and the expected count `1939 passed, 1 skipped`. `docs/RUNBOOK.md`: the `make test` row. The README status table now says "1,939 tests pass on `main` (1,913 at `v0.5-backtest`)". `deliverables/TAGS.md` and `docs/PROGRESS.md` still say 1,913 for `v0.5-backtest`, which is correct for that tag |
| Re-verified | #3' and #6' above, on the clone pulled to `b8e0b44` |

One more change went into `391783c` before the clone. The "Deliverables index" in
[`deliverables/REPORT.md`](../../deliverables/REPORT.md) did not list `VIDEO.md`, which another agent had added in
the meantime. `python scripts/make_deliverables.py --index-only` added that one line and checked that every
relative link resolves.

## 4. Observations, not changed

1. **`make backtest-synth` dirties the tree.** It writes `docs/REPORTS/synthetic_bt_books/`, which is neither
   committed nor gitignored. Runs started after it log `commit 391783cc3e8f (dirty)` and `git_dirty: true` (seen in
   #19), and `make live` would refuse until the folder is gone. The README names that output path, so this is
   documented behaviour. A judge who wants clean provenance afterwards can run
   `rm -rf docs/REPORTS/synthetic_bt_books`. Possible fixes are a gitignore entry, or an `--out` under `logs/` as
   the Docker example already uses. **Fixed after this check (tag `v0.6-ops`):** `make backtest-synth` now writes
   `logs/synthetic_bt_books/` (gitignored), and `make replay` writes its report to `logs/` instead of `docs/REPORTS/`.
2. **Heartbeat before the first tick.** In a 75-s `make shadow` with the API unreachable, the markets retries took
   about 56 s and Ctrl-C came before the first engine tick. The stop-time log line then read `fresh_books=21/21`, while the
   same health record had `book_age_s: {}`, `ticks: 0` and `reasons: ["rest: no fresh data ...", "ws: no fresh
   data", ...]`. The log line counts symbols that are not in `stale_symbols`, and only ticks fill that set. Once a
   tick has run, it reads `0/21`, as the README says (#19). This is cosmetic, in `src/wta/engine.py`
   (`write_heartbeat`), and not changed here.
3. `make shadow` with the API unreachable spends about 56 s on `GET /v1/markets` retries before the engine starts.
   The first heartbeat came 90 s after the engine started, about 2.5 min after the command. The README says "until Ctrl-C", so this is not a defect,
   but a judge should wait about 3 min before expecting a heartbeat.
4. The host's config hash `de294ab3c652` (from `.env.example`) differs from the recorded run's `481cc6dc3ce2`,
   because the model names in `.env` are part of the hash. REPRODUCE §6 explains how to reproduce `481cc6dc3ce2`
   from the start event. Host (3.12) and container (3.11) give the same hash.

## 5. What this check does not cover

* No live trading and no Wallex account call: no `WALLEX_API_KEY` was set, and live mode was not started.
* No real-LLM call: no `OPENROUTER_API_KEY` in the clone. The real-LLM evidence is the committed 4-h run, replayed
  above from its cache.
* The 30-day candle backtest (about 17 min) was not re-run here; the readme check of the same day reproduced
  `-4.459%`.
* The public GitHub clone URL was not used: the repo is private. The local clone has the same commits as
  `origin/main`.
