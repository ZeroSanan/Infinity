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
│   ├── regime_detector.py   # Live regime analysis (EMA, volume, AI narrative)
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
│   ├── mixed_dca_strategy.py# Backtest engine (adaptive bull/bear with regime + entry indicator)
│   └── data/                # Historical OHLCV CSV datasets
└── web/
    ├── app.py               # Flask dashboard (API + UI)
    └── templates/
        ├── index.html       # Live trading dashboard
        └── backtest.html    # Backtesting interface (long, short, mixed modes)
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

Infinity has three backtest engines, all in `algo-trading/`. They share the same DCA fill and portfolio-TP logic but differ in direction and regime awareness.

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

Both long and short backtest modes support an optional stop-loss. When enabled:

```
Long SL:  threshold = anchor × (1 - sl_percent/100)
          Triggers if candle.low <= threshold

Short SL: threshold = anchor × (1 + sl_percent/100)
          Triggers if candle.high >= threshold
```

The default live trading engine does **not** use a stop-loss — it simply waits for TP no matter how deep the dump goes. Stop-loss is a backtesting parameter only, used to model risk-limited scenarios.

### 8.4 Mixed Mode — Adaptive Bull/Bear Strategy

The mixed backtest (`algo-trading/mixed_dca_strategy.py`) is the most sophisticated engine. It **automatically switches** between long (buy-the-dip) and short (sell-the-rally) modes based on detected market regime, optionally gating entries behind a multi-factor confirmation signal.

#### 8.4.1 Three-Layer Decision Architecture

Every candle passes through three layers before a trade is entered:

```
Layer 1 — Regime (smoothed, 5-candle confirmation)
  Score ≥ +2  →  BULLISH  →  default active_mode = BUY
  Score ≤ -2  →  BEARISH  →  default active_mode = SELL
  Otherwise   →  NEUTRAL  →  hold prior mode, no new trades

Layer 2 — Extremity Overrides (single-candle activation)
  BULLISH + overbought signal  →  flip active_mode to SELL (counter-trend short)
  BEARISH + oversold signal    →  flip active_mode to BUY  (counter-trend long)
  Released via two-step exit: score leaves zone, then re-enters

Layer 3 — Entry Indicator (optional)
  Wait for EMA21 retest + RSI momentum turn + volume surge
  Entry confirmed when composite score ≥ 4
  Reference price = EMA21 at signal candle
```

#### 8.4.2 Regime Score

Each candle receives a score from −5 to +5 based on five signals:

| Signal | Bullish (+1) | Bearish (−1) |
|--------|-------------|-------------|
| Price vs EMA-50 | price > EMA50 | price < EMA50 |
| Price vs EMA-200 | price > EMA200 | price < EMA200 |
| EMA-50 vs EMA-200 | EMA50 > EMA200 | EMA50 < EMA200 |
| Higher-high (2-week) | new 2-week high | no new high |
| Volume trend (7d vs prior 7d) | volume rising >10% | volume falling >10% |

Volume signal is omitted if no volume column is present in the CSV.

Score ≥ +2 → raw BULLISH. Score ≤ −2 → raw BEARISH. Between −2 and +2 → NEUTRAL.

#### 8.4.3 Regime Smoothing

A raw regime signal does not immediately change the confirmed regime. A **5-candle confirmation buffer** is required: the raw signal must be the same non-neutral value for 5 consecutive candles before the confirmed regime switches. A single NEUTRAL candle clears the buffer.

This prevents whipsawing on short-lived crossovers. A minimum of 5 candles between switches is also enforced.

On a confirmed regime switch:
- Any open trade is closed at the current price (`regime_switch` reason)
- Any active extremity override is cancelled
- Any pending entry indicator wait is counted as skipped
- The anchor resets to the current candle's high (BULLISH) or low (BEARISH)

#### 8.4.4 Extremity Overrides

When the confirmed regime is BULLISH but the asset becomes overbought, the engine flips `active_mode` to SELL — enabling a counter-trend short DCA on the overextension. Symmetrically, a BEARISH regime that becomes oversold flips to BUY.

**Overbought** is triggered when RSI(14) > `rsi_overbought` (default 70) **or** price closes above the upper Bollinger Band (20-period, 2σ).

**Oversold** is triggered when RSI(14) < `rsi_oversold` (default 30) **or** price closes below the lower Bollinger Band.

**Two-step release** prevents premature exits on brief corrections:
```
OVERBOUGHT override release:
  Step 1 — regime score drops below +2 (exits overbought zone): override_saw_exit = True
  Step 2 — score rises back to ≥ +2: override released → active_mode = BUY, anchor = high

OVERSOLD override release:
  Step 1 — regime score rises above -2 (exits oversold zone): override_saw_exit = True
  Step 2 — score drops back to ≤ -2: override released → active_mode = SELL, anchor = low
```

#### 8.4.5 Entry Indicator (Optional)

When `use_entry_indicator=True` (the default), the engine does not enter a trade immediately when `active_mode` changes. It waits for a **three-factor confirmation signal**:

**BUY signal scoring:**

| Factor | Score | Condition |
|--------|-------|-----------|
| EMA21 retest | +2 | Price within 1.5% of EMA21 AND price > EMA50 |
| RSI momentum turn | +2 | RSI was below 45 in the last 3 candles, now above 45 |
| Volume surge | +1 | Volume > 1.5× 10-period average AND candle closes in upper half |

Entry fires when total score ≥ 4. The EMA21 price at the signal candle becomes the **reference price** (anchor) for DCA level calculations — not a rolling high.

**SELL signal scoring** (mirror):

| Factor | Score | Condition |
|--------|-------|-----------|
| EMA21 retest | +2 | Price within 1.5% of EMA21 AND price < EMA50 |
| RSI momentum turn | +2 | RSI was above 55 in the last 3 candles, now below 55 |
| Volume surge | +1 | Volume > 1.5× 10-period average AND candle closes in lower half |

When `use_entry_indicator=False`, trades open immediately on regime/override confirmation, and DCA levels are anchored to the rolling candle high or low.

#### 8.4.6 Warmup Guard

EMA-200 requires 200 candles to initialize. No trades are opened during the warmup period. A warning is emitted if the dataset contains fewer than 200 candles.

#### 8.4.7 Indicator Implementation

All indicators are implemented from scratch in O(n) — no external TA libraries:

| Indicator | Implementation |
|-----------|---------------|
| EMA(n) | Exponential smoothing, k = 2/(n+1), seeded with simple average |
| RSI(14) | Wilder smoothing (alpha = 1/14); `None` during 14-candle warmup |
| Bollinger Bands(20, 2σ) | O(n) prefix sums for rolling mean and variance |
| Higher-high | O(n) sliding-window maximum via monotone deque |
| Volume trend | O(n) prefix sums for 7-day average comparison |
| 10-period vol average | O(n) prefix sums |

#### 8.4.8 Output Fields

The mixed engine returns all standard backtest metrics plus:

| Field | Description |
|-------|-------------|
| `entry_indicator_used` | Whether indicator mode was active for this run |
| `entry_signals` | List of `{date, type: BUY\|SELL}` at each signal candle |
| `entry_indicator_stats` | `trades_with_indicator`, `trades_skipped_waiting`, `indicator_win_rate`, `indicator_avg_pnl` |
| `total_overrides` | Number of extremity override activations |
| `override_events` | Per-event log: date, type, regime, RSI value, BB position |
| `regime_switches` | Total confirmed regime transitions |
| `regime_timeline` | Per-candle `{date, regime, active_mode}` |
| `trades[].entry_triggered_by` | `"ENTRY_INDICATOR"` or `"REGIME_ONLY"` |
| `trades[].active_mode` | `active_mode` state at trade entry |

#### 8.4.9 Preset Configurations

| Preset | Bull Levels | Bear Levels | TP |
|--------|------------|------------|-----|
| Conservative | −6%, −10%, −15% | +6%, +10%, +15% | 8% |
| Aggressive | −8%, −13%, −20%, −28% | +8%, +13%, +20%, +28% | 12% |
| Asymmetric | −6%, −9%, −13%, −18% | +5%, +8%, +12% | 10% / 6% |

### 8.5 Output Metrics (All Modes)

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

## 9. Backtested Top Strategies

The following strategies were discovered by the optimizer running exhaustive grid search over BTC/USDT 1-hour candles from **2018 to 2025** (7 years, ~61,000 candles). All results use a starting budget of **$1,000 USDT** with equal capital split across DCA levels. Strategies are ranked by **ROI/day** — total return divided by the number of calendar days in the test window — to normalize for how quickly capital compounds.

> **Note:** Past backtest performance does not guarantee future results. These configurations represent historically optimal parameters on BTC 1H data and should be validated against other assets and timeframes before deployment.

---

### 9.1 Top 10 Long Strategies (Buy the Dip)

Long strategies profit when the asset dips and then recovers. Lower stop-loss or no stop-loss configurations show higher total ROI but require capital to remain locked longer in losing trades.

| Rank | Name | DCA Levels | Allocation / Level | TP | SL | ROI | ROI/Day | Trades | Win Rate | Avg Duration |
|------|------|-----------|-------------------|-----|-----|-----|---------|--------|----------|-------------|
| 1 | Fast #1 | −5%, −9%, −13% | $333 × 3 | 3% | 30% | **138%** | 44.4 | 317 | 96.5% | 3.1 days |
| 2 | Fast #2 | −5%, −10%, −15% | $333 × 3 | 3% | 30% | 97% | 30.3 | 311 | 96.5% | 3.2 days |
| 3 | Fast #3 | −5%, −8%, −11%, −14% | $250 × 4 | 3% | 30% | 89% | 29.8 | 326 | 96.6% | 3.0 days |
| 4 | Fast #4 | −7%, −11%, −15% | $333 × 3 | 3% | None | **225%** | 29.5 | 147 | 100% | 7.6 days |
| 5 | Fast #5 | −5%, −8%, −11% | $333 × 3 | 3% | 30% | 111% | 29.2 | 281 | 96.1% | 3.8 days |
| 6 | Fast #6 | −5%, −9%, −13%, −17% | $250 × 4 | 3% | 30% | 73% | 26.8 | 345 | 96.8% | 2.7 days |
| 7 | Fast #7 | −7%, −11%, −15% | $333 × 3 | 3% | 30% | 70% | 25.1 | 241 | 96.3% | 2.8 days |
| 8 | Fast #8 | −7%, −11%, −15%, −19% | $250 × 4 | 3% | None | **178%** | 23.6 | 148 | 100% | 7.5 days |
| 9 | Fast #9 | −5%, −9%, −13% | $333 × 3 | 3% | None | **223%** | 22.5 | 141 | 100% | 9.9 days |
| 10 | Fast #10 | −5%, −8%, −11% | $333 × 3 | 4% | 30% | 117% | 21.8 | 230 | 94.4% | 5.4 days |

**Best overall ROI (no SL):** Fast #4 — 225% total return, 100% win rate, 147 trades.
**Best ROI/day (with SL):** Fast #1 — 138% total return, 317 trades, 3.5% stopped out.

**Key pattern:** Tight first entry (−5% to −7%), narrow step spacing (3–4%), low take profit (3%), and shallow stop-loss (30%) consistently outperforms on BTC. The −5%/−9%/−13% shape is the most recurring optimal structure.

---

### 9.2 Top 10 Short Strategies (Sell the Rally)

Short strategies profit when the asset pumps and then resumes falling. Short entries are inherently riskier in a long-term bull market — higher stop-loss rates are expected and priced in. The optimizer found these configurations to be net-positive even accounting for frequent stops.

| Rank | Name | DCA Levels | Allocation / Level | TP | SL | ROI | ROI/Day | Trades | Win Rate | Avg Duration |
|------|------|-----------|-------------------|-----|-----|-----|---------|--------|----------|-------------|
| 1 | Short Fast #1 | +8%, +11%, +14% | $333 × 3 | 3% | 10% | 43% | 87.5 | 359 | 41.5% | 0.49 days |
| 2 | Short Fast #2 | +8%, +11%, +14%, +17% | $250 × 4 | 3% | 10% | 40% | 80.9 | 359 | 41.5% | 0.49 days |
| 3 | Short Fast #3 | +8%, +12%, +16% | $333 × 3 | 3% | 10% | 37% | 76.1 | 359 | 39.6% | 0.49 days |
| 4 | Short Fast #4 | +8%, +11%, +14%, +17%, +20% | $200 × 5 | 3% | 10% | 37% | 74.9 | 359 | 41.5% | 0.49 days |
| 5 | Short Fast #5 | +8%, +11%, +14% | $333 × 3 | 4% | 10% | **51%** | 71.2 | 351 | 36.2% | 0.72 days |
| 6 | Short Fast #6 | +8%, +12%, +16%, +20% | $250 × 4 | 3% | 10% | 34% | 69.9 | 359 | 39.6% | 0.49 days |
| 7 | Short Fast #7 | +8%, +11%, +14%, +17%, +20%, +23% | $167 × 6 | 3% | 10% | 34% | 69.6 | 359 | 41.5% | 0.49 days |
| 8 | Short Fast #8 | +8%, +12%, +16%, +20%, +24% | $200 × 5 | 3% | 10% | 32% | 65.1 | 359 | 39.6% | 0.49 days |
| 9 | Short Fast #9 | +8%, +11%, +14%, +17% | $250 × 4 | 4% | 10% | 46% | 63.8 | 351 | 36.2% | 0.72 days |
| 10 | Short Fast #10 | +8%, +11%, +14% | $333 × 3 | 5% | 10% | **53%** | 59.7 | 343 | 31.8% | 0.88 days |

**Best overall ROI:** Short Fast #10 — 53% return, highest TP (5%), trades close quickly.
**Best ROI/day:** Short Fast #1 — 87.5 ROI/day, fastest average duration (0.49 days ≈ 12 hours).

**Key pattern:** Short trades are high-frequency and short-lived. The +8% first entry is optimal — tight enough to catch most rallies, far enough to avoid noise. A 10% stop-loss is essential; without it, a sustained uptrend would leave capital locked indefinitely. Win rates of 30–42% are acceptable because winning trades close quickly at 3–5% TP while losses are capped at 10%.

---

### 9.3 Long vs Short Comparison

| Dimension | Long (Buy Dips) | Short (Sell Rallies) |
|-----------|----------------|---------------------|
| Dataset bias | Strongly favoured (7-year BTC bull trend) | Works against the trend |
| Typical win rate | 94–100% | 32–42% |
| Typical trade duration | 3–10 days | 0.5–1 day |
| Stop-loss necessity | Optional (30% or none) | Essential (10%) |
| Best ROI/day | 44.4 (Fast #1) | 87.5 (Short Fast #1) |
| Best total ROI | 225% (Fast #4) | 53% (Short Fast #10) |
| Capital efficiency | Lower frequency, higher per-trade gain | High frequency, small gains add up |

Short strategies generate more ROI/day in absolute terms despite lower win rates, because each trade closes in hours rather than days. In a bear market or during the short-sell phase of the Mixed Strategy, short DCA outperforms the long mode on a per-day basis.

---

## 10. Web Dashboard

The Flask dashboard (port 5050) provides full visibility and control over live strategies.

### Live Trading Tab
- Real-time price display per strategy
- Position status: dump %, average entry, total invested, P&L
- Step-by-step progress visualization
- Start / Stop engine per strategy
- Set reference price manually
- Reset position state

### Backtesting Tab

#### Long & Short Modes
- Run historical simulations on uploaded OHLCV CSV data (Binance export format)
- Configure: initial budget, DCA levels, allocations, take profit %, stop loss %
- Optional date range filter
- Results: Total ROI, Net P&L, Win Rate, trade count, average duration
- Equity curve chart
- Full trade-by-trade breakdown table
- Preset strategies panel (pre-loaded top-performing configurations)
- Long (buy dips) and Short (sell rallies) modes

#### Mixed Mode
- Adaptive bull/bear mode that automatically switches direction by detected regime
- Separate bull and short DCA level/size configuration panels
- **Entry Indicator toggle** — iOS-style switch to enable/disable the three-factor confirmation gate
  - RSI Overbought threshold input (flip regime to SELL; default 70)
  - RSI Oversold threshold input (flip regime to BUY; default 30)
- **⇄ Compare button** — runs the backtest twice (indicator ON vs OFF) and renders a side-by-side metrics table; better value highlighted green, worse in red
- **★ Star markers** on the equity curve chart at each entry signal candle (green = BUY signal, red = SELL signal)
- **Indicator stats card** when indicator mode is active: trades triggered by indicator, trades skipped while waiting, indicator win rate, indicator avg P&L
- **INDICATOR / REGIME badges** in the trade table identifying how each trade was entered; indicator-triggered rows highlighted with a gold left border
- Regime timeline overlay on equity chart (color-coded BULLISH/BEARISH/NEUTRAL background bands)

### Accounts Tab
- Add/remove Binance API accounts
- Test connectivity and view USDT balance
- Supports both live and testnet accounts

---

## 11. Data Persistence

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

## 12. Exchange Integration

- **Exchange:** Binance (spot markets only)
- **Order types:** Market buy, market sell
- **Supported:** Testnet mode for safe testing
- **Safety:** `binance_client.py` verifies fills, handles lot size precision (step size), and prevents zero-quantity orders

---

## 13. Configuration

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

## 14. CLI Usage

```bash
python main.py                        # Run all enabled strategies
python main.py --coin BTC             # Run BTC strategies only
python main.py --coin BTC --set-ref   # Set reference = current price, then run
python main.py --coin BTC --ref 95000 # Set manual reference price, then run
python main.py --status               # Print position status and exit
```

---

## 15. Infrastructure & Deployment

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

## 16. What the System Aims to Achieve

The core thesis: **crypto markets are volatile and mean-reverting over medium time horizons**. Assets regularly dump 10–40% from local tops and then recover. Infinity is built to exploit this pattern mechanically, without needing to predict when or how deep each dump will be.

A single cycle looks like this:
1. Asset is near a local top. Reference price is set.
2. Asset dumps. Infinity buys the dip in layers — small first, large later.
3. Each buy lowers the blended average entry price.
4. Asset recovers. At +5–10% above average entry, the entire position sells for profit.
5. System resets. Waits for the next cycle.

The system does not need to catch the absolute top or the absolute bottom. It only needs the asset to recover partially from its dump. Because the average entry is well below the reference price, even a 50% recovery from the bottom is enough to trigger profit.

Over hundreds of cycles across multiple assets, this produces consistent, compounding returns with defined, limited capital exposure per strategy.

The Mixed Strategy extends this to bear markets: when regime detection confirms a downtrend, the engine flips to short DCA — accumulating short exposure on rallies and taking profit as the price resumes falling. The Entry Indicator layer further filters entries to candles where EMA structure, RSI momentum, and volume all agree, reducing false-entry trades during choppy or transitional periods.

---

## 17. Planned / Possible Extensions

- **Telegram / email alerts** on buy/sell events
- **Multiple exchange support** (OKX, Bybit)
- **Dynamic position sizing** based on portfolio value
- **Trailing take profit** to capture extended uptrends
- **Risk controls** — max drawdown limits, daily loss caps
- **Multi-account portfolio view** — aggregate P&L across all accounts
- **Strategy optimizer** — auto-tune DCA levels based on backtest results
- **Live mixed strategy engine** — port the regime detector and entry indicator to the live `DCAEngine` for fully autonomous bull/bear switching

---

*Infinity — built for systematic, emotion-free DCA trading on spot markets.*
