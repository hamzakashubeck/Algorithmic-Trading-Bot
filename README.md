# TradingBot

A small algorithmic trading bot that paper-trades one stock, ETF or crypto pair on
[Alpaca](https://alpaca.markets/). Every few seconds it pulls recent 1-minute candles
from Alpaca's market data API, computes a technical indicator with
[TA-Lib](https://ta-lib.org/), turns it into a BUY / SELL / HOLD signal, and places
orders according to a chosen execution policy.

> This is a cleaned-up version of a project I wrote in 2022. The original three
> scripts are preserved at the [`v1` tag](https://github.com/hamzakashubeck/TradingBot/tree/v1).

No performance claims: nothing about the strategies' returns has been measured
yet. A backtester is the next item on the roadmap.

## Strategies

| Name | Signal |
|---|---|
| `rsi` | RSI(12): BUY below 20, SELL above 80 |
| `bbands` | Bollinger Bands: BUY when price is below the lower band, SELL above the upper band |
| `adx` | ADX above 20: BUY if DI+ > DI-, SELL if DI- > DI+ |
| `stochrsi` | Stochastic RSI: BUY once fast K rebounds back above 20, SELL as soon as K > 80 |
| `stochrsi-rebound` | Stochastic RSI: BUY on the rebound above 20, SELL once K falls back below 80 |

## Execution policies

Strategies only decide *when* to trade. The execution policy decides *how*:

| Name | Behaviour |
|---|---|
| `limit-follow` | Default for stocks. Each cycle, cancels this symbol's unfilled orders; on a new signal, places a limit order just past the ask/bid |
| `scalp` | Stocks only. Fill-or-kill entry at the ask (or bid) plus 0.05% slippage allowance; if it fills, places a take-profit limit order 0.2% away |
| `market-follow` | Default for crypto. Market-buys on a new BUY; closes the position on a new SELL |

## Project structure

```
src/tradingbot/
  config.py          settings and secrets from environment variables / .env
  strategies.py      Signal enum, indicators (TA-Lib) and the strategies; no network code
  execution.py       execution policies: how a signal becomes orders
  alpaca_client.py   everything that talks to Alpaca: market data and orders
  main.py            command-line entry point and the live trading loop
```

Each cycle, `main.py` fetches one market snapshot through `alpaca_client.py`, asks the
strategy for BUY / SELL / HOLD, and passes the signal to the execution policy. Strategies
never touch the network, so a backtester can feed them historical candles unchanged.

## Setup

Requires Python 3.10+ and a free Alpaca account with **paper trading** API keys.

**1. TA-Lib.** The `TA-Lib` Python package wraps a C library. On many platforms
`pip` installs a prebuilt wheel that bundles it. If pip tries to build from source
instead, install the C library (version 0.6 or newer) first:

```bash
brew install ta-lib            # macOS (use `brew upgrade ta-lib` if you have 0.4)
# Linux / Windows: see https://ta-lib.org/install/
```

**2. Install the bot.**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

**3. Configure keys.**

```bash
cp .env.example .env
# edit .env and paste your paper-trading key and secret
```

`ALPACA_BASE_URL` defaults to the paper endpoint. The bot refuses to start against
any other URL unless `ALLOW_LIVE_TRADING=true` is set.

## Running

```bash
tradingbot --strategy stochrsi --symbol SPY
tradingbot --strategy bbands --symbol BTC/USD
tradingbot --strategy stochrsi-rebound --symbol SPY --execution scalp
tradingbot --help
```

Options include `--qty` (units per order, default 1), `--interval` (seconds between
cycles, default 10) and `--seed-qty N` (opt-in: market-buy N units at startup, which
gives `scalp` some shares to sell).

The three original 2022 scripts correspond roughly to:

| 2022 script | Command |
|---|---|
| `_stock_trader.py` | `tradingbot --strategy stochrsi --symbol SPY --execution limit-follow` |
| `alternate_stock_trader.py` | `tradingbot --strategy stochrsi-rebound --symbol SPY --execution scalp` |
| `crypto_trader.py` | `tradingbot --strategy bbands --symbol BTC/USD --execution market-follow` |

Stop with `Ctrl+C`.

## Roadmap

- **Backtester**: replay historical candles through the same `Strategy` interface to
  measure results before running anything live
- **Tests**: unit tests for indicators, strategies (especially the StochRSI state
  machine) and execution policies against a fake broker
- **CI**: GitHub Actions running lint and tests on every push
