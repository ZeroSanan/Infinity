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
│   ├── ml_signals.py        # ML models: direction, regime & entry-quality scoring
│   ├── signal_history.py    # SQLite persistence for market signals + ML outputs
│   └── logger.py            # Structured logging
├── models/
│   └── dca_config.py        # Data models: CoinConfig, PositionState, ExecutedStep
├── config/
│   └── coins.json           # Strategy definitions (coins, levels, sizes, TP)
├── data/                    # Live position state files (one JSON per strategy)
│                             # + signal_history.db (Market Signals history)
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

### Market Signals Tab
- Live regime read for BTC, ETH, XRP and SOL on the 4-hour timeframe, refreshed every 15 minutes
- Per-coin card: regime badge, 4-signal checklist, recommended strategy (saved or suggested) with DCA trigger-price chips, anchor price, and fresh-start levels
- Action banner — **LONG NOW / SHORT NOW / WATCH / WAIT** — with a one-line plain-English condition
- ML Analysis panel — next-candle direction, ML regime confidence, and entry-quality score/grade with top contributing factors
- Signal History panel — per-coin summary cards plus a searchable table of every past signal computation

See Section 11 for the full signal computation, strategy-matching, and ML methodology behind this tab.

---

## 11. Market Signals & ML Analysis System

### 11.1 Overview

In addition to the DCA trading engines, Infinity runs a real-time **Market Signals** system (`/api/market/signals`) that continuously evaluates BTC, ETH, XRP and SOL on the 4-hour timeframe. For each coin it produces:

- A rule-based **regime score** (BULL / NEUTRAL / BEAR)
- A **strategy recommendation** — the best matching saved strategy from `config/coins.json`, or a sensible preset
- **Anchor and DCA trigger prices** so the user knows exactly where to place orders
- An **entry-readiness verdict** (LONG NOW / SHORT NOW / WATCH / WAIT) combining regime with RSI timing
- Three **machine-learning models** (`core/ml_signals.py`) that independently predict direction, regime, and entry quality
- Persistence of every fresh computation to a SQLite **Signal History** database (`core/signal_history.py`)

All of this is rendered live on the dashboard's Market Signals and Signal History panels (Section 10).

### 11.2 Regime Detection — The 4-Signal Score

For each coin, the last 200 four-hour candles are pulled from Binance public klines (`GET /api/v3/klines`, `interval=4h`, `limit=200`). From the closing prices the system derives:

| Variable | Formula |
|----------|---------|
| EMA50 | 50-period exponential moving average of closes |
| EMA200 | 200-period exponential moving average of closes |
| RSI(14) | 14-period relative strength index |
| 7-day return | `(price - close[-43]) / close[-43] × 100` (42 candles ≈ 7 days at 4h) |

Four boolean signals are scored:

| Signal | Description | Bullish if... |
|--------|-------------|----------------|
| s1 | Price vs EMA50 | price > EMA50 |
| s2 | EMA50 vs EMA200 (Golden / Death Cross) | EMA50 > EMA200 |
| s3 | RSI momentum | RSI(14) > 55 |
| s4 | Weekly trend | 7-day return > 0 |

```
score = s1 + s2 + s3 + s4   (range 0–4)
```

| Score | Regime | Base Recommendation (`rec`) |
|-------|--------|------------------------------|
| 3–4 | BULL | Long DCA |
| 2 | NEUTRAL | Mixed DCA |
| 0–1 | BEAR | Short DCA |

### 11.3 Strategy Recommendation Engine

Knowing the regime is not enough — the dashboard also tells the user **which specific strategy and levels** to use.

**Step 1 — Match a saved strategy.** `_best_saved(coin, regime)` scans `config/coins.json` for strategies on that coin:
- Regime BULL + a saved strategy whose `dump_levels` are all negative (a long strategy) → use it
- Regime BEAR + a saved strategy whose `dump_levels` are all positive (a short strategy) → use it
- Regime NEUTRAL → use the first saved strategy for that coin
- Otherwise → fall back to the first saved strategy for that coin (if any exists)

**Step 2 — Fall back to a preset.** If no saved strategy matches, `_PRESETS` supplies a level set keyed by `(regime, score)`:

| Regime / Score | Name | Levels | Take Profit |
|-----------------|------|--------|-------------|
| BULL / 4 | Conservative | 6%, 10%, 15% | 5% |
| BULL / 3 | Standard | 8%, 12%, 18%, 24% | 8% |
| NEUTRAL / 2 | Cautious | 8%, 12%, 18% | 6% |
| BEAR / 1 | Standard Short | 6%, 10%, 15% | 5% |
| BEAR / 0 | Aggressive Short | 8%, 12%, 18%, 24% | 8% |

Each card shows whether its recommendation came from a **saved** strategy (`strat_source: "saved"`) or a **suggested** preset (`strat_source: "suggested"`).

### 11.4 Anchor & DCA Trigger Prices

To turn a level list into actionable order prices, the system computes an **anchor**:

```
anchor = max(high) over the last 84 four-hour candles   (≈ 14 days)
```

DCA entry trigger prices are the anchor pulled back by each recommended level:

```
trigger_price[i] = anchor × (1 − level[i] / 100)
```

**Levels already passed.** If price has already fallen through a trigger (`trigger_price > current_price`), that level is marked **passed** — shown with a strikethrough on the dashboard, since placing that order now would fill instantly at a worse price than originally intended.

```
levels_passed = count(trigger_price[i] > current_price)
```

**Fresh-start anchor.** When one or more levels have been passed, the dashboard also computes a fresh set of trigger prices anchored to the **current price**, so the same level spacing can be deployed starting from where the market is right now:

```
fresh_anchor     = current_price
fresh_trigger[i] = fresh_anchor × (1 − level[i] / 100)
```

### 11.5 Entry Readiness — Action & Timing

Regime alone tells you *direction*; it doesn't tell you *when*. The action engine combines regime with RSI to produce one of four verdicts, shown as a banner at the top of each coin's card:

| Verdict | Meaning |
|---------|---------|
| **LONG NOW** | Conditions favour opening/adding to a long DCA position immediately |
| **SHORT NOW** | Conditions favour opening/adding to a short DCA position immediately |
| **WATCH** | Direction is known but timing isn't right yet — keep an eye on it |
| **WAIT** | Neither direction nor timing currently favour an entry |

**BULL regime:**

| RSI | Action | Condition message |
|-----|--------|--------------------|
| < 40 | LONG NOW | "RSI oversold at X — strong dip entry" |
| 40–49 | LONG NOW | "RSI X — decent dip, good entry" |
| 50–59 | WATCH | "Wait for RSI to dip below 50 (now X)" |
| ≥ 60 | WAIT | "RSI too high for long entry — wait for pullback to RSI 50 (now X)" |

**BEAR regime:**

| RSI | Action | Condition message |
|-----|--------|--------------------|
| > 65 | SHORT NOW | "RSI overbought at X — strong pump to short into" |
| 56–65 | SHORT NOW | "RSI X — elevated, decent short entry" |
| 46–55 | WATCH | "Wait for RSI to rise above 55 (now X)" |
| ≤ 45 | WAIT | "RSI too low for short — wait for bounce to RSI 55+ (now X)" |

**NEUTRAL regime:** always **WATCH** — "No clear trend — wait for BULL or BEAR confirmation"

### 11.6 ML Analysis — Three On-the-Fly Models

`core/ml_signals.py` trains three lightweight scikit-learn models **on the fly**, directly from the same 200 four-hour candles, every time signals are computed.

#### 11.6.1 Shared Feature Vector

All ML models draw from the same 10-feature vector, computed at candle index `i` (requires ≥52 candles of history):

| # | Feature | Description |
|---|---------|-------------|
| 1 | `rsi14 / 100` | Normalized RSI |
| 2 | `r1` | 1-candle return (≈4h) |
| 3 | `r3` | 3-candle return (≈12h) |
| 4 | `r7` | 7-candle return (≈28h) |
| 5 | `r14` | 14-candle return (≈56h) |
| 6 | `vol` | Std-dev of returns over the last 21 candles |
| 7 | `price_pos` | Position within the 20-candle high/low range (0 = at low, 1 = at high) |
| 8 | `price/ema20 − 1` | Distance from the 20-EMA |
| 9 | `price/ema50 − 1` | Distance from the 50-EMA |
| 10 | `ema20/ema50 − 1` | EMA alignment (trend strength) |

#### 11.6.2 Model 1 — Next-Candle Direction

`predict_direction(closes, highs, lows)` — **RandomForestClassifier** (60 trees, max depth 4).

- Trains on every historical candle from index 60 onward, labelling each `1` (next candle closed higher) or `0` (lower)
- The most recent 5 samples are held out from training
- Predicts the direction of the **next 4h candle** from the current feature vector

```json
{"direction": "UP" | "DOWN", "confidence": 55.1}
```

`confidence` is the model's predicted probability for the winning class — 50% is a coin flip, 100% is certain. Returns `"UNKNOWN"` if fewer than 80 candles are available or the training labels lack variance (e.g. price only ever moved one direction).

#### 11.6.3 Model 2 — ML Regime Classifier

`classify_regime(closes, highs, lows, rule_score, rule_regime)` — **GradientBoostingClassifier** (60 estimators, max depth 3).

- Pseudo-labels are generated for every historical candle using the same 4-signal scoring as Section 11.2 (price vs EMA50, EMA50 vs EMA200, RSI > 55, 7-day return > 0): score ≥ 3 → BULL, score ≤ 1 → BEAR, else NEUTRAL
- The model learns to reproduce these labels from the feature vector, then predicts the regime for the *current* candle
- Final confidence blends the model's own probability with the rule-based score:

```
confidence = ml_probability × 0.65 + (rule_score / 4 × 100) × 0.35
```

```json
{"regime": "BULL" | "NEUTRAL" | "BEAR", "confidence": 71.4, "agrees_with_rules": true}
```

`agrees_with_rules` is `true` when the ML regime matches the rule-based regime from Section 11.2 — a quick cross-check between the two independent methods.

#### 11.6.4 Model 3 — Entry Quality Score

`score_entry(closes, highs, lows, rec)` — a **rule-weighted composite score**, not a trained model. Starts at 50 and adjusts based on how favourable current conditions are for the recommended direction (`rec`).

- **Long DCA:** points are added for oversold RSI, price near the bottom of its 20-day range, and a recent weekly decline (better dip-buying conditions); points are subtracted for overbought RSI or a price that has already risen this week.
- **Short / Mixed DCA:** the logic mirrors this — overbought RSI, price near the top of its range, and a recent rally add points.
- **Both directions** receive a volatility adjustment: very high volatility (std-dev of returns > 5%) subtracts points, low volatility (< 1.2%) adds points.

```json
{
  "score": 78,
  "grade": "B",
  "factors": [
    ["RSI oversold", "+15", true],
    ["Price in lower range", "+10", true],
    ["Weekly decline", "+8", true],
    ["Low volatility — stable", "+5", true]
  ]
}
```

| Score | Grade |
|-------|-------|
| 80–100 | A |
| 65–79 | B |
| 50–64 | C |
| 35–49 | D |
| 0–34 | F |

The dashboard shows the grade badge plus the top 4 contributing factors, each marked with a coloured dot (green = favourable, red = unfavourable, grey = neutral).

### 11.7 Signal History — Persistence Layer

Every fresh signal computation (i.e. not served from the 15-minute cache) is written to a local SQLite database at `data/signal_history.db` via `core/signal_history.py`.

**Schema (`signal_history` table):**

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment row ID |
| `ts` | TEXT | UTC timestamp of computation |
| `coin` | TEXT | BTC / ETH / XRP / SOL |
| `price` | REAL | Price at computation time |
| `regime` | TEXT | BULL / NEUTRAL / BEAR |
| `score` | INTEGER | 0–4 regime score |
| `rsi` | REAL | RSI(14) |
| `week_ret` | REAL | 7-day return % |
| `rec` | TEXT | Long DCA / Short DCA / Mixed DCA |
| `ml_direction` | TEXT | UP / DOWN / UNKNOWN |
| `ml_direction_conf` | REAL | Model 1 confidence % |
| `ml_regime` | TEXT | Model 2 regime |
| `ml_regime_conf` | REAL | Model 2 confidence % |
| `ml_agrees` | INTEGER | 1 if Model 2 agrees with the rule-based regime |
| `entry_score` | INTEGER | Model 3 score (0–100) |
| `entry_grade` | TEXT | Model 3 grade (A–F) |
| `action` | TEXT | LONG NOW / SHORT NOW / WATCH / WAIT |
| `entry_ready` | INTEGER | 1 if action is a "NOW" verdict |

Indexed on `(ts, coin)` for fast time-range queries.

**API:** `GET /api/signals/history?coin=BTC&days=7`

```json
{
  "rows":    [ { "ts": "...", "coin": "BTC", "price": 68500.0, "regime": "BEAR", "...": "..." } ],
  "summary": [ { "coin": "BTC", "regime": "BEAR", "cnt": 12, "avg_entry_score": 64.2, "avg_rsi": 58.3, "avg_price": 67890.5 } ],
  "days": 7
}
```

- `rows` — raw history, most recent first, optionally filtered by coin
- `summary` — per-coin/per-regime aggregates (count, average entry score, average RSI, average price) over the window

**Dashboard panel.** The Signal History panel offers a coin filter and a time-range filter (1 / 3 / 7 / 30 days), and shows:
- Per-coin summary cards (regime distribution, average entry score, average RSI/price)
- A table of every persisted computation: time, coin, price, regime (as filled/empty score dots), RSI, action, ML direction, and entry grade

### 11.8 Refresh Cadence & Caching

Signal computation calls Binance four times and trains three ML models per coin — too expensive to run on every page load. Results are cached server-side for **15 minutes** (`_SIGNALS_TTL = 900` seconds); a request with `?bust` forces a fresh computation. The dashboard auto-refreshes the Market Signals and Signal History panels on the same 15-minute interval.

---

## 12. Data Persistence

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

## 13. Exchange Integration

- **Exchange:** Binance (spot markets only)
- **Order types:** Market buy, market sell
- **Supported:** Testnet mode for safe testing
- **Safety:** `binance_client.py` verifies fills, handles lot size precision (step size), and prevents zero-quantity orders

---

## 14. Configuration

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

## 15. CLI Usage

```bash
python main.py                        # Run all enabled strategies
python main.py --coin BTC             # Run BTC strategies only
python main.py --coin BTC --set-ref   # Set reference = current price, then run
python main.py --coin BTC --ref 95000 # Set manual reference price, then run
python main.py --status               # Print position status and exit
```

---

## 16. Infrastructure & Deployment

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

## 17. What the System Aims to Achieve

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

## 18. Planned / Possible Extensions

- **Telegram / email alerts** on buy/sell events
- **Multiple exchange support** (OKX, Bybit)
- **Dynamic position sizing** based on portfolio value
- **Trailing take profit** to capture extended uptrends
- **Risk controls** — max drawdown limits, daily loss caps
- **Multi-account portfolio view** — aggregate P&L across all accounts
- **Strategy optimizer** — auto-tune DCA levels based on backtest results
- **Live mixed strategy engine** — port the regime detector and entry indicator to the live `DCAEngine` for fully autonomous bull/bear switching
- **Direction-aware strategy matching** — restrict saved strategies suggested for Short DCA to those whose levels were originally designed for short entries, rather than mirroring long-only level sets
- **Charting for Signal History** — equity-style charts of regime, RSI, and entry score over time per coin

---

*Infinity — built for systematic, emotion-free DCA trading on spot markets.*
