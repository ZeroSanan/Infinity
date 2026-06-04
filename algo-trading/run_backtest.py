"""
Run DCA Strategy Backtest using configuration file
"""
import sys
import pandas as pd
from dca_strategy import DCAStrategy, load_and_prepare_data
from config_loader import load_config


def run_backtest_from_config(config_file="strategy_config.json"):
    """
    Run DCA backtest using configuration file
    
    Args:
        config_file: Path to configuration JSON file
    """
    # Load configuration
    print("Loading configuration...")
    config = load_config(config_file)
    config.print_config()
    
    # Load data
    df = load_and_prepare_data(config.csv_file)
    
    # Filter by date range if specified
    if config.start_date:
        start = pd.to_datetime(config.start_date)
        df = df[df['datetime'] >= start]
        print(f"Filtered to start date: {start}")
    
    if config.end_date:
        end = pd.to_datetime(config.end_date)
        df = df[df['datetime'] <= end]
        print(f"Filtered to end date: {end}")
    
    print(f"\nBacktest data: {len(df)} candles")
    print(f"Date range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
    
    # Create strategy with config parameters
    print(f"\nInitializing strategy...")
    strategy = DCAStrategy(
        initial_budget=config.initial_budget,
        budget_per_level=config.budget_allocation,
        dca_levels=config.dca_levels,
        take_profit_percent=config.take_profit_percent,
        stop_loss_percent=config.stop_loss_percent,
    )
    
    # Run backtest
    print(f"\nRunning backtest...")
    trades = strategy.run_backtest(df)
    
    # Print results
    strategy.print_backtest_results()
    
    return strategy, trades


def main():
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = "strategy_config.json"
    
    print(f"{'='*70}")
    print(f"DCA STRATEGY BACKTEST - CONFIG-BASED")
    print(f"{'='*70}")
    print(f"Config file: {config_file}\n")
    
    try:
        strategy, trades = run_backtest_from_config(config_file)
        
        print(f"\n{'='*70}")
        print(f"BACKTEST COMPLETED SUCCESSFULLY")
        print(f"{'='*70}")
        print(f"Total trades: {len(trades)}")
        if trades:
            total_pnl = sum(t.profit_loss for t in trades)
            print(f"Total P&L: ${total_pnl:,.2f}")
            print(f"ROI: {(total_pnl / strategy.initial_budget) * 100:.2f}%")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()