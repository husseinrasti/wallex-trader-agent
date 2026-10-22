# wallex-trader-agent

Autonomous, explainable, risk-first spot trading agent for the Wallex exchange
(Starcoach "Wallex Algorithmic Trading Challenge"). AI plans, deterministic rules trade,
a deterministic risk manager guards every order. Same code path for backtest, shadow, live and replay.

Status: under construction — see `docs/PROGRESS.md`.

## Quick start
```bash
make install && make test
cp .env.example .env   # fill WALLEX_API_KEY etc.
make record            # market data recorder
make shadow            # paper trading against the live book
```
