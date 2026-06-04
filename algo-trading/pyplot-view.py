"""
Interactive chart viewer for backtest results with trade markers
"""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sys
from pathlib import Path
from dca_strategy import DCAStrategy, load_and_prepare_data
from config_loader import load_config


def plot_backtest_chart(config_file="strategy_config.json"):
    """
    Plot interactive chart with backtest trades marked
    
    Args:
        config_file: Path to configuration JSON file
    """
    # Load configuration and run backtest
    print("Loading configuration...")
    config = load_config(config_file)
    
    # Load full data for charting (with OHLCV)
    print(f"Loading {config.csv_file}...")
    df_full = pd.read_csv(config.csv_file)
    df_full.columns = df_full.columns.str.strip().str.lower()
    
    # Set datetime
    if 'open time' in df_full.columns:
        df_full['datetime'] = pd.to_datetime(df_full['open time'])
    elif 'timestamp' in df_full.columns:
        df_full['datetime'] = pd.to_datetime(df_full['timestamp'])
    
    # Ensure datetime is timezone-naive (no conversion)
    if df_full['datetime'].dt.tz is not None:
        df_full['datetime'] = df_full['datetime'].dt.tz_localize(None)
    
    df_full = df_full.dropna(subset=['datetime'])
    print(f"Loaded {len(df_full)} candles")
    print(f"Date range: {df_full['datetime'].iloc[0]} to {df_full['datetime'].iloc[-1]}")
    
    # Load simplified data for backtest (only high/low)
    df_backtest = load_and_prepare_data(config.csv_file)
    
    # Filter both datasets by date range if specified
    if config.start_date:
        start = pd.to_datetime(config.start_date)
        df_full = df_full[df_full['datetime'] >= start]
        df_backtest = df_backtest[df_backtest['datetime'] >= start]
    
    if config.end_date:
        end = pd.to_datetime(config.end_date)
        df_full = df_full[df_full['datetime'] <= end]
        df_backtest = df_backtest[df_backtest['datetime'] <= end]
    
    print(f"Filtered to {len(df_full)} candles")
    
    # Run backtest
    print("\nRunning backtest...")
    strategy = DCAStrategy(
        initial_budget=config.initial_budget,
        budget_per_level=config.budget_allocation,
        dca_levels=config.dca_levels,
        take_profit_percent=config.take_profit_percent,
        stop_loss_percent=config.stop_loss_percent
    )
    
    trades = strategy.run_backtest(df_backtest)
    print(f"Found {len(trades)} trades\n")
    
    # Use full dataframe for plotting
    df = df_full
    
    # Create chart
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
        subplot_titles=['Price with Trades', 'Volume']
    )
    
    # Add candlestick chart
    fig.add_trace(
        go.Candlestick(
            x=df['datetime'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            name='OHLC',
            increasing_line_color='#26a69a',
            decreasing_line_color='#ef5350'
        ),
        row=1, col=1
    )
    
    # Add volume bars
    colors = ['#26a69a' if close >= open else '#ef5350' 
              for close, open in zip(df['close'], df['open'])]
    
    fig.add_trace(
        go.Bar(
            x=df['datetime'],
            y=df['volume'],
            name='Volume',
            marker_color=colors,
            showlegend=False
        ),
        row=2, col=1
    )
    
    # Add trade markers
    for i, trade in enumerate(trades, 1):
        # Trade start marker (anchor price)
        fig.add_trace(
            go.Scatter(
                x=[trade.start_time],
                y=[trade.anchor_price],
                mode='markers',
                marker=dict(
                    symbol='circle',
                    size=12,
                    color='yellow',
                    line=dict(color='white', width=2)
                ),
                name=f'Trade {i} Start',
                showlegend=True,
                hovertemplate=f'<b>Trade {i} Start</b><br>' +
                             f'Anchor: ${trade.anchor_price:,.2f}<br>' +
                             f'Time: {trade.start_time.strftime("%Y-%m-%d %H:%M")}<extra></extra>'
            ),
            row=1, col=1
        )
        
        # DCA entry markers
        # Get the filled DCA levels from the trade
        for level_num in trade.dca_levels_filled:
            # Calculate DCA entry price
            dump_pct = config.dca_levels[level_num - 1]
            entry_price = trade.anchor_price * (1 + dump_pct / 100)
            
            fig.add_trace(
                go.Scatter(
                    x=[trade.start_time],  # Use start time for DCA markers
                    y=[entry_price],
                    mode='markers',
                    marker=dict(
                        symbol='triangle-down',
                        size=10,
                        color='red',
                        line=dict(color='white', width=1)
                    ),
                    name=f'Trade {i} DCA-{level_num}',
                    showlegend=True,
                    hovertemplate=f'<b>DCA-{level_num} ({dump_pct}%)</b><br>' +
                                 f'Price: ${entry_price:,.2f}<br>' +
                                 f'Budget: ${config.budget_allocation[level_num-1]:,.2f}<extra></extra>'
                ),
                row=1, col=1
            )
        
        # Trade exit marker (take profit or stop loss)
        exit_marker_color = '#EF5350' if trade.stop_loss_triggered else '#00FF00'
        exit_marker_symbol = 'x' if trade.stop_loss_triggered else 'triangle-up'
        exit_label = 'SL' if trade.stop_loss_triggered else 'TP'
        
        fig.add_trace(
            go.Scatter(
                x=[trade.end_time],
                y=[trade.exit_price],
                mode='markers',
                marker=dict(
                    symbol=exit_marker_symbol,
                    size=12,
                    color=exit_marker_color,
                    line=dict(color='white', width=2)
                ),
                name=f'Trade {i} {exit_label}',
                showlegend=True,
                hovertemplate=f'<b>Trade {i} {exit_label}</b><br>' +
                             f'Exit: ${trade.exit_price:,.2f}<br>' +
                             f'P&L: ${trade.profit_loss:,.2f} ({trade.profit_percent:.2f}%)<br>' +
                             f'Time: {trade.end_time.strftime("%Y-%m-%d %H:%M")}<extra></extra>'
            ),
            row=1, col=1
        )
        
        # Draw line connecting trade start to exit
        fig.add_trace(
            go.Scatter(
                x=[trade.start_time, trade.end_time],
                y=[trade.anchor_price, trade.exit_price],
                mode='lines',
                line=dict(color='cyan', width=1, dash='dot'),
                showlegend=False,
                hoverinfo='skip'
            ),
            row=1, col=1
        )
    
    # Update layout
    fig.update_layout(
        title=f'DCA Strategy Backtest - {Path(config.csv_file).stem}<br>' +
              f'<sub>Trades: {len(trades)} | Total P&L: ${sum(t.profit_loss for t in trades):,.2f} | ' +
              f'ROI: {(sum(t.profit_loss for t in trades) / config.initial_budget) * 100:.2f}%</sub>',
        xaxis_rangeslider_visible=False,
        height=800,
        template='plotly_dark',
        hovermode='closest',
        showlegend=True,
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="left",
            x=0.01
        )
    )
    
    # Update axes
    fig.update_xaxes(title_text="Date", row=2, col=1)
    fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    
    # Fix timezone - ensure dates are displayed as-is without conversion
    fig.update_xaxes(type='date', tickformat='%Y-%m-%d %H:%M')
    
    print("\n✓ Opening interactive chart in browser...")
    print("  🟡 Yellow circles = Trade start (anchor)")
    print("  🔴 Red triangles = DCA entries")
    print("  🟢 Green triangles = Take profit exits")
    print("  📈 Cyan lines = Trade duration")
    print("\nInteractive controls:")
    print("  • Hover over markers to see details")
    print("  • Click and drag to zoom")
    print("  • Double-click to reset zoom")
    print("  • Click legend items to show/hide")
    
    # Show the plot
    fig.show()


def main():
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = "strategy_config.json"
    
    print(f"{'='*70}")
    print(f"DCA STRATEGY BACKTEST CHART VIEWER")
    print(f"{'='*70}")
    print(f"Config file: {config_file}\n")
    
    try:
        plot_backtest_chart(config_file)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()