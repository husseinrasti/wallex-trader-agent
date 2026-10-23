# Read first: what this evidence is and is not (2026-10-23)

This is the summary at the top of `deliverables/REPORT.md`. Every command behind the numbers is in
[`phase5_acceptance.md`](phase5_acceptance.md).

> **Most sections below run on SYNTHETIC (generated) data, which is not evidence of performance.** Their titles say
> SYNTHETIC and each one carries its own banner. Only the survey and the "recorded real" backtests (candles 2.25 h;
> order books 0.32 h, AI off and on) use data recorded from Wallex, and that data is short.

## The data we have

* **Real Wallex data** (`data/wta.sqlite`, recorded 2026-10-22). `api.wallex.ir` is reachable from the development machine only now and then.
  - 6,131 REST order-book snapshots: one ~20-minute window (2026-10-22 14:20:05–14:40:15Z, 6,103 snapshots) plus a 1-second burst of 28 snapshots at 20:16:12Z. That is **0.32 h** of 2-s ticks, and **0.33 h** where all the books S1 needs are fresh at once.
  - **2.2 h** of 1-minute candles (12:25–14:39Z).
  - There is no 30-day candle backfill.
* **Synthetic data**, generated and deterministic (seed 42):
  - a 30-day candle DB, with clustered volume so S3's volume filter can pass;
  - a 24-hour order-book DB;
  - the 4-hour shadow run (`logs/shadow_synth_4h`), which used the synthetic feed with the real LLM.

  These runs prove that the engine, the risk rules, the AI plumbing and replay work end to end. They prove nothing about returns.

## Results in one table

| Section | Data | Result |
|---|---|---|
| Survey | real, 0.33 h aligned | **Insufficient data**. 0 S1 opportunities at any margin; largest edge 0.15 % against a 0.43 % cost. Defaults kept |
| Candles 30 d, S2+S3 | SYNTHETIC | −4.46 %, max DD 5.20 %. S2 whipsaw (57 rebalances in 8 days) used up the 72-h fee budget; kill-switch on day 9; S3 3 trades (1 win) |
| Candles 2.2 h, S2+S3 | real | +0.09 %: S2's initial USDT allocation while the dollar rose. S3 cannot warm up |
| Books 0.32 h, S1+S2, AI off / on | real | −0.055 % both. 0 trades, 3 S2 orders; the LLM changed nothing |
| Books 24 h, S1+S2, AI off / offline | SYNTHETIC | −0.56 %, 4 S1 cycles (all small losses); offline rules = rules-only exactly |
| Books 6 h, AI off / on (real LLM) | SYNTHETIC | AI +0.09 pp: it raised S1's margin to 0.18 % and skipped two marginal signals |
| Walk-forward, S1 margin | SYNTHETIC | **Default kept**: the best in-sample value, 0.25 % (+0.14 % in-sample), lost out of sample (−0.60 %), so the guard rejected it; configs unchanged. Every half now ends under the same rule (no end flatten) |
| Shadow 4 h, S1+S2, AI on | SYNTHETIC feed, real LLM (`gpt-5.4-mini`), 4.08 h | +0.05 %, max DD 0.38 %, 0 trades, 2 S2 orders (the initial 50 % USDT allocation), fees 6,240 TMN. The LLM left S1 out of all 38 plans: 20 S1 opportunity ticks (of 72,920 coin-ticks) went untraded. 11 of 49 analyst cycles gave no plan (one invalid answer, then a provider outage from 02:10Z): the last plan went stale at 02:25Z and the engine ran rules-only for the last 0.56 h, as designed |
| Live | — | **No live run yet** (live trading is started only by the operator). No live order has been placed |
| Replay of the 4-h shadow run | the shadow run's recorded inputs (SYNTHETIC), 7,292 of 7,292 ticks (= the stop event's count) over 14,699 s | **40/40 decisions reproduced**; the strict pass (every field, time stamps, fees, fills and features included) 40/40, and it is part of the verdict; 38/38 plans re-derived from the LLM cache, 0 socket attempts, 0 LLM provider calls. Final equity, fees and S1's per-coin tick counters equal too (16/16) |

## What the runs found

These are mechanisms, not performance:

1. **S2 needs hysteresis.**
   - Its rules regime flips each time the 24-h dollar return crosses ±0.80 %.
   - On the synthetic dollar that meant 57 rebalances in 8 days, the 2 % fee budget gone, and the drawdown kill-switch.
2. **S1 lock-ins depend on how long cross-book gaps last.**
   - On synthetic books the gaps vanished within one 2-s step, so most crossing leg-1 orders expired unfilled.
   - Real gap durations are not measured yet.
3. **The 10-per-hour order cap is shared by the strategies.** S1's expiring orders and leg-2 retries can block S2's rebalances.
4. **SOL dust.** If the SOL step is 0.001 (unverified), S3 on SOL leaves up to about 0.7 % of equity per trade as unsellable dust.
5. **Real spreads on the USDT books are 2–4× wider than the reference snapshot** (BTCUSDT 0.18 %, SOLUSDT 0.28 %). The synthetic runs are therefore optimistic on cost.
6. **The analyst kept S1 off for the whole 4-h shadow run** (38 of 38 plans), because the cross-book edges it saw were negative after fees. S1's own counters found 20 opportunity ticks at the margin in force (BTC 4, DOGE 4, ETH 10, LTC 2); none was traded. On synthetic books this says nothing about real opportunities.
7. **The shadow report's fee pace overstates.** All 6,240 TMN of fees came from S2's two initial-allocation fills in the first 31 minutes. Extrapolated linearly to 72 h they give the 110 % of the fee budget shown in the report.

## What is proven, what is not

* **Proven.**
  - The same `Engine` and strategies run in backtest, shadow and replay.
  - The risk rules fire as specified: kill-switch, fee budget, rate caps, USDT bounds.
  - AI plans are bounded, logged, cached and replayable without network.
  - When the LLM provider fails, the engine keeps the last plan until it is 20 minutes old, then runs rules-only (4-h shadow run).
  - Replay result: all 40 decisions of the 4-h shadow run reproduced, with zero network and zero provider calls.
    The replay's inputs (`logs/shadow_synth_4h` and the 108 MB state DB) are not in git: `/logs/` and `/data/` are ignored.
* **Not proven.**
  - That S1, S2 or S3 make money on Wallex. The real data is 20 minutes of books and 2 hours of candles.
  - That the AI adds value.
