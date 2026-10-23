# Walk-forward out-of-sample: margin_min_pct=0.0012

> **SYNTHETIC DATA — not evidence of performance.** This run used generated market data (offline SyntheticFeed), not data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.

| run | value |
| --- | --- |
| Config hash | `9b7d073ad006` |
| Git commit | `e25b87b5248b1dddc40353ec1816acab378685d8` |
| Mode | backtest |
| Driver | books |
| Requested window (UTC) | 2026-10-01T16:48:00Z → 2026-10-02T00:00:00Z (7.2 h) |
| Data present (UTC) | 2026-10-01T16:48:00Z → 2026-10-02T00:00:00Z (7.2 h) |
| Data source | SYNTHETIC generated market DB synth_books_24h.sqlite (seed 42, hours 24.0, step_s 2.0, volume_model feed) (SYNTHETIC) |
| AI | off |
| Strategies | S1 |
| Data end (UTC) | 2026-10-02T00:00:00Z |
| Report date | 2026-10-23 (commit date of e25b87b5248b + uncommitted changes) |
| ai_source | disabled |
| candles_ingested | 3,031 |
| coverage_hours | 7.20 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | no |
| git_dirty | yes |
| initial_equity_tmn | 5,000,000 |
| market_db | synth_books_24h.sqlite |
| orders_stopped_by_gaps | 0 |
| s1_coverage_hours | 7.20 |
| seed | 42 |
| snapshots | 90,727 |
| state_db | state_walkforward_s1only.sqlite |
| step_s | 2 s |
| synthetic_note | synthetic market data: an offline exercise of the engine, never evidence of performance |
| tick_crashes | 0 |
| ticks | 12,961 |
| trades_published | 0 |
| warmup_candles | 7,049 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 5,000,000 |
| PnL (TMN) | 0 |
| Return | 0.00% |
| Max drawdown (below running peak) | 0.00% |
| Longest drawdown | 0 s |
| Sharpe (annualized, from 1-h returns) | n/a |
| Sortino (annualized, from 1-h returns) | n/a |
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
| Orders placed | 3 |
| Risk vetoes | 0 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 0 |
| Fees / initial equity | 0.00% |
| Turnover (TMN) | n/a |
| Days of data | 0.30 |
| Equity points | 2,593 |
| Hourly returns (Sharpe/Sortino sample) | 8 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.3 days of data (8 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 5,000,000 |
| USDT held (TMN value) | 0 |
| Coins held (TMN value) | 0 |
| Equity (TMN) | 5,000,000 |
| Realized PnL (TMN cash − initial equity) | 0 |
| Unrealized at the end (equity − TMN cash) | 0 |
| Unrealized share of equity | 0.00% |
| Flat (all TMN) | yes |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (2,593 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

_No trades in the window._ ([trades.csv](trades.csv) has the header only.)

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 3 | 0 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-01 | 5,000,000 | 5,000,000 | 0.00% | 0 | 0 | 0 | n/a | 3 | 0 |
| 2026-10-02 | 5,000,000 | 5,000,000 | 0.00% | 0 | 0 | 0 | n/a | 0 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

_No vetoes recorded._

Decision records ([decisions-20261001.jsonl](decisions-20261001.jsonl)): order 3.

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
| Fees paid (TMN) | 0 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 0.00% |
| Fees per 72 h at this run's pace (7.20 h of data, TMN) | 0 |
| Budget used per 72 h at this pace | 0.00% |

## Limitations

Read these before reading any number above:

- SYNTHETIC — not evidence of performance: the market data was generated (seed 42, hours 24.0, step_s 2.0, volume_model feed), not recorded from Wallex.
- Book window = 7.20 h of generated (synthetic) data (12,961 ticks of 2 s with a snapshot, 90,727 snapshots) inside a requested window of 7.2 h; time without snapshots is skipped, not simulated.
- S1 cross-book coverage (USDTTMN and one coin's TMN and USDT books all fresh at once): 7.20 h.
- No data in the window for: ADATMN, ADAUSDT, AVAXTMN, AVAXUSDT, DOGETMN, DOGEUSDT, LINKTMN, LINKUSDT, LTCTMN, LTCUSDT, TRXTMN, TRXUSDT, XRPTMN, XRPUSDT (left out of the watchlist).
- Indicator warm-up: 7,049 1-minute candles (16.8 h) from before the window were preloaded (the live engine loads up to 26 h at startup).
- No end-of-window flatten: holdings at the end are valued at the bid (unrealized).
- The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence of strategy performance.
- S1 backtest window limited to generated (synthetic) 7.2 h of order books.
- Fills come from the pessimistic book fill model: an order is matched only against a newer snapshot than the one it was placed on, and takes at most 80% of each level's quantity per snapshot, minus what our earlier fills took from that level while it is still shown (recorded snapshots do not reflect our fills); the taker fee is paid on every fill of an order that was crossing at placement (STOP_LIMIT: at its trigger), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- No closed trades in the window: win rate, profit factor and average PnL are undefined.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
