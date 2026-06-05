# Infinity — Dynamic Spot DCA Trading System
## Technical White Paper

---

## 1. Overview

**Infinity** is an automated cryptocurrency trading system built around a **Dynamic Dollar-Cost Averaging (DCA)** strategy. It monitors live market prices, executes tiered buy orders when an asset drops to predefined levels, and automatically takes profit once the portfolio reaches a target return. The system is designed for spot markets (no leverage), running continuously on a VPS with a web-based dashboard for monitoring and control.

**Core idea:** Instead of trying to time the market, Infinity places increasingly larger buy orders as an asset falls. As the price recovers, the blended average entry price is much lower than the initial reference price, making it easier to profit even on a partial recovery.

---

## 2. System Philosophy

This is **not** a maximum-profit strategy. The goal is not to catch tops or bottoms. The goal is:

| Principle | Meaning |
|-----------|---------|
| **Survivability** | The system never uses leverage or futures. A 100% drop cannot wipe out more than the capital allocated to that strategy. |
| **Mechanical execution** | Every decision is rule-based. There is no discretion, no panic selling, no FOMO buying outside the configured levels. |
| **Volatility harvesting** | Crypto markets are highly volatile. Infinity turns that volatility into an asset — each dip is an opportunity to accumulate at a lower average cost. |
| **Stable compounding** | Small, consistent profits (5–10% per cycle) accumulate over time. A 5% gain repeated 20 times grows capital by 165%. |
| **Long-term capital growth** | The system is designed to run indefinitely, cycling through bull and bear periods without human intervention. |

**What it is not:**
- Not a high-frequency trader
- Not a leverage or futures system
- Not a market-timing system
- Not a maximum-profit chaser
- Not emotionally driven

---

## 3. Architecture

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
├── utils/
│   └── calculations.py      # Pure math functions (no side effects)
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

## 4. Core Trading Logic — Step by Step

### 4.1 Reference Price (Anchor)

Every strategy starts with a **reference price** — the "top" price from which dump levels are calculated. This is set manually via the dashboard or CLI before starting a strategy.

```
Reference Price = P₀  (e.g. BTC at $100,000)
```

The reference price can be:
- The current market price (most common — set it when you believe the asset is near a local top)
- A recent all-time high
- Any manual price you choose

### 4.2 Dump Levels & Buy Steps

Each strategy defines N **dump levels** (negative percentages) and a corresponding **order size** (in USDT) for each level. When the market price falls to a level, a market buy order is executed.

```
Dump Level i trigger price = P₀ × (1 + dump_level_i / 100)

Example with P₀ = $100,000:
  Step 1: -10%  →  trigger at $90,000  → buy $1,500 USDT
  Step 2: -15%  →  trigger at $85,000  → buy $2,000 USDT
  Step 3: -20%  →  trigger at $80,000  → buy $2,750 USDT
  Step 4: -25%  →  trigger at $75,000  → buy $5,500 USDT
  Step 5: -30%  →  trigger at $70,000  → buy $5,000 USDT
  Step 6: -35%  →  trigger at $65,000  → buy $5,000 USDT
```

Orders are executed **in sequence** — step 2 only triggers after step 1, step 3 after step 2, and so on. Steps are never skipped.

**Why increasing sizes?** Later steps are deeper into a dump. The risk of further downside is higher, but the potential recovery gain is also higher. Larger orders at deeper levels pull the average entry price down more aggressively, requiring less recovery to reach take profit.

### 4.3 Trigger Tolerance

The engine polls prices every 10 seconds. Due to polling granularity, a price may briefly touch a level and move away. The `should_buy` function includes a **0.1% tolerance band** below each level:

```python
# utils/calculations.py
def should_buy(dump_percent, target_level, tolerance=0.1):
    return dump_percent <= target_level + tolerance
```

This means if the target level is -10%, the buy triggers when the dump reaches -10.1% or deeper. This prevents missed fills due to timing.

### 4.4 Weighted Average Entry Price

After each buy, the system recalculates the **weighted average entry price** across all executed steps:

```
avg_entry = Σ(order_size_usdt × entry_price) / Σ(order_size_usdt)
```

Implemented in `utils/calculations.py`:

```python
def calc_weighted_average_entry(executed_sizes, executed_prices):
    total_value = sum(s * p for s, p in zip(executed_sizes, executed_prices))
    total_size  = sum(executed_sizes)
    return total_value / total_size
```

**Why this matters:** The average entry moves down with each deeper buy. Because order sizes increase at deeper levels, the average is pulled down faster than a simple average would suggest.

**Worked example** (P₀ = $100,000, BTC):

| Step | Dump % | Trigger Price | Order Size | Coins Bought | Avg Entry | Avg Entry % Below P₀ |
|------|--------|--------------|------------|--------------|-----------|----------------------|
| 1 | -10% | $90,000 | $1,500 | 0.01667 BTC | $90,000 | -10.00% |
| 2 | -15% | $85,000 | $2,000 | 0.02353 BTC | $87,143 | -12.86% |
| 3 | -20% | $80,000 | $2,750 | 0.03438 BTC | $84,000 | -16.00% |
| 4 | -25% | $75,000 | $5,500 | 0.07333 BTC | $79,038 | -20.96% |
| 5 | -30% | $70,000 | $5,000 | 0.07143 BTC | $76,364 | -23.64% |
| 6 | -35% | $65,000 | $5,000 | 0.07692 BTC | $73,684 | -26.32% |

After step 6, the asset only needs to recover to **$73,684** (from $65,000) for take profit to trigger — a **13.4% recovery** rather than a 53.8% full round-trip back to P₀.

### 4.5 Take Profit

The strategy exits the **entire position** with a single market sell when:

```
current_price >= avg_entry × (1 + take_profit_percent / 100)
```

Implemented in `utils/calculations.py`:

```python
def calc_take_profit_price(average_entry, tp_percent):
    return average_entry * (1 + tp_percent / 100)

def should_take_profit(current_price, tp_price):
    return current_price >= tp_price
```

This is **portfolio-level profit**, not per-step profit. The system measures profit against the blended average entry across all executed steps. For example:

```
avg_entry = $84,000  (after steps 1–3)
TP = 10%
exit_price target = $84,000 × 1.10 = $92,400

P&L = (exit_price - avg_entry) × total_quantity
    = ($92,400 - $84,000) × 0.07458 BTC
    = +$626.47 USDT
    = +10% on the $6,250 invested
```

TP check runs **first** on every tick (before checking for new buy levels). Once TP is hit, no more buys are placed — the position is sold immediately.

### 4.6 P&L Calculation

```python
def calc_pnl(average_entry, exit_price, total_quantity):
    pnl_usdt = (exit_price - average_entry) * total_quantity
    pnl_pct  = ((exit_price - average_entry) / average_entry) * 100
    return pnl_usdt, pnl_pct
```

---

## 5. State Machine

Each strategy tracks one of three states:

```
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  ▼                                                         │
WAITING ──── first dump level hit ───► ACTIVE              │
                                         │                  │
                                         ├── TP triggered ──┘
                                         │   (full sell + reset)
                                         │
                                         └── next step hit
                                             (buy + stay ACTIVE)
```

| State | Meaning |
|-------|---------|
| `WAITING` | No position open. Monitoring for first dump level trigger. |
| `ACTIVE` | One or more buy steps executed. Monitoring for TP and next steps. |
| `EXITED` | Take profit executed. State immediately resets to `WAITING`. |

State is persisted to `data/{strategy_id}.json` so it survives restarts.

**Reset logic after TP:**
- All executed steps cleared
- Average entry reset to 0
- Total invested reset to 0
- Total quantity reset to 0
- Status set back to `WAITING`
- Reference price retained (ready for the next cycle)

---

## 6. Engine Polling Logic

The `DCAEngine` (`core/dca_engine.py`) polls prices every **10 seconds**. On each tick:

```
tick():
  1. Fetch current market price from Binance
  2. If no reference price set → log warning, skip
  3. dump_pct = (price - reference_price) / reference_price × 100
  4. If status == ACTIVE:
       tp_price = avg_entry × (1 + tp_percent/100)
       if price >= tp_price → _execute_take_profit() → return
       else → log position status
  5. next_idx = number of steps already executed
     if next_idx >= step_count → all steps done, just wait for TP
  6. target_level = dump_levels[next_idx]
     if dump_pct <= target_level + 0.1 → _execute_buy(next_idx)
```

**Safety checks in `_execute_buy`:**
- Verify USDT balance is sufficient before placing order
- Verify fill price and fill quantity from the returned order are non-zero
- Log every order with symbol, price, quantity, and Binance order ID
- Skip the step (log error) rather than crashing if anything fails

Multiple strategies run concurrently in **separate threads**, one engine per strategy.

---

## 7. Active Strategies (Current Configuration)

| Strategy | Coin | Steps | Dump Range | Total Capital | Take Profit |
|----------|------|-------|------------|---------------|-------------|
| BTC Main | BTC/USDT | 8 | -10% to -45% | $23,750 USDT | 10% |
| ETH Main | ETH/USDT | 6 | -10% to -35% | $21,750 USDT | 5% |
| BTC Aggressive | BTC/USDT | 8 | -10% to -45% | $23,750 USDT | 10% |

---

## 8. Backtesting Engine

### 8.1 Long Mode (Buy the Dip)

The long backtest (`algo-trading/dca_strategy.py`) simulates DCA strategy performance on historical OHLCV data using the exact same portfolio-level TP logic as the live engine.

**Algorithm:**

```
For each candle (high, low — open/close ignored):
  If NOT in trade:
    anchor_price = rolling max of candle highs (tracks recent top)
    If candle.low <= anchor × (1 + first_dca_level/100):
      → Start trade, calculate all level prices from anchor
      → Check fills on this same candle

  If IN trade:
    STEP 1: Check DCA fills
      For each unfilled level i:
        If candle.low <= anchor × (1 + dump_level_i/100) → fill
    STEP 2: Check Stop-Loss (if enabled)
      If candle.low <= anchor × (1 - sl_percent/100) → close at loss
    STEP 3: Check Take-Profit
      tp_price = avg_entry × (1 + tp_percent/100)
      If candle.high >= tp_price → close at profit
```

**Key design decisions:**
- Uses only **candle wicks** (high/low) — open/close are irrelevant
- Levels are **sequential** — each level only fills after all prior levels
- TP is measured at **portfolio level** (against blended avg entry, not anchor)
- After TP or SL, anchor resets to the current candle high, starting a new cycle

**Why wicks only?** A candle's low represents the actual worst price reached during that period. A candle's high represents the actual best price. Using only wicks is the most realistic model for what a limit/market order would have executed at.

### 8.2 Short Mode (Sell the Rally)

The short backtest (`algo-trading/short_dca_strategy.py`) is the **exact mirror image** of the long strategy:

| Dimension | Long (Buy Dips) | Short (Sell Rallies) |
|-----------|----------------|---------------------|
| Anchor tracking | Rolling high | Rolling low |
| Entry trigger | `candle.low <= anchor × (1 + dump%)` | `candle.high >= anchor × (1 + pump%)` |
| Dump/pump levels | Negative (e.g. -6%, -9%) | Positive (e.g. +6%, +9%) |
| Average price | Weighted avg buy price | Weighted avg short price |
| TP formula | `avg_entry × (1 + tp%)` | `avg_short × (1 - tp%)` |
| TP trigger | `candle.high >= tp_price` | `candle.low <= tp_price` |
| SL trigger | `candle.low <= sl_threshold` | `candle.high >= sl_threshold` |
| Profit source | Price recovery upward | Price drop downward |

Short mode is useful for bear markets where assets rally and then resume falling.

### 8.3 Stop-Loss (Optional)

Both backtest modes support an optional stop-loss. When enabled:

```
Long SL:  threshold = anchor × (1 - sl_percent/100)
          Triggers if candle.low <= threshold

Short SL: threshold = anchor × (1 + sl_percent/100)
          Triggers if candle.high >= threshold
```

The default live trading engine does **not** use a stop-loss — it simply waits for TP no matter how deep the dump goes. Stop-loss is a backtesting parameter only, used to model risk-limited scenarios.

### 8.4 Output Metrics

| Metric | Description |
|--------|-------------|
| Total ROI | `(final_budget - initial_budget) / initial_budget × 100` |
| Net P&L | Sum of all trade profit/loss in USDT |
| Win Rate | `winning_trades / total_trades × 100` |
| Stop Loss Rate | `stopped_out_trades / total_trades × 100` |
| Avg Trade Duration | Mean days between trade open and close |
| Largest Profit | Best single trade in USDT |
| Largest Loss | Worst single trade in USDT |
| Per-trade breakdown | Entry, levels filled, invested, exit price, profit |

---

## 9. Web Dashboard

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

## 10. Data Persistence

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

State survives process restarts and VPS reboots. The engine resumes exactly where it left off: same reference price, same executed steps, same average entry, waiting for the next level or TP trigger.

---

## 11. Exchange Integration

- **Exchange:** Binance (spot markets only)
- **Order types:** Market buy, market sell
- **Supported:** Testnet mode for safe testing
- **Safety:** `binance_client.py` verifies fills, handles lot size precision (step size), and prevents zero-quantity orders

---

## 12. Configuration

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

## 13. CLI Usage

```bash
python main.py                        # Run all enabled strategies
python main.py --coin BTC             # Run BTC strategies only
python main.py --coin BTC --set-ref   # Set reference = current price, then run
python main.py --coin BTC --ref 95000 # Set manual reference price, then run
python main.py --status               # Print position status and exit
```

---

## 14. Infrastructure & Deployment

| Component | Details |
|-----------|---------|
| VPS | Hostinger srv1052900.hstgr.cloud |
| OS | Rocky Linux / RHEL-based |
| Python | 3.x with virtualenv |
| Process management | systemd (auto-restart on crash) |
| Web server | Flask (dev server, port 5050) |
| Repository | GitHub — zerosanan/Infinity |
| Auto-deploy | Cron job polls GitHub master every 60 seconds |
| Firewall | firewalld — port 5050 open for dashboard |

---

## 15. What the System Aims to Achieve

The core thesis: **crypto markets are volatile and mean-reverting over medium time horizons**. Assets regularly dump 10–40% from local tops and then recover. Infinity is built to exploit this pattern mechanically, without needing to predict when or how deep each dump will be.

A single cycle looks like this:
1. Asset is near a local top. Reference price is set.
2. Asset dumps. Infinity buys the dip in layers — small first, large later.
3. Each buy lowers the blended average entry price.
4. Asset recovers. At +5–10% above average entry, the entire position sells for profit.
5. System resets. Waits for the next cycle.

The system does not need to catch the absolute top or the absolute bottom. It only needs the asset to recover partially from its dump. Because the average entry is well below the reference price, even a 50% recovery from the bottom is enough to trigger profit.

Over hundreds of cycles across multiple assets, this produces consistent, compounding returns with defined, limited capital exposure per strategy.

---

## 16. Planned / Possible Extensions

- **Telegram / email alerts** on buy/sell events
- **Multiple exchange support** (OKX, Bybit)
- **Dynamic position sizing** based on portfolio value
- **Trailing take profit** to capture extended uptrends
- **Risk controls** — max drawdown limits, daily loss caps
- **Multi-account portfolio view** — aggregate P&L across all accounts
- **Strategy optimizer** — auto-tune DCA levels based on backtest results

---

*Infinity — built for systematic, emotion-free DCA trading on spot markets.*
