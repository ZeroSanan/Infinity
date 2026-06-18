# Infinity — Dynamic Spot DCA Trading System
## Technical White Paper

---

## 1. Overview

**Infinity** is an automated cryptocurrency trading system built around a **Dynamic Dollar-Cost Averaging (DCA)** strategy. It monitors live market prices, executes tiered buy orders when an asset drops to predefined levels, and automatically takes profit once the portfolio reaches a target return. The default strategy type operates on spot markets (no leverage), running continuously on a VPS with a web-based dashboard for monitoring and control.

**Core idea:** Instead of trying to time the market, Infinity places increasingly larger buy orders as an asset falls. As the price recovers, the blended average entry price is much lower than the initial reference price, making it easier to profit even on a partial recovery.

**Strategy types:**

| Strategy type | Market | Leverage | Risk profile |
|---|---|---|---|
| **Spot DCA** (default) | Spot | None | Capital-bounded — a 100% drop cannot lose more than the capital allocated to that strategy. No liquidation risk. |
| **Mixed Long/Short DCA** (opt-in, live) | Binance USD-M Futures | User-selected (`leverage` field, ≥1×) | Auto-switching long/short DCA driven by live regime detection (§8.6). Liquidation risk applies — leverage amplifies both gains and losses. |

Every account and coin defaults to **Spot DCA**. The Mixed Long/Short DCA strategy type (`mode="mixed"` in `config/coins.json`, implemented by `core/mixed_engine.py`) is a separate, opt-in mode that a user must explicitly enable per coin/strategy — either by hand-editing that strategy's config or by creating a reusable **DCA Model** template (§10.3) and applying it to a coin card. Enabling it for one strategy does not change the leverage-free behavior of other `mode="long"` strategies.

---

## 2. System Philosophy

This is **not** a maximum-profit strategy. The goal is not to catch tops or bottoms. The goal is:

| Principle | Meaning |
|-----------|---------|
| **Survivability** | The default Spot DCA strategy never uses leverage or futures. A 100% drop cannot wipe out more than the capital allocated to that strategy. |
| **Mechanical execution** | Every decision is rule-based. There is no discretion, no panic selling, no FOMO buying outside the configured levels. |
| **Volatility harvesting** | Crypto markets are highly volatile. Infinity turns that volatility into an asset — each dip is an opportunity to accumulate at a lower average cost. |
| **Stable compounding** | Small, consistent profits accumulate over time. §9.1's backtests are built around an empirically-derived 3% take-profit — it is the single most recurring parameter across the top-performing configurations. After realistic Binance spot round-trip costs (~0.1–0.2%, see §19.1, which are not modeled in the §9 backtests), the net edge per cycle is closer to 1–2%. A 1.5% gain repeated 20 times grows capital by roughly 35%. |
| **Long-term capital growth** | The system is designed to run indefinitely, cycling through bull and bear periods without human intervention. |

> **A note on leverage:** The Survivability guarantee above describes the default **Spot DCA** strategy type, which remains leverage-free. A separate **Mixed Long/Short DCA** strategy type (`mode="mixed"`, see §1 and §8.6) is available for users who explicitly opt in per coin/strategy — either directly or via a DCA Model template (§10.3). That strategy type carries leverage and liquidation risk by design and does not share the capital-bounded guarantee — it is an isolated, user-selected choice that does not affect the leverage-free default.

**What it is not (default Spot DCA strategies):**
- Not a high-frequency trader
- Not a leverage or futures system by default — leverage and futures only apply to the separate, opt-in Mixed Long/Short DCA strategy type (`mode="mixed"`, §8.6)
- Not a system that requires *precise* market timing — the reference price only needs to be roughly near a local high for the DCA ladder to work, not the exact top (see §19.3 for an honest discussion of how much the reference price still matters)
- Not a maximum-profit chaser
- Not emotionally driven

---

## 3. Architecture

```
Infinity/
├── main.py                  # CLI entry point — starts trading engines
├── core/
│   ├── dca_engine.py        # Trading logic: price polling, buy/sell execution (Spot DCA, mode="long")
│   ├── mixed_engine.py      # Mixed long/short engine: live auto-switching DCA on Binance USD-M Futures (mode="mixed")
│   ├── binance_client.py    # Binance API wrapper (spot orders, price feed)
│   ├── binance_futures_client.py # Binance USD-M Futures API wrapper (orders, leverage, position/liquidation info)
│   ├── regime_detector.py   # Weekly Regime tab analysis (EMA, volume, AI narrative)
│   ├── regime_live.py       # Live regime detection (EMA21/50/200, RSI, BB, ATR) feeding the Mixed engine
│   ├── state_manager.py     # Persists position state to JSON files
│   ├── ml_signals.py        # Orphaned from the live dashboard — used only by signal_lab's optional --ml-filter overlay (§11.9)
│   ├── signal_history.py    # Orphaned — superseded by the Layer 1/2/3 Market Signals system (§11.9)
│   ├── testnet_journal.py   # Persists/aggregates completed Testnet-mode paper trades (§10.6)
│   └── logger.py            # Structured logging
├── models/
│   └── dca_config.py        # Data models: CoinConfig (long + mixed + is_testnet fields), PositionState, ExecutedStep, DCAModel
├── config/
│   ├── coins.json           # Strategy definitions (coins, levels, sizes, TP, mode, leverage, ATR spacing, is_testnet)
│   ├── accounts.json        # Binance API accounts — each flagged live or testnet (§10.4, §10.6)
│   └── dca_models.json       # DCA Model templates — reusable bull+bear ladders for Mixed strategies
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
        ├── index.html       # Live trading dashboard (Dashboard, Accounts, Strategies, DCA Models, Weekly Regime tabs)
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

### 4.1.1 Flush-and-Reclaim Anchor (Preferred Method)

A more defensible way to choose the reference price than "current price, eyeballed near a top" is to anchor it to a specific, observable price-action event instead of a subjective guess:

1. **Identify a Layer 2 liquidation cluster below the current price** (§11.3) — read off an external liquidation-heatmap tool and entered into the dashboard's cluster card.
2. **Wait for price to flush down into that cluster** — a sharp move into the cluster level, consistent with forced liquidations clearing out leveraged positions there.
3. **Wait for price to reclaim the cluster on a 4H candle close** — the candle closes back above the cluster level, not just wicking through it intraday.
4. **Use the reclaim candle's close as the reference price** — this is the value entered into the dashboard or CLI to start the strategy (§4.1).
5. **Start the strategy with that reference price.**

**Why this is better than an eyeballed top.** The anchor is tied to an observable, timestamped event instead of a subjective "this looks like a top" judgment. The flush suggests forced sellers have already been cleared out at that level — their liquidation-triggering positions no longer exist to sell again. The reclaim suggests real buying demand absorbed the flush rather than the level simply being passed through on the way to somewhere lower. Together, they argue the flushed level is a more durable line than an arbitrary recent high.

**Honest caveat.** This is still a manual technique — there is no code anywhere in the system that detects a "flush" or a "reclaim." The user is still the one watching the chart, reading the cluster value, and deciding when a 4H close counts as a genuine reclaim. It replaces one judgment call (set the price near a top) with a different, more anchored judgment call (read a confirmed event off a chart). See §19.3 for the limits of even this improved version.

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

After each buy, the system recalculates the **true average cost basis** — total USDT spent per coin held — across all executed steps:

```
avg_entry = Σ(order_size_usdt) / Σ(order_size_usdt / entry_price)
          = total_invested / total_quantity
```

Implemented in `utils/calculations.py`:

```python
def calc_weighted_average_entry(executed_sizes, executed_prices):
    total_size     = sum(executed_sizes)
    total_quantity = sum(s / p for s, p in zip(executed_sizes, executed_prices))
    return total_size / total_quantity
```

Since `total_invested` and `total_quantity` are already accumulated incrementally on every fill, `core/state_manager.py` computes this directly as `total_invested / total_quantity` — the same formula, no extra pass over the executed steps required.

**Why this matters:** A naive size-weighted average of *prices* (`Σ(size × price) / Σ(size)`) does **not** equal the true cost basis when order sizes are denominated in USDT — it systematically overstates the average entry price, which would understate realized P&L and push the TP trigger slightly higher than necessary. `total_invested / total_quantity` is the actual blended cost per coin, so a TP at `avg_entry × (1 + tp%)` realizes *exactly* `tp%` profit on the capital deployed (see the worked example in §4.5). This is also the formula used by the backtesting engines (§8), so live and backtested P&L are computed identically.

**Worked example** (P₀ = $100,000, BTC):

| Step | Dump % | Trigger Price | Order Size | Coins Bought | Avg Entry | Avg Entry % Below P₀ |
|------|--------|--------------|------------|--------------|-----------|----------------------|
| 1 | -10% | $90,000 | $1,500 | 0.01667 BTC | $90,000 | -10.00% |
| 2 | -15% | $85,000 | $2,000 | 0.02353 BTC | $87,073 | -12.93% |
| 3 | -20% | $80,000 | $2,750 | 0.03438 BTC | $83,813 | -16.19% |
| 4 | -25% | $75,000 | $5,500 | 0.07333 BTC | $79,443 | -20.56% |
| 5 | -30% | $70,000 | $5,000 | 0.07143 BTC | $76,368 | -23.63% |
| 6 | -35% | $65,000 | $5,000 | 0.07692 BTC | $73,416 | -26.58% |

After step 6, the asset only needs to recover to **$73,416** (from $65,000) for take profit to trigger — a **12.95% recovery** rather than a 53.8% full round-trip back to P₀.

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

This is **portfolio-level profit**, not per-step profit. The system measures profit against the blended average entry across all executed steps. For example, using the step 1–3 figures from §4.4:

```
avg_entry = $83,812.65  (after steps 1–3)
total_quantity = 0.074571 BTC
total_invested = $6,250
TP = 10%
exit_price target = $83,812.65 × 1.10 = $92,193.92

P&L = (exit_price - avg_entry) × total_quantity
    = ($92,193.92 - $83,812.65) × 0.074571 BTC
    = +$625.00 USDT
    = +10% on the $6,250 invested  (exact, by construction)
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

### 8.6 Live Mixed Engine — `core/mixed_engine.py`

The adaptive bull/bear logic described in §8.4 is not only a backtest concept — it runs **live**, on Binance USD-M Futures, for any strategy whose `config/coins.json` entry has `mode: "mixed"`. `MixedEngine` is the live counterpart to `MixedDCAStrategy`: same regime-scoring, 5-candle confirmation, extremity-override, and entry-indicator logic, ported to operate on a rolling window of live klines (`core/regime_live.py`) instead of a historical CSV.

#### 8.6.1 Tick Cycle

`MixedEngine.tick()` runs once per `POLL_INTERVAL` (30 seconds):

1. **Fetch data** — `core/regime_live.fetch_candles()` pulls the latest `CANDLE_LIMIT` (500) candles for `cfg.symbol` at `cfg.regime_interval` (default `1h`) from Binance's public klines endpoint; `client.get_mark_price()` fetches the current futures mark price.
2. **Compute regime** — `compute_regime_state()` replays the full candle window through the same Layer 1/2/4 state machine as §8.4.1–8.4.4 (regime score, 5-candle confirmation, extremity overrides) and returns the confirmed regime, `active_mode` (BUY/SELL/WAIT), and the latest indicator values including `atr_pct` (§8.6.4).
3. **Mode change** — if `active_mode` flipped since the last tick, any open position is closed (`reason="mode_change"`) and the ladder anchor is cleared.
4. **Arm the anchor** — if no position is open, `_arm_anchor()` either waits for the Layer 3 entry indicator (§8.4.5, when `use_entry_indicator=True`) and anchors to `ema21` at the signal candle, or — when the indicator is disabled — tracks a rolling extreme price as the anchor.
5. **Check ladder fills** — `_check_ladder_fills()` compares the % move from the anchor against the next configured level (`dump_levels`/`order_sizes` for BUY, `bear_levels`/`bear_order_sizes` for SELL), opening a market order on Binance USD-M Futures when triggered.
6. **Stop-loss / take-profit** — `_check_stop_loss()` (using `bull_stop_loss_percent`/`bear_stop_loss_percent`) and `_check_take_profit()` (using `take_profit_percent`/`bear_take_profit_percent`) are evaluated against the current position's `average_entry`.

State is persisted to `data/<strategy_id>.json` after every tick via the same `PositionState` model used by `DCAEngine`, extended with mixed-mode fields (§12).

#### 8.6.2 Position Sides & Leverage

- `state.direction` tracks the currently open broker position: `NONE`, `LONG`, or `SHORT`.
- `cfg.leverage` (≥1) is applied to every order's notional (`notional = order_size_usdt × leverage`); `client.set_leverage()` is called once on engine startup if the configured leverage differs from the persisted state.
- After every fill, `_refresh_liquidation_price()` queries `client.get_position()` and stores the exchange-reported `liquidation_price` in `PositionState` so the dashboard can display it.
- Both hedge-mode and one-way Binance Futures account modes are supported (`client.get_position_mode()`).

#### 8.6.3 Re-arming After Take-Profit

Unlike the backtest (which simply records a closed trade), the live engine **re-arms the ladder in the same direction** after a take-profit close: `state.anchor_price` is reset to the exit price, so the engine immediately starts tracking for the next entry in the still-confirmed regime, without waiting for a new regime confirmation.

#### 8.6.4 ATR-Based Dynamic Spacing

Both `core/mixed_engine.py` and `models/dca_config.py::DCAModel` support an optional **ATR-based dynamic spacing** mode, controlled by two fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `atr_based_spacing` | `False` | When `True`, every configured level/TP/SL value is interpreted as a **multiple of the live ATR%**, not a fixed percentage |
| `atr_period` | `14` | Number of candles (in `regime_interval` units) used to compute ATR |

On every tick, `compute_regime_state()` computes `atr = _atr(highs, lows, closes, atr_period)` (Average True Range over the last `atr_period` candles) and `atr_pct = atr / latest_close × 100`. `MixedEngine._effective_pct(base_value)` then returns:

```
effective_pct = base_value × atr_pct   if atr_based_spacing and atr_pct is available
effective_pct = base_value             otherwise (feature off, or ATR not ready yet)
```

This is applied to `dump_levels`, `bear_levels`, `take_profit_percent`, `bear_take_profit_percent`, `bull_stop_loss_percent`, and `bear_stop_loss_percent`. For example, with `atr_based_spacing=True` and a configured level of `-6` (meaning "−6× ATR%"): if live ATR% is 1.5%, the effective trigger distance from the anchor is −9%; if ATR% rises to 3% (more volatile), the same `-6` becomes −18% — the ladder automatically widens in choppier markets and tightens in calmer ones, instead of using a fixed spacing regardless of volatility.

The Market Signals tab's Layer 3 ATR card (§11.4) surfaces a live `atr_pct` and a Calm/Normal/Elevated/High Volatility read for the selected coin, so a user can gauge current volatility before choosing fixed-percent vs. ATR-based spacing for a new strategy or DCA Model.

---

## 9. Backtested Top Strategies

The following strategies were discovered by the optimizer running exhaustive grid search over BTC/USDT 1-hour candles from **2018 to 2025** (7 years, ~61,000 candles). All results use a starting budget of **$1,000 USDT** with equal capital split across DCA levels. Strategies are ranked by **ROI/day** — **total ROI % divided by the strategy's average trade duration in days** (`roi_per_day` in `algo-trading/optimizer.py`) — which rewards configurations that complete each buy→TP cycle quickly and redeploy capital sooner.

> **Important:** ROI/day here is *not* an annualized or compounding daily return over the full 7-year window — it is total return normalized by the typical single-cycle holding period. A strategy showing "138% ROI, ROI/day 44.4" completed that 138% across ~317 trades with an average 3.1-day hold each, **not** 44.4% per day for 7 years. Treat it as a "capital turnover efficiency" score for comparing strategies, not a literal daily yield.

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

### 10.1 Live Trading Tab
- Real-time price display per strategy
- Position status: dump %, average entry, total invested, P&L
- Step-by-step progress visualization
- Start / Stop engine per strategy
- Set reference price manually
- Reset position state

### 10.2 Backtesting Tab

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

### 10.3 DCA Models Tab

The **DCA Models** tab manages reusable `DCAModel` templates (`config/dca_models.json`, `models/dca_config.py::DCAModel`) — pre-built bull (long) + bear (short) DCA ladders, plus regime/leverage settings, that can be applied to one or more coin cards in the Strategies tab to turn them into auto-switching **Mixed** strategies (§8.6).

**Create / edit a model** (`POST` / `PUT /api/dca_models`) — a model is defined by:

| Field | Description |
|-------|-------------|
| `name` | Display name for the template |
| `bull_levels` / `bull_order_sizes` | Negative-% DCA levels and USDT sizes for the long ladder |
| `bull_take_profit_percent` | Take-profit % for the long side |
| `bear_levels` / `bear_order_sizes` | Positive-% DCA levels and USDT sizes for the short ladder |
| `bear_take_profit_percent` | Take-profit % for the short side |
| `leverage` | Futures leverage applied to every order (≥1×) |
| `bull_stop_loss_percent` / `bear_stop_loss_percent` | Optional stop-loss % per side (0 = disabled) |
| `use_entry_indicator` | Toggle the three-factor entry confirmation (§8.4.5) |
| `rsi_overbought` / `rsi_oversold` | Extremity-override thresholds (§8.4.4) |
| `regime_interval` | Candle interval used for live regime detection (default `1h`) |
| `atr_based_spacing` | When on, all of the above level/TP/SL values are treated as ATR multiples (§8.6.4) instead of fixed percentages |
| `atr_period` | Candles used for the live ATR calculation when `atr_based_spacing` is on |

A model's ladders are validated the same way as `DCAModel.validate()`: `bull_levels` must all be negative, `bear_levels` all positive, both ladders' level/size lists must be the same length, and both take-profit percentages must be > 0.

**Apply a model** (`POST /api/dca_models/<id>/apply`) — applies a model to one or more existing strategies, either by `strategy_ids` (a list of coin-card IDs from the Strategies tab) or `apply_to_all: true`. For each target strategy, the model's fields (`mode`, `step_count`, `dump_levels`, `order_sizes`, `take_profit_percent`, `bear_levels`, `bear_order_sizes`, `bear_take_profit_percent`, `leverage`, `bull_stop_loss_percent`, `bear_stop_loss_percent`, `use_entry_indicator`, `rsi_overbought`, `rsi_oversold`, `regime_interval`, `atr_based_spacing`, `atr_period`) are merged into the strategy's `config/coins.json` entry — overwriting any prior ladder/mode settings — while `id`, `name`, `coin`, `symbol`, `enabled`, and `reference_price` are preserved. This switches the strategy from `mode="long"` to `mode="mixed"`, and the live engine picks it up as a `MixedEngine` on its next reload.

> **Note:** Apply is a **one-time copy**, not a live link. There is no `model_id` back-reference stored on the strategy — editing a DCA Model after applying it does **not** retroactively change strategies it was previously applied to. To propagate a model change, re-apply it to the affected strategies.

### 10.4 Accounts Tab
- Add/remove Binance API accounts
- Test connectivity and view USDT balance
- Supports both live and testnet accounts

### 10.5 Market Signals Tab

The default tab on load. Covers **BTC, ETH, SOL, ZEC, and XAUT** (Tether Gold), selected via a coin-tab strip at the top:
- An embedded TradingView candlestick chart (4h interval) for the selected coin
- A **Master Summary Bar** combining three independently-scored layers into one composite read
- **Layer 1 — Macro Environment**, **Layer 2 — Market Positioning**, and **Layer 3 — Entry Timing** — each collapsible, each with its own verdict badge
- A **Pre-Trade Checklist** — a five-item worksheet (signal alignment, entry, take profit, stop loss, position size) with a built-in take-profit analysis and a live risk:reward summary
- A **DCA Model Level Visualizer** — an ATR-calibrated preview of a DCA Model's ladder against the live price
- An **AI Analysis Panel** — an on-demand, Claude-generated narrative read of the current Layer 1/2/3 state, in a Professional or Plain English style

See Section 11 for the full signal computation behind each layer, the checklist, the AI panel, and the DCA Visualizer.

### 10.6 LIVE / TESTNET Dashboard Mode

A header-level **LIVE / TESTNET** toggle switches every tab — Dashboard, Accounts, Strategies, and the Market Signals strategy context — between live trading and Binance Testnet paper trading, without touching any live state.

**How it's wired:**
- Each Binance account in `config/accounts.json` already carried a `testnet: bool` flag (used by `get_client()`/`get_futures_client()` to route to `testnet.binance.vision` instead of the production API). The dashboard toggle is a pure UI/filter layer on top of that existing flag — it does not introduce a second trading path.
- `CoinConfig` gained an additive `is_testnet` field (default `false`). A strategy flagged `is_testnet=true` is a paper-trading strategy: same engine code (`core/dca_engine.py` / `core/mixed_engine.py`), same state-file format, just pointed at a testnet account.
- **Mode switch (UI only):** clicking **TESTNET** shows a one-time confirmation modal (no real orders will be placed); clicking back to **LIVE** switches immediately, no confirmation needed. The selected mode is stored in the browser's `localStorage` (`dashboard_mode`) and persists across reloads.
- **Filtering, not isolation at the API level:** once in TESTNET mode, every list the dashboard renders — saved strategies, saved accounts, the running-engines bar, Start/Stop modals, USDT balance totals — is filtered client-side to `is_testnet`/`testnet` rows matching the active mode. The underlying `/api/strategies`, `/api/accounts`, and `/api/running` responses are unfiltered; the mode toggle decides what the UI shows, while a server-side guard (next bullet) decides what is allowed to *run*.
- **Mode-mismatch guard:** `POST /api/start_engine` rejects any request where a strategy's `is_testnet` flag doesn't match the target account's `testnet` flag (HTTP 400) — a testnet strategy can never be started against a live account, and vice versa, regardless of what the UI currently shows.
- **"Copy to Testnet"** (`POST /api/strategies/<id>/copy_to_testnet`) duplicates a live strategy's full configuration into a new strategy with a fresh id, `is_testnet=true`, and `reference_price` cleared — so a strategy can be rehearsed on paper money before being run live, without affecting the original.
- **Testnet Learning Journal** (`core/testnet_journal.py`) — every time a testnet strategy's state transitions through a completed cycle, `web/app.py` snapshot-diffs the position state and appends a journal entry (entry/exit price, P&L, signal context at entry) to `data/testnet_journal.json`. The Strategies tab shows this as a table with win-rate/avg-win/avg-loss stats and a CSV export, when in TESTNET mode. This hooks in entirely from `web/app.py`'s polling, with no changes to `core/dca_engine.py`'s execution path. If a Pre-Trade Checklist TP plan (§11.10) was synced for that coin before the trade closed, the entry also carries a `tp_analysis` comparison against the actual exit, and once 10+ entries have that data the journal additionally shows a TP-accuracy summary (§11.10).
- **Visual cues while in TESTNET mode:** a non-dismissible amber banner below the header, and an amber border under the tab row — both intended to make it visually unmistakable that the dashboard is not looking at live data.

**What does not change:** the live trading engine (`infinity.service`) is a separate `systemd` process from the dashboard (`infinity-web.service`, §16) and has no concept of this toggle at all — it runs whatever live strategies are enabled in `config/coins.json`, exactly as before. The toggle only affects what the Flask dashboard *displays and permits starting via its own API*; it cannot start, stop, or alter a live strategy's behavior beyond the normal Start/Stop controls that existed already.

> ⚠️ Planned — not yet implemented in code.
> A more fully isolated testnet deployment has been discussed but not built: a separate `infinity-testnet.service` systemd unit running `main.py` with a dedicated `--testnet` CLI flag, reading from its own `config/testnet_coins.json` file (mirroring `coins.json`'s schema) instead of sharing one config file and one engine process with live strategies. Today there is exactly one `coins.json`, one engine process per running strategy, and one `is_testnet` boolean field distinguishing rows within the same file — the process- and config-level isolation described above is architectural intent, not current behavior. Designed but awaiting build.

---

## 11. Market Signals System

### 11.1 Overview

The Market Signals tab (§10.5) evaluates **BTC, ETH, SOL, ZEC, and XAUT** (Tether Gold) through three independently-scored layers, each answering a different question:

| Layer | Question | Route | Cache |
|-------|----------|-------|-------|
| **Layer 1 — Macro Environment** | Is the broader macro backdrop favourable for risk assets right now? | `GET /api/layer1` | 900s, global (one shared cache for all coins) |
| **Layer 2 — Market Positioning** | Is this specific coin's futures market crowded, and on which side? | `GET /api/layer2/<symbol>` | 300s, per-symbol |
| **Layer 3 — Entry Timing** | Does the immediate price action favour entering long or short right now? | `GET /api/layer3/<symbol>` | 120s, per-symbol |

A client-side **Master Summary Bar** combines the three layers' verdicts into a single composite read (§11.5). On top of the three layers sit three on-demand tools: a **Pre-Trade Checklist** that walks through a trade setup and its take-profit logic before any order is placed (§11.10), a **DCA Model Level Visualizer** that previews a DCA Model's ladder against the live price and any layer data (§11.6), and an **AI Analysis Panel** that asks Claude to narrate the current combination of signals — including the checklist's take-profit plan, when one exists — in plain language (§11.7).

Layer 1 is intentionally coin-agnostic (macro conditions don't depend on which coin is selected), while Layers 2 and 3 are fetched per-symbol and refetched whenever the user switches coins (subject to their respective caches).

### 11.2 Layer 1 — Macro Environment

Seven live indicators are fetched in parallel on each cache miss:

| Indicator | Source | Needs a key? |
|-----------|--------|----------------|
| Fear & Greed Index | alternative.me `/fng/` (last 30 days) | No |
| BTC Dominance | CoinGecko `/global` | No |
| DXY (US Dollar Index) | Twelve Data `/time_series` (1-day, 30 bars) | `TWELVE_DATA_API_KEY` |
| Fed Funds Rate | FRED series `FEDFUNDS` | `FRED_API_KEY` |
| US 10-Year Treasury Yield | FRED series `DGS10` | `FRED_API_KEY` |
| CPI (YoY inflation) | FRED series `CPIAUCSL` | `FRED_API_KEY` |
| VIX | Twelve Data `/time_series` (1-day, 7 bars) | `TWELVE_DATA_API_KEY` |

Each indicator computes its own `signal` (`+1` bullish for crypto / `0` neutral / `−1` bearish) from its own thresholds — e.g. Fear & Greed ≤44 is bullish (contrarian: fear favours buying), BTC Dominance falling >0.5% in 24h is bullish (altcoin rotation), DXY/VIX/yield/CPI rising or elevated is bearish, Fed Funds in a cutting cycle is bullish. `GET /api/layer1` itself combines whichever indicators returned `status: "ok"` into a verdict (`_layer1_verdict`): fewer than 3 usable indicators → `INSUFFICIENT_DATA`; ≥4 bullish → `FAVORABLE`; ≥4 bearish → `UNFAVORABLE`; otherwise `MIXED`.

**Manual fallback and supplementary cards.** If `TWELVE_DATA_API_KEY` or `FRED_API_KEY` is unset, the corresponding indicator returns `status: "no_key"` and the dashboard renders a manual-entry card instead (DXY, Fed Funds, 10Y Yield, CPI, VIX) — the user looks the number up themselves and the value is persisted to `localStorage` (`layer1_<key>`), substituting for the missing live value in the verdict. Four further indicators have **no live source at all** and are always manual: CME FedWatch (next-meeting cut/hike probabilities), the latest jobs report (unemployment rate + direction), the BTC Rainbow Chart band (cycle position), and Global M2 YoY growth. The dashboard recomputes its own combined verdict client-side from up to 11 possible signals (7 base indicators, live or manual-substituted, plus the 4 always-manual ones): fewer than 3 counted → `INSUFFICIENT_DATA`; ≥6 bullish → `FAVORABLE`; ≥6 bearish → `UNFAVORABLE`; otherwise `MIXED`. This client-side verdict — not the simpler one returned by the raw `/api/layer1` JSON — is what drives the Layer 1 badge and feeds the Master Summary Bar.

**Manual card staleness.** Every manual entry (the 5 no-key fallback cards plus the 4 always-manual ones) timestamps itself when saved and re-renders a freshness note on every load: under 24 hours shows a green "✓ Updated _N_ hours ago"; 24 hours to 7 days shows an amber "⚠️ Entered _N_ days ago — verify current value"; past 7 days shows a red "⚠️ Value is _N_ days old — likely outdated". The value still counts toward the verdict regardless of staleness — the warning is a prompt for the user to re-check it, not an automatic exclusion.

### 11.3 Layer 2 — Market Positioning

For the selected coin's Binance USD-M Futures symbol (e.g. `BTCUSDT`), three signals are pulled from public futures endpoints:

| Signal | Source | What it measures |
|--------|--------|--------------------|
| **Funding Rate** | `GET /fapi/v1/fundingRate` (last 90 settlements) | Current 8h funding rate; >0.05% → "HIGH — Longs Crowded", <−0.01% → "Negative — Shorts Crowded" |
| **Open Interest** | `GET /futures/data/openInterestHist` (1h, 48 bars) + price | 24h change in OI combined with price direction, e.g. price↑ + OI↑ → "Strong — New Money Entering"; price↑ + OI↓ → "Weak — Short Covering Only" |
| **Long/Short Ratio** | `GET /futures/data/globalLongShortAccountRatio` and `topLongShortAccountRatio` (1h, 48 bars) | Retail (global) vs. top-trader account long%/short%; flags **divergence** when the two disagree by >10 points and sit on opposite sides of 50/50 — "top traders positioned opposite to retail" |

These combine into a verdict (`_layer2_verdict`): funding >0.05% *and* global longs >65% → `CAUTION_LONG` (longs are crowded and paying up — a long squeeze risk); funding <−0.01% *and* global shorts >65% → `CAUTION_SHORT`; otherwise a bull/bear tally across funding direction, crowding, and the OI label decides `NEUTRAL` or `MIXED`.

**Liquidation Heatmap (manual, context-only).** A separate input card lets the user record the nearest liquidation cluster price below and above the current price (read off an external liquidation-heatmap tool) per coin, persisted to `localStorage` (`liq_below_<coin>`, `liq_above_<coin>`, `liq_ts_<coin>`) with a staleness warning after 24 hours. This value is **not scored into the Layer 2 verdict** — it feeds the DCA Model Level Visualizer's cluster warnings (§11.6) and is passed to the AI Analysis Panel (§11.7) as context.

### 11.4 Layer 3 — Entry Timing

Layer 3 fetches the last 21 four-hour candles (`GET /api/v3/klines`) plus the top-20 order book (`GET /api/v3/depth`) for the selected symbol, and derives four directional signals (`+1`/`0`/`−1` each):

| Signal | Logic |
|--------|-------|
| **Volume Divergence** | Latest candle's volume vs. the 20-candle average. >1.2× average with price up → "Confirmed Move — Real Buyers" (+1); >1.2× with price down → "Confirmed Selling" (−1); <0.8× average flips the read (low-conviction move or exhaustion) |
| **Price Structure** | Higher-lows / lower-highs runs over the last 10 candles (≥2 consecutive higher lows → bullish structure; ≥2 consecutive lower highs → bearish; both at once → "Compression — Breakout Pending") |
| **Momentum** | ROC6 (6-candle ≈ 24h rate of change) vs. ROC14 (14-candle ≈ 56h); positive ROC6 accelerating relative to ROC14 → "Bullish Momentum Building" (+1), and the mirror image for bearish |
| **Order Book** | Bid value vs. ask value across the top 20 levels; >60% bid-side value → "Buy Pressure Dominant" (+1), <40% → "Sell Pressure Dominant" (−1) |

A fifth metric, **ATR(14)** on the same 4h candles, is computed for volatility context only (`signal: 0`, never counted in the verdict) — it classifies the coin as Very Calm / Normal / Elevated / High Volatility and suggests a DCA step-spacing band (e.g. "use wider spacing (10–15% steps)"), feeding the multiplier choice in the DCA Model Level Visualizer (§11.6).

The four directional signals sum into a verdict (`_layer3_verdict_calc`): score ≥2 → `LONG`; score ≤−2 → `SHORT`; score == 1 → `WEAK_LONG`; score == −1 → `WEAK_SHORT`; score == 0 → `NEUTRAL`; no signals available → `UNKNOWN`.

### 11.5 Master Summary Bar

A client-side function (`updateMasterSummary()`) combines the three layers' verdict codes into one composite read, shown as a banner above the layer cards:

| Condition | Result |
|-----------|--------|
| L1 `FAVORABLE` and L2 not `CAUTION_LONG` and L3 `LONG`/`WEAK_LONG` | 🟢 **ALIGNED LONG** — all layers bullish |
| L1 `UNFAVORABLE` and L2 `CAUTION_LONG` and L3 `SHORT`/`WEAK_SHORT` | 🔴 **ALIGNED SHORT** — all layers bearish |
| L1 `FAVORABLE` and L3 `LONG`/`WEAK_LONG`, but L2 is `CAUTION_LONG` | 🟡 **DEVELOPING** — missing L2 confirmation |
| L1 is `MIXED` or `INSUFFICIENT_DATA` | 🟡 **MIXED** — macro not fully clear |
| L2 is `CAUTION_LONG` or L3 is `SHORT`/`WEAK_SHORT` (none of the above matched) | 🟠 **CAUTION** — check individual layers |
| Anything else | ⚪ **WAIT** — layers not aligned |

This bar is computed entirely in the browser from the three layers' already-fetched verdict codes — it is not a separate API call, and it recomputes immediately whenever any layer's data refreshes or the coin selection changes.

### 11.5A Layer Interconnections

It's tempting to assume the three layers form a pipeline — Layer 1 gating Layer 2, Layer 2 gating Layer 3 — but that is not how they're built. Each layer fetches and computes its verdict completely independently, on its own cache TTL and its own `setInterval` (§11.8); none of the three layer routes reads another layer's output, and there is no point in the code where one layer blocks or withholds another from rendering. A user can have an `UNFAVORABLE` Layer 1 and a `LONG` Layer 3 on screen at the same time — nothing stops that from being displayed.

The one place the three layers actually meet is the **Master Summary Bar** (§11.5): a single client-side function reads all three already-computed verdict codes at once and maps the combination to one composite badge. That combination is the entire extent of "Layer 1 affecting Layer 2" or "Layer 3 affecting Layer 1" — there is no code where one layer's verdict re-scores or re-interprets another's.

There are, however, two genuine one-directional data hand-offs that exist outside the verdict logic, both feeding *into* tools below the layer cards rather than back into another layer's verdict:
- **Layer 2's liquidation cluster inputs** (§11.3) feed the DCA Model Level Visualizer's cluster warnings (§11.6) and the Pre-Trade Checklist's Take Profit Scenario 1 Target/Stretch levels and Scenario 2 distance check (§11.10).
- **Layer 3's ATR%** (§11.4) feeds the DCA Model Level Visualizer's step spacing and suggested multiplier (§11.6), and the Pre-Trade Checklist's Take Profit Scenario 1 levels and Scenario 2 ATR-multiple check (§11.10).

Neither hand-off changes how Layer 1 or Layer 2 itself is scored — they only supply numbers to the Visualizer and the Checklist, which are downstream, advisory tools, not additional layers in the verdict chain.

### 11.6 DCA Model Level Visualizer

`POST /api/market/dca-levels` previews how a saved DCA Model (§10.3) would lay out its ladder if started right now, without creating or modifying any strategy.

Given a `model_id`, a `side` (`long` or `short`), and a multiplier (0.5×–3.0×, default 1.5×), the route:
1. Fetches the live price for the symbol and a fresh ATR% from the same Layer 3 ATR helper (§11.4, computed over 15 candles).
2. For each of the model's configured order sizes (`bull_order_sizes` for long, `bear_order_sizes` for short), computes a step distance scaled by ATR% and the multiplier:
   ```
   step_pct[i]   = (i + 1) × atr_pct × multiplier
   step_price[i] = current_price × (1 − step_pct[i] / 100)   (long: price falls)
   step_price[i] = current_price × (1 + step_pct[i] / 100)   (short: price rises)
   ```
3. Flags any step landing **past** a user-entered liquidation cluster (§11.3) as a warning, or **just short of** one (within 0.5%) as a good target, and computes a suggested alternate multiplier that would place the nearest step just outside the cluster. An **"Apply _N_× multiplier"** button on the warning sets the slider directly to that suggested value and recalculates.
4. Computes the resulting average entry price and take-profit price/distance if every step fills, using the model's `bull_take_profit_percent` / `bear_take_profit_percent`, and renders each step as a row in a visual ladder (price, % distance, cumulative size).
5. Flags any step within 0.5% of the current price with an "⚡ Step _N_ approaching" note, so a step about to trigger doesn't go unnoticed between refreshes.

While a model is selected, the visualizer silently recalculates every 60 seconds in the background (no loading state shown) so the ladder and the approaching-step alert stay current with the live price without the user needing to click "Calculate" again.

The visualizer is read-only — it answers "where would this model's ladder sit today, and does it clash with a known liquidation cluster", not "start this strategy".

### 11.7 AI Analysis Panel

`POST /api/ai/analysis` sends the current Master Summary verdict, all three layers' live data (including the manual Layer 1 cards and the Layer 2 liquidation cluster inputs, if filled in), the Pre-Trade Checklist's current TP plan if one exists for the selected coin (§11.10), and — if open — the DCA Visualizer's current ladder, to Claude (model `claude-sonnet-4-6`, `max_tokens=1300`) and asks for a structured trading read.

The panel offers two response styles, chosen with a **Professional / Plain English** toggle (`style: "professional" | "plain"` in the request body, default `professional`) — both read the same underlying data, just narrated differently.

**Professional** fixes the response into four sections, with these headers verbatim:
- **`## WHAT THE MARKET IS DOING`** — 2–3 sentences combining all three layers, specific numbers only
- **`## THE KEY TENSION`** — what's agreeing vs. conflicting between layers, and why it matters
- **`## PROFESSIONAL ASSESSMENT`** — a conviction call (high/medium/low) referencing the 2–3 most important signals
- **`## SUGGESTION`** — one of LONG / SHORT / WAIT, with an entry approach, ATR-based step spacing, an explicit exit/stop signal to watch, and (when a DCA Model ladder or liquidation clusters were provided) specific commentary on step placement relative to those clusters

**Plain English** asks for the same four-part structure in jargon-free language, headers **`## WHAT'S HAPPENING RIGHT NOW`**, **`## WHAT'S PULLING IN DIFFERENT DIRECTIONS`**, **`## WHAT AN EXPERIENCED TRADER WOULD THINK`**, **`## IS YOUR PLAN GOOD?`** (BUY / SELL / WAIT) — same content requirements as the Professional sections, but any trading term is explained inline rather than assumed.

When a Pre-Trade Checklist TP plan is present, a fifth section is appended: Professional gets **`## PLAN VALIDATION`** (compares the trader's Confirmed TP against the market-offered Target TP from Scenario 1, judges whether a higher target is momentum-justified or just greed and whether a lower one is appropriately conservative or leaves profit on the table, and checks the TP against the liquidation clusters). Plain English instead folds the same comparison into its existing **`## IS YOUR PLAN GOOD?`** section via a fish-market analogy — Minimum/Target/Stretch TP as three sizes of fish, and the number of green checks from Scenario 2 deciding whether "the fish the trader wants is available at this market today." When DCA Model ladder data is present, both styles get an addendum asking for commentary on step placement relative to ATR and any liquidation clusters.

Every response is required to end with the disclaimer: *"⚠️ This is analytical context to support your own decision — not financial advice. You make the final call."* This is a single on-demand call per click (not part of the auto-refresh cycle) — there is no caching, scheduled polling, or alerting on top of it.

> ⚠️ Planned — not yet implemented in code.
> An earlier design called for two simultaneous AI calls on every click — one Professional and one Plain English response generated together, so both were always available without re-querying. The current implementation makes a single call per click and branches on the `style` field instead; switching styles triggers a fresh call rather than revealing an already-generated second response. Designed but awaiting build.

### 11.8 Refresh Cadence & Caching

| Layer | TTL | Scope | Rationale |
|-------|-----|-------|-----------|
| Layer 1 | 900s (15 min) | Global — one cache entry for all coins | Macro indicators (Fed rate, CPI, DXY, etc.) don't move meaningfully within a 15-minute window, and several depend on rate-limited third-party keys (Twelve Data, FRED) |
| Layer 2 | 300s (5 min) | Per-symbol | Funding/OI/long-short data updates on Binance's own hourly/8-hourly cadence; 5 minutes keeps the dashboard responsive without hammering the futures API on every tab switch |
| Layer 3 | 120s (2 min) | Per-symbol | Entry timing is meant to be the most reactive layer — order book and recent-candle volume can shift materially within a couple of minutes |

The DCA Model Level Visualizer and AI Analysis Panel are **not cached** — both are explicit, on-demand actions (clicking "Calculate" / "Analyze") rather than part of the passive auto-refresh cycle, and both need the live price/ATR at the moment the user asks.

### 11.9 Retirement of the Original Signal System

Earlier versions of this whitepaper described a different, single-score Market Signals system: a 4-signal regime score (price vs. EMA50/200, RSI, weekly return) over BTC/ETH/XRP/SOL/ZEC/SUI/XAU/NVDA, three on-the-fly scikit-learn models (`core/ml_signals.py`), a SQLite Signal History database (`core/signal_history.py`), Fibonacci retracement levels (`core/fibonacci.py`), and automatic Telegram alerts on regime-transition (`core/telegram_notifier.py`) backed by `core/market_data.py` for OHLC fetching. That system has been **fully superseded** by the Layer 1/2/3 architecture in this section, and its asset coverage narrowed from BTC/ETH/XRP/SOL/ZEC/SUI/XAU/NVDA to **BTC/ETH/SOL/ZEC/XAUT** — XRP, SUI, and NVDA are no longer covered, and gold is now tracked directly as XAUT (Tether Gold, `XAUTUSDT` on Binance spot) instead of via a Twelve Data XAU/USD feed.

As of this writing, the modules behind the old system are dead or orphaned from the live dashboard's perspective:
- **`core/ml_signals.py`** — no longer imported by `web/app.py`. It survives only as an optional `--ml-filter` overlay inside `signal_lab/harness.py` (`signal_lab` is an offline research tool, not part of the live dashboard).
- **`core/signal_history.py`**, **`core/fibonacci.py`**, **`core/market_data.py`** — fully orphaned; no remaining file in the codebase references any of them.
- **`core/telegram_notifier.py`** — still imported, but only by the standalone `POST /api/telegram/test` route, which has no corresponding control in the dashboard UI. The automatic alert-on-transition behaviour described in earlier drafts of this document no longer exists, because the function that used to trigger it (a single combined signal computation) no longer exists in this form.

None of this affects the **Weekly Regime tab** (`core/regime_detector.py`, §10), which has always been a separate feature with its own independent Fear & Greed and BTC Dominance fetches on the daily timeframe — it is untouched by the Layer 1/2/3 rewrite described in this section.

Also unaffected: **`core/regime_live.py`** (§8.6) is a different module from anything named in this section, and is not part of the "old signal system" being retired. It still actively feeds the live `MixedEngine` for any running `mode="mixed"` strategy, unchanged by this rewrite. It is not imported by `web/app.py` and never fed the Market Signals dashboard — it only ever fed the live trading engine, and continues to.

### 11.10 Pre-Trade Checklist

Sitting between the Layer 1/2/3 cards and the DCA Model Level Visualizer (§11.6), the Pre-Trade Checklist is a five-item, entirely client-side worksheet for walking through a trade setup before any order is placed. It resets whenever the selected coin changes — entry, stop, size, and TP are inherently tied to one specific setup — and is purely advisory: it never calls `/api/start_engine` or otherwise touches a strategy.

| Item | What it checks |
|------|-----------------|
| **1. Signal Alignment** | Auto-derived from the Master Summary Bar (§11.5) — green only when the verdict contains "ALIGNED" |
| **2. Entry Price** | A manual price, or "Use live price" to pull the current Layer 3 price |
| **3. Take Profit** | Two analysis modes — see below |
| **4. Stop Loss** | A manual price below entry; shows the derived % distance |
| **5. Position Size** | A manual USDT amount, used only for the dollar figures in the R:R summary |

A live **Risk:Reward summary** (`RISK` / `REWARD` / `R:R`) recomputes from Entry, Stop, Confirmed TP, and Position Size on every change.

**Take Profit, Scenario 1 — "What the market is offering."** Three levels are derived from the entry price and the live Layer 3 ATR% (§11.4), recalculating in real time as either changes:

```
Minimum = entry × (1 + ATR% / 100)              "1× ATR — normal candle range"
Target  = nearest_cluster_above × 0.995         "Nearest cluster above - 0.5%"   (if a Layer 2 cluster-above value is entered)
        | entry × (1 + ATR% × 2.5 / 100)         "2.5× ATR — estimated target"     (otherwise)
Stretch = second_cluster_above × 0.995          "Second cluster above - 0.5%"    (if a second cluster price is entered)
        | entry × (1 + ATR% × 4 / 100)           "4× ATR — strong trend target"    (otherwise)
```

Target is auto-selected into Confirmed TP the first time Entry Price is filled in, unless the user has already typed something into Confirmed TP themselves. Each level has a "Use this" button and a one-line tooltip explaining it in plain terms (a small fish / the most likely fish / the biggest fish, mirroring the AI panel's Plain English analogy in §11.7).

**Take Profit, Scenario 2 — "Is your target achievable?"** The user types a desired TP%, and three checks run against it: whether it sits within the nearest liquidation cluster's distance from entry, how many multiples of ATR% it represents (≤2× green / ≤4× yellow "possible but needs a strong move" / >4× red "very unlikely without a major catalyst"), and whether the current Layer 3 verdict (§11.4) supports that direction (`LONG`/`WEAK_LONG` green, `NEUTRAL` yellow, `SHORT`/`WEAK_SHORT` red). The number of green checks drives an overall verdict — 3 green: achievable; 2: possible but not ideal; 1: a stretch (suggests using the Scenario 1 Target instead); 0: unlikely (suggests accepting the Scenario 1 Target% instead). A "Use this as my TP" button sets Confirmed TP from the typed %.

**Agreement indicator.** Once both scenarios have a value, a banner compares the user's desired % against the Scenario 1 Target%: **ALIGNED** (within 0.5%), **CONSERVATIVE** (user's target is lower), **AMBITIOUS BUT SUPPORTED** (higher, but ≥2 of Scenario 2's checks are green), or **TARGET LIKELY TOO HIGH** (higher, with <2 checks green).

**Server-side hand-off.** The checklist's full state is debounced (600ms) and posted to `POST /api/checklist/tp-plan` as `{coin, plan}`, which the server caches in memory keyed by coin (`_tp_plan_cache` — not persisted to disk, cleared on app restart). Two things consume that cache:
- The **AI Analysis Panel** (§11.7) includes the cached plan in its prompt whenever one exists for the selected coin.
- **`_track_testnet_journal()`** (§10.6) pops the cached plan for a coin the moment that coin's testnet trade closes, and attaches a `tp_analysis` object to the journal entry comparing the plan's Minimum/Target/Stretch levels and Confirmed TP against the trade's actual exit (`reached_minimum`/`reached_target`/`reached_stretch` booleans, plus a `tp_accuracy` ratio of actual % gain to confirmed-target %). Once 10 or more journal entries carry this data, `GET /api/testnet/journal`'s `tp_accuracy` field — and the Strategies tab's Learning Journal UI — additionally surface a summary: % of trades that reached each level, average actual vs. average target %, and the best-performing level.

Because the cache is per-coin, in-memory, and only ever populated by an open checklist, a trade that closes without a synced plan for its coin simply gets no `tp_analysis` attached — both consumers degrade to their pre-existing behaviour with no plan present.

---

## 11A. Decision Pipeline — From Signal to Journaled Trade

None of the individual pieces below are new — each is documented in its own section above. This section is purely about the order a user actually moves through them, end to end, from looking at the Market Signals tab to a closed, journaled trade.

1. **Check the Master Summary Bar** (§11.5) for a composite read, then open the individual Layer 1/2/3 cards (§11.2–§11.4) behind it to see what's actually driving that read.
2. **Set or confirm the reference price** for the strategy you intend to run (§4.1) — optionally using the flush-and-reclaim technique (§4.1.1), anchored to a Layer 2 liquidation cluster (§11.3) instead of an eyeballed top.
3. **Fill in the Pre-Trade Checklist** (§11.10) for the coin — entry, take profit (Scenario 1 and/or Scenario 2), stop loss, and position size. The checklist is independent of everything else on the tab; nothing in the dashboard requires it to be filled in before any other action becomes available.
4. **Optionally preview the DCA Model ladder** (§11.6) for the model you intend to use, checking step placement against ATR and any liquidation cluster.
5. **Optionally request the AI Analysis Panel's read** (§11.7). The "Generate" button is not gated on checklist completeness — it can be clicked at any time, with or without a filled-in checklist; if a checklist plan exists for the coin, it's included in the prompt automatically, and if not, the panel just runs without one.
6. **Go to the Strategies tab** and create or edit the strategy with the chosen reference price and DCA model, optionally as a testnet rehearsal via "Copy to Testnet" (§10.6).
7. **Start the engine.** From this point the checklist, the AI panel, and the rest of the Market Signals tab have no further involvement — the running strategy is driven entirely by `core/dca_engine.py` / `core/mixed_engine.py`'s own polling loop (§6, §8.6), independently of whatever the dashboard happens to be showing.
8. **The engine polls, executes DCA steps, and eventually hits take profit** (§4.5, §6) according to its own configured levels — not the checklist's Confirmed TP, which is advisory only and never wired into the engine's exit logic.
9. **If the strategy was testnet** (`is_testnet=true`), the trade's close is detected by `web/app.py`'s polling and appended to the Testnet Learning Journal (§10.6), including a `tp_analysis` comparison against the checklist's plan if one was synced for that coin before the close (§11.10).
10. **The Strategies tab's Learning Journal table** shows the result, and once 10 or more entries exist, the TP-accuracy summary (§10.6, §11.10) starts aggregating across trades.

> ⚠️ Planned — not yet implemented in code.
> The checklist's first item could instead be a manual LONG/SHORT direction toggle, rather than (or in addition to) the auto-derived Signal Alignment check it is today (§11.10, Item 1). Designed but awaiting build.

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
      "timestamp": "2026-06-04 21:00:00 UTC",
      "side": "LONG"
    }
  ]
}
```

State survives process restarts and VPS reboots. The engine resumes exactly where it left off: same reference price, same executed steps, same average entry, waiting for the next level or TP trigger.

### 12.1 Mixed Engine State Fields

For strategies running `mode="mixed"`, `PositionState` carries additional fields written and read by `core/mixed_engine.py`:

```json
{
  "strategy_id": "btc-aggressive",
  "coin": "BTC",
  "symbol": "BTCUSDT",
  "status": "ACTIVE",
  "average_entry": 82500.0,
  "total_invested": 6250.0,
  "total_quantity": 0.07142,
  "executed_steps": [ { "...": "...", "side": "SHORT" } ],

  "direction": "SHORT",
  "active_mode": "SELL",
  "regime": "BEARISH",
  "anchor_price": 91200.0,
  "leverage": 3,
  "liquidation_price": 121850.0
}
```

| Field | Meaning |
|-------|---------|
| `direction` | Currently open broker position: `NONE`, `LONG`, or `SHORT` |
| `active_mode` | Regime-driven target direction: `WAIT`, `BUY`, or `SELL` (§8.6.1) |
| `regime` | Last confirmed regime: `BULLISH`, `BEARISH`, or `NEUTRAL` (§8.4.3) |
| `anchor_price` | Current ladder anchor — either the EMA21 entry-indicator signal price or a rolling extreme, depending on `use_entry_indicator` (§8.6.1) |
| `leverage` | Leverage currently applied on the exchange for this position |
| `liquidation_price` | Exchange-reported liquidation price for the open position, refreshed after every fill (`null` when no position is open) |

---

## 13. Exchange Integration

- **Exchange:** Binance (spot markets only)
- **Order types:** Market buy, market sell
- **Supported:** Testnet mode for safe testing — at the account level (`config/accounts.json`'s `testnet` flag) and, dashboard-wide, via the LIVE/TESTNET toggle described in §10.6
- **Safety:** `binance_client.py` verifies fills, handles lot size precision (step size), and prevents zero-quantity orders

### 13.1 Data Sources & Timeframes

Different parts of the system pull different kinds of Binance data, on different timeframes, chosen to match what each component needs to decide.

| Data | Source | Timeframe | Used by | Frequency |
|------|--------|-----------|---------|-----------|
| Ticker price | `GET /api/v3/ticker/price` | — (instantaneous) | Live dashboard price display, `DCAEngine.tick()` dump%/TP checks | Every 5s (dashboard poller), every 10s (engine tick) |
| Global macro APIs (alternative.me, CoinGecko, Twelve Data, FRED) | Various, see §11.2 | 1d (daily series) | Market Signals Layer 1 — Macro Environment (§11.2), coin-agnostic | Recomputed every 15 min (`_LAYER1_TTL`), shared across all coins |
| Binance Futures public data | `GET /fapi/v1/fundingRate`, `/futures/data/openInterestHist`, `/futures/data/*LongShortAccountRatio` (1h bars) | 1h | Market Signals Layer 2 — Market Positioning (§11.3), per selected coin | Recomputed every 5 min (`_LAYER2_TTL`) per symbol |
| 4h klines (21 candles ≈ 3.5 days) + order book (top 20 levels) | `GET /api/v3/klines`, `GET /api/v3/depth` | 4h | Market Signals Layer 3 — Entry Timing (§11.4), per selected coin | Recomputed every 2 min (`_LAYER3_TTL`) per symbol |
| Daily klines (200 candles ≈ 6.5 months) | `GET /api/v3/klines` | 1d | Weekly Regime tab (`core/regime_detector.py`) — EMA50/200, ATR, Bollinger Bands, volume trend, higher-highs | On-demand when the tab is opened |
| 1h historical OHLCV (CSV, ~61,000 candles, 2018–2025) | Pre-downloaded dataset | 1h | Backtesting & strategy optimization (Sections 8–9) | Static — loaded once per backtest run |

**Why these timeframes:**

- **Ticker price (no aggregation):** trading decisions for a multi-day DCA strategy don't need sub-second data, but polling too slowly risks missing a brief wick through a trigger level. 5–10s balances responsiveness against Binance's public rate limits.
- **Layer 3's short 21-candle window:** unlike a regime/trend read, entry timing (§11.4) only needs to know what happened in the last few days — volume divergence, recent structure, and ATR are all short-lookback by design. 21 four-hour candles (≈3.5 days) is enough for a 14-period ATR and a 10-candle structure read without dragging in price action from weeks ago that's no longer relevant to "should I enter in the next few hours."
- **Layer 1's 15-minute global cache:** macro indicators (Fed funds, CPI, DXY, VIX) don't move meaningfully within a 15-minute window and several depend on rate-limited third-party keys (Twelve Data, FRED) — caching once for all coins avoids redundant calls every time the user switches the selected coin.
- **Daily klines:** the Weekly Regime tab answers a different question — "what's the macro cycle right now" rather than "should I enter a level this week". Daily candles over ~6 months are the standard window for that read, and pairing them with Fear & Greed Index and BTC dominance (both meaningless on a 4h chart) adds macro context.
- **1h historical data:** the optimizer's best configurations use tight DCA levels (e.g. -5%/-9%/-13%, 3% TP) that only show up at 1h granularity. ~61,000 1h candles over 7 years is large enough for statistically meaningful backtests while staying practical to grid-search — daily data would miss the tight-level fills, and minute data would be ~60× larger for no added benefit at this strategy's scale.

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
| `step_count` | int | Number of DCA buy (long) levels. May be `0` — a strategy can be created with no ladder steps at all, as long as `take_profit_percent > 0` |
| `dump_levels` | float[] | Price drop % to trigger each long step (must be negative). Empty list if `step_count` is `0` |
| `order_sizes` | float[] | USDT amount to buy at each long step. Empty list if `step_count` is `0` |
| `take_profit_percent` | float | Target return % to trigger sell on the long side. Must be > 0 |
| `reference_price` | float? | Starting reference price (null = must be set manually) |
| `is_testnet` | bool | `false` = live strategy, `true` = paper-trading strategy shown only in the dashboard's TESTNET mode (§10.6) |

### 14.1 Mixed Mode Fields (`mode="mixed"`)

The following additional fields apply when a strategy's `mode` is `"mixed"` (§8.6) — typically set via the DCA Models "Apply" flow (§10.3) rather than hand-edited:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | `"long"` | `"long"` (Spot DCA, `core/dca_engine.py`) or `"mixed"` (auto long/short Futures, `core/mixed_engine.py`) |
| `bear_levels` | float[] | `[]` | Price pump % to trigger each short step (must be positive). Required, non-empty for `mode="mixed"` |
| `bear_order_sizes` | float[] | `[]` | USDT margin amount to sell-short at each bear step; must match `bear_levels` length |
| `bear_take_profit_percent` | float | `0.0` | Target return % to close the short side. Must be > 0 for `mode="mixed"` |
| `bull_stop_loss_percent` | float | `0.0` | Optional stop-loss % for the long side, measured from the anchor (`0` = disabled) |
| `bear_stop_loss_percent` | float | `0.0` | Optional stop-loss % for the short side, measured from the anchor (`0` = disabled) |
| `leverage` | int | `1` | Futures leverage applied to every order (≥1) |
| `use_entry_indicator` | bool | `true` | Gate entries behind the three-factor confirmation signal (§8.4.5) |
| `rsi_overbought` | float | `70.0` | RSI threshold for a BULLISH→SELL extremity override (§8.4.4) |
| `rsi_oversold` | float | `30.0` | RSI threshold for a BEARISH→BUY extremity override (§8.4.4) |
| `regime_interval` | string | `"1h"` | Candle interval used for live regime detection (`core/regime_live.py`) |
| `atr_based_spacing` | bool | `false` | When `true`, `dump_levels`/`bear_levels`/TP/SL values are interpreted as ATR multiples (§8.6.4) |
| `atr_period` | int | `14` | Candles (in `regime_interval` units) used for the live ATR calculation; must be ≥ 2 |

### 14.2 DCA Model Templates — `config/dca_models.json`

Reusable Mixed-mode templates are stored in `config/dca_models.json` as `{"models": [...]}`, where each entry is a `DCAModel` (`models/dca_config.py`). The schema mirrors §14.1's mixed-mode fields, but without a coin/symbol/id binding — a model is a *template*, not a running strategy:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique model identifier |
| `name` | string | Display name |
| `bull_levels` / `bull_order_sizes` | float[] | Long ladder levels (negative %) and USDT sizes |
| `bull_take_profit_percent` | float | Take-profit % for the long side. Must be > 0 |
| `bear_levels` / `bear_order_sizes` | float[] | Short ladder levels (positive %) and USDT sizes |
| `bear_take_profit_percent` | float | Take-profit % for the short side. Must be > 0 |
| `leverage` | int | Futures leverage (≥1), default `1` |
| `bull_stop_loss_percent` / `bear_stop_loss_percent` | float | Optional per-side stop-loss %, default `0.0` |
| `use_entry_indicator` | bool | Default `true` |
| `rsi_overbought` / `rsi_oversold` | float | Defaults `70.0` / `30.0` |
| `regime_interval` | string | Default `"1h"` |
| `atr_based_spacing` | bool | Default `false` |
| `atr_period` | int | Default `14`, must be ≥ 2 |

Applying a model (§10.3) copies these fields onto a target strategy's `config/coins.json` entry and sets `mode="mixed"`. The model itself is not referenced again afterward — there is no `model_id` stored on the strategy.

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

- **Email/Telegram alerts on Market Signals transitions** — `core/telegram_notifier.py` exists and is reachable via a test endpoint (§11.9), but nothing currently triggers it automatically off Layer 1/2/3 state; re-wiring an alert (e.g. on Master Summary Bar transitions into ALIGNED LONG/SHORT) is a possible follow-up, not something already live
- **Multiple exchange support** (OKX, Bybit)
- **Dynamic position sizing** based on portfolio value
- **Trailing take profit** to capture extended uptrends
- **Risk controls** — max drawdown limits, daily loss caps, exposure limits, circuit breakers (see §19.6 — this is treated as a near-term priority, not a nice-to-have)
- **Multi-account portfolio view** — aggregate P&L across all accounts
- **Strategy optimizer** — auto-tune DCA levels based on backtest results
- **Direction-aware strategy matching** — restrict saved strategies suggested for Short DCA to those whose levels were originally designed for short entries, rather than mirroring long-only level sets
- **Historical charting for Market Signals** — the retired Signal History database (§11.9) used to persist every signal computation for charting regime/RSI/entry-score over time; an equivalent time-series view (e.g. Layer verdict history per coin) does not currently exist for the Layer 1/2/3 system and would need to be rebuilt from scratch if wanted
- **DCA Model live link** — store a `model_id` back-reference on strategies created via "Apply" (§10.3), so a later edit to the model can optionally be re-propagated to every strategy it was applied to, instead of requiring a manual re-apply

---

## 19. Limitations, Assumptions & Honest Risk Disclosure

This section exists to counter-balance the rest of the document. Sections 1–18 describe how the system is *designed* to work; this section is about where that design is still incomplete, where the backtests are optimistic, and what a user should not assume.

### 19.1 Backtest Realism

The backtest engines (§8) execute fills against candle **high/low wicks** with a fixed, deterministic same-candle ordering (DCA fills → stop-loss → take-profit). This is a reasonable, documented convention — but it is still a simplification of live execution:

- **No fees, slippage, or spread are modeled anywhere in the codebase.** For strategies with a 3% take-profit (the most common in §9.1), a realistic ~0.1–0.2% round-trip cost on Binance spot is a meaningful fraction of the edge. All ROI figures in §9 should be read as **gross, pre-cost** returns.
- **A candle that touches both a DCA level and the TP price is resolved by the documented rule order, not by reconstructing the true intra-candle path.** In practice, price could have moved through the TP *before* reaching the new DCA level (or vice-versa), which the wick-based model cannot distinguish.
- **Market orders do not fill exactly at the trigger price** in live trading — `core/dca_engine.py` uses the real exchange fill price (`fill_price`/`fill_qty` from the order response), so live P&L already reflects actual fills; the *backtest* assumes a perfect fill at the trigger price, which live trading will not always match exactly.
- Partial fills, rejected orders, and API errors are handled defensively in `_execute_buy` (skip + log), but a skipped step in live trading means the position diverges from what the backtest would show for the same price path.

**Takeaway:** treat the ROI figures in §9 as an upper bound on what a frictionless version of the strategy could achieve, not a forecast of live results. A walk-forward or out-of-sample re-run, and a fee/slippage-adjusted re-run, are the natural next steps before sizing real capital off these numbers.

### 19.2 "No Liquidation Risk" Is Not the Same as "Low Risk"

§1 and §2 correctly state that the default Spot DCA strategy type cannot be liquidated and cannot lose more than its allocated capital. That is true, but it is a narrow guarantee. Spot DCA on a single asset can still:

- Suffer a **70–95% drawdown** in a sustained bear market, with the position held the entire time (the live engine has no max-drawdown exit — see §19.6)
- **Lock capital for months or years** waiting for a recovery that may not come within the user's planning horizon
- Be exposed to **exchange risk** (Binance outages, withdrawal halts, account restrictions) and **asset-specific risk** (delisting, project failure, a coin that never recovers to its prior range)
- Carry **opportunity cost** — capital stuck in a dead position is capital that cannot be deployed to a better setup

"No liquidation" means the position cannot be force-closed at a loss by an exchange. It does not mean the position is guaranteed to be profitable, liquid, or even sellable at a reasonable price within any particular timeframe.

> **Mixed-mode strategies (`mode="mixed"`) are the exception to all of the above.** Any strategy switched to Mixed via a DCA Model (§10.3) trades on Binance USD-M Futures with `leverage` ≥ 1× and **can be liquidated** — a sufficiently large adverse move before the configured ladder/stop-loss reacts can result in losing the full margin for that position, independent of `bull_stop_loss_percent`/`bear_stop_loss_percent` (which are checked only once per `POLL_INTERVAL`, not continuously by the exchange). `liquidation_price` (§12.1) is reported by Binance and shown on the dashboard, but the engine does not actively manage distance-to-liquidation — higher leverage directly reduces the price move needed to reach it. The Survivability guarantee in §1/§2 applies only to `mode="long"` Spot DCA strategies.

### 19.3 The Reference Price Is a Manual, Judgment-Based Input

§4.1 describes setting the reference price "when you believe the asset is near a local top." That is a discretionary, market-timing judgment call — the system does not pick it for you, and the quality of that single input materially affects outcomes:

- If the reference is set **too low** (not actually near a local top), DCA levels may never trigger, or trigger at prices that aren't meaningfully discounted.
- If the reference is set **too high** (well above where price ever returns), the position may never reach the deeper DCA levels needed to pull the average entry down enough for TP.

The system's mechanical, rule-based execution starts *after* this one discretionary decision. Users should not read "mechanical execution" (§2) as "the system removes all judgment" — it removes judgment from *execution*, not from *anchor selection*. The Market Signals tab's three layers (§11) and the DCA Model Level Visualizer's ATR-anchored ladder preview (§11.6) are aids for making this single decision more informed, not a replacement for it.

§4.1.1 describes a flush-and-reclaim technique that partially mitigates the weakness described above: instead of picking a reference price from a subjective read of the chart, the user can anchor it to a specific liquidation cluster flushing and then being reclaimed on a 4H close. This does not remove the discretionary element — the user still has to correctly identify the cluster and correctly judge whether a candle close counts as a genuine reclaim — but it gives that discretion a concrete, observable event to point to instead of an unfalsifiable feeling about where "the top" is. The method replaces "I think this is near the top" with "I observed buyers step in after the cluster swept" — a more defensible rationale, but still requiring accurate real-time observation and correct cluster identification.

### 19.4 ML Models Are Retired From the Live Dashboard, Live Only in Offline Research

Earlier versions of this whitepaper described a live "ML Analysis" panel — three scikit-learn models retrained on the fly from 200 four-hour candles on every Market Signals computation. That panel no longer exists; `core/ml_signals.py` is not imported anywhere in `web/app.py` (§11.9). Of the three models that used to back it, only the first — `predict_direction()`, a RandomForestClassifier predicting next-candle direction — is still reachable at all, as `signal_lab/harness.py`'s optional `--ml-filter` flag. The methodological caveats below are scoped to that one model, in its offline-research role, not to anything shown on the live dashboard:

- `predict_direction()` retrains a brand-new `RandomForestClassifier` from scratch on every call — there is no persisted/cached model. In a Signal Scan, `--ml-filter` calls it once per trade the rule-based signal opens, each time fitting fresh on whatever candle history is available up to that point and labelling each historical candle by whether the *next* one closed higher or lower.
- The reported `confidence` is that freshly-retrained model's own same-run predicted probability — there is no separate held-out test set tracked across the whole scan, and no walk-forward accuracy metric reported anywhere in the harness output.
- `--ml-filter`'s report is an **agreement** breakdown — whether the model's UP/DOWN call lines up with the direction the rule-based signal already traded — not an independent backtest of the model. A high agreement rate says the two methods often concur, not that either one is more accurate than chance.
- Retraining from scratch on a small, overlapping window at every trade means the model's output can be noisy/unstable from one trade to the next, even on the same underlying data.

**Takeaway:** if `--ml-filter` is used in a Signal Scan, treat its agreement breakdown as a descriptive cross-check against the rule-based verdict — not as a validated predictor, and not as something the live dashboard's Layer 1/2/3 verdicts (§11.2–§11.4) rely on in any way.

### 19.5 Security & Deployment Hardening

§3 and §16 describe the current deployment honestly, but a few aspects are worth calling out explicitly for a system that holds exchange API keys and can place real orders:

- **Auto-deploy on every push to `master`** (cron pulls, reinstalls dependencies, and restarts both services every 60 seconds) means a bad commit can affect a live trading bot within a minute, with no staging environment or manual approval gate in between. A review/staging step before deploys reach the live-trading service is recommended.
- **The web dashboard runs on the Flask development server** (`web/app.py`, port 5050). This is explicitly not recommended by Flask for production use (no production-grade concurrency, error handling, or hardening) — a WSGI server (gunicorn/waitress) behind a reverse proxy is recommended if the dashboard is exposed beyond a trusted local/VPN network.
- **Binance API keys should be created with withdrawals disabled** (trading + read-only permissions only), so that even if a key is compromised, funds cannot be moved off the exchange. The codebase does not enforce or verify this — it is an operational requirement for whoever provisions the keys.
- There is currently **no encryption-at-rest for stored API credentials**, **no role-based access control** on the dashboard, and **no audit log** of manual dashboard actions (set reference price, reset position, start/stop engine). For a single-operator setup this is a lower priority than for any multi-user deployment.

### 19.6 Risk Controls: Treat as a Pre-Scaling Requirement, Not a "Someday"

§18 lists max-drawdown limits, daily loss caps, exposure limits, and circuit breakers as planned extensions. Until they exist, the live engine's only exit condition is take-profit (§4.5) — there is **no mechanism that closes a position early in a sustained adverse move**. In its current form, the strategy's downside is bounded only by "the capital allocated to it can go to zero in USDT terms while the position is held" (§19.2), not by any active risk management.

Practically, this means:
- Capital allocated to any single strategy should be sized as if it could be fully drawn down and illiquid for an extended period — not as "capital protected by the system's risk controls," because those controls do not yet exist.
- The extensions in §18 — particularly max-drawdown limits and daily loss caps — should be implemented and tested *before* allocating capital beyond what the operator is fully prepared to hold through a worst-case drawdown.

### 19.7 ATR-Based Spacing Is Recomputed Every Tick, Not Locked at Anchor Time

When `atr_based_spacing=True` (§8.6.4), `_effective_pct()` multiplies each configured level/TP/SL by the **current** `atr_pct` on every tick — not the `atr_pct` that was in effect when the ladder was anchored or when a step was filled. This has two practical consequences:

- **Trigger distances drift while a ladder is armed.** If volatility rises after the anchor is set but before the next level fills, that level's effective trigger distance widens in real time; if volatility falls, it narrows. The dashboard reflects whatever ATR% was last computed, which can differ from the ATR% in effect at the moment a level actually triggers.
- **Cold-start fallback is a fixed percentage, not "no spacing."** Until `core/regime_live.py` has enough candles to compute ATR (`atr_period + 1`), `_effective_pct()` falls back to the configured `base_value` unchanged — so a level configured as `-6` (intended as "−6× ATR%") is briefly treated as a literal **−6%** level until ATR becomes available. For small `atr_period` values this window is short, but it is not zero.

Neither behavior is a bug — both follow directly from "spacing tracks live volatility" — but a user comparing a dashboard's displayed trigger price against the price that ultimately fills should expect small differences proportional to how much ATR% moved in between.

---

*Infinity — built for systematic, emotion-free DCA trading on spot markets. Section 19 is part of this document — read it before allocating capital.*
