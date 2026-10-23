# Shadow run report: shadow_synth_4h — SYNTHETIC

> **SYNTHETIC DATA — not evidence of performance.** This run used generated market data (offline SyntheticFeed), not data recorded from Wallex. It proves the pipeline runs end to end; it proves nothing about returns.

| run | value |
| --- | --- |
| Config hash | `481cc6dc3ce2` |
| Git commit | `7e8f2f5af1c55c3d744c6c8de22a454040b072f9` |
| Mode | shadow |
| Driver | n/a |
| Requested window (UTC) | 2026-10-22T22:53:39.578Z → 2026-10-23T02:58:39.872Z (4.1 h) |
| Data present (UTC) | 2026-10-22T22:53:39.782Z → 2026-10-23T02:58:36.767Z (4.1 h) |
| Data source | SYNTHETIC — not evidence of performance: the offline synthetic feed (paper fills) (SYNTHETIC) |
| AI | on |
| Strategies | S1, S2 |
| Data end (UTC) | 2026-10-23T02:58:39.872Z |
| Report date | 2026-10-23 (commit date of e25b87b5248b + uncommitted changes) |
| ai_source | llm |
| engine_starts | 1 |
| equity_source | state_db |
| feed | synthetic |
| log_dir | logs/shadow_synth_4h |
| state_db | data/state_shadow4h_shadow_synthetic.sqlite |

## Metrics

| metric | value |
| --- | ---: |
| Initial equity (TMN) | 5,000,000 |
| Final equity (TMN) | 5,002,526 |
| PnL (TMN) | 2,526 |
| Return | 0.05% |
| Max drawdown (below running peak) | 0.38% |
| Longest drawdown | 3h 12m |
| Sharpe (annualized, from 1-h returns) | 6.38 |
| Sortino (annualized, from 1-h returns) | 9.42 |
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
| Orders placed | 2 |
| Risk vetoes | 0 |
| Repeated vetoes suppressed in the log | 0 |
| Fees paid (TMN) | 6,240 |
| Fees / initial equity | 0.12% |
| Turnover (TMN) | n/a |
| Days of data | 0.17 |
| Equity points | 1,459 |
| Hourly returns (Sharpe/Sortino sample) | 5 |

_Percentages are shown ×100; [summary.json](summary.json) has every metric, including the nested breakdowns, with percentages as fractions (0.012 = 1.2%)._

Notes on the metrics:

- 0.2 days of data (5 hourly returns) — do not over-interpret
- initial equity = first equity row (no initial_equity given)
- Sources: fees from given.

### End state: realized vs unrealized

| end of window | value |
| --- | ---: |
| TMN cash | 2,503,557 |
| USDT held (TMN value) | 2,498,969 |
| Coins held (TMN value) | 0 |
| Equity (TMN) | 5,002,526 |
| Realized PnL (TMN cash − initial equity) | -2,496,443 |
| Unrealized at the end (equity − TMN cash) | 2,498,969 |
| Unrealized share of equity | 49.95% |
| Flat (all TMN) | no |
| Source | equity_row |

## Equity curve (TMN)

![Equity curve](equity.png)

Data: [equity.csv](equity.csv) (1,459 points).

## Drawdown

![Drawdown](drawdown.png)

_Drawdown = (running peak − equity) / running peak; the initial equity is the first peak._

## Trades

_No trades in the window._ ([trades.csv](trades.csv) has the header only.)

## Per strategy

| strategy | trades | wins | losses | win rate | PnL (TMN) | profit factor | avg PnL | avg hold | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S2 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | n/a | n/a | 2 | 0 |

## Per day

| date (UTC) | start equity (TMN) | end equity (TMN) | return | trades | wins | trades PnL (TMN) | fees (TMN) | orders | vetoes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-22 | 5,000,000 | 4,985,645 | -0.29% | 0 | 0 | 0 | n/a | 2 | 0 |
| 2026-10-23 | 4,985,645 | 5,002,526 | 0.34% | 0 | 0 | 0 | n/a | 0 | 0 |

## Risk vetoes

![Veto histogram](vetoes.png)

_No vetoes recorded._

Decision records: order 2, plan 38.

## S1 opportunity ticks

An opportunity tick is a fast tick on which a coin's lock-in edge reached cost + the margin in force (the plan's `s1_margin_min_pct`, else the config's) on either side, TMN-cheap or USDT-cheap. These are the engine's own counters, read from the heartbeats; best excess = the largest edge − cost − margin seen (negative: no tick reached the threshold).

| coin | ticks evaluated | opportunity ticks | best excess | last opportunity (UTC) | last why-no-trade |
|---|---:|---:|---:|---|---|
| ADA | 7,292 | 0 | -0.011% | - | no lock-in: TMN-cheap edge -0.165% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.216% < cost 0.430% + margin 0.150% |
| AVAX | 7,292 | 0 | -0.261% | - | no lock-in: TMN-cheap edge -0.200% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.230% < cost 0.430% + margin 0.150% |
| BTC | 7,292 | 4 | +0.138% | 2026-10-23T00:28:16.380Z | no lock-in: TMN-cheap edge -0.108% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.122% < cost 0.430% + margin 0.150% |
| DOGE | 7,292 | 4 | +0.090% | 2026-10-22T23:51:50.626Z | no lock-in: TMN-cheap edge -0.154% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.100% < cost 0.430% + margin 0.150% |
| ETH | 7,292 | 10 | +0.109% | 2026-10-23T02:19:35.205Z | no lock-in: TMN-cheap edge -0.051% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.045% < cost 0.430% + margin 0.150% |
| LINK | 7,292 | 0 | -0.093% | - | no lock-in: TMN-cheap edge -0.167% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.201% < cost 0.430% + margin 0.150% |
| LTC | 7,292 | 2 | +0.022% | 2026-10-23T01:07:21.418Z | no lock-in: TMN-cheap edge -0.190% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.183% < cost 0.430% + margin 0.150% |
| SOL | 7,292 | 0 | -0.123% | - | no lock-in: TMN-cheap edge -0.227% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.252% < cost 0.430% + margin 0.150% |
| TRX | 7,292 | 0 | -0.024% | - | no lock-in: TMN-cheap edge -0.202% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.167% < cost 0.430% + margin 0.150% |
| XRP | 7,292 | 0 | -0.156% | - | no lock-in: TMN-cheap edge -0.127% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.131% < cost 0.430% + margin 0.150% |

Total: 20 opportunity ticks in 72,920 coin-ticks evaluated. S1 entries: 0 decided, 0 placed. The analyst left S1 out of `enabled_strategies` in 38 of 38 plans (no new S1 entries while such a plan is fresh).

## AI (Regime Analyst)

| AI metric | value |
| --- | ---: |
| Plans produced | 38 |
| Average size multiplier | 0.28 |
| Plans that changed the enabled strategies | 0 |
| Plans served from the LLM cache | 0 |
| Plans with clamped fields | 1 |
| S3 entries gated by regime | 0 |
| Exits on a regime flip | 0 |
| Models | openai/gpt-5.4-mini |

Regime calls per symbol (count and share of that symbol's calls):

| symbol | trend_up | range | trend_down | chaos | calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTCUSDT | 3 (7.89%) | 25 (65.79%) | 10 (26.32%) | 0 (0.00%) | 38 |
| ETHUSDT | 1 (2.63%) | 22 (57.89%) | 15 (39.47%) | 0 (0.00%) | 38 |
| SOLUSDT | 8 (21.05%) | 12 (31.58%) | 17 (44.74%) | 1 (2.63%) | 38 |
| USDTTMN | 3 (7.89%) | 35 (92.11%) | 0 (0.00%) | 0 (0.00%) | 38 |

![Regime timeline](regimes.png)

### Analyst availability

| | value |
|---|---:|
| analyst cycles | 49 |
| plans | 38 |
| cycles without a plan | 11 |
| model calls | 86 |
| model calls served from the cache | 0 |
| tokens | 518,798 |
| post-trade reviews | 4 |

- failure streak from 2026-10-23T01:29:58.358Z: llm_invalid: an invalid answer
- failure streak from 2026-10-23T02:10:18.812Z: llm unavailable: provider unreachable
- recovered (new plan) at 2026-10-23T01:34:59.896Z
- plan stale at 2026-10-23T02:25:19.513Z: rules-only until the end of the window (2026-10-23T02:58:39.872Z, 0.56 h)

_From the heartbeats' AI counters (last value of each engine process) and the run's `ai_degraded` / `ai_recovered` / `plan_stale` events. A failure streak is logged once, at its first failed cycle._

### Ablation (ai=on vs ai=off)

_Ablation not available yet for this run._

## Fees vs budget

| fees | value |
| --- | ---: |
| Fees paid (TMN) | 6,240 |
| Initial equity (TMN) | 5,000,000 |
| Fee budget (2.00% of initial equity over the 72-h challenge, TMN) | 100,000 |
| Budget used | 6.24% |
| Fees per 72 h at this run's pace (4.08 h of data, TMN) | 110,047 |
| Budget used per 72 h at this pace | **110.05% (over budget)** |

## Limitations

Read these before reading any number above:

- The LLM analyst gave no plan in 11 of 49 cycles (llm_invalid: an invalid answer from 2026-10-23T01:29:58.358Z; llm unavailable: provider unreachable from 2026-10-23T02:10:18.812Z); the plan went stale at 2026-10-23T02:25:19.513Z and the strategies ran rules-only until the end of the window (2026-10-23T02:58:39.872Z, 0.56 h).
- The data is synthetic (generated by the offline SyntheticFeed, not recorded from Wallex): the run exercises the code path end to end, but its returns, drawdowns and trade counts are not evidence of strategy performance.
- Shadow fills are paper fills against the live book (PaperExchange): they consume no liquidity and ignore queue position.
- Sharpe and Sortino are computed from 5 hourly returns over 4.1 h of data: reported, but not meaningful over so short a window.
- No closed trades in the window: win rate, profit factor and average PnL are undefined.
- 2,498,969 TMN was still held outside TMN (unrealized) at the end of the window; the challenge counts realized TMN only, so this run was not flattened.

---

_Generated by `backtest/report.py`. Files: [summary.json](summary.json), [trades.csv](trades.csv), [equity.csv](equity.csv)._
