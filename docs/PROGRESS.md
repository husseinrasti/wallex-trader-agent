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
- [ ] `make shadow` >= 1 h with a no-op strategy
- [ ] Injected intents produce complete decision records
- [ ] Simulated drawdown >= 5 % -> kill -> flatten; `reset-kill` works
- [ ] `scripts/flatten_now.py` (paper) converts a mixed portfolio to TMN
- [ ] Restart keeps peak equity, daily PnL, fees, open positions
- [ ] Tag `v0.2-risk-exec`

## Phase 3 — Strategies (PRD-03)
- [ ] Shadow >= 4 h with S1+S2
- [ ] `docs/STRATEGY.md`
- [ ] S1 cycle + S2 rebalance decision records pass `logs check`
- [ ] Half-size live config + go-live checklist prepared; tag `v0.3-strategies`

## Phase 4 — AI layer (PRD-04)
- [ ] Plan every 5 min in shadow with notes; MCP tools when reachable
- [ ] Network loss degrades to rules-only
- [ ] `wta replay` reproduces plans from cache with 0 network calls
- [ ] Reviewer applies <= 2 bounded changes; config_hash changes
- [ ] ARCHITECTURE "How AI is used" with a real plan; tag `v0.4-ai`

## Phase 5 — Backtest, survey, replay (PRD-05)
- [ ] Survey report
- [ ] 30-day candle backtest + book backtest (ai on/off) with PNGs
- [ ] Shadow report >= 4 h
- [ ] `make replay` reproduces decisions
- [ ] `deliverables/REPORT.md`; tag `v0.5-backtest`

## Phase 6 — Ops, docs, video (PRD-06)
- [ ] docker-compose, run_forever, Telegram, status, TUI, CI
- [ ] README, ARCHITECTURE, STRATEGY, RISK, REPRODUCE, RUNBOOK, DECISION_LOG_FORMAT
- [ ] deliverables assembled; fresh-clone Docker quick start verified; video shot list
