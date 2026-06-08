"""
Short DCA Strategy — Bear Market Edition

Inverse of DCAStrategy: shorts (sells) when price PUMPS, profits when price DROPS.

Key Variables:
- P0 = anchor price (recent low, opposite of long which tracks recent high)
- pi = pump % for level i  (positive, e.g. +6%, +9%)
- entry_price = P0 * (1 + pi/100)
- qi = coins shorted = ai / entry_price

Position State:
- A = sum(ai)       total USD received from shorts
- Q = sum(qi)       total coins shorted
- avg_short = A / Q weighted average short price

Take-Profit:
- tp_price = avg_short * (1 - take_profit_percent/100)
- Triggers: candle.low <= tp_price
- profit = Q * (avg_short - tp_price)

Stop-Loss:
- sl_threshold = anchor_price * (1 + stop_loss_percent/100)
- Triggers: candle.high >= sl_threshold  (price pumped past threshold)

Candle Handling (inverse of long):
- Short fill : candle.high >= entry_price
- Take profit: candle.low  <= tp_price
- Stop loss  : candle.high >= sl_threshold
"""

import pandas as pd
from typing import List, Optional
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from dca_strategy import DCALevel, Trade, BacktestResults, load_and_prepare_data


class ShortDCAStrategy:
    """
    Multi-level short DCA for bear markets.
    Drop-in counterpart to DCAStrategy — same interface, inverted logic.
    """

    DEFAULT_PUMP_LEVELS      = [6, 9, 12, 16, 20, 24]   # positive percentages
    DEFAULT_TAKE_PROFIT_PCT  = 5.0

    def __init__(
        self,
        initial_budget: float = 1000,
        budget_per_level: Optional[List[float]] = None,
        dca_levels: Optional[List[float]] = None,   # positive pump %
        take_profit_percent: Optional[float] = None,
        stop_loss_percent: float = 0.0,
    ):
        self.initial_budget     = initial_budget
        self.stop_loss_percent  = stop_loss_percent
        self.dca_levels         = dca_levels or self.DEFAULT_PUMP_LEVELS
        self.take_profit_percent = (
            take_profit_percent if take_profit_percent is not None
            else self.DEFAULT_TAKE_PROFIT_PCT
        )

        if budget_per_level is None:
            self.budget_per_level = [initial_budget / len(self.dca_levels)] * len(self.dca_levels)
        else:
            if len(budget_per_level) != len(self.dca_levels):
                raise ValueError("budget_per_level length must match dca_levels length")
            self.budget_per_level = budget_per_level

        self.in_trade             = False
        self.anchor_price         = None
        self.trade_start_time     = None
        self.active_dca_levels:   List[DCALevel] = []
        self.completed_trades:    List[Trade]    = []
        self.available_budget     = initial_budget

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reset(self):
        self.in_trade         = False
        self.anchor_price     = None
        self.trade_start_time = None
        self.active_dca_levels = []

    def _build_levels(self, anchor: float) -> List[DCALevel]:
        return [
            DCALevel(
                level_num=i,
                dump_percent=pump_pct,          # field reused for pump %
                entry_price=anchor * (1 + pump_pct / 100),
                budget_allocation=self.budget_per_level[i - 1],
            )
            for i, pump_pct in enumerate(self.dca_levels, start=1)
        ]

    def _filled(self) -> List[DCALevel]:
        return [l for l in self.active_dca_levels if l.filled]

    def _deepest(self) -> Optional[DCALevel]:
        f = self._filled()
        return max(f, key=lambda x: x.level_num) if f else None

    # ── Position metrics ──────────────────────────────────────────────────────

    def _avg_short(self) -> Optional[float]:
        f = self._filled()
        if not f:
            return None
        A = sum(l.budget_allocation for l in f)
        Q = sum(l.budget_allocation / l.fill_price for l in f)
        return A / Q

    def calculate_take_profit_price(self) -> Optional[float]:
        avg = self._avg_short()
        if avg is None:
            return None
        return avg * (1 - self.take_profit_percent / 100)

    def calculate_stop_loss_threshold(self) -> Optional[float]:
        if self.stop_loss_percent <= 0 or not self._filled():
            return None
        deepest = self._deepest()
        return deepest.fill_price * (1 + self.stop_loss_percent / 100)

    # ── Fill / exit checks ────────────────────────────────────────────────────

    def _check_fills(self, candle_high: float):
        for level in self.active_dca_levels:
            if not level.filled and candle_high >= level.entry_price:
                level.filled     = True
                level.fill_price = level.entry_price

    def _check_tp(self, candle_low: float) -> bool:
        tp = self.calculate_take_profit_price()
        return tp is not None and candle_low <= tp

    def _check_sl(self, candle_high: float) -> bool:
        sl = self.calculate_stop_loss_threshold()
        return sl is not None and candle_high >= sl

    # ── Trade closing ─────────────────────────────────────────────────────────

    def _close_tp(self, ts: pd.Timestamp, exit_price: float) -> Trade:
        f       = self._filled()
        deepest = self._deepest()
        A = sum(l.budget_allocation for l in f)
        Q = sum(l.budget_allocation / l.fill_price for l in f)
        buyback  = Q * exit_price
        pnl      = A - buyback
        pnl_pct  = (pnl / A * 100) if A else 0

        trade = Trade(
            start_time=self.trade_start_time, end_time=ts,
            anchor_price=self.anchor_price,
            deepest_level=deepest.level_num, deepest_price=deepest.fill_price,
            exit_price=exit_price,
            total_invested=A, total_return=buyback,
            profit_loss=pnl, profit_percent=pnl_pct,
            dca_levels_filled=[l.level_num for l in f],
            completion_reason="take_profit",
        )
        self.completed_trades.append(trade)
        return trade

    def _close_sl(self, ts: pd.Timestamp, sl_price: float, budget: float):
        f       = self._filled()
        deepest = self._deepest()
        A = sum(l.budget_allocation for l in f)
        Q = sum(l.budget_allocation / l.fill_price for l in f)
        buyback      = Q * sl_price
        capital_loss = buyback - A          # positive = real loss, negative = profit
        actual_pnl   = -capital_loss        # positive if profit
        actual_pct   = (actual_pnl / A * 100) if A else 0.0
        is_real_loss = capital_loss > 0

        trade = Trade(
            start_time=self.trade_start_time, end_time=ts,
            anchor_price=self.anchor_price,
            deepest_level=deepest.level_num, deepest_price=deepest.fill_price,
            exit_price=sl_price,
            total_invested=A, total_return=buyback,
            profit_loss=actual_pnl, profit_percent=actual_pct,
            dca_levels_filled=[l.level_num for l in f],
            stop_loss_triggered=is_real_loss, stop_loss_price=sl_price,
            stop_loss_loss=capital_loss if is_real_loss else 0.0,
            completion_reason="stop_loss",
        )
        self.completed_trades.append(trade)
        return trade, budget - capital_loss

    # ── Backtest ──────────────────────────────────────────────────────────────

    def run_backtest(self, df: pd.DataFrame) -> List[Trade]:
        """
        Run short DCA backtest on OHLC data.

        Entry: price pumps >= first_pump_level% above the recent low (anchor)
        Exit:  price falls to TP, or pumps past SL threshold
        Anchor update (not in trade): tracks the rolling lowest low
        """
        self._reset()
        self.completed_trades = []
        self.available_budget = self.initial_budget

        for _, row in df.iterrows():
            ts   = row["datetime"]
            high = row["high"]
            low  = row["low"]

            if not self.in_trade:
                if self.anchor_price is None:
                    self.anchor_price = low
                    continue

                pump_pct = (high - self.anchor_price) / self.anchor_price * 100

                if pump_pct >= self.dca_levels[0]:
                    # Pump hit first level — start trade
                    self.in_trade         = True
                    self.trade_start_time = ts
                    self.active_dca_levels = self._build_levels(self.anchor_price)
                    self._check_fills(high)
                else:
                    # Update anchor to rolling low
                    self.anchor_price = min(self.anchor_price, low)

            else:
                # STEP 1: fills (price pumping higher)
                self._check_fills(high)

                # STEP 2: stop-loss (price pumped past SL threshold)
                if self._check_sl(high):
                    sl_thr = self.calculate_stop_loss_threshold()
                    _, self.available_budget = self._close_sl(ts, sl_thr, self.available_budget)
                    self._reset()
                    self.anchor_price = low
                    continue

                # STEP 3: take-profit (price fell below TP)
                if self._check_tp(low):
                    tp = self.calculate_take_profit_price()
                    trade = self._close_tp(ts, tp)
                    self.available_budget += trade.profit_loss
                    self._reset()
                    self.anchor_price = low

        return self.completed_trades

    # ── Results ───────────────────────────────────────────────────────────────

    def calculate_backtest_results(self) -> BacktestResults:
        trades = self.completed_trades
        if not trades:
            return BacktestResults(
                initial_budget=self.initial_budget, final_budget=self.initial_budget,
                total_trades=0, winning_trades=0, losing_trades=0, stopped_out_trades=0,
                total_profit=0.0, total_loss=0.0, net_pnl=0.0, total_roi=0.0,
                win_rate=0.0, stop_loss_rate=0.0, avg_trade_pnl=0.0,
                largest_loss=0.0, largest_profit=0.0,
                avg_loss_magnitude=0.0, avg_profit_magnitude=0.0,
                avg_trade_duration_days=0.0, total_test_days=0.0,
            )

        winning = [t for t in trades if t.profit_loss > 0]
        losing  = [t for t in trades if t.profit_loss < 0]
        stopped = [t for t in trades if t.stop_loss_triggered]

        total_profit = sum(t.profit_loss for t in winning)
        total_loss   = abs(sum(t.profit_loss for t in losing))
        net_pnl      = sum(t.profit_loss for t in trades)
        final_budget = self.initial_budget + net_pnl
        total_roi    = net_pnl / self.initial_budget * 100 if self.initial_budget else 0.0
        n            = len(trades)
        durations    = [(t.end_time - t.start_time).total_seconds() / 86400 for t in trades]

        return BacktestResults(
            initial_budget=self.initial_budget, final_budget=final_budget,
            total_trades=n,
            winning_trades=len(winning), losing_trades=len(losing), stopped_out_trades=len(stopped),
            total_profit=total_profit, total_loss=total_loss, net_pnl=net_pnl,
            total_roi=total_roi,
            win_rate=len(winning)/n*100 if n else 0.0,
            stop_loss_rate=len(stopped)/n*100 if n else 0.0,
            avg_trade_pnl=net_pnl/n if n else 0.0,
            largest_loss=min(t.profit_loss for t in trades),
            largest_profit=max(t.profit_loss for t in trades),
            avg_loss_magnitude=total_loss/len(losing) if losing else 0.0,
            avg_profit_magnitude=total_profit/len(winning) if winning else 0.0,
            avg_trade_duration_days=sum(durations)/len(durations) if durations else 0.0,
            total_test_days=(trades[-1].end_time - trades[0].start_time).total_seconds()/86400,
        )
