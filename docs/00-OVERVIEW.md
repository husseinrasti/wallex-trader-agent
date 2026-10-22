# Wallex Trader Agent — Project Overview (shared context for all PRDs)

**Status:** v1.0 — authoritative. Every PRD in this folder assumes this document.
**Read order for a coding agent:** `00-OVERVIEW.md` → `KICKOFF-PROMPT.md` → `PRD-01` … `PRD-06`.

---

## 1. What we are building

An autonomous, explainable, risk-first **spot trading agent** for the Wallex exchange, built for the Starcoach Agentic-AI hackathon "Wallex Algorithmic Trading Challenge". It must:

1. Ingest Wallex market data (REST + WebSocket), compute features, and trade **without any manual intervention**.
2. Use AI in a **real, measurable, logged** way (LLM "Regime Analyst" + "Post-trade Reviewer" in a slow loop; deterministic strategies in a fast loop).
3. Enforce hard, deterministic **risk rules** that AI cannot override.
4. Produce a **decision log** for every trade (signals seen, AI reasoning, risk checks, order result, outcome).
5. Run the **same code path** in `backtest`, `shadow` (paper), `live` and `replay` modes.
6. Be **re-runnable by judges** from a clean clone with one command.
7. End the challenge with **100% of equity in TMN** (realized P&L only counts).

Non-goals: a GUI (explicitly not scored), leverage/margin (not allowed), long-term investing, ML model training in phase 1.

---

## 2. Challenge facts that constrain the design

| Fact | Value | Design consequence |
|---|---|---|
| Capital | 5,000,000 TMN (≈ 18.7 USDT at ~266,700 TMN/USDT) | Position sizes are tiny; we never move the market; always fill at best level. Returns are measured in **%**, so size is irrelevant except min order sizes. |
| Duration | 3 days (72h), already running | Ship an MVP to live within the first day; activity is scored across all 3 days. |
| Scoring (300 pts) | Financial 150 (profit % rank 90, drawdown control 30, activity 15, stability 15); Technical 150 (strategy logic 40, real AI usage 35, risk mgmt 30, re-runnability 25, backtest+shadow 20) | A **negative** final return scores 0 on the 90-pt item. 210 of 300 points are engineering-controlled. Priority: **don't lose, trade regularly, be auditable.** |
| Final valuation | Net account value **in TMN** at official end; unrealized coin value is *not* accepted as performance | Mandatory flatten-to-TMN routine before deadline. |
| Fees (from `GET /v1/account/fee`) | TMN pairs: maker 0.25% / taker 0.30%. USDT pairs: maker 0.10% / taker 0.13%. | **Trade primarily in USDT pairs** (2.5× cheaper). Round-trip cost: USDT pair 0.20–0.26%; TMN pair 0.50–0.60%. |
| Min order | TMN pairs: `minNotional` 50,000 TMN. USDT pairs: `minNotional` 1 USDT. | Granularity is not a problem. |
| Order types | LIMIT, MARKET, STOP_LIMIT, STOP_MARKET (per Wallex MCP tool schema; REST docs only show LIMIT — verify at runtime) | Use **server-side STOP_LIMIT** for stop-losses so they survive a bot crash. Some markets have `isMarketTypeEnable: false` (e.g. SOLTMN) → MARKET not allowed there. |
| Allowed markets | All Wallex markets **except** a published ban list (all illiquid alts). Allowed majors include: USDT, BTC, ETH, SOL, XRP, DOGE, ADA, TRX, LTC, BNB, SHIB, PEPE, LINK, DOT, AVAX, ATOM, UNI, AAVE, SUI, HBAR, XLM, BCH, ETC, FIL, ARB, POL, SAND, MANA, CHZ, GALA, IMX, AXS, ENA, HYPE, WLD, 1INCH, CAKE, SEI, FLOKI, PENGU, PUMP, USDC, PAXG, XAUT and their TMN and USDT pairs. | `configs/markets.yaml` holds the ban list verbatim; the engine refuses to trade a banned symbol. |
| Prohibited | manual trades, self-trading, wash trading, coordinated trades, trading banned markets | Our own buy/sell orders must never cross each other in the same market; every order carries a `client_id` we generated; the decision log proves algorithmic origin. |
| Deliverables | GitHub repo with the exact code that traded; an `.md` doc (architecture, AI usage, buy/sell conditions, risk mgmt, how to re-run); decision logs; ~5-min demo video | Every decision record includes `git_commit` + `config_hash`. Live runs only from tagged commits. |

### 2.1 Observed market microstructure (snapshot 2026-10-22, used for defaults)

| Market | bid | ask | spread | note |
|---|---|---|---|---|
| USDTTMN | 266,720 | 266,758 | 0.014% | deepest market; moved +2.44% in 24h (dollar jump) |
| BTCTMN | 22,075,699,081 | 22,106,074,058 | 0.138% | |
| BTCUSDT | 82,845.00 | 82,909.16 | 0.077% | `tickSize` 2 decimals |
| ETHTMN | 650,660,181 | 650,859,142 | 0.031% | |
| SOLTMN | 28,000,019 | 28,105,486 | 0.376% | `isMarketTypeEnable: false` |

Implied BTCUSDT from TMN books = 22,090,886,570 / 266,739 = 82,818.6 vs real BTCUSDT mid 82,877 → deviation −0.07% (inside spread). Cross-book opportunities appear mainly during fast moves; frequency must be measured from recorded data (PRD-05 survey).

---

## 3. Architecture (decided)

```
                ┌──────────────────────────── SLOW LOOP (every 5 min) ───────────────────────────┐
                │  Regime Analyst (LLM via OpenRouter, tool-calling over Wallex MCP + internal)   │
                │  → MarketPlan JSON (regime per market, enabled strategies, margins, size mult,  │
                │    USDT target share, rationale)  → validated + clamped → stored + cached        │
                │  Post-trade Reviewer (LLM, hourly) → journal + bounded param suggestions         │
                └───────────────────────────────────────┬──────────────────────────────────────────┘
                                                        │ MarketPlan
┌──────────────┐   book/trades/candles   ┌──────────────▼─────────────┐   intents   ┌──────────────┐
│ Wallex REST  │────────────────────────▶│  FAST LOOP (every 2 s)      │────────────▶│ RiskManager  │
│ + WebSocket  │                         │  FeatureEngine → Strategies │             │ (hard rules) │
└──────────────┘                         │  S1 CrossBook, S2 USDTAlloc,│◀────────────│ approve/veto │
       ▲                                 │  S3 RegimeMomentum          │  decisions  └──────┬───────┘
       │ orders / fills                  └──────────────┬─────────────┘                    │
┌──────┴───────┐                                        │ approved intents                 │
│ Exchange     │◀───────────────────────────────────────┴──────────────────────────────────┘
│ Live|Paper|  │        OrderManager (idempotent client_id, reprice/cancel, server-side stops,
│ Backtest     │        fills → Positions → realized PnL)  → DecisionLog (JSONL, one record/decision)
└──────────────┘
   Scheduler: reconcile every 5 min · heartbeat 60 s · FLATTEN at T-2h/T-30min · kill-switch file
```

**Three layers, strict separation:**
- **Fast loop (deterministic, no LLM):** data → features → strategy intents → risk → execution → log.
- **Slow loop (LLM, agentic):** produces a *plan envelope*; it never places orders directly.
- **Guardian (deterministic):** RiskManager + kill-switch + flatten scheduler; cannot be overridden by any AI output.

**One code path, four modes:** `live`, `shadow` (PaperExchange fills against the live book), `backtest` (BacktestExchange over recorded/historical data), `replay` (re-executes recorded inputs + cached LLM responses; must reproduce `decisions.jsonl` byte-for-byte on the deterministic fields).

---

## 4. Strategy set (decided)

| ID | Market(s) | Logic (one line) | Role |
|---|---|---|---|
| **S1 CrossBook** | X/TMN vs X/USDT for X ∈ {BTC, ETH, SOL, XRP, DOGE, …} | Compare the TMN book's implied USDT price (`XTMN / USDTTMN`) with the real X/USDT book; when one book is cheaper than the other by more than fees + margin, buy in the cheap book and sell in the expensive one (lock-in), or hold briefly for reversion (bounded). Direction-neutral. | Core edge |
| **S2 USDTAlloc** | USDTTMN | Maintain a target USDT share of equity (bounds 25–70%) chosen from the dollar regime (rules + LLM); rebalance only when the gap exceeds a threshold. Turns unavoidable TMN/USDT exposure into an explicit, logged decision and supplies both currency pools S1 needs. | Currency risk mgmt + activity |
| **S3 RegimeMomentum** | BTCUSDT, ETHUSDT, SOLUSDT | 15-min breakout with volume confirmation, enabled only when the Analyst says `trend_up` with confidence ≥ 0.6; server-side STOP_LIMIT; risk-based sizing; ≤ 20% of equity. | Bounded upside |
| **FLATTEN** | all → TMN | T−2h: convert everything to TMN with maker limits; T−30min: MARKET for leftovers; then stop trading. | Mandatory |

Details, formulas and parameters: `PRD-03-strategies.md`.

---

## 5. Risk rules (decided; enforced in `RiskManager`, see PRD-02)

| Rule | Default |
|---|---|
| Max notional per order | 25% of equity (and ≤ 80% of quantity available at the best level) |
| Max non-TMN exposure | 60% of equity (USDT counts as exposure for this rule? **No** — USDT is governed by S2 bounds; coins ≤ 40%) |
| Max concurrent coin positions | 3 (S1 ≤ 2, S3 ≤ 1) |
| Stop-loss per position | S1 revert-mode: 1.0%; S3: 1.5×ATR (server STOP_LIMIT) |
| Time stop | S1: 20 min; S3: 4 h |
| Daily loss limit | −2.5% of day-start equity → no new entries until next UTC day |
| Max drawdown kill-switch | −5.0% from equity peak → flatten + halt; manual reset only |
| Open orders cap | 8 |
| Trade-rate cap | 10 order placements per rolling hour, 60 per day |
| Fee budget | 2.0% of initial equity over the challenge |
| Anomaly guard | Price source disagreement > 5%, 5 consecutive API errors, or stale data > 30 s → pause entries |
| Deadline guard | After `flatten_start` no new entries; after `flatten_final` only TMN-converting orders |

---

## 6. AI usage (decided; see PRD-04)

- **Regime Analyst** (every 5 min): LLM with tool-calling. Tools: Wallex MCP (`wallex_basic_get_markets`, `wallex_basic_get_market_depth`, `wallex_basic_candle_history`, `wallex_basic_get_latest_trades`, `wallex_basic_get_account_balances`, `wallex_spot_get_open_order`) + internal tools (`get_features`, `get_pnl_summary`, `get_recent_decisions`, `get_health`). Output: `MarketPlan` (strict JSON schema). Validated, clamped to bounds, stored, cached by prompt hash.
- **Post-trade Reviewer** (hourly): reads the last hour of decisions and outcomes; writes a journal entry; proposes parameter changes **only within pre-declared bounds**; accepted changes become a new versioned config (new `config_hash`).
- **Reproducibility:** every LLM request/response is cached by `sha256(model + messages + tools)`. `replay` mode never calls the network.
- **Ablation:** backtests run with `ai.enabled=false` (rules-only) vs `true`, reported side by side.
- **LLM provider:** OpenRouter (OpenAI-compatible API) primary; Google Gemini API fallback. Both configured via env vars.

---

## 7. Repository layout (fixed; all PRDs reference these paths)

```
wallex-trader-agent/
├─ README.md                      # how to run (judges start here)
├─ pyproject.toml                 # package "wta", python >= 3.11
├─ .env.example                   # all env vars, no secrets
├─ .gitignore                     # .env, data/, logs/, llm_cache/ (except deliverables/)
├─ Makefile                       # make install|test|record|survey|shadow|live|backtest|replay|report|flatten
├─ Dockerfile · docker-compose.yml
├─ configs/
│  ├─ default.yaml                # all parameters (versioned; hashed at runtime)
│  ├─ markets.yaml                # watchlist + verbatim ban list
│  └─ llm.yaml                    # models, temperatures, bounds for reviewer edits
├─ src/wta/
│  ├─ cli.py                      # `python -m wta <command>`
│  ├─ config.py                   # pydantic settings; config_hash
│  ├─ models.py                   # all pydantic data models (single source of truth)
│  ├─ exchange/   base.py · wallex_rest.py · wallex_ws.py · live.py · paper.py · backtest.py
│  ├─ data/       store.py (SQLite) · collector.py · candles.py
│  ├─ features/   engine.py
│  ├─ strategies/ base.py · s1_crossbook.py · s2_usdtalloc.py · s3_momentum.py
│  ├─ risk/       manager.py · flatten.py
│  ├─ execution/  order_manager.py · positions.py · reconcile.py
│  ├─ ai/         llm_client.py · cache.py · mcp_client.py · analyst.py · reviewer.py · tools.py · prompts/
│  ├─ logging_/   decision_log.py · health_log.py
│  ├─ engine.py                   # orchestrator (fast loop, slow loop, scheduler)
│  ├─ dashboard/  tui.py
│  └─ utils/      timeutil.py · decimalutil.py · gitinfo.py · telegram.py
├─ backtest/      runner.py · fills.py · metrics.py · report.py
├─ scripts/       record_market.py · survey.py · check_account.py · flatten_now.py · replay_check.py
├─ tests/
├─ data/          (gitignored) recorded market data, SQLite db
├─ logs/          (gitignored) decisions.jsonl, orders.jsonl, health.jsonl, llm_cache/
├─ deliverables/  (committed at the end) decision logs, reports, replay proof, video link
└─ docs/          this folder + ARCHITECTURE.md, STRATEGY.md, RISK.md, REPRODUCE.md, REPORTS/
```

---

## 8. Conventions (apply to all PRDs)

- **Language/stack:** Python ≥ 3.11, `asyncio`, `httpx`, `pydantic` v2, `python-socketio` (+ REST polling fallback), `pyyaml`, `rich`, `pandas`, `numpy`, `matplotlib` (reports), `pytest`, `ruff`. No LangChain/CrewAI; the agent loop is hand-written (~200 lines) for auditability.
- **Money math:** `decimal.Decimal` everywhere for prices/quantities; never float for order fields. Price decimals = market `tickSize` (an integer count of decimals: TMN pairs 0, BTCUSDT 2). Quantity decimals = `stepSize`. Round quantities **down**; buy prices down, sell prices up when rounding to tick.
- **Time:** UTC everywhere; ISO-8601 strings in logs; `time.time()` floats in data records.
- **IDs:** `client_id = f"{strategy}-{symbol}-{int(ts_ms)}-{nonce4}"` (≤ 64 chars). Every order we send has one; orders without our prefix found on the account are flagged as **foreign** in logs.
- **Config:** everything tunable lives in `configs/*.yaml`; `config_hash = sha256(canonical_json(config))[:12]` is logged with every decision.
- **Git:** `main` only + tags `live-YYYYMMDD-HHMM` for every live run; the live process logs `git rev-parse HEAD` and refuses to start if the working tree is dirty (override flag `--allow-dirty` for dev only).
- **Logging:** structured JSONL; no secrets ever logged (API keys masked).
- **Safety defaults:** `mode` defaults to `shadow`; `live` requires `--i-understand-live` flag and `WTA_LIVE=1` env.
- **Tests:** RiskManager, OrderManager, FeatureEngine, strategies, decision-log schema, replay determinism — all unit-tested; `make test` must pass before any live tag.

---

## 9. Wallex API reference (what we have verified)

Base URL `https://api.wallex.ir`. Auth header `X-API-Key: <key>`. All JSON responses are `{ "result": ..., "message": "...", "success": true }`.

| Purpose | Endpoint | Notes |
|---|---|---|
| Markets + precision + stats | `GET /v1/markets` | `result.symbols[SYMBOL]` → `baseAsset, quoteAsset, stepSize (qty decimals), tickSize (price decimals), minQty, minNotional, isMarketTypeEnable, stats{bidPrice, askPrice, lastPrice, 24h_volume, 24h_ch, …}` |
| Order book | `GET /v1/depth?symbol=BTCTMN` | `result.ask[] / result.bid[]` of `{price, quantity, sum}`; ask ascending, bid descending |
| All order books | `GET /v2/depth/all` | one call for all markets (use for S1 scan if rate-limited) |
| Recent trades | `GET /v1/trades?symbol=…` | inspect shape at runtime; store raw |
| Candles | `GET /v1/udf/history?symbol=&resolution=1|5|15|60|D&from=&to=` | TradingView UDF: `{s:"ok", t[], o[], h[], l[], c[], v[]}` |
| Fees | `GET /v1/account/fee` | `result[SYMBOL].makerFeeRate/takerFeeRate` (strings) |
| Balances | `GET /v1/account/balances` | inspect shape; expect per-asset `value`, `locked` |
| Place order | `POST /v1/account/orders` | body `{symbol, type: LIMIT|MARKET|STOP_LIMIT|STOP_MARKET, side: BUY|SELL, price, quantity, client_id, stop_Price?}`; field name `stop_Price` as in MCP schema — verify |
| Get order | `GET /v1/account/orders/{clientOrderId}` | |
| Cancel order | `DELETE /v1/account/orders?clientOrderId=…` | |
| Open orders | `GET /v1/account/openOrders?symbol=…` | |
| My trades | `GET /v1/account/trades?symbol=&side=` | fills with fee |
| WebSocket | socket.io on `https://api.wallex.ir`, emit `subscribe` `{channel: "BTCTMN@buyDepth"}` / `@sellDepth` / `@trade` / `@marketCap`, listen to event `Broadcaster` | server is socket.io v2-era (client v2.5 recommended by docs) → use `python-socketio` 4.x / `python-engineio` 3.x in an isolated task; if unstable, REST polling at 1–2 s is the fallback and must work standalone |
| MCP | `POST https://api.wallex.ir/mcp` (streamable HTTP, JSON-RPC 2.0) | 50+ tools; auth via `X-API-Key` header (verify). Spot tools: `wallex_spot_get_markets`, `wallex_spot_get_depth`, `wallex_basic_get_market_depth`, `wallex_basic_candle_history`, `wallex_basic_get_latest_trades`, `wallex_basic_get_account_balances`, `wallex_basic_get_account_fees`, `wallex_spot_get_open_order`, `wallex_spot_get_orders`, `wallex_spot_get_orders_client`, `wallex_spot_post_account_orders`, `wallex_spot_delete_orders_client`. Credit tools `wallex_credit_*` exist → the challenge account is a **credit sub-account**. |
| Rate limits | undocumented; HTTP 429 | token bucket default 6 req/s, exponential backoff on 429 (1s → 2 → 4 → 8, max 60s) |

**Open verification items (do in `scripts/check_account.py`, PRD-01):** exact balances shape; whether the API key routes orders to the challenge sub-account (compare `wallex_credit_sub_account_balances_details` vs `/v1/account/balances`); MARKET/STOP_LIMIT acceptance on REST; `stop_Price` field name; trades payload shape; socket.io handshake.

---

## 10. Environment variables (`.env.example`)

```
WALLEX_API_KEY=
WALLEX_BASE_URL=https://api.wallex.ir
WALLEX_MCP_URL=https://api.wallex.ir/mcp
OPENROUTER_API_KEY=
OPENROUTER_MODEL_FAST=            # e.g. a fast/cheap model for the 5-min analyst
OPENROUTER_MODEL_STRONG=          # for the hourly reviewer
GEMINI_API_KEY=                   # fallback provider
GEMINI_MODEL=
TELEGRAM_BOT_TOKEN=               # optional alerts
TELEGRAM_CHAT_ID=
WTA_MODE=shadow                   # shadow|live|backtest|replay
WTA_LIVE=0                        # must be 1 for live
CHALLENGE_START_UTC=              # ISO-8601
CHALLENGE_END_UTC=                # ISO-8601 (official end)
INITIAL_EQUITY_TMN=5000000
```

---

## 11. Delivery priority (solo developer, hackathon already running)

1. **PRD-01** skeleton + REST client + recorder + store + decision log + `check_account` (hours 0–6)
2. **PRD-02** RiskManager + OrderManager + positions + reconcile + flatten (hours 6–12)
3. **PRD-03** features + S1 + S2 (S3 later) → shadow 3–4 h → **live at half size** (by hour ~18)
4. **PRD-04** Analyst + cache + replay (hours 18–30), Reviewer after
5. **PRD-05** backtest + survey + ablation + reports (hours 30–44)
6. **PRD-03** S3, **PRD-06** TUI, docs, video, submission (hours 44–66); flatten at T−2h.

Definition of "MVP live": S1+S2 trading in `live` with RiskManager, server-side stops, decision log, reconcile, kill-switch, flatten scheduler, heartbeat/Telegram. Everything else is additive.
