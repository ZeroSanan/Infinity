# Infinity — Dynamic Spot DCA Trading System
## Technical White Paper

---

## 1. Overview

**Infinity** is an automated cryptocurrency trading system built around a **Dynamic Dollar-Cost Averaging (DCA)** strategy. It monitors live market prices, executes tiered buy orders when an asset drops to predefined levels, and automatically takes profit once the portfolio reaches a target return. The system is designed for spot markets (no leverage), running continuously on a VPS with a web-based dashboard for monitoring and control.

**Core idea:** Instead of trying to time the market, Infinity places increasingly larger buy orders as an asset falls. As the price recovers, the blended average entry price is much lower than the initial reference price, making it easier to profit even on a partial recovery.

---

## 2. Architecture

```
Infinity/
├── main.py                  # CLI entry point — starts trading engines
├── core/
│   ├── dca_engine.py        # Trading logic: price polling, buy/sell execution
│   ├── binance_client.py    # Binance API wrapper (spot orders, price feed)
│   ├── state_manager.py     # Persists position state to JSON files
│   └── logger.py            # Structured logging
├── models/
│   └── dca_config.py        # Data models: CoinConfig, PositionState, ExecutedStep
├── config/
│   └── coins.json           # Strategy definitions (coins, levels, sizes, TP)
├── data/                    # Live position state files (one JSON per strategy)
├── algo-trading/
│   ├── dca_strategy.py      # Backtest engine (long/buy-the-dip mode)
│   ├── short_dca_strategy.py# Backtest engine (short/sell-the-rally mode)
│   └── data/                # Historical OHLCV CSV datasets
└── web/
    ├── app.py               # Flask dashboard (API + UI)
    └── templates/
        ├── index.html       # Live trading dashboard
        └── backtest.html    # Backtesting interface
```

**Deployment:** Runs on a Linux VPS (Hostinger) as two `systemd` services:
- `infinity.service` — the live trading bot (`main.py`)
- `infinity-web.service` — the web dashboard (`web/app.py`, port 5050)

**Auto-deployment:** A cron job polls GitHub every 60 seconds. When a new commit is detected on `master`, it pulls the code, reinstalls dependencies, and restarts both services automatically.

---

## 3. DCA Strategy — How It Works

### 3.1 Reference Price

Every strategy starts with a **reference price** — the "top" price from which dump levels are calculated. This is set manually via the dashboard or CLI before starting a strategy.

```
Reference Price = P₀  (e.g. BTC at $100,000)
```

### 3.2 Dump Levels & Buy Steps

Each strategy defines N **dump levels** (negative percentages) and a corresponding **order size** (in USDT) for each level. When the market price falls to a level, a market buy order is executed.

```
Step 1: price drops -10% from P₀  → buy $1,500 USDT
Step 2: price drops -15% from P₀  → buy $2,000 USDT
Step 3: price drops -20% from P₀  → buy $2,750 USDT
Step 4: price drops -25% from P₀  → buy $5,500 USDT
...
```

Orders are executed **in sequence** — step 2 only triggers after step 1, step 3 after step 2, and so on. Steps are never skipped.

### 3.3 Average Entry Price

After each buy, the system calculates the weighted average entry price across all executed steps:

```
avg_entry = Σ(order_size_usdt × entry_price) / Σ(order_size_usdt)
```

Because order sizes increase at deeper levels, the average entry price is pulled down aggressively as the asset falls, making recovery much easier.

### 3.4 Take Profit

The strategy exits the entire position with a single market sell when:

```
current_price >= avg_entry × (1 + take_profit_percent / 100)
```

This is **portfolio-level profit**, not per-step profit. For example, with a 10% TP target, the system sells when the position as a whole is up 10% from the average entry — not from the first buy price.

### 3.5 Position States

Each strategy tracks one of three states:

| State | Meaning |
|-------|---------|
| `WAITING` | No position open. Monitoring for first dump level trigger. |
| `ACTIVE` | One or more buy steps executed. Monitoring for TP and next steps. |
| `EXITED` | Take profit executed. State resets to WAITING automatically. |

State is persisted to `data/{strategy_id}.json` so it survives restarts.

---

## 4. Engine Polling Logic

The `DCAEngine` polls prices every **10 seconds**. On each tick:

1. Fetch current market price from Binance
2. If no reference price set → log a warning and skip
3. Calculate `dump_pct = (price - reference_price) / reference_price × 100`
4. If status is `ACTIVE` → check if TP is triggered first (highest priority)
5. Check if the next dump level has been reached → execute buy if so
6. Log position status

Multiple strategies run concurrently in separate threads.

---

## 5. Active Strategies (Current Configuration)

| Strategy | Coin | Steps | Dump Range | Total Capital | Take Profit |
|----------|------|-------|------------|---------------|-------------|
| BTC Main | BTC/USDT | 8 | -10% to -45% | $23,750 USDT | 10% |
| ETH Main | ETH/USDT | 6 | -10% to -35% | $21,750 USDT | 5% |
| BTC Aggressive | BTC/USDT | 8 | -10% to -45% | $23,750 USDT | 10% |

---

## 6. Web Dashboard

The Flask dashboard (port 5050) provides full visibility and control over live strategies.

### Live Trading Tab
- Real-time price display per strategy
- Position status: dump %, average entry, total invested, P&L
- Step-by-step progress visualization
- Start / Stop engine per strategy
- Set reference price manually
- Reset position state

### Backtesting Tab
- Run historical simulations on uploaded OHLCV CSV data (Binance export format)
- Configure: initial budget, DCA levels, allocations, take profit %, stop loss %
- Optional date range filter
- Results: Total ROI, Net P&L, Win Rate, trade count, average duration
- Equity curve chart
- Full trade-by-trade breakdown table
- Preset strategies panel (pre-loaded top-performing configurations)
- Long (buy dips) and Short (sell rallies) modes

### Accounts Tab
- Add/remove Binance API accounts
- Test connectivity and view USDT balance
- Supports both live and testnet accounts

---

## 7. Backtesting Engine

The backtesting module (`algo-trading/`) simulates DCA strategy performance on historical OHLCV data.

**Algorithm:**
- For each candle, check if any un-hit DCA level has been reached (`candle.low <= level_price`)
- Execute DCA levels strictly in order (no skipping)
- After each fill, recalculate average entry and TP price
- Check if TP is triggered (`candle.high >= tp_price`) after all fills for that candle
- If stop-loss is configured, check if `candle.low <= stop_loss_price`

**Key design decisions:**
- Uses only candle wicks (high/low) — ignores open/close
- Portfolio-level TP: profit is measured against the blended average entry, not the initial anchor price
- Each completed trade (entry + exit) is logged with full statistics
- Equity curve is tracked across all trades

**Output metrics:**
- Total ROI, Net P&L, Win Rate, Stop Loss Rate
- Average / Largest / Smallest trade
- Average trade duration
- Per-trade breakdown with levels filled, invested, exit price, profit

---

## 8. Data Persistence

Position state is stored as JSON files in the `data/` directory. Each file corresponds to one strategy:

```json
{
  "strategy_id": "btc-main",
  "coin": "BTC",
  "symbol": "BTCUSDT",
  "status": "ACTIVE",
  "reference_price": 100000.0,
  "average_entry": 82500.0,
  "total_invested": 6250.0,
  "total_quantity": 0.07142,
  "executed_steps": [
    {
      "step_index": 0,
      "dump_level": -10.0,
      "order_size_usdt": 1500.0,
      "entry_price": 90000.0,
      "quantity": 0.01666,
      "order_id": "12345678",
      "timestamp": "2026-06-04 21:00:00 UTC"
    }
  ]
}
```

---

## 9. Exchange Integration

- **Exchange:** Binance (spot markets only)
- **Order types:** Market buy, market sell
- **Supported:** Testnet mode for safe testing
- **API wrapper:** `core/binance_client.py` handles order placement, balance checks, price feed, and lot size (step size) precision

---

## 10. Configuration

Strategies are defined in `config/coins.json`. Each entry includes:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique strategy identifier |
| `name` | string | Display name |
| `coin` | string | Asset ticker (e.g. BTC) |
| `symbol` | string | Trading pair (e.g. BTCUSDT) |
| `enabled` | bool | Whether this strategy runs on startup |
| `step_count` | int | Number of DCA buy levels |
| `dump_levels` | float[] | Price drop % to trigger each step (must be negative) |
| `order_sizes` | float[] | USDT amount to buy at each step |
| `take_profit_percent` | float | Target return % to trigger sell |
| `reference_price` | float? | Starting reference price (null = must be set manually) |

---

## 11. CLI Usage

```bash
python main.py                        # Run all enabled strategies
python main.py --coin BTC             # Run BTC strategies only
python main.py --coin BTC --set-ref   # Set reference = current price, then run
python main.py --coin BTC --ref 95000 # Set manual reference price, then run
python main.py --status               # Print position status and exit
```

---

## 12. Infrastructure & Deployment

| Component | Details |
|-----------|---------|
| VPS | Hostinger srv1052900.hstgr.cloud |
| OS | Rocky Linux / RHEL-based |
| Python | 3.x with virtualenv |
| Process management | systemd (auto-restart on crash) |
| Web server | Flask (dev server, port 5050) |
| Repository | GitHub — zerosanan/Infinity (private→public) |
| Auto-deploy | Cron job polls GitHub master every 60 seconds |
| Firewall | firewalld — port 5050 open for dashboard |

---

## 13. Planned / Possible Extensions

- **Telegram / email alerts** on buy/sell events
- **Multiple exchange support** (OKX, Bybit)
- **Dynamic position sizing** based on portfolio value
- **Trailing take profit** to capture extended uptrends
- **Risk controls** — max drawdown limits, daily loss caps
- **Multi-account portfolio view** — aggregate P&L across all accounts
- **Strategy optimizer** — auto-tune DCA levels based on backtest results

---

*Infinity — built for systematic, emotion-free DCA trading on spot markets.*
