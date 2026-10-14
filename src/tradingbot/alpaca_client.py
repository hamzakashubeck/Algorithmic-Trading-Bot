"""Everything that talks to Alpaca: market data (MarketData) and orders (Broker)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoSnapshotRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from tradingbot.config import Settings
from tradingbot.strategies import MarketSnapshot

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
}


def is_crypto(symbol: str) -> bool:
    """Alpaca crypto pairs are written with a slash, e.g. BTC/USD."""
    return "/" in symbol


# ---------------------------------------------------------------- market data


class MarketData:
    def __init__(self, settings: Settings, lookback: timedelta = timedelta(days=3)) -> None:
        self._stocks = StockHistoricalDataClient(settings.api_key, settings.secret_key)
        self._crypto = CryptoHistoricalDataClient(settings.api_key, settings.secret_key)
        self._feed = DataFeed(settings.data_feed)
        self._lookback = lookback

    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Fetch candles and the latest trade/quote for one symbol (two API calls)."""
        start = datetime.now(timezone.utc) - self._lookback
        if is_crypto(symbol):
            bars = self._crypto.get_crypto_bars(
                CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start)
            )
            snap = self._crypto.get_crypto_snapshot(
                CryptoSnapshotRequest(symbol_or_symbols=symbol)
            )[symbol]
        else:
            bars = self._stocks.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    feed=self._feed,
                )
            )
            snap = self._stocks.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=symbol, feed=self._feed)
            )[symbol]

        return MarketSnapshot(
            symbol=symbol,
            candles=_to_candles(bars.df),
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


# ---------------------------------------------------------------- orders


def default_time_in_force(symbol: str) -> TimeInForce:
    # Alpaca rejects DAY orders for crypto; DAY is what the old SDK defaulted to for stocks.
    return TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY


class Broker:
    def __init__(self, settings: Settings) -> None:
        self._client = TradingClient(
            settings.api_key,
            settings.secret_key,
            paper=settings.paper,
            url_override=settings.base_url,
        )

    def market_order(self, symbol: str, qty: float, side: OrderSide) -> Order:
        order = self._client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=default_time_in_force(symbol),
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
        tif = time_in_force or default_time_in_force(symbol)
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
        """Poll until the order reaches a terminal status or the timeout passes."""
        deadline = time.monotonic() + timeout
        order = self._client.get_order_by_id(order_id)
        while order.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(poll)
            order = self._client.get_order_by_id(order_id)
        return order

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancel this bot's open orders only, not every order on the account."""
        open_orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        )
        for order in open_orders:
            self._client.cancel_order_by_id(order.id)
            log.debug("Canceled order %s", order.id)

    def close_position(self, symbol: str) -> Order:
        # Positions are keyed without the slash (BTC/USD -> BTCUSD).
        order = self._client.close_position(symbol.replace("/", ""))
        log.info("Closing position in %s (id=%s)", symbol, order.id)
        return order
