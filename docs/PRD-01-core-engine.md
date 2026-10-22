# PRD-01 — Core Engine: Skeleton, Config, Models, Wallex Client, Data Store, Recorder, Decision Log

**Depends on:** `00-OVERVIEW.md`. **Blocks:** PRD-02…06.
**Goal:** a runnable package `wta` that can (a) talk to Wallex safely, (b) record market data continuously, (c) persist everything needed for replay, (d) write decision records — with no trading logic yet.

---

## 1. Deliverables

| # | Deliverable | Acceptance |
|---|---|---|
| 1.1 | Repo skeleton exactly as in Overview §7, `pyproject.toml`, `Makefile`, `.env.example`, `.gitignore`, `README.md` stub, `ruff` + `pytest` configured | `make install && make test` passes on a clean clone |
| 1.2 | `wta.config` — typed settings from `configs/*.yaml` + env; `config_hash` | `python -m wta config show` prints resolved config with secrets masked and the hash |
| 1.3 | `wta.models` — all pydantic models in §4 | import-clean; JSON round-trip tests |
| 1.4 | `wta.exchange.wallex_rest` — async REST client with auth, rate limiting, retries, precision helpers | `python -m wta markets` lists allowed markets with tick/step; `scripts/check_account.py` runs |
| 1.5 | `wta.exchange.wallex_ws` — socket.io depth/trade feed with automatic REST fallback | `python -m wta feed BTCUSDT` prints live best bid/ask from either source, labelled |
| 1.6 | `wta.data.store` — SQLite schema + writers/readers | recorder writes ≥ 1 snapshot/s/market; readers return typed objects |
| 1.7 | `scripts/record_market.py` + `python -m wta record` | runs for hours without crash; survives API errors; daily file rotation |
| 1.8 | `wta.logging_.decision_log` + `health_log` | writes schema-valid JSONL; schema test |
| 1.9 | `wta.utils.gitinfo` | returns commit, dirty flag; engine refuses to start dirty in live |
| 1.10 | `scripts/check_account.py` | prints fees, balances, sub-account routing check, order-type probe results (no real orders unless `--probe-order`) |

---

## 2. Project setup details

### 2.1 `pyproject.toml`
```toml
[project]
name = "wta"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27", "pydantic>=2.7", "pydantic-settings>=2.3", "pyyaml>=6",
  "python-socketio[client]==4.6.1", "python-engineio==3.14.2",   # socket.io v2-compatible; isolate in its own task
  "rich>=13", "pandas>=2.2", "numpy>=1.26", "matplotlib>=3.8",
  "aiosqlite>=0.20", "orjson>=3.10", "python-dotenv>=1.0",
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.5", "respx>=0.21", "freezegun>=1.5"]
[tool.ruff] line-length = 110
[tool.pytest.ini_options] asyncio_mode = "auto"
```
If `python-socketio 4.x` conflicts with Python 3.12+, pin Python 3.11 in Dockerfile; if it still fails, ship REST polling only and note it in `docs/ARCHITECTURE.md` (WS is an optimization, not a requirement).

### 2.2 `Makefile` targets
```
install   : python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
test      : ruff check src tests && pytest -q
record    : python -m wta record --symbols-from configs/markets.yaml
survey    : python scripts/survey.py --since 24h
shadow    : python -m wta run --mode shadow
live      : WTA_LIVE=1 python -m wta run --mode live --i-understand-live
backtest  : python -m wta backtest --from $(FROM) --to $(TO)
replay    : python -m wta replay --decisions logs/decisions.jsonl
report    : python -m wta report --out docs/REPORTS
flatten   : python scripts/flatten_now.py --i-understand-live
```

### 2.3 `.gitignore`
```
.env
.venv/
data/
logs/
__pycache__/
*.pyc
.pytest_cache/
!deliverables/
!deliverables/**
```

### 2.4 Git workflow (the repo goes to GitHub)
- `git init`, first commit = skeleton. Conventional commits: `feat(risk): …`, `fix(exec): …`, `docs: …`, `chore: …`.
- Never commit `.env`, `data/`, `logs/`. `deliverables/` is committed at the end of the challenge (decision logs copied there).
- Before each live start: `make test` → `git tag live-$(date -u +%Y%m%d-%H%M)` → `git push --tags`.
- `wta.utils.gitinfo.get()` → `{"commit": str, "dirty": bool, "tag": str|None}`; live mode aborts if `dirty` unless `--allow-dirty`.

---

## 3. Configuration (`wta/config.py`)

- `Settings` (pydantic-settings) reads env (`.env` via dotenv) for secrets/urls/mode; `Config` (pydantic) reads `configs/default.yaml`, `configs/markets.yaml`, `configs/llm.yaml` and merges CLI overrides (`--set risk.max_order_pct=0.2`).
- `config_hash`: `sha256(orjson.dumps(config.model_dump(), option=OPT_SORT_KEYS))[:12]`.
- `python -m wta config show` and `config hash`.

### 3.1 `configs/default.yaml` (complete initial values)
```yaml
engine:
  fast_loop_interval_s: 2
  slow_loop_interval_s: 300
  reviewer_interval_s: 3600
  reconcile_interval_s: 300
  heartbeat_interval_s: 60
  stale_data_s: 30
  initial_equity_tmn: 5000000
  challenge_start_utc: "${CHALLENGE_START_UTC}"
  challenge_end_utc: "${CHALLENGE_END_UTC}"
  flatten_start_before_end_s: 7200     # T-2h
  flatten_final_before_end_s: 1800     # T-30min
data:
  sqlite_path: data/wta.sqlite
  depth_levels_stored: 10
  rest_poll_interval_s: 2
  ws_enabled: true
  rate_limit_rps: 6
  http_timeout_s: 8
  max_retries: 4
risk:
  max_order_pct_equity: 0.25
  max_order_pct_of_best_level_qty: 0.8
  max_coin_exposure_pct: 0.40
  max_concurrent_positions: 3
  max_open_orders: 8
  max_orders_per_hour: 10
  max_orders_per_day: 60
  daily_loss_limit_pct: 0.025
  max_drawdown_kill_pct: 0.05
  fee_budget_pct: 0.02
  price_disagreement_pause_pct: 0.05
  consecutive_api_errors_pause: 5
  min_notional_tmn: 50000
  min_notional_usdt: 1
execution:
  limit_reprice_after_s: 15
  limit_max_reprices: 2
  limit_cancel_after_s: 45
  maker_offset_ticks: 1
  use_server_side_stops: true
  stop_limit_slippage_pct: 0.004       # STOP_LIMIT limit price = stop * (1 - this) for sells
strategies:
  s1_crossbook:
    enabled: true
    coins: [BTC, ETH, SOL, XRP, DOGE, ADA, TRX, LTC, LINK, AVAX]
    margin_min_pct: 0.0015              # above total cost
    lockin_enabled: true
    revert_enabled: true
    revert_max_hold_s: 1200
    revert_stop_pct: 0.010
    revert_target_fraction: 0.6         # take profit when 60% of deviation closed
    cooldown_s: 60
    max_concurrent: 2
    size_pct_equity: 0.20
  s2_usdtalloc:
    enabled: true
    target_share_by_regime: {range: 0.50, trend_up: 0.65, trend_down: 0.30, chaos: 0.40}
    bounds: [0.25, 0.70]
    rebalance_threshold_pp: 0.15
    min_rebalance_interval_s: 1800
    ema_fast: 20
    ema_slow: 60
    candle_resolution: "15"
  s3_momentum:
    enabled: false                       # enable after MVP live
    symbols: [BTCUSDT, ETHUSDT, SOLUSDT]
    candle_resolution: "15"
    breakout_lookback: 20
    volume_mult: 1.5
    atr_period: 14
    stop_atr_mult: 1.5
    target_atr_mult: 2.5
    max_hold_s: 14400
    risk_per_trade_pct: 0.005
    max_size_pct_equity: 0.20
    min_regime_confidence: 0.6
ai:
  enabled: true
  provider: openrouter                  # openrouter|gemini
  temperature: 0.0
  analyst_tools_enabled: true
  cache_dir: logs/llm_cache
  replay_strict: true
alerts:
  telegram_enabled: false
```

### 3.2 `configs/markets.yaml`
```yaml
quote_assets: [TMN, USDT]
watchlist:            # symbols the fast loop keeps fresh
  - USDTTMN
  - BTCTMN
  - BTCUSDT
  - ETHTMN
  - ETHUSDT
  - SOLTMN
  - SOLUSDT
  - XRPTMN
  - XRPUSDT
  - DOGETMN
  - DOGEUSDT
banned:               # verbatim from organizer; engine refuses these
  - ATMN
  - GTMN
  # ... paste the full organizer list here, one symbol per line, with "/" removed (A/TMN -> ATMN)
```
The coding agent must paste the **entire** organizer ban list (provided separately in the chat; ~400 symbols). `wta markets` must print any watchlist symbol that is banned and exit non-zero.

---

## 4. Data models (`wta/models.py`) — single source of truth

All models are pydantic v2, `frozen=True` unless noted, `Decimal` for money.

```python
class MarketInfo(BaseModel):
    symbol: str; base: str; quote: str
    tick_decimals: int        # from tickSize
    step_decimals: int        # from stepSize
    min_qty: Decimal; min_notional: Decimal
    market_orders_enabled: bool   # isMarketTypeEnable
    maker_fee: Decimal; taker_fee: Decimal   # filled from /v1/account/fee; default by quote (TMN .0025/.003, USDT .001/.0013)

class Level(BaseModel): price: Decimal; qty: Decimal
class BookSnapshot(BaseModel):
    symbol: str; ts: float; source: Literal["ws","rest","replay","backtest"]
    bids: list[Level]; asks: list[Level]      # bids desc, asks asc, ≤ 10 levels
    @property best_bid/best_ask/mid/spread_pct

class Trade(BaseModel): symbol: str; ts: float; price: Decimal; qty: Decimal; side: Literal["BUY","SELL"]|None
class Candle(BaseModel): symbol: str; resolution: str; ts: int; o: Decimal; h: Decimal; l: Decimal; c: Decimal; v: Decimal

class Balance(BaseModel): asset: str; free: Decimal; locked: Decimal

OrderType = Literal["LIMIT","MARKET","STOP_LIMIT","STOP_MARKET"]
Side = Literal["BUY","SELL"]
OrderStatus = Literal["NEW","PARTIALLY_FILLED","FILLED","CANCELED","REJECTED","EXPIRED","UNKNOWN"]

class OrderRequest(BaseModel):
    client_id: str; symbol: str; side: Side; type: OrderType
    qty: Decimal; price: Decimal|None = None; stop_price: Decimal|None = None
    strategy: str; purpose: Literal["entry","exit","stop","rebalance","flatten","leg2"]
    decision_id: str

class Order(BaseModel, frozen=False):
    req: OrderRequest; exchange_id: str|None; status: OrderStatus
    filled_qty: Decimal = 0; avg_price: Decimal|None; fee_paid: Decimal = 0; fee_asset: str|None
    created_ts: float; updated_ts: float; raw: dict = {}

class Fill(BaseModel): order_client_id: str; symbol: str; side: Side; price: Decimal; qty: Decimal; fee: Decimal; fee_asset: str; ts: float; is_maker: bool|None

class Position(BaseModel, frozen=False):
    id: str; strategy: str; symbol: str; base: str; quote: str
    qty: Decimal; entry_price: Decimal; entry_ts: float
    stop_price: Decimal|None; target_price: Decimal|None; time_stop_ts: float|None
    stop_order_client_id: str|None; meta: dict = {}

class Intent(BaseModel):
    id: str; ts: float; strategy: str; symbol: str; side: Side
    order_type: OrderType; qty: Decimal|None; notional: Decimal|None
    price: Decimal|None; stop_price: Decimal|None
    purpose: str; reason: str            # human-readable, goes to the decision log verbatim
    features: dict; expected_edge_pct: Decimal|None
    linked_position_id: str|None = None

class RiskCheck(BaseModel): code: str; passed: bool; detail: str
class RiskDecision(BaseModel): approved: bool; checks: list[RiskCheck]; adjusted_qty: Decimal|None; veto_reason: str|None

class MarketPlan(BaseModel):   # LLM output, see PRD-04 for full schema
    ts: float; regimes: dict[str, RegimeCall]; enabled_strategies: list[str]
    s1_margin_min_pct: Decimal; size_multiplier: Decimal; usdt_target_share: Decimal
    notes: str; model: str; prompt_hash: str; cached: bool

class DecisionRecord(BaseModel):   # one line of decisions.jsonl
    decision_id: str; ts: str; mode: str; git_commit: str; config_hash: str
    strategy: str; symbol: str
    features: dict; plan_snapshot: dict|None
    intent: dict|None; risk: dict; order: dict|None
    outcome: dict|None            # filled later: pnl_tmn, pnl_pct, closed_ts, exit_reason
    notes: str
```

---

## 5. Wallex REST client (`wta/exchange/wallex_rest.py`)

- `class WallexREST(base_url, api_key, rps=6, timeout=8, max_retries=4)` using `httpx.AsyncClient`.
- **Rate limiter:** token bucket (`rps` tokens/s, burst `rps`). **Retries:** on 429/5xx/timeouts with exponential backoff `1,2,4,8 s` (+ jitter), honor `Retry-After`. Never retry a `POST /orders` blindly: after a timeout, first `GET /orders/{client_id}`; only re-send if the order does not exist (idempotency via `client_id`).
- **Error model:** raise `WallexAPIError(status, code, message, payload)`; `success: false` bodies are errors.
- Methods (all return typed models; keep `raw` dict for unknown shapes):
  - `get_markets() -> dict[str, MarketInfo]`
  - `get_depth(symbol, levels=10) -> BookSnapshot`
  - `get_all_depth() -> dict[str, BookSnapshot]` (`/v2/depth/all`)
  - `get_trades(symbol) -> list[Trade]`
  - `get_candles(symbol, resolution, from_ts, to_ts) -> list[Candle]`
  - `get_fees() -> dict[str, (maker, taker)]`
  - `get_balances() -> dict[str, Balance]`
  - `place_order(OrderRequest) -> Order`
  - `get_order(client_id) -> Order`
  - `cancel_order(client_id) -> Order`
  - `get_open_orders(symbol=None) -> list[Order]`
  - `get_my_trades(symbol=None, side=None) -> list[Fill]`
- **Precision helpers** (`wta/utils/decimalutil.py`): `round_price(symbol, price, side)`, `round_qty(symbol, qty)`, `tick(symbol)`, `check_min_notional(symbol, qty, price)`.
- **Status mapping:** map Wallex status strings to `OrderStatus`; unknown → `UNKNOWN` and log raw.
- **Masking:** API key never appears in logs/exceptions.
- Unit tests with `respx` mocking: rate limit, 429 backoff, idempotent re-send, precision rounding, status mapping.

---

## 6. WebSocket feed (`wta/exchange/wallex_ws.py`)

- `class WallexWS(symbols, on_book, on_trade)`; connects socket.io to `https://api.wallex.ir`, `transports=["websocket"]`; on connect emits `subscribe` for `{sym}@buyDepth`, `{sym}@sellDepth`, `{sym}@trade`; handles `Broadcaster` events; maintains per-symbol partial book (buy side + sell side merged into `BookSnapshot(source="ws")`).
- Runs in its own `asyncio` task with reconnect/backoff; publishes to an in-memory `MarketDataBus` (latest snapshot per symbol + `last_update_ts`).
- **Fallback:** `RestPoller` task polls `/v1/depth` for watchlist every `rest_poll_interval_s` and publishes `source="rest"`. The bus keeps whichever is fresher. If WS has not delivered for `stale_data_s`, log a warning once per minute. The engine must function with WS disabled (`data.ws_enabled=false`).
- `python -m wta feed SYMBOL` prints `ts source bid ask spread%` once per second.

---

## 7. Data store (`wta/data/store.py`) — SQLite (WAL mode)

Tables:
```sql
books(ts REAL, symbol TEXT, source TEXT, bids_json TEXT, asks_json TEXT)            -- ≤10 levels each
trades(ts REAL, symbol TEXT, price TEXT, qty TEXT, side TEXT)
candles(symbol TEXT, resolution TEXT, ts INTEGER, o TEXT, h TEXT, l TEXT, c TEXT, v TEXT, PRIMARY KEY(symbol,resolution,ts))
balances(ts REAL, asset TEXT, free TEXT, locked TEXT)
equity(ts REAL, equity_tmn TEXT, tmn TEXT, usdt_tmn_value TEXT, coins_tmn_value TEXT, peak_tmn TEXT, drawdown_pct TEXT)
orders(client_id TEXT PRIMARY KEY, json TEXT, updated_ts REAL)
fills(id INTEGER PRIMARY KEY AUTOINCREMENT, order_client_id TEXT, json TEXT, ts REAL)
positions(id TEXT PRIMARY KEY, json TEXT, open INTEGER, updated_ts REAL)
plans(ts REAL, json TEXT, prompt_hash TEXT)
events(ts REAL, kind TEXT, json TEXT)    -- engine-level events: start, stop, pause, kill, flatten, config_change
```
Indexes on `(symbol, ts)` for books/trades/candles. Writers are batched (flush every 1 s or 200 rows). Readers: `iter_books(symbol, from_ts, to_ts)`, `load_candles(...)`, `latest_equity()`, etc. Retention: none during the challenge (disk is cheap); `python -m wta db vacuum` optional.

**Recorder (`python -m wta record`, also `scripts/record_market.py`):** subscribes to the bus for watchlist symbols and writes every snapshot (dedupe identical consecutive snapshots per symbol) + trades; fetches 1-min candles every 60 s for watchlist symbols (incremental, upsert). Must run as a standalone process so data collection continues even if the trading engine is restarted. Logs a health line every 60 s (`rows_written`, `ws_ok`, `rest_ok`, errors).

**Historical backfill:** `python -m wta backfill --symbols ... --resolution 1 --days 30` pages `/v1/udf/history` in ≤ 1-day windows and upserts candles (needed by PRD-05).

---

## 8. Decision log (`wta/logging_/decision_log.py`)

- Append-only JSONL at `logs/decisions.jsonl` (rotated daily: `decisions-YYYYMMDD.jsonl`, plus a symlink/`current`), one `DecisionRecord` per **decision** — including vetoed intents (risk said no) and "no-trade" slow-loop plans (kind `plan`). Fields are in §4.
- `outcome` is filled by a later **update record** (`{"decision_id": ..., "outcome": {...}, "kind": "outcome"}`) — never rewrite earlier lines (append-only guarantees integrity for judges). A small reader merges them.
- `logs/orders.jsonl`: every order request/response/cancel/fill, raw.
- `logs/health.jsonl`: heartbeat every 60 s: `{ts, mode, git_commit, config_hash, equity_tmn, drawdown_pct, open_orders, positions, ws_ok, rest_ok, api_errors_1m, llm_ok, paused, reasons[]}`.
- `logs/events.jsonl`: start/stop/pause/resume/kill/flatten/config_change with reason.
- A `jsonschema`/pydantic validation test for each file type; a `python -m wta logs check` command validates all logs.

---

## 9. CLI (`wta/cli.py`)

`python -m wta <cmd>`: `config show|hash`, `markets`, `feed SYMBOL`, `record`, `backfill`, `account` (balances+fees), `run --mode shadow|live [--i-understand-live] [--allow-dirty] [--set k=v]`, `backtest ...` (PRD-05), `replay ...` (PRD-05), `report ...` (PRD-05), `logs check`, `db vacuum`, `tui` (PRD-06).

`run` in PRD-01 only starts the data bus, store, heartbeat and the (empty) engine loop — a smoke test that the process stays up for hours.

---

## 10. `scripts/check_account.py` (first thing to run with the challenge key)

Prints, in order:
1. `/v1/account/fee` for watchlist symbols (table).
2. `/v1/account/balances` raw + parsed (TMN, USDT, coins) and computed equity in TMN.
3. MCP: `initialize`, `tools/list` count; call `wallex_credit_active_sub_accounts_list` and `wallex_credit_sub_account_balances_details` → print; **warn loudly if balances differ from REST balances** (order routing may go to the wrong account).
4. Market info for watchlist: tick/step decimals, minQty, minNotional, `isMarketTypeEnable`, and whether banned.
5. `--probe-order` (optional, explicit flag): place a `LIMIT BUY USDTTMN` at `best_bid × 0.9` for `minNotional/price` qty (never fills), fetch it, cancel it; then try a `STOP_LIMIT SELL` far away and cancel; report which types the REST API accepts and the exact error for rejected ones. Record results to `docs/REPORTS/api_probe.md`.

---

## 11. Tests (`tests/`)

- `test_config.py` — load, override, hash stability.
- `test_models.py` — JSON round-trip, Decimal preservation.
- `test_rest_client.py` — respx mocks: markets parse, depth parse, 429 backoff, idempotent order resend, error mapping.
- `test_precision.py` — rounding rules for TMN (0 decimals) and BTCUSDT (2 decimals); qty step; min notional.
- `test_store.py` — write/read books, candles upsert.
- `test_decision_log.py` — schema validation, append-only outcome merge.

---

## 12. Acceptance checklist for PRD-01

- [ ] `make install && make test` green on clean clone (Python 3.11).
- [ ] `python -m wta markets` lists watchlist with precision, flags banned.
- [ ] `python -m wta record` runs ≥ 2 h; `data/wta.sqlite` grows; health lines every 60 s; no uncaught exceptions on injected API failures.
- [ ] `python -m wta feed BTCUSDT` shows data from `ws` or `rest`.
- [ ] `scripts/check_account.py` output saved to `docs/REPORTS/api_probe.md` (secrets masked).
- [ ] `logs/decisions.jsonl` validates with `python -m wta logs check` (even if empty).
- [ ] First tag `v0.1-core` pushed to GitHub.
