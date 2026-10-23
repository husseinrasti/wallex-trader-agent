# Submission checklist (PRD-06 §7)

**Summary**
1. This file tracks the PRD-06 §7 items. Status as of 2026-10-23 08:20 UTC, at `main` (`b8e0b44`: `v0.5-backtest` plus the committed Phase-6 work).
2. **Done:** docs, reports, the replay bundle (`make replay-deliverable`: 40/40 decisions), the tags list, the shot list and demo helper, CI green on `main`, a clean secrets scan of the history, and a fresh-clone check of the README quick start.
3. **Pending operator action:** fill in `.env` and go live, record and upload the video, make the repo public or share it with the judges, push the `live-*` and `final` tags.
4. **Pending data:** the live decision logs for the whole challenge window, the live report, the final 100 % TMN state. No live trade has happened, and `api.wallex.ir` is reachable only now and then from the dev machine.
5. Each item lists the command that checks it. Re-run them on the final commit before submitting.

Status words: **done** · **pending operator action** · **pending data** (it needs the live run) · **partly done**.

## 1. Checklist

| # | Item (PRD-06 §7) | Status | Evidence / what is left |
|---|---|---|---|
| 1 | GitHub repo public (or shared with judges), default branch `main` | **pending operator action** | `husseinrasti/wallex-trader-agent` is **private** today, default branch `main`. Making it public, or adding the judges as collaborators, is the operator's decision at the end (§3) |
| 1a | tags `live-*` | **pending data** | none yet: the operator creates `live-YYYYMMDD-HHMM` before each live start ([`deliverables/TAGS.md`](../deliverables/TAGS.md) §3). `v0.1-core` … `v0.5-backtest` exist and are pushed |
| 1b | tag `final` | **pending data** | set after the challenge ends, on the commit holding the copied live logs (`GO_LIVE_CHECKLIST.md` §8) |
| 2 | README quick start verified on a fresh machine/container (`docker compose run agent python -m wta backtest ...`) | **done** for `b8e0b44` | [`REPORTS/fresh_clone_check.md`](REPORTS/fresh_clone_check.md): a fresh clone on macOS (Python 3.12) and in the Docker image (Python 3.11) gave `1939 passed, 1 skipped` and `40/40 decisions reproduced -> PASS` (also through `docker compose run`), and README quick-start steps 1–6 printed what the README says. [`REPRODUCE.md`](REPRODUCE.md) reports that its commands were run from a fresh clone on macOS and in the Docker image on 2026-10-23. CI (`.github/workflows/ci.yml`: ruff, pytest, docker build) is green on `main` and `v0.5-backtest`. Left: re-run the quick start on the final commit |
| 3 | `deliverables/` committed | **done** for the evidence that exists | committed in `391783c`: `REPORT.md`, the replay bundle `replay/shadow_synth_4h/`, the 4-h shadow run's logs `shadow_synth_4h/` (decisions, orders, events, hourly heartbeats, journal, LLM cache), `TAGS.md`, `VIDEO.md` (link pending). The live logs are item 3a |
| 3a | decision logs covering the entire challenge window, with orders, events, health, journal, LLM cache | **pending data** | no live run yet. After the end: `python scripts/make_deliverables.py --run-dir logs --name live --bundle --replay` (copies the run, packs its replay bundle, scans for secrets; `make deliverables` runs the same for `DELIVER_RUN`), then commit |
| 3b | replay proof | **done** | `make replay-deliverable` → `40/40 decisions reproduced -> PASS`, 0 socket attempts, 0 LLM provider calls ([`REPORTS/replay_20261023.md`](REPORTS/replay_20261023.md)). It covers the 4-h shadow run (synthetic feed, real LLM). The live run's replay is pending data |
| 4 | `docs/` complete: ARCHITECTURE, STRATEGY, RISK, REPRODUCE, RUNBOOK, DECISION_LOG_FORMAT, REPORTS/ | **done**, except the live report | all six files and `REPORTS/` exist (`ls docs/{ARCHITECTURE,STRATEGY,RISK,REPRODUCE,RUNBOOK,DECISION_LOG_FORMAT}.md`). `REPORTS/` has the survey, backtests, ablation, walk-forward, shadow and replay. The live report is **pending data** |
| 5 | Final account state: 100 % TMN (dust documented) | **pending data** | needs the live run and its deadline flatten (T−2h maker, T−30min market). `CHALLENGE_END_UTC` is still **empty** in `.env`, and live refuses to start without it. Proof after the end: the `final_state` event ("100% TMN (dust only)") |
| 6 | Video uploaded, link in README; length ≈ 5 min | **pending operator action** | [`VIDEO_SHOTLIST.md`](VIDEO_SHOTLIST.md) and `scripts/demo.sh` are ready. [`deliverables/VIDEO.md`](../deliverables/VIDEO.md) holds the placeholder, and the README points to it |
| 7 | Secrets scan returns nothing; `.env` never committed | **done** for the history up to `391783c` | `.env` is gitignored and was never committed. No configured key value and no key pattern appears in any commit, and `wta logs check --secrets` is clean (§2). Re-run on the final commit |
| 8 | Team can explain every file; `STRATEGY.md` has a "how to explain in 2 minutes" box | **done** (box) / **operator** (explaining) | the box is at the top of [`STRATEGY.md`](STRATEGY.md) |

## 2. Secrets scan

Run these on the final commit, before making the repo public. None of them prints a key.

```bash
# 1. .env never committed: prints nothing
git log --all --oneline -- .env

# 2. the configured values themselves, in every commit and in the working tree (tracked + untracked):
#    prints only counts; every line must read "history 0  tree 0"
grep -E '^(WALLEX_API_KEY|OPENROUTER_API_KEY|GEMINI_API_KEY|TELEGRAM_BOT_TOKEN|WALLEX_SUB_ACCOUNT_CLIENT_ID)=.' .env |
  cut -d= -f2- | while IFS= read -r v; do
    v=${v%\"}; v=${v#\"}; [ ${#v} -ge 8 ] || continue
    printf 'history %s  tree %s\n' "$(git log --all -p | grep -cF -- "$v")" \
      "$(git grep -lF --untracked -- "$v" | wc -l | tr -d ' ')"
  done

# 3. key shapes in every commit: prints 0. OpenRouter sk-or-v1-…, Google AIza… and AQ.…, Telegram bot
#    tokens, and the Wallex "<id>|<token>" form (the PRD-06 §7 "\|[A-Za-z0-9]{30,}" term); this also
#    catches a rotated or earlier key that is no longer in .env
git log --all -p | grep -cE 'sk-or-v1-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35}|(^|[^A-Za-z0-9])AQ\.[A-Za-z0-9_-]{40,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|(^|[^A-Za-z0-9|])[0-9]{1,10}\|[A-Za-z0-9]{30,}'

# 4. logs, deliverables/ and docs/REPORTS/, including the .xz bundles: "secrets scan: clean"
.venv/bin/python -m wta logs check --secrets --dir deliverables/shadow_synth_4h
```

Command 4 and `scripts/make_deliverables.py` use the same set as command 2: every `SecretStr` value of
`.env` plus `WALLEX_SUB_ACCOUNT_CLIENT_ID` (`Settings.scan_values`), and the key shapes of command 3
(`KEY_PATTERNS` in `src/wta/logging_/checker.py`). `scripts/check_account.py` writes its report to the
gitignored `logs/api_probe.md` and shows account ids only as their last 4 characters, so the go-live
probe cannot put the sub-account id into git.

Results on 2026-10-23 07:30 UTC (history up to `85acc16`): (1) nothing; (2) `history 0` for each of the 4 configured
values; (3) `0`; (4) `secrets scan: clean`. Re-run on 2026-10-23 08:10 UTC, after `391783c` (the Phase-6 commit,
with the replay bundle) was committed: the same four results.

The literal PRD-06 §7 command, `git log -p | grep -E "API_KEY|sk-|\|[A-Za-z0-9]{30,}"`, does **not** return nothing.
It matches 89 lines today, and every one is a false positive. `API_KEY` matches variable names (for example
`WALLEX_API_KEY=` in `.env.example` and the docs), and `sk-` matches words such as `risk-first` and `PRD-02-risk-execution`.
Commands 2 and 3 above test what that command is meant to test.

## 3. Making the repo public (operator decision, at the end)

Do this only after the challenge has ended and the final commit and the `final` tag are pushed, the secrets scan (§2)
is clean, and the video link is in the README.

```bash
gh repo view husseinrasti/wallex-trader-agent --json visibility,defaultBranchRef   # PRIVATE, main today
gh repo edit husseinrasti/wallex-trader-agent --visibility public --accept-visibility-change-consequences
```

If the judges prefer it, the alternative is to keep the repo private and add them as read-only collaborators
(GitHub → Settings → Collaborators). Either way, check that the clone works without credentials:
`git clone https://github.com/husseinrasti/wallex-trader-agent.git`.

## 4. Before going live (operator; it blocks items 1a, 3a, 5)

From [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md), these items are still open today:
* `.env`: `CHALLENGE_END_UTC` is **empty** (required), `WALLEX_SUB_ACCOUNT_CLIENT_ID` is **empty** (required for the
  challenge sub-account) and `TELEGRAM_CHAT_ID` is **empty** (optional alerts).
* `api.wallex.ir` must answer consistently from the trading machine. From the dev machine it does only now and then.
  The real data recorded so far is about 20 minutes of REST order books on 2026-10-22, plus a 1-second burst.
* A shadow run on the real feed has not been done. The only 4-h shadow run used the synthetic feed.
* The Gemini fallback key is quota-limited (HTTP 429). OpenRouter is the only working LLM route, and without it the
  engine runs rules-only by design.
* `scripts/check_account.py --probe-order` (places and cancels 2 far-from-market orders) is run by the operator only.

## 5. Final pass (in order)

1. `make test` green, `git status --porcelain` empty.
2. Live logs copied into `deliverables/` (`scripts/make_deliverables.py --run-dir logs --name live --bundle --replay`), `deliverables/REPORT.md`
   reassembled with the live section (`python -m wta report --source logs --out docs/REPORTS/live_<date>`, then `make final-report`), `deliverables/TAGS.md` live rows filled in, video link in `deliverables/VIDEO.md`
   and the README.
3. `make replay` on the live run, then `make replay-deliverable`, both PASS.
4. Secrets scan (§2) clean.
5. Commit, `git tag final`, `git push origin main --tags`.
6. Visibility or judge access (§3); clone check without credentials.
