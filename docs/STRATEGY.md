# Strategy — Wallex Trader Agent

1. Three deterministic strategies and a FLATTEN routine trade Wallex spot. Every 5 minutes an LLM writes a bounded plan that can only narrow what they do, and a deterministic RiskManager runs 23 checks on every order, which can veto or shrink it.
2. **S1 CrossBook** (core edge, direction-neutral): converts a coin's TMN price to USDT through the USDTTMN book and buys in the cheaper book. It sells those coins (less the BUY fee, which Wallex takes in the coin) in the other book seconds later, but only when the gap beats both taker fees plus a 0.15 % margin (plus a small charge for the sub-step remainder leg 2 cannot sell).
3. **S2 USDTAlloc** (always on): holds 25–70 % of equity in USDT according to the dollar regime. It rebalances only when the share is ≥ 15 pp off target and no more often than every 30 min. This states the unavoidable dollar bet openly and provides the regular activity.
4. **S3 RegimeMomentum** (**off by default**) buys 15-minute breakouts on BTC/ETH/SOL-USDT only when the AI calls `trend_up` with ≥ 0.6 confidence, and protects them with an exchange-side stop. **FLATTEN** converts everything to TMN from T−2h, because only TMN counts at the end.
5. **Status:** S1 and S2 (and S3 when enabled) run in the engine's fast loop through the `StrategyRouter` (`engine._strategy_step`), every decision goes through the RiskManager, and each heartbeat carries their diagnostics (`health.extra.strategies`). Only the AI plan is not wired yet, so the strategies run rules-only. The code has never seen real Wallex data, because `api.wallex.ir` is unreachable from the development machine. Every run so far used the synthetic feed, and every evidence section below is still a TODO.

> ### How to explain in 2 minutes
>
> **The constraint.** We are scored on net TMN value after 3 days, and a negative return scores zero. Fees are 0.25 % maker / 0.30 % taker on TMN pairs and 0.10 % / 0.13 % on USDT pairs. So the plan is: don't lose, trade regularly, and be auditable.
>
> **S1, the edge.** Every major coin trades twice on Wallex, against TMN and against USDT, and different people trade the two books. When BTC or the dollar moves fast, one book lags. We convert the TMN price into USDT through USDTTMN. If buying in one book and selling in the other still clears both fees plus 0.15 %, we buy the cheap side at once and sell the other side as soon as the buy fills. A trade lasts seconds and does not depend on direction. In calm markets S1 does nothing, and that is correct.
>
> **S2, the dollar bet.** To use the cheap USDT books we must hold USDT, and holding USDT is a bet on the dollar against the toman. S2 makes the bet explicit. The target USDT share is 30–65 % depending on the dollar regime, with hard bounds of 25–70 %. S2 trades only when the share is 15 points off target, at most once per 30 minutes, and always as a maker. It also makes the first trade.
>
> **S3, the bounded upside (off until there is evidence).** It buys a 15-minute breakout on 1.5× volume only when the AI says `trend_up` with ≥ 60 % confidence. The stop rests on the exchange at 1.5 ATR, the target is 2.5 ATR, and a position is held at most 4 h.
>
> **FLATTEN.** From T−2h everything is sold to TMN with maker orders. From T−30 min any leftovers are sold with market orders.
>
> **The AI.** Every 5 minutes an LLM reads the market through tools and writes a plan: the regime per market, which strategies may run, a size multiplier in [0, 1], an S1 margin that can only go **up**, and a USDT target inside 25–70 %. It never places orders. Without a fresh plan the rules run alone. Without an LLM key, a deterministic *offline rules provider* fills the same slot, and it is labelled everywhere as not an LLM.
>
> **Why trust it.** Every order has a decision record: the numbers behind it, all 23 risk checks, the order sent and the outcome. Replay reproduces the records. Honest status: we have no recorded Wallex data yet.

**Contents.** [1 Costs](#1-costs-and-decision-path) · [2 S1](#2-s1-crossbook--core-edge) · [3 S2](#3-s2-usdtalloc--explicit-dollar-bet-and-regular-activity) · [4 S3](#4-s3-regimemomentum--bounded-upside-disabled-by-default) · [5 FLATTEN](#5-flatten--everything-back-to-tmn) · [6 AI plan](#6-how-the-ai-plan-changes-each-strategy) · [7 Sizing](#7-sizing-at-5000000-tmn) ·
[8 Example record](#8-example-decision-record-illustrative) · [9 Expected behaviour](#9-expected-behaviour) · [10 Tuning](#10-tuning-discipline-survey-walk-forward-overfitting-guard) · [11 Evidence](#11-evidence-todo) · [12 vs PRD-03](#12-where-the-code-differs-from-prd-03) · [13 Limitations](#13-known-limitations)

Formulas are written from `src/wta/strategies/` and `src/wta/features/engine.py`; **where code and PRD-03 differ, the
code is what trades** (section 12). The S1 rules below were checked against the final `s1_crossbook.py` and
`tests/test_s1.py`.

---

## 1. Costs and decision path

**Notation.** `bid(M)`, `ask(M)`, `mid(M)`, `spread(M) = (ask − bid) / mid` · `X` is a coin with both an `XTMN` and an
`XUSDT` book · `u_bid`, `u_ask`, `u_mid` describe USDTTMN (TMN per USDT) · `mk(M)` and `tk(M)` are the maker and taker fees · `E` = equity in
TMN, valued conservatively at bids (TMN + USDT × `u_bid` + Σ coin × `bid(XTMN)`) · `usdt_share` = USDT × `u_bid` / `E` ·
`k` = the plan's `size_multiplier` (1 without a fresh plan) · a *fresh plan* is ≤ `ai.plan_stale_s` = 1,200 s old ·
quantities are floored to the market step; BUY prices round down to the tick, SELL prices up.

**Fees** (`GET /v1/account/fee`; offline snapshot `configs/fees_snapshot.yaml`):

| Market type | maker | taker | round trip maker+maker | maker+taker | taker+taker |
|---|---|---|---|---|---|
| TMN pairs (`XTMN`, `USDTTMN`) | 0.25 % | 0.30 % | 0.50 % | 0.55 % | 0.60 % |
| USDT pairs (`XUSDT`) | 0.10 % | 0.13 % | 0.20 % | 0.23 % | 0.26 % |

**What each trade must beat before it may enter:**

| Trade | Fees | Entry hurdle in the code |
|---|---|---|
| S1 lock-in, taker leg 1 (default) | tk + tk = **0.43 %** in both directions | executable edge ≥ 0.43 % + 0.15 % = **0.58 %** (spreads are already inside the edge), plus the unhedged-remainder charge `u × 3 %` (2.2; ≈ +0.06 % for BTC at 1M TMN) |
| S1 lock-in, maker leg 1 | 0.25 + 0.13 = 0.38 % (TMN cheap); 0.10 + 0.30 = 0.40 % (USDT cheap) | 0.53 % / 0.55 % (+ the same charge) |
| S1 revert in the USDT book | 0.10 + 0.13 % + spread | the 60 % of the gap to the 1-h mean that the exit captures ≥ **0.38 % + spread**, i.e. a gap ≥ 0.63 % + spread / 0.6 |
| S1 revert in the TMN book | 0.25 + 0.30 % + spread | captured 60 % ≥ **0.70 % + spread**, i.e. a gap ≥ 1.17 % + spread / 0.6 |
| S2 rebalance (USDTTMN) | 0.25 % maker (S2 never crosses) | none: a portfolio decision; the fee budget still applies |
| S3 (BTC/ETH/SOL-USDT) | 0.10 % in; 0.10 % at target or 0.13 % at stop / time stop / regime flip; + spread | target distance (2.5 ATR) ≥ 0.23 % + spread; a stop-limit may slip ≤ 0.4 % |
| FLATTEN | 0.25–0.30 % per TMN-pair sale (a coin without a TMN pair pays a USDT sale too) | none: it must happen |

The RiskManager re-checks the hurdle on every S1/S3 entry (`EDGE_VS_COST`). It also enforces a fee budget of
2 % of initial equity, i.e. 100,000 TMN (`FEE_BUDGET`: fees paid plus this order's expected fee). For scale: one full S1 cycle of
1,000,000 TMN costs ≈ 4,300 TMN, one 25 % S2 step costs 3,125 TMN, and flattening 2,500,000 TMN of USDT costs 6,250–7,500 TMN.

**Decision path.** The same code runs in backtest, shadow, live and replay:

```
FAST LOOP, every 2 s, no LLM
books + 1-min candles → FeatureEngine → position exits (stop / target / time stop) → S1 → S2 → S3
   StrategyRouter: one strategy per symbol (an S1 lock-in also owns its leg-2 book for the whole cycle),
   one open order per (strategy, symbol), 30-s cooldown after any fill on a symbol; exits, stops, leg 2
   and flatten orders are never filtered
→ RiskManager: 23 checks in fixed order → approve / shrink qty / veto (a lock-in leg 1 only with a placeable leg 2)
→ OrderManager: LIMIT (maker-first or crossing), ≤ 2 re-prices, server STOP_LIMIT after an entry fills;
   S1's two legs are never re-priced or chased (an unfilled one is given up after 15 s and S1 decides again)
→ logs/decisions.jsonl: one record per decision; the outcome is appended later as its own line
SLOW LOOP, every 5 min: Regime Analyst → validated, clamped MarketPlan read by the next tick (section 6)
```

---

## 2. S1 CrossBook — core edge

`src/wta/strategies/s1_crossbook.py` · features: `cross_features` and `_zscore` in `src/wta/features/engine.py`

**Thesis.** Wallex lists the same coin against TMN and against USDT. Different participants make the two books, and they
update at different speeds. When BTC moves in the USDT book, or the dollar jumps in USDTTMN, the other book can lag
for seconds. Converted through USDTTMN, the two books then disagree by more than the cost of trading both. S2 holds
both TMN and USDT, so S1 can buy the coin in the cheap book and sell the same quantity in the expensive one. This
**lock-in** fixes the difference whatever the coin does next. A bounded **revert** mode buys only the cheap side when
the gap is unusual for its own last hour but too small to lock in.

### 2.1 What it measures (per coin, every tick)

```
implied_mid_usdt = mid(XTMN) / u_mid
dev    = implied_mid_usdt / mid(XUSDT) − 1                 # > 0: the TMN book is expensive
edge_T = bid(XUSDT) × u_bid / ask(XTMN) − 1                # TMN book cheap: buy XTMN, sell XUSDT
edge_U = bid(XTMN) / (ask(XUSDT) × u_ask) − 1              # USDT book cheap: buy XUSDT, sell XTMN
f1(M)  = tk(M) if leg1_taker else mk(M)                    # leg1_taker = true by default
cost_T = f1(XTMN) + tk(XUSDT);   cost_U = f1(XUSDT) + tk(XTMN)          # 0.43 % each by default
z      = (dev − mean) / std of the coin's dev samples in the previous 3,600 s (current sample excluded;
         undefined below 30 samples). A tick adds a sample only when all three books are fresh (≤ 30 s)
         and at least one of them changed since the last sample: a stalled feed does not fill the hour
         with copies of one stale deviation (that would shrink the std and inflate z)
m      = max(margin_min_pct, fresh plan's s1_margin_min_pct)          # 0.15 %; the plan can only raise it
```

The edges are executable: they buy at the ask and sell at the bid, so the spreads are already paid. USDT is valued
on the conservative side: USDT we receive counts at `u_bid`, and USDT we spend counts at `u_ask`. Roughly,
`edge ≈ |dev| − (spread(XTMN) + spread(XUSDT) + spread(USDTTMN)) / 2`.

### 2.2 Lock-in entry

```
every fast tick, for each coin X in sorted(coins) whose XTMN, XUSDT and USDTTMN books exist:
  gates  S1 enabled by config and by the fresh plan (if any); not flattening; k > 0;
         all three books ≤ stale_data_s (30 s) old; ≥ cooldown_s (60 s) since the last entry on X;
         neither XTMN nor XUSDT held or worked by any strategy (position or open order)
  TMN_CHEAP  if edge_T ≥ cost_T + m  →  leg 1 BUY X on XTMN now, leg 2 SELL X on XUSDT on fill
  USDT_CHEAP if edge_U ≥ cost_U + m  →  leg 1 BUY X on XUSDT now, leg 2 SELL X on XTMN on fill
  otherwise try revert (2.4); a lock-in edge that fails sizing does not fall back to revert
  leg-1 price = ask of the buy book (crossing LIMIT)  [leg1_taker=false: bid + 1 tick while below the ask]
  q_max = min(size_pct_equity (20 %) × E × k / leg-1 price in TMN,
              80 % × best-ask qty of the leg-1 book (also for a maker leg 1: the lag rarely leaves more),
              80 % × best-bid qty of the leg-2 book,
              free leg-1 quote × (1 − 0.5 %) / (price × (1 + f1)))
  a BUY pays its fee in the coin, so leg 1 buys q1 and leg 2 can sell only q2 (lockin_leg_qtys, shared
  with the RiskManager):
        q2 = floor_leg2step(floor_leg1step(q_max) × (1 − f1))
        q1 = the smallest leg-1 step multiple with q1 × (1 − f1) ≥ q2      (≤ q_max)
  q1 must meet leg 1's min qty and min notional, q2 leg 2's (at its bid): TMN 50,000 / USDT 1
  u = (q1 × (1 − f1) − q2) / q1   # the coins leg 2 cannot sell (below one step), as a share of the cycle
  need = cost + m + u × 3 %       # the unhedged remainder priced at a 3 % adverse move (UNHEDGED_STRESS_PCT)
  enter when edge ≥ need          # at 1M TMN: u ≈ 0.4 % ETH, 1.9 % BTC, 2.5 % SOL, 4.5 % DOGE
  rank candidates: lock-in before revert, then edge above the need, then coin name;
  take them while S1 cycles (positions + working entries) < max_concurrent (2)
  expected_edge_pct = edge  → the RiskManager's EDGE_VS_COST re-checks edge ≥ cost + max(margin, config margin);
  when its adjust checks shrink q1 it re-lands it with lockin_leg_qtys and vetoes the entry (MIN_NOTIONAL,
  "lock-in leg 2 ...") if leg 2 could then not be sold
```

**In plain language.** Every two seconds, for each coin: if I bought now at the ask in one book and sold now at the
bid in the other (converting USDT at a price someone would really pay), would I clear both taker fees plus 0.15 %? If
yes, buy at once (taker, because these gaps close in seconds) and sell in the other book when the buy fills. Size: 20 %
of equity, less if a best level is thin or the paying currency is short. The exchange keeps its BUY fee in the coin and
sells only whole steps, so a sliver (under one step, ≈ 2 % of a BTC cycle at 1M TMN) stays unhedged; S1 asks the gap
to also cover a 3 % move against that sliver, and never buys more coins than leg 2 can sell.

### 2.3 Leg 2: hedging until hedged, or degrading

Emitting leg 2 hedges nothing by itself: the RiskManager may veto it, or the exchange may reject it. S1 therefore
never *remembers* a cycle as hedged. It derives the state from the portfolio every time:

```
unhedged = lock-in position qty − remaining qty of our open S1 leg-2 SELLs
           − leg-2 intents emitted this same tick that are not orders yet
on a leg-1 fill  : leg 2 for the whole unhedged qty (this fill plus any earlier remainder not covered by an open
                   or same-tick leg 2); fills below the leg-2 minimum accumulate until they are not
on every tick    : no leg-2 order open and unhedged > 0  →  leg 2 for the whole unhedged qty
                   (a vetoed, rejected or expired leg 2 is retried every tick until hedged)
leg 2            : SELL unhedged (floored to the leg-2 step) at bid(leg-2 book), crossing LIMIT, purpose "leg2"
edge_now = sell_TMN / buy_TMN − 1 − (f1 + tk(leg 2))           # what the cycle realises after both fees
           buy_TMN  = leg-1 avg price × (u_ask if leg 1 was the USDT book else 1)
           sell_TMN = bid(leg 2)       × (u_bid if leg 2 is the USDT book else 1)
if edge_now ≤ −leg2_max_loss_pct (−0.30 %):  "lockin_degraded_to_revert"
           STOP_LIMIT SELL of the position on the leg-1 book: stop = entry × (1 − 1 %), limit = stop × (1 − 0.4 %),
           time stop 1,200 s, re-emitted every tick while the position has no stop; then a revert-mode exit
           against the target recorded at the lock-in entry: mean + 40 % × (dev − mean) (2.4)
a remainder below the leg-2 minimum, once leg 1 is no longer working → sold back on the leg-1 book
```

Leg 2 is risk-reducing: no edge test, never blocked by the order-rate limits or the USDT band (the band was checked
for the **whole two-leg cycle** when leg 1 was approved, and so was leg 2's minimum order). It still passes
`STALE_DATA`, `PRICE_SANITY` and `BALANCE` (capped at the coins held). The OrderManager does **not** chase it: S1
checked the loss limit at the bid it chose, so an unfilled leg 2 rests there for `limit_reprice_after_s` (15 s), is
then given up (`EXPIRED_UNFILLED`, also when the exchange ends it), and S1's next tick re-emits the unhedged coins at
the new bid, re-checking `edge_now`, or degrades. While the cycle lasts, its leg-2 book belongs to S1 in the router
(a position's `leg2_symbol`, the other book of a working S1 entry, an entry accepted earlier in the tick): no other
strategy can rest a BUY where leg 2 must cross. The cycle closes when leg 2 fills (`exit_reason: lockin`); PnL is
booked in TMN, USDT legs at the USDTTMN bid at fill time. A degraded cycle risks about 1 % stop + 0.4 % slippage + up
to 0.6 % fees ≈ 2 % of the cycle ≈ 0.4 % of equity at 20 % size.

**In plain language.** Once the buy fills, we sell the coins in the other book at its bid, and retry every tick
until they are sold, each time at the then bid and only while the loss after fees stays under 0.3 %: being flat beats
hoping, but we never sell into a falling book at any price. A bigger loss keeps the coins as a short directional
position with a 1 % exchange stop and a 20-min clock.

### 2.4 Revert mode (bounded directional)

```
only when no lock-in edge exists for X, revert_enabled, revert + degraded S1 cycles < revert_max_concurrent (1),
and X is not in chaos: a fresh plan calls chaos on XTMN, XUSDT or USDTTMN, or, for a symbol without a plan call,
ATR15m% > 2 × its 24 h median (S2's rule)
  B       = XUSDT if dev > 0 else XTMN                                    # the cheap book
  cost_B  = mk(B) + tk(B) + spread(B)
  mean    = the 1-h mean of dev (the z-score's)
  target  = mean + (dev − mean) × (1 − revert_target_fraction)            # 60 % of the gap to the mean closed
  capture = revert_target_fraction × |dev − mean|                         # what the exit at the target earns
  enter when z is defined, |z| ≥ z_min (2.0), z and dev have the same sign, capture ≥ cost_B + m
  expected_edge_pct = capture  (the RiskManager re-checks it against the round trip + spread + margin)
  BUY on B as maker at bid + 1 tick (the bid if that would cross)
  qty    = floor(min(20 % × E × k / price in TMN, free quote × (1 − 0.5 %) / (price × (1 + mk(B)))))
  stop   = price × (1 − revert_stop_pct)  → server STOP_LIMIT after the fill (limit = stop × (1 − 0.4 %))
exit, whichever comes first:
  target     bought XTMN: dev ≥ target; bought XUSDT: dev ≤ target  → SELL on B, maker-first (2 re-prices, then crossing)
  stop       bid ≤ stop (exchange STOP_LIMIT; a software stop if the exchange refuses it)
  time stop  revert_max_hold_s (1,200 s) after the fill → SELL crossing
  flatten / kill-switch
```

**In plain language.** When the books disagree by more than usual (≥ 2σ of the last hour) but not enough to lock in,
buy only the cheap side as a maker and sell it in the same book once 60 % of the gap to the hour's normal level has
gone; a 1 % exchange stop and a 20-min clock bound it, one at a time. Only that 60 % is profit, so it must pay the round
trip and the margin on its own: a gap of ≥ 0.63 % (+ spread / 0.6) to the hourly mean in the USDT book, ≥ 1.17 % in the
TMN book. A gap that large is usually lockable, so reverts mostly happen when wide spreads kill the lock-in edge.

### 2.5 Parameters (`strategies.s1_crossbook`) and why

| Parameter | Value | Why |
|---|---|---|
| `coins` | BTC, ETH, SOL, XRP, DOGE, ADA, TRX, LTC, LINK, AVAX | allowed majors with both books. Only BTC, ETH, SOL, XRP and DOGE are on the watchlist today, so the rest are skipped ("no cross features") |
| `margin_min_pct` | 0.15 % | covers what can go wrong between legs: book moves in the ~2–4 s before leg 2, partial fills, conversion error. ≈ ⅓ of the fees |
| `leg1_taker` | true | lags last seconds, so a resting maker leg 1 would mostly miss them. Costs +0.05 % (TMN) / +0.03 % (USDT), already in `cost` |
| `leg2_max_loss_pct` | 0.30 % | a known small loss beats an unhedged coin position |
| `lockin_enabled`, `revert_enabled` | true, true | lock-in is the edge; revert is a small, bounded extra |
| `z_min`, `dev_window_s` | 2.0, 3,600 s | "unusual" = 2σ from the deviation's own last hour: long enough for normal noise, short enough to follow the dollar |
| `revert_target_fraction`, `revert_stop_pct` | 0.6, 1.0 % | the last part of a gap converges slowest, so the exit takes 60 % of the gap to the hourly mean, and the entry must pay for itself out of that 60 %; a 1 % stop is far outside the traded deviations, so it fires only when the thesis is wrong |
| `revert_max_hold_s`, `revert_max_concurrent` | 1,200 s, 1 | lags resolve in minutes; revert is directional, so hold at most one |
| `cooldown_s`, `max_concurrent` | 60 s, 2 | never enter one dislocation twice (= the survey's 60-s rule); the RiskManager also caps S1 at 2 positions, all at 3 |
| `size_pct_equity` | 20 % | under the 25 % per-order cap and leaves room for a second cycle and S2. 10 % in the first 6 h live (`configs/live_halfsize.yaml`) |

**The plan's effect on S1** (section 6). Without `"S1"` in `enabled_strategies` there are no new entries, but open
cycles are still tended: leg 2, degrade stops and revert exits all continue. `size_multiplier` scales the 20 % budget, and 0 blocks
entries but never leg 2. The margin is `max(config, plan)`. `chaos` on any of the coin's three books blocks
revert entries, while lock-in, being direction-neutral, still runs.

---

## 3. S2 USDTAlloc — explicit dollar bet and regular activity

`src/wta/strategies/s2_usdtalloc.py`

**Thesis.** The challenge is valued in TMN, and USDTTMN moves 1–3 % a day. To trade the cheap USDT pairs (S1's USDT legs, S3) we must hold
USDT, which is an unavoidable position on the dollar. S2 makes that position explicit, bounded, regime-driven and
logged. In a range it keeps about half the equity in USDT, which gives S1 room to run in both directions. It leans
toward USDT when the dollar rises and toward TMN when it falls. Each rebalance is a cheap maker order on the deepest
Wallex market, and these orders are the agent's regular activity.

### 3.1 Regime, target, rebalance

```
regime = fresh plan's regimes["USDTTMN"].regime          if the fresh plan has a USDTTMN call
       = rule_regime(), first match wins:                otherwise
           trend_up    trend_flag > 0 and ret_24h > +0.8 %
           trend_down  trend_flag < 0 and ret_24h < −0.8 %
           chaos       atr_15m_pct > 2 × median(atr_15m_pct over the last 24 h)
           range       otherwise
  trend_flag  = sign(ema20 − ema60) if |ema20 − ema60| / ema60 > 0.15 % else 0  (15-min closes from 1-min candles)
  ret_24h     = newest close / close of the last 1-min bar at or before 24 h earlier − 1 (none if that bar is
                missing or more than a minute older: looked up by time, not by candle count); minutes without
                a candle are flat bars at the previous close (the candle cache fills them), so 15-min bars,
                EMAs and ATR measure time, not trades
  atr_15m_pct = Wilder ATR(14) on 15-min bars / last close
target = clamp(fresh plan's usdt_target_share  if a fresh plan exists,
               else target_share_by_regime[regime] (range 50 %, trend_up 65 %, trend_down 30 %, chaos 40 %),
               25 %, 70 %)                                   # a clamp is written into the reason
gap    = target − usdt_share

rebalance when ALL hold: S2 enabled (config and fresh plan); not flattening; USDTTMN book two-sided, not crossed;
  |gap| ≥ rebalance_threshold_pp (15 pp); no S2 order open;
  ≥ min_rebalance_interval_s (30 min) since the last rebalance that filled; ≥ 120 s since the last attempt
  or the last order that ended unfilled
order: BUY USDTTMN if gap > 0 else SELL, maker LIMIT at bid + 1 tick (SELL: ask − 1 tick) while that does not cross
  qty = floor(|gap| × E / price), must meet min notional (50,000 TMN); purpose "rebalance", no edge required
  the RiskManager caps one order at 25 % of equity, so a larger gap closes in steps;
  the OrderManager re-prices a maker order at most twice, then gives up (S2 never crosses)
```

A strong trend wins over high volatility. S2 has no positions to exit: its USDT is a balance that later rebalances
move and FLATTEN sells before the end.

**In plain language.** Keep half the account in dollars normally, about two thirds when the dollar is clearly rising,
under a third when it is clearly falling, 40 % when it is erratic, never below 25 % or above 70 %; trade only when the
share is ≥ 15 points off target, at most every 30 minutes, with patient maker orders.

**Initial allocation (the first trade).** Starting at 100 % TMN, S2 buys on the first tick with a USDTTMN book. The 25 %
per-order cap splits the move: a 50 % target takes two orders 30 min apart, 65 % takes three. With the half-size overlay
(12.5 % per order) a 50 % target stops at 37.5 %, because the remaining 12.5 pp gap is below the threshold.

### 3.2 Parameters (`strategies.s2_usdtalloc`) and why

| Parameter | Value | Why |
|---|---|---|
| `target_share_by_regime` | range 50 %, trend_up 65 %, trend_down 30 %, chaos 40 % | 50 % is neutral and lets S1 run both ways; ±15–20 pp expresses a dollar view without betting the account; chaos leans to TMN (the valuation currency) but keeps USDT for the USDT books |
| `bounds` | [25 %, 70 %] | never all-in on either currency. The RiskManager enforces the same band (`USDT_BOUNDS`) for **every** strategy's USDT-moving entry |
| `rebalance_threshold_pp`, `min_rebalance_interval_s` | 15 pp, 1,800 s | hysteresis: a 15 pp step costs ≈ 0.04 % of equity in fees, smaller ones churn; ≤ 2 rebalances an hour bounds fee drag and plan whipsaw |
| `ema_fast` / `ema_slow`, `trend_flag_min_gap_pct` | 20 / 60 bars of 15 min, 0.15 % | a 5-h vs a 15-h trend; the gap must be ≈ 10× the USDTTMN spread (0.014 %) to count |
| `trend_ret_24h_pct`, `chaos_atr_mult` | 0.8 %, 2.0 | the dollar moves 1–3 %/day, so 0.8 % separates a move from noise; volatility at 2× its 24-h median is "chaos" |

**The plan's effect on S2.** A fresh plan's `usdt_target_share` **is** the target, clamped to [25 %, 70 %]. The plan's
USDTTMN call supplies the regime label and evidence, and the rules' regime is logged next to it, so plan and rules can
be compared line by line. Without `"S2"` in `enabled_strategies` there are no rebalances. `size_multiplier` is not used,
because S2's size is the gap. **Rules-only warm-up:** `ema60` needs 15 h of 1-min candles and `ret_24h` needs 24 h. Until then
the rules say `range` (or `chaos`). `python -m wta backfill` avoids the warm-up.

---

## 4. S3 RegimeMomentum — bounded upside (disabled by default)

`src/wta/strategies/s3_momentum.py` · `strategies.s3_momentum.enabled: false`. It is switched on only after the MVP is
live **and** the evidence in section 11 supports it.

**Thesis.** USDT-pair fees are low (≈ 0.23 % round trip), so a conservative breakout strategy on BTC, ETH and SOL can pay, but
only in a trending market. Whether the market is trending is exactly the judgement the LLM analyst is asked to make. S3 is
therefore gated by the AI, bounded by a small risk budget and an exchange-side stop, and long only (spot).

### 4.1 Entry

```
once per completed 15-min bar per symbol (BTCUSDT, ETHUSDT, SOLUSDT), when settled (a 1-min candle of the next bar exists or
2 candle polls have passed); a bar that closed ≥ 15 min ago is stale and skipped; a bar is consumed once evaluated,
even when the entry is blocked, so a signal is never acted on late and never retried on the same bar
breakout  = close > max(high of the previous 20 completed bars)
            and bar volume / mean volume of those 20 bars ≥ 1.5
            and 0.3 % ≤ ATR(14) / close ≤ 3 %
regime_ok = the fresh plan's call for the symbol: regime == trend_up and confidence ≥ 0.6
            | no call (or no fresh plan) and allow_rules_only: trend_flag > 0 and ret_24h > 0.8 %   (default: off)
blocked while any S3 position or S3 order exists, the symbol is held or worked by another strategy (or is an
S1 lock-in's leg-2 book), or k = 0;
several breakouts on one bar → the strongest (distance above the 20-bar high in ATR units) wins
price     = bid + 1 tick while below the ask (maker LIMIT; the OrderManager re-prices ≤ 2, then gives up)
E_usdt    = E / u_bid
qty       = floor(min( min(0.5 % × E_usdt / (1.5 ATR),  20 % × E_usdt / price) × k,
                       free USDT × (1 − 0.5 %) / (price × (1 + mk)) ))
stop      = price − 1.5 ATR → server STOP_LIMIT once filled (limit = stop × (1 − 0.4 %))
target    = price + 2.5 ATR → software target watched by the PositionManager
time stop = 4 h;  expected_edge_pct = (target − price) / price  (RiskManager: ≥ maker + taker + spread)
```

### 4.2 Exits, whichever comes first

| Exit | Condition | Order |
|---|---|---|
| `stop` | bid ≤ entry − 1.5 ATR | server STOP_LIMIT; a software taker exit if the exchange refused it or the price gapped through its limit |
| `target` | bid ≥ entry + 2.5 ATR | SELL maker-first, crossing after 2 re-prices |
| `time_stop` | 4 h after the fill | SELL crossing |
| `regime_flip` | the fresh plan now calls the symbol `trend_down` or `chaos` | SELL crossing; emitted even when S3 is disabled by config or plan (it only reduces risk) |
| `flatten` / `kill` | deadline or kill-switch | FLATTEN routine |

**In plain language.** When the AI says a market trends up (≥ 60 % confidence) and a 15-minute candle closes above the
previous five hours' high on 1.5× normal volume with normal volatility (0.3–3 %), buy as a maker. The stop rests on the
exchange 1.5 ATR below (it survives a bot crash); take profit at 2.5 ATR, give up after 4 h, leave if the AI changes its mind.

**Break-even** (why it stays off without evidence). With ATR% `a` and round-trip cost `c` ≈ 0.3 %, a win is `2.5a − c`
and a loss `1.5a + c` (more if the stop slips): break-even win rate `(1.5a + c) / 4a` = 62.5 % at `a` = 0.3 %, 52.5 % at
0.5 %, 45 % at 1 %, 41 % at 2 %.

### 4.3 Parameters (`strategies.s3_momentum`) and why

| Parameter | Value | Why |
|---|---|---|
| `enabled` / `allow_rules_only` | false / false | no evidence yet; S3 is the strategy the AI gates, so it does not trade without a plan |
| `symbols` | BTCUSDT, ETHUSDT, SOLUSDT | the deepest USDT books, the cheapest fees |
| `breakout_lookback`, `volume_mult` | 20 bars of 15 min, 1.5 | a 5-hour high is a real level; a breakout without volume is usually fake |
| `atr_min_pct` / `atr_max_pct` | 0.3 % / 3 % | below 0.3 % the target barely covers costs; above 3 % stops become gap-prone |
| `stop_atr_mult` / `target_atr_mult`, `max_hold_s` | 1.5 / 2.5, 14,400 s | reward:risk 1.67 before costs; a breakout that has not worked in 4 h has failed |
| `risk_per_trade_pct` | 0.5 % | ≈ 25,000 TMN at the stop on 5M TMN, before slippage |
| `max_size_pct_equity` | 20 % | binds whenever ATR% < 1.67 %, so the real risk is usually **below** 0.5 % (ATR 0.5 % → 0.15 % of equity) |
| `min_regime_confidence` | 0.6 | the Reviewer may set 0.5–0.9 |

**The plan's effect on S3.** `regimes[symbol]` gates every entry and triggers the regime-flip exit. `size_multiplier`
scales the risk-based quantity and the 20 % cap alike. Without `"S3"` in `enabled_strategies` there are no entries. With
`ai.enabled=false` the plan is never read. S3 is therefore the clearest measure of what the AI adds (ablation, section 11).

---

## 5. FLATTEN — everything back to TMN

`src/wta/risk/flatten.py`, on the deadline clock (`CHALLENGE_END_UTC`). Only TMN counts at the official end, so the agent
must finish at ≈ 100 % TMN. Nobody can switch this off: the AI cannot touch it, and live refuses to start without an end time.

| Time | What happens |
|---|---|
| T − 2 h | S1/S2/S3 stop (they emit nothing while flattening). Entry and rebalance orders are canceled, and only SELLs pass the RiskManager. Each non-TMN asset is sold with a maker-first LIMIT, re-priced ≤ 3 times, then crossing |
| T − 30 min | Leftovers are sold with MARKET, or with a crossing LIMIT at `bid × (1 − 0.5 %)` where MARKET is disabled (e.g. SOLTMN) |
| T − 5 min | A `final_state` event records balances, equity, TMN share, and whatever is not TMN (dust listed separately) |
| T | All open orders are canceled; nothing is placed again |

Coin X is sold on `XTMN` if allowed, else on `XUSDT` (that USDT goes on USDTTMN in a later round). One SELL per asset at
a time makes the routine idempotent and restart-safe. Free coins go first, then each position's (its stop is canceled
just before; `exit_reason: flatten`). A balance below the minimum order is **dust**: reported, never topped up (flatten
never buys). The kill-switch (drawdown ≥ 5 %, a `KILL` file, repeated crashes) runs phase 1 with `exit_reason: kill`.

---

## 6. How the AI plan changes each strategy

The Regime Analyst (`src/wta/ai/analyst.py`) runs every 300 s, at startup, on a kill / pause / flatten-start event, and
when USDTTMN moves > 1 % or BTCUSDT > 2 % in 15 min (never within 60 s of the last run). The model reads features
through tools (internal, plus Wallex MCP read-only tools when reachable) and returns strict JSON, which is parsed (one
"fix the JSON" retry) and **clamped**; every changed value is recorded in `plan.clamped`. The plan is an **envelope**:
it can turn strategies off, raise S1's bar, scale size down, call regimes and set the USDT target. It cannot enable a
strategy the config disables, lower S1's bar, size above 100 %, leave 25–70 % USDT, or touch risk limits or FLATTEN.

| Plan field | Bound | S1 | S2 | S3 |
|---|---|---|---|---|
| `enabled_strategies` | ⊆ strategies enabled in config | absent → no new cycles (open ones tended) | absent → no rebalances | absent → no entries (regime-flip exits continue) |
| `size_multiplier` | [0, 1] | scales the 20 % budget; 0 = no entries | not used | scales qty; 0 = no entries |
| `s1_margin_min_pct` | [0.08 %, 1 %]; S1 uses `max(config, plan)` | raises the entry bar | — | — |
| `usdt_target_share` | [25 %, 70 %] (clamped again by S2 and enforced by `USDT_BOUNDS`) | — | the target | — |
| `regimes[sym]` | 4 labels, confidence ∈ [0, 1]; USDTTMN, BTCUSDT, ETHUSDT, SOLUSDT required | `chaos` blocks revert | label + evidence in the reason | gate and regime-flip exit |
| `risk_flags`, `notes` | ≤ 12 flags, ≤ 2,000 chars | logged | logged | logged |

**Freshness and fallback.** Strategies read only a plan ≤ 1,200 s old (`ai.plan_stale_s`: 3 missed runs are tolerated).
An older plan, a failed run (provider down, JSON invalid twice) or `ai.enabled=false` means **rules-only**: S1 uses the
config margin, `k = 1` and its own ATR chaos test; S2 uses `rule_regime()` and its table; S3 makes no entries (unless
`allow_rules_only`). Each
strategy re-applies its own bounds, and the RiskManager checks every order whatever the plan says.

**Offline rules provider — not an LLM.** With `ai.enabled=true` and no OpenRouter/Gemini key,
`src/wta/ai/offline_provider.py` answers the analyst's prompt with fixed rules, so the pipeline (prompt → tool calls →
JSON → validation → clamp → cache → replay) still runs: each regime is S2's `rule_regime` on that symbol's features;
`enabled_strategies` = S1 + S2 (never S3); `size_multiplier` 1, 0.5 above 2 % drawdown, 0 when any regime is `chaos`;
USDT target from S2's table; S1 margin from config. It is labelled everywhere (model `offline-rules-v1`, notes starting
`[offline rules provider — not an LLM]`, health/TUI `AI: offline rules (no LLM key)`). **Its runs are not evidence of LLM value.**

**Reviewer (hourly)** may change only `s1.margin_min_pct` ∈ [0.08 %, 0.50 %], `s1.leg1_taker`,
`s2.rebalance_threshold_pp` ∈ [10, 25] pp, `s3.min_regime_confidence` ∈ [0.5, 0.9] and `engine.slow_loop_interval_s` ∈
[180, 900] s: ≤ 2 changes per review, ≥ 1 h apart, each logged as a new `config_hash`. Risk parameters are never editable.

> **Status:** the strategies run in the engine's fast loop; the analyst, reviewer and offline provider are merged and
> unit-tested but **not wired into the engine yet**. Until they are, there is no plan, the strategies run rules-only and
> `plan_snapshot` is `null`. The engine applies the same freshness rule as the strategies: a decision record's
> `plan_snapshot` and the health line's `strategies.plan` show only a plan ≤ `ai.plan_stale_s` old (a stale one appears
> as `strategies.plan_stale`). `TODO(phase-4)`: replace the illustrative plan in section 8 with a real cached plan.

---

## 7. Sizing at 5,000,000 TMN

E = 5,000,000 TMN and USDTTMN bid = 266,720, so E ≈ 18.75 USDT.

| Strategy | Strategy-side formula | Full size | First 6 h live (`live_halfsize.yaml`) |
|---|---|---|---|
| S1 lock-in | min(20 % × E × k, 80 % of both crossed best levels, free quote − 0.5 % − fee); leg 2 = what is left after the coin fee, in whole steps | ≤ 1,000,000 TMN per cycle (≈ 0.000045 BTC; ≈ 3.75 USDT) | ≤ 500,000 TMN (BTC: u ≈ 4 %, need ≈ 0.70 %) |
| S1 revert | min(20 % × E × k, free quote − 0.5 % − fee) | ≤ 1,000,000 TMN; ≈ 10,000 TMN at the 1 % stop + slippage | ≤ 500,000 TMN |
| S2 | \|gap\| × E, then capped at 25 % by the RiskManager | initial 50 %: 1,250,000 TMN now + 1,250,000 TMN 30 min later | 625,000 TMN steps; a 50 % target ends at 37.5 % |
| S3 | min(0.5 % × E / 1.5 ATR, 20 % × E) × k | 1,000,000 TMN whenever ATR% < 1.67 %; risk at the stop = 20 % × 1.5 × ATR% | capped at 12.5 % by `MAX_ORDER_PCT` |

On top of every strategy, the RiskManager **shrinks** an entry when the order exceeds 25 % of equity, takes more than
80 % of the best level, or would push coins above 40 % of equity. It also shrinks an entry that would move the USDT share outside
[25 %, 70 %] (open orders counted as filled; for a lock-in, the whole two-leg cycle) or that needs more than the free
balance less 0.5 %. It **vetoes** an entry at the position caps (3 in total, S1 ≤ 2, S3 ≤ 1) or 8 open orders; above
10 placements per rolling hour or 60 per UTC day (each entry reserves 1 + 2 re-prices); at a daily loss of −2.5 %, a 5 %
drawdown (kill-switch → flatten) or the 2 % fee budget; on edge below cost, data older than 30 s, a price beyond ±3 % of
mid, a self-cross (wash-trade prevention) or a duplicate. Exposure limits never block an exit.

---

## 8. Example decision record (illustrative)

> **Illustrative, not a recorded trade.** Features, reasons and the 23 risk checks come from running the final S1
> code, the real FeatureEngine (an hour of made-up deviation samples) and the real RiskManager on **made-up books**
> (BTC rising in BTCUSDT while BTCTMN lags). The plan is invented; the order response and outcome are illustrative
> values consistent with the code's accounting; identity fields are placeholders and some keys are elided.
> `TODO(phase-5)`: replace with a real shadow/live record.

Situation: USDTTMN 266,720 / 266,758; BTCUSDT 83,450.00 / 83,510.00; BTCTMN 22,073,000,000 / 22,100,000,000;
account 2,500,000 TMN + 9.40 USDT (≈ 50 % USDT).

```json
{
  "kind": "order", "decision_id": "d_1792683012000_S1_BTCTMN_1", "ts": "2026-10-22T15:30:12.000Z",
  "mode": "shadow", "git_commit": "<sha>", "config_hash": "<config_hash>", "strategy": "S1", "symbol": "BTCTMN",
  "features": {"u_bid": "266720", "u_ask": "266758", "implied_mid_usdt": "82801.91", "dev_mid_pct": "-0.008123",
    "dev_mean_1h": "-0.00055", "dev_std_1h": "0.0026", "dev_zscore": "-2.91", "dev_samples": 1800,
    "edge_tmn_cheap_pct": "0.00714", "edge_usdt_cheap_pct": "-0.009156", "cost_pct": "0.0043",
    "margin_min_pct": "0.0015", "unhedged_stress_pct": "0.000577", "need_pct": "0.006377",
    "mode": "lockin", "side": "TMN_CHEAP", "leg2_symbol": "BTCUSDT",
    "sizing": {"binding_limit": "budget", "budget_qty": "0.000045", "free_quote_qty": "0.000112",
               "leg1_liquidity_qty": "0.048000", "leg2_liquidity_qty": "0.064000", "leg1_fee_pct": "0.003",
               "held_after_fee": "0.000044865", "leg2_sell": "0.000044", "unhedged": "0.000000865",
               "unhedged_pct": "0.0192222", "qty": "0.000045"},
    "books_used": ["BTCTMN", "BTCUSDT", "USDTTMN"]},
  "plan_snapshot": {"ts": 1792682872.0, "model": "<analyst model>", "cached": false, "clamped": {},
    "regimes": {"USDTTMN": {"regime": "range", "confidence": 0.7, "evidence": "ema gap +0.04%, 24h +0.31%"},
                "BTCUSDT": {"regime": "trend_up", "confidence": 0.64, "evidence": "1h +1.1%, ema20 > ema60 by 0.35%"}},
    "enabled_strategies": ["S1", "S2"], "s1_margin_min_pct": "0.0015", "size_multiplier": "1",
    "usdt_target_share": "0.5", "risk_flags": [], "notes": "BTC rising fast in the USDT book, dollar flat."},
  "intent": {"side": "BUY", "order_type": "LIMIT", "qty": "0.000045", "price": "22100000000", "purpose": "entry",
    "taker": true, "expected_edge_pct": "0.00714",
    "reason": "BTC: BTCTMN ask 22,100,000,000 TMN / USDTTMN bid 266,720 = 82,858.43 USDT vs BTCUSDT bid 83,450.00 -> TMN book 0.714% cheap; cost 0.430% (0.300% taker BTCTMN + 0.130% taker BTCUSDT); edge - cost = 0.284% >= margin min 0.150% = max(config 0.150%, plan 0.150%); unhedged remainder: buying 0.000045 BTC leaves 0.000044865 after the 0.300% leg-1 fee (paid in BTC) and leg 2 on BTCUSDT (step 0.000001) can sell 0.000044: the unhedged 0.000000865 BTC (1.922% of the cycle) x a 3% adverse move = 0.058%, so edge 0.714% >= need 0.638%; lock-in: buy 0.000045 BTC on BTCTMN at 22,100,000,000 (994,500.00 TMN) as taker, then sell the 0.000044 BTC left after the leg-1 fee on BTCUSDT at its bid 83,450.00 (leg 2); size 0.000045 BTC limited by budget (budget 0.000045, free_quote 0.000112, leg1_liquidity 0.048000, leg2_liquidity 0.064000); plan size mult 1, regime BTCUSDT trend_up (0.64), USDTTMN range (0.70)."},
  "risk": {"approved": true, "adjusted_qty": "0.000044", "veto_reason": null, "checks": [
    {"code": "USDT_BOUNDS", "passed": true, "detail": "adjusted qty 0.000045 -> 0.000044: USDT share 50.07% -> 70.07% (lock-in cycle incl. leg 2 SELL on BTCUSDT) would cross 70%; sized to stop at the bound"},
    {"code": "FEE_BUDGET", "passed": true, "detail": "fees paid 0.0000 TMN + expected 4,260 TMN (0.300% taker + 0.130% taker leg 2 on BTCUSDT) = 4,260 TMN <= budget 100,000 TMN (2.0% of initial)"},
    {"code": "EDGE_VS_COST", "passed": true, "detail": "edge 0.714% vs cost 0.430% (lock-in: 0.300% taker BTCTMN + 0.130% taker BTCUSDT) + margin 0.150% = 0.580%"}]},
  "order": {"request": {"client_id": "S1_BTCTMN_1792683012000_B109", "type": "LIMIT", "side": "BUY",
    "qty": "0.000044", "price": "22100000000"}, "accepted": true, "response": {"status": "NEW", "filled_qty": "0"}},
  "outcome": null
}
```

The real record lists all 23 checks in fixed order (the 20 elided here passed). `features` holds every number S1 used,
`plan_snapshot` the AI envelope in force (only a fresh plan is logged), `intent.reason` one sentence with the numbers.
`USDT_BOUNDS` **shrank** the cycle: 0.000045 BTC would have taken the post-leg-2 USDT share to 70.07 %. The shrunk
0.000044 BTC still lands on a leg-2 step after the fee (0.000043868 held, leg 2 sells 0.000043 = 3.59 USDT ≥ 1 USDT), so
no "lock-in leg 2" check is appended; had it fallen below the leg-2 minimum, the entry would have been vetoed. The `S1_`
client-id prefix marks algorithmic origin; the log is append-only, so `outcome` arrives later on its own line.

Leg 1 fills 0.000044 BTC; the 0.30 % BUY fee is charged in BTC, so 0.000043868 BTC are held. The leg-2 intent, from the
same code: "BTC: lock-in leg 2 on the leg-1 fill of 0.000044 at 22,100,000,000: 0.000043868 BTC held from leg 1 on
BTCTMN at avg 22,100,000,000, 0 already in open leg-2 orders; sell 0.000043 BTC on BTCUSDT at bid 83,450.00 (3.59 USDT,
crossing, taker 0.130%); cycle edge now 0.284% after 0.430% fees (entry edge 0.714% before fees) is above the -0.30%
leg-2 loss limit." When leg 2 fills, the outcome line is appended:

```json
{"kind": "outcome", "decision_id": "d_1792683012000_S1_BTCTMN_1", "ts": "2026-10-22T15:30:16.100Z",
 "outcome": {"pnl_tmn": "2599.86", "pnl_pct": "0.002674", "hold_s": 2.0, "exit_reason": "lockin",
             "position_id": "p_S1_BTCTMN_1792683014100_1", "dust_qty": "0.000000868"}}
```

That is +2,600 TMN (+0.27 % of the 972,400 TMN cycle) after ≈ 4,160 TMN of fees: leg 2 nets 3.583685 USDT × 266,720 =
955,841 TMN, and the 0.000000868 BTC of dust (below the minimum order, section 13) is valued at the BTCTMN bid (19,159 TMN).

**S2's first trade** (real S2 code, rules-only, dollar trending up; illustrative inputs): "USDTTMN regime=trend_up
[rules] (ema20>ema60 by 0.40%, 24h +2.40%, ATR15m 0.210% vs 24h median 0.180%; trend_flag +1 and 24h > +0.80%); target
USDT share 65% (rules table for trend_up) vs current 0.0% -> gap +65.0 pp >= threshold 15 pp; initial allocation from
100% TMN (no S2 rebalance has filled yet); buy 3,250,000 TMN of USDT (12.18 USDT, 65% of equity 5,000,000 TMN) as maker
at 266,721 (bid 266,720 / ask 266,758); the RiskManager caps one order at 25% of equity (1,250,000 TMN), the rest of the
gap waits for the next rebalance." `MAX_ORDER_PCT` then shrinks the order from 12.18 to 4.68 USDT.

---

## 9. Expected behaviour

- **S1 will often do nothing for hours, and that is correct.** In the one observed snapshot (Overview §2.1) the BTC
  books disagreed by −0.07 %, inside the spread, while S1 needs ≈ 0.58 % executable. Expect **bursts of 1–5 cycles
  during fast moves** of BTC or the dollar; how often they happen is what the survey must measure (section 11).
- **S2 provides the regular activity**: the first trade (2–3 steps, 30 min apart), then 1–4 rebalances a day when the
  regime changes the target or S1 cycles move the share by ≥ 15 pp. **Caveat:** at 50 % USDT a 2 % dollar move shifts
  the share by only ≈ 0.5 pp, so a day with no regime change and no S1 cycle can be quiet.
- **S3 is off.** When enabled: ≤ 1 position, ≤ 1 decision per 15-min bar, 0–3 trades a day.
- **Overall:** 5–30 orders a day (hard cap 10 per hour, 60 per day), 2–10 round trips, fees of 40,000–90,000 TMN over 3
  days (0.8–1.8 % of equity), inside the 2 % budget (PRD-03 §7). **FLATTEN:** a few SELLs in the last 2 h, then
  silence; `final_state` should show ≈ 100 % TMN plus documented dust.
- **Visible without trades:** each strategy's `diagnostics()` explains every skip (S1 per coin: edge, cost, need,
  deviation, z, `why_no_trade`; S2: regime, source, target, gap, status; S3: per-symbol bar status). Every heartbeat
  carries them in `health.extra.strategies` (S1 also with the engine's per-coin opportunity-tick counters), and an
  intent the router drops gets a `no_trade` decision record.

---

## 10. Tuning discipline (survey, walk-forward, overfitting guard)

**The defaults here were set by design** from fees, one observed spread snapshot and the PRDs. **They were not fitted
to data**, because no Wallex data has been recorded yet. Any change must follow these rules:

1. **S1 survey rule** (PRD-05 §2). From recorded books, count distinct lock-in opportunities (edge ≥ cost + m, ≥ 60 s
   apart) for m ∈ {0.05, 0.10, 0.15, 0.25, 0.40} %, taker and maker leg 1. Lock-in is **viable** at ≥ 3 a day at
   m ≥ 0.10 %. With fewer than 2 a day at 0.15 %, lower `margin_min_pct` to 0.08 % and/or set `leg1_taker: false`;
   that change must then pass rule 2 when the data allows.
2. **Walk-forward split** (PRD-05 §3.4). Every grid is evaluated in-sample on the first **70 %** of the window and
   out-of-sample on the last **30 %**; the report shows both. The best in-sample value is adopted only if out-of-sample
   it **does not degrade by more than 50 %**, else the pre-grid default stays. Our reading: out-of-sample net return per
   day after fees ≥ 50 % of the in-sample one, and a non-positive in-sample return is never adopted.
3. **Small grids.** ≤ **8 values per parameter**, ≤ **2 parameters at a time**. Planned: `s1.margin_min_pct ∈ {0.08,
   0.12, 0.15, 0.25} %` × `leg1_taker`, and `s2.rebalance_threshold_pp ∈ {10, 15, 20, 25} pp`; S3 not until enabled.
4. **Same bounds as the AI** (the Reviewer's, `configs/llm.yaml`); risk parameters are never tuned for return. Every
   accepted change gives a new `config_hash` on every later decision and a `config_change` event.

---

## 11. Evidence (TODO)

Every number here must come from a report under `docs/REPORTS/` that a command regenerates. **Synthetic-feed runs and
offline-rules-provider runs never count as evidence.** List the placeholders with `grep -n "TODO(" docs/STRATEGY.md`.

| # | Evidence | Source | Status |
|---|---|---|---|
| 11.1 | **S1 survey**: distinct lock-in opportunities/day per coin at m ∈ {0.05, 0.10, 0.15, 0.25, 0.40} %, median duration, maker-leg-1 count; spreads/depth per market; USDTTMN time per regime; computed recommendation block | `make survey` → `docs/REPORTS/survey_<date>.md` | `TODO(phase-5)` |
| 11.2 | **Candle backtest** (30 d, S2+S3, AI off): return, max DD, trades, win rate, fees | `docs/REPORTS/bt_candles_30d/` | `TODO(phase-5)` |
| 11.3 | **Book backtest + ablation** (recorded window, S1+S2, AI off vs on with a real LLM): the same metrics plus vetoes by code and S3 trades gated by regime | `docs/REPORTS/bt_books_<window>/` | `TODO(phase-5)` |
| 11.4 | **Walk-forward** per grid (section 10): best in-sample value, in/out-of-sample return per day, degradation, adopted? | backtest `--param-grid` report | `TODO(phase-5)` |
| 11.5 | **Shadow ≥ 4 h, real feed**: equity curve, trades, vetoes by code, S1 opportunities seen vs taken, S2 initial allocation | `docs/REPORTS/shadow_<date>/` | `TODO(phase-5)` |
| 11.6 | **Live run**: return, max DD, trades per strategy/day, fees vs budget, final TMN share and dust, live tags | `deliverables/REPORT.md`, `TAGS.md` | `TODO(phase-6)` |
| 11.7 | **Replay proof**: "N/N decisions reproduced" | `make replay` → `docs/REPORTS/replay_<date>.md` | `TODO(phase-5)` |

S1 is **not** backtested on candles (they carry no cross-book information); its evidence is the survey, the book-driven
window and the live logs.

---

## 12. Where the code differs from PRD-03

The details are in sections 2–4; in short, the code is stricter than the PRD:
- **S1:** its own book-freshness gate; free quote less a 0.5 % buffer **and** the fee; leg 2 sized to the coins left
  after the coin-paid leg-1 fee (PRD-03: "the same quantity") and the remainder priced into the need; a 60-s coin
  cooldown on top of the router's 30 s; `USDT_BOUNDS` applied to the **whole cycle** (PRD-03 bounds only S2) and leg
  2's minimum checked before leg 1 is approved; leg 2 judged after **both** fees, derived from the portfolio, retried
  every tick and never chased by the OrderManager; the leg-2 book reserved in the router for the whole cycle; revert
  measured from the 1-h mean and gated on the captured 60 % (PRD-03: |dev| ≥ cost + margin); chaos blocks revert per
  book, also in rules-only mode.
- **S2:** a 120-s retry wait; the 30-min interval counts from the last rebalance that **filled**.
- **S3:** a software target (a resting STOP_LIMIT most likely locks the coins, so a second resting SELL would be
  rejected; `docs/DECISIONS.md`); one evaluation per settled bar; `size_multiplier` also scales the 20 % cap.

---

## 13. Known limitations

- **No real Wallex data yet.** `api.wallex.ir` drops the TLS handshake from the development machine (likely IP
  filtering; `docs/DECISIONS.md`, `docs/REPORTS/api_probe.md`); the recorder retries forever and, at the time of writing,
  has stored no market data. **Every run so far used the synthetic feed** (deterministic, seeded from one snapshot in
  `configs/markets_snapshot.yaml`): it exercises the engine end to end, is never evidence of performance, and every
  `start` event and heartbeat says so. We do not know whether S1 opportunities occur several times a day or not for days.
- **Defaults, not fits** (section 10). Step sizes and min quantities in `configs/markets_snapshot.yaml` are
  `verified: false`; `/v1/markets` and `/v1/account/fee` have never been reached from this machine.
- **AI layer not integrated yet.** S1, S2 (and S3 when enabled) run in the fast loop through the router; the analyst,
  reviewer and offline provider are unit-tested but not plugged in, so every run is rules-only. `TODO(phase-4)`.
- **S1 legs are not atomic.** Leg 2 follows the leg-1 fill ≈ 2–4 s later. An unfilled crossing leg 1 is never
  re-crossed: it rests at its limit for `limit_reprice_after_s` (15 s), then expires, and S1 re-evaluates the edge on a
  later tick; an unfilled leg 2 is likewise given up and re-decided by S1 at the new bid. The leg-2 rule bounds the
  damage (≤ 0.3 % loss, or a degrade worth ≈ 0.4 % of equity).
- **A stale book looks like a lagging book.** S1 trades exactly the lag a stale feed creates, and its only freshness
  gate is the global 30-s limit; a tighter S1-specific book-age limit would be safer. (A stalled book at least no
  longer distorts the z-score: it adds no samples.)
- **"Locked" means locked in leg 2's currency.** A TMN-cheap cycle ends in USDT, whose TMN value moves with the dollar (S2's job).
- **Revert is half a trade.** If the gap closes because the **expensive** book falls, the target fires with our coin
  unchanged: roughly flat minus fees. The z-score needs only 30 correlated 2-s samples, so `|z| ≥ 2` is a threshold, not a probability.
- **Granularity and dust.** If the BTC step of 0.000001 is right, one step ≈ 22,000 TMN ≈ 2 % of a cycle. A coin-charged
  BUY fee leaves a sub-step remainder (≈ 19,000 TMN in section 8). S1 buys no coins leg 2 cannot sell beyond that
  remainder and charges `u × 3 %` for it (2.2), which raises BTC's lock-in need to ≈ 0.64 % at 1M TMN and ≈ 0.70 % at
  half size and practically excludes DOGE (u ≈ 4.5 %). The remainder is valued in PnL and sold by FLATTEN once the
  dust of a coin reaches the minimum order; anything below the 50,000 TMN minimum at the end does not count as TMN.
- **S2's dollar bet can lose**: at 50 % USDT a 2 % dollar move is ±1 % of equity. In a flat market, buying 2.5M TMN of
  USDT and flattening it costs ≈ 0.25–0.28 % of equity (+ ≈ 0.04 % per rebalance). Activity is not guaranteed (section 9).
- **S3** has no evidence and needs ≈ 52.5 % wins at ATR 0.5 %; its stop and target are fixed from the signal price; a
  STOP_LIMIT can be gapped through (the PositionManager then exits as taker); at ≈ 0.000044 BTC, steps are coarse.
- **Paper fills** are pessimistic (next tick, ≤ 80 % of the level) but cannot model queue position. **Three days** is
  short: Sharpe-like statistics are reported, not over-interpreted (PRD-05 §4).

**Code map.** `src/wta/strategies/` (`base.py`, `router.py`, `s1_crossbook.py`, `s2_usdtalloc.py`, `s3_momentum.py`) ·
`src/wta/features/engine.py` · `src/wta/risk/` (`manager.py`: 23 checks; `flatten.py`) · `src/wta/execution/`
(`order_manager.py`, `positions.py`) · `src/wta/ai/` (`analyst.py`, `plan_schema.py`, `offline_provider.py`, `reviewer.py`)
· `configs/` (`default.yaml`, `live_halfsize.yaml`, `llm.yaml`) · `DecisionRecord` in `src/wta/models.py` (`python -m wta logs check`).
