# Decisions

One line each: date — decision — why.

- 2026-10-22 — Use Python 3.12 locally (uv), 3.11 in Docker — both satisfy `>=3.11`; 3.14 system Python is too new for some wheels.
- 2026-10-22 — WebSocket via plain `websockets` speaking Centrifugo v2 (`/connection/websocket`) instead of `python-socketio 4.x` — official docs mark the socket.io-style v1 endpoint deprecated; v2 is plain JSON and needs no pinned legacy libs.
- 2026-10-22 — `client_id` uses `_` separators (`S1_BTCUSDT_<ts_ms>_<nonce4>`) — Wallex only accepts letters/digits/underscore in `client_id`.
- 2026-10-22 — Cancel via `DELETE /v1/account/orders/{client_id}` — documented path-variable form (Overview listed a query form; both exist).
- 2026-10-22 — Send `sub-account-client-id` header on account endpoints when `WALLEX_SUB_ACCOUNT_CLIENT_ID` is set — Wallex docs require it for spot trading on a credit (challenge) sub-account.
- 2026-10-22 — Balances: `value` = total, `free = value − locked` (main wallet); `available/freeze/total` (balances-detail) — conservative reading of the documented fields.
- 2026-10-22 — `Trade.side` = taker side (`isBuyOrder=true` ⇒ SELL) — docs define `isBuyOrder` as "maker was the buy order".
- 2026-10-22 — Build offline against documented payloads; the recorder retries forever with backoff — `api.wallex.ir` drops TLS from this machine (likely IP filtering); user chose "build offline for now".
- 2026-10-22 — Reviewer-editable `ai.slow_loop_interval_s` mapped to `engine.slow_loop_interval_s` — the parameter lives under `engine` in default.yaml.
- 2026-10-22 — Stdlib `sqlite3` (WAL) with batched writes instead of `aiosqlite` — fewer dependencies; writes are small and batched every 1 s.
- 2026-10-22 — The assistant does not place exchange orders itself (not even the far-from-market `--probe-order`) — operator safety policy; the script is ready and the user runs it.
- 2026-10-22 — `video/` is gitignored — the pre-existing 53 MB video.mp4 exceeds GitHub's 50 MB warning size; the demo video link goes into deliverables/VIDEO.md instead.
- 2026-10-22 — S3 target is a software target (PositionManager) instead of a resting target LIMIT — a resting STOP_LIMIT SELL most likely locks the coins, so a second SELL for the same qty would be rejected for balance; one SELL resting at a time also keeps self-cross checks trivial.
- 2026-10-22 — Live guard lives in `wta/safety.py` and runs in the CLI, `run_engine` and `Engine.run`; `WTA_LIVE=1` must be in the process environment (a `.env` value does not arm live); the clean-tree check counts untracked files — live can only start deliberately from a committed tree.
- 2026-10-22 — WS close code >= 3500 stops the WebSocket feed for the life of the process (Centrifugo "do not retry"); REST polling covers the data.
- 2026-10-22 — With a sub-account configured, balances come from `/v1/account/balances-detail` only; the main-wallet fallback is used only on 404/405 — never silently value the wrong wallet.
- 2026-10-22 — One-shot CLI commands and `check_account.py` use `max_retries=0` and short timeouts — an outage fails in seconds instead of minutes of backoff.
- 2026-10-22 — MCP client is read-only by construction: default allowlist = the 8 read-only tools; state-changing names are refused even if allowlisted.
- 2026-10-22 — `check_account.py --probe-order` refuses to run when routing warnings exist (sub-account id missing) unless `--probe-main-wallet`; STOP_LIMIT probe limit price sits on the non-filling side.
- 2026-10-22 — S1 coins ADA/TRX/LTC/LINK/AVAX added to the watchlist (21 symbols, 63 of 64 WS channels) — S1 trades them but had no books; snapshot rows for them are estimates.
- 2026-10-22 — Engine state DB per mode and feed (`data/state_{mode}.sqlite`, `_synthetic` suffix for the synthetic feed) — shadow, live and synthetic exercises can never share kv/positions/orders.
- 2026-10-22 — A lock-in leg 1 is never re-priced or chased; an unfilled leg 2 is given up after `limit_reprice_after_s` and S1 re-decides (fresh loss-limit check) — chasing an edge-less market lost money in a synthetic exercise.
- 2026-10-22 — `get_features` returns a curated view (short keys, 6 significant digits, None dropped, S1 edges net of fees) — the full snapshot exceeded the 4 kB tool cap and was truncated to unparsable JSON.
- 2026-10-22 — With an LLM key configured the offline rules provider is never used; provider failure means plan=None (rules-only), visible as `llm_ok=false` — a fallback must never pretend to be an LLM.
- 2026-10-22 — AI budgets count analyst cycles / reviews (PRD-04 sizes them per cycle); the LLMClient per-request count is only a ceiling.
- 2026-10-22 — A reviewer config reload waits for the next tick boundary; runtime overlays live per account (`configs/runtime/<account>/latest.yaml`), are re-applied at startup, never in the ai-off ablation, and never override operator `--set`/`--overlay` keys.
- 2026-10-22 — Only one engine per Telegram bot polls commands (`alerts.telegram_commands_enabled`); commands are operational only (status/pause/resume/kill) — no trading through Telegram.
- 2026-10-23 — Analyst prompt asks the model to close `regimes` and check that the JSON parses — 5 of 15 first answers in the acceptance run had an unclosed `regimes` object (each fixed by the retry).
- 2026-10-23 — `wta run/plan --ai offline` and `demo.sh --feed live --ai offline` — a network that reaches Wallex (Iran, VPN off) often cannot reach OpenRouter; the labelled offline rules provider then keeps the AI layer visible without pretending to be an LLM. `make smoke-real` checks every real Wallex path read-only in one command.
- 2026-10-23 — WS payloads are unwrapped from `{"message": ...}` before parsing — the live endpoint wraps them (docs show the bare payload); both forms accepted.
- 2026-10-23 — `ai.repair_plan_nesting` (default false in code, true in `configs/default.yaml`): after the one fix-the-JSON retry, top-level plan keys written inside `regimes` are lifted back (values untouched, still clamped, logged as `repaired`) — gpt-5.4-mini made this slip twice in a row on the real feed.
- 2026-10-23 — A config field added after recorded runs is left out of `config_hash` while it holds its legacy value (`HASH_LEGACY_DEFAULTS`) — the shipped 4-h run keeps hash `481cc6dc3ce2` and still replays 40/40.
- 2026-10-23 — `WALLEX_SUB_ACCOUNT_CLIENT_ID` set in `.env` from `/sub-accounts` (the only active credit account, verified read-only to hold 5,000,000 TMN) — without it account calls use the main wallet.
