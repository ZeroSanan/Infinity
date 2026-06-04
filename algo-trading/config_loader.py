"""
Load and validate strategy configuration from JSON file
"""
import json
from pathlib import Path
from typing import List, Optional, Dict, Any


class StrategyConfig:
    """Strategy configuration loader and validator"""
    
    def __init__(self, config_file: str = "strategy_config.json"):
        """
        Load configuration from JSON file
        
        Args:
            config_file: Path to JSON config file
        """
        self.config_file = config_file
        self.config = self._load_config()
        self._validate_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load JSON configuration file"""
        config_path = Path(self.config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_file}")
        
        with open(config_path, 'r') as f:
            return json.load(f)
    
    def _validate_config(self):
        """Validate configuration values"""
        # Validate DCA levels
        levels = self.config['dca_levels']
        if len(levels) < 1:
            raise ValueError("Must have at least 1 DCA level")
        
        if not all(l < 0 for l in levels):
            raise ValueError("All DCA levels must be negative percentages")
        
        if levels != sorted(levels, reverse=True):
            raise ValueError("DCA levels must be in descending order (e.g., -6, -9, -12...)")
        
        # Validate DCA allocations if provided - auto-adjust if mismatch
        if 'dca_allocations' in self.config:
            allocations = self.config['dca_allocations']
            if not all(a > 0 for a in allocations):
                raise ValueError("All DCA allocations must be positive")
            
            # If allocation length doesn't match levels, auto-generate to match
            if len(allocations) != len(levels):
                print(f"⚠️  DCA allocations length ({len(allocations)}) doesn't match levels ({len(levels)})")
                print(f"   Auto-generating allocations to match {len(levels)} levels...")
                # Don't raise error - let budget_allocation property handle it
        
        # Validate budget
        if self.config['initial_budget'] <= 0:
            raise ValueError("Initial budget must be positive")
        
        # Validate take profit
        if self.config['take_profit_percent'] <= 0:
            raise ValueError("Take profit percent must be positive")
        
        # Validate stop loss (NEW)
        stop_loss = self.config.get('stop_loss_percent', 0.0)
        if stop_loss < 0:
            raise ValueError("stop_loss_percent must be >= 0 (0 = disabled)")
        
        # Optional: warn if stop_loss >= 50% (too aggressive)
        if stop_loss >= 50:
            print(f"⚠️  WARNING: stop_loss_percent = {stop_loss}% is very aggressive")
    
    @property
    def csv_file(self) -> str:
        """Get CSV file path"""
        return self.config['csv_file']
    
    @property
    def start_date(self) -> Optional[str]:
        """Get backtest start date"""
        start = self.config.get('start_date', '')
        return start if start else None
    
    @property
    def end_date(self) -> Optional[str]:
        """Get backtest end date"""
        end = self.config.get('end_date', '')
        return end if end else None
    
    @property
    def initial_budget(self) -> float:
        """Get initial budget"""
        return self.config['initial_budget']
    
    @property
    def budget_allocation(self) -> List[float]:
        """Get budget allocation per DCA level (custom or % of initial budget)"""
        if 'dca_allocations' in self.config:
            allocations = self.config['dca_allocations']
            # If lengths match, use as-is
            if len(allocations) == len(self.dca_levels):
                return allocations
            # If lengths don't match, auto-generate to match levels
            else:
                return self._generate_allocations()
        # Default: allocation is abs(level)% of initial budget
        # Example: level -10 => 10% of initial_budget
        return self._generate_allocations()
    
    def _generate_allocations(self) -> List[float]:
        """Generate allocations based on levels or initial budget"""
        budget = self.initial_budget
        # If initial budget is large enough, use exponential scaling
        # Otherwise use percentage-based scaling
        if budget >= 100:
            # For larger budgets: exponential scaling (1, 2, 4, 8, 16, ...)
            return [budget / (2 ** i) for i in range(len(self.dca_levels))]
        else:
            # For smaller budgets: percentage-based (abs(level)%)
            return [budget * (abs(level) / 100.0) for level in self.dca_levels]
    
    @property
    def dca_levels(self) -> List[float]:
        """Get DCA dump levels"""
        return self.config['dca_levels']
    
    @property
    def take_profit_percent(self) -> float:
        """Get take profit percentage"""
        return self.config['take_profit_percent']
    
    @property
    def stop_loss_percent(self) -> float:
        """
        Get stop-loss percentage.
        
        Returns:
            float: Stop-loss percentage (0 = disabled, >0 = enabled)
                    E.g., 10 means stop loss at -10% from entry
        """
        return self.config.get('stop_loss_percent', 0.0)
    
    def print_config(self):
        """Print configuration summary"""
        print(f"\n{'='*70}")
        print(f"STRATEGY CONFIGURATION")
        print(f"{'='*70}")
        print(f"CSV File:           {self.csv_file}")
        print(f"Date Range:         {self.start_date or 'Start'} → {self.end_date or 'End'}")
        print(f"Initial Budget:     ${self.initial_budget:,.2f}")
        print(f"Stop Loss Percent:  {self.stop_loss_percent}% "
              f"({'DISABLED' if self.stop_loss_percent == 0 else 'ENABLED'})")
        print(f"DCA Levels:         {self.dca_levels}")
        allocations = self.budget_allocation
        print(f"DCA Allocations:    {[f'${a:,.0f}' for a in allocations]}")
        print(f"Allocation Rule:    abs(level)% of initial budget")
        print(f"Take Profit:        +{self.take_profit_percent}%")
        print(f"\nDCA Table:")
        print(f"{'Level':<8} {'Dump %':<10} {'Order ($)':<12}")
        print(f"{'-'*30}")
        for i, (lvl, alloc) in enumerate(zip(self.dca_levels, allocations), 1):
            print(f"{i:<8} {lvl:<10} ${alloc:,.0f}")
        print(f"{'='*70}\n")


def load_config(config_file: str = "strategy_config.json") -> StrategyConfig:
    """
    Load strategy configuration
    
    Args:
        config_file: Path to config file
        
    Returns:
        StrategyConfig object
    """
    return StrategyConfig(config_file)


if __name__ == '__main__':
    # Test configuration loading
    config = load_config()
    config.print_config()