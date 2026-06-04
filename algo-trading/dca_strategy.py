"""
DCA (Dollar Cost Averaging) Strategy Implementation

DETERMINISTIC BACKTEST ALGORITHM - PORTFOLIO-LEVEL TAKE-PROFIT

Strategy Overview:
- Multi-level DCA with fixed USD allocations per level
- Portfolio-level profit calculation (NOT price-from-last-entry)
- Spot-only, no leverage
- Deterministic execution

Key Variables:
- P0 = reference/anchor price
- di = dump percentage for level i (e.g., -6%, -9%)
- ai = USD allocation for level i
- pi = execution price = P0 * (1 + di/100)
- qi = quantity bought = ai / pi

Position State (maintained continuously):
- A = sum(ai)        # total invested capital
- Q = sum(qi)        # total position size
- avg_price = A / Q  # weighted average entry price

Profit Calculation (CRITICAL):
- Unrealized PnL at market price P:
  pnl = Q * P - A
  pnl_pct = pnl / A

Take-Profit Condition:
- TP triggers when: pnl_pct >= target_profit (5%)
- Equivalent: P >= avg_price * (1 + target_profit)
- ✓ Portfolio-level profit (correct)
- ✗ NOT: price >= last_dca_price * 1.05 (incorrect)

Candle Handling:
- DCA entry: candle.low <= pi
- Take profit: candle.high >= tp_price
- DCA levels executed in order, never skipped
- After TP, position resets completely

Strategy operates on fixed timeframe with predefined DCA levels.
Uses only candle wicks (high/low), ignoring bodies (open/close).
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path


@dataclass
class DCALevel:
    """Represents a single DCA level"""
    level_num: int          # 1..N
    dump_percent: float     # -6%, -9%, etc.
    entry_price: float      # Calculated entry price
    budget_allocation: float  # Fixed budget for this level
    filled: bool = False    # Whether this level was filled
    fill_price: Optional[float] = None  # Actual fill price
    

@dataclass
class Trade:
    """Represents a completed trade cycle"""
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    anchor_price: float
    deepest_level: int
    deepest_price: float
    exit_price: float
    total_invested: float
    total_return: float
    profit_loss: float
    profit_percent: float
    dca_levels_filled: List[int]
    
    # NEW: Stop-loss tracking
    stop_loss_triggered: bool = False
    stop_loss_price: Optional[float] = None
    stop_loss_loss: float = 0.0
    completion_reason: str = "take_profit"


@dataclass
class BacktestResults:
    """Comprehensive backtest statistics including stop-loss metrics"""
    initial_budget: float
    final_budget: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    stopped_out_trades: int
    
    total_profit: float
    total_loss: float
    net_pnl: float
    
    total_roi: float
    win_rate: float
    stop_loss_rate: float
    avg_trade_pnl: float
    
    largest_loss: float
    largest_profit: float
    avg_loss_magnitude: float
    avg_profit_magnitude: float
    
    avg_trade_duration_days: float
    total_test_days: float


class DCAStrategy:
    """
    DCA Strategy with adjustable dump levels and take-profit target
    
    Key features:
    - Configurable DCA levels (negative percentages)
    - Uses only candle wicks (high/low)
    - Fixed budget allocation per level
    - Take profit at +5% from deepest DCA level
    - Restarts after each complete trade
    """
    
    # Default DCA dump levels (negative percentages)
    DEFAULT_DCA_LEVELS = [-6, -9, -12, -16, -20, -24]
    
    # Take profit target (positive percentage from deepest level)
    DEFAULT_TAKE_PROFIT_PERCENT = 5.0
    
    def __init__(
        self,
        initial_budget: float = 1000,
        budget_per_level: Optional[List[float]] = None,
        dca_levels: Optional[List[float]] = None,
        take_profit_percent: Optional[float] = None,
        stop_loss_percent: float = 0.0,
    ):
        """
        Initialize DCA strategy with optional stop-loss
        
        Args:
            initial_budget: Starting budget (default: $1000)
            budget_per_level: Optional custom budget allocation per level
            dca_levels: Optional custom DCA dump levels (negative percentages)
            take_profit_percent: Optional custom take-profit percentage
            stop_loss_percent: Stop-loss percentage (0 = disabled)
        """
        self.initial_budget = initial_budget
        self.stop_loss_percent = stop_loss_percent
        self.dca_levels = dca_levels or self.DEFAULT_DCA_LEVELS
        self.take_profit_percent = (
            take_profit_percent
            if take_profit_percent is not None
            else self.DEFAULT_TAKE_PROFIT_PERCENT
        )
        
        # Default equal budget allocation across N levels
        if budget_per_level is None:
            self.budget_per_level = [initial_budget / len(self.dca_levels)] * len(self.dca_levels)
        else:
            if len(budget_per_level) != len(self.dca_levels):
                raise ValueError("Budget allocations must match DCA levels length")
            self.budget_per_level = budget_per_level
        
        # Strategy state
        self.in_trade = False
        self.anchor_price = None
        self.trade_start_time = None
        self.active_dca_levels: List[DCALevel] = []
        self.completed_trades: List[Trade] = []
        self.available_budget = initial_budget
        
    def reset_game(self):
        """Reset strategy state for new trade cycle"""
        self.in_trade = False
        self.anchor_price = None
        self.trade_start_time = None
        self.active_dca_levels = []
        
    def calculate_dca_prices(self, anchor_price: float) -> List[DCALevel]:
        """
        Calculate DCA entry prices from anchor price
        
        Args:
            anchor_price: Current price that becomes the anchor
            
        Returns:
            List of DCALevel objects with calculated entry prices
        """
        dca_levels = []
        for i, dump_pct in enumerate(self.dca_levels, start=1):
            entry_price = anchor_price * (1 + dump_pct / 100)
            dca_levels.append(DCALevel(
                level_num=i,
                dump_percent=dump_pct,
                entry_price=entry_price,
                budget_allocation=self.budget_per_level[i-1]
            ))
        return dca_levels
    
    def check_dca_fills(self, candle_low: float) -> List[DCALevel]:
        """
        Check which DCA levels were filled by candle low (wick touch)
        
        Execution rule: DCA entry triggers if candle.low <= pi
        Levels are executed in order, never skipped.
        
        Args:
            candle_low: Low price of current candle
            
        Returns:
            List of newly filled DCA levels
        """
        newly_filled = []
        for level in self.active_dca_levels:
            if not level.filled and candle_low <= level.entry_price:
                level.filled = True
                level.fill_price = level.entry_price  # Filled at limit price
                newly_filled.append(level)
        return newly_filled
    
    def get_deepest_filled_level(self) -> Optional[DCALevel]:
        """Get the deepest (lowest price) filled DCA level"""
        filled_levels = [lvl for lvl in self.active_dca_levels if lvl.filled]
        if not filled_levels:
            return None
        return max(filled_levels, key=lambda x: x.level_num)
    
    def calculate_take_profit_price(self) -> Optional[float]:
        """
        Calculate take-profit price that guarantees target profit on total position
        
        PORTFOLIO-LEVEL PROFIT CALCULATION:
        - A = sum(ai) = total invested capital
        - Q = sum(qi) = total position size (coins)
        - avg_price = A / Q = weighted average entry price
        
        Take-profit condition:
        - pnl_pct = (Q * P - A) / A >= target_profit
        - Solving for P: P >= (1 + target_profit) * A / Q
        - Therefore: tp_price = (1 + target_profit) * avg_price
        
        This ensures PORTFOLIO profit = target_profit, not just price movement.
        
        Returns:
            Take-profit price or None if no levels filled
        """
        filled_levels = [lvl for lvl in self.active_dca_levels if lvl.filled]
        if not filled_levels:
            return None
        
        # Calculate position state variables
        # A = total invested capital
        total_invested = sum(lvl.budget_allocation for lvl in filled_levels)
        # Q = total position size (coins)
        total_coins = sum(lvl.budget_allocation / lvl.fill_price for lvl in filled_levels)
        
        # avg_price = A / Q
        avg_price = total_invested / total_coins
        
        # tp_price = avg_price * (1 + target_profit)
        # Equivalent to: (1 + target_profit) * A / Q
        tp_price = avg_price * (1 + self.take_profit_percent / 100)
        
        return tp_price
    
    def check_take_profit_hit(self, candle_high: float) -> bool:
        """
        Check if take-profit level was hit by candle high (wick touch)
        
        Execution rule: TP triggers if candle.high >= tp_price
        where tp_price guarantees portfolio-level target profit.
        
        Args:
            candle_high: High price of current candle
            
        Returns:
            True if take-profit was hit
        """
        tp_price = self.calculate_take_profit_price()
        if tp_price is None:
            return False
        return candle_high >= tp_price
    
    def close_trade(self, exit_time: pd.Timestamp, exit_price: float) -> Trade:
        """
        Close current trade and calculate final results
        
        Position state at close:
        - A = total invested capital
        - Q = total position size
        - P = exit price (take-profit price)
        
        Profit calculation:
        - total_return = Q * P
        - pnl = total_return - A = Q * P - A
        - pnl_pct = pnl / A
        
        Args:
            exit_time: Timestamp of exit
            exit_price: Price at exit (take-profit price)
            
        Returns:
            Trade object with complete trade information
        """
        deepest_level = self.get_deepest_filled_level()
        filled_levels = [lvl for lvl in self.active_dca_levels if lvl.filled]
        
        # Calculate position state: A (total invested) and Q (total coins)
        total_invested = sum(lvl.budget_allocation for lvl in filled_levels)  # A
        total_coins = sum(lvl.budget_allocation / lvl.fill_price for lvl in filled_levels)  # Q
        
        # Calculate return: total_return = Q * P
        total_return = total_coins * exit_price
        
        # Calculate profit: pnl = Q * P - A
        profit_loss = total_return - total_invested
        
        # Calculate profit percentage: pnl_pct = pnl / A
        profit_percent = (profit_loss / total_invested) * 100 if total_invested > 0 else 0
        
        trade = Trade(
            start_time=self.trade_start_time,
            end_time=exit_time,
            anchor_price=self.anchor_price,
            deepest_level=deepest_level.level_num,
            deepest_price=deepest_level.fill_price,
            exit_price=exit_price,
            total_invested=total_invested,
            total_return=total_return,
            profit_loss=profit_loss,
            profit_percent=profit_percent,
            dca_levels_filled=[lvl.level_num for lvl in filled_levels]
        )
        
        self.completed_trades.append(trade)
        return trade
    
    def calculate_stop_loss_threshold(self) -> Optional[float]:
        """
        Calculate the price threshold that triggers stop-loss.
        
        ONLY valid when:
        - stop_loss_percent > 0 (enabled)
        - At least one DCA level is filled
        
        Formula:
        - threshold = anchor_price * (1 - stop_loss_percent/100)
        
        Returns:
            float: Stop-loss price threshold
            None: If stop-loss disabled or no position open
        """
        if self.stop_loss_percent <= 0:
            return None
        
        filled_levels = [lvl for lvl in self.active_dca_levels if lvl.filled]
        if not filled_levels:
            return None
        
        threshold = self.anchor_price * (1 - self.stop_loss_percent / 100)
        return threshold
    
    def check_stop_loss_hit(self, candle_low: float) -> bool:
        """
        Check if stop-loss threshold was breached by candle wick.
        
        Execution rule: SL triggers if candle.low <= threshold
        
        Args:
            candle_low: Lowest price of current candle
            
        Returns:
            bool: True if stop-loss was triggered
        """
        threshold = self.calculate_stop_loss_threshold()
        
        if threshold is None:
            return False
        
        return candle_low <= threshold
    
    def close_trade_with_loss(
        self,
        exit_time: pd.Timestamp,
        loss_price: float,
        available_budget: float
    ) -> tuple[Trade, float]:
        """
        Close trade due to stop-loss being triggered.
        
        Loss Calculation:
        - loss_pct = (loss_price - anchor_price) / anchor_price * 100
        - capital_loss = abs(loss_pct / 100) * total_invested
        - updated_budget = available_budget - capital_loss
        
        Args:
            exit_time: Timestamp of stop-loss execution
            loss_price: Price at which stop-loss executed
            available_budget: Current available capital before loss
            
        Returns:
            tuple[Trade, float]: (Trade object with loss record, updated_budget)
        """
        filled_levels = [lvl for lvl in self.active_dca_levels if lvl.filled]
        deepest_level = self.get_deepest_filled_level()
        
        # Calculate total invested capital
        total_invested = sum(lvl.budget_allocation for lvl in filled_levels)
        
        # Calculate unrealized loss percentage at stop price
        loss_pct = ((loss_price - self.anchor_price) / self.anchor_price) * 100
        
        # Calculate actual capital loss amount
        capital_loss = abs(loss_pct / 100) * total_invested
        
        # Update available budget
        updated_budget = available_budget - capital_loss
        
        # Create trade record
        trade = Trade(
            start_time=self.trade_start_time,
            end_time=exit_time,
            anchor_price=self.anchor_price,
            deepest_level=deepest_level.level_num,
            deepest_price=deepest_level.fill_price,
            exit_price=loss_price,
            total_invested=total_invested,
            total_return=loss_price * sum(lvl.budget_allocation / lvl.fill_price 
                                          for lvl in filled_levels),
            profit_loss=-capital_loss,
            profit_percent=loss_pct,
            dca_levels_filled=[lvl.level_num for lvl in filled_levels],
            stop_loss_triggered=True,
            stop_loss_price=loss_price,
            stop_loss_loss=capital_loss,
            completion_reason="stop_loss"
        )
        
        self.completed_trades.append(trade)
        
        return trade, updated_budget
    
    def calculate_backtest_results(self) -> BacktestResults:
        """
        Calculate comprehensive backtest statistics.
        
        Recalculates ALL metrics considering stop-loss impact:
        - Available budget affected by losses
        - Win/loss/stop-out counts
        - Profit/loss totals
        - ROI and rates
        - Risk metrics
        
        Returns:
            BacktestResults object with all statistics
        """
        trades = self.completed_trades
        
        # Handle empty trades case
        if not trades:
            return BacktestResults(
                initial_budget=self.initial_budget,
                final_budget=self.initial_budget,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                stopped_out_trades=0,
                total_profit=0.0,
                total_loss=0.0,
                net_pnl=0.0,
                total_roi=0.0,
                win_rate=0.0,
                stop_loss_rate=0.0,
                avg_trade_pnl=0.0,
                largest_loss=0.0,
                largest_profit=0.0,
                avg_loss_magnitude=0.0,
                avg_profit_magnitude=0.0,
                avg_trade_duration_days=0.0,
                total_test_days=0.0
            )
        
        # Separate trades by outcome
        winning_trades = [t for t in trades if t.profit_loss > 0]
        losing_trades = [t for t in trades if t.profit_loss < 0]
        stopped_out_trades = [t for t in trades if t.stop_loss_triggered]
        
        # Profit/Loss totals
        total_profit = sum(t.profit_loss for t in winning_trades)
        total_loss = abs(sum(t.profit_loss for t in losing_trades))
        net_pnl = sum(t.profit_loss for t in trades)
        
        # Final budget = initial + all P&L
        final_budget = self.initial_budget + net_pnl
        
        # ROI = (final - initial) / initial * 100
        total_roi = ((final_budget - self.initial_budget) / self.initial_budget * 100) \
            if self.initial_budget > 0 else 0.0
        
        # Counts and rates
        total_trades = len(trades)
        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0.0
        stop_loss_rate = (len(stopped_out_trades) / total_trades * 100) if total_trades > 0 else 0.0
        
        # Averages
        avg_trade_pnl = net_pnl / total_trades if total_trades > 0 else 0.0
        avg_loss_magnitude = total_loss / len(losing_trades) if losing_trades else 0.0
        avg_profit_magnitude = total_profit / len(winning_trades) if winning_trades else 0.0
        
        # Extremes
        largest_loss = min(t.profit_loss for t in trades) if trades else 0.0
        largest_profit = max(t.profit_loss for t in trades) if trades else 0.0
        
        # Duration metrics
        durations_days = [(t.end_time - t.start_time).total_seconds() / (24 * 3600) 
                          for t in trades]
        avg_trade_duration_days = sum(durations_days) / len(durations_days) if durations_days else 0.0
        
        if trades:
            total_test_days = (trades[-1].end_time - trades[0].start_time).total_seconds() / (24 * 3600)
        else:
            total_test_days = 0.0
        
        return BacktestResults(
            initial_budget=self.initial_budget,
            final_budget=final_budget,
            total_trades=total_trades,
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            stopped_out_trades=len(stopped_out_trades),
            total_profit=total_profit,
            total_loss=total_loss,
            net_pnl=net_pnl,
            total_roi=total_roi,
            win_rate=win_rate,
            stop_loss_rate=stop_loss_rate,
            avg_trade_pnl=avg_trade_pnl,
            largest_loss=largest_loss,
            largest_profit=largest_profit,
            avg_loss_magnitude=avg_loss_magnitude,
            avg_profit_magnitude=avg_profit_magnitude,
            avg_trade_duration_days=avg_trade_duration_days,
            total_test_days=total_test_days
        )
    
    def run_backtest(self, df: pd.DataFrame) -> List[Trade]:
        """
        Run deterministic backtest on historical data with stop-loss support.
        
        MODIFIED ALGORITHM:
        1. Track anchor price (reference price P0)
        2. When candle.low <= P0 * (1 + first_dca_level), start trade
        3. For each candle in trade:
           a. Check DCA fills: if candle.low <= pi, fill level i
           b. Check STOP-LOSS: if candle.low <= threshold, close with loss
           c. Check TP: if candle.high >= tp_price, close with profit
        4. After close, reset position and start new anchor
        5. Track available_budget throughout
        
        Args:
            df: DataFrame with columns: datetime, high, low
               (only wicks are used, open/close ignored)
               
        Returns:
            List of completed Trade objects
        """
        self.reset_game()
        self.completed_trades = []
        self.available_budget = self.initial_budget
        
        for idx, row in df.iterrows():
            timestamp = row['datetime']
            candle_high = row['high']
            candle_low = row['low']
            
            if not self.in_trade:
                # Not in trade - looking for entry trigger
                if self.anchor_price is None:
                    # First candle becomes anchor (P0)
                    self.anchor_price = candle_high
                    continue
                
                # Check if price dumped to first DCA level or beyond
                dump_from_anchor = ((candle_low - self.anchor_price) / self.anchor_price) * 100
                
                if dump_from_anchor <= self.dca_levels[0]:  # e.g., -6%
                    # Start new trade
                    self.in_trade = True
                    self.trade_start_time = timestamp
                    # Calculate all DCA entry prices: pi = P0 * (1 + di)
                    self.active_dca_levels = self.calculate_dca_prices(self.anchor_price)
                    
                    # Check fills on this candle
                    self.check_dca_fills(candle_low)
                else:
                    # Update anchor to current high if no dump yet
                    self.anchor_price = max(self.anchor_price, candle_high)
                    
            else:
                # In trade - check for events in order
                
                # STEP 1: Check DCA fills (price going down touches low)
                newly_filled = self.check_dca_fills(candle_low)
                
                # STEP 2: Check STOP-LOSS (NEW - BEFORE TP)
                if self.check_stop_loss_hit(candle_low):
                    sl_threshold = self.calculate_stop_loss_threshold()
                    trade, self.available_budget = self.close_trade_with_loss(
                        timestamp,
                        sl_threshold,
                        self.available_budget
                    )
                    
                    # Reset and start looking for next trade
                    self.reset_game()
                    self.anchor_price = candle_high
                    continue  # Skip TP check for this candle
                
                # STEP 3: Check TAKE-PROFIT (only if not stopped out)
                if self.check_take_profit_hit(candle_high):
                    tp_price = self.calculate_take_profit_price()
                    trade = self.close_trade(timestamp, tp_price)
                    
                    # Update available budget with profit
                    self.available_budget += trade.profit_loss
                    
                    # Reset and start looking for next trade
                    # Position resets completely after TP
                    self.reset_game()
                    self.anchor_price = candle_high  # New anchor after trade closes
        
        return self.completed_trades
    
    def print_trade_summary(self, trade: Trade):
        """Print summary of a single trade (deprecated - use _print_trade_details)"""
        self._print_trade_details(self.completed_trades.index(trade) + 1, trade)
        
    def print_backtest_results(self):
        """Print comprehensive backtest results with stop-loss metrics"""
        results = self.calculate_backtest_results()
        
        if not self.completed_trades:
            print("\nNo completed trades found.")
            return
        
        print(f"\n{'='*70}")
        print(f"BACKTEST RESULTS - DCA STRATEGY")
        print(f"{'='*70}")
        print(f"Initial Budget:     ${results.initial_budget:,.2f}")
        print(f"Final Budget:       ${results.final_budget:,.2f}")
        print(f"Total P&L:          ${results.net_pnl:,.2f}")
        print(f"ROI:                {results.total_roi:+.2f}%")
        
        print(f"\n{'Trade Summary':^70}")
        print(f"{'-'*70}")
        print(f"Total Trades:       {results.total_trades}")
        print(f"  Winning:          {results.winning_trades} ({results.win_rate:.1f}%)")
        print(f"  Losing:           {results.losing_trades} ({100-results.win_rate:.1f}%)")
        print(f"  Stopped Out:      {results.stopped_out_trades} ({results.stop_loss_rate:.1f}%)")
        
        print(f"\n{'Profit & Loss':^70}")
        print(f"{'-'*70}")
        print(f"Total Profit:       ${results.total_profit:,.2f}")
        print(f"Total Loss:         -${results.total_loss:,.2f}")
        print(f"Avg Trade P&L:      ${results.avg_trade_pnl:,.2f}")
        
        if results.winning_trades > 0:
            print(f"Avg Win:            ${results.avg_profit_magnitude:,.2f}")
        if results.losing_trades > 0:
            print(f"Avg Loss:           -${results.avg_loss_magnitude:,.2f}")
        
        print(f"Largest Profit:     ${results.largest_profit:,.2f}")
        print(f"Largest Loss:       ${results.largest_loss:,.2f}")
        
        print(f"\n{'Duration':^70}")
        print(f"{'-'*70}")
        print(f"Avg Trade Duration: {results.avg_trade_duration_days:.2f} days")
        print(f"Total Test Period:  {results.total_test_days:.2f} days")
        
        # Print stop-loss info if enabled
        if self.stop_loss_percent > 0:
            print(f"\n{'Stop-Loss (Enabled)':^70}")
            print(f"{'-'*70}")
            print(f"Stop-Loss %:        {self.stop_loss_percent}%")
            print(f"Stopped Out:        {results.stopped_out_trades} trades")
            total_sl_loss = sum(t.stop_loss_loss for t in self.completed_trades 
                               if t.stop_loss_triggered)
            print(f"Total SL Loss:      -${total_sl_loss:,.2f}")
        else:
            print(f"\n{'Stop-Loss (Disabled)':^70}")
        
        # Print individual trades
        print(f"\n{'='*70}")
        print(f"TRADE DETAILS")
        print(f"{'='*70}")
        for i, trade in enumerate(self.completed_trades, 1):
            self._print_trade_details(i, trade)
    
    def _print_trade_details(self, trade_num: int, trade: Trade):
        """Print details for a single trade"""
        duration = trade.end_time - trade.start_time
        duration_days = duration.total_seconds() / (24 * 3600)
        
        completion_status = "🛑 STOP-LOSS" if trade.stop_loss_triggered else "✓ TAKE-PROFIT"
        profit_color = "📈" if trade.profit_loss > 0 else "📉"
        
        print(f"\n{'─'*70}")
        print(f"Trade #{trade_num} - {completion_status}")
        print(f"{'─'*70}")
        print(f"Period:             {trade.start_time} → {trade.end_time}")
        print(f"Duration:           {duration_days:.2f} days")
        print(f"Anchor Price:       ${trade.anchor_price:,.2f}")
        print(f"Deepest Level:      DCA-{trade.deepest_level} (${trade.deepest_price:,.2f})")
        print(f"DCA Levels Filled:  {trade.dca_levels_filled}")
        print(f"Total Invested:     ${trade.total_invested:,.2f}")
        print(f"Exit Price:         ${trade.exit_price:,.2f}")
        print(f"Result:             {profit_color} ${trade.profit_loss:,.2f} ({trade.profit_percent:+.2f}%)")
        
        if trade.stop_loss_triggered:
            print(f"Stop-Loss Price:    ${trade.stop_loss_price:,.2f}")
            print(f"SL Loss Amount:     -${trade.stop_loss_loss:,.2f}")


def load_and_prepare_data(csv_file: str) -> pd.DataFrame:
    """
    Load CSV data and prepare for strategy
    
    Args:
        csv_file: Path to CSV file
        
    Returns:
        DataFrame with datetime, high, low columns
    """
    print(f"Loading {csv_file}...")
    df = pd.read_csv(csv_file)
    
    # Standardize column names
    df.columns = df.columns.str.strip().str.lower()
    
    # Set datetime
    if 'open time' in df.columns:
        df['datetime'] = pd.to_datetime(df['open time'])
    elif 'timestamp' in df.columns:
        df['datetime'] = pd.to_datetime(df['timestamp'])
    else:
        raise ValueError("No timestamp column found")
    
    # Ensure datetime is timezone-naive (no conversion)
    if df['datetime'].dt.tz is not None:
        df['datetime'] = df['datetime'].dt.tz_localize(None)
    
    df = df.dropna(subset=['datetime'])
    
    # Only need datetime, high, low (wicks only)
    df = df[['datetime', 'high', 'low']].copy()
    
    print(f"Loaded {len(df)} candles")
    print(f"Date range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
    
    return df


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 dca_strategy.py data/btc_15m_data_2018_to_2025.csv")
        print("  python3 dca_strategy.py data/btc_1h_data_2018_to_2025.csv 5000")
        print("\nArguments:")
        print("  csv_file      Path to CSV file with OHLC data")
        print("  budget        Initial budget (default: 1000)")
        print("\nStrategy Parameters:")
        print("  DCA Levels:   -6%, -9%, -12%, -16%, -20%, -24%")
        print("  Take Profit:  +5% from deepest filled level")
        print("  Uses:         Only candle wicks (high/low)")
        return
    
    csv_file = sys.argv[1]
    initial_budget = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    
    # Load data
    df = load_and_prepare_data(csv_file)
    
    # Initialize strategy
    strategy = DCAStrategy(initial_budget=initial_budget)
    
    print(f"\nRunning DCA Strategy Backtest...")
    print(f"Initial Budget: ${initial_budget:,.2f}")
    print(f"DCA Levels: {strategy.dca_levels}")
    print(f"Take Profit: +{strategy.take_profit_percent}%")
    
    # Run backtest
    trades = strategy.run_backtest(df)
    
    # Print results
    strategy.print_backtest_results()


if __name__ == '__main__':
    main()