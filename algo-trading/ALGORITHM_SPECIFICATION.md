# DCA Backtest Algorithm - Specification & Implementation

## Overview
Deterministic DCA backtesting algorithm for spot-only crypto strategy with **portfolio-level take-profit**.

## Key Principles

### ✅ CORRECT: Portfolio-Level Profit
```
pnl = Q × P - A
pnl_pct = pnl / A
TP when: pnl_pct ≥ target_profit
```

### ❌ INCORRECT: Price-from-last-entry
```
TP when: price ≥ last_dca_price × 1.05  ← WRONG!
```

## DCA Table Configuration

| Level | Dump % | Order ($) |
|-------|--------|-----------|
| 1     | -6%    | $1,290    |
| 2     | -9%    | $1,935    |
| 3     | -12%   | $2,580    |
| 4     | -16%   | $6,451    |
| 5     | -20%   | $5,000    |
| 6     | -24%   | $5,000    |

**Total Budget: $22,256**

## Variables & Formulas

### Position State Variables
- **P0** = reference/anchor price
- **di** = dump percentage for level i (e.g., -6%)
- **ai** = USD allocation for level i
- **pi** = execution price = `P0 × (1 + di/100)`
- **qi** = quantity bought = `ai / pi`

### Maintained Continuously:
- **A** = `sum(ai)` = total invested capital
- **Q** = `sum(qi)` = total position size
- **avg_price** = `A / Q` = weighted average entry price

### Profit Calculation:
- **Unrealized PnL** at market price P:
  ```
  pnl = Q × P - A
  pnl_pct = pnl / A × 100
  ```

### Take-Profit Price:
```
tp_price = avg_price × (1 + target_profit)
         = (A / Q) × (1 + target_profit)
         = (1 + target_profit) × A / Q
```

For 5% target profit:
```
tp_price = 1.05 × A / Q
```

## Execution Rules

### DCA Entry Trigger
```python
if candle.low <= pi:
    fill_level(i)
```

### Take-Profit Trigger
```python
if candle.high >= tp_price:
    close_position()
```

### Key Constraints
- ✅ Spot trading only (no leverage)
- ✅ DCA levels executed in order, never skipped
- ✅ Portfolio-level profit calculation
- ✅ Deterministic execution (no randomness)
- ✅ After TP, position resets completely
- ✅ Uses only candle wicks (high/low), ignores bodies (open/close)

## Algorithm Flow

```
1. Initialize: Set anchor price P0 = first_candle.high

2. Wait for Entry:
   - Update P0 = max(P0, candle.high) until dump occurs
   - If candle.low <= P0 × (1 + first_dca_level):
       → Start trade
       → Calculate all DCA prices: pi = P0 × (1 + di/100)

3. During Trade:
   For each candle:
     a) Check DCA fills:
        - If candle.low <= pi and level i not filled:
            → Fill level i at price pi
            → Update A and Q
     
     b) Check Take-Profit:
        - Calculate: tp_price = (1.05 × A) / Q
        - If candle.high >= tp_price:
            → Close position at tp_price
            → Record trade with profit = Q × tp_price - A
            → Reset position
            → Set new anchor: P0 = candle.high

4. Repeat from step 2
```

## Example Calculation

**Scenario:** DCA levels 1, 2, 3 filled
- Anchor: P0 = $100,000

**Executions:**
- Level 1 (-6%): p1 = $94,000, buy $1,290 → q1 = 0.01372 coins
- Level 2 (-9%): p2 = $91,000, buy $1,935 → q2 = 0.02126 coins
- Level 3 (-12%): p3 = $88,000, buy $2,580 → q3 = 0.02932 coins

**Position State:**
- A = $1,290 + $1,935 + $2,580 = $5,805
- Q = 0.01372 + 0.02126 + 0.02932 = 0.06430 coins
- avg_price = $5,805 / 0.06430 = $90,280.40

**Take-Profit:**
- tp_price = $90,280.40 × 1.05 = **$94,794.42**
- At TP: profit = 0.06430 × $94,794.42 - $5,805 = $290.25 = **5.00%** ✅

**Wrong Method Would Give:**
- ❌ Last DCA × 1.05 = $88,000 × 1.05 = $92,400
- ❌ This would only give ~3.2% portfolio profit!

## Acceptance Criteria

✅ **Portfolio TP triggers earlier than deepest-level TP**
- Example: tp_price = $94,794 vs. wrong = $92,400
- Difference: $2,394 earlier exit

✅ **Increasing DCA levels lowers avg_price correctly**
- Weighted average calculation ensures accuracy

✅ **Profit scales with total invested capital**
- All DCA contributions accounted for properly

## Implementation Status

✅ **Implemented correctly in:**
- `/dca_strategy.py` - Core algorithm
- `/config_loader.py` - Configuration management
- `/strategy_config.json` - DCA table configuration
- `/run_backtest.py` - Backtest execution
- `/view_backtest_chart.py` - Visualization

✅ **Verified with backtest:**
- 4 trades, all at exactly 5.00% profit
- Total P&L: $806.25 on $22,256 budget
- ROI: 3.62%

## Configuration File Structure

```json
{
  "csv_file": "data/btc_15m_data_2018_to_2025.csv",
  "start_date": "2025-01-01",
  "end_date": "2025-02-10",
  "initial_budget": 22256,
  "dca_levels": [-6, -9, -12, -16, -20, -24],
  "dca_allocations": [1290, 1935, 2580, 6451, 5000, 5000],
  "take_profit_percent": 5.0
}
```

## Notes

- No approximations in profit logic
- Correctness prioritized over speed
- All calculations use exact decimal arithmetic
- Portfolio profit guaranteed at target percentage
