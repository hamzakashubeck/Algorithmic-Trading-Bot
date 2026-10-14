"""Everything that talks to Alpaca: market data (MarketData) and orders (AlpacaBroker)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Clock, Order
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from tradingbot.config import Settings
from tradingbot.strategies import MarketSnapshot

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]

# How much candle history the indicators get (the backtester uses this too)
LOOKBACK = timedelta(days=3)

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
}


# Market data


class MarketData:
    def __init__(self, settings: Settings, lookback: timedelta = LOOKBACK) -> None:
        self._stocks = StockHistoricalDataClient(settings.api_key, settings.secret_key)
        self._feed = DataFeed(settings.data_feed)
        self._lookback = lookback

    def candles(self, symbol: str, start: datetime, end: datetime | None = None) -> pd.DataFrame:
        """1-minute OHLCV candles between start and end (default: now), oldest first."""
        bars = self._stocks.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=self._feed,
            )
        )
        return _to_candles(bars.df)

    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Fetch candles and the latest trade/quote for one symbol"""
        candles = self.candles(symbol, start=datetime.now(timezone.utc) - self._lookback)
        snap = self._stocks.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbol, feed=self._feed)
        )[symbol]

        return MarketSnapshot(
            symbol=symbol,
            candles=candles,
            last_price=float(snap.latest_trade.price),
            bid=float(snap.latest_quote.bid_price),
            ask=float(snap.latest_quote.ask_price),
        )


def _to_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten Alpaca's (symbol, timestamp) index into a plain OHLCV frame."""
    if df.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS, dtype=float)
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel("symbol")
    return df[CANDLE_COLUMNS].astype(float).sort_index()


# Orders


class AlpacaBroker:
    def __init__(self, settings: Settings) -> None:
        self._client = TradingClient(
            settings.api_key,
            settings.secret_key,
            paper=settings.paper,
            url_override=settings.base_url,
        )

    def clock(self) -> Clock:
        """Use Alpaca's market clock"""
        return self._client.get_clock()

    def market_order(self, symbol: str, qty: float, side: OrderSide) -> Order:
        order = self._client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )
        log.info("Submitted market %s %s x%s (id=%s)", side.value, symbol, qty, order.id)
        return order

    def limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: TimeInForce | None = None,
    ) -> Order:
        tif = time_in_force or TimeInForce.DAY  # the old SDK's default
        order = self._client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=round(limit_price, 2),
                time_in_force=tif,
            )
        )
        log.info(
            "Submitted %s limit %s %s x%s @ %.2f (id=%s)",
            tif.value, side.value, symbol, qty, limit_price, order.id,
        )
        return order

    def wait_for_order(self, order_id: str, timeout: float = 5.0, poll: float = 0.5) -> Order:
        """Check the order every half second until it's done, for up to `timeout` seconds."""
        deadline = time.monotonic() + timeout
        order = self._client.get_order_by_id(order_id)
        while order.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(poll)
            order = self._client.get_order_by_id(order_id)
        return order

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancel this bot's open orders only"""
        for order in self._open_orders(symbol):
            self._client.cancel_order_by_id(order.id)
            log.debug("Canceled order %s", order.id)

    def has_open_orders(self, symbol: str) -> bool:
        return bool(self._open_orders(symbol))

    def position_qty(self, symbol: str) -> float:
        """How many shares are held."""
        try:
            return float(self._client.get_open_position(symbol).qty)
        except APIError as e:
            if e.status_code == 404:  # Alpaca's answer for "no position"
                return 0.0
            raise

    def _open_orders(self, symbol: str) -> list[Order]:
        return self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        )

    def close_position(self, symbol: str) -> Order:
        order = self._client.close_position(symbol)
        log.info("Closing position in %s (id=%s)", symbol, order.id)
        return order
