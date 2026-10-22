# Phase 2 acceptance (PRD-02 §11): short runs, 2026-10-22

**Read this first.** `api.wallex.ir` is unreachable from the development machine (see
`docs/DECISIONS.md`), so every run below uses `--feed synthetic`: the deterministic offline feed
seeded with the reference mids and spreads in `configs/markets_snapshot.yaml`, with markets from
the same snapshot (`markets_source: snapshot`). These runs **exercise the engine end to end
(risk, orders, paper fills, kill-switch, flatten, restart). They are not evidence of strategy
performance.** Each `start` event and every heartbeat says so (`feed: synthetic` plus a note).

The runs are short versions, as the task asked: 180 s instead of the PRD's ≥ 1 h for (a). The
1-hour `make shadow` run on the real feed is still open.

**Re-run after the Phase-2 review fixes (same day).** All five runs were repeated with the same
commands after the reviewer's findings were fixed, in a fresh scratch directory (`$A` is now
`.../scratchpad/acc2`). Every run again exited 0 and `wta logs check --secrets` was OK and clean
on all five log directories. The numbers below are from the re-run. What changed in the results:

* (b) the BTCUSDT entry's PnL is now -1,967 TMN (-0.30 %) instead of -23,259 TMN (-3.5 %): the
  0.00000097 BTC left over after the exit (the BUY fee is taken in BTC) is now valued as dust at
  the closing price instead of being booked as lost. Its checklist also shows the new exposure
  rules: open orders count in `COIN_EXPOSURE` / `USDT_BOUNDS`, and `RATE_*` reserves re-prices.
* (c) the -8 % gap left the STOP_LIMIT stranded (bid 76,202 below its limit 81,273.60). The
  position is now sold by a stop exit in the same tick as the kill (the stranded stop is canceled
  first), instead of waiting for the kill-switch flatten. The veto of the entry-like intent while
  killed now reads "only flatten/exit/stop SELLs allowed, got BUY rebalance".
* (a), (d), (e): same results as before (numbers refreshed).

Setup for every run:

* code: commit `0e27389` plus the uncommitted Phase-2 work in this checkout (`git_dirty: true`
  in every start event), Python 3.12 (`.venv`), shadow mode (PaperExchange), no strategy
  (the "S0 no-op strategy": `Engine.intent_sources` is empty);
* a temporary log directory and SQLite file for each run, never the repo's `logs/` or `data/`.
  `$A` below is the scratch directory
  `/private/tmp/claude-501/-Users-hussein-Code-hackathon-wallex-trader-agent/c1bcf3cd-52c7-4b43-bf93-ad2b10bf3e92/scratchpad/acc` (first run) and `.../acc2` (re-run);
* `config_hash` differs between runs only because `--set data.sqlite_path=...` is part of the
  hashed config.

| PRD-02 §11 item | Run | Result |
|---|---|---|
| shadow with the no-op strategy: heartbeats, reconcile, equity rows, no exceptions | (a), 180 s | pass |
| injected intents give complete decision records (checks, order, outcome) | (b), 150 s | pass |
| drawdown ≥ 5 % → kill → flatten in paper; `events.jsonl` shows it; `reset-kill` works | (c), 200 s + 30 s | pass |
| `scripts/flatten_now.py` turns a mixed paper portfolio into 100 % TMN (minus dust) | (d), ~70 s | pass |
| a restart mid-run keeps peak equity, daily PnL base, fees and open positions | (e), 60 s + 40 s | pass |
| full suite: `ruff check src tests scripts backtest` and `pytest` | - | pass (973 tests) |

---

## (a) Shadow, synthetic feed, no-op strategy, 180 s

```
$ python -m wta run --mode shadow --feed synthetic --seconds 180 \
    --log-dir $A/logs_a --set data.sqlite_path=$A/a.sqlite --set engine.reconcile_interval_s=60
exit=0   (3:01 wall clock)
```

Events, heartbeats and store rows:

```
events:
  2026-10-22T11:43:25.232Z start: engine started in shadow mode
  2026-10-22T11:46:25.928Z stop: ran for 180 s
heartbeats: 4  (every 60 s + the final one)
  11:44:25 equity=5000000 dd=0 pos=0 orders=0 killed=False paused=False feed=synthetic reasons=[] tick_crashes=0 task_crashes={}
  11:45:25 equity=5000000 ...same...
  11:46:25 equity=5000000 ...same...
  11:46:25 equity=5000000 ...same... (final heartbeat at shutdown)
store rows: equity 18 (every 10 s), ticks 90, engine_books 990, events 2, kv 4
grep -ciE "error|traceback|exception" run_a.out -> 0
$ python -m wta logs check --dir $A/logs_a
health-20261022.jsonl: 4 lines, ok
events-20261022.jsonl: 2 lines, ok
OK
```

Start event data and the last heartbeat's `extra` (trimmed):

```json
{"mode": "shadow", "feed": "synthetic", "reason": "fresh", "git_commit": "0e273891271a29d1cfb0c2dcb22eca65de963c9f",
 "config_hash": "4939ccf02f36", "markets_source": "snapshot", "synthetic": true, "exchange": "paper",
 "note": "synthetic market data: an offline exercise of the engine, never evidence of performance"}
{"feed": "synthetic", "markets_source": "snapshot", "flatten_phase": 0, "daily_pnl_pct": "0.000000",
 "fees_paid_tmn": "0.00", "fee_budget_used_pct": "0.000000", "usdt_share": "0.000000",
 "coin_exposure_pct": "0.000000", "orders_today": 0, "last_reconcile": 1792669585.44}
```

The scheduled reconcile ran every 60 s (`last_reconcile`). Runs that find nothing are not
written to `events.jsonl`, by design.

## (b) Injected intents, 150 s

`$A/inject_b.jsonl` (lines 2-4; line 1 is a comment):

```
{"at_s": 5,  "strategy": "S2", "symbol": "USDTTMN", "side": "BUY", "order_type": "LIMIT", "qty": "2", "purpose": "rebalance", "taker": true, ...}
{"at_s": 20, "strategy": "S3", "symbol": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.00003", "stop_price": "81600", "purpose": "entry", "taker": true, "expected_edge_pct": "0.008", "features": {"time_stop_s": 60}, ...}
{"at_s": 30, "strategy": "S3", "symbol": "ETHUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.001", "purpose": "entry", "taker": true, "expected_edge_pct": "0.001", ...}
```

```
$ python -m wta run --mode shadow --feed synthetic --seconds 150 --inject-intents $A/inject_b.jsonl \
    --paper-balances '{"TMN": 3000000, "USDT": 8}' --log-dir $A/logs_b --set data.sqlite_path=$A/b.sqlite
exit=0
$ python -m wta logs check --dir $A/logs_b
decisions-20261022.jsonl: 9 lines, ok
health-20261022.jsonl: 3 lines, ok
events-20261022.jsonl: 2 lines, ok
orders-20261022.jsonl: 19 lines, ok
OK
```

Merged decisions (`merge_decisions`, one block per decision id; every outcome references a
recorded decision, `orphan outcomes: 0`):

```
d_1792673196278_S2_USDTTMN_inj2
    kind=order purpose=rebalance approved=True checks=23 failed=[]
    order: LIMIT BUY 2.00 @ 266827 accepted=True
    outcome: status=FILLED, done_reason=FILLED, filled_qty=2.00, avg_price=266918
d_1792673210301_S3_BTCUSDT_inj3
    kind=order purpose=entry approved=True checks=23 failed=[]
    order: LIMIT BUY 0.000030 @ 82857.05 accepted=True
    outcome: exit_reason=time_stop, pnl_tmn=-1967.02, pnl_pct=-0.0030, hold_s=62.123, dust_qty=0.00000097
d_1792673210301_S3_BTCUSDT_inj3_stop1
    kind=order purpose=stop approved=True checks=1 (PROTECTIVE_STOP)
    order: STOP_LIMIT SELL 0.000029 @ 81273.60 stop=81600.00 accepted=True
    outcome: status=UNFILLED, done_reason=CANCELED_FOR_EXIT
    notes: server stop placed for position p_S3_BTCUSDT_1792673214312_1: STOP_LIMIT SELL 0.000029 stop 81600.00 limit 81273.60
d_1792673220323_S3_ETHUSDT_inj4
    kind=veto purpose=entry approved=False checks=23 failed=['MAX_POSITIONS', 'EDGE_VS_COST']
    veto: MAX_POSITIONS: S3 already has 1 positions (incl. pending) >= its cap 1
d_1792673274428_S3_BTCUSDT_1
    kind=order purpose=exit approved=True checks=23 failed=[]
    order: LIMIT SELL 0.000029 @ 82789.46 accepted=True
    outcome: status=FILLED, done_reason=FILLED, filled_qty=0.000029, avg_price=82791.41
    notes: server stop S3_BTCUSDT_1792673214312_2C98 canceled first
```

The full checklist of the BTCUSDT entry, as written in its decision record:

| code | result | detail |
|---|---|---|
| KILLED | pass | kill-switch off |
| PAUSED | pass | engine not paused |
| DEADLINE | pass | before flatten_start |
| BANNED_MARKET | pass | BTCUSDT allowed |
| MARKET_ORDER_DISABLED | pass | n/a (LIMIT order) |
| MIN_NOTIONAL | pass | qty 0.000030 x 82857.05 = 2.4857 USDT >= min 1.0000 USDT |
| MAX_ORDER_PCT | pass | notional 663,414 TMN <= 25% of equity = 1,283,782 TMN |
| BEST_LEVEL_QTY | pass | qty 0.000030 <= 80% of best ask qty 0.000686 |
| COIN_EXPOSURE | pass | post-trade coins 663,414 TMN (held 0 TMN + open BUYs 0 TMN + this 663,414 TMN) = 12.92% <= 40% of equity |
| USDT_BOUNDS | pass | USDT share incl. our open orders (533,782 TMN) 51.97% -> 39.05% within [25%, 70%] |
| MAX_POSITIONS | pass | 0 positions (incl. pending) < max 3; S3 0 < 1 |
| MAX_OPEN_ORDERS | pass | 1 open orders < max 8 |
| RATE_HOUR | pass | 1 placements in the last 60 min + 2 re-placements reserved by our open entries + 3 for this one (1 + 2 re-prices) = 6 <= max 10 |
| RATE_DAY | pass | same count, <= max 60 |
| DAILY_LOSS | pass | daily PnL 0.03% > -2.50% |
| DRAWDOWN_KILL | pass | drawdown 0.00% < 5.00% |
| FEE_BUDGET | pass | fees paid 0 + expected 862 TMN (0.130% taker) <= budget 100,000 TMN (2.0% of initial) |
| EDGE_VS_COST | pass | edge 0.800% vs cost 0.337% (0.130% taker + 0.130% taker exit + spread 0.077%) + margin 0 |
| STALE_DATA | pass | books fresh (<= 30s): BTCUSDT 0.2s, USDTTMN 0.2s |
| PRICE_SANITY | pass | no limit price (priced from the book at placement) |
| SELF_CROSS | pass | no open SELL orders of ours in BTCUSDT |
| BALANCE | pass | needs 2.4889 USDT incl. 0.13% fee <= free 8.0000 USDT less 0.5% buffer = 7.9600 USDT |
| DUPLICATE | pass | no open order or approved within the cooldown for S3/BTCUSDT/BUY/entry |

What the trail shows: entry filled → STOP_LIMIT placed right away → time stop after 60 s → the
stop canceled first (never two SELLs for the same coins) → exit filled → PnL outcome on the
entry's decision id.

Two things worth knowing from this run:

* The USDTTMN taker order did not fill at once: the synthetic book had moved away by the next
  tick, so the paper exchange let it rest. The OrderManager crossed again after 15 s, and that
  fill shows in the outcome.
* The USDTTMN rebalance was still working when the BTCUSDT entry was checked, so the entry's
  `USDT_BOUNDS` counts it (+533,782 TMN of USDT) and `RATE_*` reserves its 2 possible re-prices.
* `pnl_pct = -0.30 %` (first run: -3.5 %). The BUY fee is charged in BTC, so the position holds
  0.000029970 BTC; orders round down to the 6-decimal step, so 0.000029 is sold and 0.00000097 BTC
  (below the minimum order) stays in the account. The position now closes with that remainder
  valued as dust at the closing price (`dust_qty` in the outcome); the first run booked it as
  lost (about -21,000 TMN).

## (c) Synthetic -8 % shock → kill → flatten → reset-kill

`$A/inject_c.jsonl`: a BTCUSDT entry at 5 s (0.00004 BTC, stop 81600, taker), and an entry-like
USDTTMN rebalance at 120 s, which must be vetoed while killed.

```
$ python -m wta run --mode shadow --feed synthetic --seconds 200 --inject-intents $A/inject_c.jsonl \
    --paper-balances '{"TMN": 2000000, "USDT": 12}' --synthetic-shock-at 60 --synthetic-shock-pct -0.08 \
    --log-dir $A/logs_c --set data.sqlite_path=$A/c.sqlite
exit=0
```

The shock multiplies the dollar (USDTTMN) and every coin's USDT mid by 0.92. A coin's TMN book
(USDT mid x dollar) therefore drops about 15.4 %.

```
events:
  12:46:30.065Z start: engine started in shadow mode
  12:47:30.379Z kill: drawdown_kill: drawdown 6.19% >= 5.00% from peak 5,201,792 TMN (equity 4,879,939 TMN)
  12:47:30.379Z flatten: kill-switch flatten started: every non-TMN balance is sold to TMN, then the engine only monitors
  12:49:40.672Z flatten: kill flatten complete: only TMN and dust left
  12:49:50.850Z stop: ran for 200 s
heartbeats (monitoring mode continues while killed):
  12:47:30 equity=5200795.84 dd=0.0002 pos=1 orders=1 killed=False
  12:48:30 equity=4878609.01 dd=0.0621 pos=0 orders=1 killed=True
  12:49:30 equity=4871499.83 dd=0.0635 pos=0 orders=1 killed=True
  12:49:50 equity=4869887.02 dd=0.0638 pos=0 orders=0 killed=True
$ python -m wta logs check --dir $A/logs_c   -> decisions 11 lines, health 7, events 11, orders 44: OK
```

Decisions:

```
d_..._S3_BTCUSDT_inj2           entry, approved, 23 checks -> outcome: exit_reason=stop, pnl_tmn=-138490.55, hold_s=72.1
d_..._S3_BTCUSDT_inj2_stop1     STOP_LIMIT SELL 0.000039 stop 81600 -> CANCELED_FOR_EXIT
d_..._S3_BTCUSDT_1              stop exit SELL 0.000039 BTCUSDT (taker), 23 checks -> FILLED avg 76,154.24
                                features.stranded_server_stop = the STOP_LIMIT: "bid 76201.96 is below its
                                limit 81273.60; it is canceled first"; notes: server stop ... canceled first
d_..._FLATTEN_USDTTMN_1         flatten SELL 8.68 USDTTMN -> FILLED avg 245,548
d_..._S2_USDTTMN_inj3           veto: KILLED: kill-switch active (drawdown_kill: ...): only flatten/exit/stop SELLs
                                allowed, got BUY rebalance (also RATE_HOUR, DAILY_LOSS, DRAWDOWN_KILL, SELF_CROSS)
d_..._FLATTEN_USDTTMN_2         flatten SELL 2.97 USDTTMN (the USDT the stop exit brought in) -> FILLED avg 245,382
```

The -8 % gap triggered the STOP_LIMIT, but its limit (stop x 0.996 = 81,273.60) sat above the
new market, so it could never fill there. The PositionManager sees that (bid below the live stop's
limit) and emits a taker stop exit; the OrderManager cancels the stranded stop first. In the first
run the position stayed open with that useless stop until the kill-switch flatten sold it. The
flatten took longer this time (130 s) because a second USDT sale was needed for the proceeds of
the BTCUSDT stop exit, and it went maker-first again.

Manual reset (`wta reset-kill`), then a 30 s restart on the same store:

```
$ python -m wta reset-kill --log-dir $A/logs_c --set data.sqlite_path=$A/c.sqlite
wta reset-kill: error: the following arguments are required: --reason          exit=2
$ python -m wta reset-kill --reason "acceptance (c): synthetic -8% shock reviewed, account flat in TMN" \
    --log-dir $A/logs_c --set data.sqlite_path=$A/c.sqlite
kill-switch reset: acceptance (c): synthetic -8% shock reviewed, account flat in TMN
  previous kill reason: drawdown_kill: drawdown 6.19% >= 5.00% from peak 5,201,792 TMN (equity 4,879,939 TMN)
  peak equity moved to the latest equity: 4869887.235828089120
  reset_kill event written to $A/logs_c/events.jsonl and $A/c.sqlite
  a running engine applies it on its next tick; otherwise the next start does
exit=0
$ python -m wta run --mode shadow --feed synthetic --seconds 30 --log-dir $A/logs_c --set data.sqlite_path=$A/c.sqlite \
    --set engine.heartbeat_interval_s=10
exit=0
events (continued):
  12:49:51.595Z reset_kill: acceptance (c): synthetic -8% shock reviewed, account flat in TMN
  12:49:51.992Z start: engine started in shadow mode                     (data.reason = "restart")
  12:49:51.992Z reset_kill_applied: kill-switch reset applied: ...        (flatten_orders_canceled: 0, already flat)
  12:49:52.195Z restart: state restored from the store (peak, day start, fees, counters, flags, positions, orders)
  12:49:52.198Z pause: daily_loss: daily PnL -6.30% <= -2.50%; no new entries until 2026-10-23T00:00:00.000Z
                (entries_canceled: 0)
  12:50:22.085Z stop: ran for 30 s
heartbeats after the reset: killed=False, dd=0 (peak = the reset equity), paused=True (daily loss)
$ python -m wta logs check --dir $A/logs_c   -> OK
```

After the reset the kill does not fire again: drawdown is now measured from the reset equity.
The **daily-loss** rule still pauses entries until the next UTC day (-6.30 % of the day-start
equity). The two guards are independent, as intended. A reset clears the kill-switch, not the
day's loss.

## (d) `scripts/flatten_now.py` on a mixed paper portfolio

```
$ python scripts/flatten_now.py --feed synthetic \
    --paper-balances '{"TMN": 1000000, "USDT": 5, "BTC": "0.00002", "ETH": "0.001", "SOL": "0.05", "DOGE": 40, "XRP": 3}' \
    --phase1-seconds 60 --timeout 300 --log-dir $A/logs_d --set data.sqlite_path=$A/d.sqlite
flatten_now: paper account (feed synthetic); stopped: flatten complete
before:
  BTC    0.00002
  DOGE   40
  ETH    0.001
  SOL    0.05
  TMN    1000000
  USDT   5
  XRP    3
  equity 7189164.17096 TMN, of which TMN 1000000 (13.9098 %)
after:
  TMN    7173795.4264429500
  equity 7173795.4264429500 TMN, of which TMN 7173795.4264429500 (100.0000 %)
result: 100 % TMN (dust excepted)
exit=0
```

Six flatten SELLs (BTCTMN, DOGETMN, ETHTMN, SOLTMN, USDTTMN, XRPTMN), each FILLED. Placements by
reason: 6 `submit` (maker-first), 14 `reprice`, 6 `cross`; all 26 placements were SELLs. Equity
lost 0.21 % to fees and spread. `wta logs check --dir $A/logs_d`:
OK. Phase 2 (MARKET) was not needed here; it is covered by `tests/test_flatten_now.py` (MARKET
where allowed, a LIMIT under the bid on SOLTMN, where MARKET is disabled).

## (e) Restart mid-run keeps peak, day start, fees, counters and positions

```
$ python -m wta run --mode shadow --feed synthetic --seconds 60 --inject-intents $A/inject_e.jsonl \
    --paper-balances '{"TMN": 3000000, "USDT": 10}' --log-dir $A/logs_e --set data.sqlite_path=$A/e.sqlite \
    --set engine.heartbeat_interval_s=20                                   exit=0
$ python state.py $A/e.sqlite        # kv state after run 1 (state_e1.txt)
$ python -m wta run --mode shadow --feed synthetic --seconds 40 --log-dir $A/logs_e \
    --set data.sqlite_path=$A/e.sqlite --set engine.heartbeat_interval_s=20  exit=0
$ python state.py $A/e.sqlite        # after run 2 (state_e2.txt)
$ diff state_e1.txt state_e2.txt     # no difference
peak_equity_tmn      5668160.000000000000
day_start_equity_tmn 5667200
fees_paid_tmn_total  861.9183295710
placements (orders)  2
flags                {"flatten_phase": 0, "kill_reason": "", "killed": false, "pause_reason": "", "paused": false, ...}
open positions       [('p_S3_BTCUSDT_1792673451506_1', '0.0000299610', 'S3_BTCUSDT_1792673451506_2DDA')]
live orders          ['S3_BTCUSDT_1792673451506_2DDA stop']
```

The second run's events: `start` (`data.reason = "restart"`), then `restart` with the restore
summary. The startup reconcile found nothing to repair:

```json
{"reason": "startup", "missed_fills": 0, "adopted": [], "finalized": [], "drift": {}, "errors": []}
```

The startup reconcile now runs only once the books it values with are on the bus (the data feed
starts before the startup; here the synthetic books were there at once, so it was not deferred).

The server stop was still managed after the restart, and the position kept its stop. Note: the
synthetic feed restarts from the reference mids (its random walk is not persisted), so equity
jumps slightly at a restart. That jump comes from the synthetic data, not from the engine.

## Full suite

```
$ .venv/bin/ruff check src tests scripts backtest
All checks passed!
$ .venv/bin/python -m pytest
973 passed
$ python -m wta logs check --secrets --dir $A/logs_{a,b,c,d,e}   # the runs loaded the real .env API key
OK / secrets scan: clean   (all five; also covers deliverables/ and docs/REPORTS/)
```

`tests/test_engine.py` (31 tests) and `tests/test_flatten_now.py` (6 tests) cover the same paths
deterministically on a `SimClock`:

* entry → server stop → time-stop exit → outcome;
* drawdown kill → entries canceled → flatten → `reset-kill`;
* the KILL file and the exception storm;
* deadline phases (`flatten_start`, `flatten_final` with MARKET orders, `final_state`, the
  challenge end);
* restart and a reset applied at startup;
* `engine_books` / `ticks` dedupe and veto-repeat suppression;
* the CLI options and the synthetic shock;
* the review fixes: pause cancels entries, kill reset cancels the kill's flatten orders, a stop
  never cuts a tick in half, the deferred startup reconcile, the live deadline guard, the
  one-engine-per-account lock, tick-frozen ids and timers, one commit per tick (a crash restores
  the previous tick), a stranded STOP_LIMIT and a dead server stop found by the reconciler.
