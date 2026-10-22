# PRD-03 — Feature Engine and Strategies (S1 CrossBook, S2 USDTAlloc, S3 RegimeMomentum)

**Depends on:** PRD-01, PRD-02. **Related:** PRD-04 (MarketPlan inputs), PRD-05 (validation).
**Goal:** deterministic, pure, testable strategy functions that turn features + the current `MarketPlan` into `Intent`s with human-readable reasons.

Scoring targets: strategy logic 40 pts, activity 15 pts, profit/drawdown.

---

## 1. Strategy interface (`wta/strategies/base.py`)

```python
class StrategyContext(BaseModel):
    now: float
    books: dict[str, BookSnapshot]            # latest per symbol
    features: dict[str, FeatureSet]           # per symbol (see §2)
    candles: dict[tuple[str,str], list[Candle]]  # (symbol, resolution) -> last N
    markets: dict[str, MarketInfo]
    portfolio: PortfolioView                   # equity, balances, positions, usdt_share, coin_exposure, open_orders
    plan: MarketPlan | None                    # latest validated LLM plan (may be None → rules-only defaults)
    cfg: StrategyConfig
    rng_seed: int                              # for any tie-breaks (determinism)

class Strategy(Protocol):
    id: str
    def on_tick(self, ctx: StrategyContext) -> list[Intent]: ...
    def on_fill(self, fill: Fill, position: Position | None) -> list[Intent]: ...   # e.g., S1 leg-2 after leg-1 fill
    def state(self) -> dict: ...               # serializable; persisted every tick for replay
```
Rules:
- Strategies are **pure** given `ctx` + their own `state`; no I/O, no clock reads, no randomness except `rng_seed`.
- Each `Intent.reason` must be a complete sentence a judge can read, e.g. `"BTC: TMN book ask implies 82,610 USDT vs BTCUSDT bid 83,020 → TMN book 0.49% cheap; cost 0.37% (0.25% maker TMN + 0.10% maker USDT + spread 0.02%); margin 0.12% ≥ min 0.10%; regime=range; plan size mult 0.8"`.
- `Intent.features` carries the numbers used (for the decision log and replay equality tests).
- Per-symbol **cooldown** and **one strategy per symbol at a time** are enforced in the engine (`StrategyRouter`), not in each strategy.

---

## 2. Feature engine (`wta/features/engine.py`)

Computed every fast tick for watchlist symbols from the bus + candle cache. All `Decimal`/float as noted; store `FeatureSet` in `DecisionRecord.features` for the symbols involved.

Per symbol `S`:
- `bid, ask, mid, spread_pct = (ask−bid)/mid`, `book_age_s`
- `depth_bid_tmn_0p3 / depth_ask_tmn_0p3`: notional within 0.3% of mid, in TMN
- `imbalance = (Σbid_qty_5 − Σask_qty_5) / (Σbid_qty_5 + Σask_qty_5)` (top 5)
- `ret_1m, ret_5m, ret_15m, ret_1h` (from 1-min candles, close-to-close)
- `atr_15m_pct` (ATR(14) on 15-min candles / close), `ema20_15m, ema60_15m`
- `vol_ratio_15m = last 15m volume / mean(20)`
- `hh20_15m = max(high of previous 20 completed 15-min bars)`
- `realized_vol_1h_pct` (std of 1-min returns × sqrt(60))

Cross-book features for coin `X` with both `XTMN` and `XUSDT` allowed (needs `USDTTMN`):
- `u_bid, u_ask, u_mid` = USDTTMN
- `implied_mid_usdt = mid(XTMN) / u_mid`
- `dev_mid_pct = implied_mid_usdt / mid(XUSDT) − 1` (positive ⇒ TMN book expensive)
- Executable deviations (what we can actually capture):
  - **TMN cheap:** buy in TMN at `ask(XTMN)`, sell in USDT at `bid(XUSDT)`:
    `edge_tmn_cheap_pct = (bid(XUSDT) × u_bid) / ask(XTMN) − 1`
    (we convert the USDT proceeds at `u_bid` conceptually; in lock-in mode the USDT stays in the USDT pool, but we value it at `u_bid` for a conservative edge)
  - **USDT cheap:** buy in USDT at `ask(XUSDT)`, sell in TMN at `bid(XTMN)`:
    `edge_usdt_cheap_pct = bid(XTMN) / (ask(XUSDT) × u_ask) − 1`
- `cost_lockin_tmn_cheap = maker_fee(XTMN) + taker_fee(XUSDT)` (leg 1 maker-first, leg 2 crosses) — and symmetric `cost_lockin_usdt_cheap = maker_fee(XUSDT) + taker_fee(XTMN)`. Config may force leg 1 taker (`s1.leg1_taker=true`) for speed; then use taker fees for both.
- `dev_zscore` = `dev_mid_pct` standardized by its rolling 1-h mean/std (for revert mode).

Dollar features (USDTTMN): `ret_1h, ret_24h, ema20_15m, ema60_15m, atr_15m_pct, trend_flag = sign(ema20−ema60) if |ema20−ema60|/ema60 > 0.15% else 0`.

Unit tests with fixed book fixtures for every formula (including the §0 snapshot from the Overview; expected `dev_mid_pct ≈ −0.07%`).

---

## 3. S1 — CrossBook (`wta/strategies/s1_crossbook.py`)

**Thesis:** Wallex's TMN books and USDT books for the same coin are priced by different participants and update at different speeds; during fast moves (BTC or the dollar), one side lags, creating a measurable executable spread between `XTMN/USDTTMN` and `XUSDT`. Because we hold both TMN and USDT (S2), we can buy in the cheap book and sell in the expensive one and lock the difference, direction-neutral.

**Modes:**
- **Lock-in (preferred):** both legs executed back-to-back; profit realized immediately in the quote currency of leg 2.
- **Revert (bounded directional):** when the executable lock-in edge is below margin but the mid deviation is large vs its recent noise (`|dev_zscore| ≥ z_min`), buy the cheap book only and exit in the *same* book when the deviation closes. Stop + time stop. Disabled by `plan.enabled_strategies` if regime=`chaos`.

**Entry condition (lock-in), evaluated per coin each tick:**
```
cost = cost_lockin_{side}              # fees
margin_min = max(cfg.margin_min_pct, plan.s1_margin_min_pct)
if edge_tmn_cheap_pct  ≥ cost + margin_min  → side = TMN_CHEAP  (BUY XTMN, then SELL XUSDT)
elif edge_usdt_cheap_pct ≥ cost + margin_min → side = USDT_CHEAP (BUY XUSDT, then SELL XTMN)
and: book ages ≤ stale_data_s; not in cooldown; open S1 positions < max_concurrent; plan.size_multiplier > 0
```
**Sizing:** `notional = min(size_pct_equity × equity × plan.size_multiplier, 0.8 × qty@best of BOTH legs × price, free quote balance of leg 1 − buffer)`; must satisfy min notional on both legs (TMN 50,000 / USDT 1). If leg 2's best-level liquidity is thinner than leg 1's, size to leg 2.

**Execution:**
- Leg 1: LIMIT at `best_ask − 0` (i.e., take, taker) if `cfg.leg1_taker` else maker-first at `best_bid + tick` with reprice ≤ 1 (lag opportunities are short — default `leg1_taker: true` for lock-in; cost computed accordingly).
- On leg-1 fill (`on_fill`), immediately emit leg 2 intent `purpose="leg2"` as a crossing LIMIT at the current opposite best (taker). If the edge has vanished at that moment (`edge < 0`), still complete leg 2 only if the loss would be < `cfg.leg2_max_loss_pct` (0.3%); otherwise convert to **revert mode** for that position (set stop/time-stop) — logged as `lockin_degraded_to_revert`.
- Partial fills: leg 2 sized to the filled qty.

**Revert mode details:** entry when `|dev_zscore| ≥ 2.0` and `|dev_mid_pct| ≥ cost_roundtrip(same book) + margin_min`; buy the cheap book; target = deviation closed by `revert_target_fraction` (60%); stop = `revert_stop_pct` (1.0%) via server STOP_LIMIT; time stop `revert_max_hold_s` (20 min). Max 1 revert position at a time.

**Parameters** (see `configs/default.yaml` `strategies.s1_crossbook`): `coins`, `margin_min_pct=0.0015`, `leg1_taker=true`, `leg2_max_loss_pct=0.003`, `revert_*`, `cooldown_s=60`, `max_concurrent=2`, `size_pct_equity=0.20`, `z_min=2.0`.

**State:** last entry ts per coin; open cycles with leg status; rolling dev stats per coin (window 1 h) — persisted so replay is exact.

**Expected behaviour:** zero trades in calm markets (that is correct behaviour, not a bug — S2 provides activity); bursts of 1–5 cycles during fast moves. The survey (PRD-05) must report how many lock-in opportunities per day exist at `margin_min` so we can tune it; if < 2/day at 0.15%, lower `margin_min` to 0.08% and/or set `leg1_taker=false`.

---

## 4. S2 — USDTAlloc (`wta/strategies/s2_usdtalloc.py`)

**Thesis:** the challenge is valued in TMN, USDTTMN moves 1–3%/day, and we must hold USDT to trade cheap USDT pairs. Holding USDT is therefore an unavoidable directional position on the dollar; S2 makes it explicit, bounded, regime-driven, and logged — and produces regular, cheap activity.

**Target share:**
```
regime = plan.regimes["USDTTMN"].regime if plan else rule_regime()
rule_regime(): trend_flag>0 and ret_24h>+0.8% → trend_up; trend_flag<0 and ret_24h<−0.8% → trend_down;
               atr_15m_pct > 2× its 24h median → chaos; else range
base_target = cfg.target_share_by_regime[regime]
target = clamp(plan.usdt_target_share if plan else base_target, bounds)      # bounds [0.25, 0.70]
```
**Rebalance condition:** `|usdt_share − target| ≥ rebalance_threshold_pp` (15 pp) and `now − last_rebalance ≥ min_rebalance_interval_s` (30 min) and no open S2 order.
**Intent:** BUY USDTTMN (if under target) or SELL USDTTMN (if over) for `notional = |usdt_share − target| × equity`, capped by risk (25%); maker-first LIMIT (`best_bid + tick` / `best_ask − tick`), reprice ≤ 2 then give up (next tick re-evaluates). `purpose="rebalance"`, `expected_edge_pct = None` (RiskManager skips `EDGE_VS_COST` for rebalances but enforces `FEE_BUDGET`).
**Initial allocation:** on first run with 100% TMN, S2 immediately moves to target (e.g. 50%) — this is the "first trade". Threshold logic prevents churn afterwards.
**Reason text example:** `"USDTTMN regime=trend_up (ema20>ema60 by 0.4%, 24h +2.4%); target USDT share 65% (plan) vs current 48% → buy 850,000 TMN of USDT as maker at 266,721"`.

**Guard:** S2 never trades during flatten phases; flatten handles the final conversion.

---

## 5. S3 — RegimeMomentum (`wta/strategies/s3_momentum.py`) — enable after MVP

**Thesis:** cheap USDT-pair fees (0.20% round trip) make a conservative breakout strategy viable on BTC/ETH/SOL when the LLM analyst confirms a trending regime; bounded by a small risk budget and server-side stops.

**Universe:** `BTCUSDT, ETHUSDT, SOLUSDT` (all allowed; `isMarketTypeEnable` for these to be checked — use LIMIT anyway).
**Signal (on each completed 15-min bar):**
```
long_breakout = close > hh20_15m  and  vol_ratio_15m ≥ 1.5  and  0.3% ≤ atr_15m_pct ≤ 3%
regime_ok    = plan and plan.regimes[sym].regime == "trend_up" and plan.regimes[sym].confidence ≥ 0.6
no_position  = no open S3 position (global cap 1)
```
Spot only → long only.
**Sizing (risk-based):** `stop_dist = stop_atr_mult × ATR = 1.5 ATR`; `qty = (risk_per_trade_pct × equity_in_usdt) / stop_dist`, capped at `max_size_pct_equity` (20%) and risk caps.
**Orders:** entry maker-first LIMIT at `best_bid + tick`, reprice ≤ 2, give up if unfilled. On fill: server `STOP_LIMIT` at `entry − 1.5 ATR`; target LIMIT SELL at `entry + 2.5 ATR` (placed immediately — one SELL only, so no self-cross: target and stop are both SELLs; allowed). Time stop 4 h → cancel both, sell crossing.
**Exit reasons:** `target`, `stop`, `time_stop`, `regime_flip` (plan turns `trend_down`/`chaos` → exit at market-crossing LIMIT), `flatten`.
**Reason text:** includes breakout level, volume ratio, ATR%, regime and confidence, stop/target prices.

---

## 6. Strategy router (`wta/engine.py`)

- Order of evaluation each tick: exits (position manager) → S1 → S2 → S3.
- Enabled set = `cfg.enabled ∧ plan.enabled_strategies` (if plan exists) ∧ not flatten phase.
- One open order per (strategy, symbol). Symbol-level cooldown after any fill (default 30 s).
- Deterministic ordering of intents (sorted by strategy id, symbol).

---

## 7. Activity & fee budget sanity (design check)

Expected placements/day: S2 1–4, S1 0–10 cycles (2 orders each), S3 0–3 → 5–30 orders/day, ~2–10 round trips. Fee estimate at 25% size: S1 cycle ≈ 0.35% × 1.25M = 4,400 TMN; S2 rebalance ≈ 0.25% × 750k = 1,900 TMN; 3-day total ≈ 40–90k TMN ≈ 0.8–1.8% of equity — inside the 2% fee budget. If the budget is at risk, RiskManager blocks entries, not exits.

---

## 8. Tests

- `test_features.py`: every formula on fixtures, including Overview snapshot.
- `test_s1.py`: lock-in triggers on synthetic deviations (both directions), sizing to thinner leg, leg-2 emission on fill, degradation to revert, cooldown, no trade when below margin, `reason` text contains the numbers.
- `test_s2.py`: regime → target mapping, bounds clamp, threshold/hysteresis, initial allocation, no churn, flatten guard.
- `test_s3.py`: breakout detection on candle fixtures, regime gating, sizing math, stop/target prices, time stop.
- `test_determinism.py`: same ctx + state → identical intents (bytes-equal JSON), 1000 iterations with shuffled dict orders.

---

## 9. Acceptance checklist

- [ ] Shadow run ≥ 4 h with S1+S2: S2 performs initial allocation; S1 shows evaluated opportunities in logs (even if zero executed) with edge/cost numbers every minute in `health`/debug.
- [ ] `docs/STRATEGY.md` written from this PRD (judge-facing: buy/sell conditions in plain language + formulas).
- [ ] Decision records for a full S1 cycle and an S2 rebalance pass `python -m wta logs check`.
- [ ] Tag `v0.3-strategies` → **go live at half size** (`risk.max_order_pct_equity=0.125`, `s1.size_pct_equity=0.10`) → after 6 h without incidents, restore defaults.
