# Decision log format

**Summary**
1. Every trading decision (a placed order or a risk veto) is one JSON line in `logs/decisions-YYYYMMDD.jsonl`; `logs/decisions.jsonl` is a symlink to the current UTC day's file.
2. Results come later as separate `kind: "outcome"` lines that point back by `decision_id`. Earlier lines are never rewritten; `merge_decisions` joins them.
3. Each record holds the intent and its reason, the full risk checklist, the order request and the exchange's answer, plus `mode`, `git_commit` and `config_hash`.
4. `orders.jsonl` (order traffic), `events.jsonl` (engine events) and `health.jsonl` (heartbeats) complete the trail. `python -m wta logs check` validates all four.
5. The examples come from a synthetic-feed run (offline exercise). `api.wallex.ir` is unreachable from the dev machine, so no live Wallex data has been recorded yet.

This file describes the code at commit `b943545` (`src/wta/models.py`, `src/wta/logging_/`,
`src/wta/execution/order_manager.py`, `src/wta/engine.py`). Where the code differs from the PRDs,
this file follows the code and §9 lists the differences.

## 1. Files and how they are written

| Family | Model (`src/wta/models.py`) | Writer | One line per |
|---|---|---|---|
| `decisions` | `DecisionRecord`, `OutcomeUpdate` | `DecisionLog` (`logging_/decision_log.py`) | decision, or outcome update |
| `orders` | `OrderLogRecord` | `OrdersLog` (same file) | placement, answer, cancel, status change, fill |
| `events` | `EventRecord` | `EventsLog` (same file) | engine event (start, kill, flatten, ...) |
| `health` | `HealthRecord` | `HealthLog` (`logging_/health_log.py`) | heartbeat (every `engine.heartbeat_interval_s` = 60 s, and one at shutdown) |

Each family is a set of files `logs/<family>-YYYYMMDD.jsonl` plus the relative symlink
`logs/<family>.jsonl -> <family>-YYYYMMDD.jsonl`. All four share one writer, `JsonlWriter`
(`logging_/jsonl.py`):

| Property | Implementation |
|---|---|
| Append-only | Files are opened in `"a"` mode, line-buffered and flushed after every line (`fsync` available, off by default). Nothing is ever truncated or rewritten; a correction or result is a new line. |
| Daily rotation | The file is chosen at each write from the UTC day of the engine's clock (`utc_day(clock.now())`), so a backtest or replay names files by simulated date. |
| Current-day link | When a new day's file is opened, `<family>.jsonl` is re-pointed atomically (temporary link + `os.replace`). A real file with that name (e.g. a copied deliverable) is never overwritten. If the OS refuses symlinks, logging continues without the link. |
| Encoding | Every line is built through its pydantic model (`extra="forbid"`), then written as canonical JSON: sorted keys, no whitespace, UTF-8, one object per line. The same record always gives the same bytes (replay compares lines). |
| Secrets | Each configured secret (Wallex, OpenRouter, Gemini keys, Telegram token; ≥ 4 chars), raw and JSON-escaped, is replaced by `***` before the line reaches the disk. |
| Time | `ts` is ISO-8601 UTC with milliseconds (`2026-10-22T16:26:36.573Z`). During a tick the clock is frozen at the tick's start, so all lines of one tick share one `ts`. |
| Reading | `family_files(log_dir, family)`: a plain `<family>.jsonl` first if it is a regular file, then the dated files in date order. The symlink is skipped, so nothing is read twice. |

Value conventions: money and quantities are **strings** (Python `Decimal`), sometimes in exponent
form (a fee of `"3.0E-8"` BTC); parse them as decimals, never as floats. `*_pct` fields and
`usdt_share` are **fractions** (`0.008` = 0.8 %); only risk-check `detail` texts print percentages.
Only the top-level `ts` of a line is an ISO string; `intent.ts`, `*_ts` fields inside objects and
fill `ts` are Unix seconds (floats).

## 2. `decisions.jsonl`

### 2.1 Record kinds

| `kind` | Written at this commit by | Meaning |
|---|---|---|
| `order` | `OrderManager._record` | Risk approved the intent. `order.accepted` says whether the exchange took it. When the manager refused before sending (unknown market, no book, below minimum, self-cross guard), `order` is `null` and `notes` starts with `REFUSED:` or `SKIPPED:`. Auto-placed server stops are also `order` records. |
| `veto` | `OrderManager.submit` | The RiskManager said no. Nothing was sent. |
| `outcome` | `DecisionLog.outcome` | An `OutcomeUpdate` (§2.4), not a `DecisionRecord`. |
| `plan`, `no_trade`, `intent`, `event` | nothing yet | Defined in `DecisionKind`. PRD-04 puts the Analyst's `MarketPlan` here as `kind: "plan"`; `wta status` and the Reviewer already read it, but no code writes it at this commit. |

### 2.2 `DecisionRecord` fields

| Field | Type | Content |
|---|---|---|
| `kind` | string | See §2.1. |
| `decision_id` | string | Equals `intent.id`. Links the record to its outcome and to `orders.jsonl` (§2.5). |
| `ts` | ISO string | Tick time when the line was written. |
| `mode` | string | `shadow` or `live` (the modes `wta run` accepts). |
| `git_commit` | 40 hex chars | `git rev-parse HEAD` at process start, or `unknown`. See §8. |
| `config_hash` | 12 hex chars | `sha256(canonical_json(resolved config))[:12]`. See §8. |
| `strategy`, `symbol` | string | `S1`/`S2`/`S3`/`FLATTEN`, or the name given in an injected intent; e.g. `BTCUSDT`. |
| `features` | object | Inputs the strategy saw: a copy of `intent.features`. The engine adds `veto_repeats_suppressed` (a count) to the first veto logged after identical vetoes were suppressed. |
| `plan_snapshot` | object or null | The `MarketPlan` in force. Always `null` at this commit (no planner is plugged into the engine). |
| `intent`, `risk` | object | The full `Intent` and the `RiskDecision` (§2.3). |
| `order` | object or null | Request and exchange answer (§2.3). `null` for vetoes and for refusals before a request was built. |
| `outcome` | null | Always `null` in a decision line; results arrive as outcome lines. |
| `notes` | string | Short human note: veto reason, `REFUSED`/`SKIPPED`/`REJECTED` reason, server-stop placement, `server stop ... canceled first`. Empty when there is nothing to add. |

### 2.3 Sub-objects: `intent`, `risk`, `order`

`intent` (model `Intent`): `id`, `ts`, `strategy`, `symbol`, `side` (`BUY`/`SELL`), `order_type`
(`LIMIT`/`MARKET`/`STOP_LIMIT`/`STOP_MARKET`), `qty` or `notional` (in the quote currency),
`price` (`null` = the OrderManager prices it from the book), `stop_price`, `purpose` (`entry`,
`exit`, `stop`, `rebalance`, `flatten`, `leg2`, `probe`), `reason` (free text, copied verbatim),
`features`, `expected_edge_pct`, `linked_position_id` (set on exits and stops) and `taker` (true
= cross the spread).

`risk` (model `RiskDecision`):

| Key | Content |
|---|---|
| `approved` | True only when no check failed. |
| `checks` | Every check, passed or not, in a fixed order: `{code, passed, detail}`. An evaluated intent has 23: `KILLED, PAUSED, DEADLINE, BANNED_MARKET, MARKET_ORDER_DISABLED, MIN_NOTIONAL, MAX_ORDER_PCT, BEST_LEVEL_QTY, COIN_EXPOSURE, USDT_BOUNDS, MAX_POSITIONS, MAX_OPEN_ORDERS, RATE_HOUR, RATE_DAY, DAILY_LOSS, DRAWDOWN_KILL, FEE_BUDGET, EDGE_VS_COST, STALE_DATA, PRICE_SANITY, SELF_CROSS, BALANCE, DUPLICATE`. A second `MIN_NOTIONAL` ("after adjustments") is appended when sizing made the order too small. Rules that do not apply say `n/a (...)` and pass. |
| `adjusted_qty` | Quantity after risk sizing, rounded down to the market step. Present on vetoes too; `null` when the intent could not be sized (no reference price) and on the fixed-check records below. |
| `veto_reason` | `"<first failed code>: <its detail>"`, or `null`. |

Two kinds of order skip the RiskManager and carry one fixed check: `PROTECTIVE_STOP` (the server
stop after a filled, risk-approved entry; it only reduces exposure) and `ADOPTED` (our order found
on the exchange without a stored request). The rules are in PRD-02 and `src/wta/risk/manager.py`.

`order` (built by `OrderManager._record`):

| Key | Content |
|---|---|
| `request` | The `OrderRequest` sent: `client_id`, `decision_id`, `symbol`, `side`, `type`, `qty` (rounded down to the step), `price`, `stop_price`, `strategy`, `purpose`. |
| `accepted` | `true` if the exchange accepted it, else `false` with `notes: "REJECTED: <code> <message>"`. |
| `response` | Only when accepted: the exchange's answer to the placement (`exchange_id`, `status`, `filled_qty`, `avg_price`, `fee_paid`, `fee_asset`, `created_ts`, `updated_ts`, `reprices`). |

Re-prices and crosses do not write new decision lines. Each new placement gets a new `client_id`
and shows up in `orders.jsonl` and in the outcome's `client_ids`.

### 2.4 Outcome updates (`OutcomeUpdate`)

An update is `{"kind": "outcome", "decision_id": "<earlier decision>", "ts": "<ISO>", "outcome": {...}}`.

**Position outcome:** on the *entry* decision id, written when the position closes (by a fill or
by the Reconciler). Keys: `pnl_tmn`, `pnl_pct`, `hold_s`, `closed_ts`, `exit_reason` (`target`,
`stop`, `time_stop`, `lockin`, `flatten`, `kill`, ...), `position_id`, `strategy`, `symbol`.
Optional: `dust_qty` (unsellable remainder left on the account), and `pnl_complete: false` with a
`note` when a leg could not be valued in TMN. Such a PnL is not added to the realized total.

```
value_in_tmn  = Σ entry fills   qty × price, converted to TMN at the USDTTMN bid at fill time
value_out_tmn = Σ closing fills qty × price (same conversion) + dust_qty × closing price
fees_tmn      = Σ fees in TMN − fees already taken out of the coin quantity
pnl_tmn       = value_out_tmn − value_in_tmn − fees_tmn
pnl_pct       = pnl_tmn / value_in_tmn                      (fraction)
hold_s        = closed_ts − time of the first entry fill
```

**Order outcome** (`outcome.kind = "order"`): on the decision id of every other logical order
when it ends: exits, stops, `leg2`, flatten, rebalances, and entries that never filled. A filled
entry gets no order outcome; its position outcome reports instead. Keys: `status` (`FILLED`,
`PARTIALLY_FILLED`, `UNFILLED`), `done_reason`, `filled_qty`, `qty`, `avg_price`
(volume-weighted over all placements), `fee_paid`, `fee_asset`, `symbol`, `side`, `purpose`,
`reprices`, `crossed`, `client_ids` (every placement), `done_ts`. Optional: `position_id`, `detail`.
`done_reason` is one of `FILLED`; `EXPIRED_UNFILLED` (entry or rebalance given up: hard cap
`limit_cancel_after_s`, or out of re-prices); `CANCELED_FOR_EXIT` (a server stop canceled so a
closing SELL could take its coins); `POSITION_CLOSED`; `DUST_REMAINDER` (remainder below the
minimum); `NOT_FOUND` (unknown to the exchange after 3 polls); `REPLACE_REJECTED`;
`SELF_CROSS_GUARD`; `ADOPTED_UNKNOWN`; `EXCHANGE_<status>` (the exchange ended it).

### 2.5 Identifiers and how records link

| Id | Format | Example from the run in §3 |
|---|---|---|
| Strategy / exit / flatten decision | `d_<tick ms>_<strategy>_<symbol>_<seq>` | `d_1792686460672_S3_BTCUSDT_1` |
| Injected decision (tests) | `d_<ms>_<strategy>_<symbol>_inj<line>` | `d_1792686396573_S3_BTCUSDT_inj3` |
| Protective server stop | `<entry decision id>_stop<n>` | `d_1792686396573_S3_BTCUSDT_inj3_stop1` |
| `client_id` | `<S1/S2/S3/FL/EX/PR>_<SYMBOL>_<13-digit ms>_<4 hex>` | `S3_BTCUSDT_1792686396573_0DCF` |
| Position | `p_<strategy>_<symbol>_<ms>_<seq>` | `p_S3_BTCUSDT_1792686400583_1` |

The 4-hex nonce is `sha1(decision_id | attempt)[:4]`, so a replay produces the same client ids
(Wallex allows only letters, digits and `_`). Links: `decision_id` = `order.request.decision_id` =
`data.request.decision_id` (placements) and `data.decision_id` (fills) in `orders.jsonl`. The
outcome's `position_id` = `intent.linked_position_id` of the position's exits and stops.

### 2.6 Joining decisions and outcomes: `merge_decisions`

```python
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
merged = merge_decisions(iter_family("logs", "decisions"))   # decision_id -> merged record
merged["d_1792686396573_S3_BTCUSDT_inj3"]["outcome"]["pnl_tmn"]; merged.orphans
```

Several decision records with the same id: later non-empty fields overlay earlier ones. An outcome
replaces `outcome` as a whole; the last one wins. Outcomes whose id never appeared are kept, unchanged,
in `merged.orphans`. Input dicts are not modified.

## 3. Annotated examples: from a synthetic-feed run (offline exercise)

**Read this first.** These lines come from the synthetic feed (deterministic offline books seeded
from `configs/markets_snapshot.yaml`) and the paper exchange. They show the log format, not
strategy performance; no Wallex market data was involved. The strategies are not plugged into
`wta run` at this commit (§9), so the orders come from **injected intents**: hand-written test
inputs that go through the normal risk → order path, marked `features.injected: true` and with a
`reason` that starts with `[injected for acceptance testing]`. The protective stop, the time-stop
exit and every outcome were produced by the engine itself. Run: commit `b943545`, clean tree
(`git_dirty: false`), 2026-10-22 16:26–16:28 UTC; `$T` is a scratch directory outside the repo.

```
$ cat $T/inject.jsonl        # line 1 is a comment
{"at_s": 5, "strategy": "S2", "symbol": "USDTTMN", "side": "BUY", "order_type": "LIMIT", "qty": "2", "purpose": "rebalance", "taker": true, "reason": "S2 example: raise the USDT share toward the target", "features": {"usdt_share": "0.39", "target_share": "0.50"}}
{"at_s": 20, "strategy": "S3", "symbol": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.00003", "stop_price": "81600", "purpose": "entry", "taker": true, "expected_edge_pct": "0.008", "reason": "S3 example: breakout entry with a server-side stop and a 60 s time stop", "features": {"time_stop_s": 60}}
{"at_s": 30, "strategy": "S3", "symbol": "ETHUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.001", "purpose": "entry", "taker": true, "expected_edge_pct": "0.001", "reason": "S3 example: second S3 entry while one S3 position is open (expected veto)"}
$ python -m wta run --mode shadow --feed synthetic --seconds 150 --inject-intents $T/inject.jsonl \
    --paper-balances '{"TMN": 3000000, "USDT": 8}' --log-dir $T/logs --set data.sqlite_path=$T/run.sqlite
exit 0
$ python -m wta logs check --secrets --dir $T/logs
decisions-20261022.jsonl: 9 lines, ok
health-20261022.jsonl: 3 lines, ok
events-20261022.jsonl: 2 lines, ok
orders-20261022.jsonl: 19 lines, ok
OK
secrets scan: clean
```

| # | kind | decision_id | What happened |
|---|---|---|---|
| 1 | order | `d_1792686382553_S2_USDTTMN_inj2` | USDTTMN rebalance BUY placed (Example 2) |
| 2 | order | `d_1792686396573_S3_BTCUSDT_inj3` | BTCUSDT entry with `stop_price` placed (Example 1) |
| 3 | order | `..._inj3_stop1` | entry filled → STOP_LIMIT SELL placed at once |
| 4 | outcome | `..._S2_USDTTMN_inj2` | rebalance FILLED after one cross |
| 5 | veto | `d_1792686406592_S3_ETHUSDT_inj4` | ETHUSDT entry vetoed (Example 3) |
| 6 | outcome | `..._inj3_stop1` | stop canceled to make room for the exit |
| 7 | order | `d_1792686460672_S3_BTCUSDT_1` | time-stop exit SELL (engine-generated) |
| 8 | outcome | `..._S3_BTCUSDT_inj3` | position closed: PnL on the entry's id |
| 9 | outcome | `d_1792686460672_S3_BTCUSDT_1` | exit FILLED |

The JSON below is taken from that log. For reading, keys are grouped (the file sorts them
alphabetically), small objects are put on one line, `//` comments are added and `/* ... */` marks
removed parts. Values are unchanged.

### Example 1: entry with `stop_price`, and its outcome (lines 2 and 8)

```jsonc
{
  "kind": "order",                                   // approved and sent
  "decision_id": "d_1792686396573_S3_BTCUSDT_inj3",  // the outcome on line 8 uses this id
  "ts": "2026-10-22T16:26:36.573Z", "mode": "shadow", // shadow = paper exchange
  "git_commit": "b943545cdad0bcd2be5caf048f059ae6b2c89df4", "config_hash": "01d6ebc6184d",
  "strategy": "S3", "symbol": "BTCUSDT", "features": {"inject_line": 3, "injected": true, "time_stop_s": 60},
  "intent": {
    "id": "d_1792686396573_S3_BTCUSDT_inj3", "ts": 1792686396.5730891, "linked_position_id": null,
    "side": "BUY", "order_type": "LIMIT", "purpose": "entry", "qty": "0.00003", "notional": null,
    "price": null,                                   // no limit given: priced from the book
    "stop_price": "81600",                           // becomes the server STOP_LIMIT after the fill
    "taker": true,                                   // cross the spread (buy at the best ask)
    "expected_edge_pct": "0.008",                    // 0.8 %, tested by EDGE_VS_COST
    "reason": "[injected for acceptance testing] S3 example: breakout entry with a server-side stop and a 60 s time stop",
    /* strategy, symbol, features: same as above */ },
  "risk": {
    "approved": true, "veto_reason": null,
    "adjusted_qty": "0.000030",                      // rounded to the 6-decimal BTC step
    "checks": [
      {"code": "KILLED", "passed": true, "detail": "kill-switch off"},
      /* 7 passed checks */
      {"code": "COIN_EXPOSURE", "passed": true, "detail": "post-trade coins 663,414 TMN (held 0.0000 TMN + open BUYs 0.0000 TMN + this 663,414 TMN) = 12.92% <= 40% of equity"},
      /* 8 passed checks */
      {"code": "EDGE_VS_COST", "passed": true, "detail": "edge 0.800% vs cost 0.337% (round trip: 0.130% taker BTCUSDT + 0.130% taker exit + spread 0.077%) + margin 0.000% = 0.337%"},
      /* 3 passed checks */
      {"code": "BALANCE", "passed": true, "detail": "needs 2.4889 USDT incl. 0.13% fee <= free 8.0000 USDT less 0.5% buffer = 7.9600 USDT"},
      /* 1 passed check (DUPLICATE): 23 in total */ ] },
  "order": {
    "accepted": true,
    "request": {"client_id": "S3_BTCUSDT_1792686396573_0DCF", "type": "LIMIT", "side": "BUY",
                "qty": "0.000030", "price": "82857.03",  // the best ask at that tick
                "stop_price": null,                      // the stop is a separate order (line 3)
                "purpose": "entry", "strategy": "S3", "symbol": "BTCUSDT", "decision_id": "d_1792686396573_S3_BTCUSDT_inj3"},
    "response": {"exchange_id": "paper-o-2", "status": "NEW", "filled_qty": "0", "avg_price": null, "fee_paid": "0",
                 "fee_asset": null, "reprices": 0, "created_ts": 1792686396.5730891, "updated_ts": 1792686396.5730891} },
  "plan_snapshot": null, "outcome": null, "notes": ""  // no planner at this commit; outcome: see line 8
}
```

* Two ticks later the entry filled (`orders.jsonl`: `fill` of 0.000030 BTC at 82857.03, fee
  `3.0E-8` BTC, so the position holds 0.000029970 BTC).
* Line 3, `..._inj3_stop1`: the protective `STOP_LIMIT` SELL of 0.000029 BTC (rounded down),
  `stop_price` 81600.00, limit `price` 81273.60 (= stop × (1 − 0.004)), `linked_position_id`
  `p_S3_BTCUSDT_1792686400583_1`; `risk.checks` is the single `PROTECTIVE_STOP` check.
* Line 7: after `time_stop_s` = 60 s the PositionManager emitted a taker exit (`purpose: "exit"`,
  `features.exit_reason: "time_stop"`, `notes: "server stop S3_BTCUSDT_1792686400583_AD96
  canceled first"`). Line 6 is the stop's order outcome: `UNFILLED`, `CANCELED_FOR_EXIT`.
* Line 8: when the exit filled, the position outcome was appended on the **entry's** id:

```jsonc
{
  "kind": "outcome", "ts": "2026-10-22T16:27:42.674Z",
  "decision_id": "d_1792686396573_S3_BTCUSDT_inj3",  // points back to line 2
  "outcome": {
    "position_id": "p_S3_BTCUSDT_1792686400583_1", "strategy": "S3", "symbol": "BTCUSDT",
    "exit_reason": "time_stop",                      // from the exit intent's features
    "hold_s": 62.091, "closed_ts": 1792686462.6739998,  // hold_s = closed_ts - first entry fill
    "pnl_tmn": "-1966.861495520897",                 // value out - value in - fees, all in TMN
    "pnl_pct": "-0.002964669019547503625948077050",  // -0.30 % of value in
    "dust_qty": "0.000000970"                        // below the minimum order; valued at the exit price
  }
}
```

The loss is the taker fees on both sides plus the spread, on synthetic books. It says nothing
about S3 itself.

### Example 2: USDTTMN rebalance and its outcome (lines 1 and 4)

```jsonc
{
  "kind": "order", "ts": "2026-10-22T16:26:22.553Z",
  "decision_id": "d_1792686382553_S2_USDTTMN_inj2", "strategy": "S2", "symbol": "USDTTMN",
  "features": {"inject_line": 2, "injected": true, "target_share": "0.50", "usdt_share": "0.39"},
  "intent": {"side": "BUY", "order_type": "LIMIT", "purpose": "rebalance", "qty": "2", "taker": true,
             "expected_edge_pct": null,             // rebalances need no edge
             /* id, ts, reason, features, ... */},
  "risk": {
    "approved": true, "adjusted_qty": "2.00", "veto_reason": null,
    "checks": [
      /* 5 passed checks */
      {"code": "MIN_NOTIONAL", "passed": true, "detail": "qty 2.00 x 266827 = 533,654 TMN >= min 50,000 TMN"},
      /* 3 passed checks */
      {"code": "USDT_BOUNDS", "passed": true, "detail": "USDT share 41.57% -> 51.96% within [25%, 70%]"},
      {"code": "MAX_POSITIONS", "passed": true, "detail": "n/a (rebalance)"},  // not a position
      /* 5 passed checks */
      {"code": "FEE_BUDGET", "passed": true, "detail": "fees paid 0.0000 TMN + expected 1,601 TMN (0.300% taker) = 1,601 TMN <= budget 100,000 TMN (2.0% of initial)"},
      {"code": "EDGE_VS_COST", "passed": true, "detail": "n/a (rebalance: no edge required)"},
      /* 5 passed checks: 23 in total */ ] },
  "order": {"accepted": true,
            "request": {"client_id": "S2_USDTTMN_1792686382553_3336", "type": "LIMIT", "side": "BUY",
                        "qty": "2.00", "price": "266827", /* ... */},
            "response": {"exchange_id": "paper-o-1", "status": "NEW", "filled_qty": "0", /* ... */}},
  "notes": "", /* mode, git_commit, config_hash as in Example 1; plan_snapshot and outcome null */
}
```

The first placement did not fill: the synthetic book moved away by the next tick. After
`limit_reprice_after_s` = 15 s the OrderManager canceled it and crossed again at the new best ask
with a new client id. Only `orders.jsonl` shows this (`cancel` and `place`, both `why: "cross"`).
The outcome (line 4):

```jsonc
{
  "kind": "outcome", "ts": "2026-10-22T16:26:42.585Z",
  "decision_id": "d_1792686382553_S2_USDTTMN_inj2",
  "outcome": {
    "kind": "order",                                 // order outcome, not a position outcome
    "status": "FILLED", "done_reason": "FILLED",
    "symbol": "USDTTMN", "side": "BUY", "purpose": "rebalance", "qty": "2.00", "filled_qty": "2.00",
    "avg_price": "266918",                           // the second placement's price
    "fee_paid": "0.005000", "fee_asset": "USDT",     // 0.25 % of 2 USDT, taken from the bought USDT
    "reprices": 1, "crossed": true, "done_ts": 1792686402.585671,
    "client_ids": ["S2_USDTTMN_1792686382553_3336", "S2_USDTTMN_1792686398578_BFF8"]  // both placements
  }
}
```

### Example 3: veto with failed checks (line 5)

```jsonc
{
  "kind": "veto", "ts": "2026-10-22T16:26:46.592Z",  // a veto: nothing was sent
  "decision_id": "d_1792686406592_S3_ETHUSDT_inj4", "strategy": "S3", "symbol": "ETHUSDT",
  "intent": {"side": "BUY", "order_type": "LIMIT", "purpose": "entry", "qty": "0.001",
             "expected_edge_pct": "0.001", "taker": true, /* ... */},
  "risk": {
    "approved": false, "adjusted_qty": "0.00100",    // adjusted_qty is computed on a veto too
    "veto_reason": "MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1",
    "checks": [
      /* 8 passed checks */
      {"code": "COIN_EXPOSURE", "passed": true, "detail": "post-trade coins 1,312,648 TMN (held 661,854 TMN + open BUYs 0.0000 TMN + this 650,795 TMN) = 25.58% <= 40% of equity"},
      {"code": "USDT_BOUNDS", "passed": true, "detail": "USDT share 39.05% -> 26.37% within [25%, 70%]"},
      {"code": "MAX_POSITIONS", "passed": false,     // first failure -> veto_reason
       "detail": "S3 already has 1 positions (incl. pending) >= its cap 1"},
      /* 6 passed checks */
      {"code": "EDGE_VS_COST", "passed": false,      // second failure, also recorded
       "detail": "edge 0.100% vs cost 0.310% (round trip: 0.130% taker ETHUSDT + 0.130% taker exit + spread 0.050%) + margin 0.000% = 0.310%: edge too small"},
      /* 5 passed checks: 23 in total */ ] },
  "order": null,                                     // no request was built
  "notes": "MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1",
  /* mode, git_commit, config_hash, features as in Example 1; plan_snapshot and outcome null */
}
```

A veto gets no outcome line. An identical veto of the same intent within 60 s (`VETO_REPEAT_S`)
is not logged again; the next logged one carries `features.veto_repeats_suppressed`.

## 4. `orders.jsonl`

`OrderLogRecord`: `ts`, `action`, `client_id`, `symbol`, `data`. Written by the OrderManager.

| `action` | `data` |
|---|---|
| `place`, `response` | `source`, `why` (`submit`, `reprice`, `cross`, `server stop`), `request` (the `OrderRequest`); the answer: `source`, `status`, `exchange_id` |
| `error` | `code`, `status`, `message`; `ambiguous: true` when the order may exist (tracked as `UNKNOWN`, resolved by `get_order`, never re-sent) |
| `cancel` | `source`, `why` (`cross`, `reprice`, or a done reason) |
| `status` | End of a logical order (`done`, `done_reason`, `filled_qty`, `reprices`); not found after polls; a reconcile finding (`source: "reconcile"`); an adopted order; a lagging exchange snapshot ignored (`baseline_kept`) |
| `fill` | The `Fill` (`order_client_id`, `price`, `qty`, `fee`, `fee_asset`, `ts`, `trade_id`, ...) plus `decision_id` and `purpose` |

`cancel_response` and `timeout` exist in the schema but are not written at this commit. The
example run wrote 19 lines: 5 `place`, 5 `response`, 2 `cancel`, 3 `fill`, 4 `status`.

## 5. `events.jsonl`

`EventRecord`: `ts`, `kind`, `reason` (a human sentence), `data` (object). Kinds the engine writes:

| `kind` | When; notable `data` |
|---|---|
| `start` | Process start: `mode`, `feed`, `reason` (`fresh`/`restart`), `git_commit`, `git_dirty`, `git_tag`, `config_hash`, `markets_source`, `exchange`, `watchlist`, `pid`, deadline times; `synthetic: true` and a `note` on the synthetic feed; `injected_intents` |
| `restart`, `stop` | State restored from the store (restore and startup-reconcile summaries); shutdown (`uptime_s`, `ticks`, `heartbeats`) |
| `pause`, `resume` | Daily-loss or anomaly pause; `reason` names the rule |
| `kill`, `flatten`, `reset_kill`, `reset_kill_applied` | Kill-switch; flatten phases (`trigger`, `kind`, final balances when complete); manual reset (`wta reset-kill --reason ...`) and when the engine applied it |
| `day_start`, `final_state`, `tick_error`, `reconcile` | New UTC day; state at T−5 min; a crashing tick; a reconcile that found something |

`config_change` (`old_hash`, `new_hash`, `diff`, `overlay_path`) comes from the Reviewer code,
which is not wired into the engine at this commit. The example run wrote only `start` (with
`"feed": "synthetic"`, `"git_dirty": false`, `"git_tag": null`) and `stop`.

## 6. `health.jsonl`

`HealthRecord`, built by `Engine.health_record`:

| Field | Content |
|---|---|
| `ts`, `mode`, `git_commit`, `config_hash` | As in decision records |
| `equity_tmn`, `drawdown_pct` | Equity in TMN; drawdown from the peak as a fraction (`null` when not trading) |
| `open_orders`, `positions`, `paused`, `killed` | Counts and engine flags |
| `ws_ok`, `rest_ok` | Fresh data from that source within `engine.stale_data_s` (30 s). Both `false` on the synthetic feed, which uses neither. |
| `api_errors_1m` | Failed API attempts in the last 60 s |
| `llm_ok` | Always `null` at this commit (no LLM in the engine loop) |
| `reasons` | Why the engine is not healthy, e.g. `rest: no fresh data`, `stale books (> 30s): ...`, `killed: ...`, `paused: ...`, `flatten phase 2`, `valuation incomplete: ...`. Empty = healthy. |
| `extra` | Object, keys below |

`extra` always has `feed`, `uptime_s`, `ticks`, `book_age_s` (per symbol), `rest` (client
health), `rest_poller` (`cycles`, `errors`, `consecutive_failures`, or `null`), `ws` (stats or
`null`), `task_crashes`, `tick_crashes`, `git_dirty`, and a `note` on the synthetic feed. When
trading it adds `markets_source`, `flatten_phase`, `daily_pnl_pct`, `fees_paid_tmn`,
`fee_budget_used_pct`, `usdt_share`, `coin_exposure_pct`, `orders_today`, `orders_last_hour`,
`peak_equity_tmn`, `realized_pnl_tmn`, `positions` (ids), `anomalies`, `last_reconcile`, and
`paper_tick` in shadow mode. The example run's last heartbeat: `equity_tmn` 5,130,131.83,
`realized_pnl_tmn` -1966.86, `fees_paid_tmn` 2830.15, `reasons: []`.

## 7. `python -m wta logs check`

```
python -m wta logs check [--dir logs] [--secrets]      # exit 0 = OK, 1 = a check failed
```

What `src/wta/logging_/checker.py` checks, for the files `family_files` finds (a missing or empty
directory is OK):

1. Every non-blank line is valid JSON, an object, and passes its model: `decisions` lines with
   `kind: "outcome"` → `OutcomeUpdate`, other `decisions` lines → `DecisionRecord`, then
   `HealthRecord`, `EventRecord`, `OrderLogRecord`. Unknown fields, wrong types and invalid
   `kind`/`action` values fail. `ts` must parse as ISO-8601.
2. After all files are read, every outcome's `decision_id` exists as a decision record in some
   `decisions` file (possibly the previous day's). Otherwise:
   `line N: outcome references unknown decision_id '...'`.
3. Errors name the file, line and field path, never the values, so a bad line cannot leak a key
   into the report. At most 50 errors per file are listed.

`--secrets` also scans the log directory, `deliverables/` and `docs/REPORTS/` for each configured
secret (raw and JSON-escaped) and for key patterns (OpenRouter `sk-or-v1-`, `sk-`, Google `AIza`,
Telegram bot tokens, `X-API-Key` values). zip/gzip/bzip2/xz files are decompressed first; a file it
cannot search (unreadable, 7z/zstd) is reported as a finding, so the scan fails closed.

It does **not** check PnL arithmetic, timestamp order, duplicate decision ids, or that
`git_commit`/`config_hash` match a tag. §8 covers the last point by hand.

## 8. Verifying `git_commit` and `config_hash` against tags

`git_commit` is the full SHA of `HEAD` at process start. Live mode refuses a dirty tree (untracked
files count) unless started with `--allow-dirty`; the `start` event records `git_dirty` and
`git_tag`. Live runs start from tags `live-YYYYMMDD-HHMM` (`docs/GO_LIVE_CHECKLIST.md` §5).

```bash
# 1. distinct (git_commit, config_hash, mode) in the delivered decision files
cat deliverables/decisions-*.jsonl | python -c 'import sys, json
rows = {(r["git_commit"], r["config_hash"], r["mode"]) for r in map(json.loads, sys.stdin) if r["kind"] != "outcome"}
print(*sorted(rows), sep="\n")'
# 2. each live commit must be the commit of a live tag
git tag --points-at <git_commit>              # prints live-YYYYMMDD-HHMM
git rev-parse 'live-YYYYMMDD-HHMM^{commit}'   # must print exactly <git_commit>
# 3. the run's start event: clean tree, same tag
grep '"kind":"start"' deliverables/events-*.jsonl   # data.git_dirty false, data.git_tag = the tag
# 4. the config hash at that tag
git checkout live-YYYYMMDD-HHMM
python -m wta config hash [the run's --set flags]   # must print the logged config_hash
```

The hash is `sha256` over the canonical JSON of the resolved config: `configs/default.yaml`,
`markets.yaml` and `llm.yaml`, then the `--set` overrides, then the `${VAR}` fields. To get the
same hash, use **the same `--set` flags** (every override counts, including paths such as
`data.sqlite_path`) and **the same values for the `${VAR}` fields** `CHALLENGE_START_UTC`,
`CHALLENGE_END_UTC`, `OPENROUTER_MODEL_FAST`, `OPENROUTER_MODEL_STRONG`, `GEMINI_MODEL` (read from
the environment, else from the repo's `.env`; empty becomes `null`). API keys are settings, not
config, so they are not in the hash. `python -m wta config show` prints the resolved config with
its hash. For the run in §3, `python -m wta config show --set data.sqlite_path=$T/run.sqlite` at
`b943545` printed `config_hash: 01d6ebc6184d`, the value in every line of that run (a shadow run
on an untagged commit, so its `git_tag` is `null`).

At this commit `config_hash` is fixed for the life of a process: nothing calls
`DecisionLog.set_config_hash`. Once the Reviewer is wired in, an accepted change writes a
`config_change` event with `old_hash`, `new_hash` and the overlay file. Later decisions carry
`new_hash`, and reproducing it needs that overlay.

## 9. Differences from the PRDs, and limitations

| Topic | PRD | Implementation at `b943545` |
|---|---|---|
| File name | `logs/decisions.jsonl` (rotation allowed) | Dated files plus a symlink (§1). Deliver the dated files. |
| `plan` / `no_trade` records | PRD-01 §8, PRD-04: every Analyst plan is logged | Not written; `plan_snapshot` is always `null`. AI reasoning is not yet in decision records. |
| Strategies in `wta run` | PRD-03 | No intent source is plugged in (the "S0 no-op" engine). The examples use injected intents, so there is no S1 example (PRD-06 asks for an S1 cycle). |
| `orders.jsonl` "raw" | PRD-01 §8 | Normalized fields only. The exchange payload (`Order.raw`) is kept in the SQLite `orders` table. |
| `client_id` | Overview §8: `-` separators | `_` separators (Wallex accepts only letters, digits, `_`; `docs/DECISIONS.md`). |
| Health | PRD-01 §8 field list | Same fields plus `extra`; `llm_ok` always `null`. |
| Modes | `backtest`/`replay` write the same schema (PRD-05) | Only `shadow`/`live` produce these logs at this commit. |
| Half-size overlay | `GO_LIVE_CHECKLIST.md` §5 runs `wta run ... --overlay configs/live_halfsize.yaml` | `wta run` has no `--overlay` option at this commit. The equivalent `--set` flags are in that file's header; they enter `config_hash` like any override. |

**Limitations.** No live data yet: `api.wallex.ir` is unreachable from the development machine,
and every run so far, including §3 and `docs/REPORTS/phase2_acceptance.md`, used the synthetic
feed and the paper exchange. Prices, fills and PnL in these logs are not market evidence. Paper
fills are simulated against synthetic books with fees from `configs/fees_snapshot.yaml`; order
behavior on Wallex, including STOP_LIMIT acceptance, is still unverified (`GO_LIVE_CHECKLIST.md`
§3). `logs check` validates structure and references, not the meaning of the numbers.
