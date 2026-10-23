# wallex-trader-agent

[![ci](https://github.com/husseinrasti/wallex-trader-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/husseinrasti/wallex-trader-agent/actions/workflows/ci.yml)

**Summary**
1. An autonomous, explainable spot-trading agent for the Wallex exchange, built for the Starcoach "Wallex Algorithmic Trading Challenge". In one line: **AI plans, rules trade, risk guards.**
2. One code path for four modes: `backtest`, `shadow` (paper trading on live books), `live` and `replay`. Only the exchange object, the data source and the clock change.
3. Every decision is one JSON line holding the signal, the AI plan in force, all 23 risk checks, the order and its outcome, plus `git_commit` and `config_hash`.
4. Proven offline: 1,939 tests pass. A 4-hour shadow run with a real LLM, on the synthetic feed, replays 40 of 40 decisions with zero network calls (`make replay-deliverable`).
5. Not proven yet: profitability, and behaviour on real Wallex data. `api.wallex.ir` answers the development machine only now and then, so only about 20 minutes of real order books exist. No live trading has happened.

[فارسی (Persian summary and quick start)](README.fa.md)

## The idea

A negative return scores zero in this challenge, fees are 0.25–0.30 % on TMN pairs, and only the final
TMN balance counts. Our thesis: **don't lose, trade regularly, be auditable.** The agent therefore has
three layers that are kept strictly apart. A deterministic **fast loop** runs every 2 s: order books →
features → strategy intents → risk → orders. The strategies are **S1 CrossBook**, which buys a coin in
whichever of its TMN and USDT books is cheaper and sells it in the other when the gap beats both fees
plus a margin, so it does not depend on direction. **S2 USDTAlloc** holds an explicit, bounded share of USDT. **S3 RegimeMomentum** is off by
default. An LLM **slow loop** contains the Regime Analyst (every 5 min, with tool calling over the
Wallex MCP server and internal tools) and the Post-trade Reviewer (hourly). It writes a clamped *plan*:
regimes, enabled strategies, size multiplier and USDT target. The plan can only narrow what the rules
do, and the slow loop never places an order. A deterministic **guardian** is the third layer: the
RiskManager with 23 checks, a drawdown kill-switch, and a flatten to TMN before the deadline. Every
order passes through it, and no AI output can override it. Every LLM answer is cached by a hash of its
request, so `replay` re-runs a recorded session without any network access.

```
 SLOW LOOP  (LLM)            Regime Analyst every 5 min -> MarketPlan (validated, clamped, cached)
                             Post-trade Reviewer hourly -> journal + bounded parameter edits
                 | plan (can only narrow the rules)
 FAST LOOP  (deterministic)  books -> features -> S1 CrossBook / S2 USDTAlloc / S3 RegimeMomentum
                 | intents
 GUARDIAN   (deterministic)  RiskManager (23 checks) | kill switch | flatten to TMN at T-2h / T-30min
                 | approved orders
 EXCHANGE                    live (Wallex REST) | paper (shadow) | backtest | replay (recorded answers)
                 -> decisions.jsonl, orders.jsonl, events.jsonl, health.jsonl
```

## Status (2026-10-23, tag `v0.5-backtest`)

> | | What | Evidence |
> |---|---|---|
> | **Proven** | 1,939 tests pass on `main` (1,913 at `v0.5-backtest`), 1 skipped; CI runs ruff, pytest on Python 3.11 and a Docker build | badge above, `make test` |
> | **Proven** | The same `Engine` runs backtest, shadow and replay; risk rules fire as specified (kill-switch at −5 % drawdown, then flatten; fee budget; rate caps) | [phase2_acceptance](docs/REPORTS/phase2_acceptance.md), [REPORT.md](deliverables/REPORT.md) |
> | **Proven** | A real LLM is in the loop: a 4.08-h shadow run on the **synthetic feed** got 38 plans from `openai/gpt-5.4-mini` via OpenRouter. When the provider failed, the engine ran rules-only, as designed | [shadow report](docs/REPORTS/shadow_20261023_synth4h/report.md) |
> | **Proven** | Replay of that run: 40/40 decisions identical (strict pass, time stamps included), 0 socket attempts, 0 LLM calls | [replay proof](docs/REPORTS/replay_20261023.md), `make replay-deliverable` |
> | **Pending** | **Live run: none.** Live trading is started only by the operator after "go live" ([GO_LIVE_CHECKLIST](docs/GO_LIVE_CHECKLIST.md)). No live order has been placed | — |
> | **Pending** | **Real data:** about 20 minutes of REST order books (2026-10-22 14:20–14:40Z, plus a 1-second burst at 20:16Z) and 2.2 h of 1-minute candles. The survey verdict is "insufficient data". No 30-day candle backfill exists | [survey](docs/REPORTS/survey_20261022.md) |
> | **Pending** | **API connectivity:** `api.wallex.ir` answers the development machine only intermittently. The WebSocket feed, the MCP tools, STOP_LIMIT acceptance and sub-account routing are built against the docs and still unverified on the live API | [api_probe](docs/REPORTS/api_probe.md) |
> | **Pending** | **Operator settings:** `CHALLENGE_END_UTC`, `WALLEX_SUB_ACCOUNT_CLIENT_ID` and `TELEGRAM_CHAT_ID` are still empty in the operator's `.env` (live refuses to start without `CHALLENGE_END_UTC`). The Gemini fallback key is quota-limited (HTTP 429), so in practice an OpenRouter failure means rules-only | [RUNBOOK](docs/RUNBOOK.md) |
>
> All the longer runs used **SYNTHETIC** (generated) market data: the 4-h shadow run, the 6-h, 24-h
> and 30-day backtests and the walk-forward. Those reports say so in their titles and banners. They
> prove the mechanics, never the returns.

## Quick start for judges

Needs Python ≥ 3.11 (tested with 3.11 in Docker/CI, 3.12 and 3.14), `make` and git. None of these
commands can place a real order: live mode needs `WTA_LIVE=1` in the environment, `--i-understand-live`,
a clean git tree and `CHALLENGE_END_UTC`.

```bash
git clone https://github.com/husseinrasti/wallex-trader-agent.git
cd wallex-trader-agent
cp .env.example .env          # all keys are optional, see below
make install                  # creates .venv (uses python3; pick another with: make install PYTHON=python3.12)
source .venv/bin/activate     # so that `python` below is the venv's (make targets use .venv on their own)
make test                     # ruff ("All checks passed!") + pytest (`1939 passed, 1 skipped`), about 2 min, exit 0
```

**Keys.** Without any key everything below works. The AI layer then uses the offline rules provider
(`offline-rules-v1`). It is labelled "NOT an LLM" in the logs, in `wta status` and in every report.
With `OPENROUTER_API_KEY` and `OPENROUTER_MODEL_FAST` set, the Regime Analyst calls the real LLM. The
`WALLEX_API_KEY` is needed only for account commands (`wta account`, `scripts/check_account.py`) and
for live trading. Market data is public.

| # | Command | Needs | Time | What you should see |
|---|---|---|---|---|
| 1 | `make shadow-synth` | nothing (offline synthetic feed) | until Ctrl-C | `AI: offline rules (no LLM key) ...`, one `heartbeat: feed=synthetic ... fresh_books=21/21` line per 60 s, an S2 order in the first tick; Ctrl-C prints `engine: stopped (signal SIGINT)` |
| 2 | `make shadow` | `api.wallex.ir` reachable | until Ctrl-C | paper trading on live books. When the API is unreachable: `markets fetch failed ... using configs/markets_snapshot.yaml`, WebSocket timeouts, `fresh_books=0/21`, and no trades |
| 3 | `make backtest-synth` | nothing (writes a SYNTHETIC 6-h book DB) | ~30 s | `return -0.236%  max drawdown +0.770%  trades 2  orders 19  vetoes 18  fees 16,063 TMN`, report in `docs/REPORTS/synthetic_bt_books/` |
| 4 | `make backtest` | 30 days of real 1-min candles in `data/wta.sqlite` (`python -m wta backfill --days 30`, needs the API) | ~15–20 min | on a fresh clone: `error: market database data/wta.sqlite does not exist` (exit 2). The offline variant is in [REPRODUCE §4](docs/REPRODUCE.md#4-backtests) |
| 5 | `make replay-deliverable` | nothing (the bundle is in git) | ~2–3 min | `result:   40/40 decisions reproduced -> PASS` with `network:  0 socket attempts, 0 LLM provider calls` (all lines in [REPRODUCE §3](docs/REPRODUCE.md#3-replay-the-key-reproducibility-check)) |
| 6 | `python -m wta status` / `python -m wta tui` | a run's logs | instant | one-screen summary / live dashboard (Ctrl-C quits, `--once` prints one frame) |

`make status` and `make tui` read the logs of `make shadow` (`logs/shadow`). For the synthetic run of step 1:

```bash
python -m wta status --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite
python -m wta tui    --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite
```

`make replay` (no bundle) replays the newest run under `logs/` instead, for example the one you just
made in step 1. It rewrites `docs/REPORTS/replay_<day>.md` and the reports index; run
`git checkout -- docs/REPORTS` afterwards to restore the committed proof (details in [REPRODUCE](docs/REPRODUCE.md)).

### Docker

```bash
cp .env.example .env                                  # compose reads it (env_file)
docker compose build
docker compose run --rm --no-deps agent python -m pytest
docker compose run --rm --no-deps agent python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h
docker compose run --rm --no-deps agent python -m wta backtest --driver books \
  --synthetic-db data/synthetic_books.sqlite --synthetic-hours 6 --strategies S1,S2 --out logs/bt_synth_books
docker compose run --rm --no-deps agent python -m wta run --mode shadow --feed synthetic \
  --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite --seconds 120

docker compose up -d recorder agent                   # long-running: recorder + shadow agent on the real feed
docker compose logs -f agent
docker compose down
```

`--no-deps` matters: `agent` depends on `recorder`, so without it compose also starts the recorder and
leaves it running. Only `./data`, `./logs` and `./configs/runtime` are mounted, so write outputs under
`logs/` (the backtest above writes `logs/bt_synth_books/report.md`). The image runs Python 3.11 as a
non-root user, and the synthetic backtest gives the same numbers there as on the host.

## Where things are

| Path | What | In git |
|---|---|---|
| [`docs/REPORTS/`](docs/REPORTS/README.md) | survey, backtests, AI ablation, walk-forward, shadow report, replay proof; index in `README.md` | yes |
| [`deliverables/REPORT.md`](deliverables/REPORT.md) | the final report, assembled from `docs/REPORTS` (real and SYNTHETIC sections labelled) | yes |
| `deliverables/replay/shadow_synth_4h/` | replay bundle of the 4-h shadow run: `logs.tar.xz`, `state.sqlite.xz`, `SHA256SUMS` | yes |
| `deliverables/shadow_synth_4h/` | that run's decision, order and event logs, hourly health, journal, LLM cache | yes |
| [`deliverables/TAGS.md`](deliverables/TAGS.md) | every git tag with its commit, and which commit produced each evidence run | yes |
| `logs/<run>/` | logs of runs you start: `decisions-YYYYMMDD.jsonl` (+ `decisions.jsonl` link), `orders`, `events`, `health`, `journal.md`, `llm_cache/` | no |
| `data/` | the market store `data/wta.sqlite` and one engine state DB per mode and feed | no |
| `configs/` | `default.yaml` (every parameter, hashed into `config_hash`), `markets.yaml` (watchlist + verbatim ban list), `llm.yaml`, `live_halfsize.yaml` | yes |

## Documentation (read in this order)

1. [`docs/STRATEGY.md`](docs/STRATEGY.md): S1, S2, S3 and FLATTEN with exact conditions, parameters and evidence, plus a "how to explain in 2 minutes" box.
2. [`docs/RISK.md`](docs/RISK.md): the 23 RiskManager checks, kill and pause, flatten, sizing, fee budget, anti-wash controls, and what AI cannot do.
3. [`docs/REPORTS/`](docs/REPORTS/README.md) and [`deliverables/REPORT.md`](deliverables/REPORT.md): every result, each labelled real or SYNTHETIC.
4. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components, data flow, modes, determinism, and how AI is used, with a real plan.
5. [`docs/REPRODUCE.md`](docs/REPRODUCE.md): re-run everything; check git tags against the logs.
6. [`docs/RUNBOOK.md`](docs/RUNBOOK.md): operations, monitoring, emergencies.
7. [`docs/DECISION_LOG_FORMAT.md`](docs/DECISION_LOG_FORMAT.md): every field of `decisions.jsonl`, with annotated examples.
8. [`docs/GO_LIVE_CHECKLIST.md`](docs/GO_LIVE_CHECKLIST.md): the operator's gate before live trading.

Background: [`docs/00-OVERVIEW.md`](docs/00-OVERVIEW.md) and the PRDs (the spec),
[`docs/DECISIONS.md`](docs/DECISIONS.md) (one line per design decision),
[`docs/PROGRESS.md`](docs/PROGRESS.md) (what is done and verified).

## Demo video

About 5 minutes, recorded by the operator. **Not recorded yet:** the link will be in
[`deliverables/VIDEO.md`](deliverables/VIDEO.md) and here. The shot list and narration are in
[`docs/VIDEO_SHOTLIST.md`](docs/VIDEO_SHOTLIST.md). `bash scripts/demo.sh --list` lists the scenes,
which all run offline on the synthetic feed.

## Team and contact

Hussein Rasti, solo developer. GitHub: [@husseinrasti](https://github.com/husseinrasti).
Questions about any file are welcome. Please open an issue on the repository.
