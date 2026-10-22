# Phase 3 acceptance (PRD-03 §9): strategies wired into the engine, 2026-10-22

**Read this first.** `api.wallex.ir` is unreachable from the development machine (see
`docs/DECISIONS.md`), so the shadow run below uses `--feed synthetic`: the deterministic offline
feed seeded with the reference mids and spreads of `configs/markets_snapshot.yaml`, markets from the
same snapshot (`markets_source: snapshot`). It **exercises the engine end to end (features ->
strategies -> router -> risk -> orders -> paper fills -> logs). It is not evidence of strategy
performance**, and every `start` event and heartbeat says so. Run (b) uses hand-crafted books in a
test rig (simulated clock) to force one full S1 lock-in cycle; it is a wiring demonstration, not a
market observation.

The PRD's **≥ 4 h shadow run with S1 + S2 is still open**: it needs the real feed. The run here is
600 s, as the phase task asked.

Setup:

* code: commit `667d781` plus the uncommitted Phase-3 work in this checkout (`git_dirty: true` in
  the start event): `src/wta/engine.py`, `cli.py`, `data/collector.py`, `data/store.py`,
  `strategies/{base,router,s1_crossbook,s3_momentum}.py`, `features/engine.py`, `risk/manager.py`,
  `execution/order_manager.py`, `scripts/shadow_report.py`, new tests, **including the Phase-3 review
  fixes** (section "Review fixes" below). Both runs were repeated after the fixes; the numbers below
  are from those re-runs. Python 3.12 (`.venv`), shadow mode (PaperExchange), S1 + S2 constructed
  (S3 is disabled in `configs/default.yaml`), no MarketPlan (the AI layer is not wired yet: rules-only
  defaults, `rules_only: true` in the start event).
* After the re-run started only docstrings and `docs/STRATEGY.md` changed.
* `$S` is the session scratch directory
  `/private/tmp/claude-501/-Users-hussein-Code-hackathon-wallex-trader-agent/c1bcf3cd-52c7-4b43-bf93-ad2b10bf3e92/scratchpad`;
  nothing was written to the repo's `logs/` or `data/`.

| Item | Where | Result |
|---|---|---|
| Per-mode (and per-feed) engine state DB (`data/state_{mode}.sqlite`; synthetic: `state_shadow_synthetic`), lock follows it, shadow/live/synthetic never share | run (a), tests | pass |
| In-memory 26 h candle cache (missing minutes filled flat): synthetic `on_candle` -> cache + state DB (`candles` + append-only `engine_candles`, `ticks.candle_seq`); live: backfill <= 1-day windows, then refresh (supervised task) | run (a) (synthetic), tests (live, fake REST; replay of the log) | pass |
| Every tick: features, portfolio view, `StrategyContext`, `router.tick(ctx, exits)` -> cooldown -> risk -> submit with compact features | run (a), (b), tests | pass |
| Hooks: fills -> `on_fill` (rebalance fills too), done orders -> `on_order_done` with the logical fill, `note_fill`, `prune` | run (b), tests | pass |
| kv `strategy_state` / `feature_state` saved and restored at startup | run (a) state DB, restart test | pass |
| S2 performs the initial allocation | run (a) | pass (2nd attempt filled) |
| S1 shows evaluated opportunities with edge/cost numbers every minute in `health` | run (a) | pass |
| Decision records of a full S1 cycle and an S2 rebalance pass `python -m wta logs check` | run (b) (crafted books), run (a) (S2) | pass |
| `wta run --overlay configs/live_halfsize.yaml` merges the overlay, `config_hash` changes and is logged | tests, `wta config hash --overlay` | pass |
| Shadow >= 4 h with S1 + S2 on the real feed | - | **open** (API unreachable) |
| full suite: `ruff check src tests scripts backtest` and `pytest` | - | pass: `1688 passed in 54.75s` |

---

## (a) Shadow, synthetic feed, S1 + S2, 600 s

```
$ python -m wta run --mode shadow --feed synthetic --seconds 600 \
    --log-dir $S/logs4 --set data.state_sqlite_path=$S/state4.sqlite
... engine: shadow mode, feed synthetic, commit 667d781468b0 (dirty), config 58c570d67cea, 11 symbols
... engine: stopped (ran for 600 s)
exit 0

$ python -m wta logs check --dir $S/logs4 --secrets
decisions-20261022.jsonl: 5 lines, ok
health-20261022.jsonl: 11 lines, ok
events-20261022.jsonl: 2 lines, ok
orders-20261022.jsonl: 14 lines, ok
OK
secrets scan: clean

$ python scripts/shadow_report.py --log-dir $S/logs4 --state-db $S/state4.shadow_synthetic.sqlite \
    --out $S/p3/shadow_acceptance4.md
```

The full report is reproduced in the appendix. What it shows:

* **Start event**: `strategies: ["S1", "S2"]`, `rules_only: true`,
  `state_db: $S/state4.shadow_synthetic.sqlite` (the given path has no `{mode}`, so the account label
  is inserted, and a synthetic run gets its own account), `overlays: []`,
  `config_hash: 58c570d67cea` (it differs from the first run's only by the state path override),
  `synthetic: true`. 299 fast ticks, 11 heartbeats, no tick or task crash, no health reason in any
  heartbeat.
* **State DB** (`$S/state4.shadow_synthetic.sqlite`): kv keys `engine_flags, feature_state,
  order_manager_state, paper_state, portfolio_state, strategy_state`; `candles` 121 rows (11 symbols
  x 11 one-minute bars, latest versions), `engine_candles` 6,578 arrivals (the synthetic feed
  re-emits every symbol's forming bar each tick), `ticks` 299 with `candle_seq` 11 ... 6,556,
  `engine_books` 3,289, `equity` 60, `orders` 4, `fills` 1. The engine lock is
  `$S/state4.shadow_synthetic.sqlite.engine.lock`.
* **S2 initial allocation** (the first trade): the first tick with a USDTTMN book asks for 50 % USDT
  (rules regime `range`; no candle history yet, so every EMA/ATR rule is `n/a`), i.e. 2,500,000 TMN;
  the RiskManager's `MAX_ORDER_PCT` cuts the order to 25 % of equity = 4.68 USDT. Attempt 1
  (18:06:11 UTC, maker BUY at bid + 1 tick) was not filled after 2 re-prices and expired after 45 s;
  attempt 2, 120 s later, filled 4.68 USDT at 266,770 TMN as a maker (fee 0.0117 USDT). USDT share
  ends at 24.89 %; the rest of the gap waits for `min_rebalance_interval_s` (30 min, the S2
  diagnostics show `status: min_interval`, `wait_s: 1555` at 18:13:11). Equity 5,000,000 ->
  4,994,936.78 TMN (-0.10 %: the 3,120 TMN maker fee plus marking USDT at the bid).
* **S1 diagnostics in every heartbeat** (`health.extra.strategies.S1`), e.g. 18:13:11 UTC:

  | coin | edge TMN-cheap | edge USDT-cheap | need (cost + margin) | dev | z | why no trade |
  |---|---|---|---|---|---|---|
  | BTC | -0.097 % | -0.132 % | 0.58 % | -0.017 % | -1.05 | no lock-in ...; revert: \|z\| 1.05 < z_min 2.0 |
  | ETH | +0.043 % | -0.138 % | 0.58 % | -0.090 % | 0.06 | no lock-in ...; revert: \|z\| 0.06 < z_min 2.0 |
  | SOL | -0.201 % | -0.274 % | 0.58 % | -0.036 % | -1.12 | no lock-in ...; revert: \|z\| 1.12 < z_min 2.0 |

  The engine's counters over the run (all 299 ticks evaluated per coin): opportunity ticks
  (lock-in edge >= cost + margin) BTC 0, DOGE 0, ETH 2, SOL 0, XRP 0. ADA, TRX, LTC, LINK and AVAX
  are in `strategies.s1_crossbook.coins` but have no TMN/USDT books in the watchlist ("no cross
  features"): see "Open items".
* **S1 decided one entry** at 18:16:07 UTC: a synthetic ETH jump made the USDT book 0.696 % cheap;
  the need was 0.596 % = cost 0.430 % + margin 0.150 % + the unhedged-remainder charge 0.016 %
  (buying 0.00152 ETH leaves 0.001518024 after the 0.13 % fee paid in ETH, leg 2 can sell 0.00151:
  0.528 % of the cycle x 3 %). The RiskManager vetoed it on `USDT_BOUNDS`: a USDT-cheap lock-in turns
  USDT into TMN (buy ETHUSDT with USDT, sell ETHTMN), and the USDT share was already 24.89 %, just
  under the 25 % S2 lower bound, so the trade (-> 5.02 %) would have moved it further out. That is
  the band doing its job; it also shows that S1's USDT-cheap direction depends on S2 having built
  the USDT pool first (the initial allocation stops at 25 % per order).

## (b) Full S1 lock-in cycle + S2 rebalance on crafted books (simulated clock)

The rig of `tests/test_strategies_engine.py` (real engine, RiskManager, OrderManager,
PaperExchange, PositionManager, Store, logs; `SimClock` ticks of 2 s) with `CrossFeed` books:
BTCTMN = BTCUSDT x USDTTMN x (1 + lag). 10,000,000 TMN paper account. Ticks 5-7 open a -0.9 %
TMN-book lag; from tick 3 the dollar dips so S2's resting maker BUY fills. Script:
`$S/p3/demo_cycle.py $S/p3/demo4` (scratch, re-run after the review fixes); the same scenario runs
in `test_two_engines_on_identical_books_write_identical_decision_logs`.

```
order   10:00:02  S2 USDTTMN rebalance   initial allocation, 25 % cap -> FILLED 9.37 USDT at 266,713 (maker)
order   10:00:12  S1 BTCTMN  entry       "BTC: BTCTMN ask 21,911,480,434 TMN / USDTTMN bid 266,573 = 82,196.92 USDT
                                          vs BTCUSDT bid 82,843.84 -> TMN book 0.787% cheap; cost 0.430% (0.300% taker
                                          BTCTMN + 0.130% taker BTCUSDT); edge - cost = 0.357% >= margin min 0.150% ...;
                                          unhedged remainder: buying 0.000091 BTC leaves 0.000090727 after the 0.300%
                                          leg-1 fee (paid in BTC) and leg 2 on BTCUSDT (step 0.000001) can sell 0.000090:
                                          the unhedged 0.000000727 BTC (0.799% of the cycle) x a 3% adverse move =
                                          0.024%, so edge 0.787% >= need 0.604%; lock-in: buy 0.000091 BTC on BTCTMN ...
                                          as taker, then sell the 0.000090 BTC left after the leg-1 fee on BTCUSDT at its
                                          bid 82,843.84 (leg 2); size ... limited by budget ..."
order   10:00:14  S1 BTCUSDT leg2        on the leg-1 fill (same tick as the fill): "sell 0.000090 BTC on BTCUSDT at bid
                                          82,843.84 (7.46 USDT, crossing, taker 0.130%); cycle edge now 0.357% after
                                          0.430% fees (entry edge 0.787% before fees) is above the -0.30% leg-2 loss limit."
outcome           entry d_1792663212000_S1_BTCTMN_1: exit_reason lockin, pnl_tmn +6,932.61, pnl_pct +0.348 %,
                  hold 2 s, dust 0.000000727 BTC (the BUY fee is taken in BTC)
outcome           leg 2: FILLED 0.00009 BTC at 82,843.84

$ python -m wta logs check --dir $S/p3/demo4/logs
decisions-20261022.jsonl: 6 lines, ok
health-20261022.jsonl: 4 lines, ok
events-20261022.jsonl: 2 lines, ok
orders-20261022.jsonl: 12 lines, ok
OK
```

Each decision record carries the intent (with the strategy's own numbers in `intent.features`),
the full 23-check risk list, the order request/response and, in `features`, the compact feature
snapshot of the symbols the intent used (`FeatureSnapshot.compact` for `books_used`: BTCTMN,
BTCUSDT, USDTTMN and the BTC cross-book features).

## Review fixes (Phase-3 reviewer findings, all applied)

Each fix has a regression test that fails on the code before the fix (checked by reverting the fix
locally for the router, RiskManager, OrderManager and z-score fixes).

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | critical: RiskManager shrank a lock-in leg 1 (USDT_BOUNDS etc.) below what leg 2 can sell (1 USDT ~ 5x the 50,000 TMN minimum) | `risk/manager.py`: a lock-in leg 1 is re-landed with `lockin_leg_qtys` and approved only when leg 2 (coins after the coin-paid fee, leg-2 step, at the leg-2 bid) meets its minimum; else `MIN_NOTIONAL: lock-in leg 2 on ...` | `test_risk_manager.py::test_lockin_leg1_shrunk_below_what_leg2_can_sell_is_vetoed` (+3) |
| 2 | major: leg 2 re-crossed without limit (100.00 -> 97.00) | `order_manager.py`: `leg2` is a give-up purpose with 0 re-prices; S1 re-emits at the new bid and re-checks `leg2_max_loss_pct` (or degrades) | `test_order_manager.py::test_lockin_leg2_is_never_rechased_below_the_price_s1_checked` (+1), `test_strategies_engine.py::test_unfilled_leg2_is_not_chased_and_the_cycle_degrades` |
| 3 | major: leg-2 book not owned by S1 | `strategies/base.py::symbol_owners` (lock-in positions' `leg2_symbol`, working S1 entries' other book), router reserves an accepted entry's `leg2_symbol` in the tick; S3 `_owner` uses the same rule | `test_router.py` (3), `test_s3.py::test_s1_lockin_leg2_book_is_not_s3s` |
| 4 | major: revert gate assumed the whole deviation is captured, target measured from 0 | `s1_crossbook.py`: target = mean + (dev - mean) x 40 %, gate and `expected_edge_pct` = 60 % x \|dev - mean\| | `test_s1.py::test_revert_needs_the_captured_part_to_cover_cost_and_margin`, `::test_revert_target_is_measured_from_the_1h_mean` |
| 5 | major: leg-2 minimum checked on the gross qty; coarse-step remainder unhedged | `lockin_leg_qtys` (leg 1 lands on a leg-2 step after the coin fee), leg-2 minimum on the net qty, the remainder priced in: need = cost + margin + u x 3 % | `test_s1.py::test_leg2_minimum_is_checked_after_the_coin_fee`, `::test_leg1_qty_lands_on_a_leg2_step_after_the_coin_fee` |
| 6 | major: a failed restore force-wrote default state at shutdown / next ticks | `engine.py`: `_restored`; nothing persisted until the restore succeeded; the next tick retries the startup | `test_phase3_fixes.py::test_failed_restore_persists_nothing_and_is_retried` |
| 7 | major: synthetic feed shared the real-feed shadow state DB | `state_db_path(cfg, mode, feed)`: `state_shadow_synthetic`; `reset-kill --feed` | `test_phase3_fixes.py::test_state_db_path_never_shared_by_modes_or_feeds`, `::test_reset_kill_finds_a_killed_synthetic_account` |
| 8 | major: a path without `{mode}` was shared by shadow and live | `.<mode>` inserted before the suffix | same |
| 9 | major: candle inputs not replayable | append-only `engine_candles` (seq), `ticks.candle_seq`, the candle snapshot taken with the books at the tick start, startup load by the cache's own trim rule | `test_phase3_fixes.py::test_every_tick_records_the_candles_it_used`, `::test_candle_seq_continues_after_a_restart` |
| 10 | minor: revert exit reason misstated the target | the parenthetical now states target = mean + 40 % x (entry - mean) | `test_s1.py::test_revert_exit_when_the_deviation_closes` |
| 11 | minor: returns counted candles, not minutes | `features/engine.py::ret_by_time`; realized vol over consecutive minutes only | `test_features.py` (3) |
| 12 | minor: candle gaps not filled / not trimmed | `CandleCache.get` fills missing minutes flat (v 0), trims on every update | `test_strategies_engine.py::test_candle_cache_fills_missing_minutes_and_trims_on_every_update` |
| 13 | minor: stalled books flooded the z-score history | a sample only when all three books are fresh and one changed | `test_features.py::test_a_stalled_book_adds_no_zscore_samples` |
| 14 | minor: decision records showed a stale plan | `current_plan(now)` applies `ai.plan_stale_s`; health shows `plan_stale` | `test_phase3_fixes.py::test_stale_plan_is_not_logged_as_the_envelope_in_force` |
| 15 | minor: `wta run` shadow logs went to `logs/` | default `logs/shadow` for shadow, `logs` for live | `test_phase3_fixes.py::test_run_log_dir_defaults_per_mode`, `test_cli.py` |
| 16-19 | minor: STRATEGY.md (lines 7/29/402/512/578 status, 122 leg-1 cap, 144 leg 2 on fill, 580 leg 1 never re-crossed, TODO(S1) markers) | rewritten against the final code, plus every logic change above | - |

Consequence worth knowing: with the remainder charge a BTC lock-in at ~1M TMN needs ~0.64 %
instead of 0.58 % (u ~ 1.9 %), at half size ~0.70 %; DOGE (u ~ 4.5 %) practically never qualifies.

## Found while wiring: a lock-in leg 1 must not chase the market

The first version of the determinism scenario moved BTC by 1 USDT per tick. Leg 1 (a taker LIMIT
at the dislocated ask) then missed by one tick, rested, and the OrderManager re-crossed it 15 s
later at the *new* ask, after the gap had closed. The cycle bought at a price with no lock-in left,
degraded to revert (`lockin_degraded_to_revert`, leg 2 would have realised -0.55 %) and was sold
back for -13,776 TMN. Fix (`execution/order_manager.py`, `_max_reprices`): an entry with
`features.cost_model == "lockin"` is never re-priced or re-crossed; it rests at its own limit until
`limit_reprice_after_s` and is then given up (`EXPIRED_UNFILLED`), and S1 decides again with fresh
books. Test: `test_lockin_leg1_never_chases_a_market_that_moved_away`.

## Tests added (22)

* `tests/test_strategies_engine.py` (19): S2 initial allocation through risk + paper fill (order and
  outcome records, no churn); full S1 lock-in cycle (leg 1 -> leg 2 in the fill's tick -> closed ->
  outcome PnL); vetoed leg 2 retried on the next tick; leg 1 never chases; heartbeat diagnostics;
  router drops logged as `no_trade` once; strategies not asked while killed; restart restores
  strategy and feature state (z-score warm at once); feature state throttled to once a minute;
  per-mode state DB paths; shadow and live never share state or lock (+ synthetic candles into
  cache and state DB); `reset-kill` picks the killed mode; candle cache; candle refresher (26 h
  backfill in day windows, incremental refresh, rejected symbol); live-feed engine with the
  refresher and a restart that reloads candles; `--overlay` merging (order, `--set` wins, bad
  files); `config hash --overlay`; start event names overlays/strategies/state DB; shadow report.
* `tests/test_determinism.py` (3): S1 + S2 + S3 through the router plus S1 leg 2 on the fill give
  byte-identical intent JSON over 1000 iterations with shuffled dict orders; two engines on
  identical books write byte-identical `decisions` and `orders` logs (S2 fill + S1 cycle);
  `FeatureSnapshot.compact` is order-independent.
* Adjusted where the wiring changed behaviour: `tests/test_engine.py` (the synthetic wiring test
  now finds the engine state in the per-mode state DB and allows S2's initial order; `reset-kill`
  tests address the state DB), `tests/test_flatten_now.py` (the state DB / lock path).
* Review fixes: `tests/test_phase3_fixes.py` (7) plus the tests named in the table above; existing
  S1 tests updated to the fee-aware sizing and the mean-based revert (new spike books).

## Open items

* Shadow >= 4 h with S1 + S2 on the **real** feed (PRD-03 §9), once `api.wallex.ir` is reachable.
* `configs/markets.yaml`: S1's coin list has ADA, TRX, LTC, LINK, AVAX without TMN/USDT books in the
  watchlist; either add their pairs or trim the list (config owner).
* Docs that still describe the Phase-2 wiring (no strategies, state in `data.sqlite_path`, no
  `--overlay`): `docs/RUNBOOK.md` §0 and file layout, `docs/ARCHITECTURE.md`, `docs/RISK.md` (lock
  path), `docs/DECISION_LOG_FORMAT.md` (known gaps), `docs/PROGRESS.md` Phase 3.
* A taker fill that arrives in the placement answer (live Wallex) reaches S1's `on_fill` on the
  next tick's poll (OrderManager carry), i.e. up to one fast tick (2 s) before leg 2 goes out.
* `src/wta/status.py` (`resolve_state_db`) and the TUI's `--db` default must follow
  `wta.engine.state_db_path(cfg, mode, feed)` (feed suffix, `.<mode>` insertion); `docs/RISK.md`,
  `docs/RUNBOOK.md`, `docs/DECISION_LOG_FORMAT.md` need the lock-in leg-2 check, the leg-2 give-up,
  the per-feed state DB, `logs/shadow` and `engine_candles` / `candle_seq`.

---

## Appendix: shadow report of run (a)

### Shadow report — 2026-10-22

> **SYNTHETIC — not evidence.** This run used the offline synthetic market feed (`--feed synthetic`). It exercises the engine end to end; its trades, PnL and opportunity counts say nothing about Wallex or about strategy performance.

Generated by `scripts/shadow_report.py` from `logs4/` and the state DB `state4.shadow_synthetic.sqlite`. Every number below comes from those files.

#### Window

- from 2026-10-22T18:06:11.052Z to 2026-10-22T18:16:11.985Z: 0.17 h (601 s)
- engine runs: 1 start event(s), 1 stop event(s)
  - stop at 2026-10-22T18:16:11.985Z: ran for 600 s (uptime 600.925 s, 299 ticks, 11 heartbeats)

#### Feed

**Synthetic feed** (see the banner above): not evidence of anything about Wallex.

| start | mode | feed | synthetic | commit | config | overlays | strategies | rules-only | reason |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-22T18:06:11.052Z | shadow | synthetic | yes | 667d781468b0 (dirty) | 58c570d67cea | - | S1, S2 | yes | fresh |

#### Heartbeats

- 11 heartbeats, median interval 60 s
- gaps longer than 2.5x the median: 0
- heartbeats killed: 0, paused: 0
- health reasons: none (every heartbeat healthy)

#### Equity

Source: state DB `equity` table (60 rows).

| metric | value |
|---|---|
| start equity | 5,000,000.00 TMN |
| end equity | 4,994,936.78 TMN |
| peak equity | 5,000,000.00 TMN |
| PnL | -5,063.22 TMN (-0.101 %) |
| max drawdown | 0.101 % |
| equity points | 60 |

#### Decisions

| kind | strategy | records |
|---|---|---|
| order | S2 | 2 |
| veto | S1 | 1 |

Outcome updates: 2.

#### Vetoes

| risk check | vetoed decisions |
|---|---|
| USDT_BOUNDS | 1 |

#### Fills and closed positions

| symbol | side | fills |
|---|---|---|
| USDTTMN | BUY | 1 |

Closed positions: 0 (win rate n/a, closed-trade PnL 0.00 TMN).

#### Fees

- fees paid 3,120.18 TMN = 3.120 % of the fee budget (risk.fee_budget_pct x initial equity; the RiskManager's FEE_BUDGET check)
- orders today (last heartbeat): 4, last hour: 4
- realized PnL (last heartbeat): 0.00 TMN

#### S1 opportunities

An opportunity tick is a fast tick where a lock-in edge reached cost + margin on either side (the engine's counters, sampled by the heartbeats). Best excess = max(edge - need) seen.

| coin | ticks evaluated | opportunity ticks | best excess | last why-no-trade |
|---|---|---|---|---|
| BTC | 299 | 0 | -0.652 % | no lock-in: TMN-cheap edge -0.138% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.091% < cost 0.430% + margin 0.150%; revert: |z| 1.14 |
| DOGE | 299 | 0 | -0.667 % | no lock-in: TMN-cheap edge -0.130% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.120% < cost 0.430% + margin 0.150%; revert: |z| 0.19 |
| ETH | 299 | 2 | 0.116 % | cooldown: last ETH entry 2s ago < 60s |
| SOL | 299 | 0 | -0.755 % | no lock-in: TMN-cheap edge -0.238% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.246% < cost 0.430% + margin 0.150%; revert: |z| 0.28 |
| XRP | 299 | 0 | -0.661 % | no lock-in: TMN-cheap edge -0.174% < cost 0.430% + margin 0.150% and USDT-cheap edge -0.081% < cost 0.430% + margin 0.150%; revert: the exit |

S1 entries: 1 decided, 0 placed; closed S1 cycles: 0.

#### S2 rebalances

| ts | kind | side | qty USDT (intent / order) | price | share before | target | initial | status | filled |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-22T18:06:11.256Z | order | BUY | 9.37 / 4.68 | 266721 | 0.0 % | 50.0 % | yes | UNFILLED | 0 |
| 2026-10-22T18:08:57.682Z | order | BUY | 9.37 / 4.68 | 266770 | 0.0 % | 50.0 % | yes | FILLED | 4.68 |

#### Errors

- events: none of tick_error, kill, pause, resume, flatten, reset_kill, reset_kill_applied
- tick crashes (last heartbeat): 0, task crashes: {}
- max API errors in one minute: 0
- order errors (orders log): 0

#### Logs check

logs check: OK (4 files, 32 lines)
