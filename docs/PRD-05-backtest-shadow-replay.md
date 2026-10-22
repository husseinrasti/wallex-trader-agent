# PRD-05 — Backtesting, Market Survey, Shadow Testing, Replay, Metrics & Reports

**Depends on:** PRD-01…04.
**Goal:** prove the strategy logic on historical and recorded data, prove the live run is reproducible, and generate judge-facing reports.

Scoring targets: backtest + shadow 20 pts, re-runnability 25 pts, strategy logic 40 pts (evidence).

---

## 1. Data sources

| Source | Used for | How obtained |
|---|---|---|
| 1-min candles, 30 days, watchlist | S2, S3 backtests; S1 proxy backtest | `python -m wta backfill --resolution 1 --days 30` (UDF history, paged per day) |
| 15-min candles | S2/S3 signals | derived from 1-min (resample) — never fetch separately to keep one truth |
| Recorded order books (≥ 1 snapshot/2 s/market) | S1 opportunity survey + S1 backtest; paper fills | `python -m wta record` running since day 0 (PRD-01) |
| Recorded trades | fill realism checks | same recorder |
| Our live/shadow logs | replay, reports | `logs/` |

Store everything in the same SQLite (`data/wta.sqlite`); export to Parquet for reports (`python -m wta export --parquet data/export/`).

---

## 2. Market survey (`scripts/survey.py`) — run before tuning S1/S2

Reads recorded books for the last N hours and prints + writes `docs/REPORTS/survey_<date>.md`:
- Per market: median/p90 spread %, median depth within 0.3% (TMN), snapshot count, data gaps.
- Cross-book (per coin): time series of `edge_tmn_cheap_pct` and `edge_usdt_cheap_pct`; for margins `m ∈ {0.05%, 0.10%, 0.15%, 0.25%, 0.40%}` → count of **distinct opportunities** (edge ≥ cost + m, with ≥ 60 s separation), their median duration (seconds until edge < cost), and gross edge sum. Also the maker-leg-1 variant (`cost` with maker fee) vs taker.
- USDTTMN: 15-min ATR %, 1-h/24-h return distribution, time in trend vs range by `rule_regime`.
- A **recommendation block** (computed, not LLM): suggested `s1.margin_min_pct`, `s1.leg1_taker`, and whether S1 lock-in is viable (≥ 3 opportunities/day at ≥ 0.10% margin) — this is copied into `docs/STRATEGY.md`.

---

## 3. Backtest engine (`backtest/`)

### 3.1 Design
- Same `Engine` and strategies; only `Exchange = BacktestExchange` and the data bus is driven by a **clock** that replays stored candles/books in time order. `now` = simulated time (strategies/risk never read the wall clock — enforced by injecting `clock`).
- Two drivers:
  - **Book-driven** (for S1, S2 and realistic fills): iterate recorded book snapshots (2-s resolution). Requires recorded data (only the hours we recorded during the hackathon — be honest about the window length in the report).
  - **Candle-driven** (for S2/S3 over 30 days): 1-min candles; synthetic book = `mid=close`, spread = market's median observed spread, depth unlimited at best (we are tiny). S1 is **not** backtested on candles (no cross-book info) — state this in the report; S1 evidence comes from the survey + book-driven window + live logs.
- The LLM analyst in backtests: either `ai=off`, or `ai=on` with the analyst called every 5 simulated minutes using `_local` tools over the historical store (cached; first run hits the API — budget: 30 days × 288 = 8,640 calls ⇒ **use a 3-day backtest window for ai=on**, 30 days for ai=off).

### 3.2 Fill model (`backtest/fills.py`) — pessimistic by construction
- LIMIT (maker-first) BUY fills when `best_ask ≤ price` **on the next snapshot/bar** (not the same one), qty ≤ 80% of level qty (book-driven) or unlimited (candle-driven); maker fee if the price was not crossing at placement, else taker.
- Candle-driven LIMIT: BUY fills if `low ≤ price` in a *later* bar; price = min(price, open) for gap-through; SELL symmetric.
- MARKET / crossing LIMIT: fill at opposite best (book) or at `close × (1 ± spread/2 ± slippage)` with `slippage = 0.02%` (candle), taker fee.
- STOP_LIMIT: triggers when `low ≤ stop` (bar) / `best_bid ≤ stop` (book), then behaves as LIMIT at the stop-limit price; if the bar's low is below the limit price, assume fill at the limit price minus one tick (conservative) — but if the gap is > 2× the slippage allowance, assume a partial 50% fill and the rest at `low` (stress).
- Fees per market from `/v1/account/fee` snapshot stored in `configs/fees_snapshot.yaml` (so backtests do not need API access).
- Latency: all orders act one step later.

### 3.3 CLI
```
python -m wta backtest --driver candles --from 2026-09-22 --to 2026-10-22 --ai off --strategies S2,S3 --out docs/REPORTS/bt_candles_30d
python -m wta backtest --driver books   --from 2026-10-22T10:00Z --to 2026-10-23T10:00Z --ai on  --strategies S1,S2 --out docs/REPORTS/bt_books_24h
python -m wta backtest ... --param-grid strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015,0.0025   # small grid, walk-forward split 70/30
```
Outputs: `summary.json`, `trades.csv`, `equity.csv`, `decisions.jsonl` (same schema as live), `report.md` with PNGs.

### 3.4 Walk-forward & overfitting guard
- Any parameter grid must be evaluated **in-sample (first 70%) / out-of-sample (last 30%)**; the report shows both; defaults are chosen from in-sample and must not degrade > 50% out-of-sample, otherwise keep the pre-grid default. Grid size ≤ 8 values per parameter, ≤ 2 parameters at a time. Document this rule in `docs/STRATEGY.md` (judges like it).

---

## 4. Metrics (`backtest/metrics.py`) — used for backtest, shadow and live alike

From `equity.csv` (10-s or 1-min) and `trades.csv`:
- `return_pct` = final/initial − 1 (TMN-valued), `max_drawdown_pct`, `drawdown_duration_max`, `daily_returns`, `sharpe_annualized` (from 1-h returns; report but don't over-interpret for 3 days), `sortino`
- `n_trades` (closed positions/cycles), `n_orders`, `win_rate`, `profit_factor`, `avg_pnl_pct`, `avg_hold_s`, `fees_paid_tmn`, `fees_pct_of_equity`, `turnover`
- Per strategy breakdown; per day breakdown; **vetoes by risk code** (from decisions)
- `realized_vs_unrealized` at the end (must be ~0 unrealized after flatten)
- AI metrics: number of plans, distribution of regimes, average `size_multiplier`, how often a plan changed enabled strategies, trades gated by regime (S3), **ablation delta** (ai on − off) for return, DD, trades.

---

## 5. Shadow testing (`--mode shadow`)

- Live data bus + `PaperExchange` (PRD-02 §9) + full engine incl. AI. Produces the same logs as live into `logs/shadow/`.
- Mandatory: ≥ 4 h shadow before first live; ≥ 1 h shadow after any strategy change (can run **in parallel** with live from the same recorder data — separate process, separate log dir).
- `python -m wta report --source logs/shadow --out docs/REPORTS/shadow_<date>` produces the same report as backtests plus a **paper-vs-live divergence** section if a live run overlaps (compare intents by time: same signals? different fills?).

---

## 6. Replay (`python -m wta replay`)

Purpose: prove to judges that the delivered code reproduces the delivered decisions.
- Inputs: `data/wta.sqlite` (books/candles as the live engine saw them — the engine stores every snapshot it acted on, flagged `used=1`, PRD-01), `logs/llm_cache/`, `configs/` versions, `logs/decisions.jsonl` (reference).
- Runs the engine with `Exchange = ReplayExchange` that returns the **recorded order results/fills** for the same `client_id`s (from `orders.jsonl`), so the portfolio state evolves identically.
- Asserts, decision by decision: same `strategy, symbol, side, purpose, qty (after risk), price, reason, risk checks, plan prompt_hash`. Differences are listed in `docs/REPORTS/replay_<date>.md` with a summary `N/N decisions reproduced`. Timestamp fields are excluded from equality; the engine's `rng_seed` is fixed.
- `scripts/replay_check.py` is the one-liner for judges: `make replay` → prints the result and exits non-zero on mismatch.
- Known non-determinism sources to eliminate: dict ordering (sort), float formatting (Decimal), wall-clock reads (clock injection), set iteration (sorted), LLM (cache).

---

## 7. Reports (`backtest/report.py`, `python -m wta report`)

Each report folder has `report.md` with: config hash + git commit, data window, metrics table, equity curve PNG (TMN), drawdown PNG, trades table (first 50 + CSV link), per-strategy table, veto histogram, AI section (plans count, regime timeline PNG, ablation table), fees vs budget, honest limitations paragraph (auto-filled: e.g., "S1 backtest window limited to recorded 31 h").
`docs/REPORTS/README.md` indexes all reports. The final `deliverables/REPORT.md` concatenates: survey → backtests (candles 30 d, books window, ablation) → shadow → live (actual challenge run) → replay proof.

---

## 8. Tests

- `test_fills.py`: each fill rule, pessimistic latency, STOP_LIMIT gap handling, fee selection.
- `test_backtest_runner.py`: tiny synthetic dataset end-to-end; S2 initial allocation occurs; S3 breakout produces one trade with stop; equity math.
- `test_metrics.py`: known equity/trade series → expected numbers.
- `test_replay.py`: run engine in shadow on a fixture stream → replay → 100% match; mutate a cached plan → mismatch detected.

## 9. Acceptance checklist

- [ ] `docs/REPORTS/survey_<date>.md` exists with S1 opportunity counts and recommendation block.
- [ ] 30-day candle backtest (ai off) and ≥ 24-h book backtest (ai on/off) reports exist with PNGs.
- [ ] Shadow report ≥ 4 h exists.
- [ ] `make replay` reproduces the live decision log (target 100% of deterministic fields; document any exception).
- [ ] `deliverables/REPORT.md` assembled.
