"""Command-line entry point and the live trading loop."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from tradingbot.alpaca_client import Broker, MarketData, is_crypto
from tradingbot.config import ConfigError, load_settings
from tradingbot.execution import EXECUTORS, Executor
from tradingbot.strategies import STRATEGIES, Signal, Strategy

log = logging.getLogger("tradingbot")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradingbot",
        description="Paper-trade one symbol on Alpaca using a technical-indicator strategy.",
    )
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), default="stochrsi")
    parser.add_argument("--symbol", default="SPY", help="e.g. SPY, AAPL or BTC/USD")
    parser.add_argument(
        "--execution",
        choices=sorted(EXECUTORS),
        help="order policy (default: market-follow for crypto, limit-follow for stocks)",
    )
    parser.add_argument("--qty", type=float, default=1, help="units per order (default: 1)")
    parser.add_argument(
        "--interval", type=float, default=10, help="seconds between cycles (default: 10)"
    )
    parser.add_argument(
        "--seed-qty",
        type=float,
        default=0,
        help="market-buy this many units at startup before trading (default: 0, off)",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    args.symbol = args.symbol.upper()
    if args.execution is None:
        args.execution = "market-follow" if is_crypto(args.symbol) else "limit-follow"
    if args.execution == "scalp" and is_crypto(args.symbol):
        parser.error("scalp uses fill-or-kill orders, which Alpaca does not support for crypto")
    if args.interval <= 0 or args.qty <= 0 or args.seed_qty < 0:
        parser.error("--interval and --qty must be positive and --seed-qty non-negative")
    return args


def run_cycle(symbol: str, strategy: Strategy, executor: Executor, data: MarketData) -> None:
    """One pass: fetch data once, ask the strategy for a signal, hand it to the executor.
    Failures are logged and the cycle is skipped, so one bad request can't stop the bot."""
    try:
        snapshot = data.snapshot(symbol)
    except (APIError, requests.RequestException, KeyError) as e:
        # KeyError: the snapshot endpoint returned no data for the symbol.
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

    broker = Broker(settings)
    data = MarketData(settings)
    strategy = STRATEGIES[args.strategy]()
    executor = EXECUTORS[args.execution](broker, args.symbol, args.qty)

    log.info(
        "Starting: strategy=%s symbol=%s execution=%s qty=%s interval=%ss %s",
        args.strategy, args.symbol, args.execution, args.qty, args.interval,
        "PAPER" if settings.paper else "LIVE",
    )

    if args.seed_qty:
        log.warning("--seed-qty: market-buying %s %s before starting", args.seed_qty, args.symbol)
        try:
            broker.market_order(args.symbol, args.seed_qty, OrderSide.BUY)
        except APIError as e:
            log.error("Seed order failed: %s", e)
            return 1

    try:
        while True:
            started = time.monotonic()
            run_cycle(args.symbol, strategy, executor, data)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
