"""Execution policies: how a signal turns into orders.

Strategies decide *when* to trade; executors decide *how*. Each executor
reproduces the order handling of one of the original 2022 scripts.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce

from tradingbot.alpaca_client import Broker
from tradingbot.strategies import MarketSnapshot, Signal

log = logging.getLogger(__name__)


class Executor(ABC):
    def __init__(self, broker: Broker, symbol: str, qty: float = 1) -> None:
        self.broker = broker
        self.symbol = symbol
        self.qty = qty

    @abstractmethod
    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None: ...


class LimitFollowExecutor(Executor):
    """From _stock_trader.py.

    Every cycle, cancel this symbol's unfilled orders. On a new BUY, place a
    limit buy a little above the ask; on a new SELL, a limit sell a little below
    the bid. A signal only trades once until it changes.
    """

    def __init__(self, broker: Broker, symbol: str, qty: float = 1, offset: float = 0.20) -> None:
        super().__init__(broker, symbol, qty)
        self.offset = offset
        self._last = Signal.HOLD

    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None:
        self.broker.cancel_open_orders(self.symbol)

        if signal is Signal.HOLD or signal is self._last:
            self._last = signal
            return
        self._last = signal

        if signal is Signal.BUY:
            side, price = OrderSide.BUY, snapshot.ask + self.offset
        else:
            side, price = OrderSide.SELL, snapshot.bid - self.offset
        try:
            self.broker.limit_order(self.symbol, self.qty, side, price)
        except APIError as e:
            log.warning("%s order rejected: %s", side.value, e)


class ScalpExecutor(Executor):
    """From alternate_stock_trader.py.

    On BUY, try a fill-or-kill buy at the ask plus a small slippage allowance; if
    it fills, place a GTC take-profit sell `take_profit` above the fill price.
    SELL mirrors this (FOK sell at the bid, then a GTC re-buy below the fill).

    Both amounts are fractions of price (0.002 = 0.2%), so they mean the same
    thing for a $20 stock as for a $700 ETF. The 2022 version used fixed cents.
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
        if signal is Signal.BUY:
            self._scalp(OrderSide.BUY, OrderSide.SELL, snapshot.ask, +1)
        elif signal is Signal.SELL:
            self._scalp(OrderSide.SELL, OrderSide.BUY, snapshot.bid, -1)

    def _scalp(self, entry: OrderSide, exit_: OrderSide, quote: float, direction: int) -> None:
        # Outside market hours the quote can be empty (0), which would price the order at 0.
        if quote <= 0:
            side = "ask" if direction > 0 else "bid"
            log.info("No %s quote for %s; skipping scalp", side, self.symbol)
            return
        try:
            order = self.broker.limit_order(
                self.symbol,
                self.qty,
                entry,
                quote * (1 + direction * self.slippage),
                time_in_force=TimeInForce.FOK,
            )
            order = self.broker.wait_for_order(order.id)
            if order.status != OrderStatus.FILLED:
                log.info("FOK %s was not filled (status=%s)", entry.value, order.status.value)
                return
            fill = float(order.filled_avg_price)
            self.broker.limit_order(
                self.symbol,
                self.qty,
                exit_,
                fill * (1 + direction * self.take_profit),
                time_in_force=TimeInForce.GTC,
            )
        except APIError as e:
            log.warning("%s scalp failed: %s", entry.value, e)


class MarketFollowExecutor(Executor):
    """From crypto_trader.py.

    On a new BUY, market-buy `qty`; on a new SELL, close the whole position.
    A signal only trades once until it changes.
    """

    def __init__(self, broker: Broker, symbol: str, qty: float = 1) -> None:
        super().__init__(broker, symbol, qty)
        self._last = Signal.HOLD

    def on_signal(self, signal: Signal, snapshot: MarketSnapshot) -> None:
        if signal is Signal.HOLD or signal is self._last:
            self._last = signal
            return
        self._last = signal

        try:
            if signal is Signal.BUY:
                self.broker.market_order(self.symbol, self.qty, OrderSide.BUY)
            else:
                self.broker.close_position(self.symbol)
        except APIError as e:
            # Most commonly: not enough buying power, or no position to close.
            log.warning("%s failed: %s", signal.value, e)


EXECUTORS: dict[str, type[Executor]] = {
    "limit-follow": LimitFollowExecutor,
    "scalp": ScalpExecutor,
    "market-follow": MarketFollowExecutor,
}
