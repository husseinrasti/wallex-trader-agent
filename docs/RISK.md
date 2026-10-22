# Risk management

> **Summary**
> 1. Every intent goes through `RiskManager.evaluate` (`src/wta/risk/manager.py`) before it can become an order. It runs 23 checks in a fixed order and logs all of them, also the ones that pass.
> 2. *Hard* checks veto the intent. *Adjust* checks only reduce the quantity. A final `MIN_NOTIONAL` check runs after all adjustments, so no approved order is below the exchange minimum.
> 3. Rolling limits: a drawdown of 5 % or more kills the bot (manual reset only). A daily loss of −2.5 % pauses entries until the next UTC day. Anomalies pause entries until they have been gone for 60 s.
> 4. From T−2h before the end, a SELL-only flatten moves the account to 100 % TMN. At T−30min it uses MARKET or crossing orders. At T−5min the `final_state` is logged.
> 5. The AI only writes clamped plans and bounded edits to non-risk parameters. It cannot place orders or change a risk rule. **No live Wallex data exists yet:** `api.wallex.ir` is unreachable from the dev machine, so every run so far used the synthetic feed.

Code is the reference. Where it differs from `PRD-02-risk-execution.md`, this document follows the
code and lists the differences in §15. Every config name below is the key in `configs/default.yaml`,
shown with its default value.

---

## 1. Status and evidence

| What | Where | State at the time of writing |
|---|---|---|
| Unit tests: one or more per check, adjust vs veto, kill/pause/resume, deadline phases, a random-intent property test | `tests/test_risk_manager.py` (75 tests) | pass |
| Flatten: routing, phases, dust, never buys, idempotent | `tests/test_flatten.py` (25), `tests/test_flatten_now.py` | pass |
| Live guard, reconcile/restart, reviewer bounds, MCP read-only | `tests/test_safety.py`, `test_reconcile.py`, `test_config_manager.py`, `test_mcp_client.py` | pass |
| End-to-end: kill after a −8 % shock, flatten, `reset-kill`, restart | `docs/REPORTS/phase2_acceptance.md` runs (c), (e) | pass, **synthetic feed only** |
| `STOP_LIMIT` accepted by Wallex REST | `scripts/check_account.py --probe-order` (run by the operator) | **not verified**: the API is unreachable |

The risk layer is the same in every mode (`backtest`, `shadow`, `live`, `replay`). `RiskManager.evaluate`
is pure: it reads only the intent, a `RiskContext` and the config. Its only clock is `ctx.now`.

## 2. Where the RiskManager sits

```
intent sources                       Engine._process                      OrderManager.submit
 position exits (stop/target/time) ─┐   decision = risk.evaluate(...)  ─▶   vetoed  -> decision log only
 flatten (deadline or kill)        ─┼─▶ (one RiskDecision per intent)  ─▶   approved -> exchange order(s)
 strategies (Engine.intent_sources)─┤
 injected test intents             ─┘
```

`Engine._process` is the only code that calls `OrderManager.submit`, so no intent source can skip the
check. At commit `b943545`, `Engine.intent_sources` is empty by default (the "S0 no-op strategy"). The
S1–S3 strategies and the AI layer exist as tested modules, and connecting them to `wta run` is being
done separately. Whatever gets connected still passes through `_process`.

The exchange can still receive orders that `evaluate` did not see one by one. All of them are listed here:

* **Follow-ups of an approved order.** Re-prices, spread crossings and remainders after partial fills
  continue a logical order that was already approved. `RATE_*` reserves these extra placements when
  the order is approved (see check 13).
* **Protective stops.** When an entry fills, the OrderManager places the position's server-side
  `STOP_LIMIT` SELL. Its decision record carries a single check, `PROTECTIVE_STOP`. A stop can only
  reduce exposure.
* **The API probe.** `scripts/check_account.py --probe-order` places two orders far from the market
  and cancels them (client-id prefix `PR`). Only the operator runs it, after typing `PROBE`. The
  assistant never runs it (`DECISIONS.md`).

## 3. Terms used in the check table

* **Entry-like** means the purpose is `entry` or `rebalance`, or anything that is not risk-reducing
  (for example `probe`). **Risk-reducing** means `exit`, `stop`, `leg2` or `flatten`. A **BUY is always
  entry-like**, whatever its purpose, because every risk-reducing order in this codebase is a SELL:
  positions are long only and flatten sells. Exposure limits apply only to entry-like intents, so the
  guardian never stops the bot from getting smaller.
* **Taker** means one of three things: the intent is flagged `taker`, it is a `MARKET` order, or it is
  a `LIMIT` whose price already crosses the book (a BUY at or above the best ask, a SELL at or below
  the best bid).
* **Reference price** (`ref`) is the limit price. If there is none, it is the price we would cross at
  (best ask for a BUY, best bid for a SELL). `MARKET` and `STOP_MARKET` orders always use the opposite
  best, because the exchange ignores their price.
  `qty = intent.qty`, or `intent.notional / ref` when no qty is given.
* **TMN values** convert USDT at the USDTTMN **bid**, the same conservative side the equity uses:
  ```
  equity_tmn = TMN.total + USDT.total × bid(USDTTMN) + Σ coin.total × bid(coinTMN)
               (fallback for a coin without a TMN book: bid(coinUSDT) × bid(USDTTMN))
  ```
* **Fail closed / fail open.** When data is missing (no equity, no book, no USDTTMN price), entries
  are vetoed and risk-reducing orders are allowed. The one exception is `STALE_DATA`, which also
  blocks `exit` and `leg2`.
* Open orders count. Coin BUYs still open count as coins already held. Open USDT-moving orders count as
  USDT already moved. Open entries reserve the re-prices they may still make.

## 4. The check table (evaluation order, exactly as in `RiskManager._checks`)

| # | Code | Type | Applies to | Rule (config key = default) |
|---|---|---|---|---|
| 1 | `KILLED` | hard | all | If the kill-switch is on, only SELLs with purpose `flatten`, `exit` or `stop` pass. Everything else fails, including `leg2` and every BUY. |
| 2 | `PAUSED` | hard | entry-like | If the engine is paused, entry-like intents fail. `exit`, `stop`, `leg2` and `flatten` SELLs pass. |
| 3 | `DEADLINE` | hard | all | `phase = max(clock phase, flags.flatten_phase)`. Phase 1 starts at `end − engine.flatten_start_before_end_s` (7200): entry-like intents fail. Phase 2 starts at `end − engine.flatten_final_before_end_s` (1800): only `flatten` SELLs pass. Phase 3 starts at the end: everything fails. With no `CHALLENGE_END_UTC` the phase is 0. |
| 4 | `BANNED_MARKET` | hard | all | Fails if the symbol is in `markets.banned` (`configs/markets.yaml`, the organizer's list copied verbatim, 345 symbols) or is not in `/v1/markets`. |
| 5 | `MARKET_ORDER_DISABLED` | hard | `MARKET`, `STOP_MARKET` | Fails where the market has `isMarketTypeEnable=false`. The detail tells the caller to retry as a crossing LIMIT. |
| 6 | `MIN_NOTIONAL` | hard | all | `q = round_down(qty, step)` must satisfy `q > 0`, `q ≥ minQty` and `q × ref ≥ minNotional`. The exchange value is used; if it is 0, the fallback is `risk.min_notional_tmn` (50,000) or `risk.min_notional_usdt` (1). Also fails if the intent cannot be sized. |
| 7 | `MAX_ORDER_PCT` | adjust | entry-like | `qty × price_tmn ≤ risk.max_order_pct_equity` (0.25) `× equity`. Otherwise `qty := cap / price_tmn`. Fails closed if equity or the TMN price is unknown. |
| 8 | `BEST_LEVEL_QTY` | adjust | taker orders | `qty ≤ risk.max_order_pct_of_best_level_qty` (0.8) `× opposite best-level qty`. For risk-reducing orders the cap is never below the smallest sellable quantity. An entry with no opposite level fails. |
| 9 | `COIN_EXPOSURE` | adjust / hard | entry-like BUY of a coin (base not TMN/USDT) | `coins held + our open coin BUYs + this ≤ risk.max_coin_exposure_pct` (0.40) `× equity`. Otherwise the quantity is reduced to the remaining room. Fails if that room is below the minimum order, or if a value is unknown. |
| 10 | `USDT_BOUNDS` | adjust / hard | entry-like intents that change the USDT holding | The post-trade USDT share must stay inside `strategies.s2_usdtalloc.bounds` (`[0.25, 0.70]`). Our open USDT-moving orders count as filled. For a lock-in entry, the whole cycle counts: leg 1 plus the leg-2 SELL it commits to. If the share is already outside the band, a trade that moves it further away fails and a trade toward the band passes. A trade that would cross a bound is reduced so it stops at the bound. Fails if the room is below the minimum order. |
| 11 | `MAX_POSITIONS` | hard | purpose `entry` (adding to an existing linked position passes) | `positions + pending entry orders < risk.max_concurrent_positions` (3). Per strategy, below `risk.max_positions_per_strategy` (`{S1: 2, S3: 1}`). |
| 12 | `MAX_OPEN_ORDERS` | hard | entry-like | Our non-terminal open orders, resting stops included, must number fewer than `risk.max_open_orders` (8). |
| 13 | `RATE_HOUR` | hard | entry-like | `n_60min + reserved + (1 + execution.limit_max_reprices) ≤ risk.max_orders_per_hour` (10). `n` counts **every** placement of any purpose in the rolling 3600 s. `reserved` is the sum of the re-prices our open entry-like orders may still make. |
| 14 | `RATE_DAY` | hard | entry-like | The same sum over the current UTC day must be `≤ risk.max_orders_per_day` (60). |
| 15 | `DAILY_LOSS` | hard | entry-like | `daily_pnl_pct > −risk.daily_loss_limit_pct` (0.025). |
| 16 | `DRAWDOWN_KILL` | hard | entry-like | `drawdown_pct < risk.max_drawdown_kill_pct` (0.05). The kill itself is triggered in the rolling state (§6). |
| 17 | `FEE_BUDGET` | hard | entry-like | `fees_paid + expected_fee ≤ risk.fee_budget_pct` (0.02) `× engine.initial_equity_tmn` (5,000,000). See §9. |
| 18 | `EDGE_VS_COST` | hard | purpose `entry` | `expected_edge_pct ≥ cost + margin` (formula below). An entry without `expected_edge_pct` fails. `rebalance` is n/a. |
| 19 | `STALE_DATA` | hard | all except `stop` and `flatten` | Each book used must exist and be at most `engine.stale_data_s` (30) seconds old. "Books used" means the symbol, `features.books_used`, and USDTTMN when the quote is USDT or the strategy is S1. |
| 20 | `PRICE_SANITY` | hard | LIMIT with a price; entries carrying `dev_mid_pct` | Entry-like: `abs(price / mid − 1) ≤ risk.price_sanity_pct` (0.03), and a missing mid fails. Closing SELL: only a floor, `price ≥ bid × (1 − 0.03)`. MARKET, STOP_LIMIT and unpriced orders are n/a. An entry also fails if its cross-book deviation is `abs(dev_mid_pct) ≥ risk.price_disagreement_pause_pct` (0.05). |
| 21 | `SELF_CROSS` | hard | all | A BUY must be priced below our lowest open SELL in the symbol, and a SELL above our highest open BUY. Resting stops count, at their limit price or else their stop price. A closing SELL ignores its own position's entry BUY, which the OrderManager cancels first. An unpriced opposite order of ours fails the check. |
| 22 | `BALANCE` | adjust / hard | all (when the intent can be sized) | BUY: `qty × ref × (1 + fee) ≤ free_quote × (1 − risk.balance_buffer_pct)` (0.005). SELL: `qty ≤ free_base`, with no buffer. Otherwise the quantity is reduced. Fails if nothing tradable is left. |
| 23 | `DUPLICATE` | hard | all (rules differ by purpose) | (a) Fails if an open order has the same `(strategy, symbol, side, purpose)`; `leg2` is exempt. (b) Entry-like only: fails if the same key was approved less than `risk.duplicate_cooldown_s` (60) seconds ago. (c) `exit`/`stop`: fails if another exit or stop SELL of the same strategy is open in the symbol. A position's own protective stop never counts. |
| — | `MIN_NOTIONAL` (trailing) | hard | intents that can be sized | Runs after all checks. If the adjusted quantity no longer meets check 6, a failed `MIN_NOTIONAL` "after adjustments: …" is appended (only when check 6 had passed). |

Cost used by `EDGE_VS_COST` (`round_trip_cost`, `_entry_cost`, `_margin_min`):

```
entry_fee  = taker_fee(symbol) if taker else maker_fee(symbol)
lock-in    : cost = entry_fee + taker_fee(leg2_symbol)              # edge is measured at executable bid/ask
otherwise  : cost = entry_fee + taker_fee(symbol) + spread(symbol)  # same-book round trip, taker exit
margin     = max(features.margin_min_pct, strategies.s1_crossbook.margin_min_pct (0.0015) if S1, 0)
pass if    : expected_edge_pct >= cost + margin
```

Fees come from `/v1/account/fee` in a live run and from `configs/fees_snapshot.yaml` offline. TMN pairs
(USDTTMN included) cost 0.25 % maker and 0.30 % taker. USDT pairs cost 0.10 % maker and 0.13 % taker.

## 5. Adjust vs veto

* **All 23 checks always run.** Nothing short-circuits. A check that does not apply is logged as passed
  with detail `n/a (<why>)`, so every decision record carries the full checklist.
* `approved = no failed check`. `veto_reason = "<first failed code>: <detail>"`. The decision log also
  keeps the full list (`failed_codes`).
* An adjust check that reduces the quantity **passes**, with detail `adjusted qty A -> B: <why>`. Each
  later check sees the reduced quantity, so adjustments compound in table order: `MAX_ORDER_PCT` →
  `BEST_LEVEL_QTY` → `COIN_EXPOSURE` → `USDT_BOUNDS` → `BALANCE`.
* An adjust check becomes a **hard failure** when even the remaining room is below the minimum order
  (`COIN_EXPOSURE`, `USDT_BOUNDS`, `BALANCE`), or when it cannot value the trade.
* `adjusted_qty = round_down(final qty, step)`. `MIN_NOTIONAL` (check 6) runs *before* the adjust
  checks, so it is repeated at the end:

```
if sized and size_problem(round_down(qty)) and MIN_NOTIONAL had passed:
    append RiskCheck("MIN_NOTIONAL", passed=False, "after adjustments: <problem>")
```

The OrderManager places `decision.adjusted_qty`. A closing SELL can be reduced further, to the part
of the position that no other live SELL already covers. In acceptance run (b), a `MAX_ORDER_PCT` check
that passed without adjusting reads `notional 663,414 TMN <= 25% of equity = 1,283,782 TMN`.

## 6. Rolling state: kill, pause, resume

`RiskManager.update_rolling_state(ctx, anomalies)` runs once per fast tick (every 2 s). It returns
`kill` / `pause` / `resume` events. The engine applies each event to `EngineFlags`, persists it and
writes it to `events.jsonl`. The engine adds two kill triggers of its own.

| Trigger | Condition | Effect | How it ends |
|---|---|---|---|
| Drawdown kill | `(peak − equity) / peak ≥ risk.max_drawdown_kill_pct` (0.05) | kill, emitted once | **Manual only**: `wta reset-kill`. Never resumed automatically. |
| `KILL` file | a file named `KILL` exists in the repo root (`touch KILL`) | kill (`source: kill_file`) | Manual only. `reset-kill` refuses to run while the file exists. |
| Exception storm | `≥ engine.exception_storm_count` (3) fast-tick crashes within `engine.exception_storm_window_s` (600 s) | kill (`source: exception_storm`). If the kill procedure itself fails, the flag is still set. | Manual only |
| Daily loss | `equity / day_start_equity − 1 ≤ −risk.daily_loss_limit_pct` (0.025) | pause with `until = next 00:00 UTC` | Resumes at the new UTC day. If an anomaly is active then, the pause becomes an anomaly pause. |
| Anomaly | any one of: `≥ risk.consecutive_api_errors_pause` (5) consecutive API errors; USDTTMN book missing or older than 30 s; `abs(XTMN / USDTTMN ÷ XUSDT − 1) ≥ 0.05` (fresh mids); a held asset has no price | pause | Resumes only after **all** conditions have been clear for `risk.anomaly_clear_s` (60 s). The timer restarts whenever a condition comes back. |
| Other pauses | `pause_reason` not starting with `daily_loss` / `anomaly` | — | The RiskManager never resumes them. |

Precedence: a kill outranks every pause (no rolling events while killed), and a daily-loss pause
replaces an anomaly pause. Drawdown and daily PnL use the last **usable** valuation, meaning every
held asset has a price and equity is above 0. Peak equity and day-start equity also move only on a
usable valuation. A book that has not arrived yet, or an empty balance read, therefore cannot look
like a crash.

```
drawdown_pct  = max((peak_equity − equity) / peak_equity, 0)
daily_pnl_pct = equity / day_start_equity − 1          # day_start_equity reset at 00:00 UTC
```

**What a kill does** (`Engine._kill`):

1. Sets and commits `killed` at once.
2. Writes a `kill` event with source, equity, peak, drawdown, positions and orders.
3. Cancels open `entry`/`rebalance` orders.
4. Starts the kill-switch flatten (§7, phase 1 behaviour, `exit_reason=kill`). After that, strategies
   are no longer called, position exits (stops, targets, time stops) keep running, and heartbeats
   continue: the bot stays in monitoring mode.

**What a pause does:** it cancels working `entry`/`rebalance` orders, so nothing keeps re-pricing an
entry without a risk check. Exits continue.

**Reset** (`python -m wta reset-kill --reason "..."`, `src/wta/cli.py`):

1. `--reason` is required.
2. The command refuses while the `KILL` file exists.
3. It writes `killed=false`, moves the stored peak to the latest equity row (so the old drawdown does
   not kill again at once), and writes a `reset_kill` event to `events.jsonl` and to the store's
   `events` table.
4. A running engine applies the reset on its next tick (`reset_kill_applied` event). Otherwise the
   next start applies it.
5. Kill-flatten orders are canceled at the reset unless a deadline phase or a manual flatten still
   needs them.

A daily-loss pause can still be active after a reset. Acceptance run (c) shows exactly that.

Telegram `/kill`, `/pause`, `/resume` and `/status` are implemented in `src/wta/utils/telegram.py`
(operational only, no order commands). At this commit the engine does not start the Telegram listener,
so the `KILL` file is the operator's kill path.

## 7. Flatten to TMN

Only TMN counts at the official end. `FlattenManager.plan` (`src/wta/risk/flatten.py`) runs every tick
while a flatten phase is active and returns SELL intents. These go through the RiskManager and the
OrderManager like every other intent. The deadline code is `Engine._deadline` and `Engine._flatten_phase`.

| Time | Phase | Engine | Orders |
|---|---|---|---|
| T−2h (`engine.flatten_start_before_end_s` = 7200) | 1 | `flatten` event. Open `entry`/`rebalance` orders are canceled. Strategies and the router stop emitting entries, and `DEADLINE` vetoes any entry-like intent. Exit and `leg2` intents still pass. | Maker-first LIMIT SELL (OrderManager: best ask − `execution.maker_offset_ticks` (1) tick while that stays above the best bid). Re-priced every `execution.limit_reprice_after_s` (15 s), at most `execution.flatten_max_reprices` (3) times. After that it crosses the spread and re-crosses until filled. |
| T−30min (`engine.flatten_final_before_end_s` = 1800) | 2 | Only `flatten` SELLs pass. Position exits are no longer generated: flatten sells the position coins itself. | Taker: `MARKET` where `isMarketTypeEnable`, else a LIMIT at `best_bid × (1 − execution.flatten_final_cross_pct)` (0.005). |
| T−5min (`engine.final_state_before_end_s` = 300) | 2 | `final_state` event: balances, equity, TMN share, `non_tmn_over_min`, `dust`, `complete`. The text is `ALERT: not flat yet: …` if anything sellable is left. | — |
| T (`CHALLENGE_END_UTC`) | 3 | Every open order is canceled and `DEADLINE` vetoes everything. Heartbeats continue. | none |

Rules (all in `flatten.py`):

* **Route.** Coin X is sold on `XTMN` if that market is listed and not banned, else on `XUSDT`. USDT is
  sold on `USDTTMN`. USDT that comes back from a coin sale is sold in a later round. An asset with no
  allowed market is reported as `no_market`, never traded.
* **Order of sales.** Each round sells at most one unit per asset, in this order:
  1. coins held by no position (nothing protects them);
  2. one position's coins, with `linked_position_id` set, so the OrderManager cancels that position's
     server stop right before the sale and the position closes with `exit_reason` `flatten` or `kill`;
  3. the whole balance, when no smaller part reaches the minimum order.
* **Dust.** A unit is sold only if `round_down(qty) ≥ minQty` and `qty × bid ≥ minNotional` (exchange
  value, else the config fallback). Anything smaller is reported as `dust` and left as is. Flatten
  **never buys** to top up dust. `assert_sell_only` raises if a plan ever contains a non-SELL or
  non-`flatten` intent. It is an explicit `raise`, not an `assert`, so `python -O` cannot remove it.
* **Idempotent and restart-safe.** `plan` is a function of balances, positions and our open orders. An
  asset that already has one of our non-stop SELLs working is skipped ("working"). Calling `plan` again,
  even after a restart, simply continues. Phases only move forward and are persisted in
  `engine_flags.flatten_phase`. After a late restart, every missed boundary is applied in order
  (`flatten_phase_transitions`): cancel entries first, then go final.
* **Risk checks still apply.** `KILLED` and `DEADLINE` let `flatten` SELLs through. `STALE_DATA` is n/a
  for `flatten`. `PRICE_SANITY` only sets a floor 3 % below the bid. `BALANCE` reduces the quantity to
  the free coins. `BEST_LEVEL_QTY` caps a taker sale at 80 % of the bid level, but never below one
  minimum order; the remainder goes out the next round.
* **Kill-switch flatten** uses phase 1 behaviour (maker-first, then crossing). It escalates to phase 2
  only when the deadline reaches T−30min.
* **Manual flatten:** `scripts/flatten_now.py`. Phase 1 lasts `--phase1-seconds` (default 60), then phase
  2. It stops when only TMN and dust are left. Live use requires the same guards as §13, except
  `CHALLENGE_END_UTC`.

## 8. Sizing math and caps

Strategies propose a size, and the RiskManager can only shrink it (checks 7–10 and 22).
`mult` is the size multiplier of a **fresh** plan (not older than `ai.plan_stale_s` = 1200 s), clamped
to `[0, 1]`. With no fresh plan, `mult = 1`.

```
S1 (lock-in / revert), src/wta/strategies/s1_crossbook.py::_size_qty
  qty = min( strategies.s1_crossbook.size_pct_equity (0.20) × equity_tmn × mult / price_tmn,
             0.8 × leg-1 best-ask qty            (taker leg 1),
             0.8 × leg-2 best-bid qty            (lock-in),
             free_quote × (1 − 0.005) / price / (1 + taker_fee) )
  rounded down to the coarser step of the two legs

S2 (USDT allocation), s2_usdtalloc.py
  rebalance only if |usdt_share − target| ≥ rebalance_threshold_pp (0.15), target ∈ bounds [0.25, 0.70]
  notional_tmn = |usdt_share − target| × equity_tmn ;  qty = round_down(notional_tmn / price)
  (MAX_ORDER_PCT caps one order at 25 % of equity, so a large gap closes over several rebalances)

S3 (momentum, disabled by default: strategies.s3_momentum.enabled = false), s3_momentum.py::_size
  equity_usdt = equity_tmn / bid(USDTTMN)
  qty = min( risk_per_trade_pct (0.005) × equity_usdt / (stop_atr_mult (1.5) × ATR14),
             max_size_pct_equity (0.20) × equity_usdt / price ) × mult
  capped by free_USDT × (1 − 0.005) / (price × (1 + maker_fee))
```

Caps at the reference equity of 5,000,000 TMN:

| Limit | Formula | Value |
|---|---|---|
| One order (entry-like) | 0.25 × equity | 1,250,000 TMN |
| S1 size budget | 0.20 × equity × mult | ≤ 1,000,000 TMN |
| Coin exposure (held + open BUYs) | 0.40 × equity | 2,000,000 TMN |
| USDT holding band | [0.25, 0.70] × equity | 1,250,000 – 3,500,000 TMN |
| S3 risk at the stop (before fees and slippage) | 0.005 × equity | 25,000 TMN |
| Daily-loss pause | −0.025 × day-start equity | −125,000 TMN |
| Drawdown kill | 0.95 × peak | 4,750,000 TMN at a 5,000,000 peak |
| Positions | 3 total; S1 ≤ 2; S3 ≤ 1 | — |
| Fee budget | 0.02 × initial equity | 100,000 TMN |

For the first 6 h of live trading, `configs/live_halfsize.yaml` halves the size:
`risk.max_order_pct_equity: 0.125` and `strategies.s1_crossbook.size_pct_equity: 0.10`. A config
change shows up as a new `config_hash` in every decision.

Stops and time stops per strategy: S1 revert uses `revert_stop_pct` 1.0 % and `revert_max_hold_s` 1200 s.
S3 uses `stop_atr_mult` 1.5 ATR, a software target at `target_atr_mult` 2.5 ATR, and `max_hold_s` 14400 s.
An S1 lock-in sends leg 2 only if the vanished edge costs less than `leg2_max_loss_pct` (0.003).
Otherwise the cycle becomes a stopped revert position.

## 9. Fee budget

```
expected_fee = qty × price_tmn × (taker_fee if taker else maker_fee)
               + qty × price_tmn × taker_fee(leg2_symbol)      # lock-in entries only
veto entry-like if  fees_paid_tmn_total + expected_fee > risk.fee_budget_pct × engine.initial_equity_tmn
                                                         = 0.02 × 5,000,000 = 100,000 TMN
```

* `fees_paid_tmn_total` sums every fill's fee, converted to TMN at fill time. It is persisted, so a
  restart does not reset it.
* Only entry-like intents are checked. Exits, stops, `leg2` and flatten are never blocked by the
  budget, but their fees count toward it.
* For a plain (non-lock-in) entry only the entry fee is projected. Exit, stop and flatten fees are
  counted when they are paid, so they can push the total above 100,000 TMN. The budget only stops new
  entries.
* Scale: an S1 lock-in where the TMN book is cheap costs 0.30 % + 0.13 % = 0.43 %, which is 4,300 TMN
  on 1,000,000 TMN, so about 23 such cycles use up the budget. A maker S2 rebalance of 1,250,000 TMN
  on USDTTMN costs 3,125 TMN.
* For each single trade, `EDGE_VS_COST` is the fee guard: an entry must expect more than its full cost.

## 10. Anti-wash and self-trade controls

| Control | Where | Rule |
|---|---|---|
| `SELF_CROSS` | RiskManager, check 21 | A new order may never trade against one of our resting orders (see the table). |
| One side per symbol | `OrderManager._opposite_open` | Never a BUY and a SELL of ours open at once in one symbol. A placement that would cause this is refused and logged `REFUSED: SELF_CROSS_GUARD`. Exception: our resting server stops. They rest below the market, are canceled before any other SELL of their position, and `SELF_CROSS` still sees them. |
| Closing-SELL hygiene | OrderManager | Before a SELL that closes a position: the position's open BUYs in that symbol are canceled, and its server stop is canceled; if that cancel fails, the SELL is refused. A position is never covered by two live SELLs. |
| One strategy per symbol | `StrategyRouter` | One open order per (strategy, symbol), and one strategy per symbol at a time. After any fill on a symbol, a cooldown of `engine.symbol_cooldown_s` (30 s) applies. |
| `DUPLICATE` | RiskManager, check 23 | No second order for the same (strategy, symbol, side, purpose), and a 60 s cooldown for entries. |
| Our `client_id` | `src/wta/utils/ids.py` | Format `^(S1\|S2\|S3\|FL\|EX\|PR)_[A-Z0-9]{2,18}_\d{13}_[A-Z0-9]{4}$`, at most 40 characters. Separators are `_` because Wallex accepts only letters, digits and `_`. Prefixes: `S1`/`S2`/`S3` strategies, `FL` flatten, `EX` generic exit or re-placement, `PR` API probe. The nonce is `sha1(decision_id, attempt)`, so replay regenerates the same ids. |
| Foreign orders | Reconciler | An open order that does not match the pattern is logged once as `foreign_order` and **never touched**. An order with our prefix but no stored request is adopted and canceled (`ADOPTED_UNKNOWN`). |
| Algorithmic origin | decision log | Every order has a decision record with intent, checks, `git_commit` and `config_hash`. |

Foreign orders are not in our order book, so `SELF_CROSS` cannot see them. The account must never be
traded by hand. The challenge forbids it, and `GO_LIVE_CHECKLIST.md` allows only read-only use of the
web UI.

## 11. Server-side stops vs software fallback

* **Default: server-side** (`execution.use_server_side_stops: true`). When an entry with a `stop_price`
  fills, the OrderManager places a `STOP_LIMIT` SELL for the position quantity:
  `stop = stop_price`, `limit = stop × (1 − execution.stop_limit_slippage_pct)` (0.004), rounded as a SELL.
  A server stop also protects the position while the process is down. Resting stops are polled every
  5 × `execution.poll_interval_s` (10 s).
* **Software fallback** (`position.meta.stop_mode = "software"`) is used for one position when:
  * the config switch is off;
  * the exchange rejects the stop;
  * a closing SELL is already live;
  * the bid is already at or below the stop;
  * the stop quantity is below the minimum order.

  Then `PositionManager.check_exits` sends a taker stop exit as soon as `bid ≤ stop`. The check runs
  once per fast tick, so the delay is about 2 s plus order latency. There is no protection while the
  process is down; the reconcile at restart closes positions whose coins are already gone.
* **Stranded stop.** If the price gaps through the `STOP_LIMIT`'s limit (`bid < limit`), that stop can
  no longer fill. The PositionManager then sends a taker stop exit anyway, and the OrderManager cancels
  the stranded stop first. Acceptance run (c) shows this after the synthetic −8 % gap.
* **Why the S3 target is software** (`DECISIONS.md`, 2026-10-22). A resting `STOP_LIMIT` SELL most
  likely locks the coins, so a second resting SELL (a target LIMIT) for the same quantity would be
  rejected for balance. Keeping only one SELL resting per position also keeps the self-cross rules
  simple. The PositionManager therefore watches `bid ≥ target`, and the closing SELL cancels the stop
  first.
* **Not verified yet.** Nobody knows yet whether Wallex REST accepts `STOP_LIMIT` (`stop_Price` field):
  the probe could not run because the API is unreachable. If it is rejected, the fallback takes over
  per position automatically, and `GO_LIVE_CHECKLIST.md` step 3 sets `use_server_side_stops: false`.

## 12. Restart safety

One fast tick is **one SQLite commit**: orders, fills, positions and kv state move together. Two
writes commit immediately: a write-ahead record committed right before each order is sent, and the
kill flag.

| Persisted | Store location |
|---|---|
| Peak equity, day-start equity and its timestamp, total fees, placement timestamps (rate limits), realized PnL by strategy | kv `portfolio_state` |
| `killed`, `kill_reason`, `paused`, `pause_reason`, `paused_until_ts`, `flatten_phase` | kv `engine_flags` |
| Live orders and their fill baselines; write-ahead `UNKNOWN` records of in-flight requests | kv `order_manager_state`, `orders` table |
| Open positions, including stop client id and stop mode | `positions` table |
| Equity row every `engine.equity_row_interval_s` (10 s) | `equity` table; after a restart the peak is `max(stored peak, highest row)` |
| Manual kill resets; paper account (shadow) | kv `kill_reset`; kv `paper_state` |

Not persisted: the anomaly clear timer (after a restart a resume needs another full 60 s clear), the
crash window of the exception storm, and the veto-repeat memo.

**Startup sequence** (`Engine.startup`):

1. `start` event (`reason: restart` or `fresh`).
2. `startup_restore`.
3. Apply any pending kill reset.
4. Seed balances.
5. Wait up to `engine.stale_data_s` for the USDTTMN book and the books of our positions and orders.
6. **Startup reconcile**: fills missed while the process was down are booked once, deduplicated by
   trade id. Our untracked orders are adopted; foreign orders are logged. Positions whose coins are
   gone are closed as `stop`. Balance drift above `risk.reconcile_drift_pct` (0.005) is logged as
   `reconcile_drift`, and the exchange values always win.
7. `restart` event, then the loops start.

If the books are missing, the reconcile is deferred to the first tick that has them, and strategies
wait. The reconcile also runs every `engine.reconcile_interval_s` (300 s) and after an API-error
burst. One engine per account is enforced by the lock file `<sqlite_path>.engine.lock`, so `wta run`
and `flatten_now.py` can never trade the same account at once. Acceptance run (e) restarted mid-run and
kept peak equity, the daily-PnL base, fees and open positions.

## 13. Live guard

Live trading needs all of these (`src/wta/safety.py`, `src/wta/engine.py`). They are checked in the CLI,
in `build_engine`/`run_engine` and again in `Engine.run`. Without them the default mode is `shadow`.

| # | Guard | Refuses when |
|---|---|---|
| 1 | `--i-understand-live` | the flag is missing |
| 2 | `WTA_LIVE=1` | not set in the **process environment**; a value in `.env` does not arm live |
| 3 | `WALLEX_API_KEY` | empty |
| 4 | Clean git tree | `git status --porcelain` lists anything, untracked files included, or git fails (fail closed). `--allow-dirty` overrides this, for dev only. |
| 5 | `CHALLENGE_END_UTC` | missing, not ISO-8601, or already past. Without it the flatten would never run. `flatten_now.py` is exempt. |
| 6 | Real market rules | `/v1/markets` is unreachable and only the offline snapshot is available |
| 7 | Account lock | another engine or `flatten_now.py` holds `<sqlite_path>.engine.lock` |

The start event logs the git commit, the dirty flag, the tag and the `config_hash`. Live runs start from
tagged commits (`GO_LIVE_CHECKLIST.md` §5). The operator starts live mode; the agent never does.

## 14. What AI cannot do

| AI part | What it can do | What stops it |
|---|---|---|
| **Regime Analyst** (5-min loop) | Return one `MarketPlan` JSON. | Strict parsing (`extra="forbid"`, finite numbers, no booleans as numbers), then `clamp_plan`: `s1_margin_min_pct ∈ [0.0008, 0.01]`, `size_multiplier ∈ [0, 1]`, `usdt_target_share ∈ S2 bounds [0.25, 0.70]`, confidences in `[0, 1]`, `enabled_strategies` ⊆ the strategies enabled in config. Every changed value is recorded in `MarketPlan.clamped`. |
| How strategies use the plan | Enable or disable strategies, tighten S1, size down, pick the S2 target inside the band. | S1 margin = `max(config, plan)`, so it can only go up. `mult ≤ 1`, so size can only go down. A plan older than 1200 s is ignored (rules-only). |
| **RiskManager** | — | It does not read the plan at all. Its limits come only from config. It evaluates every intent (§2), and an AI output never reaches the exchange directly. |
| **Post-trade Reviewer** (hourly) | Propose config edits. | Only the allowlist in `configs/llm.yaml` (table below), within bounds. At most `max_changes_per_review` (2) changes, and `cooldown_s` (3600) between applies. The prefixes `risk.`, `execution.`, `markets.`, `engine.initial_equity`, `engine.challenge`, `engine.flatten`, `ai.replay_strict` and `llm.` are refused by a second, hard-coded check (`FORBIDDEN_PREFIXES`). An accepted change writes `configs/runtime/config-<ts>.yaml`, a new `config_hash` and a `config_change` event. |
| **Wallex MCP** | Read market data and account views. | Read-only by construction (`src/wta/ai/mcp_client.py`): `call_tool` accepts only the 8 tools in `READ_ONLY_ALLOWLIST`. `is_state_changing` also refuses any name containing words like post, delete, cancel, place, submit, transfer, buy, sell, or "order" without a preceding get/list, even if allowlisted. An empty allowlist allows nothing. |
| Telegram (operator, not AI) | `/status`, `/pause`, `/resume`, `/kill` | No order commands. `/resume` cannot lift a risk pause or reset a kill. Not yet connected to the engine (§6). |

Reviewer allowlist (`configs/llm.yaml → reviewer.editable`). None of these is a risk parameter:

| Path | Bounds |
|---|---|
| `strategies.s1_crossbook.margin_min_pct` | [0.0008, 0.005] |
| `strategies.s1_crossbook.leg1_taker` | true / false |
| `strategies.s2_usdtalloc.rebalance_threshold_pp` | [0.10, 0.25] |
| `strategies.s3_momentum.min_regime_confidence` | [0.5, 0.9] |
| `engine.slow_loop_interval_s` | [180, 900] |

## 15. Differences from PRD-02 and known limitations

Where the code differs from PRD-02:

* `KILLED` also requires side SELL, and `leg2` is not allowed while killed.
* `PAUSED` blocks every BUY and every unknown purpose, not only `entry`/`rebalance`.
* `DEADLINE` phase 2 allows only flatten **SELLs**, and phase 3 blocks everything.
* `USDT_BOUNDS` is adjust/hard rather than hard. It exempts all risk-reducing purposes (the PRD
  exempted only `flatten`) and sizes a lock-in on its whole cycle.
* `RATE_*` reserves worst-case re-prices instead of comparing `n < limit`. In practice a new entry
  needs `n_hour ≤ 7` when nothing else is open.
* `MAX_POSITIONS` counts pending entry orders. `COIN_EXPOSURE` and `USDT_BOUNDS` count open orders.
* `STALE_DATA` never blocks `stop`/`flatten`. `PRICE_SANITY` only sets a floor for closing SELLs.
* Flatten sells USDT maker-first like coins (best ask − 1 tick, 3 re-prices, then cross). The PRD
  described a fixed "best bid + 1 tick" price.

Known limitations:

* **No live data yet.** `api.wallex.ir` drops TLS from the dev machine. Every engine run, including the
  kill and flatten demos, used `--feed synthetic` with markets from `configs/markets_snapshot.yaml`. These
  runs exercise the code path and are not evidence of performance. Fees, `minNotional`, `STOP_LIMIT`
  acceptance and the `stop_Price` field still need checking against the real API (`GO_LIVE_CHECKLIST.md`
  §2–3).
* Re-prices, crosses and protective stops of an approved order are not re-evaluated (§2).
* The fee budget projects only entry fees (plus leg 2 for a lock-in). Exit, stop and flatten fees can
  push the total above it (§9).
* Software stops do not protect a position while the process is down (§11).
* A foreign (manual) order is invisible to `SELF_CROSS` (§10).
* Telegram `/kill` is implemented but not yet connected to the engine (§6).
