"""Indicators and strategies. A strategy looks at recent candles and returns BUY,
SELL or HOLD. Nothing in here touches the network, so the same code works live and
in the backtester."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import numpy as np
import pandas as pd
import talib
from talib import MA_Type

log = logging.getLogger(__name__)


class Signal(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything a strategy or executor needs for one cycle, fetched once."""

    symbol: str
    candles: pd.DataFrame  # 1-minute OHLCV, oldest first
    last_price: float
    bid: float
    ask: float


# Indicators. Each one returns its latest value, or NaN if there isn't enough data
# yet (any comparison with NaN is False, so the strategies just return HOLD).
# TA-Lib parameters are spelled out because some defaults changed between versions.


def _col(candles: pd.DataFrame, name: str) -> np.ndarray:
    return candles[name].to_numpy(dtype=np.float64)


def rsi(candles: pd.DataFrame, period: int = 12) -> float:
    return float(talib.RSI(_col(candles, "close"), timeperiod=period)[-1])


def bollinger_bands(
    candles: pd.DataFrame, period: int = 5, num_std: float = 2.0
) -> tuple[float, float, float]:
    """Returns (upper, middle, lower)."""
    upper, middle, lower = talib.BBANDS(
        _col(candles, "close"),
        timeperiod=period,
        nbdevup=num_std,
        nbdevdn=num_std,
        matype=MA_Type.SMA,
    )
    return float(upper[-1]), float(middle[-1]), float(lower[-1])


def stoch_rsi_k(candles: pd.DataFrame, period: int = 14, fastk_period: int = 5) -> float:
    k, _d = talib.STOCHRSI(
        _col(candles, "close"),
        timeperiod=period,
        fastk_period=fastk_period,
        fastd_period=3,
        fastd_matype=MA_Type.SMA,
    )
    return float(k[-1])


def adx(candles: pd.DataFrame, period: int = 14) -> tuple[float, float, float]:
    """Returns (adx, di_plus, di_minus)."""
    high, low, close = _col(candles, "high"), _col(candles, "low"), _col(candles, "close")
    return (
        float(talib.ADX(high, low, close, timeperiod=period)[-1]),
        float(talib.PLUS_DI(high, low, close, timeperiod=period)[-1]),
        float(talib.MINUS_DI(high, low, close, timeperiod=period)[-1]),
    )


# Strategies


class Strategy(ABC):
    """evaluate() should return right away. If a strategy needs to wait for something,
    it remembers that between calls and returns HOLD in the meantime."""

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> Signal: ...


class RsiStrategy(Strategy):
    """BUY when RSI is oversold, SELL when it is overbought."""

    def __init__(self, period: int = 12, oversold: float = 20, overbought: float = 80) -> None:
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        value = rsi(snapshot.candles, self.period)
        log.debug("RSI=%.2f", value)
        if value < self.oversold:
            return Signal.BUY
        if value > self.overbought:
            return Signal.SELL
        return Signal.HOLD


class BollingerStrategy(Strategy):
    """BUY when price is below the lower band, SELL when it is above the upper band."""

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        upper, _middle, lower = bollinger_bands(snapshot.candles)
        price = snapshot.last_price
        log.debug("price=%.2f lower=%.2f upper=%.2f", price, lower, upper)
        if price < lower:
            return Signal.BUY
        if price > upper:
            return Signal.SELL
        return Signal.HOLD


class AdxStrategy(Strategy):
    """Trend following: with ADX above the threshold, BUY in an uptrend
    (DI+ over DI-) and SELL in a downtrend (DI- over DI+)."""

    def __init__(self, threshold: float = 20) -> None:
        self.threshold = threshold

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        value, di_plus, di_minus = adx(snapshot.candles)
        log.debug("ADX=%.2f DI+=%.2f DI-=%.2f", value, di_plus, di_minus)
        if value > self.threshold:
            return Signal.BUY if di_plus > di_minus else Signal.SELL
        return Signal.HOLD


class StochRsiStrategy(Strategy):
    """Stochastic RSI with "wait for the rebound" entries.

    When fast K drops below `oversold`, the strategy arms a BUY and returns HOLD
    until K climbs back to `oversold` or above, then returns BUY once.

    When K rises above `overbought`, it either SELLs immediately
    (sell_on_rebound=False) or arms a SELL and fires once K falls back to
    `overbought` or below (sell_on_rebound=True).
    """

    def __init__(
        self,
        oversold: float = 20,
        overbought: float = 80,
        sell_on_rebound: bool = False,
        fastk_period: int = 5,
    ) -> None:
        self.oversold = oversold
        self.overbought = overbought
        self.sell_on_rebound = sell_on_rebound
        self.fastk_period = fastk_period
        self._armed: Signal | None = None

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        k = stoch_rsi_k(snapshot.candles, fastk_period=self.fastk_period)
        log.debug("StochRSI K=%.2f armed=%s", k, self._armed)

        if self._armed is Signal.BUY:
            if k < self.oversold:
                return Signal.HOLD
            self._armed = None
            return Signal.BUY

        if self._armed is Signal.SELL:
            if k > self.overbought:
                return Signal.HOLD
            self._armed = None
            return Signal.SELL

        if k < self.oversold:
            log.info("StochRSI K=%.2f below %s, waiting for rebound to BUY", k, self.oversold)
            self._armed = Signal.BUY
            return Signal.HOLD

        if k > self.overbought:
            if not self.sell_on_rebound:
                return Signal.SELL
            log.info("StochRSI K=%.2f above %s, waiting for drop to SELL", k, self.overbought)
            self._armed = Signal.SELL
            return Signal.HOLD

        return Signal.HOLD


# Names accepted by --strategy.
STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "rsi": RsiStrategy,
    "bbands": BollingerStrategy,
    "adx": AdxStrategy,
    # From _stock_trader.py: buy on the rebound above 20, sell as soon as K > 80.
    "stochrsi": StochRsiStrategy,
    # From alternate_stock_trader.py: rebound on both sides (20 / 80).
    "stochrsi-rebound": lambda: StochRsiStrategy(sell_on_rebound=True),
}
