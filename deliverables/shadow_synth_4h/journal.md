# Trading journal

Written by the Post-trade Reviewer (PRD-04 §7), one section per review, newest at the bottom.

## Review 2026-10-22T23:53:41.240Z

- config_hash: `481cc6dc3ce2` -> `481cc6dc3ce2` (unchanged)
- window: 2026-10-22T22:53:41.240Z .. 2026-10-22T23:53:41.240Z
- llm: openrouter/openai/gpt-6-luna-pro, cache key `58482f52a9163b2c`

- 11 plans kept S2 enabled; S1 stayed off as watched cross-book net edges were negative after fees (about -0.47% to -0.69% in one plan).
- One reported S2 rebalance order was accepted; its outcome reports FILLED, quantity 4.68 and fee 0.011700 USDT.
- USDT share moved from about 24.92% in early plans to about 49.8% later, near the 50% target.
- No window PnL was recorded (0 TMN); daily PnL was -13,168.86 TMN (-0.2634%).
- Fees were 6,239.79 TMN, or 6.24% of the 100,000 TMN budget; no open orders or positions remained.
- No risk vetoes were recorded. Health reports zero API errors and zero stale events, despite repeated plan flags for unhealthy/stale heartbeat.
- Reporting anomalies: 1 orphan outcome, and the listed order/outcome counts do not reconcile with 3 orders last hour.

**Alerts**
- Investigate the orphan outcome and order-count discrepancy (1 listed order vs. 3 orders last hour).
- Reconcile repeated unhealthy/stale heartbeat plan flags with health reporting zero errors and stale events.

## Review 2026-10-23T00:53:45.050Z

- config_hash: `481cc6dc3ce2` -> `481cc6dc3ce2` (unchanged)
- window: 2026-10-22T23:53:45.050Z .. 2026-10-23T00:53:45.050Z
- llm: openrouter/openai/gpt-6-luna-pro, cache key `03fe1e82ba77bd61`

- 12 plans were recorded; all enabled S2, with a neutral 50% USDT target while reported USDT share stayed near target (49.77%–49.81%).
- No decisions, orders, open positions, or vetoes were recorded; window PnL was 0 and strategy-level PnL was unavailable.
- S1 remained disabled across plans because BTC, ETH, and SOL net cross-book edges were repeatedly described as negative after fees.
- Plans consistently used `s1_margin_min_pct` 0.0018; no outcomes support changing the configured threshold.
- Portfolio daily PnL was +6,236.85 TMN, but realized PnL was 0 and equity was 4,992,050.29 TMN versus 5,000,000 initial equity.
- Fees paid totaled 6,239.79 TMN, or 6.2398% of the 100,000 TMN budget; no orders occurred in this hour.
- Early plans flagged `heartbeat_unhealthy`; later plans did not. Health reported 0 API error records, 0 paused, and 0 killed.
- One plan reported `clamped: ["regimes.dropped"]`; no material trading activity followed.
- No parameter changes are supported by this hour's evidence.

**Alerts**
- Reconcile PnL/equity metrics: daily PnL is +6,236.85 TMN while return is -0.1590% and equity is below initial; window PnL and realized PnL are both 0.
- Investigate the early `heartbeat_unhealthy` plan flags despite 0 health API-error records and no pause or kill.

## Review 2026-10-23T01:53:47.262Z

- config_hash: `481cc6dc3ce2` -> `481cc6dc3ce2` (unchanged)
- window: 2026-10-23T00:53:47.262Z .. 2026-10-23T01:53:47.262Z
- llm: openrouter/openai/gpt-6-luna-pro, cache key `97ccb567210ba05a`

- 11 plans were produced; there were no orders, open positions, or recorded strategy PnL in the hour.
- S1 remained disabled: plans repeatedly reported cross-book edges negative after fees. No trade outcomes support changing its parameters.
- S2 was the only enabled strategy; its USDT target moved from 50% to 55% and back to 50%. Actual USDT share ended at 49.9037%.
- No risk vetoes or API errors were recorded; open orders were 0.
- Fees paid were 6,239.79 TMN, or 6.2398% of the 100,000 TMN fee budget.
- Health recorded one failed analyst cycle and recovery after 1 failed cycle; the health summary also reports 5 LLM-down and 5 stale events.
- Reported daily PnL is +11,680.09 TMN (+0.2343%), while equity is 4,997,493.53 TMN versus 5,000,000 initial (-0.0501% return); reconcile these figures.
- No parameter change is supported by trading outcomes this hour.

**Alerts**
- Reconcile daily PnL (+11,680.09 TMN) with equity return (-0.0501%).
- Review the analyst health incidents: 5 LLM-down and 5 stale events, despite recovery after one failed cycle.

## Review 2026-10-23T02:53:47.618Z

- config_hash: `481cc6dc3ce2` -> `481cc6dc3ce2` (unchanged)
- window: 2026-10-23T01:53:47.618Z .. 2026-10-23T02:53:47.618Z

reviewer skipped: gemini: ProviderError: gemini transport error: ConnectError
