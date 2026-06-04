# Dynamic Spot DCA Trading System (Binance API Based)

## Goal

Build a fully dynamic spot DCA trading system connected to Binance via API.

The system must:
- connect to Binance Spot API
- monitor live market prices
- execute configurable DCA buy orders
- calculate weighted average entry dynamically
- fully exit positions on configurable take profit
- reset and wait for the next trading cycle

The system is designed for:
- survivability
- fully mechanical execution
- low emotional decision making
- long-term compounding

Leverage and futures are NOT used.

Only Binance Spot trading is used.

---

# Exchange Integration

## Exchange
Binance Spot

## API Connection
The system must connect using:
- Binance API Key
- Binance Secret Key

The system must support:
- authenticated private endpoints
- public market data endpoints

---

# Required Binance Features

## Market Data
The system must:
- fetch live ticker price
- monitor price changes in real time
- calculate dump percentage from reference top

---

## Trading
The system must:
- place spot market buy orders
- place spot market sell orders
- fetch balances
- track filled orders
- calculate executed average price

---

# Core Trading Logic

1. Select a reference top price.
   This can be:
   - ATH
   - Local top
   - Manual reference

2. Monitor market dump percentage from the reference top.

3. Execute buy orders on configured dump levels.

4. After each buy:
   - recalculate weighted average entry
   - recalculate total invested capital
   - recalculate total coin quantity

5. If market price rises above average entry by configured TP percentage:
   - fully close the entire position
   - take full profit
   - reset all system state
   - switch back to WAITING mode

---

# Dynamic Configuration

All major parameters must be dynamic and editable.

---

## Step Count

```json
step_count: 6
```

---

## Dump Levels

```json
dump_levels: [-10, -15, -20, -25, -30, -35]
```

---

## Order Sizes

```json
order_sizes: [1500, 2000, 2750, 5500, 5000, 5000]
```

---

## Take Profit Percentage

```json
take_profit_percent: 5
```

---

## Trading Pair

```json
symbol: "BTCUSDT"
```

---

# Example DCA Model

| Step | Dump % | Order Size | Average Entry |
|------|---------|------------|----------------|
| 1 | -10% | 1500 | -10.00% |
| 2 | -15% | 2000 | -12.86% |
| 3 | -20% | 2750 | -16.00% |
| 4 | -25% | 5500 | -20.96% |
| 5 | -30% | 5000 | -23.64% |
| 6 | -35% | 5000 | -26.32% |

---

# Weighted Average Entry Formula

Average Entry = SUM(order_size * entry_price) / SUM(order_size)

---

# Dump Formula

Dump % = ((reference_price - current_price) / reference_price) * 100

---

# Take Profit Formula

Take Profit Price = Average Entry * (1 + TP%)

Example:
- average entry = 80,000
- TP = 5%
- exit price = 84,000

---

# Important Rule

No matter which step the system is currently in:

IF:
market price >= average entry + configured TP %

THEN:
- close entire position
- take full profit
- reset cycle
- return to WAITING mode

No partial take profits are used.

---

# State Management

The system should support the following states:

## WAITING
No active position.

## ACTIVE
At least one DCA step executed.

## EXITED
Position fully closed after TP.

---

# Reset Logic

After full TP:
- reset all executed steps
- reset average entry
- reset invested capital
- reset coin quantity
- wait for next cycle

---

# Coin-Specific Configuration

Each coin must support separate configuration.

Example BTC config:

```json
{
  "coin": "BTC",
  "symbol": "BTCUSDT",
  "step_count": 6,
  "dump_levels": [-10,-15,-20,-25,-30,-35],
  "order_sizes": [1500,2000,2750,5500,5000,5000],
  "take_profit_percent": 5
}
```

Example altcoin config:

```json
{
  "coin": "SOL",
  "symbol": "SOLUSDT",
  "step_count": 8,
  "dump_levels": [-15,-20,-25,-30,-40,-50,-60,-70],
  "take_profit_percent": 8
}
```

---

# Binance Safety Requirements

The system must:
- verify filled order status
- handle API failures safely
- retry failed requests
- prevent duplicate order execution
- prevent double-buy on same step
- verify sufficient balance before order placement

---

# Logging Requirements

The system should log:
- all executed orders
- dump levels hit
- average entry updates
- TP executions
- full cycle history
- profit/loss history
- timestamps

---

# Future Scalability

The architecture should support:
- multiple coins
- multiple independent DCA strategies
- admin dashboard
- strategy presets
- backtesting
- notifications
- Telegram integration

---

# System Philosophy

This is NOT a maximum-profit strategy.

The goal is:
- survivability
- mechanical execution
- volatility harvesting
- stable compounding
- long-term capital growth
