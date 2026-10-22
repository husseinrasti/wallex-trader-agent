# Wallex API notes (from developers.wallex.ir, fetched 2026-10-22)

Source: the official docs site (Docusaurus, prerendered pages) — `api.wallex.ir` itself was not
reachable from the development machine (TLS handshake dropped; see `docs/DECISIONS.md`), so these
shapes come from the documentation and the Overview §9. Parsers are tolerant and keep `raw`.
`scripts/check_account.py` re-verifies everything against the live API when reachable.

## Auth & accounts
- Header `x-api-key: <key>` (case-insensitive; we send `X-API-Key`).
- **Credit (challenge) sub-account:** "each credit account has its own wallet, its own API key and
  separate order history". To trade spot on a credit account you send that account's API key **and**
  the header `sub-account-client-id: <clientId>`. We send it on every `/v1/account/*` call when
  `WALLEX_SUB_ACCOUNT_CLIENT_ID` is set.
- `GET /sub-accounts` → `result: [{userId, title, clientId, isActive, type, isSuspend, createdAt}]`
  (types seen: `prop`, `trading_bot_trade`). Path prefix not documented → we try
  `/sub-accounts`, `/v1/sub-accounts`, `/v1/account/sub-accounts`.
- `GET /v1/account/balances-detail` (credit docs) → `result[ASSET] = {symbol, total, freeze, available, stats}`.
- `GET /v1/account/balances` (main wallet) → `result.balances[ASSET] = {asset, faName, fiat, value, locked, is_dust, ...}`.
  We read `value` as the **total** and `free = value − locked`.

## Market data
- `GET /v1/markets` (Overview, verified earlier) → `result.symbols[SYMBOL] = {baseAsset, quoteAsset,
  stepSize, tickSize, minQty, minNotional, isMarketTypeEnable, stats{bidPrice, askPrice, lastPrice, ...}}`
  where `stepSize`/`tickSize` are **decimal counts**.
- `GET /hector/web/v1/markets` (new docs) → `result.markets[] = {symbol, base_asset, quote_asset,
  price, amount_precision, price_precision, is_spot, is_tmn_based, is_usdt_based, ...}` — fallback.
- `GET /v1/depth?symbol=X` → `result.ask[] / result.bid[]` of `{price, quantity, sum}`.
- `GET /depth/all` (docs) or `/v2/depth/all` (Overview) → all books; we try both.
- `GET /v1/trades?symbol=X` → `result.latestTrades[] = {symbol, quantity, price, sum, isBuyOrder, timestamp}`.
  `isBuyOrder=true` means the **maker was the buy order** → aggressor/taker side = SELL.
- `GET /v1/udf/history?symbol=&resolution=&from=&to=` → `{s, t[], o[], h[], l[], c[], v[]}`.
  Resolutions in docs: `1, 15, 60, 240, 480, 720, 1D, 2D, 3D` (no `5`).
- `GET /v1/account/fee` → `result[SYMBOL] = {makerFeeRate, takerFeeRate, recent_days_sum}` (strings).

## Orders (spot)
- `POST /v1/account/orders` body (all strings): `{symbol, type: LIMIT|MARKET|STOP_LIMIT|STOP_MARKET,
  side: BUY|SELL, price, quantity, client_id?, stop_Price?}`. `stop_Price` required only for STOP_*,
  must NOT be sent otherwise. `client_id`: **letters, digits, underscore only**, unique (duplicate ⇒
  order not registered). Success = HTTP **201**.
- Order object (create/get/cancel): `{active, clientOrderId, created_at, executedPercent, executedPrice,
  executedQty, executedSum, fee, fills[{fee, feeAsset, feeCoefficient, isBuyer, makerFeeCoefficient,
  price, quantity, sum, symbol, takerFeeCoefficient, timestamp}], origQty, price, side, status,
  stopPrice, stopPriceCondition, sum, symbol, transactTime, type, updated_at}`.
- `GET /v1/account/orders/{client_id}` → order object (404 = not found).
- `DELETE /v1/account/orders/{client_id}` (path variable; a query-param variant `DELETE /v1/account/orders`
  also exists) → order object. Cancel allowed while not FINISHED.
- `GET /v1/account/openOrders?symbol=&page=&per_page=` → `result` is a mapping of lists (swagger shows
  `additionalProp1..3`) — we flatten every list value; also accept `result.orders` or a bare list.
- `GET /v1/account/orders?from=&to=&page=&per_page=&type=&side=&market=` → same shape (order history).
- `GET /v1/account/trades?symbol=&side=` (Overview) → fills; shape unverified → tolerant parser.
- Status strings seen in docs: `NEW`, `FILLED`, `CANCELED`, `FINISHED` (fully done). Map tolerant.
- Order placement rate limit: **20 requests / 10 s**.
- MARKET orders auto-cancel if size exceeds book or would move price > 2% vs last trade.
- STOP_LIMIT: if `stop < last` at creation, it activates when `last <= stop` (our sell-stop case);
  if `stop > last`, activates when `last >= stop`. Then behaves as LIMIT.

## WebSocket (v2 = Centrifugo; v1 socket.io-style `wss://api.wallex.ir/ws` is deprecated)
- URL `wss://api.wallex.ir/connection/websocket` (no trailing slash). Text JSON frames; a frame may
  hold several newline-separated messages.
- `{"id":1,"connect":{}}` → `{"id":1,"connect":{"client":"…","ping":25,"pong":true}}`.
- `{"id":N,"subscribe":{"channel":"orderbook:BTCTMN@buyDepth"}}` → `{"id":N,"subscribe":{}}` (no snapshot).
- Channels: `orderbook:<SYM>@buyDepth`, `orderbook:<SYM>@sellDepth`, `public:<SYM>@trade`,
  `public:all@price`, `candle:<SYM>@1m`. Max 64 channels per connection.
- Data: `{"push":{"channel":"…","pub":{"data": …}}}`. Depth data = **full snapshot** list
  `[{quantity, price, sum}]` (numbers) — replace, don't merge. Trade data = `{isBuyOrder, quantity,
  price, timestamp(ISO)}`.
- Ping: server sends `{}` every 25 s; reply `{}` within 8 s or it closes with 3012.
- Close codes 3000–3499 → reconnect; ≥ 3500 → do not retry. No history: messages during a
  disconnect are lost (REST poller covers gaps).
