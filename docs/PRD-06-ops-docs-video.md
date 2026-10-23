# PRD-06 — Operations, Monitoring, Dashboard, Documentation, Demo Video, Submission

**Depends on:** PRD-01…05.
**Goal:** the agent runs unattended for 3 days, the judges can re-run it, and the deliverables are complete and persuasive.

Scoring targets: stability 15 pts, re-runnability 25 pts, video (gate + quality), docs (technical review).

---

## 1. Runtime & process management

- **Docker:** `Dockerfile` (python:3.11-slim, non-root, `pip install -e .`), `docker-compose.yml` with two services sharing `./data` and `./logs` volumes:
  - `recorder`: `python -m wta record` (restart: always)
  - `agent`: `python -m wta run --mode ${WTA_MODE}` (restart: always), env from `.env`.
  Optional third service `shadow` for parallel paper testing.
- **Bare-metal alternative** (documented): `systemd` units or `tmux` + `scripts/run_forever.sh` (loop with 10-s backoff, max 20 restarts/hour → stop and alert).
- **Restart safety:** PRD-02 §5 (reconcile on start; no reset of peak/daily/fees). Process restarts are logged as `events: start {reason: restart}` and counted in health.
- **Clock:** `ntp` check at startup (compare `Date` header from Wallex with local; warn if > 2 s).
- **Disk:** recorder ≈ 150 MB/day; logs < 50 MB/day. Check free space in heartbeat.

## 2. Monitoring & alerts

- **Health line** every 60 s (PRD-01 §8) + `python -m wta status` (reads latest health and prints a one-screen summary).
- **Telegram** (`wta/utils/telegram.py`, optional but recommended): alerts on start/stop/restart, every fill (strategy, symbol, side, notional, PnL if closing), risk pause/kill, flatten phases, LLM failures ≥ 3 in a row, data stale > 2 min, API error bursts, equity update hourly. Rate-limit to ≤ 20 msgs/10 min (digest otherwise). Inbound commands (optional): `/status`, `/pause`, `/resume`, `/kill` — each logged as an event with `source: telegram` (these are **operational**, not trading decisions; document that no manual trade is possible through them).
- **Watchdog:** if no heartbeat for 3 min, `run_forever.sh`/compose restarts the agent.

## 3. Terminal dashboard (`wta/dashboard/tui.py`, `python -m wta tui`)

`rich` live layout reading `logs/health.jsonl`, `decisions.jsonl`, `data/wta.sqlite` (read-only, separate process). Panels:
1. Header: mode, git tag/commit, config hash, uptime, time to deadline, flatten phase, paused/killed.
2. Equity: current TMN, return %, peak, drawdown %, daily PnL, fees used / budget, USDT share, coin exposure.
3. Markets: watchlist bid/ask/spread/age, cross-book edges for S1 coins vs cost (colored when ≥ margin).
4. Current plan: regimes with confidence, enabled strategies, size mult, USDT target, `notes` (wrapped).
5. Positions & open orders.
6. Last 10 decisions: time, strategy, symbol, side, qty, risk result (✓/✗ + code), reason (truncated), outcome PnL.
7. Health: ws/rest/llm/mcp status, API errors, restarts.
No GUI points are scored, but this is what the demo video shows — make it readable at 1080p.

## 4. Documentation deliverables (`docs/`, judge-facing)

| File | Content |
|---|---|
| `README.md` (root) | 1-paragraph pitch; **Quick start for judges** (clone → `.env` → `make install` → `make test` → `make shadow` → `make backtest` → `make replay`); where the logs/reports are; link to video; team & contact. |
| `docs/ARCHITECTURE.md` | diagram (Overview §3), components, data flow, modes, determinism/replay design, **How AI is used** (Analyst, Reviewer, MCP tools, cache, ablation) with one real plan pasted, failure handling. |
| `docs/STRATEGY.md` | For each strategy: thesis, exact entry/exit conditions (formulas + plain language), parameters and why, example decision record, survey findings, backtest/ablation results, known limitations. |
| `docs/RISK.md` | The full RiskManager table, kill-switch/pause semantics, flatten procedure, sizing math, fee budget, anti-wash/self-trade controls, what AI cannot do. |
| `docs/REPRODUCE.md` | Step-by-step re-run: environment, data needed (what's in `deliverables/`), `make replay` expectations, how to run backtests/shadow, how to verify git tag ↔ decision log commits, how to read `decisions.jsonl`. |
| `docs/REPORTS/` | survey, backtests, shadow, live run, replay proof (PRD-05). |
| `docs/DECISION_LOG_FORMAT.md` | field-by-field description of `decisions.jsonl` with 3 annotated examples (S1 cycle, S2 rebalance, a veto). |
| `deliverables/` | `decisions-*.jsonl`, `orders.jsonl`, `events.jsonl`, `health.jsonl` (sampled hourly), `journal.md`, `llm_cache/`, `REPORT.md`, `VIDEO.md` (link), `TAGS.md` (list of live tags with commit hashes and time ranges). |

Writing rule: judges read `README → STRATEGY → RISK → REPORTS`. Each file starts with a 5-line summary. Persian translation of README summary is a plus (optional `README.fa.md`).

## 5. Demo video (≈ 5 min, mandatory)

Script (record with screen + voice; show outputs, not code):
| t | Scene | Shows |
|---|---|---|
| 0:00–0:40 | Problem & approach | one slide: constraints (fees table, TMN valuation, 3 days), our thesis: "don't lose, trade regularly, be auditable"; 3-layer design |
| 0:40–1:40 | Architecture | diagram; "AI plans, rules trade, risk guards"; same code path for backtest/shadow/live/replay |
| 1:40–3:00 | Live dashboard | TUI: equity, plan notes read aloud, an S1 cycle or S2 rebalance appearing with its reason; open `decisions.jsonl` line and walk through fields |
| 3:00–3:40 | Risk in action | show a veto record (code), kill-switch demo in shadow (drawdown injected) → flatten to TMN |
| 3:40–4:30 | Evidence | survey chart, backtest equity + ablation table (AI on/off), shadow report, live equity curve |
| 4:30–5:00 | Reproducibility & wrap | `make replay` output "N/N reproduced", git tag ↔ log commit, final TMN balance; thanks |
Tools: OBS/QuickTime; 1080p; mic; no editing required. Upload (YouTube unlisted / Drive) and put the link in `deliverables/VIDEO.md` and README.

## 6. Go-live runbook (`docs/RUNBOOK.md`)

1. `make test` green; `git status` clean; `git tag live-<ts>`; `git push --tags`.
2. `scripts/check_account.py` → balances on the **challenge sub-account**, fees, order probe OK.
3. `.env`: `WTA_MODE=live`, deadline set, Telegram set; keep `WTA_LIVE=0` in `.env` (as built, live is armed only by `WTA_LIVE=1` in the live command's own environment, never by `.env`; see `docs/GO_LIVE_CHECKLIST.md`).
4. Start recorder (if not running) → start agent → confirm first heartbeat + Telegram "started".
5. Watch first S2 allocation fill; verify in Wallex web UI (read-only!) that the order carries our `client_id` pattern.
6. Half-size for the first 6 h, then defaults (config change logged by `events`).
7. Every 6 h: `python -m wta status`; read `journal.md`; never place manual orders on this account.
8. T−2h: confirm flatten phase 1 started; T−30min: phase 2; T−5min: `final_state` logged; after end: stop agent, copy logs to `deliverables/`, commit, tag `final`, push.

## 7. Submission checklist

- [ ] GitHub repo public (or shared with judges), default branch `main`, tags `live-*` and `final`.
- [ ] `README.md` quick start verified on a fresh machine/container (`docker compose run agent python -m wta backtest ...`).
- [ ] `deliverables/` committed: decision logs covering the entire challenge window, reports, replay proof, journal, llm cache, video link, tags list.
- [ ] `docs/` complete (ARCHITECTURE, STRATEGY, RISK, REPRODUCE, RUNBOOK, DECISION_LOG_FORMAT, REPORTS/).
- [ ] Final account state: 100% TMN (dust documented).
- [ ] Video uploaded, link in README; length ≈ 5 min.
- [ ] Secrets scan: `git log -p | grep -E "API_KEY|sk-|\|[A-Za-z0-9]{30,}"` returns nothing; `.env` never committed.
- [ ] Team can explain every file (judges may ask); `docs/STRATEGY.md` has a "how to explain in 2 minutes" box.

## 8. Tests

- `test_tui.py`: renders with empty and populated fixtures (no crash).
- `test_telegram.py`: rate limiting/digest; commands create events.
- `test_run_forever.sh` (bash): restart loop and backoff.
- Docker build in CI (GitHub Actions: `ruff`, `pytest`, `docker build`) — a green badge in the README helps the re-runnability score.
