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
