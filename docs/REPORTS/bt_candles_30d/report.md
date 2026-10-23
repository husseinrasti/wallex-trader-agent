# Candle backtest, 30 days, S2+S3 (S3 rules-only), ai=off — SYNTHETIC

> **SYNTHETIC DATA — not evidence of performance.** This run used generated market data (offline SyntheticFeed), not data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.

| run | value |
| --- | --- |
| Config hash | `3ac4c2c08f98` |
| Git commit | `e25b87b5248b1dddc40353ec1816acab378685d8` |
| Mode | backtest |
| Driver | candles |
| Requested window (UTC) | 2026-10-01T00:00:00Z → 2026-10-31T00:00:00Z (720.0 h) |
| Data present (UTC) | 2026-10-01T00:00:00Z → 2026-10-31T00:00:00Z (720.0 h) |
| Data source | SYNTHETIC generated market DB synth_candles_30d.sqlite (seed 42, hours 720.0, step_s 10.0, volume_model clustered) (SYNTHETIC) |
| AI | off |
| Strategies | S2, S3 |
| Data end (UTC) | 2026-10-31T00:00:00Z |
| Report date | 2026-10-23 (commit date of e25b87b5248b + uncommitted changes) |
| ai_source | disabled |
| candles_ingested | 302,400 |
| coverage_hours | 720.00 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | yes |
| git_dirty | yes |
| initial_equity_tmn | 5,000,000 |
| market_db | synth_candles_30d.sqlite |
| seed | 42 |
| state_db | state_bt_candles_30d.sqlite |
| step_s | 1m 00s |
| synthetic_note | synthetic market data: an offline exercise of the engine, never evidence of performance |
| tick_crashes | 0 |
| ticks | 43,200 |
| warmup_candles | 0 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 4,777,057 |
| PnL (TMN) | -222,943 |
| Return | -4.46% |
| Max drawdown (below running peak) | 5.20% |
| Longest drawdown | 29d 17h |
| Sharpe (annualized, from 1-h returns) | -7.81 |
| Sortino (annualized, from 1-h returns) | -9.85 |
| Closed trades / cycles | 3 |
| Winning trades | 1 |
| Losing trades | 2 |
| Win rate | 33.33% |
| Profit factor | 0.31 |
| Average PnL per trade | -0.68% |
| Average hold | 1h 31m |
| Gross profit (TMN) | 8,717 |
| Gross loss (TMN) | 28,380 |
| Closed trades PnL (TMN) | -19,663 |
| Orders placed | 68 |
| Risk vetoes | 79 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 104,683 |
| Fees / initial equity | 2.09% |
| Turnover (TMN) | n/a |
| Days of data | 30.00 |
| Equity points | 43,200 |
| Hourly returns (Sharpe/Sortino sample) | 720 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 30.0 days of data (720 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 4,685,474 |
| USDT held (TMN value) | 1,122 |
| Coins held (TMN value) | 90,461 |
| Equity (TMN) | 4,777,057 |
| Realized PnL (TMN cash − initial equity) | -314,526 |
| Unrealized at the end (equity − TMN cash) | 91,583 |
| Unrealized share of equity | 1.92% |
| Flat (all TMN) | no |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (43,200 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

Showing all 3 trades; full list (all columns): [trades.csv](trades.csv).

| entry_ts | closed_ts | strategy | symbol | pnl_tmn | pnl_pct | fees_tmn | hold_s | exit_reason | position_id | decision_id |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-03T12:32:00Z | 2026-10-03T13:46:00Z | S3 | SOLUSDT | 8,717 | 0.89% | 1,943 | 1h 14m | target | p_S3_SOLUSDT_1791030720000_1 | d_1791030660000_S3_SOLUSDT_1 |
| 2026-10-03T13:47:00Z | 2026-10-03T14:07:00Z | S3 | BTCUSDT | -13,359 | -1.37% | 2,207 | 20m 00s | stop | p_S3_BTCUSDT_1791035220000_2 | d_1791035160000_S3_BTCUSDT_2 |
| 2026-10-08T22:32:00Z | 2026-10-09T01:32:00Z | S3 | SOLUSDT | -15,021 | -1.57% | 2,140 | 3h 00m | stop | p_S3_SOLUSDT_1791498720000_3 | d_1791498660000_S3_SOLUSDT_1 |

Exit reasons: stop 2, target 1.

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FLATTEN | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 1 | 0 |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 57 | 76 |
| S3 | 3 | 1 | 2 | 33.33% | -19,663 | 0.31 | -0.68% | 1h 31m | 6,290 | 10 | 3 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-01 | 5,000,000 | 4,951,181 | -0.98% | 0 | 0 | 0 | n/a | 3 | 0 |
| 2026-10-02 | 4,951,181 | 4,966,948 | 0.32% | 0 | 0 | 0 | n/a | 4 | 0 |
| 2026-10-03 | 4,966,948 | 4,953,129 | -0.28% | 2 | 1 | -4,641 | 4,151 | 10 | 0 |
| 2026-10-04 | 4,953,129 | 5,000,033 | 0.95% | 0 | 0 | 0 | n/a | 2 | 0 |
| 2026-10-05 | 5,000,033 | 4,937,048 | -1.26% | 0 | 0 | 0 | n/a | 12 | 0 |
| 2026-10-06 | 4,937,048 | 4,856,771 | -1.63% | 0 | 0 | 0 | n/a | 20 | 1 |
| 2026-10-07 | 4,856,771 | 4,814,421 | -0.87% | 0 | 0 | 0 | n/a | 4 | 1 |
| 2026-10-08 | 4,814,421 | 4,787,123 | -0.57% | 0 | 0 | 0 | n/a | 12 | 31 |
| 2026-10-09 | 4,787,123 | 4,766,620 | -0.43% | 1 | 0 | -15,021 | 2,140 | 1 | 46 |
| 2026-10-10 | 4,766,620 | 4,774,853 | 0.17% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-11 | 4,774,853 | 4,773,013 | -0.04% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-12 | 4,773,013 | 4,769,347 | -0.08% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-13 | 4,769,347 | 4,767,360 | -0.04% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-14 | 4,767,360 | 4,768,574 | 0.03% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-15 | 4,768,574 | 4,770,119 | 0.03% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-16 | 4,770,119 | 4,769,865 | -0.0053% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-17 | 4,769,865 | 4,767,600 | -0.05% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-18 | 4,767,600 | 4,768,630 | 0.02% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-19 | 4,768,630 | 4,769,263 | 0.01% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-20 | 4,769,263 | 4,769,154 | -0.0023% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-21 | 4,769,154 | 4,767,429 | -0.04% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-22 | 4,767,429 | 4,768,108 | 0.01% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-23 | 4,768,108 | 4,765,955 | -0.05% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-24 | 4,765,955 | 4,764,385 | -0.03% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-25 | 4,764,385 | 4,765,964 | 0.03% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-26 | 4,765,964 | 4,768,461 | 0.05% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-27 | 4,768,461 | 4,771,353 | 0.06% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-28 | 4,771,353 | 4,772,807 | 0.03% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-29 | 4,772,807 | 4,770,811 | -0.04% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-30 | 4,770,811 | 4,777,080 | 0.13% | 0 | 0 | 0 | n/a | 0 | 0 |
| 2026-10-31 | 4,777,080 | 4,777,057 | -0.0005% | 0 | 0 | 0 | n/a | 0 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

| risk code | vetoes | share |
| --- | ---: | ---: |
| FEE_BUDGET | 76 | 96.20% |
| USDT_BOUNDS | 3 | 3.80% |

Decision records (9 daily files, together the whole run: [decisions-20261001.jsonl](decisions-20261001.jsonl), [decisions-20261002.jsonl](decisions-20261002.jsonl), [decisions-20261003.jsonl](decisions-20261003.jsonl), [decisions-20261004.jsonl](decisions-20261004.jsonl), [decisions-20261005.jsonl](decisions-20261005.jsonl), [decisions-20261006.jsonl](decisions-20261006.jsonl), [decisions-20261007.jsonl](decisions-20261007.jsonl), [decisions-20261008.jsonl](decisions-20261008.jsonl), [decisions-20261009.jsonl](decisions-20261009.jsonl); `decisions.jsonl` links only the newest day; orders and events are in the matching `orders-*` / `events-*` files): order 68, veto 79.

## AI (Regime Analyst)

AI was **off** in this run (rules-only defaults); no plans were produced.

| AI metric | value |
| --- | ---: |
| Plans produced | 0 |
| Average size multiplier | n/a |
| Plans that changed the enabled strategies | 0 |
| S3 entries gated by regime | 0 |
| Exits on a regime flip | 0 |

### Ablation (ai=on vs ai=off)

_Ablation not available yet for this run._

## Fees vs budget

| fees | value |
| --- | ---: |
| Fees paid (TMN) | 104,683 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | **104.68% (over budget)** |
| Fees per 72 h at this run's pace (720.00 h of data, TMN) | 10,468 |
| Budget used per 72 h at this pace | 10.47% |

## Limitations

Read these before reading any number above:

- SYNTHETIC — not evidence of performance: the market data was generated (seed 42, hours 720.0, step_s 10.0, volume_model clustered), not recorded from Wallex.
- Candle window = 720.0 h with 1-minute bars inside a requested window of 720.0 h; a minute without a candle inside a gap of at most 30 min is a flat bar at the previous close (the engine's CandleCache rule).
- No candles before the window: S2/S3 indicators warm up inside it (EMA60 of 15-minute bars needs 15 h, S3's breakout 21 bars).
- The challenge end was set to the window end: the deadline flatten ran in the last 2 h (no new entries there).
- S3 ran with allow_rules_only=true: without a fresh plan its regime gate is the rules regime of each symbol (S2's rule_regime), not an AI call.
- The drawdown kill-switch fired at 2026-10-09T01:39:00.000Z (drawdown_kill: drawdown 5.02% >= 5.00% from peak 5,023,241 TMN (equity 4,771,272 TMN)): the engine flattened to TMN and only monitored afterwards (manual reset only), so the rest of the window has no new trading.
- The fee budget (2.0% of initial equity, sized for the 72-h challenge) ran out: 76 entries were vetoed with FEE_BUDGET from 2026-10-08T23:01:00.000Z on.
- The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence of strategy performance.
- S1 not backtested on candles (candles carry no cross-book information); S1 evidence comes from the survey, the book-driven window and the live logs.
- Candle-driven fills use a synthetic book: mid = close, spread = the reference spread of configs/markets_snapshot.yaml for every symbol, unlimited depth at the best level; queue position and depth are not modelled.
- Fills come from the pessimistic candle fill model: an order acts one bar later; MARKET and crossing orders fill at close ± half the spread ± 0.02% slippage; the taker fee is paid on every fill of an order that crossed at placement (STOP orders always), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- 91,583 TMN was still held outside TMN (unrealized) at the end of the window although the deadline flatten ran (dust below a market's minimum order, or an exit still open); the challenge counts realized TMN only.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
