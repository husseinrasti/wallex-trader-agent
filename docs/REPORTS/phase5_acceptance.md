# Phase 5 acceptance — survey, backtests, ablation, walk-forward, shadow, replay (PRD-05 §9)

Date: 2026-10-23, 00:00–07:10 UTC, in three sessions (the third: the review fixes of §8).

Code provenance:
* **Survey, backtests, ablation, walk-forward** (00:00–03:30Z): git `7e8f2f5` plus the then-uncommitted Phase-5 tree,
  so these reports say `git_dirty: yes`. That tree was committed as `e25b87b` at 05:20Z.
* **The 4-h shadow run** (22:53:39Z–02:58:39Z, pid 67416): the clean commit `7e8f2f5` (`git_dirty: false` in its
  start event).
* **Shadow report and replay** (05:21–05:33Z): `e25b87b` plus the report-only fixes of §4 items 7–10 (not committed
  yet). The engine code is `e25b87b`'s. Between `7e8f2f5` and `e25b87b` the engine changed only in how the heartbeat
  names an offline feed (`OFFLINE_FEEDS`); no decision path changed.
* **Review fixes and reruns** (06:00–07:10Z, §8): `e25b87b` plus every fix of §4 and §8 (not committed). Every
  backtest, the survey, the shadow report, the ablation tables and the replay were **rerun** with this code; every
  number below is from these reruns (`<scratch>/fix/runs/*.log`). The only engine change of §8 (where a restart
  continues the candle numbering) does not touch a single-process run.

`<scratch>` below means the session scratchpad (`/private/tmp/claude-501/…/scratchpad/p5` for the first session,
`…/p5b` for the second and `…/p5b/fix` for the third; the synthetic market DBs stayed in `…/p5`). Every run state DB, every synthetic market DB, the run logs (`<scratch>/runs/*.log`: the
command, its output, exit code and wall time) and every replay output went there. Nothing was written to the repo's
`logs/` or `data/`. `data/wta.sqlite` was only read, and `logs/shadow_synth_4h` + `data/state_shadow4h_shadow_synthetic.sqlite` were
only read. The recorder (pid 40393) was never touched; it is still running, but `data/wta.sqlite` has gained no row
since 2026-10-22T20:16:13Z (the API is unreachable again).

## 0. Read this first: what the data is

* **Real Wallex data in `data/wta.sqlite`** (read-only, unchanged during this phase; the newest book is still
  2026-10-22T20:16:13Z, although the recorder keeps running):
  - 6,131 REST order-book snapshots for 21 markets, 2026-10-22 14:20:05–20:16:13Z. They sit in one ~20-minute
    window (14:20:05–14:40:15Z, 6,103 snapshots, about 300 a minute) plus a 1-second burst of 28 snapshots at
    20:16:12–13Z: only **0.32 h** of 2-s ticks have a snapshot, and S1's cross-book alignment (USDTTMN + XTMN + XUSDT
    all fresh) covers **0.33 h** for BTC/ETH/XRP/DOGE and 0.32 h for SOL.
  - 1-minute candles for 11 symbols over 12:25–14:39Z only (135 bars each).
  - 300 public trades.
* **There is no 30-day candle backfill.** The 30-day candle backtest (step 2) therefore runs on a **SYNTHETIC** market DB.
* **The 4-h shadow run uses the synthetic feed** with the real LLM, so its report is SYNTHETIC too.
* **Everything long is synthetic.** Every number from a synthetic run shows mechanics only: it is **not evidence of
  performance**, and every such report carries the banner.
* **LLM spend** (OpenRouter `openai/gpt-5.4-mini`; every answer is cached in the report folder's `llm_cache/`):
  - recorded-books ai=on: 6 analyst cycles, 11 model calls, ~58 k tokens;
  - synthetic 6-h ai=on: 73 cycles (cap 150), 132 model calls, ~655 k tokens.

  The reruns of both made 0 provider calls (11/11 and 132/132 cached), in the second session and again after the
  review fixes (§8). The offline-rules run is not an LLM.
* **The 4-h shadow run** spent its own budget while it ran (49 analyst cycles, 86 model calls, 518,798 tokens, 4
  reviewer runs, the last one skipped). The report and the replay made **0 provider calls** (replay: 81 cache hits,
  0 misses). No new ai=on run was made in the second session.

## 1. Results against PRD-05 §9

| PRD-05 §9 item | Status | Where |
|---|---|---|
| Survey with S1 opportunity counts and recommendation block | **done; insufficient data** (0.33 h aligned < the 1 h minimum; 0 opportunities at every margin; configured values kept) | `survey_20261022.md` (+ `.json`, `_edges.png`) |
| 30-day candle backtest (ai off) with PNGs | **done on SYNTHETIC candles** (no real 30-day data exists). The real 2.2 h of candles were run too | `bt_candles_30d/`, `bt_candles_recorded_ai_off/` |
| ≥ 24-h book backtest (ai on/off) with PNGs | **real: all recorded hours = 0.32 h** (on/off). **SYNTHETIC: 24 h** (off / offline) **+ a 6-h sub-window** (off / on with the real LLM, to bound cost) | `bt_books_recorded_ai_{off,on}/`, `bt_books_synth24h_*/` |
| Ablation table (AI on − off) | **done** (3 pairs) | `ablation_20261023.md` and the AI section of each report |
| Walk-forward (70/30, ≤ 8 values) | **done on SYNTHETIC books**, every run under the same end rule (§8 finding 1). **Default kept**: the best in-sample value lost out of sample; no parameter change is proposed, and configs are unchanged | `walkforward_s1_margin/`, `walkforward_s1_margin_s1only/` |
| Shadow report ≥ 4 h | **done on the SYNTHETIC feed** with the real LLM: 4.08 h, S1+S2, AI on. A shadow run on the live Wallex feed is still pending API access | `shadow_20261023_synth4h/` |
| `make replay` reproduces the decision log | **done: 40/40 decisions reproduced** over the whole 4-h run (7,292 of 7,292 ticks = the stop event's count, 14,699 s); strict pass 40/40 with time stamps, fees, fills and features compared, and part of the verdict (§8); 0 socket attempts, 0 LLM provider calls; final heartbeat values 16/16 equal. No exception to document. There is no live run to replay yet | `replay_20261023.md` |
| `deliverables/REPORT.md` assembled | **done**: survey → candle backtests → book backtests → ablation → walk-forward → shadow → live ("no live run yet") → replay | `deliverables/REPORT.md` |
| `ruff` + `pytest` | **green** | §2.9 |

## 2. Commands and their summaries

Run from the repo root with `.venv/bin/python` (written `python` below).

The synthetic market DBs are deterministic:
* `--synthetic-db PATH` regenerates the same rows at any path (seed 42 = `engine.rng_seed`, start 2026-10-01T00:00Z);
* an existing file with the same marker is reused;
* a file without the marker (e.g. `data/wta.sqlite`) is refused.

### 2.1 Survey (real data)

```
python scripts/survey.py --since 72h --png --json
```
```
Market survey 20261022: window 2026-10-19T20:16:13.129Z -> 2026-10-22T20:16:13.129Z
6,131 snapshots in 21 markets; sources: rest: 6,131
  BTC: 0.33 h aligned (not in totals)   DOGE 0.33 h   ETH 0.33 h   SOL 0.32 h   XRP 0.33 h   ADA/AVAX/LINK/LTC/TRX 0.00 h
Recommendation (insufficient_data): margin_min_pct=0.0015 leg1_taker=true S1 lock-in viable: unknown
  (the block's reason: "no coin has at least 1 h of aligned cross-book data (...; best coin: 0.33 h)"; it said
   "0.00 h" before §8 finding 9)
```

What it shows:
* **Opportunities:** 0 distinct lock-in opportunities at every margin (0.05–0.40 %), for both taker and maker leg 1.
* **Largest edges seen**, against a taker cost of 0.43 %:
  - TMN-cheap: BTC +0.100 %, ETH +0.031 %;
  - USDT-cheap: DOGE +0.154 %, SOL +0.021 %.
* **Median spreads are wider than the one snapshot behind the defaults** (`configs/markets_snapshot.yaml`):

  | market | median spread | snapshot |
  |---|---:|---:|
  | USDTTMN | 0.007 % | 0.014 % |
  | BTCTMN | 0.109 % | 0.138 % |
  | BTCUSDT | 0.183 % | 0.077 % |
  | ETHTMN | 0.186 % | 0.031 % |
  | ETHUSDT | 0.147 % | 0.05 % |
  | SOLTMN | 0.297 % | 0.376 % |
  | SOLUSDT | 0.276 % | 0.08 % |

* **USDTTMN:** 1-h returns in the window ran from +0.06 % to +0.31 %. The ATR and the regime cannot be computed from 2.2 h of candles.
* The file name `survey_20261022` comes from the newest recorded book. This run replaced the earlier 48-h survey of the same name; the data is identical.

### 2.2 Candle backtests (S2 + S3, ai off; S3 enabled with `allow_rules_only` for these runs only)

```
python -m wta backtest --driver candles --synthetic-db <scratch>/synth_candles_30d.sqlite --synthetic-hours 720 \
  --ai off --strategies S2,S3 --set strategies.s3_momentum.allow_rules_only=true --out docs/REPORTS/bt_candles_30d \
  --state-db <scratch>/runs/state_bt_candles_30d.sqlite \
  --title "Candle backtest, 30 days, S2+S3 (S3 rules-only), ai=off — SYNTHETIC"
```
```
synthetic market DB ... (reused): 2026-10-01T00:00:00.000Z -> 2026-10-31T00:00:00.000Z, 0 books, 302400 candles (SYNTHETIC — not evidence of performance)
backtest candles S2,S3 ai=off (disabled): 2026-10-01T00:00:00.000Z -> 2026-10-31T00:00:00.000Z, data 720.0 h, 43200 ticks
  return -4.459%  max drawdown +5.200%  trades 3  orders 68  vetoes 79  fees 104,683 TMN        (wall 958 s)
```

What happened in this SYNTHETIC run:
* **S2 whipsawed.**
  - It placed 57 orders in 8 days, 20 of them on simulated 10-06.
  - The cause: its rules regime flips between `range` and `trend_down` every time the 24-h dollar return crosses −0.80 %. The rule has no hysteresis.
  - Those orders used up the 2 % fee budget, which is sized for 72 h: 76 FEE_BUDGET vetoes from 10-08 23:01.
* **The kill-switch fired.** On 10-09 01:39 the drawdown reached 5.02 %. The engine flattened and only monitored for the remaining 21 days.
* **S3 traded 3 times.** It had 6 breakout entry signals and 3 entries filled:
  - SOLUSDT reached its target, +0.89 %;
  - BTCUSDT and SOLUSDT were stopped out, −1.37 % and −1.57 %.

  Each entry got a server-side STOP_LIMIT. Win rate 33 %, S3 PnL −19,663 TMN.
* **91,583 TMN was left as dust (1.9 %).** The flatten could not sell it: mostly 0.001934 SOL, below SOLTMN's minimum (step 0.001 × price < 50,000 TMN) and below SOLUSDT's 1 USDT minimum.

The synthetic generator is v2 (`volume_model: clustered`, see §4.1). With v1's flat volume, S3's 1.5× volume filter could never pass.

```
python -m wta backtest --driver candles --from 2026-10-22T12:25Z --to 2026-10-22T14:40Z --ai off --strategies S2,S3 \
  --set strategies.s3_momentum.allow_rules_only=true --out docs/REPORTS/bt_candles_recorded_ai_off \
  --state-db <scratch>/runs/state_bt_candles_recorded_ai_off.sqlite --title "Candle backtest on ALL recorded real Wallex 1-min candles (...)"
```
```
backtest candles S2,S3 ai=off (disabled): 2026-10-22T12:25:00.000Z -> 2026-10-22T14:40:00.000Z, data 2.25 h, 135 ticks
  return +0.092%  max drawdown +0.091%  trades 0  orders 4  vetoes 0  fees 6,242 TMN
```

On real data:
* The 4 orders are S2's initial USDT allocation. The +0.09 % is the dollar moving up while S2 held about 50 % USDT.
* S3 cannot warm up: it needs 21 bars of 15 min, about 5 h, and there are only 2.25 h.
* The spreads are the medians of the stored books recorded up to the window's end (14:20:05–14:39:59Z, 320–591 books
  per symbol; `meta.spreads` lists them). Before §8 finding 5 they also used the books recorded after the window
  (20:16Z): ETHTMN 0.186 % → 0.190 %, SOLTMN 0.297 % → 0.310 %, the others within 0.001 pp; the result did not change.

### 2.3 Book backtests on the recorded real books (S1 + S2) and the ablation

```
python -m wta backtest --driver books --from 2026-10-22T14:20Z --to 2026-10-22T20:17Z --ai off --strategies S1,S2 \
  --out docs/REPORTS/bt_books_recorded_ai_off --state-db <scratch>/runs/state_bt_books_recorded_ai_off.sqlite --title "..."
python -m wta backtest --driver books --from 2026-10-22T14:20Z --to 2026-10-22T20:17Z --ai on --ai-max-calls 150 \
  --strategies S1,S2 --out docs/REPORTS/bt_books_recorded_ai_on --state-db <scratch>/runs/state_bt_books_recorded_ai_on.sqlite --title "..."
```
```
backtest books S1,S2 ai=off (disabled): 2026-10-22T14:20:00.000Z -> 2026-10-22T20:17:00.000Z, data 0.3239 h, 583 ticks
  return -0.055%  max drawdown +0.063%  trades 0  orders 3  vetoes 0  fees 3,123 TMN                  (wall 2 s)
backtest books S1,S2 ai=on (llm): ... data 0.3239 h, 583 ticks
  return -0.055%  max drawdown +0.063%  trades 0  orders 3  vetoes 0  fees 3,123 TMN
  analyst cycles 6 (cap 150), plans 6, model calls 11 (0 cached)                                    (wall 33 s; rerun: 11 cached, 3 s)
```

The ablation tables were filled into both reports, and into `ablation_20261023.md`, by a short script. It runs
`backtest.metrics.ablation_delta` on the two `summary.json` files and puts the `_ablation_table` markdown in place of
the "not available yet" line. The script is reproduced in the appendix (§7).

```
PYTHONPATH=. python <scratch>/ablation.py docs/REPORTS/bt_books_recorded_ai_on docs/REPORTS/bt_books_recorded_ai_off \
  docs/REPORTS/bt_books_synth24h_6h_ai_on docs/REPORTS/bt_books_synth24h_6h_ai_off \
  docs/REPORTS/bt_books_synth24h_ai_offline docs/REPORTS/bt_books_synth24h_ai_off
```

Real result: on and off are **identical**.
* 0 trades and 3 S2 orders in both.
* The LLM enabled S2 only in all 6 plans, called USDTTMN `range` (`trend_up` twice), sized 0.85–0.9, and kept the USDT target at the rules' 50 %.
* S1 never had an edge in this window (§2.1).

### 2.4 SYNTHETIC 24-h book DB (S1 + S2): ai off vs ai=offline, and a 6-h sub-window off vs on (real LLM)

```
python -c "from backtest.runner import make_synthetic_market_db as m; print(m('<scratch>/synth_books_24h.sqlite', 24, 42, step_s=2))"
  # 302,407 books, 10,080 candles, 2026-10-01T00:00Z -> 2026-10-02T00:00Z (the CLI's --synthetic-db makes the same file)
python -m wta backtest --driver books --synthetic-db <scratch>/synth_books_24h.sqlite --synthetic-hours 24 --ai off \
  --strategies S1,S2 --out docs/REPORTS/bt_books_synth24h_ai_off --state-db <scratch>/runs/state_synth24h_ai_off.sqlite --title "..."
python -m wta backtest --driver books --synthetic-db <scratch>/synth_books_24h.sqlite --synthetic-hours 24 --ai offline \
  --ai-max-calls 400 --strategies S1,S2 --set ai.cache_dir=<scratch>/runs/llm_cache_offline \
  --out docs/REPORTS/bt_books_synth24h_ai_offline --state-db <scratch>/runs/state_synth24h_ai_offline.sqlite --title "..."
python -m wta backtest --driver books --synthetic-db <scratch>/synth_books_24h.sqlite --synthetic-hours 24 \
  --from 2026-10-01T12:00Z --to 2026-10-01T18:00Z --ai off --strategies S1,S2 --out docs/REPORTS/bt_books_synth24h_6h_ai_off ...
python -m wta backtest --driver books --synthetic-db <scratch>/synth_books_24h.sqlite --synthetic-hours 24 \
  --from 2026-10-01T12:00Z --to 2026-10-01T18:00Z --ai on --ai-max-calls 150 --strategies S1,S2 \
  --out docs/REPORTS/bt_books_synth24h_6h_ai_on ...
```
```
24 h ai=off     : return -0.561%  max DD 0.897%  trades 4 (S1, all losses)  orders 43  vetoes 38  fees 36,577 TMN   (wall 138 s)
24 h ai=offline : identical (289 offline-rules cycles; NOT an LLM)                                                     (wall 141 s)
6 h  ai=off     : return +0.031%  max DD 0.380%  trades 1  orders 12  vetoes 0  fees 13,006 TMN                      (wall 35 s)
6 h  ai=on      : return +0.121%  max DD 0.380%  trades 0  orders  6  vetoes 0  fees  6,234 TMN
                  analyst cycles 73 (cap 150), model calls 132 (0 cached; reruns 132 cached)                          (wall 416 s; rerun 31 s)
                  (before §8: 24 h -0.548% / DD 0.884% / fees 35,483; 6 h off +0.037% / fees 12,510; 6 h on unchanged)
```

What these SYNTHETIC runs show:
* **24 h, vetoes.** The 38 vetoes were RATE_HOUR 34, RATE_DAY 4 and USDT_BOUNDS 2. S2 had 30 of them, all RATE_HOUR: S1's expiring leg-1 orders and its leg-2 retries used up the cap of 10 placements per hour.
* **24 h, S1.** It completed 4 cycles (all ETHUSDT → ETHTMN) and lost 0.14–0.34 % on each, 9,126 TMN in total, after 11,749 TMN of fees (before §8: 8,456 and 10,655 TMN; the difference is the taker fee on the remainders of crossing orders).
* **6 h, AI.** The LLM raised S1's margin to 0.18 % in 45 of 73 plans and skipped two marginal S1 signals (`ablation_20261023.md` §2): **+0.09 pp**, 6 fewer orders, 6,772 TMN less fees.
* **Offline rules provider.** Zero delta, by design.

### 2.5 Walk-forward, `strategies.s1_crossbook.margin_min_pct = 0.0008, 0.0012, 0.0015 (default), 0.0025` (SYNTHETIC 24 h books)

```
python -m wta backtest --driver books --synthetic-db <scratch>/synth_books_24h.sqlite --synthetic-hours 24 --ai off \
  --strategies S1,S2 --param-grid strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015,0.0025 \
  --out docs/REPORTS/walkforward_s1_margin --state-db <scratch>/runs/state_walkforward.sqlite --title "..."
```
```
walk-forward: chosen margin_min_pct=0.0025; out-of-sample return per day -2.0080% is negative while in-sample is
+0.2020%; the pre-grid default is kept; final margin_min_pct=0.0015                          (wall 578 s)
final run: return -0.571%  max DD 1.043%  trades 4  orders 42  vetoes 39  fees 30,384 TMN (no end flatten: 2,494,102 TMN
           still outside TMN, valued at the bid)
```

The split: in-sample is the first 16.80 h, out-of-sample the last 7.20 h. **Every run ends under the same rule**: no
end-of-window flatten, holdings valued at the bid (§8 finding 1). Before that fix the 16.8-h in-sample halves ran the
deadline flatten (the runner's automatic rule for windows ≥ 8 h) and the 7.2-h out-of-sample halves did not.

| candidate | IS return | IS / day | IS trades | OOS return | OOS / day | OOS trades |
|---|---:|---:|---:|---:|---:|---:|
| 0.0008 | −0.050 % | −0.071 % | 6 | −0.840 % | −2.801 % | 3 |
| 0.0012 | −0.086 % | −0.123 % | 3 | −0.840 % | −2.801 % | 3 |
| 0.0015 (default) | −0.086 % | −0.123 % | 3 | −0.900 % | −3.001 % | 3 |
| 0.0025 (best IS) | +0.141 % | +0.202 % | 2 | −0.602 % | −2.008 % | 0 |

**Decision: default kept. No change is proposed, and configs are unchanged.**
* 0.25 % is the only candidate with a positive in-sample return. It loses out of sample with no trade there, so the
  guard rejects it (an out-of-sample return per day below 0).
* The returns are dominated by S2's dollar exposure, which is the same across candidates, and by 0–6 S1 trades.
* Under the old, unequal end rule every candidate lost in-sample (0.25 %: −0.261 %); the flatten's last 2 h and its
  sell-to-TMN fees cost only the in-sample halves.

The S1-only variant is a sensitivity check without S2's dollar exposure, on a 100 % TMN start:

```
python -m wta backtest ... --strategies S1 --param-grid strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015,0.0025 \
  --out docs/REPORTS/walkforward_s1_margin_s1only --state-db <scratch>/runs/state_walkforward_s1only.sqlite --title "..."
```
```
walk-forward: chosen margin_min_pct=0.0008; out-of-sample return per day +0.0000% degrades 100% vs in-sample +0.0163%
(> 50%); the pre-grid default is kept; final margin_min_pct=0.0015                           (wall 531 s)
final run: return +0.000%, 13 S1 leg-1 orders, all EXPIRED_UNFILLED, 0 trades
```

Here 0.0008 made 1 in-sample trade (+0.011 %, valued at the bid at the half's end; −0.147 % under the old end rule,
which flattened it) and none out of sample, so it was rejected; every other candidate made no trade.
* With 100 % TMN only TMN-cheap cycles are possible.
* On this synthetic feed every TMN-cheap gap was gone by the next snapshot, so the one-step fill latency left every crossing leg-1 order unfilled.

### 2.6 Shadow report (the finished 4-h run; SYNTHETIC feed, real LLM)

The run, from its `start` and `stop` events: `logs/shadow_synth_4h`, mode `shadow`, feed `synthetic`, S1+S2, AI
`llm` (analyst `openrouter/openai/gpt-5.4-mini`, reviewer `openrouter/openai/gpt-6-luna-pro`, fallback
`gemini/gemini-4-argon`), config `481cc6dc3ce2`, commit `7e8f2f5` (clean), state DB
`data/state_shadow4h_shadow_synthetic.sqlite`, 2026-10-22T22:53:39.578Z → 2026-10-23T02:58:39.872Z: **14,700 s**,
7,292 ticks, 245 heartbeats, one process (no restart), no tick error.

```
python -m wta report --source logs/shadow_synth_4h --state-db data/state_shadow4h_shadow_synthetic.sqlite \
  --out docs/REPORTS/shadow_20261023_synth4h
```
```
SYNTHETIC — not evidence of performance (the run used the synthetic feed)
report shadow 2026-10-22T22:53:39.578Z -> 2026-10-23T02:58:39.872Z: return +0.051%, max drawdown +0.383%, trades 0,
  orders 2 (equity from state_db)                                                                    (wall 1.7 s)
```

What the report shows (equity, drawdown, veto and regime PNGs; every one carries the SYNTHETIC watermark):
* **Portfolio.** 5,000,000 → 5,002,526 TMN (+0.05 %), max drawdown 0.38 % (longest 3 h 12 m), 0 closed trades, 0
  vetoes. The equity moves with the synthetic dollar, because S2 holds about 50 % USDT.
* **Orders.** 2, both S2 rebalances on USDTTMN, each BUY 4.68 USDT, cut by `MAX_ORDER_PCT` (25 % of equity) from the
  9.37 and 4.70 USDT the intents asked for:
  - 22:53:39Z, decided by the rules before the first plan arrived; repriced twice, filled 22:54:21Z at 266,824;
  - 23:24:23Z, under a plan (confidence 0.93); filled 23:24:29Z at 266,604.
* **Fees.** 6,240 TMN = 6.24 % of the 100,000 TMN budget. The report's "110 % per 72 h at this pace" extrapolates
  these two one-off allocation fills linearly; no fee was paid after 23:24Z.
* **S1 opportunity ticks** (new section, the engine's own counters): 7,292 ticks evaluated per coin, 20 opportunity
  ticks at the margin in force (ETH 10, BTC 4, DOGE 4, LTC 2; best excess +0.138 %, BTC), 0 S1 entries. The analyst
  left S1 out of `enabled_strategies` in **38 of 38** plans ("cross-book edges negative after fees").
* **Plans and regimes.** 38 plans, all `openai/gpt-5.4-mini`, 0 from the cache, 1 with a clamped field, average
  `size_multiplier` 0.28 (0.35 in 30 plans, 0 in 8), `s1_margin_min_pct` 0.18 % in all 38, USDT target 50 % (55 %
  in 3). USDTTMN was `range` in 35 of 38 plans and `trend_up` in 3.
* **Analyst availability** (new section and limitation): 49 cycles, 11 without a plan. One invalid answer at
  01:29:58Z (recovered 01:34:59Z), then the provider was unreachable from 02:10:18Z (`ConnectError` on OpenRouter and
  the Gemini fallback) to the end. The plan of 02:05:15Z went stale at 02:25:19Z (`ai.plan_stale_s` 1,200 s), and
  the strategies ran **rules-only for the last 0.56 h**, as designed: no exception, no crash.
* **Post-trade reviewer.** 4 reviews in `logs/shadow_synth_4h/journal.md`, 0 config changes; the fourth was skipped
  (provider unreachable).

### 2.7 Replay (the whole 4-h run)

```
python scripts/replay_check.py --log-dir logs/shadow_synth_4h --state-db data/state_shadow4h_shadow_synthetic.sqlite \
  --out <scratch>/fix/replay_final2 --no-index
```
```
recorded: mode shadow, feed synthetic — SYNTHETIC, not evidence of performance; segment 1 of 1
window:   2026-10-22T22:53:39.782Z -> 2026-10-23T02:58:38.810Z (4.08 h, 7292 ticks); plans 38/38 re-derived identically;
          config 481cc6dc3ce2 (= recorded)
network:  0 socket attempts, 0 LLM provider calls; LLM cache 81 hits / 0 misses
exchange: recorded 160, repeat 22; divergences 0
strict:   40/40 decisions identical (every field, time stamps included)
end:      16/16 final-heartbeat values equal (equity, fees, plan freshness, S1 tick counters; informational)
result:   40/40 decisions reproduced -> PASS
report:   docs/REPORTS/replay_20261023.md                                                     (exit 0, wall 115 s)
```

* **Coverage: the whole run.** 7,292 of 7,292 ticks, from the first (22:53:39.782Z) to the last (02:58:38.810Z):
  14,699 s of the 14,700-s run. The segment ends at its own stop event (02:58:39.872Z), whose tick count (7,292)
  equals the ticks in the state DB; nothing was left out (0 decision or order lines after the last tick, one
  segment). A shorter tick table, a missing stop event or records after the last tick now fail the replay (§8).
* **Decisions.** 40 = 38 plans + 2 S2 orders (their 2 outcome lines are joined to them). All PRD-05 fields plus the
  plan's content are equal, and so is the strict pass: every field, including every time stamp and duration (line
  times, `order.response.created_ts/updated_ts`, `outcome.done_ts`, plan `latency_ms`), fees, fills and features,
  40/40. Since §8 the strict pass and the plan re-derivation are part of the verdict.
* **Zero network.** Every `AF_INET`/`AF_INET6` connect and every `getaddrinfo` was refused and counted: 0. The LLM
  providers were counting sentinels: 0 calls. All 38 plans were re-derived from `logs/shadow_synth_4h/llm_cache` (81
  cached answers) and swapped in at the recorded tick.
* **Beyond the decisions** (new, informational): the replay's final heartbeat equals the recorded one in 16 of 16
  values: equity 5,002,180.5056 TMN, fees 6,239.79 TMN, realized PnL 0, no open order or position, plan stale, and
  S1's per-coin counters for all 10 coins (7,292 ticks evaluated; the opportunity ticks, best excess and last
  opportunity time to the microsecond). Those counters change on every tick, so they also check the ticks on which
  no decision was made. The recorded `day_start` equity at 00:00:01.618Z (4,985,813.4458 TMN) is reproduced as well.
* **The 11 analyst cycles without a plan** wrote no decision record, so there is nothing to re-derive for them. The
  plan in force ages on the tick clock in the replay as in the recorded engine, so the rules-only stretch after
  02:25:19Z is reproduced (no decision in it, in either run, and the S1 counters above agree). The report lists this
  under Limitations.
* **Provenance.** Recorded commit `7e8f2f5`, replayed on `e25b87b` (+ report-only changes), config hash equal.

The first session's partial replay (`replay_20261022.md`: 15/15, but only over the first 1.03 h while the run was
still being written) is **superseded and deleted**. Replay reports are now named after the UTC day of the last
replayed tick (§4 item 7), so `make replay` on this run rewrites `replay_20261023.md` instead of creating a second
file.

`make replay` with no arguments picks the newest live run under `logs/`, else the newest shadow run: today that is
`logs/shadow_synth_4h`. Its inputs (`logs/`, `data/`) are not in git, so the replay can be rerun only on a machine
that holds them (§6).

### 2.8 Consolidation, index and final report

Superseded outputs were deleted (§5). Then:

```
PYTHONPATH=. python <scratch>/assemble.py      # update_index('docs/REPORTS') + assemble_final_report(...), §7
```

The index (`docs/REPORTS/README.md`) is regenerated by `backtest.report.update_index`. Every relative link in `docs/`,
`deliverables/` and `README.md` was checked to resolve after the consolidation.

### 2.9 Lint and tests

```
.venv/bin/ruff check src tests scripts backtest && .venv/bin/python -m pytest
```
```
All checks passed!
1913 passed, 1 skipped in 96.84s (0:01:36)                         (2026-10-23 06:58Z, after the review fixes of §8)
```

No test touches the network: the replay tests refuse and count every socket connect and `getaddrinfo`, and the new
report tests use hand-written log files in `tmp_path`.

## 3. Findings

These are mechanisms found by the runs. The synthetic ones are **not** performance evidence.

1. **S2 has no hysteresis** (synthetic 30 d). The rules regime flips `range`/`trend_down` around the ±0.80 % 24-h
   threshold, and each flip moves the target by 20 pp (50 % ↔ 30 %). The result:
   * 57 rebalances in 8 days, 2–3 times what section 9 of `docs/STRATEGY.md` expects;
   * the fee budget used up and the kill-switch fired.

   Suggested fix (not made, needs real data to tune): enter a trend at |24h| > 0.8 % but leave it only below 0.4 %,
   or require a minimum dwell time per regime.
2. **S1 lock-ins die on latency** (synthetic). Entry edges of 0.6–0.8 % were usually gone by the next 2-s snapshot:
   * 5 of 6 leg-1 orders in the 6-h run expired unfilled;
   * all 13 in the S1-only run expired unfilled;
   * the 4 cycles that completed in 24 h all lost money after fees.

   This shows what the pessimistic fill model means for S1. Real gaps may last longer or shorter, and that is not
   measured yet (0.33 h of real books, no gap above cost).
3. **The trade-rate cap couples the strategies** (synthetic 24 h). S1's expiring orders and leg-2 retries use up the
   10-per-hour cap, after which S2's rebalances are vetoed (RATE_HOUR, 30×).
4. **S3 leaves SOL dust** (synthetic 30 d). The coin-paid BUY fee leaves a remainder below SOL's 0.001 step, worth
   up to about 0.7 % of equity per trade at 5 M TMN. If the step is right (`verified: false`), S3 on SOL leaves unsellable
   dust: 66,330 TMN after 2 trades.
5. **Real spreads are wider than the defaults assume** (real survey). BTCUSDT 0.18 % vs 0.077 %, ETHTMN 0.19 % vs
   0.031 %, SOLUSDT 0.28 % vs 0.08 %. Synthetic books and synthetic candle runs use the narrower reference spreads, so
   they are optimistic on spread.
6. **The fee budget is a 72-h budget.** Any backtest longer than about 3 days hits it. The 30-day run shows it binding
   on day 8, and that is the intended behaviour for the challenge.
7. **AI.** The recorded window shows no effect. The synthetic 6-h window shows the margin-raising mechanism working.
   The offline provider changes nothing.
8. **The analyst keeps S1 off** (4-h shadow, synthetic feed). All 38 plans left S1 out of `enabled_strategies`,
   citing negative cross-book edges after fees; S1's counters saw 20 opportunity ticks at the plans' 0.18 % margin
   and none was traded. Whether that is right on Wallex is unknown: the gaps are synthetic.
9. **LLM outage handling works as specified** (4-h shadow). With both providers unreachable from 02:10Z, the engine
   kept the last plan until it was 1,200 s old, then ran rules-only for the remaining 0.56 h: no exception, no tick
   error, every failure visible as `ai_degraded`, `plan_stale` and `llm_ok=false`.
10. **A fee pace extrapolated from a short run misleads.** The shadow report's "110 % of the 72-h budget at this pace"
    is S2's one-off initial allocation (2 fills in the first 31 minutes) stretched linearly; read it with the order
    times.
11. **An unequal end rule biased the walk-forward** (synthetic 24 h, §8 finding 1). With the deadline flatten in the
    in-sample halves only, every candidate lost in-sample. Under one rule (no flatten anywhere) 0.25 % is positive
    in-sample (+0.141 %) and still loses out of sample (−0.602 %), so the decision (default kept) is unchanged.
12. **The book fill model under-charged S1** (§8 finding 2): the remainder of a crossing leg paid the maker fee. With
    the taker fee on every fill of a crossing order, S1's 4 synthetic 24-h cycles cost 11,749 TMN of fees instead of
    10,655 (+10 %), and the run returns −0.561 % instead of −0.548 %.

## 4. Code changes made in this phase (small fixes, with tests)

1. **`backtest/runner.py`, synthetic candle volume (generator v2).** `make_synthetic_market_db(..., volume_model="clustered")` is the new default for candle-only DBs:
   * volume is multiplied by `exp(x − σ²/2)`, where `x` is a per-symbol log-activity Ornstein-Uhlenbeck process (τ 30 min, σ 0.5) on its own seeded RNG;
   * prices are unchanged;
   * the marker carries `volume_model` and `version: 2`.

   v1 (`"uniform"`) could never pass S3's 1.5× volume filter. Books DBs keep the feed's volumes (`"feed"`).
2. **`backtest/runner.py`, fees per trade and per run.**
   * `add_trade_fees` fills each closed trade's `fees_tmn` from its position's `meta.fees_tmn` in the state DB. Outcome lines carry no fees, so every per-trade and per-strategy fee column used to read 0.
   * `state_fees_tmn` gives `wta report` the engine's fee total (`portfolio_state.fees_paid_tmn_total`). Shadow reports used to say "Fees paid 0 (no fee data recorded)".
3. **`backtest/metrics.py`.** `trade_stats` returns `fees_tmn=None` (shown as n/a) when no trade carries fees, instead of a false 0.
4. **`backtest/report.py`, limitation texts.**
   * A synthetic S1 window now says "generated (synthetic)" instead of "recorded".
   * "so this run was not flattened" appears only when no flatten ran.
   * The Sharpe note gives the hourly-return count and hours with one decimal: it used to say "0 h" for 0.32 h.
5. **`backtest/runner.py`, automatic limitations.** The report now says when:
   * the drawdown kill-switch fired (time and reason);
   * the fee budget ran out (FEE_BUDGET veto count and first time);
   * S3 ran with `allow_rules_only`.
6. **`backtest/walkforward.py`, the overfitting guard as `docs/STRATEGY.md` §10 states it.**
   * IS and OOS are compared **per day of data**. Before, the 70 % total was compared with the 30 % total, which rejects even a perfectly steady strategy.
   * A grid value with a non-positive in-sample return is **never adopted**. Before, a less-negative value could be adopted.
   * `walkforward.md` shows the return per day and the data hours.

Tests:
* `tests/test_phase5_fixes.py` (new): the clustered and uniform volume models, prices unchanged, the marker, trade fees from positions, the fee total from kv, and fee columns showing n/a.
* `tests/test_walkforward.py`: the guard cases for non-positive IS were updated, a per-day test was added, and the "degrades" scenario was rescaled to per day.
* `tests/test_report.py`: the pinned limitation text was updated.

Second session (shadow report, full replay, consolidation), not committed yet:

7. **`src/wta/replay/runner.py`, report date.** `replay_<YYYYMMDD>.md` is named after the UTC day of the **last**
   replayed tick (`report_day`), like the shadow report. It used the first tick's day, so the 4-h run that crossed
   midnight was filed as `replay_20261022.md` while its shadow report is dated 2026-10-23, and a rerun of `make replay`
   would have recreated the deleted partial file.
   Test: `tests/test_replay.py::test_the_report_is_filed_under_the_day_of_the_last_tick`.
8. **`src/wta/replay/runner.py`, end state and analyst gaps (informational, not part of the verdict).**
   * `end_state_check` compares the recorded final heartbeat with the replay's: equity, fees, realized PnL, open
     orders, positions, plan freshness and S1's per-coin tick counters. They show that the ticks without a decision
     were replayed identically too. `scripts/replay_check.py` prints the `end:` line.
   * `analyst_gaps_note` lists the recorded analyst cycles that gave no plan (heartbeat stats, `ai_degraded` /
     `ai_recovered` / `plan_stale` events) and why the replay has nothing to re-derive for them.

   Tests: `tests/test_replay.py::test_the_end_state_and_the_analyst_gaps_are_reported`; the `end:` line in
   `test_replay_check_script_exit_codes`.
9. **`backtest/runner.py` (`wta report`), S1 opportunity ticks and analyst availability.** The shadow/live report had
   no S1 section, and it said nothing when the LLM failed. It now adds:
   * "S1 opportunity ticks": per coin, ticks evaluated, opportunity ticks, best excess, last opportunity and the last
     why-no-trade, from the engine's counters in the heartbeats (summed over engine processes), plus the number of
     plans that left S1 out;
   * "Analyst availability": cycles, plans, cycles without a plan, model calls, tokens, reviews, failure streaks and
     the rules-only stretch after a stale plan, also as a limitation line.

   Both also go into `summary.json` (`s1_opportunities`, `ai_availability`). Tests: `tests/test_phase5_reports.py`
   (new): counters summed over a restart, the sections' place and text, nothing added for an AI-off run.
10. **`backtest/report.py`, final report.** `FinalSection` takes an optional `status_if_missing` / `text_if_missing`,
    so the live section says "no live run yet (live trading is started only by the operator)" instead of "not
    available yet". The header's order line now names walk-forward. Test:
    `tests/test_phase5_reports.py::test_a_section_may_say_why_it_is_missing`.

## 5. Consolidation: what was deleted

`docs/REPORTS/` now holds one canonical output per PRD-05 artefact. The builder's smoke runs from earlier that night and
the partial replay were **deleted** in the second session (no doc links to them any more):

| Deleted | Canonical output | Why it was superseded |
|---|---|---|
| `bt_books_recorded_20261022/` and `_ai_on/` | `bt_books_recorded_ai_off/` and `_ai_on/` | Window 14:00–20:30 instead of exactly the recorded 14:20–20:17, same data and results; written before the fee/limitation fixes |
| `bt_candles_recorded_20261022/` | `bt_candles_recorded_ai_off/` | Same data and result; now with S3 `allow_rules_only`, as in the 30-day run |
| `synthetic_bt_books_6h/` | `bt_books_synth24h_6h_ai_{off,on}/` | Different synthetic DB |
| `synthetic_bt_candles_30d/` | `bt_candles_30d/` | Generator v1 (flat volume) and no `allow_rules_only`, so S3 could never trade |
| `replay_20261022.md` | `replay_20261023.md` | 15/15 over the first 1.03 h only, while the 4-h run was still being written |

What remains, per artefact:

| PRD-05 artefact | Output | Data |
|---|---|---|
| Survey | `survey_20261022.md` (+ `.json`, `_edges.png`) | real |
| Candle backtest | `bt_candles_30d/` (30 d) and `bt_candles_recorded_ai_off/` (all 2.25 h of real candles) | SYNTHETIC / real |
| Book backtests | `bt_books_recorded_ai_off/`, `bt_books_recorded_ai_on/` (0.32 h); `bt_books_synth24h_ai_off/`, `bt_books_synth24h_ai_offline/` (24 h); `bt_books_synth24h_6h_ai_off/`, `bt_books_synth24h_6h_ai_on/` (6 h) | real / SYNTHETIC |
| Ablation | `ablation_20261023.md` (3 pairs) | real + SYNTHETIC |
| Walk-forward | `walkforward_s1_margin/` (S1+S2) and `walkforward_s1_margin_s1only/` (S1 alone, sensitivity check) | SYNTHETIC |
| Shadow ≥ 4 h | `shadow_20261023_synth4h/` | SYNTHETIC feed, real LLM |
| Replay | `replay_20261023.md` | the shadow run's recorded inputs |
| Final report | `deliverables/REPORT.md`; its first section is `final_report_summary.md` | — |

The two non-listed companions are kept on purpose: `bt_candles_recorded_ai_off/` is the only candle backtest on real
data, and the S1-only walk-forward is the evidence for finding 2.

## 6. Open items

* **Real data.** `data/wta.sqlite` still ends at 2026-10-22T20:16:13Z: the recorder runs, but the API has not been
  reachable since.
  - The survey verdict and every real-data backtest need many more recorded hours, at least 1 h aligned and ideally ≥ 24 h, plus a real 30-day candle backfill (`python -m wta backfill --resolution 1 --days 30` when the API is reachable).
  - Then rerun §2.1–§2.5 on real data. The commands stay the same, minus `--synthetic-db`.
* **Shadow on the live feed.** The ≥ 4-h shadow requirement is met on the synthetic feed only. PRD-05 §5 also wants
  ≥ 4 h of shadow on Wallex data before the first live run (`docs/GO_LIVE_CHECKLIST.md`).
* **No live run has happened** (live trading is started only by the operator). The live section of
  `deliverables/REPORT.md` says so; there is no live decision log to replay yet.
* **Replay inputs are not in git.** `/logs/` and `/data/` are ignored, so `logs/shadow_synth_4h` (5.5 MB with its LLM
  cache) and `data/state_shadow4h_shadow_synthetic.sqlite` (108 MB) exist only on this machine. For judges to rerun
  `make replay` on this run they must be shipped (e.g. as a release asset), or the replay must be rerun on the live run.
* **Commit and tag.** The second and third sessions' code changes (§4 items 7–10, §8), the regenerated reports and the
  docs are not committed. The report provenance will read `git_dirty` until they are; tag `v0.5-backtest` after that
  commit.
* **S2 hysteresis** (finding 1) and the **SOL step / S3 dust** check (finding 4) need decisions by the strategy owner.

## 7. Appendix: the helper scripts

<details><summary><code>&lt;scratch&gt;/ablation.py</code> — fills the ablation tables (deterministic, no network)</summary>

```python
import json, sys
from pathlib import Path
from backtest.metrics import ablation_delta
from backtest.report import NOT_AVAILABLE, _ablation_table
from backtest.runner import update_summary

PLACEHOLDER = f"_Ablation {NOT_AVAILABLE} for this run._"

def pair(on_dir: Path, off_dir: Path) -> str:
    on = json.loads((on_dir / "summary.json").read_text()); off = json.loads((off_dir / "summary.json").read_text())
    won, woff = on["meta"], off["meta"]
    assert (won["window_from"], won["window_to"], won["market_db"]) == (woff["window_from"], woff["window_to"], woff["market_db"])
    abl = ablation_delta(on["metrics"], off["metrics"])
    abl["note"] = (f"ai={won.get('ai')} ({won.get('ai_source')}) run `{on_dir.name}` vs ai={woff.get('ai')} run `{off_dir.name}`: "
                   f"same window {won.get('window_from')} → {won.get('window_to')}, same market DB `{won.get('market_db')}`, "
                   "same seed and config apart from ai.enabled")
    table = _ablation_table(abl)
    for d in (on_dir, off_dir):
        rep = d / "report.md"; text = rep.read_text()
        if PLACEHOLDER in text:
            rep.write_text(text.replace(PLACEHOLDER, table, 1))
        update_summary(d / "summary.json", "ablation", abl)
    return table

args = sys.argv[1:]
for i in range(0, len(args), 2):
    print(pair(Path(args[i]), Path(args[i + 1])))
```
</details>

<details><summary><code>&lt;scratch&gt;/assemble.py</code> — the index and <code>deliverables/REPORT.md</code> (run from the repo root with <code>PYTHONPATH=.</code>)</summary>

```python
from backtest.report import FinalSection as F
from backtest.report import assemble_final_report, update_index

LIVE_TEXT = (
    "**No live run yet** (live trading is started only by the operator). No live order has been placed. "
    "A live run needs `WTA_LIVE=1` in the process environment and a clean, committed tree (`docs/DECISIONS.md`); "
    "the steps are in `docs/GO_LIVE_CHECKLIST.md`. Its report will be "
    "`python -m wta report --source logs --out docs/REPORTS/live_<date>`, which this section then embeds."
)
SECTIONS = [
    F("summary", "Read first: what this evidence is and is not", ("final_report_summary.md",)),
    F("survey", "Market survey (real recorded books)", ("survey_20261022.md",)),
    F("bt_candles_30d", "Backtest: 1-min candles, 30 days, S2+S3 (SYNTHETIC)", ("bt_candles_30d/report.md",)),
    F("bt_candles_real", "Backtest: 1-min candles, all recorded real candles (2.25 h), S2+S3",
      ("bt_candles_recorded_ai_off/report.md",)),
    F("bt_books_real_off", "Backtest: all recorded real order books (0.32 h), S1+S2, ai=off",
      ("bt_books_recorded_ai_off/report.md",)),
    F("bt_books_real_on", "Backtest: all recorded real order books (0.32 h), S1+S2, ai=on (real LLM)",
      ("bt_books_recorded_ai_on/report.md",)),
    F("bt_books_24h", "Backtest: 24 h order books, S1+S2, ai=off (SYNTHETIC)", ("bt_books_synth24h_ai_off/report.md",)),
    F("bt_books_6h_on", "Backtest: 6 h order books, S1+S2, ai=on with the real LLM (SYNTHETIC)",
      ("bt_books_synth24h_6h_ai_on/report.md",)),
    F("ablation", "AI ablation (ai=on − ai=off)", ("ablation_20261023.md",)),
    F("wf", "Walk-forward: S1 margin grid, S1+S2 (SYNTHETIC)", ("walkforward_s1_margin/walkforward.md",)),
    F("wf_s1only", "Walk-forward: S1 margin grid, S1 only (SYNTHETIC)", ("walkforward_s1_margin_s1only/walkforward.md",)),
    F("shadow", "Shadow (paper) run: 4 h, S1+S2, AI on (SYNTHETIC feed)", ("shadow_20261023_synth4h/report.md",)),
    F("live", "Live challenge run", ("live_*/report.md", "live_*.md"), status_if_missing="no live run yet",
      text_if_missing=LIVE_TEXT),
    F("replay", "Replay proof (the 4-h shadow run)", ("replay_20261023.md",)),
]

update_index("docs/REPORTS")
assemble_final_report("docs/REPORTS", "deliverables/REPORT.md", SECTIONS,
                      title="Wallex Trader Agent — final report (Phase 5: backtests, shadow, replay)")
```
</details>

## 8. Review findings and fixes (third session, 06:00–07:10Z)

A code review of the Phase-5 tree raised 16 findings. Each was checked first (a throwaway run or a query), fixed with a
regression test, and every report whose numbers could change was rerun (§2 shows the new numbers; the old ones are
named where they changed). None was a false positive. Test file names are under `tests/`.

| # | Finding (file) | Verified | Fix | Regression test |
|---|---|---|---|---|
| 1 | Walk-forward halves scored under different end rules (`backtest/walkforward.py`) | `meta.flatten_at_end` was True for every 16.8-h IS half and False for every 7.2-h OOS half | Every IS, OOS and final run gets the same `flatten_at_end` (False unless given); `walkforward.md` and `walkforward.json` state the rule | `test_walkforward.py::test_every_walkforward_run_ends_under_the_same_rule` |
| 2 | Books driver: the remainder of a crossing LIMIT paid the maker fee (`src/wta/exchange/backtest.py`) | A crossing BUY's second fill came out `is_maker=True` at 0.10 % | `BacktestExchange._match_limit`: an order crossing at placement (STOP_LIMIT: at trigger) is a taker on every fill, at the level prices | `test_fills.py::test_books_next_snapshot_only_and_80_percent_cap` (now expects taker, fee 0.13 %), `::test_books_resting_limit_is_maker_at_its_limit` |
| 3 | Books driver: the 80 % level cap reset on every snapshot (`src/wta/exchange/paper.py` via `backtest.py`) | An unchanged 0.01 level filled 0.008 per snapshot | What our fills took from a level is carried to the next snapshots while the price level is still shown (`_carried_taken`); only added quantity is offered again | `test_fills.py::test_books_a_repeated_level_is_not_offered_again` |
| 4 | Books driver: an order resting before a recording gap filled on the first snapshot after it (`backtest/runner.py`) | A maker BUY placed before a 1-h gap filled 3,600 s after placement | `BacktestExchange(max_gap_s=engine.stale_data_s)`: an order live in the book whose symbol had no snapshot for > 30 s is never filled afterwards; the OrderManager re-prices, crosses or gives it up on the post-gap books. (An extra tick at the gap start was tried first: it made the engine pause on the missing books and veto the first post-gap rebalance, treating a recording gap as an outage, so the exchange-side rule was kept.) `meta.orders_stopped_by_gaps` + a limitation line | `test_fills.py::test_books_an_order_open_across_a_recording_gap_never_fills`, `test_backtest_runner.py::test_books_driver_an_order_resting_before_a_recording_gap_is_not_filled_after_it` |
| 5 | Candle spreads calibrated on books after the window (`backtest/runner.py`) | `observed_spreads` had no time filter | Books stamped at or before the window end only; `meta.spreads` records the source, book count and sample span; reference spread when there is no sample | `test_backtest_runner.py::test_candle_spreads_come_only_from_books_up_to_the_window_end` |
| 6 | Auto-limitations contradicted the candle runs (`backtest/report.py`) | Candle reports listed "unlimited depth" and "80 % of the best-level quantity" together | Driver-specific fill-model sentences; the candle spread sentence is built from `meta.spreads` | `test_report.py::test_limitations_auto_filled_and_custom_first` |
| 7 | The decision-log link showed one day of a multi-day run (`backtest/report.py`) | `bt_candles_30d/decisions.jsonl` → the 10-09 file only | The report links every daily `decisions-*.jsonl` file and says `decisions.jsonl` is the newest day only | `test_report.py::test_decision_log_links_cover_every_daily_file` |
| 8 | "Two ~10-minute windows" (`final_report_summary.md` and 4 other docs) | `data/wta.sqlite`: one block 14:20:05–14:40:15Z (6,103 snapshots) + 28 snapshots at 20:16:12–13Z | Corrected everywhere; `deliverables/REPORT.md` re-assembled | — (documentation) |
| 9 | The survey's recommendation said "0.00 h aligned" (`src/wta/research/survey.py`) | The message used only coins with ≥ 1 h | The message names the best coin's coverage (0.33 h); the ≥ 1 h inclusion rule is unchanged; `survey_20261022.*` regenerated | `test_survey.py::test_no_synthetic_warning_for_recorded_sources` |
| 10 | "Report date" was the data's end, e.g. 2026-10-31 (`backtest/runner.py`) | `generated_at = iso(run.to_ts)` | `meta.data_end` (shown as "Data end (UTC)") and a separate `report_date` from `--report-date` or the commit date of the code (`report_date_source` says which); the index orders by it | `test_backtest_runner.py::test_report_date_is_never_the_data_end`, `test_report.py::test_report_date_is_labelled_and_never_taken_for_the_data_end` |
| 11 | The strict pass and plan re-derivation did not affect the verdict (`src/wta/replay/runner.py`) | Mutated `fee_paid`/`filled_qty`/`features.equity_tmn` still gave PASS | `ReplayRun.ok` needs the strict pass, every plan re-derived identically and ≥ 1 decision; a strict failure and a differently re-derived plan are errors; `wta replay --no-strict` became `--lenient` (it never changed what is compared) | `test_replay.py::test_the_strict_pass_is_part_of_the_verdict`, `::test_a_mutated_cached_plan_is_detected` |
| 12 | A stopped run with a short tick table passed on a prefix (`src/wta/replay/runner.py`) | 70 % of the ticks deleted, stop event kept: PASS | `partial_reasons`: the stop event's tick count must equal the ticks in the state DB, and nothing may be written after the last tick; a run without a stop event fails too, unless `--allow-partial` (a labelled PARTIAL replay) | `test_replay.py::test_a_state_db_with_fewer_ticks_than_the_stop_event_counts_fails`, `::test_a_run_still_being_written_fails_unless_a_partial_replay_is_allowed` |
| 13 | A segment did not end at its own stop event (`src/wta/replay/runner.py`) | Run A replayed with run B's ticks from the shared state DB | The segment ends at min(next start, its stop + 1 ms) for ticks, balances, fills and events. The test also found that a restarted engine numbered its first candle arrivals from 1 again (`engine_candles.seq`), so a replay of the earlier run read the later run's candles: `Engine` now continues the numbering from the store at once, and the replay's candle feed skips bars that open after the segment's last tick | `test_replay.py::test_a_segment_ends_at_its_own_stop_event_when_runs_share_the_state_db` |
| 14 | Time stamps were never compared (`src/wta/replay/compare.py`) | Shifting 15 timing values by 1 h still gave PASS; in the real 4-h replay every time stamp was equal | `IGNORE_KEYS`/`IGNORE_SUFFIX` removed: only `mode`, `git_commit`, `config_hash`, `plan_snapshot.cached` are ignored, by exact path | `test_replay.py::test_time_stamps_are_compared`, `test_replay_compare.py` (two tests updated) |
| 15 | A replay-only crash was only a note (`src/wta/replay/runner.py`) | Two injected crashes in `_scheduled`: PASS | The replayed `tick_error` ticks must equal the recorded ones (both engines log the same first crashes of a window) | `test_replay.py::test_a_crash_only_in_the_replay_fails_it` |
| 16 | A window without decisions passed as 0/0 (`src/wta/replay/runner.py`) | AI-off, S1/S2-off recording: PASS | "nothing to compare" is an error; `replay_check.py` exits 2, `wta replay` exits 2 | `test_replay.py::test_a_window_without_decisions_is_nothing_to_compare`, `test_cli_backtest.py::test_replay_with_nothing_to_compare_exits_2` |

**What changed in the published numbers** (all SYNTHETIC except where noted):

* Book backtests with S1 trades: the taker fee on crossing remainders (finding 2) and the level carry (3).
  24 h: −0.548 % → **−0.561 %**, max DD 0.884 % → 0.897 %, fees 35,483 → **36,577 TMN** (offline-rules run identical).
  6 h ai=off: +0.037 % → **+0.031 %**, fees 12,510 → 13,006 TMN; ai=on unchanged; ablation +0.08 → **+0.09 pp**.
* Walk-forward (finding 1): the table in §2.5; the decision (default kept) is unchanged. The final S1+S2 run is no
  longer flattened: −0.571 %, max DD 1.043 %, 42 orders.
* Unchanged: the real-data backtests (books ai=off/on, candles; findings 4 and 5 did not apply to any order there),
  the 30-day candle run, the shadow report's numbers and the replay (40/40, now with time stamps and a complete
  segment).
* Every report: the header's "Report date" is now 2026-10-23 (commit date of `e25b87b` + uncommitted changes; the
  30-day report said 2026-10-31) with a separate "Data end (UTC)" row; the limitations name the driver's own fill
  model; the decision link covers every day.

Reruns: the commands of §2.2–§2.7 with `--state-db <scratch>/fix/runs/...`; logs (command, output, exit code, wall
time) in `<scratch>/fix/runs/*.log`. The ai=on reruns were served entirely from their caches (11/11 and 132/132 calls,
0 provider calls); no new ai=on run was made.
