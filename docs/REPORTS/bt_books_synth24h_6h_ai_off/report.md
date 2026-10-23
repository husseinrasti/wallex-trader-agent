# Book backtest, 6 h sub-window 12:00-18:00Z, S1+S2, ai=off — SYNTHETIC

> **SYNTHETIC DATA — not evidence of performance.** This run used generated market data (offline SyntheticFeed), not data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.

| run | value |
| --- | --- |
| Config hash | `c76fea1dcafa` |
| Git commit | `e25b87b5248b1dddc40353ec1816acab378685d8` |
| Mode | backtest |
| Driver | books |
| Requested window (UTC) | 2026-10-01T12:00:00Z → 2026-10-01T18:00:00Z (6.0 h) |
| Data present (UTC) | 2026-10-01T12:00:00Z → 2026-10-01T18:00:00Z (6.0 h) |
| Data source | SYNTHETIC generated market DB synth_books_24h.sqlite (seed 42, hours 24.0, step_s 2.0, volume_model feed) (SYNTHETIC) |
| AI | off |
| Strategies | S1, S2 |
| Data end (UTC) | 2026-10-01T18:00:00Z |
| Report date | 2026-10-23 (commit date of e25b87b5248b + uncommitted changes) |
| ai_source | disabled |
| candles_ingested | 2,527 |
| coverage_hours | 6.00 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | no |
| git_dirty | yes |
| initial_equity_tmn | 5,000,000 |
| market_db | synth_books_24h.sqlite |
| orders_stopped_by_gaps | 0 |
| s1_coverage_hours | 6.00 |
| seed | 42 |
| snapshots | 75,607 |
| state_db | state_synth24h_6h_ai_off.sqlite |
| step_s | 2 s |
| synthetic_note | synthetic market data: an offline exercise of the engine, never evidence of performance |
| tick_crashes | 0 |
| ticks | 10,801 |
| trades_published | 0 |
| warmup_candles | 5,033 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 5,001,574 |
| PnL (TMN) | 1,574 |
| Return | 0.03% |
| Max drawdown (below running peak) | 0.38% |
| Longest drawdown | 3h 22m |
| Sharpe (annualized, from 1-h returns) | 3.77 |
| Sortino (annualized, from 1-h returns) | 6.06 |
| Closed trades / cycles | 1 |
| Winning trades | 0 |
| Losing trades | 1 |
| Win rate | 0.00% |
| Profit factor | 0.00 |
| Average PnL per trade | -0.20% |
| Average hold | 38 s |
| Gross profit (TMN) | 0 |
| Gross loss (TMN) | 1,953 |
| Closed trades PnL (TMN) | -1,953 |
| Orders placed | 12 |
| Risk vetoes | 0 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 13,006 |
| Fees / initial equity | 0.26% |
| Turnover (TMN) | n/a |
| Days of data | 0.25 |
| Equity points | 2,161 |
| Hourly returns (Sharpe/Sortino sample) | 6 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.2 days of data (6 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 2,496,520 |
| USDT held (TMN value) | 2,499,719 |
| Coins held (TMN value) | 5,335 |
| Equity (TMN) | 5,001,574 |
| Realized PnL (TMN cash − initial equity) | -2,503,480 |
| Unrealized at the end (equity − TMN cash) | 2,505,054 |
| Unrealized share of equity | 50.09% |
| Flat (all TMN) | no |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (2,161 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

Showing all 1 trades; full list (all columns): [trades.csv](trades.csv).

| entry_ts | closed_ts | strategy | symbol | pnl_tmn | pnl_pct | fees_tmn | hold_s | exit_reason | position_id | decision_id |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-01T17:42:46Z | 2026-10-01T17:43:24Z | S1 | ETHUSDT | -1,953 | -0.20% | 4,277 | 38 s | lockin | p_S1_ETHUSDT_1790876566000_1 | d_1790876564000_S1_ETHUSDT_1 |

Exit reasons: lockin 1.

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 | 1 | 0 | 1 | 0.00% | -1,953 | 0.00 | -0.20% | 38 s | 4,277 | 9 | 0 |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 3 | 0 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-01 | 5,000,000 | 5,001,574 | 0.03% | 1 | 0 | -1,953 | 4,277 | 12 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

_No vetoes recorded._

Decision records ([decisions-20261001.jsonl](decisions-20261001.jsonl)): order 12.

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

_ai=on (llm) run `bt_books_synth24h_6h_ai_on` vs ai=off run `bt_books_synth24h_6h_ai_off`: same window 2026-10-01T12:00:00.000Z → 2026-10-01T18:00:00.000Z, same market DB `synth_books_24h.sqlite`, same seed and config apart from ai.enabled_

| metric | ai=off | ai=on | delta (on − off) |
| --- | ---: | ---: | ---: |
| return_pct | 0.03% | 0.12% | +0.09 pp |
| max_drawdown_pct | 0.38% | 0.38% | +0.00 pp |
| n_trades | 1 | 0 | -1 |
| n_orders | 12 | 6 | -6 |
| fees_paid_tmn | 13,006 | 6,234 | -6,772 |
| n_vetoes | 0 | 0 | +0 |

_Drawdowns are positive fractions: a positive max_drawdown_pct delta means the AI run drew down more._

## Fees vs budget

| fees | value |
| --- | ---: |
| Fees paid (TMN) | 13,006 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 13.01% |
| Fees per 72 h at this run's pace (6.00 h of data, TMN) | 156,056 |
| Budget used per 72 h at this pace | **156.06% (over budget)** |

## Limitations

Read these before reading any number above:

- SYNTHETIC — not evidence of performance: the market data was generated (seed 42, hours 24.0, step_s 2.0, volume_model feed), not recorded from Wallex.
- Book window = 6.00 h of generated (synthetic) data (10,801 ticks of 2 s with a snapshot, 75,607 snapshots) inside a requested window of 6.0 h; time without snapshots is skipped, not simulated.
- S1 cross-book coverage (USDTTMN and one coin's TMN and USDT books all fresh at once): 6.00 h.
- No data in the window for: ADATMN, ADAUSDT, AVAXTMN, AVAXUSDT, DOGETMN, DOGEUSDT, LINKTMN, LINKUSDT, LTCTMN, LTCUSDT, TRXTMN, TRXUSDT, XRPTMN, XRPUSDT (left out of the watchlist).
- Indicator warm-up: 5,033 1-minute candles (12.0 h) from before the window were preloaded (the live engine loads up to 26 h at startup).
- No end-of-window flatten: holdings at the end are valued at the bid (unrealized).
- The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence of strategy performance.
- S1 backtest window limited to generated (synthetic) 6.0 h of order books.
- Fills come from the pessimistic book fill model: an order is matched only against a newer snapshot than the one it was placed on, and takes at most 80% of each level's quantity per snapshot, minus what our earlier fills took from that level while it is still shown (recorded snapshots do not reflect our fills); the taker fee is paid on every fill of an order that was crossing at placement (STOP_LIMIT: at its trigger), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- Sharpe and Sortino are computed from 6 hourly returns over 6.0 h of data: reported, but not meaningful over so short a window.
- 2,505,054 TMN was still held outside TMN (unrealized) at the end of the window; the challenge counts realized TMN only, so this run was not flattened.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
