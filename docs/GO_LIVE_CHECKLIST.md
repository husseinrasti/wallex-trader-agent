# Go-live checklist (operator)

**Summary**
1. Live trading starts only when **the operator** says "go live" and runs the commands below by hand. The agent never starts live on its own, and no live order has been placed so far.
2. Every step is a hard gate: if one fails, stop and fix it before going on.
3. Required in `.env` before live: `WALLEX_API_KEY` of the challenge account, `CHALLENGE_END_UTC` (live refuses to start without it) and `WALLEX_SUB_ACCOUNT_CLIENT_ID`; the last two are still empty today. `TELEGRAM_CHAT_ID` (also empty) and the LLM keys are optional.
4. The first 6 hours run at half size with `--overlay configs/live_halfsize.yaml`; after 6 h without incidents the agent is restarted at full size from a new tag.
5. `api.wallex.ir` is reachable from the dev machine only intermittently, so check reachability several times and trade from a network where it answers consistently.

Commands assume the repo root and `.venv` (`make install`). Background: [`RUNBOOK.md`](RUNBOOK.md)
(operations), [`RISK.md`](RISK.md) (guards and limits).

## 0. Preconditions (one-time)

- [ ] The operator has said "go live".
- [ ] `api.wallex.ir` answers from the machine that will trade, three times over at least 10 minutes:
      `curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://api.wallex.ir/v1/markets` prints `200`.
- [ ] `.env` (never committed; changing it does not dirty the tree):

| Variable | Required? | Why |
|---|---|---|
| `WALLEX_API_KEY` | **required** | the key of the **challenge credit sub-account**; live refuses to start without a key |
| `CHALLENGE_END_UTC` | **required** | the official end, ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`). Live refuses to start without it (or with a past time): the flatten to 100 % TMN at T−2h / T−30min depends on it. **Empty today** |
| `WALLEX_SUB_ACCOUNT_CLIENT_ID` | **required** for the challenge sub-account | Wallex needs the `sub-account-client-id` header to trade spot on a credit account; without it account calls go to the main wallet. `scripts/check_account.py` lists `/sub-accounts`. **Empty today** |
| `CHALLENGE_START_UTC` | optional | informational |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional | alerts and `/status /pause /resume /kill`; also need `--set alerts.telegram_enabled=true` on the run. `TELEGRAM_CHAT_ID` is **empty today** |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL_FAST`, `OPENROUTER_MODEL_STRONG` | optional | the AI plan and the reviewer; without a key the offline rules provider runs (labelled, not an LLM) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | optional | fallback only; the current key is quota-limited (HTTP 429), so do not count on it |
| `WTA_LIVE` | keep `0` | live is armed only by `WTA_LIVE=1` in the command's own environment, never by `.env` |

- [ ] `.venv/bin/python -m wta config show | grep challenge_end_utc` prints the end time you set.
- [ ] Recorder running and fresh: `bash scripts/ps.sh` shows books for every watchlist symbol in the
      last 10 minutes.

## 1. Code and tests

```bash
make test                      # must be green
git status --porcelain         # must print nothing (live refuses a dirty tree, untracked files included)
```

## 2. Account verification (read-only)

```bash
.venv/bin/python scripts/check_account.py      # exit 0 = every section clean, no warning
```

- [ ] Balances are the **challenge sub-account** (≈ 5,000,000 TMN), not the main wallet.
- [ ] No routing warnings (sub-account id set; MCP and REST balances agree where both answer).
- [ ] Fees match the table (TMN pairs 0.25 / 0.30 %, USDT pairs 0.10 / 0.13 %).
- [ ] Market rules printed for every watchlist symbol; none banned.
- [ ] The script rewrote `docs/REPORTS/api_probe.md`: read it, then commit it (the tree must be clean
      again before step 5).

## 3. Order-type probe (operator only; places and cancels 2 far-from-market orders)

```bash
.venv/bin/python scripts/check_account.py --probe-order     # asks you to type PROBE
```

- [ ] LIMIT accepted and cancelled.
- [ ] STOP_LIMIT accepted and cancelled. If it is rejected, set `execution.use_server_side_stops: false`
      in `configs/default.yaml` (software stops take over) and commit.
- [ ] `docs/REPORTS/api_probe.md` committed.

## 4. Shadow on the real feed (≥ 4 h before the first live start, PRD-05 §5)

The only ≥ 4 h shadow run so far used the **synthetic** feed (`docs/REPORTS/shadow_20261023_synth4h/`);
it does not satisfy this step.

```bash
nohup scripts/run_forever.sh run --mode shadow --log-dir logs/shadow > logs/agent_shadow.out 2>&1 &
echo $! > run/shadow.pid
# after >= 4 h
.venv/bin/python -m wta status --mode shadow
.venv/bin/python -m wta logs check --dir logs/shadow --secrets
.venv/bin/python -m wta report --source logs/shadow --out docs/REPORTS/shadow_$(date -u +%Y%m%d)_real
```

- [ ] Heartbeats with `rest_ok=true` and fresh books; no `tick_error` in `logs/shadow/events.jsonl`.
- [ ] `llm_ok=true` (or rules-only by choice) and plan records in `logs/shadow/decisions.jsonl`.
- [ ] Report committed.
- [ ] To keep this paper engine running next to live with Telegram configured, restart it with
      `--set alerts.telegram_commands_enabled=false` (only one engine per bot may poll commands).
      Otherwise stop it: `kill "$(cat run/shadow.pid)"`.

## 5. Tag and start (half size for the first 6 h)

```bash
git status --porcelain                                      # still empty
TAG=live-$(date -u +%Y%m%d-%H%M); git tag "$TAG" && git push origin "$TAG"
nohup env WTA_LIVE=1 .venv/bin/python -m wta run --mode live --i-understand-live \
  --overlay configs/live_halfsize.yaml > logs/agent.out 2>&1 & echo $! > run/agent.pid
```

With Telegram configured, add `--set alerts.telegram_enabled=true` to the run command (and use the
same flag on every later restart). The live engine logs to `logs/`, keeps its state in
`data/state_live.sqlite`, its LLM cache in `logs/llm_cache/` and its reviewer overlays in
`configs/runtime/live/`.

- [ ] `logs/agent.out` shows no `refusing to start live mode: …` (the message names the failed guard).
- [ ] First heartbeat within 60 s: `.venv/bin/python -m wta status --mode live`; `paused=false`,
      `killed=false`, books fresh.
- [ ] Start event: `grep '"kind":"start"' logs/events.jsonl | tail -n 1` shows `"mode":"live"`,
      `"git_tag"` = `$TAG`, `"git_dirty":false`, `"overlays":["configs/live_halfsize.yaml"]`,
      `"markets_source":"api"`, the AI source you expect.
- [ ] First S2 allocation appears in `logs/decisions.jsonl` with a `client_id` like
      `S2_USDTTMN_<ms>_<hex>`; verify it in the Wallex web UI **read-only** (never place, change or
      cancel an order by hand on this account).

## 6. Full size after 6 h without incidents

- [ ] No kill, no unexpected pause, no `tick_error`, no `reconcile_drift` in `logs/events.jsonl`.
- [ ] Stop: `kill "$(cat run/agent.pid)"`; wait for the `stop` event in `logs/events.jsonl`.
- [ ] Tag a new `live-YYYYMMDD-HHMM` and push it.
- [ ] Restart with the same command **without** `--overlay configs/live_halfsize.yaml` (keep any other
      `--set` flags). The `start` event shows `reason: restart`, the restored state and a new
      `config_hash`.

## 7. Monitoring (every 6 h; RUNBOOK §7)

- [ ] `.venv/bin/python -m wta status --mode live` and `bash scripts/ps.sh`: no `!!` lines.
- [ ] `tail -n 50 logs/events.jsonl`; `logs/journal.md` (reviewer); `wta logs check --dir logs --secrets`.

## 8. Deadline

- [ ] T−2h: `logs/events.jsonl` has `flatten` with `kind: flatten_start`.
- [ ] T−30min: `flatten` with `kind: flatten_final`.
- [ ] T−5min: `final_state` says "100% TMN (dust only)". If it says "ALERT: not flat yet", stop the
      agent and run `make flatten-live` at once.
- [ ] After the end: stop the agent; copy the run's dated `decisions`/`orders`/`events`/`health`
      files, `journal.md` and `llm_cache/` into `deliverables/`; `wta logs check --dir <that dir>
      --secrets`; commit; tag `final`; push.

## Emergency

- **Kill switch:** `touch KILL` in the repo root (or Telegram `/kill`) → entries cancelled, flatten to
  TMN, the engine stays in monitoring mode.
- **Reset (manual only):** `rm KILL`, then
  `.venv/bin/python -m wta reset-kill --mode live --log-dir logs --reason "..."` (logged as an event).
- **Flatten now:** stop the agent first (`kill "$(cat run/agent.pid)"`; the account lock refuses a
  second process), then `make flatten-live`.
