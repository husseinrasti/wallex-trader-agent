# Git tags

**Summary**
1. Five tags exist, one per engineering phase: `v0.1-core` … `v0.5-backtest`. All are lightweight tags on `main` and all are pushed to `origin`.
2. **There is no `live-*` tag yet, because no live run has happened.** Only the operator starts live trading, after "go live". Each live start gets a `live-YYYYMMDD-HHMM` tag (UTC) on the exact commit it runs, created and pushed before the process starts.
3. Every decision record carries `git_commit` and `config_hash`, and every `start` event carries `git_dirty` and `git_tag`. Together they tie a log line to a tag (§4).
4. The evidence runs did not come from a tagged commit. The 4-h shadow run ran on the clean commit `7e8f2f5` (one commit after `v0.4-ai`). The Phase-5 backtests ran on `e25b87b` plus fixes that were later committed as `v0.5-backtest`. Both commits are contained in `v0.5-backtest`.
5. A `final` tag is set after the challenge ends, on the commit that holds the copied live logs (`GO_LIVE_CHECKLIST.md` §8).

Generated from `git for-each-ref` and `git log` on 2026-10-23. To regenerate, run the commands in §5.

## 1. Tags

Tag dates are commit dates in UTC. These are lightweight tags, so git keeps no separate tag time.

| tag | commit | commit date (UTC) | phase | accepted at this tag |
|---|---|---|---|---|
| `v0.1-core` | `6fa1412141149d82090b1e47746c1bb4de4a4614` | 2026-10-22 10:44:02 | 1 (PRD-01) | core engine: config and `config_hash`, Wallex REST + WebSocket client, recorder, SQLite store, decision/health logs, `wta logs check`, `scripts/check_account.py` |
| `v0.2-risk-exec` | `6094408dd4de6c26ee1681544550dfcfa71f2ccd` | 2026-10-22 12:59:05 | 2 (PRD-02) | RiskManager, OrderManager, positions, reconcile, server-side stops, kill-switch and pause, flatten phases, restart safety, `wta reset-kill`, `scripts/flatten_now.py` |
| `v0.3-strategies` | `15a3485bbd261ad3c43c8f670e9fe2eb89b49b0a` | 2026-10-22 18:25:14 | 3 (PRD-03) | features, S1 CrossBook, S2 USDTAlloc and S3 RegimeMomentum (off by default) wired into the engine through the router; half-size live overlay `configs/live_halfsize.yaml`; `docs/GO_LIVE_CHECKLIST.md` |
| `v0.4-ai` | `2c313664bf18b3d767fec2bc7869ed79b040fc87` | 2026-10-22 22:52:06 | 4 (PRD-04) | Regime Analyst (OpenRouter, Gemini fallback), Post-trade Reviewer with bounded config changes, LLM cache, MCP client (read-only), Telegram alerts, `wta status` / `tui` / `plan` |
| `v0.5-backtest` | `85acc16b6778f259bcdfb8fda51c44bd80abf944` | 2026-10-23 07:02:51 | 5 (PRD-05) | backtest runner (books and candles; AI off, on or offline), survey, ablation, walk-forward guard, shadow report, replay runner and the replay proof; 1913 tests |

Each tag marks the commit where that phase's acceptance criteria passed (`docs/PROGRESS.md`). Work ran in parallel
worktrees, so a tag can already contain modules of a later phase that were not yet wired into `wta run`. For
example, `v0.2-risk-exec` already has `strategies/` and `ai/` files.

Commits between `v0.4-ai` and `v0.5-backtest`: `7e8f2f5` (a fix: `get_candle_stats` during warm-up),
`e25b87b` (backtest runner, walk-forward guard, replay runner) and `85acc16` (Phase-5 runs, reports and review
fixes).

## 2. Which commit produced the evidence

| run | `git_commit` in its records | clean tree? | config hash | relation to the tags |
|---|---|---|---|---|
| 4-h shadow run `logs/shadow_synth_4h` (synthetic feed, real LLM), 2026-10-22T22:53:39Z → 2026-10-23T02:58:39Z | `7e8f2f5af1c55c3d744c6c8de22a454040b072f9` | yes (`git_dirty: false`) | `481cc6dc3ce2` | `git describe --tags` → `v0.4-ai-1-g7e8f2f5`; contained in `v0.5-backtest` |
| its replay (`make replay-deliverable`) | replays `7e8f2f5`'s records | — | `481cc6dc3ce2` (equal) | 40/40 decisions reproduced, 0 network calls |
| Phase-5 backtests in `docs/REPORTS/bt_*`, `walkforward_*` | `e25b87b5248b1dddc40353ec1816acab378685d8` | no (`git_dirty: true`): the review fixes were not committed yet | one per run (in each `summary.json`) | `v0.4-ai-2-ge25b87b`; the fixes were committed as `85acc16` = `v0.5-backtest` (`docs/REPORTS/phase5_acceptance.md`, "Code provenance") |
| live | none | — | — | no live run yet |

## 3. Live tags: `live-YYYYMMDD-HHMM`

The convention (`docs/00-OVERVIEW.md` §8, `GO_LIVE_CHECKLIST.md` §5):

1. **Before** each live start, on a clean committed tree with `make test` green:

   ```bash
   TAG=live-$(date -u +%Y%m%d-%H%M); git tag "$TAG" && git push origin "$TAG"
   WTA_LIVE=1 .venv/bin/python -m wta run --mode live --i-understand-live --overlay configs/live_halfsize.yaml
   ```
   The time in the name is UTC. Live mode refuses a dirty tree (untracked files count; `--allow-dirty` is for
   development only). The `start` event records `git_commit`, `git_dirty: false` and `git_tag`.
2. **Every deliberate live start gets a new tag.** That includes the planned switch from half size to full size after
   6 h (`GO_LIVE_CHECKLIST.md` §6) and any restart on a different commit or config. Automatic restarts on the same
   commit (`scripts/run_forever.sh`, compose `restart: always`) keep the tag. They show up as `start` events with
   `reason: restart`.
3. **The time range** of a tag runs from its `start` event to the `stop` event of the same process segment, as
   found in `deliverables/events-*.jsonl`.
4. **Who:** the operator only. The assistant never creates a `live-*` tag and never starts live mode.

Live tags so far:

| tag | commit | start (UTC) | stop (UTC) | config hash | overlay | notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | none yet: live is started only by the operator after "go live" |

Before the first live tag, the operator fills in `.env` (`GO_LIVE_CHECKLIST.md` §0):
* `CHALLENGE_END_UTC`: live refuses to start without it, and the deadline flatten depends on it.
* `WALLEX_SUB_ACCOUNT_CLIENT_ID`: without it, orders do not go to the challenge sub-account.
* `TELEGRAM_CHAT_ID`: optional, for alerts.

All three are empty today. `CHALLENGE_START_UTC` (optional, informational) is empty too. Both challenge times are part
of `config_hash` (`docs/DECISION_LOG_FORMAT.md` §8). Reproducing a live run's hash therefore needs the same values,
so record them in the live tag's row below.

## 4. Checking a decision line against a tag

```bash
# 1. distinct (git_commit, config_hash, mode) in the delivered decision files
cat deliverables/<run>/decisions-*.jsonl | python -c 'import sys, json
rows = {(r["git_commit"], r["config_hash"], r["mode"]) for r in map(json.loads, sys.stdin) if r["kind"] != "outcome"}
print(*sorted(rows), sep="\n")'
# 2. a live commit must be exactly the commit of a live tag
git tag --points-at <git_commit>                # prints live-YYYYMMDD-HHMM
git rev-parse 'live-YYYYMMDD-HHMM^{commit}'     # must print <git_commit>
# 3. the run's start event: clean tree, same tag
grep -h '"kind":"start"' deliverables/<run>/events-*.jsonl   # data.git_dirty false, data.git_tag = the tag
# 4. the config hash at that tag (same --set flags and ${VAR} values as the run)
git checkout live-YYYYMMDD-HHMM && python -m wta config hash
```

For the 4-h shadow run, step 1 on `deliverables/shadow_synth_4h` prints one row:
`7e8f2f5af1c55c3d744c6c8de22a454040b072f9  481cc6dc3ce2  shadow`. `git tag --points-at 7e8f2f5` prints nothing, as
expected for an untagged shadow run. Details are in `docs/DECISION_LOG_FORMAT.md` §8.

## 5. Regenerating this list

```bash
git for-each-ref --sort=creatordate \
  --format='%(refname:short) %(objecttype) %(objectname) %(creatordate:iso8601-strict) %(subject)' refs/tags
for t in $(git tag -l); do echo "$t $(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd "$t")"; done
git ls-remote --tags origin        # what is pushed
git tag -l 'live-*'                # live tags (none yet)
```
