# Wallex API probe

- generated: 2026-10-22T10:43:05.764Z (UTC)
- git commit: 3d71ac98152761bd49ef93dd1a82f7f1cb8d1a40 (dirty)
- config hash: 5751839f7e2d
- REST base URL: https://api.wallex.ir
- MCP URL: https://api.wallex.ir/mcp (auth header `X-API-Key`)
- API key: set (never printed)
- sub-account-client-id: not set
- order probe: not requested (read-only run)
- REST reachable: **NO** (GET /v1/markets: ConnectTimeout)

## 1. Fee rates (/v1/account/fee)

**skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)**


## 2. Balances

**skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)**


## 3. MCP and sub-account routing

### MCP

**MCP: initialize: MCP request timed out (ConnectTimeout)**

### REST `/sub-accounts`

**skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)**

### Routing checks

- no credit sub-accounts listed and no sub-account-client-id configured.
- MCP vs REST balances: not compared (MCP balances unavailable).

## 4. Market rules for the watchlist (/v1/markets)

**market rules unavailable (/v1/markets failed, see the header); banned flags come from the config**

| symbol | tick dec | step dec | minQty | minNotional | isMarketTypeEnable | banned |
|---|---|---|---|---|---|---|
| USDTTMN | - | - | - | - | - | no |
| BTCTMN | - | - | - | - | - | no |
| BTCUSDT | - | - | - | - | - | no |
| ETHTMN | - | - | - | - | - | no |
| ETHUSDT | - | - | - | - | - | no |
| SOLTMN | - | - | - | - | - | no |
| SOLUSDT | - | - | - | - | - | no |
| XRPTMN | - | - | - | - | - | no |
| XRPUSDT | - | - | - | - | - | no |
| DOGETMN | - | - | - | - | - | no |
| DOGEUSDT | - | - | - | - | - | no |


## 5. Order-type probe (--probe-order)

Not run. Pass `--probe-order` to place and cancel two far-from-market USDTTMN orders.

## Summary

- warnings: 0
- problems: 5
  - skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)
  - skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)
  - MCP: initialize: MCP request timed out (ConnectTimeout)
  - skipped: REST API unreachable (GET /v1/markets: ConnectTimeout)
  - market rules unavailable (/v1/markets failed, see the header); banned flags come from the config
