"""
Interactive chart viewer for backtest results with trade markers using TradingView Lightweight Charts
Official documentation: https://tradingview.github.io/lightweight-charts/docs
"""
import pandas as pd
import json
import sys
import webbrowser
from pathlib import Path
from dca_strategy import DCAStrategy, load_and_prepare_data
from config_loader import load_config


def plot_backtest_chart(config_file="strategy_config.json"):
    """
    Plot interactive chart with backtest trades marked using TradingView Lightweight Charts
    
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
    
    # Drop rows with any null values in OHLCV columns
    df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
    
    # Ensure all numeric columns are valid and positive
    df = df[(df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0) & (df['close'] > 0) & (df['volume'] >= 0)]
    
    # Sort by datetime to ensure chronological order
    df = df.sort_values('datetime').reset_index(drop=True)
    
    # Additional validation: ensure high >= low, high >= open, high >= close, low <= open, low <= close
    df = df[(df['high'] >= df['low']) & 
            (df['high'] >= df['open']) & 
            (df['high'] >= df['close']) & 
            (df['low'] <= df['open']) & 
            (df['low'] <= df['close'])]
    
    print(f"Valid candles for charting: {len(df)}")
    
    # Prepare candlestick data for TradingView Lightweight Charts
    # For intraday data (15m, 1h, 4h), use Unix timestamps in seconds
    # For daily data, use YYYY-MM-DD string format
    candles = []
    seen_times = set()
    
    for _, row in df.iterrows():
        try:
            # Use Unix timestamp in seconds for intraday data
            time_value = int(row['datetime'].timestamp())
            
            # Skip duplicate timestamps
            if time_value in seen_times:
                continue
            seen_times.add(time_value)
            
            # Validate OHLC relationships
            o, h, l, c = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
            
            if not (h >= max(o, c) and l <= min(o, c) and h >= l):
                continue
            
            candles.append({
                'time': time_value,
                'open': o,
                'high': h,
                'low': l,
                'close': c
            })
        except (ValueError, TypeError, OverflowError) as e:
            continue
    
    print(f"Prepared {len(candles)} valid candles")
    
    # Prepare volume data
    volume = []
    seen_volume_times = set()
    
    for _, row in df.iterrows():
        try:
            time_value = int(row['datetime'].timestamp())
            
            # Skip duplicate timestamps
            if time_value in seen_volume_times:
                continue
            seen_volume_times.add(time_value)
            
            vol = float(row['volume'])
            if vol < 0 or not (0 <= vol < float('inf')):
                continue
                
            volume.append({
                'time': time_value,
                'value': vol
            })
        except (ValueError, TypeError, OverflowError) as e:
            continue
    
    print(f"Prepared {len(volume)} valid volume bars")
    
    # Prepare trade markers
    markers = []
    
    for i, trade in enumerate(trades, 1):
        trade_start_time = int(trade.start_time.timestamp())
        trade_end_time = int(trade.end_time.timestamp())
        
        # Anchor marker (yellow circle)
        markers.append({
            'time': trade_start_time,
            'position': 'inBar',
            'color': '#FFD700',
            'shape': 'circle',
            'text': f'T{i}'
        })
        
        # DCA entry markers (red arrows pointing up)
        for level_num in trade.dca_levels_filled:
            dump_pct = config.dca_levels[level_num - 1]
            markers.append({
                'time': trade_start_time,
                'position': 'belowBar',
                'color': '#ef5350',
                'shape': 'arrowUp',
                'text': f'DCA{level_num} ({dump_pct:+.1f}%)'
            })
        
        # Exit marker (green for TP, red for SL)
        exit_profit_pct = ((trade.exit_price - trade.anchor_price) / trade.anchor_price) * 100
        
        if trade.stop_loss_triggered:
            # Stop-Loss exit (red arrow pointing down)
            markers.append({
                'time': trade_end_time,
                'position': 'aboveBar',
                'color': '#EF5350',
                'shape': 'arrowDown',
                'text': f'SL{i} ({exit_profit_pct:.1f}%)'
            })
        else:
            # Take-Profit exit (green arrow pointing down)
            markers.append({
                'time': trade_end_time,
                'position': 'aboveBar',
                'color': '#00FF00',
                'shape': 'arrowDown',
                'text': f'TP{i} (+{exit_profit_pct:.1f}%)'
            })
    
    # Calculate stats
    total_pnl = sum(t.profit_loss for t in trades)
    roi = (total_pnl / config.initial_budget) * 100 if config.initial_budget > 0 else 0
    wins = sum(1 for t in trades if t.profit_loss > 0)
    losses = sum(1 for t in trades if t.profit_loss < 0)
    stopped_out = sum(1 for t in trades if t.stop_loss_triggered)
    sl_losses = sum(t.stop_loss_loss for t in trades if t.stop_loss_triggered)
    
    # Create HTML with TradingView Lightweight Charts
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DCA Strategy Backtest - {Path(config.csv_file).stem}</title>
    <script src="https://unpkg.com/lightweight-charts@3.8.0/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background-color: #131722;
            color: #d1d4dc;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1600px;
            margin: 0 auto;
        }}
        
        .header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        
        h1 {{
            font-size: 28px;
            color: #ffffff;
            margin-bottom: 15px;
            font-weight: 600;
        }}
        
        .stats {{
            display: flex;
            justify-content: center;
            gap: 40px;
            margin-top: 15px;
        }}
        
        .stat {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 5px;
        }}
        
        .stat-label {{
            color: #787b86;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .stat-value {{
            color: #ffffff;
            font-size: 24px;
            font-weight: 700;
        }}
        
        .positive {{
            color: #26a69a;
        }}
        
        .negative {{
            color: #ef5350;
        }}
        
        .charts-container {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        
        #candle-info {{
            position: absolute;
            top: 10px;
            left: 10px;
            background: rgba(19, 23, 34, 0.85);
            padding: 10px 15px;
            border-radius: 6px;
            font-size: 13px;
            z-index: 100;
            pointer-events: none;
            display: flex;
            gap: 20px;
            align-items: center;
        }}
        
        #candle-info .info-item {{
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        
        #candle-info .label {{
            color: #787b86;
            font-size: 11px;
            text-transform: uppercase;
        }}
        
        #candle-info .value {{
            color: #ffffff;
            font-weight: 600;
            font-size: 14px;
        }}
        
        #candle-info .value.up {{
            color: #26a69a;
        }}
        
        #candle-info .value.down {{
            color: #ef5350;
        }}
        
        #price-chart {{
            height: 600px;
            position: relative;
        }}
        
        #volume-chart {{
            height: 150px;
            position: relative;
        }}
        
        .legend {{
            margin-top: 30px;
            padding: 20px;
            background: #1e222d;
            border-radius: 8px;
            border: 1px solid #2a2e39;
        }}
        
        .legend-title {{
            font-weight: 600;
            margin-bottom: 15px;
            font-size: 16px;
            color: #ffffff;
        }}
        
        .legend-items {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 12px;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
        }}
        
        .legend-marker {{
            width: 16px;
            height: 16px;
            border-radius: 50%;
            flex-shrink: 0;
        }}
        
        .legend-marker.circle {{
            background: #FFD700;
            box-shadow: 0 0 8px rgba(255, 215, 0, 0.5);
        }}
        
        .legend-marker.up {{
            width: 0;
            height: 0;
            border-left: 8px solid transparent;
            border-right: 8px solid transparent;
            border-bottom: 14px solid #ef5350;
            border-radius: 0;
        }}
        
        .legend-marker.down {{
            width: 0;
            height: 0;
            border-left: 8px solid transparent;
            border-right: 8px solid transparent;
            border-top: 14px solid #00FF00;
            border-radius: 0;
        }}
        
        .controls {{
            margin-top: 20px;
            padding: 15px;
            background: #1e222d;
            border-radius: 8px;
            border: 1px solid #2a2e39;
            font-size: 13px;
            color: #787b86;
        }}
        
        .controls strong {{
            color: #ffffff;
            display: block;
            margin-bottom: 8px;
        }}
        
        .controls ul {{
            list-style: none;
            padding-left: 0;
        }}
        
        .controls li {{
            padding: 4px 0;
        }}
        
        .controls li:before {{
            content: "•";
            color: #2962FF;
            font-weight: bold;
            display: inline-block;
            width: 1em;
            margin-left: 0;
        }}
        
        #measurement-info {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(30, 34, 45, 0.95);
            border: 2px solid #2962FF;
            border-radius: 8px;
            padding: 15px 20px;
            display: none;
            min-width: 250px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            z-index: 1000;
        }}
        
        #measurement-info.active {{
            display: block;
        }}
        
        #measurement-info .title {{
            font-weight: 600;
            color: #2962FF;
            margin-bottom: 10px;
            font-size: 14px;
        }}
        
        #measurement-info .item {{
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            font-size: 13px;
        }}
        
        #measurement-info .label {{
            color: #787b86;
        }}
        
        #measurement-info .value {{
            color: #ffffff;
            font-weight: 600;
        }}
        
        #measurement-info .change-positive {{
            color: #26a69a;
        }}
        
        #measurement-info .change-negative {{
            color: #ef5350;
        }}
    </style>
</head>
<body>
    <div id="measurement-info">
        <div class="title">📏 Measurement Tool</div>
        <div class="item">
            <span class="label">Start Price:</span>
            <span class="value" id="start-price">-</span>
        </div>
        <div class="item">
            <span class="label">End Price:</span>
            <span class="value" id="end-price">-</span>
        </div>
        <div class="item">
            <span class="label">Change:</span>
            <span class="value" id="price-change">-</span>
        </div>
        <div class="item">
            <span class="label">Percentage:</span>
            <span class="value" id="percentage-change">-</span>
        </div>
    </div>
    
    <div class="container">
        <div class="header">
            <div class="stats">
                <div class="stat">
                    <span class="stat-label">Trades</span>
                    <span class="stat-value">{len(trades)}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Wins</span>
                    <span class="stat-value positive">{wins}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Losses</span>
                    <span class="stat-value negative">{losses}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Stopped Out</span>
                    <span class="stat-value negative">{stopped_out}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">Total P&L</span>
                    <span class="stat-value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:,.2f}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">SL Loss</span>
                    <span class="stat-value {'positive' if sl_losses >= 0 else 'negative'}">${sl_losses:,.2f}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">ROI</span>
                    <span class="stat-value {'positive' if roi >= 0 else 'negative'}">{roi:.2f}%</span>
                </div>
            </div>
        </div>
        
        <div class="charts-container">
            <div id="price-chart">
                <div id="candle-info">
                    <div class="info-item">
                        <span class="label">O</span>
                        <span class="value" id="info-open">-</span>
                    </div>
                    <div class="info-item">
                        <span class="label">H</span>
                        <span class="value" id="info-high">-</span>
                    </div>
                    <div class="info-item">
                        <span class="label">L</span>
                        <span class="value" id="info-low">-</span>
                    </div>
                    <div class="info-item">
                        <span class="label">C</span>
                        <span class="value" id="info-close">-</span>
                    </div>
                    <div class="info-item">
                        <span class="label">Vol</span>
                        <span class="value" id="info-volume">-</span>
                    </div>
                </div>
            </div>
            <div id="volume-chart"></div>
        </div>
        
        <div class="legend">
            <div class="legend-title">📊 Chart Legend</div>
            <div class="legend-items">
                <div class="legend-item">
                    <div class="legend-marker circle"></div>
                    <span>Trade Start (Anchor Price)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-marker up"></div>
                    <span>DCA Entry (Buy on Dip)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-marker down"></div>
                    <span>Take Profit Exit</span>
                </div>
            </div>
        </div>
        
        <div class="controls">
            <strong>Interactive Controls:</strong>
            <ul>
                <li>Scroll wheel to zoom in/out</li>
                <li>Click and drag to pan horizontally</li>
                <li>Double-click to fit all data</li>
                <li>Hover over markers for trade details</li>
                <li><strong>Hold Shift + Click & Drag</strong> to measure price change between two points</li>
            </ul>
        </div>
    </div>

    <script>
        // Data from backtest
        const candleData = {json.dumps(candles)};
        const volumeData = {json.dumps(volume)};
        const markers = {json.dumps(markers)};
        const candleByTime = new Map(candleData.map(c => [c.time, c]));
        const volumeByTime = new Map(volumeData.map(v => [v.time, v.value]));

        function getCandleFromParam(param) {{
            if (!param) return undefined;
            const seriesData = param.seriesData && typeof param.seriesData.get === 'function'
                ? param.seriesData.get(candlestickSeries)
                : undefined;
            if (seriesData) return seriesData;
            const seriesPrices = param.seriesPrices && typeof param.seriesPrices.get === 'function'
                ? param.seriesPrices.get(candlestickSeries)
                : undefined;
            if (seriesPrices) return seriesPrices;
            if (typeof param.time === 'number') {{
                return candleByTime.get(param.time);
            }}
            return undefined;
        }}

        // Create price chart
        const priceChartElement = document.getElementById('price-chart');
        const priceChart = LightweightCharts.createChart(priceChartElement, {{
            width: priceChartElement.clientWidth,
            height: 600,
            layout: {{
                background: {{ color: '#131722' }},
                textColor: '#d1d4dc',
            }},
            grid: {{
                vertLines: {{ color: '#1e222d' }},
                horzLines: {{ color: '#1e222d' }},
            }},
            crosshair: {{
                mode: LightweightCharts.CrosshairMode.Normal,
            }},
            rightPriceScale: {{
                borderColor: '#2b2b43',
                scaleMargins: {{
                    top: 0.1,
                    bottom: 0.2,
                }},
            }},
            timeScale: {{
                borderColor: '#2b2b43',
                timeVisible: true,
                secondsVisible: false,
            }},
            handleScroll: {{
                mouseWheel: true,
                pressedMouseMove: true,
            }},
            handleScale: {{
                axisPressedMouseMove: true,
                mouseWheel: true,
                pinch: true,
            }},
        }});

        // Add candlestick series
        const candlestickSeries = priceChart.addCandlestickSeries({{
            upColor: '#26a69a',
            downColor: '#ef5350',
            borderDownColor: '#ef5350',
            borderUpColor: '#26a69a',
            wickDownColor: '#ef5350',
            wickUpColor: '#26a69a',
        }});

        candlestickSeries.setData(candleData);
        candlestickSeries.setMarkers(markers);

        // Create volume chart
        const volumeChartElement = document.getElementById('volume-chart');
        const volumeChart = LightweightCharts.createChart(volumeChartElement, {{
            width: volumeChartElement.clientWidth,
            height: 150,
            layout: {{
                background: {{ color: '#131722' }},
                textColor: '#d1d4dc',
            }},
            grid: {{
                vertLines: {{ color: '#1e222d' }},
                horzLines: {{ color: '#1e222d' }},
            }},
            rightPriceScale: {{
                borderColor: '#2b2b43',
                scaleMargins: {{
                    top: 0.1,
                    bottom: 0,
                }},
            }},
            timeScale: {{
                borderColor: '#2b2b43',
                timeVisible: true,
                secondsVisible: false,
                visible: true,
            }},
            handleScroll: {{
                mouseWheel: true,
                pressedMouseMove: true,
            }},
            handleScale: {{
                axisPressedMouseMove: true,
                mouseWheel: true,
                pinch: true,
            }},
        }});

        // Add volume histogram
        const volumeSeries = volumeChart.addHistogramSeries({{
            color: '#26a69a',
            priceFormat: {{
                type: 'volume',
            }},
            priceScaleId: '',
        }});

        volumeSeries.setData(volumeData);
        
        // Update OHLCV info on crosshair move
        function formatNumber(num) {{
            if (num >= 1000000) {{
                return (num / 1000000).toFixed(2) + 'M';
            }} else if (num >= 1000) {{
                return (num / 1000).toFixed(2) + 'K';
            }}
            return num.toFixed(2);
        }}
        
        function updateOHLCV(data, volumeValue) {{
            if (!data) return;
            
            const isUp = data.close >= data.open;
            
            document.getElementById('info-open').textContent = '$' + data.open.toFixed(2);
            document.getElementById('info-high').textContent = '$' + data.high.toFixed(2);
            document.getElementById('info-low').textContent = '$' + data.low.toFixed(2);
            
            const closeEl = document.getElementById('info-close');
            closeEl.textContent = '$' + data.close.toFixed(2);
            closeEl.className = isUp ? 'value up' : 'value down';
            
            if (volumeValue !== undefined) {{
                document.getElementById('info-volume').textContent = formatNumber(volumeValue);
            }}
        }}
        
        // Initialize with last candle
        if (candleData.length > 0) {{
            const lastCandle = candleData[candleData.length - 1];
            const lastVolume = volumeData.length > 0 ? volumeData[volumeData.length - 1].value : 0;
            updateOHLCV(lastCandle, lastVolume);
        }}
        
        priceChart.subscribeCrosshairMove((param) => {{
            const data = getCandleFromParam(param);
            const volumeValue = (param && typeof param.time === 'number') ? volumeByTime.get(param.time) : undefined;

            if (data) {{
                updateOHLCV(data, volumeValue);
            }} else {{
                // Show last candle data when not hovering
                if (candleData.length > 0) {{
                    const lastCandle = candleData[candleData.length - 1];
                    const lastVolume = volumeData.length > 0 ? volumeData[volumeData.length - 1].value : 0;
                    updateOHLCV(lastCandle, lastVolume);
                }}
            }}
        }});

        // Sync time scales between charts
        priceChart.timeScale().subscribeVisibleTimeRangeChange((timeRange) => {{
            if (timeRange) {{
                volumeChart.timeScale().setVisibleRange(timeRange);
            }}
        }});

        volumeChart.timeScale().subscribeVisibleTimeRangeChange((timeRange) => {{
            if (timeRange) {{
                priceChart.timeScale().setVisibleRange(timeRange);
            }}
        }});

        // Handle window resize
        window.addEventListener('resize', () => {{
            priceChart.applyOptions({{ 
                width: priceChartElement.clientWidth 
            }});
            volumeChart.applyOptions({{ 
                width: volumeChartElement.clientWidth 
            }});
        }});

        // Fit content on load
        priceChart.timeScale().fitContent();
        
        // Price measurement tool
        let measurementStartPrice = null;
        let measurementStartTime = null;
        let isShiftPressed = false;
        let isMeasuring = false;

        function setChartInteraction(enabled) {{
            priceChart.applyOptions({{
                handleScroll: enabled ? {{ mouseWheel: true, pressedMouseMove: true }} : false,
                handleScale: enabled ? {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }} : false,
            }});
            volumeChart.applyOptions({{
                handleScroll: enabled ? {{ mouseWheel: true, pressedMouseMove: true }} : false,
                handleScale: enabled ? {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }} : false,
            }});
        }}
        
        // Track shift key state
        document.addEventListener('keydown', (e) => {{
            if (e.key === 'Shift') {{
                isShiftPressed = true;
                setChartInteraction(false);
            }}
        }});
        
        document.addEventListener('keyup', (e) => {{
            if (e.key === 'Shift') {{
                isShiftPressed = false;
                setChartInteraction(true);
                if (!isMeasuring) {{
                    document.getElementById('measurement-info').classList.remove('active');
                }}
            }}
        }});
        
        // Handle crosshair move for measurement
        priceChart.subscribeCrosshairMove((param) => {{
            if (!isShiftPressed || !isMeasuring) return;
            
            if (param && measurementStartPrice !== null) {{
                const data = getCandleFromParam(param);
                if (data && data.close) {{
                    const endPrice = data.close;
                    const priceChange = endPrice - measurementStartPrice;
                    const percentageChange = (priceChange / measurementStartPrice) * 100;
                    
                    // Update measurement display
                    document.getElementById('start-price').textContent = `$${{measurementStartPrice.toFixed(2)}}`;
                    document.getElementById('end-price').textContent = `$$${{endPrice.toFixed(2)}}`;
                    document.getElementById('price-change').textContent = `$$${{priceChange.toFixed(2)}}`;
                    
                    const percentEl = document.getElementById('percentage-change');
                    percentEl.textContent = `${{percentageChange >= 0 ? '+' : ''}}${{percentageChange.toFixed(2)}}%`;
                    percentEl.className = `value ${{percentageChange >= 0 ? 'change-positive' : 'change-negative'}}`;
                    
                    document.getElementById('measurement-info').classList.add('active');
                }}
            }}
        }});
        
        // Handle click to start/end measurement
        priceChartElement.addEventListener('mousedown', (e) => {{
            if (!isShiftPressed) return;
            e.preventDefault();
            
            const param = priceChart.priceScale('right');
            const timeScale = priceChart.timeScale();
            
            // Get price at click location
            const rect = priceChartElement.getBoundingClientRect();
            const y = e.clientY - rect.top;
            const price = candlestickSeries.coordinateToPrice(y);
            
            if (price) {{
                measurementStartPrice = price;
                isMeasuring = true;
                document.getElementById('measurement-info').classList.add('active');
                
                // Update start price
                document.getElementById('start-price').textContent = `$$${{price.toFixed(2)}}`;
                document.getElementById('end-price').textContent = '-';
                document.getElementById('price-change').textContent = '-';
                document.getElementById('percentage-change').textContent = '-';
            }}
        }});
        
        priceChartElement.addEventListener('mouseup', () => {{
            if (isMeasuring) {{
                isMeasuring = false;
                // Keep the measurement displayed until shift is released
            }}
        }});
    </script>
</body>
</html>"""
    
    # Save HTML file
    output_file = Path("backtest") / "backtest_chart.html"
    output_file.parent.mkdir(exist_ok=True)
    output_file.write_text(html_content)
    
    print("\n✓ Chart generated successfully!")
    print(f"  📊 Output: {output_file}")
    print(f"  🌐 Using: TradingView Lightweight Charts")
    print("\nLegend:")
    print("  🟡 Yellow circles = Trade start (anchor)")
    print("  🔴 Red arrows = DCA entries")
    print("  🟢 Green arrows = Take profit exits")
    print("\nInteractive controls:")
    print("  • Scroll wheel to zoom in/out")
    print("  • Click and drag to pan")
    print("  • Double-click to fit content")
    print("  • Hover over markers for details")
    
    # Open in browser
    webbrowser.open(f"file://{output_file.absolute()}")
    print(f"\n✓ Opening chart in browser...")


def main():
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = "strategy_config.json"
    
    print(f"{'='*70}")
    print(f"DCA STRATEGY BACKTEST CHART VIEWER")
    print(f"Using TradingView Lightweight Charts")
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
