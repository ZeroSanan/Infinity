"""
Trend-Filtered Mean-Reversion Scalp Strategy

Idea: take many small, high-probability trades with a tight 1:1
risk/reward (default TP = SL = 1%). At 1:1 R:R, a ~60% win rate is
enough to stay profitable after fees — so entries are filtered hard
for "trade with the trend, buy/sell the dip in that trend" setups.

Regime filter (EMA fast vs EMA slow on closes):
  - EMA_fast > EMA_slow  -> BULL regime -> LONG entries only
  - EMA_fast < EMA_slow  -> BEAR regime -> SHORT entries only (optional)
  - EMA_fast == EMA_slow -> no entries

Entry trigger (RSI pullback-and-recover):
  - BULL: RSI dips below `rsi_long_level` then crosses back above it
          -> open LONG at that candle's close
  - BEAR: RSI rises above `rsi_short_level` then crosses back below it
          -> open SHORT at that candle's close

Exit (one position at a time):
  - TP / SL are fixed percentages from the entry price, checked
    against candle high/low wicks (SL checked before TP on the same
    candle, matching the other strategies in this package).

Each trade risks a fixed dollar amount (`position_size`, defaults to
`initial_budget`) — this is a per-trade sizing model, not compounding.
"""
import sys
from pathlib import Path
from typing import List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dca_strategy import Trade, BacktestResults, compute_backtest_results
from mixed_dca_strategy import _ema, _rsi_series


class MRScalpStrategy:
    """Trend-filtered mean-reversion scalp with fixed TP/SL."""

    DEFAULT_TAKE_PROFIT_PERCENT = 1.0
    DEFAULT_STOP_LOSS_PERCENT = 1.0

    def __init__(
        self,
        initial_budget: float = 1000,
        position_size: Optional[float] = None,
        take_profit_percent: Optional[float] = None,
        stop_loss_percent: Optional[float] = None,
        ema_fast: int = 50,
        ema_slow: int = 200,
        rsi_period: int = 14,
        rsi_long_level: float = 45.0,
        rsi_short_level: float = 55.0,
        allow_short: bool = True,
    ):
        self.initial_budget = initial_budget
        self.position_size = position_size if position_size else initial_budget
        self.take_profit_percent = (
            take_profit_percent if take_profit_percent is not None
            else self.DEFAULT_TAKE_PROFIT_PERCENT
        )
        self.stop_loss_percent = (
            stop_loss_percent if stop_loss_percent is not None
            else self.DEFAULT_STOP_LOSS_PERCENT
        )
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.rsi_period = rsi_period
        self.rsi_long_level = rsi_long_level
        self.rsi_short_level = rsi_short_level
        self.allow_short = allow_short

        self.in_trade = False
        self.direction: Optional[str] = None
        self.entry_price: Optional[float] = None
        self.entry_time = None
        self.completed_trades: List[Trade] = []
        self.available_budget = initial_budget

    def run_backtest(self, df: pd.DataFrame) -> List[Trade]:
        """
        Run the scalp backtest on historical candles.

        Args:
            df: DataFrame with columns: datetime, high, low, close
                ('close' falls back to (high+low)/2 if missing)

        Returns:
            List of completed Trade objects
        """
        self.completed_trades = []
        self.available_budget = self.initial_budget
        self.in_trade = False
        self.direction = None
        self.entry_price = None
        self.entry_time = None

        closes = (df["close"].tolist() if "close" in df.columns
                  else [(h + l) / 2 for h, l in zip(df["high"], df["low"])])
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        times = df["datetime"].tolist()

        ema_fast = _ema(closes, self.ema_fast_period)
        ema_slow = _ema(closes, self.ema_slow_period)
        rsi = _rsi_series(closes, self.rsi_period)

        below_long = False   # RSI dipped below rsi_long_level, waiting to cross back up
        above_short = False  # RSI rose above rsi_short_level, waiting to cross back down

        for i in range(len(closes)):
            if self.in_trade:
                if self.direction == "long":
                    tp = self.entry_price * (1 + self.take_profit_percent / 100)
                    sl = self.entry_price * (1 - self.stop_loss_percent / 100)
                    if lows[i] <= sl:
                        self._close_trade(times[i], sl, "stop_loss")
                    elif highs[i] >= tp:
                        self._close_trade(times[i], tp, "take_profit")
                else:  # short
                    tp = self.entry_price * (1 - self.take_profit_percent / 100)
                    sl = self.entry_price * (1 + self.stop_loss_percent / 100)
                    if highs[i] >= sl:
                        self._close_trade(times[i], sl, "stop_loss")
                    elif lows[i] <= tp:
                        self._close_trade(times[i], tp, "take_profit")
                continue

            ef, es, r = ema_fast[i], ema_slow[i], rsi[i]
            if ef is None or es is None or r is None:
                continue

            if ef > es:
                # BULL regime — look for an RSI pullback-and-recover (long entry)
                above_short = False
                if r < self.rsi_long_level:
                    below_long = True
                elif below_long and r >= self.rsi_long_level:
                    self._open_trade("long", times[i], closes[i])
                    below_long = False
            elif ef < es and self.allow_short:
                # BEAR regime — look for an RSI rally-and-fade (short entry)
                below_long = False
                if r > self.rsi_short_level:
                    above_short = True
                elif above_short and r <= self.rsi_short_level:
                    self._open_trade("short", times[i], closes[i])
                    above_short = False
            else:
                below_long = False
                above_short = False

        return self.completed_trades

    def _open_trade(self, direction: str, time, price: float):
        self.in_trade = True
        self.direction = direction
        self.entry_price = price
        self.entry_time = time

    def _close_trade(self, exit_time, exit_price: float, reason: str):
        entry = self.entry_price
        invested = self.position_size

        if self.direction == "long":
            total_return = invested * (exit_price / entry)
        else:
            total_return = invested * (1 + (entry - exit_price) / entry)

        profit_loss = total_return - invested
        profit_percent = (profit_loss / invested * 100) if invested else 0.0
        is_loss = reason == "stop_loss"

        trade = Trade(
            start_time=self.entry_time,
            end_time=exit_time,
            anchor_price=entry,
            deepest_level=1,
            deepest_price=entry,
            exit_price=exit_price,
            total_invested=invested,
            total_return=total_return,
            profit_loss=profit_loss,
            profit_percent=profit_percent,
            dca_levels_filled=[1],
            stop_loss_triggered=is_loss,
            stop_loss_price=exit_price if is_loss else None,
            stop_loss_loss=abs(profit_loss) if is_loss else 0.0,
            completion_reason=reason,
        )
        self.completed_trades.append(trade)
        self.available_budget += profit_loss

        self.in_trade = False
        self.direction = None
        self.entry_price = None
        self.entry_time = None

    def calculate_backtest_results(self) -> BacktestResults:
        return compute_backtest_results(self.completed_trades, self.initial_budget)
