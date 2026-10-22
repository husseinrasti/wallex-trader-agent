# Go-live checklist (operator)

Summary: live trading starts only when **the operator** says "go live" and runs the commands
below. The agent never starts live on its own. Every step is a hard gate: if one fails, stop.

## 0. Preconditions (one-time)
- [ ] `api.wallex.ir` reachable from the machine that will trade:
      `curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://api.wallex.ir/v1/markets` prints `200`.
- [ ] `.env` contains `WALLEX_API_KEY` of the **challenge credit sub-account**, `WALLEX_SUB_ACCOUNT_CLIENT_ID`,
      `CHALLENGE_END_UTC` (ISO-8601, UTC — **required**: live refuses to start without it, the flatten
      scheduler depends on it), optional LLM keys and Telegram.
- [ ] Recorder running and fresh: `bash scripts/ps.sh` shows books for every watchlist symbol in the last 10 min.

## 1. Code & tests
```bash
make test                      # must be green
git status --porcelain         # must print nothing (live refuses a dirty tree)
```

## 2. Account verification (read-only)
```bash
.venv/bin/python scripts/check_account.py
```
- [ ] Balances are the **challenge sub-account** (≈ 5,000,000 TMN), not the main wallet.
- [ ] No routing warnings in the summary (sub-account id set; MCP vs REST balances agree).
- [ ] Fees match the table (TMN 0.25/0.30 %, USDT 0.10/0.13 %).
- [ ] Market rules printed for all watchlist symbols; none banned.

## 3. Order-type probe (operator runs it; places and cancels 2 far-from-market orders)
```bash
.venv/bin/python scripts/check_account.py --probe-order
```
- [ ] LIMIT accepted and cancelled; STOP_LIMIT accepted (or rejected → set
      `execution.use_server_side_stops: false` — software stops take over) and cancelled.
- [ ] `docs/REPORTS/api_probe.md` committed.

## 4. Shadow sanity (>= 4 h before the first live start, PRD-05 §5)
- [ ] `docs/REPORTS/shadow_*` report exists; no exceptions in `logs/shadow/`.

## 5. Tag and start (half size for the first 6 h)
```bash
TAG=live-$(date -u +%Y%m%d-%H%M); git tag "$TAG" && git push origin "$TAG"
WTA_LIVE=1 .venv/bin/python -m wta run --mode live --i-understand-live --overlay configs/live_halfsize.yaml
```
(or detached: `nohup env WTA_LIVE=1 .venv/bin/python -m wta run --mode live --i-understand-live --overlay configs/live_halfsize.yaml > logs/agent.out 2>&1 & echo $! > run/agent.pid`)
- [ ] First heartbeat in `logs/health.jsonl` within 60 s; `paused=false`, `killed=false`.
- [ ] First S2 allocation appears in `logs/decisions.jsonl` with a `client_id` like `S2_USDTTMN_<ms>_<hex>`;
      verify it in the Wallex web UI **read-only** (never place manual orders on this account).

## 6. Full size after 6 h without incidents
- [ ] Stop (`kill $(cat run/agent.pid)`), tag a new `live-…`, restart without `--overlay`.

## 7. Monitoring (every 6 h)
- [ ] `python -m wta status`, `bash scripts/ps.sh`, read `logs/journal.md`.

## 8. Deadline
- [ ] T−2h: events show `flatten` phase 1; T−30min: phase 2; T−5min: `final_state` logged.
- [ ] After the end: stop the agent, copy logs to `deliverables/`, commit, tag `final`, push.

## Emergency
- Kill switch: `touch KILL` in the repo root → cancel entries, flatten to TMN, stay in monitoring mode.
- Reset (manual only): `python -m wta reset-kill --reason "..."` (logged as an event).
