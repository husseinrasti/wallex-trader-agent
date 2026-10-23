# Progress

Legend: [x] done and verified · [~] done offline, needs the live API to verify · [ ] open

> **Connectivity:** `api.wallex.ir` drops the TLS handshake from the development machine (likely
> IP filtering). Everything below is built and tested offline against the documented payloads
> (`docs/WALLEX_API_NOTES.md`) and a deterministic synthetic feed; items marked [~] re-verify
> automatically once the API is reachable.

## Phase 1 — Core engine (PRD-01)
- [x] `make install && make test` green (563 tests, ruff clean; Python 3.12 local, 3.11 Docker build)
- [~] `python -m wta markets` implemented + tested with fixtures (banned -> exit 1, unreachable -> exit 2); live run pending API access
- [~] `python -m wta record` running detached since 2026-10-22 10:42 UTC (run/recorder.pid); survives the outage; rows start when the API is reachable
- [~] `python -m wta feed BTCUSDT` implemented (WS Centrifugo + REST fallback, tested against a local WS server)
- [~] `scripts/check_account.py` report saved (`docs/REPORTS/api_probe.md`: API unreachable); `--probe-order` must be run by the operator
- [x] `python -m wta logs check` validates `logs/` (exit 0)
- [x] Tag `v0.1-core` pushed

## Phase 2 — Risk & execution (PRD-02)
- [x] Shadow >= 1 h with the no-op strategy: 3700 s synthetic-feed soak, 62 heartbeats, 0 errors, `logs check` OK (live-feed run pending API access)
- [x] Injected intents produce complete decision records (docs/REPORTS/phase2_acceptance.md run b)
- [x] Simulated drawdown >= 5 % -> kill -> flatten; `reset-kill` works (run c, synthetic shock -8 %)
- [x] `scripts/flatten_now.py` (paper) converts a mixed portfolio to TMN (run d)
- [x] Restart keeps peak equity, daily PnL, fees, open positions (run e + tests)
- [x] Tag `v0.2-risk-exec`

## Phase 3 — Strategies (PRD-03)
- [~] Shadow >= 4 h with S1+S2: 4.08 h on the synthetic feed with the real LLM (`docs/REPORTS/shadow_20261023_synth4h/`); live feed pending API access
- [x] `docs/STRATEGY.md` (evidence sections TODO until data exists)
- [x] S1 cycle + S2 rebalance decision records pass `logs check` (docs/REPORTS/phase3_acceptance.md, tests/test_strategies_engine.py)
- [x] Half-size live config (`configs/live_halfsize.yaml`, `run --overlay`) + `docs/GO_LIVE_CHECKLIST.md`; tag `v0.3-strategies`

## Phase 4 — AI layer (PRD-04)
- [x] Plan every slow-loop interval in shadow with notes: 15 real OpenRouter plans (gpt-5.4-mini) in 900 s at a 60 s interval (synthetic feed; docs/REPORTS/phase4_acceptance.md). [~] MCP tools on the real feed pending API reachability
- [x] Provider failure / network loss degrades to rules-only (`llm_ok=false`, no exceptions)
- [x] Plans replay from the cache with 0 provider calls (`python -m wta.ai.replay`: 15/15 LLM, 6/6 offline). Full engine replay: Phase 5
- [x] Reviewer applies <= 2 bounded changes; config_hash of later decisions changes (tests + 2 real reviewer journal entries)
- [x] ARCHITECTURE "How AI is used" with a real plan; tag `v0.4-ai`

## Phase 5 — Backtest, survey, replay (PRD-05)
Details, commands and numbers: `docs/REPORTS/phase5_acceptance.md`. SYNTHETIC results are never evidence of performance.
- [x] Survey report with S1 opportunity counts and the recommendation block (`survey_20261022.md`). Verdict: **insufficient data** (0.33 h of aligned real books; 0 opportunities at every margin); defaults kept
- [~] 30-day candle backtest (ai off) with PNGs: on SYNTHETIC candles (`bt_candles_30d/`), since no real 30-day backfill exists; all 2.25 h of real candles in `bt_candles_recorded_ai_off/`. Real backfill pending API access
- [~] Book backtest ai on/off with PNGs: all real books (0.32 h, `bt_books_recorded_ai_{off,on}/`, identical); >= 24 h only on SYNTHETIC books (`bt_books_synth24h_*`); ablation (`ablation_20261023.md`) and walk-forward (default kept, `walkforward_s1_margin*/`)
- [~] Shadow report >= 4 h: 4.08 h, S1+S2, real LLM, SYNTHETIC feed (`shadow_20261023_synth4h/`); a live-feed shadow is pending API access
- [x] `make replay` reproduces decisions: 40/40 over the whole 4-h shadow run (7,292 of 7,292 ticks; the strict pass, time stamps included, is part of the verdict), 0 socket attempts, 0 LLM provider calls (`replay_20261023.md`); no live run to replay yet
- [x] `deliverables/REPORT.md` assembled (survey → backtests → ablation → walk-forward → shadow → live: none yet → replay); `docs/REPORTS/` consolidated, index regenerated
- [x] Review fixes (16 findings: walk-forward end rule, book fill fees / level cap / recording gaps, candle spread window, replay verdict and segment checks, report dates and texts), each with a test; affected reports rerun (`phase5_acceptance.md` §8)
- [x] `ruff check src tests scripts backtest` clean; `pytest`: 1913 passed, 1 skipped (no network)
- [ ] Commit the second- and third-session changes; tag `v0.5-backtest`

## Phase 6 — Ops, docs, video (PRD-06)
- [x] docker-compose (recorder + agent + optional shadow, WTA_LIVE pinned to 0), run_forever.sh (restart cap + heartbeat watchdog), Telegram alerts/commands (needs TELEGRAM_CHAT_ID), `wta status`, `wta tui`, CI (ruff + pytest + docker build, green)
- [x] README (+ README.fa.md), ARCHITECTURE, STRATEGY, RISK, REPRODUCE, RUNBOOK, DECISION_LOG_FORMAT, GO_LIVE_CHECKLIST, SUBMISSION_CHECKLIST
- [x] deliverables assembled (REPORT.md, 4 h run logs + llm_cache, replay bundle: `make replay-deliverable` -> 40/40 PASS, 0 network; real market DB); fresh-clone quick start verified on host + Docker (docs/REPORTS/fresh_clone_check.md); video shot list (docs/VIDEO_SHOTLIST.md + scripts/demo.sh). [ ] Video recording and live run: operator
