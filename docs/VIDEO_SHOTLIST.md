# Demo video: shot list and narration (about 5 minutes)

**Summary**
1. One take of about 5 minutes, screen plus voice. It shows outputs, not code, in seven scenes that follow PRD-06 §5: problem, architecture, dashboard, risk in action, evidence, reproducibility, wrap-up.
2. `bash scripts/demo.sh` runs every scene in order. Each step prints its command, waits for Enter, then runs it. Scene 0 runs before recording. It starts three background jobs in a scratch directory: the dashboard run, the kill-switch drill and the replay.
3. Everything on screen runs offline on the **synthetic feed**, and every screen says so. The analyst is the real OpenRouter LLM (`gpt-5.4-mini`) when a key is set, and the demo never runs live mode.
4. The narration states the facts as they are. `api.wallex.ir` was reachable only now and then. The real data is about 20 minutes of order books. No live trade has happened. The 4-hour shadow run replays 40/40 decisions with 0 network calls.
5. The operator records and uploads the video, then puts the link into `deliverables/VIDEO.md` and the README (§6).

Related: [`scripts/demo.sh`](../scripts/demo.sh) (the helper), [`deliverables/VIDEO.md`](../deliverables/VIDEO.md)
(the link), [`ARCHITECTURE.md`](ARCHITECTURE.md) (the diagram), [`REPORTS/`](REPORTS/README.md) (the evidence).

---

## 1. The timeline at a glance

| t | Scene | Screen | `demo.sh` steps |
|---|---|---|---|
| before | 0 Preparation | not recorded: preflight, three background jobs | `--scene 0` (0.1–0.5) |
| 0:00–0:40 | 1 Problem & approach | slide: capital, 3 days, TMN valuation, fees, thesis, three layers | 1.1 |
| 0:40–1:40 | 2 Architecture | the diagram from `ARCHITECTURE.md`; `python -m wta --help` | 2.1, 2.2 |
| 1:40–3:00 | 3 Live dashboard | `wta tui` on the running synthetic shadow; one frame of the 4-h run; its last plan; one `decisions.jsonl` line, field by field | 3.1–3.4 |
| 3:00–3:40 | 4 Risk in action | a FEE_BUDGET veto; the kill-switch drill (shock, kill, flatten); the KILLED veto; `wta reset-kill` | 4.1–4.4 |
| 3:40–4:30 | 5 Evidence | results table; survey chart; backtest equity and ablation tables; shadow PNGs; walk-forward table; "no live curve yet" | 5.1–5.6 |
| 4:30–4:50 | 6 Reproducibility | `make replay-deliverable`: "40/40 decisions reproduced"; git tag ↔ decision-log commit; final balance | 6.1–6.3 |
| 4:50–5:00 | 7 Wrap-up | closing card; stop the background jobs | 7.1, 7.2 |

The narration below is about 740 words, which is 5 minutes at about 150 words a minute. If a take runs long, shorten
what you say, not what you show.

## 2. Before recording (scene 0, about 6 minutes ahead)

1. **Tree and tag.** Record from a clean, committed tree, or the dashboard header shows `dirty` next to the commit:

   ```bash
   git status --porcelain          # must print nothing
   git describe --tags             # e.g. v0.5-backtest, or a later tag
   source .venv/bin/activate       # so that "python" in the shown commands is the venv's
   ```
2. **Terminal.** Make it full screen with at least 200 columns and 60 rows (`tput cols; tput lines`). The dashboard
   needs 200 columns. If the window is narrower, set `DEMO_WIDTH=200`. Use a dark theme without transparency.
3. **Scene 0.** Run `bash scripts/demo.sh --scene 0` and press Enter at each step. It prints the preflight first:
   Python, commit, which keys are set (never their values), and whether the 4-h run and the replay bundle are present.
   Then it starts three background jobs and writes their output under the work dir (`$DEMO`, default `$TMPDIR/wta-demo`):

   | job | what it is | time |
   |---|---|---|
   | `dash` | `wta run --mode shadow --feed synthetic` with S1+S2. The analyst runs every 60 s, not 300 s, so a new plan arrives during the take | 1,500 s |
   | `drill` | the kill-switch drill. Strategies and AI are off. It injects one BTCUSDT entry at 5 s, applies a −8 % synthetic shock at 60 s, and injects one USDT buy at 120 s that must be vetoed | 200 s |
   | `replay` | `make replay-deliverable`: the committed bundle of the 4-h run. Without the bundle it is `scripts/replay_check.py` on `logs/shadow_synth_4h` | about 2 min |
4. **Wait** until `bash scripts/demo.sh --status` shows `drill` and `replay` as `finished, exit 0` (about 3.5 minutes).
   Then start the recorder and run the recorded scenes:

   ```bash
   bash scripts/demo.sh --scene 1 --scene 2 --scene 3 --scene 4 --scene 5 --scene 6 --scene 7
   ```
5. **Rehearse** once, before step 3 and in a work dir of its own, without waiting for Enter:
   `bash scripts/demo.sh --yes --no-open --workdir /tmp/wta-rehearsal` (about 3.5 minutes, all scenes, background
   jobs stopped at the end). With `--yes` the dashboard prints one frame instead of running live. Add `--ai off` to
   make no network call at all.

What the helper never does: it never runs live mode (`WTA_LIVE=0` is exported, and any command with `--mode live`,
`--i-understand-live` or `flatten-live` is refused). Its runs write only into the work dir, never into `logs/`,
`data/`, `configs/runtime/` or `docs/`. (Reading the 4-h run's state DB, which is in WAL mode, only touches SQLite's
`-shm` index file next to it.) It never sends Telegram messages, and it never prints a key.

## 3. Scenes, commands and narration

The commands are what `demo.sh` prints, from the repo root. `$DEMO` is the work dir. Each "Say" block is read aloud
as written.

### Scene 1 — Problem & approach (0:00–0:40)

**Screen.** Step 1.1: `python $DEMO/demo_helper.py slide problem`. One panel shows the capital, the 3 days, the TMN
valuation, the fee table, the thesis, S1/S2/S3/FLATTEN and the three layers.

**Say.**
> This is the Wallex Trader Agent, an autonomous spot-trading agent for the Wallex algorithmic trading challenge.
> The account holds five million toman, the challenge runs for three days, and the score is the net account value in
> toman at the end. Coins still held at the end do not count, and a negative return scores zero on the profit item.
> Fees are high: a round trip costs about half a percent on toman pairs and a quarter of a percent on USDT pairs.
> So our thesis is: don't lose, trade regularly, and be auditable.

### Scene 2 — Architecture (0:40–1:40)

**Screen.**
* Step 2.1: `python $DEMO/demo_helper.py diagram docs/ARCHITECTURE.md docs/00-OVERVIEW.md`. It prints the diagram
  block of `ARCHITECTURE.md` (slow loop, fast loop, RiskManager, exchange modes).
* Step 2.2: `python -m wta --help`. One CLI: `run` (shadow or live), `backtest`, `replay`, `tui`, `status`.

**Say.**
> The agent has three layers. The fast loop runs every two seconds and contains no AI: market data, features, three
> rule-based strategies, the risk manager, orders, and one log record per decision. The slow loop is the AI. Every
> five minutes a Regime Analyst, an LLM reached through OpenRouter, reads the market through tools and returns a plan:
> a regime per market, which strategies may run, a size multiplier and a USDT target. The plan is validated and
> clamped to fixed bounds: size can only shrink, S1's margin can only rise, and the USDT target stays between 25 and
> 70 percent. The plan never places an order. The third layer is the guardian: twenty-three risk checks on every
> entry, exit and rebalance, a drawdown kill-switch, and a flatten schedule that converts everything to toman before
> the deadline. AI plans, rules trade, risk guards. One engine runs in four modes: backtest, shadow with paper fills,
> live, and replay.

### Scene 3 — Live dashboard (1:40–3:00)

**Screen and commands.**
* Step 3.1 (about 30 s; press Ctrl-C to leave, which returns to the demo and leaves the run going):

  ```bash
  python -m wta tui --log-dir $DEMO/dash/logs --mode shadow --feed synthetic \
    --set data.sqlite_path=$DEMO/dash/market.sqlite --set "data.state_sqlite_path=$DEMO/dash/state_{mode}.sqlite" --width 200
  ```
  Point at the SYNTHETIC banner, Equity, Markets with the S1 edge columns, Current plan, and Last 10 decisions. The
  first S2 order (`BUY 4.68 USDTTMN`, ✓) appears at the start of the run.
* Step 3.2: one frame of the recorded 4-h run (synthetic feed, real LLM):

  ```bash
  python -m wta tui --log-dir logs/shadow_synth_4h --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite' --width 200 --once
  ```
  The run stopped hours ago, so the header reads `STALE`. Read the plan notes in the Current plan panel.
* Step 3.3: `python $DEMO/demo_helper.py plan logs/shadow_synth_4h`. The last plan: model, prompt hash, envelope
  (enabled S2, size ×0.35, USDT target 50 %, S1 margin ≥ 0.18 %), regimes with evidence, notes.
* Step 3.4: `python $DEMO/demo_helper.py decision logs/shadow_synth_4h first-order`. Line 1 of
  `decisions-20261022.jsonl`, field by field, joined with its outcome line (line 3).

On a fresh clone `logs/` is absent, and `demo.sh` uses the run's copy in `deliverables/shadow_synth_4h` for 3.2–3.4.
That copy has no state DB, so the frame takes equity from the heartbeats.

**Say.**
> This is the dashboard. It runs in a separate, read-only process. The banner says synthetic feed: this demo uses
> generated market data, so nothing on this screen is evidence of performance. On the left: equity in toman, drawdown
> against the five-percent kill level, the fee budget and the USDT share. In the middle: every book we watch and the
> cross-book edges S1 looks for. Below them: the analyst's current plan, the open orders, and the last decisions with
> their risk result. Here S2 makes its first allocation and buys USDT toward its target.
>
> This is the four-hour shadow run, with the real LLM, GPT five point four mini. Its last plan reads: "Cross-book
> edges stay negative after fees for the main names, so S1 remains disabled. Crypto is still weak versus USDT,
> especially SOL and BTC, so I keep size reduced to limit churn and preserve capital."
>
> Every decision is one line of decisions.jsonl. This one names the commit and the config hash. The intent says what
> S2 wanted and why, in plain words. Then come all twenty-three risk checks with their numbers: MAX_ORDER_PCT cut the
> order from 9.37 to 4.68 USDT. Then the order with our client id, and the outcome, joined by the decision id: filled
> after two re-prices.

### Scene 4 — Risk in action (3:00–3:40)

**Screen and commands.**
* Step 4.1: `python $DEMO/demo_helper.py decision docs/REPORTS/bt_candles_30d veto:FEE_BUDGET`. The first FEE_BUDGET
  veto of the 30-day candle backtest (SYNTHETIC): `decisions-20261008.jsonl` line 24. One check fails: "fees paid 97,642
  TMN + expected 2,394 TMN = 100,036 TMN > budget 100,000 TMN". The order line says nothing was sent.
* Step 4.2: the drill. Scene 0 ran it in the background. If it has not run, this step runs it in the foreground, which
  takes 200 s:

  ```bash
  python -m wta run --mode shadow --feed synthetic --seconds 200 --inject-intents $DEMO/drill/inject.jsonl \
    --paper-balances '{"TMN": 2000000, "USDT": 12}' --synthetic-shock-at 60 --synthetic-shock-pct -0.08 \
    --log-dir $DEMO/drill/logs --set data.sqlite_path=$DEMO/drill/market.sqlite \
    --set "data.state_sqlite_path=$DEMO/drill/state_{mode}.sqlite" --set ai.enabled=false \
    --set strategies.s1_crossbook.enabled=false --set strategies.s2_usdtalloc.enabled=false \
    --set alerts.telegram_enabled=false --runtime-dir $DEMO/drill/runtime
  ```
  Its events follow: `kill drawdown_kill: drawdown 6.23% >= 5.00% from peak 5,201,576 TMN`, then `flatten ... started`,
  then `flatten kill flatten complete: only TMN and dust left` about 76 s later, then `stop`.
* Step 4.3: the drill's veto (`veto:KILLED`). An injected USDT buy at 120 s fails `KILLED` ("only flatten/exit/stop
  SELLs allowed, got BUY rebalance"), `DAILY_LOSS`, `DRAWDOWN_KILL` and `SELF_CROSS`. Below it is the dashboard header
  with `KILLED`.
* Step 4.4:

  ```bash
  python -m wta reset-kill --reason 'demo drill: synthetic -8% shock reviewed; the account is flat in TMN' \
    --log-dir $DEMO/drill/logs --mode shadow --feed synthetic \
    --set data.sqlite_path=$DEMO/drill/market.sqlite --set "data.state_sqlite_path=$DEMO/drill/state_{mode}.sqlite"
  ```
  It prints the previous kill reason and the new peak, and adds a `reset_kill` event. If you run scene 4 again after a
  reset, `demo.sh` keeps the old drill as `drill.prev` and runs a fresh one (200 s).

**Say.**
> Risk in action. First, a veto from the thirty-day backtest on synthetic candles. S2 wanted to rebalance, but the
> fees would have gone over the two-percent budget, so FEE_BUDGET said no, and nothing reached the exchange. Next, a
> kill-switch drill in shadow: the synthetic market drops eight percent. Drawdown reaches 6.2 percent, the kill-switch
> fires at five, and everything is sold back to toman. An order sent after the kill is vetoed with the code KILLED.
> Only a person can clear a kill: reset-kill, with a written reason.

### Scene 5 — Evidence (3:40–4:30)

**Screen and commands.** PNGs open in the image viewer (macOS `open`; `--no-open` only prints their paths).
* 5.1 `python $DEMO/demo_helper.py md docs/REPORTS/final_report_summary.md 'Results in one table'`
* 5.2 `open docs/REPORTS/survey_20261022_edges.png` + the survey's "Recommendation" block (insufficient data,
  defaults kept)
* 5.3 `open docs/REPORTS/bt_candles_30d/equity.png` (SYNTHETIC) + ablation tables 1 (real books, 0.32 h) and 2
  (SYNTHETIC books, 6 h, real LLM) of `docs/REPORTS/ablation_20261023.md`
* 5.4 `open docs/REPORTS/shadow_20261023_synth4h/equity.png docs/REPORTS/shadow_20261023_synth4h/drawdown.png` + the
  report's Metrics table
* 5.5 the walk-forward table of `docs/REPORTS/walkforward_s1_margin/walkforward.md`
  ("Decision: REJECTED ... the pre-grid default is kept")
* 5.6 `ls docs/REPORTS | grep -i '^live'`, which prints "(none: no live run yet ...)"

**Say.**
> Now the evidence, and what it is worth. api.wallex.ir was reachable only now and then from our machine, so the real
> data is about twenty minutes of order books. On it the survey found no S1 opportunity after fees, so the defaults
> stayed. Every longer run is synthetic. The thirty-day backtest shows S2 overtrading until the fee budget and the
> kill-switch stopped it, so S2 needs hysteresis. In the AI ablation the LLM changed nothing on real data, and added
> nine hundredths of a point on synthetic data by skipping two marginal trades. The walk-forward guard rejected the
> best in-sample margin because it lost out of sample. The four-hour shadow run ended at plus 0.05 percent with no
> trades. There is no live curve: live trading has not started.

### Scene 6 — Reproducibility (4:30–4:50)

**Screen and commands.**
* Step 6.1: `make replay-deliverable`, which runs `python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h`.
  Scene 0 started it, so the step shows its output. It ends with
  `network: 0 socket attempts, 0 LLM provider calls`, `strict: 40/40 decisions identical` and
  `result: 40/40 decisions reproduced -> PASS`. Without the bundle, the fallback is
  `python scripts/replay_check.py --log-dir logs/shadow_synth_4h --state-db data/state_shadow4h_shadow_synthetic.sqlite --out $DEMO/replay/out --reports-dir '' --no-index`.
* Step 6.2: the distinct `git_commit, config_hash, mode` of the run's decision lines
  (`7e8f2f5…  481cc6dc3ce2  shadow`), then `git describe --tags 7e8f2f5` → `v0.4-ai-1-g7e8f2f5`,
  `git tag --contains 7e8f2f5` → `v0.5-backtest`, `git tag -l`, and `git tag -l "live-*"` → none yet.
* Step 6.3: `python $DEMO/demo_helper.py final docs/REPORTS/shadow_20261023_synth4h/summary.json`. The 4-h shadow run
  went from 5,000,000 to 5,002,526 TMN (+0.05 %). There is no live final balance yet.

**Say.**
> Reproducibility. make replay-deliverable unpacks the committed bundle of the four-hour run and replays it with
> every network call refused: forty of forty decisions reproduced, zero network calls. Every record carries its
> commit, 7e8f2f5, and that commit is contained in tag v0.5-backtest. Each live start will get its own live tag.

### Scene 7 — Wrap-up (4:50–5:00)

**Screen.** Step 7.1: `python $DEMO/demo_helper.py slide wrap` (built / proven / not proven / live / what to read).
Step 7.2: `bash scripts/demo.sh --stop` stops the background jobs (after the recording is stopped is fine too).

**Say.**
> There is no final toman balance yet: it will come from the live run, which only the operator starts. The code, the
> logs and the reports are in the repository. Thank you.

**If you record after the live run,** say the live result in place of "no live curve" (scene 5) and "no final
balance" (scenes 6–7). Show the live report under `docs/REPORTS/` and the `final_state` event, and name the `live-*`
tag from `deliverables/TAGS.md`.

## 4. Rules for the narration

* Say "synthetic" whenever the screen says SYNTHETIC. Never call a synthetic result a profit or evidence.
* Do not claim live trading, live fills or a final balance before they exist.
* Never show `.env`, an API key or a Telegram token. The preflight prints only "set" or "not set". Do not open the
  Wallex web UI with the account visible.
* If the analyst fails during the take (no key, a provider outage, the Gemini fallback's quota error 429), the
  plan panel says rules-only. Say so: "the analyst is unavailable, so the rules run alone, as designed". Step 3.2
  still shows a real LLM plan from the 4-h run.
* If the replay is still running at scene 6, `demo.sh` waits up to 300 s (Ctrl-C stops waiting). The committed proof
  is `docs/REPORTS/replay_20261023.md` and `deliverables/replay/shadow_synth_4h/README.md`.

## 5. Recording tips

* **Tool.** Use OBS Studio or QuickTime Player (macOS: Cmd-Shift-5, Options, then pick the microphone). No editing
  is needed: record one take, and start a new take instead of cutting.
* **1080p.** In OBS, set Settings → Video → Output (scaled) resolution to 1920×1080 at 30 fps. Record mp4, or mkv
  and then File → Remux Recordings. Use a "Display Capture" or "macOS Screen Capture" source and a mic source. QuickTime records
  at the display's native size, so on a Retina screen pick a 1920×1080 scaled display mode first, or use OBS.
* **Terminal.** Full screen, at least 200 × 60 (`tput cols; tput lines`). Use the largest font that still fits that,
  often 13–15 pt Menlo on a 1920×1080 display. Use a dark theme with high contrast, no transparency and no blur. The
  dashboard (`wta tui`, `rich`) needs about 200 columns: `--width 200` fits all panels, and `demo.sh` passes the
  terminal's width, or `DEMO_WIDTH`.
* **Screen.** Hide the dock and menu bar, turn on Do Not Disturb, and quit chat and mail apps. Close every window
  that shows a key or a token. Close the Preview windows of scene 5 with Cmd-W before scene 6.
* **Sound.** Use a headset or a close mic in a quiet room. Check the level first: peaks around −12 dB in OBS.
* **Size.** A 5-minute 1080p file is often above 50 MB. Do not commit it: `video/` is gitignored (see
  `DECISIONS.md`). Upload it and commit only the link.

## 6. After recording (operator)

1. Check the length (about 5 minutes) and that every SYNTHETIC banner is readable.
2. Upload as YouTube **unlisted** or as a Google Drive file shared with "anyone with the link".
3. Put the link, the length, the date and the commit in [`deliverables/VIDEO.md`](../deliverables/VIDEO.md), and the
   link in the README section "Demo video". Commit both.
4. Tick the video item of [`SUBMISSION_CHECKLIST.md`](SUBMISSION_CHECKLIST.md).

## 7. Where the spoken numbers come from

| Said in the video | Value | Source |
|---|---|---|
| capital, duration, TMN valuation | 5,000,000 TMN; 3 days; net TMN value at the end | `00-OVERVIEW.md` §2 |
| fees | TMN pairs 0.25 % / 0.30 %, USDT pairs 0.10 % / 0.13 % | `STRATEGY.md` §1 |
| 23 risk checks | every intent (entries, exits, rebalances, flatten) gets 23; a protective stop gets its own single check | `RISK.md` §1, §4; any `risk.checks` |
| real data | ~20 min of REST order books on 2026-10-22 (14:20–14:40Z) plus a 1-s burst at 20:16Z; 2.2 h of 1-min candles | `REPORTS/final_report_summary.md` |
| survey | 0 S1 opportunities at any margin; largest edge 0.15 % against a 0.43 % cost; defaults kept | `REPORTS/survey_20261022.md` |
| 30-day backtest | SYNTHETIC; −4.46 %; 57 S2 rebalances in 8 days; fee budget used up; kill on day 9 | `REPORTS/final_report_summary.md` |
| AI ablation | real books: no change; SYNTHETIC 6 h: +0.09 pp, two marginal S1 signals skipped | `REPORTS/ablation_20261023.md` |
| walk-forward | 0.25 % chosen in-sample, rejected out of sample (−0.60 %); default 0.15 % kept | `REPORTS/walkforward_s1_margin/walkforward.md` |
| 4-h shadow run | synthetic feed, real `gpt-5.4-mini`; +0.05 %; max DD 0.38 %; 0 trades; 2 S2 orders; 38 plans | `REPORTS/shadow_20261023_synth4h/report.md` |
| plan notes read aloud | the last plan (2026-10-23T02:05:15Z) | `logs/shadow_synth_4h/decisions-20261023.jsonl` line 24 |
| 9.37 → 4.68 USDT | MAX_ORDER_PCT, the first S2 order of the 4-h run | `decisions-20261022.jsonl` line 1 |
| drill | drawdown 6.23 % ≥ 5 % → kill → flatten complete about 76 s later | `demo.sh` runs on 2026-10-23 (deterministic synthetic feed) |
| replay | 40/40 reproduced; strict 40/40; 0 socket attempts; 0 LLM provider calls | `REPORTS/replay_20261023.md`; `deliverables/replay/shadow_synth_4h/README.md` |
| commit ↔ tag | `7e8f2f5` = `v0.4-ai-1-g7e8f2f5`, contained in `v0.5-backtest` | `git describe`, `git tag --contains`; `deliverables/TAGS.md` |
| live | none yet; only the operator starts it | `deliverables/TAGS.md`, `GO_LIVE_CHECKLIST.md` |
