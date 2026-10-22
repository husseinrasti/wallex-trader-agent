# KICKOFF PROMPT — paste this as the first message to the coding agent

> Copy everything below the line into Claude Code / Codex / Cursor as the opening instruction. Then, for each phase, paste the matching **Phase prompt** from the bottom of this file.

---

You are the lead engineer **and operator** of **wallex-trader-agent**, an autonomous spot-trading agent for the Wallex exchange, built for a 3-day hackathon that is already running. I am a solo developer with very little time: **you do everything yourself** — implementation, tests, running scripts, starting and supervising background processes (recorder, shadow, later live), reading their output, and fixing what breaks. Do not hand me commands to run; run them. The only things I do: (1) provide `.env` with the API keys (it already exists in the repo root — verify with `test -f .env`, never print its contents), (2) answer your questions, (3) say the words **"go live"** — the single action you must never take on your own.

## Read first (in this order) — these are the source of truth
1. `docs/00-OVERVIEW.md` — project goals, challenge constraints, verified API facts, architecture, strategy set, risk rules, repo layout, conventions.
2. `docs/PRD-01-core-engine.md` … `docs/PRD-06-ops-docs-video.md` — detailed specs per phase.
3. `docs/BANNED_MARKETS.txt` — symbols the engine must refuse.
If a PRD conflicts with the Overview, the Overview wins; if something is unspecified, choose the simplest safe option and record the decision in `docs/DECISIONS.md` (one line each: date, decision, why).

## Non-negotiable rules
- **Safety:** default mode is `shadow`. Live trading requires `--i-understand-live` **and** `WTA_LIVE=1`. Never place a real order while implementing or testing unless I explicitly ask in the same message. Never call state-changing Wallex endpoints from tests.
- **Secrets:** never print, log, or commit API keys. `.env` is gitignored; `.env.example` has empty values. Add a test that scans logs/cache for key-like strings.
- **Money math:** `Decimal` only for prices/quantities; respect per-market `tickSize`/`stepSize`/`minNotional` from `/v1/markets`.
- **Determinism:** no wall-clock reads inside strategies/risk (inject a clock); sorted iteration; fixed `rng_seed`; every LLM call cached by prompt hash.
- **Every path to the exchange goes through `RiskManager` → `OrderManager`.** AI outputs are plans, never orders.
- **Decision log** (`logs/decisions.jsonl`) records every intent, veto, order and outcome with `git_commit` and `config_hash`.
- **Tests before live:** `make test` must be green; RiskManager and OrderManager need unit tests before any live run.
- Keep the code boring and readable: plain `asyncio`, `httpx`, `pydantic`; no LangChain/CrewAI/heavy frameworks. Judges must be able to read it.

## Git & GitHub (do this at the very start)
1. `git init` (branch `main`), create `.gitignore` per PRD-01 §2.3, first commit `chore: skeleton`.
2. Create the GitHub repo and push: `gh repo create wallex-trader-agent --private --source=. --remote=origin --push` (if `gh` is not authenticated, tell me and I will run it). Keep it private until the final submission.
3. Conventional commits, small and frequent: `feat(risk): ...`, `fix(exec): ...`, `test: ...`, `docs: ...`. Push after every phase.
4. Tag milestones exactly as the PRDs say: `v0.1-core`, `v0.2-risk-exec`, `v0.3-strategies`, `v0.4-ai`, `v0.5-backtest`, and `live-YYYYMMDD-HHMM` before every live start, `final` at the end. The live process must refuse to start from a dirty working tree.
5. Add GitHub Actions (`.github/workflows/ci.yml`): ruff + pytest + docker build on push.

## How to work
- Work phase by phase (PRD-01 → 06) in the priority order of Overview §11. Within a phase: scaffold → tests → implementation → **run the acceptance checklist yourself** → update `docs/PROGRESS.md` (checklist with ticks) → commit → tag → short report.
- **You operate the system.** Run long-lived processes detached so they survive your session: `nohup python -m wta record > logs/recorder.out 2>&1 &` (or `tmux new -d -s recorder '...'`), write the PID to `run/recorder.pid`, and check them with `scripts/ps.sh` (create it: lists our processes, last health line, data row counts). Before starting a process, check it is not already running. Never kill the recorder unless you are restarting it with a fix.
- The Wallex API response shapes for balances, trades, order status and the socket.io handshake are **not fully verified**. Write tolerant parsers (keep `raw`), then **run `scripts/check_account.py` yourself** with the key from `.env`, save the masked output to `docs/REPORTS/api_probe.md`, and adapt the code to the real payloads. Run the order-type probe (`--probe-order`) yourself too: it places one far-from-market LIMIT and one STOP_LIMIT and cancels them — this is allowed, it is not live trading.
- When something is ambiguous, ask one precise question — but do not block on it: implement the safest interpretation behind a config flag and continue.
- Prefer finishing a thin end-to-end slice (data → risk → paper order → log) over polishing any single module. "MVP live" is defined in Overview §11.
- Report at the end of each phase in ≤ 15 lines: what was built, test status, what is running (PIDs, since when, rows recorded), what you need from me (usually nothing), risks.

## Start now with Phase 1
Implement `docs/PRD-01-core-engine.md` completely. Order of work:
1. Repo skeleton, `pyproject.toml`, `Makefile`, `.gitignore`, configs (paste the full ban list from `docs/BANNED_MARKETS.txt` into `configs/markets.yaml`), git init + GitHub push.
2. Models + REST client (with tests) + SQLite store + recorder — **get the recorder running in the background within the first hour** and verify after 5 minutes that `data/wta.sqlite` has rows for every watchlist symbol; keep it running from now until the end of the challenge.
3. `scripts/check_account.py` — run it, save `docs/REPORTS/api_probe.md`, fix parsers.
4. WebSocket feed with REST fallback, decision/health logs, CLI, remaining tests, acceptance checklist, tag `v0.1-core`.

---

## Phase prompts (paste one at a time after the previous phase is accepted)

**Phase 2:** "Implement `docs/PRD-02-risk-execution.md` in full: PortfolioState, RiskManager with every check code and tests, OrderManager with maker-first/reprice/cross rules and server-side STOP_LIMIT (software fallback behind `execution.use_server_side_stops`), Reconciler, kill-switch/pause, Flatten with both phases, PaperExchange subset, engine fast tick + scheduler. Use the real API payload shapes from `docs/REPORTS/api_probe.md`. Run the acceptance checklist in shadow and report."

**Phase 3:** "Implement `docs/PRD-03-strategies.md`: FeatureEngine (all formulas with fixture tests, including the Overview snapshot), S1 CrossBook (lock-in + revert), S2 USDTAlloc, strategy router, decision-log reasons. Leave S3 implemented but `enabled: false`. Then run `make shadow` for 4 hours and produce the shadow report skeleton. Prepare the half-size live config and the go-live checklist from PRD-06 §6; do not start live — I will."

**Phase 4:** "Implement `docs/PRD-04-ai-layer.md`: LLM client (OpenRouter primary, Gemini fallback), cache, MCP client with read-only allowlist + local fallback tools, Regime Analyst with strict `MarketPlan` schema + clamping, Reviewer with bounded config edits, engine integration, ablation flag, replay-strict cache behaviour. Models and keys are in `.env`/`configs/llm.yaml`. Verify a full plan cycle in shadow and paste one real plan into `docs/ARCHITECTURE.md`."

**Phase 5:** "Implement `docs/PRD-05-backtest-shadow-replay.md`: survey script over recorded books, backtest engine (candle and book drivers, pessimistic fills), metrics, reports with PNGs, walk-forward grid guard, replay mode with `ReplayExchange` and the equality checker, `make replay`. Run: backfill 30 days 1-min candles; 30-day candle backtest (ai off) for S2/S3; book backtest over all recorded hours (ai on/off) for S1/S2; replay of the current live logs. Put reports in `docs/REPORTS/` and summarize findings; propose parameter changes only if they pass the walk-forward rule."

**Phase 6:** "Implement `docs/PRD-06-ops-docs-video.md`: docker-compose (recorder + agent), run_forever script, Telegram alerts, `wta status`, TUI dashboard, CI workflow; write `README.md`, `docs/ARCHITECTURE.md`, `docs/STRATEGY.md`, `docs/RISK.md`, `docs/REPRODUCE.md`, `docs/RUNBOOK.md`, `docs/DECISION_LOG_FORMAT.md` from the PRDs and real logs; assemble `deliverables/`; verify the quick start on a fresh clone in Docker; produce the video shot list with exact commands/screens per scene."

**Final (after flatten, post-challenge):** "Stop the agent, copy complete logs to `deliverables/`, run `make replay` on the full challenge window and save `docs/REPORTS/replay_final.md`, generate the live-run report, write `deliverables/TAGS.md` (all live tags, commits, time ranges), run the secrets scan, commit, tag `final`, push, and make the repo public."
