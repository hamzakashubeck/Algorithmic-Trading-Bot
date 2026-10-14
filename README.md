# TradingBot

A small algorithmic trading bot that paper-trades one stock or ETF on
[Alpaca](https://alpaca.markets/). Every few seconds it pulls recent 1-minute candles
from Alpaca's market data API, computes a technical indicator with
[TA-Lib](https://ta-lib.org/), turns it into a BUY / SELL / HOLD signal, and places
orders according to a chosen execution policy.

> This is a cleaned-up version of a project I wrote in 2022. The original three
> scripts are preserved at the [`v1` tag](https://github.com/hamzakashubeck/TradingBot/tree/v1).

It also has a backtester that runs the same strategies over historical data. I'm
not claiming any of these strategies make money; the backtester is there to find out
how they actually behave.

## Strategies

| Name | Signal |
|---|---|
| `rsi` | RSI(12): BUY below 20, SELL above 80 |
| `bbands` | Bollinger Bands: BUY when price is below the lower band, SELL above the upper band |
| `adx` | ADX above 20: BUY if DI+ > DI-, SELL if DI- > DI+ |
| `stochrsi` | Stochastic RSI: BUY once fast K rebounds back above 20, SELL as soon as K > 80 |
| `stochrsi-rebound` | Stochastic RSI: BUY on the rebound above 20, SELL once K falls back below 80 |

## Execution policies

Strategies only decide *when* to trade. The execution policy decides *how*. All of
them are long-only and position-aware: a BUY only opens a position when the bot holds
nothing, and a SELL exits whatever it holds.

| Name | Behaviour |
|---|---|
| `limit-follow` | Default. Each cycle, cancels this symbol's unfilled orders, then buys (or sells everything) with a limit order just past the ask/bid |
| `scalp` | Fill-or-kill buy at the ask plus a 0.05% slippage allowance; if it fills, places a take-profit sell 0.2% higher. A SELL signal cancels that and exits at market |
| `market-follow` | Market-buys on BUY; closes the position on SELL |

## Project structure

```
src/tradingbot/
  config.py          settings and secrets from environment variables / .env
  strategies.py      Signal enum, indicators (TA-Lib) and the strategies; no network code
  execution.py       execution policies: how a signal becomes orders
  alpaca_client.py   everything that talks to Alpaca: market data and orders
  main.py            command-line entry point and the live trading loop
  backtest.py        simulated broker, backtest loop and the backtest command
```

Each cycle, `main.py` fetches one market snapshot through `alpaca_client.py`, asks the
strategy for BUY / SELL / HOLD, and passes the signal to the execution policy. Strategies
never touch the network, and execution policies talk to a small `Broker` interface. That
lets `backtest.py` run the exact same strategy and execution code against historical
candles, with a simulated broker in place of Alpaca.

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
tradingbot --strategy bbands --symbol AAPL --execution market-follow
tradingbot --strategy stochrsi-rebound --symbol SPY --execution scalp
tradingbot --help
```

It only trades during regular market hours (9:30–16:00 New York). If you start it
outside those hours, it just waits for the open.

Other options: `--qty` (shares per order, default 1), `--interval` (seconds between
checks, default 10) and `--seed-qty N` (buy N shares at startup; off by default).

The original 2022 scripts correspond roughly to:

| 2022 script | Command |
|---|---|
| `_stock_trader.py` | `tradingbot --strategy stochrsi --symbol SPY --execution limit-follow` |
| `alternate_stock_trader.py` | `tradingbot --strategy stochrsi-rebound --symbol SPY --execution scalp` |

Stop with `Ctrl+C`.

## Backtesting

```bash
tradingbot-backtest --strategy stochrsi --symbol SPY --start 2026-09-01 --end 2026-09-30
tradingbot-backtest --strategy bbands --symbol AAPL --start 2026-10-01 --fills-csv fills.csv
```

It takes the same options as the live bot, plus `--start`/`--end`, `--cash` (default
100,000), `--spread-bps` (assumed bid/ask spread, default 2), `--fee-bps` (default 0)
and `--fills-csv` to save every trade to a file. The report shows the P&L, what you'd
have made just holding the stock, max drawdown, number of trades, and how much of the
time it was in a position.

Some notes on how it works:

- It runs the same strategy and executor code as the live bot; only the broker is
  swapped for a simulated one.
- Each decision is made at the open of a 1-minute bar using only the candles before
  it, so it can't peek ahead.
- Orders fill at the open plus half the assumed spread. Limit orders only fill if the
  price trades through the limit, not if it just touches it.
- Like the live bot, it only trades regular market hours.

It doesn't model partial fills or latency, so treat the numbers as rough.

## Roadmap

- **Tests**: unit tests for indicators, strategies (especially the StochRSI state
  machine) and execution policies against a fake broker
- **CI**: GitHub Actions running lint and tests on every push
