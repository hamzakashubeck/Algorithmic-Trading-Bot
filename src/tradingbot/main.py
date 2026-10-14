"""Command-line entry point and the live trading loop."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from tradingbot.alpaca_client import AlpacaBroker, MarketData
from tradingbot.config import ConfigError, load_settings
from tradingbot.execution import EXECUTORS, Executor
from tradingbot.strategies import STRATEGIES, Signal, Strategy

log = logging.getLogger("tradingbot")

CLOSED_RECHECK = 300  # seconds between clock checks while the market is closed


def add_trading_args(parser: argparse.ArgumentParser) -> None:
    """Options shared by the live bot and the backtester."""
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), default="stochrsi")
    parser.add_argument("--symbol", default="SPY", help="stock or ETF ticker, e.g. SPY or AAPL")
    parser.add_argument(
        "--execution",
        choices=sorted(EXECUTORS),
        default="limit-follow",
        help="order policy (default: limit-follow)",
    )
    parser.add_argument("--qty", type=float, default=1, help="units per order (default: 1)")
    parser.add_argument(
        "--seed-qty",
        type=float,
        default=0,
        help="market-buy this many units at startup before trading (default: 0, off)",
    )


def check_trading_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Clean up and sanity-check the shared options."""
    args.symbol = args.symbol.upper()
    if args.qty <= 0 or args.seed_qty < 0:
        parser.error("--qty must be positive and --seed-qty non-negative")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradingbot",
        description="Paper-trade one symbol on Alpaca using a technical-indicator strategy.",
    )
    add_trading_args(parser)
    parser.add_argument(
        "--interval", type=float, default=10, help="seconds between cycles (default: 10)"
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    check_trading_args(parser, args)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return args


def run_cycle(symbol: str, strategy: Strategy, executor: Executor, data: MarketData) -> None:
    """Fetch data, get a signal, act on it. If a request fails, log it and skip this cycle."""
    try:
        snapshot = data.snapshot(symbol)
    except (APIError, requests.RequestException, KeyError) as e:
        # KeyError means Alpaca returned nothing for this symbol
        log.warning("Could not fetch market data for %s: %s", symbol, e)
        return

    if snapshot.candles.empty:
        log.warning("No candles returned for %s; skipping cycle", symbol)
        return

    signal = strategy.evaluate(snapshot)
    if signal is not Signal.HOLD:
        log.info("%s signal for %s at %.2f", signal.name, symbol, snapshot.last_price)

    try:
        executor.on_signal(signal, snapshot)
    except (APIError, requests.RequestException) as e:
        log.warning("Order handling failed: %s", e)


def wait_for_open_market(broker: AlpacaBroker, retry_delay: float) -> datetime:
    """Sleep until the market is open, then return when it next closes.

    Outside market hours the candles don't change, so the signal is stuck and orders
    would just sit in a queue. Easier to not trade then at all.
    """
    announced = False
    while True:
        try:
            clock = broker.clock()
        except (APIError, requests.RequestException) as e:
            log.warning("Could not check the market clock: %s", e)
            time.sleep(retry_delay)
            continue
        if clock.is_open:
            if announced:
                log.info("Market is open")
            return clock.next_close
        if not announced:
            log.info("Market is closed; waiting for the open at %s", clock.next_open)
            announced = True
        until_open = (clock.next_open - clock.timestamp).total_seconds()
        time.sleep(min(max(until_open, 1.0), CLOSED_RECHECK))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = load_settings()
    except ConfigError as e:
        log.error("%s", e)
        return 2

    broker = AlpacaBroker(settings)
    data = MarketData(settings)
    strategy = STRATEGIES[args.strategy]()
    executor = EXECUTORS[args.execution](broker, args.symbol, args.qty)

    log.info(
        "Starting: strategy=%s symbol=%s execution=%s qty=%s interval=%ss %s",
        args.strategy, args.symbol, args.execution, args.qty, args.interval,
        "PAPER" if settings.paper else "LIVE",
    )

    try:
        next_close = wait_for_open_market(broker, args.interval)

        if args.seed_qty:
            log.warning("--seed-qty: market-buying %s %s", args.seed_qty, args.symbol)
            try:
                broker.market_order(args.symbol, args.seed_qty, OrderSide.BUY)
            except APIError as e:
                log.error("Seed order failed: %s", e)
                return 1

        while True:
            # no need to ask for the clock again until the market closes
            if datetime.now(timezone.utc) >= next_close:
                next_close = wait_for_open_market(broker, args.interval)
            started = time.monotonic()
            run_cycle(args.symbol, strategy, executor, data)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
