"""Executors turn a BUY/SELL/HOLD signal into actual orders.

All of them are long-only: BUY opens a position only if we don't already have one,
and SELL sells everything we hold.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Protocol

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce

from tradingbot.strategies import MarketSnapshot, Signal

log = logging.getLogger(__name__)


class Broker(Protocol):
    """The broker methods executors use. AlpacaBroker and the backtester's
    SimulatedBroker both have these, so the same executor code runs in both."""

    def market_order(self, symbol: str, qty: float, side: OrderSide) -> Any: ...

    def limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: TimeInForce | None = None,
    ) -> Any: ...

    def wait_for_order(self, order_id: str) -> Any: ...

    def cancel_open_orders(self, symbol: str) -> None: ...

    def has_open_orders(self, symbol: str) -> bool: ...

    def position_qty(self, symbol: str) -> float: ...

    def close_position(self, symbol: str) -> Any: ...


class Executor(ABC):
    def __init__(self, broker: Broker, symbol: str, qty: float = 1) -> None:
        self.broker = broker
        self.symbol = symbol
        self.qty = qty

    @abstractmethod
    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None: ...


class LimitFollowExecutor(Executor):
    """Limit orders just past the ask/bid. Unfilled orders get canceled at the start
    of the next cycle (and placed again if the signal is still there)."""

    def __init__(self, broker: Broker, symbol: str, qty: float = 1, offset: float = 0.20) -> None:
        super().__init__(broker, symbol, qty)
        self.offset = offset

    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None:
        self.broker.cancel_open_orders(self.symbol)
        if signal is Signal.HOLD:
            return

        try:
            held = self.broker.position_qty(self.symbol)
            if signal is Signal.BUY and held == 0:
                self.broker.limit_order(
                    self.symbol, self.qty, OrderSide.BUY, snapshot.ask + self.offset
                )
            elif signal is Signal.SELL and held > 0:
                self.broker.limit_order(
                    self.symbol, held, OrderSide.SELL, snapshot.bid - self.offset
                )
        except APIError as e:
            log.warning("%s order rejected: %s", signal.value, e)


class ScalpExecutor(Executor):
    """Buy with a fill-or-kill order, then put in a take-profit sell right away.
    A SELL signal cancels the take-profit and sells at market instead.

    slippage and take_profit are fractions of the price (0.002 = 0.2%).
    """

    def __init__(
        self,
        broker: Broker,
        symbol: str,
        qty: float = 1,
        slippage: float = 0.0005,
        take_profit: float = 0.002,
    ) -> None:
        super().__init__(broker, symbol, qty)
        self.slippage = slippage
        self.take_profit = take_profit

    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None:
        if signal is Signal.HOLD:
            return
        try:
            held = self.broker.position_qty(self.symbol)
            if signal is Signal.BUY and held == 0 and not self.broker.has_open_orders(self.symbol):
                self._enter(snapshot.ask)
            elif signal is Signal.SELL and held > 0:
                self.broker.cancel_open_orders(self.symbol)  # the resting take-profit
                self.broker.close_position(self.symbol)
        except APIError as e:
            log.warning("%s scalp failed: %s", signal.value, e)

    def _enter(self, ask: float) -> None:
        if ask <= 0:  # no quote
            log.info("No ask quote for %s; skipping scalp", self.symbol)
            return
        order = self.broker.limit_order(
            self.symbol,
            self.qty,
            OrderSide.BUY,
            ask * (1 + self.slippage),
            time_in_force=TimeInForce.FOK,
        )
        order = self.broker.wait_for_order(order.id)
        if order.status != OrderStatus.FILLED:
            log.info("FOK buy was not filled (status=%s)", order.status.value)
            return
        fill = float(order.filled_avg_price)
        self.broker.limit_order(
            self.symbol,
            self.qty,
            OrderSide.SELL,
            fill * (1 + self.take_profit),
            time_in_force=TimeInForce.GTC,
        )


class MarketFollowExecutor(Executor):
    """Plain market orders: buy on BUY, close the position on SELL. Waits if an
    earlier order hasn't gone through yet, so orders don't pile up."""

    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None:
        if signal is Signal.HOLD:
            return
        try:
            if self.broker.has_open_orders(self.symbol):
                return
            held = self.broker.position_qty(self.symbol)
            if signal is Signal.BUY and held == 0:
                self.broker.market_order(self.symbol, self.qty, OrderSide.BUY)
            elif signal is Signal.SELL and held > 0:
                self.broker.close_position(self.symbol)
        except APIError as e:
            log.warning("%s failed: %s", signal.value, e)


EXECUTORS: dict[str, type[Executor]] = {
    "limit-follow": LimitFollowExecutor,
    "scalp": ScalpExecutor,
    "market-follow": MarketFollowExecutor,
}
