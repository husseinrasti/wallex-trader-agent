# PRD-02 — Risk Management, Execution, Positions, Reconciliation, Flatten, Kill-Switch

**Depends on:** PRD-01. **Blocks:** PRD-03 (strategies need a working `submit(intent)` path).
**Goal:** every path from an `Intent` to the exchange passes through deterministic risk checks; orders are idempotent and recoverable; the account can always be brought back to 100% TMN; the process can die and restart without losing state.

Scoring targets: risk mgmt 30 pts, drawdown control 30 pts, stability 15 pts.

---

## 1. Components

```
Intent ──▶ RiskManager.evaluate(intent, ctx) ──▶ RiskDecision ──▶ OrderManager.submit(intent, decision)
                                                                       │
                                                                       ├─ Exchange.place_order / cancel / get_order
                                                                       ├─ PositionBook (open/close, PnL)
                                                                       └─ DecisionLog / orders.jsonl
Scheduler: Reconciler (5 min) · EquityTracker (every tick) · FlattenScheduler · KillSwitch (file + rule)
```

---

## 2. Portfolio state (`wta/execution/positions.py`)

`PortfolioState` (single object owned by the engine, updated by fills/balances):
- `balances: dict[asset, Balance]` (from exchange; authoritative after reconcile)
- `positions: dict[id, Position]` (ours; strategy-tagged)
- `open_orders: dict[client_id, Order]`
- `equity_tmn()` = `TMN.free+locked` + `USDT.total × bid(USDTTMN)` + Σ `coin.total × bid(coinTMN)` (fallback `bid(coinUSDT) × bid(USDTTMN)`). Computed every fast tick; written to `equity` table every 10 s and to `health.jsonl`.
- `peak_equity_tmn`, `drawdown_pct = (peak − equity)/peak`, `day_start_equity_tmn` (reset at 00:00 UTC), `daily_pnl_pct`.
- `fees_paid_tmn_total` (sum of fills' fees converted to TMN at fill time).
- `coin_exposure_pct` = Σ coin value / equity; `usdt_share` = USDT value / equity.
- `orders_last_hour`, `orders_today` (placements, not cancels).
- `realized_pnl_tmn` by strategy and total (from closed positions).

**Realized PnL definition (used in logs/reports):** for a closed position, `pnl = Σ(sell fills value) − Σ(buy fills value) − fees`, converted to TMN at the fill-time USDTTMN bid when the quote is USDT. Lock-in S1 cycles (buy in one book, sell in the other) are one position with two legs; PnL = value out − value in, both in TMN at the time.

---

## 3. RiskManager (`wta/risk/manager.py`)

`evaluate(intent: Intent, ctx: RiskContext) -> RiskDecision`. `RiskContext` = portfolio state + latest books + market info + config + clock + engine flags (`paused`, `killed`, `flatten_phase`).

Checks run in order; **every check appends a `RiskCheck(code, passed, detail)`**, even when passed, so the decision log shows the full checklist. Any failed *hard* check ⇒ `approved=false`. Some checks may **adjust** qty down (`adjusted_qty`) instead of vetoing.

| Code | Type | Rule |
|---|---|---|
| `KILLED` | hard | kill-switch active → reject everything except `purpose in {flatten, exit, stop}` |
| `PAUSED` | hard | engine paused (anomaly/daily loss) → reject `entry`, `rebalance`; allow exits |
| `DEADLINE` | hard | `now ≥ flatten_start` → reject anything that increases non-TMN exposure; `now ≥ flatten_final` → only `flatten` |
| `BANNED_MARKET` | hard | symbol in `markets.banned` or not in `/v1/markets` |
| `MARKET_ORDER_DISABLED` | hard | `type=MARKET` and `!market_orders_enabled` → reject (caller should retry as LIMIT crossing the spread) |
| `MIN_NOTIONAL` | hard | `qty×price < minNotional` (TMN 50,000 / USDT 1) after rounding |
| `MAX_ORDER_PCT` | adjust | notional ≤ `max_order_pct_equity × equity` (25%) |
| `BEST_LEVEL_QTY` | adjust | for crossing (taker) orders, qty ≤ `max_order_pct_of_best_level_qty × best level qty` |
| `COIN_EXPOSURE` | adjust/hard | post-trade coin exposure ≤ 40% of equity; if would exceed with any qty ≥ min → reject |
| `USDT_BOUNDS` | hard | post-trade USDT share within S2 bounds [25%, 70%] unless `purpose=flatten` |
| `MAX_POSITIONS` | hard | open positions < 3; per-strategy caps (S1 2, S3 1) |
| `MAX_OPEN_ORDERS` | hard | open orders < 8 |
| `RATE_HOUR` / `RATE_DAY` | hard | placements in last 60 min < 10; today < 60 (exits exempt) |
| `DAILY_LOSS` | hard | `daily_pnl_pct ≤ −2.5%` → reject entries; set `paused_until_next_day` |
| `DRAWDOWN_KILL` | hard | `drawdown_pct ≥ 5%` → trigger kill-switch (see §6) |
| `FEE_BUDGET` | hard | `fees_paid + expected_fee(intent) > 2% × initial_equity` → reject entries |
| `EDGE_VS_COST` | hard | for entries: `intent.expected_edge_pct ≥ round_trip_cost(symbol, type) + margin_min` (cost uses maker or taker fee by order type) |
| `STALE_DATA` | hard | book age for the symbol (and USDTTMN for cross calcs) > `stale_data_s` |
| `PRICE_SANITY` | hard | limit price within ±3% of mid; for S1 the two books' implied prices disagree < 5% (else anomaly → pause) |
| `SELF_CROSS` | hard | a BUY limit price ≥ our lowest open SELL in the same symbol (or vice versa) → reject (wash-trade prevention) |
| `BALANCE` | adjust/hard | sufficient free balance (quote for BUY, base for SELL); adjust down to available minus 0.5% buffer |
| `DUPLICATE` | hard | identical open intent (same strategy/symbol/side/purpose) within cooldown |

Outputs `adjusted_qty` rounded to step; if adjusted below min notional → reject with `MIN_NOTIONAL`.

**Unit tests:** one test per code with a fixture portfolio; a property test that no approved intent violates any invariant; a test that `kill` blocks everything but exits/flatten.

---

## 4. OrderManager (`wta/execution/order_manager.py`)

### 4.1 Submit path
```
submit(intent, decision):
  if not decision.approved: log decision (kind="veto"); return
  req = build_request(intent, decision.adjusted_qty)      # rounding, client_id, decision_id
  order = await exchange.place_order(req)                  # idempotent (see PRD-01 §5)
  track(order); log orders.jsonl; log decision (kind="order")
```
### 4.2 Limit order lifecycle (maker-first policy)
- **Entry/rebalance** orders: LIMIT at `best_bid + maker_offset_ticks×tick` for BUY (`best_ask − …` for SELL) — joins/improves the top of book, usually maker.
- Poll `get_order(client_id)` every 2 s (or WS fill events if available).
- If not filled after `limit_reprice_after_s` (15 s): cancel and re-place at the new top (max `limit_max_reprices` = 2). After that, if purpose is `leg2`/`exit`/`stop`/`flatten`: cross the spread (LIMIT at opposite best, taker). If purpose is `entry`/`rebalance`: cancel and give up (log `EXPIRED_UNFILLED`).
- Partial fills: accept; the remainder follows the same reprice rules; positions record actual filled qty.
- Hard cap: any order older than `limit_cancel_after_s` (45 s) without the above handling is canceled.
- **Never** have a BUY and SELL open simultaneously in the same symbol (RiskManager `SELF_CROSS` + OrderManager guard).

### 4.3 Server-side stops
- On entry fill of a directional position (S1 revert-mode, S3): immediately place `STOP_LIMIT SELL` with `stop_price = stop`, `price = stop × (1 − stop_limit_slippage_pct)`, qty = position qty, `purpose="stop"`. Store `stop_order_client_id` in the position.
- If the exchange rejects STOP_LIMIT (probe in PRD-01 §10 decides), fall back to **software stops**: the fast loop checks `best_bid ≤ stop` and sends a crossing LIMIT (taker). Config `execution.use_server_side_stops` toggles; the decision log records which mechanism was used.
- When the position exits via target/time-stop, cancel the stop order first, then sell (avoid double-sell).

### 4.4 Fill handling
- Each fill updates: `Order`, `PortfolioState.balances` (optimistic; corrected at reconcile), `Position` (open on entry fill, close on exit fill), `fees_paid`, and emits an `outcome` update to the decision log when a position closes (`pnl_tmn`, `pnl_pct`, `hold_s`, `exit_reason ∈ {target, stop, time_stop, lockin, flatten, kill, manual_reset}`).

### 4.5 Client ID & idempotency
`client_id = f"{strategy[:3]}-{symbol}-{ts_ms}-{nonce4}"`, ≤ 40 chars. Timeout on POST ⇒ `get_order(client_id)` before any resend. All orders found on the account whose `client_id` does not match our pattern are logged as `foreign_order` (never touched; alert).

---

## 5. Reconciler (`wta/execution/reconcile.py`)

Runs at startup and every 5 min (and after any API error burst):
1. Fetch balances, open orders, recent my-trades (last 24 h).
2. Match fills to tracked orders by `client_id`; apply any missed fills.
3. Orders on exchange not tracked (ours by prefix) → adopt into tracking; foreign → log.
4. Tracked orders not on exchange and not terminal → fetch individually; set final status.
5. Positions: for each open position verify base balance ≥ qty; if the coin balance is missing (e.g., stop fired while down) close the position with `exit_reason=stop` using my-trades data.
6. Balance drift: if `|computed − exchange| > 0.5%` of equity for any asset, overwrite with exchange values and log `reconcile_drift`.
7. Persist state to SQLite (`positions`, `orders`), so a restart resumes from disk + exchange truth.

**Startup sequence:** load config → gitinfo (refuse dirty in live) → store → exchange → reconcile → set `peak_equity` from `equity` table (or current if none) → start loops. A restart must **not** reset `peak_equity`, `day_start_equity`, `fees_paid`, `orders_today`.

---

## 6. Kill-switch & pause (`wta/risk/manager.py`, `wta/engine.py`)

- **Triggers:** `drawdown ≥ 5%`; file `KILL` present in repo root; Telegram command `/kill` (optional); unhandled exception storm (≥ 3 engine-loop crashes in 10 min).
- **Action:** set `killed=true` (persisted in `events` table), cancel all open entry orders, run **Flatten** (§7) to TMN, then keep the process alive in monitoring mode (heartbeats continue; no new orders). Alert via Telegram if enabled.
- **Reset:** manual only — `python -m wta reset-kill --reason "..."`, which writes an `events` row (judges can see every reset).
- **Pause** (softer): daily loss limit, anomaly guard, stale data, API error burst → `paused=true` with `reason` and `until_ts` (daily loss: next UTC day; anomaly: until condition clears for 60 s). Exits still allowed. Everything logged to `events.jsonl` and health.

---

## 7. Flatten (`wta/risk/flatten.py`)

Goal: 100% TMN at `challenge_end_utc`. Phases driven by the scheduler (and callable manually: `scripts/flatten_now.py`):
1. **`flatten_start` (T−2h):** `flatten_phase=1`; RiskManager rejects exposure-increasing intents; cancel all open entry/rebalance orders; cancel stop orders *only right before* selling the related position.
   For each coin position/balance (any amount ≥ min notional): sell to **TMN** market if it exists and is allowed (`XTMN`), else sell to USDT. Use maker-first LIMIT with reprice (§4.2) but max 3 reprices then cross. Then USDT → TMN via `USDTTMN` LIMIT SELL at best bid + 1 tick, reprice every 15 s.
2. **`flatten_final` (T−30min):** `flatten_phase=2`; anything still not TMN: MARKET SELL where `market_orders_enabled`, else LIMIT at `best_bid × 0.995` (crossing).
3. **Dust:** balances below min notional are left (report them; they are worth < 50,000 TMN). Try once to top-up-sell? No — never buy during flatten.
4. **Verify:** at T−5min log `final_state` with balances and equity; alert if any non-TMN balance ≥ min notional remains.
5. After `challenge_end_utc`: engine stops placing any order (`DEADLINE` hard). Process keeps logging heartbeats.

Flatten must be idempotent and restart-safe (re-running it just continues).

---

## 8. Engine integration (`wta/engine.py`, part owned by this PRD)

Fast tick (every 2 s):
```
books = bus.snapshot(watchlist); portfolio.refresh_equity(books)
risk.update_rolling_state(portfolio)          # drawdown, daily loss, kill/pause evaluation
order_manager.poll_open_orders()              # fills, reprices, cancels
position_manager.check_exits(books)           # software stops, targets, time stops → exit intents
intents = [...]                               # from strategies (PRD-03); empty in PRD-02
for it in intents: decision = risk.evaluate(it, ctx); await order_manager.submit(it, decision)
decision_log.flush()
```
Scheduler tasks: reconcile (5 min), heartbeat (60 s), equity row (10 s), flatten phases (by clock), daily reset (00:00 UTC).

---

## 9. Shadow (paper) exchange — minimum for PRD-02 testing (`wta/exchange/paper.py`)

Implements the same `Exchange` interface against the live data bus:
- `place_order(LIMIT)`: fills when the live book crosses the price (BUY fills if `best_ask ≤ price`; SELL if `best_bid ≥ price`), qty ≤ available at that level else partial; maker fee if our price was not crossing at placement, else taker fee.
- `MARKET`: fills at best level(s) walking the book, taker fee.
- `STOP_LIMIT`: becomes a LIMIT when `last/bid ≤ stop`.
- Latency model: fills evaluated on the next tick (≥ 2 s) — pessimistic.
- Balances start from `initial_equity_tmn` TMN (or a `--paper-balances` JSON).
Full fill-model details and backtest exchange are in PRD-05; PRD-02 needs only this subset.

---

## 10. Tests

- `test_risk_manager.py`: every code; adjust vs veto; kill/pause semantics; deadline phases (freezegun); self-cross; fee budget; edge-vs-cost with maker/taker.
- `test_order_manager.py` (paper exchange + fake bus): maker placement price, reprice twice then cross for exits, give up for entries, partial fills, stop placement after entry fill, cancel-stop-before-exit, idempotent resend after timeout.
- `test_reconcile.py`: missed fill recovery, foreign order detection, restart restores peak/daily/fees.
- `test_flatten.py`: phases, idempotency, dust handling, nothing bought during flatten.
- `test_equity.py`: equity in TMN with mixed balances; drawdown and daily PnL math.

---

## 11. Acceptance checklist

- [ ] `make shadow` runs ≥ 1 h with S0 "noop strategy" (no intents) — heartbeats, reconcile, equity rows, no exceptions.
- [ ] Injected fake intents in shadow produce complete decision records (checks list, order, outcome).
- [ ] Simulated drawdown ≥ 5% → kill → flatten in paper; `events.jsonl` shows it; `reset-kill` works.
- [ ] `scripts/flatten_now.py` in paper converts a mixed portfolio to 100% TMN (minus dust).
- [ ] Restart mid-run keeps peak equity, daily PnL, fee total and open positions.
- [ ] Tag `v0.2-risk-exec`.
