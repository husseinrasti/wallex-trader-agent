# Candle backtest on ALL recorded real Wallex 1-min candles (2026-10-22 12:25-14:40Z), S2+S3 (S3 rules-only), ai=off

| run | value |
| --- | --- |
| Config hash | `8fe0589adfb8` |
| Git commit | `34d5419073364f911376247ce165bf18ddd0a834` |
| Mode | backtest |
| Driver | candles |
| Requested window (UTC) | 2026-10-22T12:25:00Z → 2026-10-22T14:40:00Z (2.2 h) |
| Data present (UTC) | 2026-10-22T12:25:00Z → 2026-10-22T14:40:00Z (2.2 h) |
| Data source | recorded Wallex market data (wta.sqlite) |
| AI | off |
| Strategies | S2, S3 |
| Data end (UTC) | 2026-10-22T14:40:00Z |
| Report date | 2026-10-23 (commit date of 34d541907336) |
| ai_source | disabled |
| candles_ingested | 940 |
| coverage_hours | 2.25 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | no |
| git_dirty | no |
| initial_equity_tmn | 5,000,000 |
| market_db | data/wta.sqlite |
| seed | 42 |
| state_db | state_bt_candles_recorded_ai_off.sqlite |
| step_s | 1m 00s |
| tick_crashes | 0 |
| ticks | 135 |
| warmup_candles | 0 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 5,004,606 |
| PnL (TMN) | 4,606 |
| Return | 0.09% |
| Max drawdown (below running peak) | 0.09% |
| Longest drawdown | 1h 48m |
| Sharpe (annualized, from 1-h returns) | 36.74 |
| Sortino (annualized, from 1-h returns) | 243.46 |
| Closed trades / cycles | 0 |
| Winning trades | 0 |
| Losing trades | 0 |
| Win rate | n/a |
| Profit factor | n/a |
| Average PnL per trade | n/a |
| Average hold | n/a |
| Gross profit (TMN) | 0 |
| Gross loss (TMN) | 0 |
| Closed trades PnL (TMN) | 0 |
| Orders placed | 4 |
| Risk vetoes | 0 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 6,242 |
| Fees / initial equity | 0.12% |
| Turnover (TMN) | n/a |
| Days of data | 0.09 |
| Equity points | 135 |
| Hourly returns (Sharpe/Sortino sample) | 3 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.1 days of data (3 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 2,503,136 |
| USDT held (TMN value) | 2,501,469 |
| Coins held (TMN value) | 0 |
| Equity (TMN) | 5,004,606 |
| Realized PnL (TMN cash − initial equity) | -2,496,864 |
| Unrealized at the end (equity − TMN cash) | 2,501,469 |
| Unrealized share of equity | 49.98% |
| Flat (all TMN) | no |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (135 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

_No trades in the window._ ([trades.csv](trades.csv) has the header only.)

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 4 | 0 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-22 | 5,000,000 | 5,004,606 | 0.09% | 0 | 0 | 0 | n/a | 4 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

_No vetoes recorded._

Decision records ([decisions-20261022.jsonl](decisions-20261022.jsonl)): order 4.

## AI (Regime Analyst)

AI was **off** in this run (rules-only defaults); no plans were produced.

| AI metric | value |
| --- | ---: |
| Plans produced | 0 |
| Average size multiplier | n/a |
| Plan-to-plan changes of enabled_strategies | 0 |
| S3 entries gated by regime | 0 |
| Exits on a regime flip | 0 |

### Ablation (ai=on vs ai=off)

_Ablation not available yet for this run._

## Fees vs budget

| fees | value |
| --- | ---: |
| Fees paid (TMN) | 6,242 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 6.24% |
| Fees per 72 h at this run's pace (2.25 h of data, TMN) | 199,740 |
| Budget used per 72 h at this pace | **199.74% (over budget)** |

## Limitations

Read these before reading any number above:

- Candle window = 2.2 h with 1-minute bars inside a requested window of 2.2 h; a minute without a candle inside a gap of at most 30 min is a flat bar at the previous close (the engine's CandleCache rule).
- No candles before the window: S2/S3 indicators warm up inside it (EMA60 of 15-minute bars needs 15 h, S3's breakout 21 bars).
- No end-of-window flatten: holdings at the end are valued at the bid (unrealized).
- S3 ran with allow_rules_only=true: without a fresh plan its regime gate is the rules regime of each symbol (S2's rule_regime), not an AI call.
- S1 not backtested on candles (candles carry no cross-book information); S1 evidence comes from the survey, the book-driven window and the live logs.
- Candle-driven fills use a synthetic book: mid = close, spread = the median spread of recorded books (3,705 books, 2026-10-22T14:20:05.929Z → 2026-10-22T14:39:59.737Z, none after the window end) for every symbol, unlimited depth at the best level; queue position and depth are not modelled.
- Fills come from the pessimistic candle fill model: an order acts one bar later; MARKET and crossing orders fill at close ± half the spread ± 0.02% slippage; the taker fee is paid on every fill of an order that crossed at placement (STOP orders always), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- Sharpe and Sortino are computed from 3 hourly returns over 2.2 h of data: reported, but not meaningful over so short a window.
- No closed trades in the window: win rate, profit factor and average PnL are undefined.
- 2,501,469 TMN was still held outside TMN (unrealized) at the end of the window; the challenge counts realized TMN only, so this run was not flattened.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
