# Dynamic Spot DCA Trading System

An automated spot DCA (Dollar-Cost Averaging) bot connected to Binance via API.

---

## How It Works

1. You set a **reference top price** (ATH, local top, or manual)
2. The bot monitors live price and calculates the **dump % from reference**
3. It **buys in layers** when configured dump levels are hit (-10%, -15%, etc.)
4. After each buy it recalculates the **weighted average entry**
5. When price recovers above `average_entry × (1 + TP%)` → **full exit, full profit**
6. System resets and waits for the next cycle

No leverage. No futures. Spot only.

---

## Project Structure

```
DCA Project/
├── main.py                  # Entry point
├── requirements.txt
├── .env                     # Your API keys (create from .env.example)
├── config/
│   └── coins.json           # Per-coin DCA strategies
├── core/
│   ├── binance_client.py    # Binance API wrapper
│   ├── dca_engine.py        # Main trading logic
│   ├── state_manager.py     # State persistence
│   └── logger.py            # Coloured console + file logging
├── models/
│   └── dca_config.py        # Data classes
├── utils/
│   └── calculations.py      # Pure math formulas
├── data/                    # Persisted position state (auto-created)
└── logs/                    # Daily log files (auto-created)
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# Edit .env and add your Binance API key and secret

# 3. Configure your coins
nano config/coins.json
```

---

## Configuration (config/coins.json)

```json
{
  "coin": "BTC",
  "symbol": "BTCUSDT",
  "enabled": true,
  "step_count": 6,
  "dump_levels": [-10, -15, -20, -25, -30, -35],
  "order_sizes": [1500, 2000, 2750, 5500, 5000, 5000],
  "take_profit_percent": 5,
  "reference_price": null
}
```

| Field | Description |
|-------|-------------|
| `dump_levels` | Negative % from reference top to trigger each buy |
| `order_sizes` | USDT amount to spend at each level |
| `take_profit_percent` | % above average entry to exit the full position |
| `reference_price` | Set manually or via CLI flag |

---

## Usage

```bash
# Run all enabled coins
python main.py

# Run BTC only
python main.py --coin BTC

# Set reference price = current market price, then run
python main.py --coin BTC --set-ref

# Set a manual reference price, then run
python main.py --coin BTC --ref 95000

# Check current position status (no trading)
python main.py --status
python main.py --coin BTC --status
```

---

## Safety Features

- ✅ Balance check before every buy
- ✅ Duplicate step prevention (state persisted to disk)
- ✅ API retry logic (3 attempts with back-off)
- ✅ Lot size rounding (prevents Binance filter errors)
- ✅ Full order fill verification
- ✅ Crash-safe: state survives restarts

---

## DCA Math

**Weighted Average Entry**
```
avg = SUM(order_size × fill_price) / SUM(order_size)
```

**Dump %**
```
dump % = ((current_price - reference_price) / reference_price) × 100
```

**Take Profit Price**
```
tp_price = avg_entry × (1 + tp_percent / 100)
```

---

## ⚠️ Risk Warning

This bot trades with real money on Binance Spot.
- Always test on **Testnet** first (`USE_TESTNET=true` in `.env`)
- Never invest more than you can afford to lose
- Past DCA performance does not guarantee future results
