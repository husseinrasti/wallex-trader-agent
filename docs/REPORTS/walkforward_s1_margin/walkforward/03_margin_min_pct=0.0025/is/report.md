# Walk-forward in-sample: margin_min_pct=0.0025

> **SYNTHETIC DATA — not evidence of performance.** This run used generated market data (offline SyntheticFeed), not data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.

| run | value |
| --- | --- |
| Config hash | `911f47de3964` |
| Git commit | `34d5419073364f911376247ce165bf18ddd0a834` |
| Mode | backtest |
| Driver | books |
| Requested window (UTC) | 2026-10-01T00:00:00Z → 2026-10-01T16:47:58Z (16.8 h) |
| Data present (UTC) | 2026-10-01T00:00:00Z → 2026-10-01T16:47:58Z (16.8 h) |
| Data source | SYNTHETIC generated market DB synth_books_24h.sqlite (seed 42, hours 24.0, step_s 2.0, volume_model feed) (SYNTHETIC) |
| AI | off |
| Strategies | S1, S2 |
| Data end (UTC) | 2026-10-01T16:47:58Z |
| Report date | 2026-10-23 (commit date of 34d541907336) |
| ai_source | disabled |
| candles_ingested | 7,049 |
| coverage_hours | 16.80 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | no |
| git_dirty | no |
| initial_equity_tmn | 5,000,000 |
| market_db | synth_books_24h.sqlite |
| orders_stopped_by_gaps | 0 |
| s1_coverage_hours | 16.80 |
| seed | 42 |
| snapshots | 211,680 |
| state_db | state_walkforward.sqlite |
| step_s | 2 s |
| synthetic_note | synthetic market data: an offline exercise of the engine, never evidence of performance |
| tick_crashes | 0 |
| ticks | 30,240 |
| trades_published | 0 |
| warmup_candles | 0 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 5,007,069 |
| PnL (TMN) | 7,069 |
| Return | 0.14% |
| Max drawdown (below running peak) | 0.60% |
| Longest drawdown | 15h 24m |
| Sharpe (annualized, from 1-h returns) | 7.10 |
| Sortino (annualized, from 1-h returns) | 9.67 |
| Closed trades / cycles | 2 |
| Winning trades | 0 |
| Losing trades | 2 |
| Win rate | 0.00% |
| Profit factor | 0.00 |
| Average PnL per trade | -0.29% |
| Average hold | 1m 57s |
| Gross profit (TMN) | 0 |
| Gross loss (TMN) | 5,813 |
| Closed trades PnL (TMN) | -5,813 |
| Orders placed | 21 |
| Risk vetoes | 16 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 16,403 |
| Fees / initial equity | 0.33% |
| Turnover (TMN) | n/a |
| Days of data | 0.70 |
| Equity points | 6,048 |
| Hourly returns (Sharpe/Sortino sample) | 17 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.7 days of data (17 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 2,488,829 |
| USDT held (TMN value) | 2,507,445 |
| Coins held (TMN value) | 10,700 |
| Equity (TMN) | 5,006,975 |
| Realized PnL (TMN cash − initial equity) | -2,511,171 |
| Unrealized at the end (equity − TMN cash) | 2,518,145 |
| Unrealized share of equity | 50.29% |
| Flat (all TMN) | no |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (6,048 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

Showing all 2 trades; full list (all columns): [trades.csv](trades.csv).

| entry_ts | closed_ts | strategy | symbol | pnl_tmn | pnl_pct | fees_tmn | hold_s | exit_reason | position_id | decision_id |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-01T04:22:12Z | 2026-10-01T04:24:08Z | S1 | ETHUSDT | -3,343 | -0.34% | 2,583 | 1m 56s | target | p_S1_ETHUSDT_1790828532000_1 | d_1790828522000_S1_ETHUSDT_1 |
| 2026-10-01T09:05:12Z | 2026-10-01T09:07:10Z | S1 | ETHUSDT | -2,470 | -0.25% | 2,596 | 1m 58s | target | p_S1_ETHUSDT_1790845512000_2 | d_1790845510000_S1_ETHUSDT_1 |

Exit reasons: target 2.

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 | 2 | 0 | 2 | 0.00% | -5,813 | 0.00 | -0.29% | 1m 57s | 5,179 | 16 | 1 |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 5 | 15 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-01 | 5,000,000 | 5,007,069 | 0.14% | 2 | 0 | -5,813 | 5,179 | 21 | 16 |

## Risk vetoes

![Veto histogram](vetoes.png)

| risk code | vetoes | share |
| --- | ---: | ---: |
| RATE_HOUR | 16 | 100.00% |

Decision records ([decisions-20261001.jsonl](decisions-20261001.jsonl)): order 21, veto 16.

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
| Fees paid (TMN) | 16,403 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 16.40% |
| Fees per 72 h at this run's pace (16.80 h of data, TMN) | 70,299 |
| Budget used per 72 h at this pace | 70.30% |

## Limitations

Read these before reading any number above:

- SYNTHETIC — not evidence of performance: the market data was generated (seed 42, hours 24.0, step_s 2.0, volume_model feed), not recorded from Wallex.
- Book window = 16.80 h of generated (synthetic) data (30,240 ticks of 2 s with a snapshot, 211,680 snapshots) inside a requested window of 16.8 h; time without snapshots is skipped, not simulated.
- S1 cross-book coverage (USDTTMN and one coin's TMN and USDT books all fresh at once): 16.80 h.
- No data in the window for: ADATMN, ADAUSDT, AVAXTMN, AVAXUSDT, DOGETMN, DOGEUSDT, LINKTMN, LINKUSDT, LTCTMN, LTCUSDT, TRXTMN, TRXUSDT, XRPTMN, XRPUSDT (left out of the watchlist).
- No candles before the window: S2/S3 indicators warm up inside it (EMA60 of 15-minute bars needs 15 h, S3's breakout 21 bars).
- No end-of-window flatten: holdings at the end are valued at the bid (unrealized).
- The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence of strategy performance.
- S1 backtest window limited to generated (synthetic) 16.8 h of order books.
- Fills come from the pessimistic book fill model: an order is matched only against a newer snapshot than the one it was placed on, and takes at most 80% of each level's quantity per snapshot, minus what our earlier fills took from that level while it is still shown (recorded snapshots do not reflect our fills); the taker fee is paid on every fill of an order that was crossing at placement (STOP_LIMIT: at its trigger), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- Sharpe and Sortino are computed from 17 hourly returns over 17 h of data: reported, but not meaningful over so short a window.
- 2,518,145 TMN was still held outside TMN (unrealized) at the end of the window; the challenge counts realized TMN only, so this run was not flattened.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
