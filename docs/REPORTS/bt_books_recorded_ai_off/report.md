# Book backtest on ALL recorded real Wallex books (2026-10-22 14:20-20:17Z), S1+S2, ai=off

| run | value |
| --- | --- |
| Config hash | `b942685638b5` |
| Git commit | `34d5419073364f911376247ce165bf18ddd0a834` |
| Mode | backtest |
| Driver | books |
| Requested window (UTC) | 2026-10-22T14:20:00Z → 2026-10-22T20:17:00Z (6.0 h) |
| Data present (UTC) | 2026-10-22T14:20:05.929Z → 2026-10-22T20:16:13.129Z (0.3 h) |
| Data source | recorded Wallex market data (wta.sqlite) |
| AI | off |
| Strategies | S1, S2 |
| Data end (UTC) | 2026-10-22T20:17:00Z |
| Report date | 2026-10-23 (commit date of 34d541907336) |
| ai_source | disabled |
| candles_ingested | 211 |
| coverage_hours | 0.32 |
| fee_budget_pct | 2.00% |
| fees_source | configs/fees_snapshot.yaml |
| flatten_at_end | no |
| git_dirty | no |
| initial_equity_tmn | 5,000,000 |
| market_db | data/wta.sqlite |
| orders_stopped_by_gaps | 0 |
| s1_coverage_hours | 0.32 |
| seed | 42 |
| snapshots | 6,131 |
| state_db | state_bt_books_recorded_ai_off.sqlite |
| step_s | 2 s |
| tick_crashes | 0 |
| ticks | 583 |
| trades_published | 300 |
| warmup_candles | 1,265 |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 4,997,244 |
| PnL (TMN) | -2,756 |
| Return | -0.06% |
| Max drawdown (below running peak) | 0.06% |
| Longest drawdown | 5h 53m |
| Sharpe (annualized, from 1-h returns) | -52.14 |
| Sortino (annualized, from 1-h returns) | -48.25 |
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
| Fees paid (TMN) | 3,123 |
| Fees / initial equity | 0.06% |
| Turnover (TMN) | n/a |
| Days of data | 0.25 |
| Equity points | 122 |
| Hourly returns (Sharpe/Sortino sample) | 7 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.2 days of data (7 hourly returns) — do not over-interpret
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 3,750,916 |
| USDT held (TMN value) | 1,246,328 |
| Coins held (TMN value) | 0 |
| Equity (TMN) | 4,997,244 |
| Realized PnL (TMN cash − initial equity) | -1,249,084 |
| Unrealized at the end (equity − TMN cash) | 1,246,328 |
| Unrealized share of equity | 24.94% |
| Flat (all TMN) | no |
| Source | valuation |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (122 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

_No trades in the window._ ([trades.csv](trades.csv) has the header only.)

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 3 | 0 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-22 | 5,000,000 | 4,997,244 | -0.06% | 0 | 0 | 0 | n/a | 3 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

_No vetoes recorded._

Decision records ([decisions-20261022.jsonl](decisions-20261022.jsonl)): order 3.

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

_ai=on (llm) run `bt_books_recorded_ai_on` vs ai=off run `bt_books_recorded_ai_off`: same window 2026-10-22T14:20:00.000Z → 2026-10-22T20:17:00.000Z, same market DB `data/wta.sqlite`, same seed and config apart from ai.enabled_

| metric | ai=off | ai=on | delta (on − off) |
| --- | ---: | ---: | ---: |
| return_pct | -0.06% | -0.06% | +0.00 pp |
| max_drawdown_pct | 0.06% | 0.06% | +0.00 pp |
| n_trades | 0 | 0 | +0 |
| n_orders | 3 | 3 | +0 |
| fees_paid_tmn | 3,123 | 3,123 | +0 |
| n_vetoes | 0 | 0 | +0 |

_Drawdowns are positive fractions: a positive max_drawdown_pct delta means the AI run drew down more._

## Fees vs budget

| fees | value |
| --- | ---: |
| Fees paid (TMN) | 3,123 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 3.12% |

## Limitations

Read these before reading any number above:

- Book window = 0.32 h of recorded data (583 ticks of 2 s with a snapshot, 6,131 snapshots) inside a requested window of 6.0 h; time without snapshots is skipped, not simulated.
- S1 cross-book coverage (USDTTMN and one coin's TMN and USDT books all fresh at once): 0.32 h.
- Indicator warm-up: 1,265 1-minute candles (1.9 h) from before the window were preloaded (the live engine loads up to 26 h at startup).
- No end-of-window flatten: holdings at the end are valued at the bid (unrealized).
- The requested window is 6.0 h but the data present covers only 0.3 h.
- S1 backtest window limited to recorded 0.3 h of order books.
- Fills come from the pessimistic book fill model: an order is matched only against a newer snapshot than the one it was placed on, and takes at most 80% of each level's quantity per snapshot, minus what our earlier fills took from that level while it is still shown (recorded snapshots do not reflect our fills); the taker fee is paid on every fill of an order that was crossing at placement (STOP_LIMIT: at its trigger), the maker fee only by a LIMIT resting when placed; real queue position is not modelled.
- AI disabled (ai=off): rules-only defaults; the AI's contribution shows only in the ablation.
- Sharpe and Sortino are computed from 7 hourly returns over 0.3 h of data: reported, but not meaningful over so short a window.
- No closed trades in the window: win rate, profit factor and average PnL are undefined.
- 1,246,328 TMN was still held outside TMN (unrealized) at the end of the window; the challenge counts realized TMN only, so this run was not flattened.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
