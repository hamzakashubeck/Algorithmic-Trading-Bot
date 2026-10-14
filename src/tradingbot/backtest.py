"""Backtester: runs the same strategies and executors as the live bot over
historical 1-minute candles, with a simulated broker in place of Alpaca.

Each decision happens at the open of a bar, using only the candles before it plus
that opening price, which is what the live bot sees too. Orders fill at the open
plus half the spread; resting limit orders only fill if a later bar trades through
the limit price.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce

from tradingbot.alpaca_client import LOOKBACK, MarketData
from tradingbot.config import ConfigError, load_settings
from tradingbot.execution import EXECUTORS
from tradingbot.main import add_trading_args, check_trading_args
from tradingbot.strategies import STRATEGIES, MarketSnapshot, Strategy

log = logging.getLogger("tradingbot.backtest")

NEW_YORK = "America/New_York"


@dataclass
class SimOrder:
    id: str
    side: OrderSide
    qty: float
    limit_price: float | None
    time_in_force: TimeInForce | None
    status: OrderStatus = OrderStatus.NEW
    filled_avg_price: float | None = None


@dataclass(frozen=True)
class Fill:
    time: pd.Timestamp
    side: str
    qty: float
    price: float
    fee: float


class SimulatedBroker:
    """Same methods as AlpacaBroker, but fills orders against historical bars.

    For each bar the loop calls set_current_bar(i) before the strategy runs (so new
    orders fill at bar i's open), then process_bar(i) to fill any resting limit orders.
    """

    def __init__(
        self, candles: pd.DataFrame, cash: float, spread_bps: float, fee_bps: float
    ) -> None:
        self.cash = cash
        self.position = 0.0
        self.fills: list[Fill] = []
        self.rejected = 0
        self._times = candles.index
        self._open = candles["open"].to_numpy(dtype=np.float64)
        self._high = candles["high"].to_numpy(dtype=np.float64)
        self._low = candles["low"].to_numpy(dtype=np.float64)
        self._half_spread = spread_bps / 2 / 10_000
        self._fee = fee_bps / 10_000
        self._orders: dict[str, SimOrder] = {}
        self._resting: list[SimOrder] = []
        self._current = 0

    def set_current_bar(self, i: int) -> None:
        self._current = i

    def process_bar(self, i: int) -> None:
        for order in list(self._resting):
            price = self._resting_fill_price(order, i)
            if price is None:
                continue
            self._resting.remove(order)
            try:
                self._fill(order, price, i)
            except APIError:
                pass  # not enough cash; already counted in self.rejected

    def market_order(self, symbol: str, qty: float, side: OrderSide) -> SimOrder:
        order = self._new_order(side, qty, None, None)
        self._fill(order, self._open_price(side), self._current)
        return order

    def limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: TimeInForce | None = None,
    ) -> SimOrder:
        order = self._new_order(side, qty, round(limit_price, 2), time_in_force)
        if time_in_force == TimeInForce.FOK:
            price = self._open_price(side)
            if side is OrderSide.BUY:
                can_fill = price <= order.limit_price
            else:
                can_fill = price >= order.limit_price
            if can_fill:
                self._fill(order, price, self._current)
            else:
                order.status = OrderStatus.CANCELED  # Alpaca reports a killed FOK as canceled
        else:
            self._resting.append(order)
        return order

    def wait_for_order(self, order_id: str) -> SimOrder:
        return self._orders[order_id]

    def cancel_open_orders(self, symbol: str) -> None:
        for order in self._resting:
            order.status = OrderStatus.CANCELED
        self._resting.clear()

    def has_open_orders(self, symbol: str) -> bool:
        return bool(self._resting)

    def position_qty(self, symbol: str) -> float:
        return self.position

    def close_position(self, symbol: str) -> SimOrder:
        if self.position == 0:
            self.rejected += 1
            raise APIError("position does not exist")
        side = OrderSide.SELL if self.position > 0 else OrderSide.BUY
        return self.market_order(symbol, abs(self.position), side)

    def _new_order(
        self,
        side: OrderSide,
        qty: float,
        limit_price: float | None,
        time_in_force: TimeInForce | None,
    ) -> SimOrder:
        order = SimOrder(str(len(self._orders) + 1), side, qty, limit_price, time_in_force)
        self._orders[order.id] = order
        return order

    def _open_price(self, side: OrderSide) -> float:
        """Price for an order placed right now: this bar's open, plus or minus half the spread."""
        price = self._open[self._current]
        if side is OrderSide.BUY:
            return price * (1 + self._half_spread)
        return price * (1 - self._half_spread)

    def _resting_fill_price(self, order: SimOrder, i: int) -> float | None:
        limit = order.limit_price
        if order.side is OrderSide.BUY:
            at_open = self._open[i] * (1 + self._half_spread)
            if at_open <= limit:
                return at_open
            return limit if self._low[i] < limit else None
        at_open = self._open[i] * (1 - self._half_spread)
        if at_open >= limit:
            return at_open
        return limit if self._high[i] > limit else None

    def _fill(self, order: SimOrder, price: float, i: int) -> None:
        value = order.qty * price
        fee = value * self._fee
        if order.side is OrderSide.BUY:
            if value + fee > self.cash + 1e-9:
                order.status = OrderStatus.REJECTED
                self.rejected += 1
                raise APIError("insufficient buying power")
            self.cash -= value + fee
            self.position += order.qty
        else:
            self.cash += value - fee
            self.position -= order.qty
        order.status = OrderStatus.FILLED
        order.filled_avg_price = price
        self.fills.append(Fill(self._times[i], order.side.value, order.qty, price, fee))


@dataclass
class BacktestResult:
    qty: float
    starting_cash: float
    equity: pd.Series  # account value at each bar's close
    position: pd.Series  # shares held at each bar's close
    fills: list[Fill]
    rejected: int
    first_open: float
    last_close: float

    @property
    def pnl(self) -> float:
        return float(self.equity.iloc[-1] - self.starting_cash)

    @property
    def buy_and_hold_pnl(self) -> float:
        """What you'd have made just holding `qty` shares the whole time."""
        return self.qty * (self.last_close - self.first_open)

    @property
    def max_drawdown(self) -> float:
        """Biggest drop from a high point in account value, in dollars."""
        return float((self.equity - self.equity.cummax()).min())

    @property
    def time_in_market(self) -> float:
        return float((self.position != 0).mean())


def run_backtest(
    candles: pd.DataFrame,
    start: pd.Timestamp,
    strategy: Strategy,
    execution: str,
    symbol: str,
    qty: float = 1,
    cash: float = 100_000,
    spread_bps: float = 2.0,
    fee_bps: float = 0.0,
    seed_qty: float = 0,
    lookback: timedelta = LOOKBACK,
) -> BacktestResult:
    """Replay candles from `start` onward. Candles before `start` are just there so the
    indicators have the same 3 days of history the live bot would have."""
    index = candles.index
    first = max(int(index.searchsorted(start)), 1)  # need one completed candle before
    if first >= len(candles):
        raise ValueError("No candles on or after the start date")

    # Where each bar's 3-day lookback window starts.
    window_starts = index.searchsorted(index - lookback, side="right")
    opens = candles["open"].to_numpy(dtype=np.float64)
    closes = candles["close"].to_numpy(dtype=np.float64)
    half_spread = spread_bps / 2 / 10_000

    broker = SimulatedBroker(candles, cash, spread_bps, fee_bps)
    executor = EXECUTORS[execution](broker, symbol, qty)

    if seed_qty:
        broker.set_current_bar(first)
        broker.market_order(symbol, seed_qty, OrderSide.BUY)

    equity, position = [], []
    for i in range(first, len(candles)):
        # Decide at bar i's open, using the candles before it.
        broker.set_current_bar(i)
        price = opens[i]
        snapshot = MarketSnapshot(
            symbol=symbol,
            candles=candles.iloc[window_starts[i] : i],
            last_price=price,
            bid=price * (1 - half_spread),
            ask=price * (1 + half_spread),
        )
        # Empty after a long weekend; the live bot skips these too.
        if not snapshot.candles.empty:
            executor.on_signal(strategy.evaluate(snapshot), snapshot)

        broker.process_bar(i)
        equity.append(broker.cash + broker.position * closes[i])
        position.append(broker.position)

    times = index[first:]
    return BacktestResult(
        qty=qty,
        starting_cash=cash,
        equity=pd.Series(equity, index=times),
        position=pd.Series(position, index=times),
        fills=broker.fills,
        rejected=broker.rejected,
        first_open=float(opens[first]),
        last_close=float(closes[-1]),
    )


def regular_hours_only(candles: pd.DataFrame) -> pd.DataFrame:
    """Drop pre-market and after-hours bars (the live bot doesn't trade then either)."""
    et = candles.index.tz_convert(NEW_YORK)
    return candles.iloc[et.indexer_between_time("09:30", "15:59")]


def money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def print_report(result: BacktestResult, args: argparse.Namespace) -> None:
    start = result.equity.index[0].tz_convert(NEW_YORK)
    end = result.equity.index[-1].tz_convert(NEW_YORK)
    buys = sum(f.side == "buy" for f in result.fills)
    sells = len(result.fills) - buys

    print(f"{args.symbol}: {args.strategy} / {args.execution}, {args.qty:g} share(s) per trade")
    print(f"{start:%Y-%m-%d} to {end:%Y-%m-%d} ({len(result.equity):,} one-minute bars)")
    print(f"Assumed spread {args.spread_bps:g} bps, fees {args.fee_bps:g} bps\n")
    print(f"P&L:              {money(result.pnl)}")
    print(f"Buy and hold:     {money(result.buy_and_hold_pnl)}")
    print(f"Max drawdown:     {money(result.max_drawdown)}")
    print(f"Trades:           {len(result.fills)} ({buys} buys, {sells} sells)")
    print(f"Rejected orders:  {result.rejected}")
    print(f"Time in market:   {result.time_in_market:.1%}")
    print(f"Final position:   {result.position.iloc[-1]:g}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradingbot-backtest",
        description="Backtest a strategy on historical 1-minute candles.",
    )
    add_trading_args(parser)
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD (New York time)")
    parser.add_argument("--end", help="last day, inclusive, YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--cash", type=float, default=100_000, help="starting cash (default: 100000)"
    )
    parser.add_argument(
        "--spread-bps",
        type=float,
        default=2.0,
        help="assumed bid/ask spread in basis points, 1 bp = 0.01%% (default: 2)",
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=0.0,
        help="fee per trade in basis points (default: 0)",
    )
    parser.add_argument("--fills-csv", help="also write every fill to this CSV file")
    # Errors only by default; otherwise every rejected order prints a warning.
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "ERROR"))
    args = parser.parse_args(argv)

    check_trading_args(parser, args)
    args.start = pd.Timestamp(args.start, tz=NEW_YORK)
    end = pd.Timestamp(args.end, tz=NEW_YORK) + pd.Timedelta(days=1) if args.end else None
    args.end = min(end, pd.Timestamp.now(tz=NEW_YORK)) if end is not None else None
    if args.end is not None and args.end <= args.start:
        parser.error("--end must not be before --start")
    return args


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

    print(f"Fetching 1-minute candles for {args.symbol}...", file=sys.stderr)
    try:
        candles = MarketData(settings).candles(
            args.symbol, start=args.start - LOOKBACK, end=args.end
        )
    except (APIError, requests.RequestException) as e:
        log.error("Could not fetch candles: %s", e)
        return 1
    candles = regular_hours_only(candles)

    try:
        result = run_backtest(
            candles,
            start=args.start,
            strategy=STRATEGIES[args.strategy](),
            execution=args.execution,
            symbol=args.symbol,
            qty=args.qty,
            cash=args.cash,
            spread_bps=args.spread_bps,
            fee_bps=args.fee_bps,
            seed_qty=args.seed_qty,
        )
    except (ValueError, APIError) as e:
        log.error("%s", e)
        return 1

    print_report(result, args)
    if args.fills_csv:
        pd.DataFrame(result.fills).to_csv(args.fills_csv, index=False)
        print(f"\nWrote {len(result.fills)} fills to {args.fills_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
