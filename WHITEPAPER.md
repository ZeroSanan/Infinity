# Infinity — Market Analysis Dashboard
## Technical White Paper

---

## 1. Overview

**Infinity** is a cryptocurrency market analysis dashboard. It aggregates live data from Binance public APIs, macro economic feeds, and AI-powered narrative generation to produce structured, three-layer signal reads for five assets: **BTC, ETH, SOL, ZEC, and XAUT** (Tether Gold).

The system does **not** execute trades, manage positions, or connect to exchange accounts. Its purpose is to help a trader answer three questions before placing any order:

1. **Is the macro backdrop favourable for risk assets right now?** (Layer 1)
2. **Is this specific coin's futures market crowded, and on which side?** (Layer 2)
3. **Does the immediate price action favour entering long or short right now?** (Layer 3)

A **Pre-Trade Checklist** then translates those reads into a concrete trade plan — entry, take profit (with two analytical scenarios), stop loss, and position size — and an **AI Analysis Panel** generates an on-demand narrative combining all three layers, the checklist plan, and any entered liquidation-cluster data.

**Deployment:** Runs as a single Flask web application (`web/app.py`) on a VPS, served on port 5050 with a two-tab UI: **Market Signals** and **Weekly Regime**.

---

## 2. System Philosophy

| Principle | Meaning |
|-----------|---------|
| **Signal before execution** | The dashboard is a decision-support tool, not an execution tool. No order is placed from within the system — the analysis informs a decision the user makes elsewhere. |
| **Three independent layers** | Macro environment, market positioning, and entry timing each answer a different question from different data sources. None gates another; they are read together, not in sequence. |
| **Quantitative where possible, manual where not** | Indicators that can be computed from live APIs are computed automatically. Indicators that have no live source (CME FedWatch, BTC Rainbow Chart, jobs report, Global M2) are manual-entry cards with staleness warnings. |
| **Transparency over confidence** | The dashboard does not hide uncertainty or aggregate signals into a single "buy/sell" number. Every layer shows the individual signals that drove its verdict. The Pre-Trade Checklist shows how many checks are green, not just a go/no-go. |
| **Advisory only** | Nothing in the system — not the Master Summary Bar, not the AI Analysis Panel — constitutes financial advice. Every AI response ends with an explicit disclaimer to that effect. |

---

## 3. Architecture

```
Infinity/
├── core/
│   ├── __init__.py
│   ├── regime_detector.py   # Weekly Regime tab — EMA50/200, ATR, Bollinger Bands, volume trend, AI narrative
│   └── signal_recorder.py   # Signal History — snapshots Layer 1/2/3 verdicts to data/signal_history.json
├── data/
│   └── signal_history.json  # Persisted signal snapshots (written by SignalRecorder)
├── deploy/
│   └── setup-vps.sh         # One-time VPS setup script (clones repo, creates venv, installs systemd service)
├── web/
│   ├── app.py               # Flask application — all API routes, background scheduler, regime detector
│   └── templates/
│       └── index.html       # Dashboard UI — Market Signals + Weekly Regime tabs
├── .env.example             # Environment variable template (API keys)
└── requirements.txt         # Python dependencies
```

**Surviving core modules:**

| Module | Role |
|--------|------|
| `core/regime_detector.py` | Fetches daily klines + Fear & Greed + BTC Dominance; scores weekly regime; generates an AI narrative via Claude |
| `core/signal_recorder.py` | Snapshots the current Layer 1/2/3 verdicts at a configured interval; persists to `data/signal_history.json` for the Signal History table |

**Deployment:** One `systemd` service:
- `infinity-web.service` — the Flask dashboard (`web/app.py`, port 5050)

There is no live trading engine, no separate bot process, and no `main.py`.

---

## 4. Web Dashboard

The dashboard is a single-page Flask application with two tabs:

| Tab | Purpose |
|-----|---------|
| **Market Signals** | The primary tab. Layer 1/2/3 analysis, Master Summary Bar, Pre-Trade Checklist, Signal History, AI Analysis Panel. |
| **Weekly Regime** | Macro cycle read from daily candles — EMA structure, ATR, Bollinger Bands, volume trend, AI narrative. |

Both tabs are loaded at page load. The Market Signals tab is the default active tab; the Weekly Regime tab loads its data on first click.

**Coin selection** (Market Signals tab): a five-button strip — BTC, ETH, SOL, ZEC, XAUT — at the top of the page. Switching coins refetches Layer 2 and Layer 3 data for the new symbol, resets the Pre-Trade Checklist, and re-renders the TradingView chart. Layer 1 data is shared across coins (it is coin-agnostic) and is not refetched on coin switch.

**TradingView chart:** An embedded `<iframe>` pointing to `tradingview.com/widgetembed/` with the selected coin's Binance symbol at 4H interval. The embed is rebuilt whenever the coin selection changes. No external JavaScript library is loaded — the iframe approach requires no CDN dependency.

---

## 5. Market Signals System

### 5.1 Overview

The Market Signals tab evaluates each selected coin through three independently-scored layers:

| Layer | Question | Route | Cache |
|-------|----------|-------|-------|
| **Layer 1 — Macro Environment** | Is the broader macro backdrop favourable for risk assets right now? | `GET /api/layer1` | 900s, global (shared across all coins) |
| **Layer 2 — Market Positioning** | Is this specific coin's futures market crowded, and on which side? | `GET /api/layer2/<symbol>` | 300s, per-symbol |
| **Layer 3 — Entry Timing** | Does the immediate price action favour entering long or short right now? | `GET /api/layer3/<symbol>` | 120s, per-symbol |

A client-side **Master Summary Bar** combines the three layers' verdicts into a single composite read (§5.5). On top of the three layers sit two on-demand tools: a **Pre-Trade Checklist** (§5.9) and an **AI Analysis Panel** (§5.7).

### 5.2 Layer 1 — Macro Environment

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

Each indicator computes its own `signal` (`+1` bullish for crypto / `0` neutral / `−1` bearish) from its own thresholds — e.g. Fear & Greed ≤44 is bullish (contrarian: fear favours buying), BTC Dominance falling >0.5% in 24h is bullish (altcoin rotation), DXY/VIX/yield/CPI rising or elevated is bearish, Fed Funds in a cutting cycle is bullish. `GET /api/layer1` combines whichever indicators returned `status: "ok"` into a verdict: fewer than 3 usable indicators → `INSUFFICIENT_DATA`; ≥4 bullish → `FAVORABLE`; ≥4 bearish → `UNFAVORABLE`; otherwise `MIXED`.

**Manual fallback and supplementary cards.** If `TWELVE_DATA_API_KEY` or `FRED_API_KEY` is unset, the corresponding indicator returns `status: "no_key"` and the dashboard renders a manual-entry card instead — the user looks the number up themselves and the value is persisted to `localStorage` (`layer1_<key>`). Four further indicators have **no live source at all** and are always manual: CME FedWatch (next-meeting cut/hike probabilities), the latest jobs report (unemployment rate + direction), the BTC Rainbow Chart band (cycle position), and Global M2 YoY growth. The dashboard recomputes a client-side combined verdict from up to 11 possible signals (7 base + 4 manual): fewer than 3 → `INSUFFICIENT_DATA`; ≥6 bullish → `FAVORABLE`; ≥6 bearish → `UNFAVORABLE`; otherwise `MIXED`.

**Manual card staleness.** Every manual entry timestamps itself when saved and re-renders a freshness note: under 24 hours → green "✓ Updated N hours ago"; 24 hours to 7 days → amber "⚠️ Entered N days ago — verify current value"; past 7 days → red "⚠️ Value is N days old — likely outdated". The value still counts toward the verdict regardless of staleness.

### 5.3 Layer 2 — Market Positioning

For the selected coin's Binance USD-M Futures symbol (e.g. `BTCUSDT`), three signals are pulled from public futures endpoints:

| Signal | Source | What it measures |
|--------|--------|--------------------|
| **Funding Rate** | `GET /fapi/v1/fundingRate` (last 90 settlements) | Current 8h funding rate; >0.05% → "HIGH — Longs Crowded", <−0.01% → "Negative — Shorts Crowded" |
| **Open Interest** | `GET /futures/data/openInterestHist` (1h, 48 bars) + price | 24h change in OI combined with price direction; price↑ + OI↑ → "Strong — New Money Entering"; price↑ + OI↓ → "Weak — Short Covering Only" |
| **Long/Short Ratio** | `GET /futures/data/globalLongShortAccountRatio` and `topLongShortAccountRatio` (1h, 48 bars) | Retail (global) vs. top-trader account long%/short%; flags **divergence** when the two disagree by >10 points and sit on opposite sides of 50/50 |

These combine into a verdict: funding >0.05% *and* global longs >65% → `CAUTION_LONG` (long squeeze risk); funding <−0.01% *and* global shorts >65% → `CAUTION_SHORT`; otherwise a bull/bear tally across funding direction, crowding, and the OI label decides `NEUTRAL` or `MIXED`.

**Liquidation Heatmap (manual, context-only).** A separate input card lets the user record the nearest liquidation cluster price below and above the current price (read off an external liquidation-heatmap tool) per coin, persisted to `localStorage` with a staleness warning after 24 hours. This is **not scored into the Layer 2 verdict** — it feeds the Pre-Trade Checklist's target-price analysis (§5.9) and is passed to the AI Analysis Panel (§5.7) as context.

### 5.4 Layer 3 — Entry Timing

Layer 3 fetches the last 21 four-hour candles (`GET /api/v3/klines`) plus the top-20 order book (`GET /api/v3/depth`) for the selected symbol, and derives four directional signals (`+1`/`0`/`−1` each):

| Signal | Logic |
|--------|-------|
| **Volume Divergence** | Latest candle's volume vs. the 20-candle average. >1.2× average with price up → "Confirmed Move — Real Buyers" (+1); >1.2× with price down → "Confirmed Selling" (−1); <0.8× flips the read (low-conviction move or exhaustion) |
| **Price Structure** | Higher-lows / lower-highs runs over the last 10 candles (≥2 consecutive higher lows → bullish; ≥2 consecutive lower highs → bearish; both at once → "Compression — Breakout Pending") |
| **Momentum** | ROC6 (6-candle ≈ 24h rate of change) vs. ROC14 (14-candle ≈ 56h); positive ROC6 accelerating relative to ROC14 → "Bullish Momentum Building" (+1), and the mirror image for bearish |
| **Order Book** | Bid value vs. ask value across the top 20 levels; >60% bid-side value → "Buy Pressure Dominant" (+1), <40% → "Sell Pressure Dominant" (−1) |

A fifth metric, **ATR(14)** on the same 4h candles, is computed for volatility context only (`signal: 0`, never counted in the verdict) — it classifies the coin as Very Calm / Normal / Elevated / High Volatility and is used by the Pre-Trade Checklist to compute ATR-calibrated take-profit targets (§5.9).

The four directional signals sum into a verdict: score ≥2 → `LONG`; score ≤−2 → `SHORT`; score == 1 → `WEAK_LONG`; score == −1 → `WEAK_SHORT`; score == 0 → `NEUTRAL`; no signals available → `UNKNOWN`.

### 5.5 Master Summary Bar

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

### 5.6 Layer Interconnections

Each layer fetches and computes its verdict completely independently, on its own cache TTL and its own polling interval. None of the three layer routes reads another layer's output, and there is no point in the code where one layer blocks or withholds another from rendering. A user can have an `UNFAVORABLE` Layer 1 and a `LONG` Layer 3 on screen at the same time.

The one place the three layers actually meet is the **Master Summary Bar** (§5.5): a single client-side function reads all three already-computed verdict codes at once. That is the entire extent of "Layer 1 affecting Layer 3" — there is no code where one layer's verdict re-scores or re-interprets another's.

There are, however, two genuine one-directional data hand-offs that exist outside the verdict logic, both feeding into tools below the layer cards:
- **Layer 2's liquidation cluster inputs** (§5.3) feed the Pre-Trade Checklist's Scenario 1 Target/Stretch levels and Scenario 2 distance check (§5.9).
- **Layer 3's ATR%** (§5.4) feeds the Pre-Trade Checklist's Scenario 1 ATR-calibrated levels and Scenario 2 ATR-multiple check (§5.9).

Neither hand-off changes how any layer is scored — they only supply numbers to the Checklist, a downstream advisory tool.

### 5.7 AI Analysis Panel

`POST /api/ai/analysis` sends the current Master Summary verdict, all three layers' live data (including the manual Layer 1 cards and the Layer 2 liquidation cluster inputs, if filled in), and the Pre-Trade Checklist's current TP plan if one exists for the selected coin (§5.9), to Claude (`claude-sonnet-4-6`, `max_tokens=1300`) for a structured trading read.

The panel offers two response styles, chosen with a **Professional / Plain English** toggle (`style: "professional" | "plain"`, default `professional`).

**Professional** fixes the response into four sections:
- **`## WHAT THE MARKET IS DOING`** — 2–3 sentences combining all three layers, specific numbers only
- **`## THE KEY TENSION`** — what's agreeing vs. conflicting between layers, and why it matters
- **`## PROFESSIONAL ASSESSMENT`** — a conviction call (high/medium/low) referencing the 2–3 most important signals
- **`## SUGGESTION`** — one of LONG / SHORT / WAIT, with an entry approach, ATR-based step spacing, and an explicit exit/stop signal to watch

**Plain English** asks for the same four-part structure in jargon-free language, headers: **`## WHAT'S HAPPENING RIGHT NOW`**, **`## WHAT'S PULLING IN DIFFERENT DIRECTIONS`**, **`## WHAT AN EXPERIENCED TRADER WOULD THINK`**, **`## IS YOUR PLAN GOOD?`** — same content requirements as the Professional sections, but any trading term is explained inline.

When a Pre-Trade Checklist TP plan is present, a fifth section is appended: Professional gets **`## PLAN VALIDATION`** (compares the trader's Confirmed TP against the market-offered Target TP from Scenario 1, judges whether a higher target is momentum-justified, and checks the TP against the liquidation clusters). Plain English folds the same comparison into **`## IS YOUR PLAN GOOD?`** via a fish-market analogy — Minimum/Target/Stretch TP as three sizes of fish, and the number of green checks from Scenario 2 deciding whether "the fish the trader wants is available at this market today."

Every response ends with: *"⚠️ This is analytical context to support your own decision — not financial advice. You make the final call."*

This is a single on-demand call per click — there is no caching, scheduled polling, or alerting on top of it.

### 5.8 Refresh Cadence & Caching

| Layer | TTL | Scope | Rationale |
|-------|-----|-------|-----------|
| Layer 1 | 900s (15 min) | Global | Macro indicators don't move within 15 minutes; several depend on rate-limited third-party keys |
| Layer 2 | 300s (5 min) | Per-symbol | Funding/OI/long-short data updates on Binance's hourly/8-hourly cadence |
| Layer 3 | 120s (2 min) | Per-symbol | Entry timing is the most reactive layer; order book and recent-candle volume shift within minutes |

The AI Analysis Panel is **not cached** — it is an explicit, on-demand action that needs the live state at the moment the user asks.

Client-side auto-refresh intervals:
```
Layer 2:        every 5 min  (300s)
Layer 1:        every 15 min (900s)
Layer 3:        every 2 min  (120s)
Signal History: every 5 min  (300s)
```

### 5.9 Pre-Trade Checklist

Sitting below the Layer 1/2/3 cards, the Pre-Trade Checklist is a five-item, entirely client-side worksheet for walking through a trade setup before any order is placed. It resets whenever the selected coin changes — entry, stop, size, and TP are tied to one specific setup — and is purely advisory: it has no connection to any trading engine or exchange.

| Item | What it checks |
|------|-----------------|
| **1. Signal Alignment** | Auto-derived from the Master Summary Bar (§5.5) — green only when the verdict contains "ALIGNED". Also has a LONG/SHORT direction toggle that determines the direction of Scenarios 1 and 2. |
| **2. Entry Price** | A manual price, or "Use live price" to pull the current Layer 3 price |
| **3. Take Profit** | Two analysis modes — see below |
| **4. Stop Loss** | A manual price below/above entry; shows the derived % distance |
| **5. Position Size** | A manual USDT amount, used only for the dollar figures in the R:R summary |

A live **Risk:Reward summary** (`RISK` / `REWARD` / `R:R`) recomputes from Entry, Stop, Confirmed TP, and Position Size on every change.

**Take Profit, Scenario 1 — "What the market is offering."** Three levels are derived from the entry price and the live Layer 3 ATR% (§5.4), recalculating in real time:

```
Minimum = entry × (1 ± ATR% / 100)              "1× ATR — normal candle range"
Target  = nearest_cluster × 0.995 / 1.005       "Nearest cluster ± 0.5%"    (if a Layer 2 cluster value is entered)
        | entry × (1 ± ATR% × 2.5 / 100)        "2.5× ATR — estimated target" (otherwise)
Stretch = second_cluster × 0.995 / 1.005         "Second cluster ± 0.5%"     (if a second cluster price is entered)
        | entry × (1 ± ATR% × 4 / 100)          "4× ATR — strong trend target" (otherwise)
```

Target is auto-selected into Confirmed TP the first time Entry Price is filled in, unless the user has already typed something into Confirmed TP. Each level has a "Use this" button and a one-line tooltip explaining it in plain terms.

**Take Profit, Scenario 2 — "Is your target achievable?"** The user types a desired TP%, and three checks run against it:
1. Whether it sits within the nearest liquidation cluster's distance from entry
2. How many multiples of ATR% it represents (≤2× green / ≤4× yellow / >4× red)
3. Whether the current Layer 3 verdict supports that direction (`LONG`/`WEAK_LONG` → green; `NEUTRAL` → yellow; `SHORT`/`WEAK_SHORT` → red)

The number of green checks drives an overall verdict: 3 green → achievable; 2 → possible but not ideal; 1 → a stretch; 0 → unlikely. A "Use this as my TP" button sets Confirmed TP from the typed %.

**Agreement indicator.** Once both scenarios have a value, a banner compares the user's desired % against the Scenario 1 Target%: **ALIGNED** (within 0.5%), **CONSERVATIVE** (user's target is lower), **AMBITIOUS BUT SUPPORTED** (higher, but ≥2 Scenario 2 checks are green), or **TARGET LIKELY TOO HIGH** (higher, with <2 checks green).

**Server-side hand-off.** The checklist's full state is debounced (600ms) and posted to `POST /api/checklist/tp-plan` as `{coin, plan}`, which the server caches in memory keyed by coin (`_tp_plan_cache` — not persisted to disk, cleared on app restart). The **AI Analysis Panel** (§5.7) includes the cached plan in its prompt whenever one exists for the selected coin.

### 5.10 Signal History

`GET /api/signal_history/<symbol>` returns the persisted signal snapshots for a coin, written by `core/signal_recorder.py`. The Signal History table renders the last N days (user-selectable: 7, 14, 30 days) of historical Layer 1/2/3 verdicts and key indicator values as a scrollable table with color-coded verdict pills.

`SignalRecorder` is triggered by `web/app.py`'s APScheduler background job. On each scheduled tick, it reads the current in-memory cache for each of the five coins and appends a snapshot to `data/signal_history.json` if the cache is fresh.

### 5.11 Retirement of the Original Signal System

Earlier versions of this system had a different, single-score Market Signals architecture: a 4-signal regime score (price vs. EMA50/200, RSI, weekly return) over BTC/ETH/XRP/SOL/ZEC/SUI/XAU/NVDA, three on-the-fly scikit-learn models, a SQLite Signal History database, Fibonacci retracement levels, and automatic Telegram alerts on regime-transition. That system has been **fully superseded** by the Layer 1/2/3 architecture in this section, and asset coverage narrowed to **BTC/ETH/SOL/ZEC/XAUT** — XRP, SUI, and NVDA are no longer covered, and gold is now tracked as XAUT (Tether Gold, `XAUTUSDT` on Binance spot) instead of via a Twelve Data XAU/USD feed.

---

## 6. Weekly Regime Tab

`GET /api/regime` triggers `core/regime_detector.py`, which fetches:
- Last 200 daily klines from Binance spot (`GET /api/v3/klines`, 1d interval)
- Fear & Greed Index (last 30 days, alternative.me)
- BTC Dominance (CoinGecko `/global`)

From the daily klines it computes:
- **EMA50 / EMA200** — trend direction (price above/below each; EMA50 vs. EMA200 crossover)
- **ATR(14)** — normalized volatility (`atr_pct = ATR / price × 100`)
- **Bollinger Bands (20, 2σ)** — price position relative to bands
- **Volume trend** — 7-day vs. prior 7-day average volume
- **Higher-high scan** — whether recent 10d is a new 30d high

These combine with Fear & Greed and BTC Dominance into an overall regime score (`BULLISH` / `NEUTRAL` / `BEARISH`).

`POST /api/regime/confirm` accepts a user-submitted regime label and reasoning, stores it in memory, and informs the AI narrative prompt.

`GET /api/regime/state` returns the saved user confirmation and the last computed regime.

**AI Narrative.** When Claude's API key is configured, `core/regime_detector.py` generates a short weekly narrative from the computed indicators, describing the macro cycle in plain language. This is included in the `GET /api/regime` response as `ai_narrative`.

The Weekly Regime tab is intentionally daily-timeframe and coin-agnostic (BTC-denominated macro view) — it answers "what's the cycle right now" rather than "should I enter a level this week."

---

## 7. Data Sources & Timeframes

| Data | Source | Timeframe | Used by | Cache / Frequency |
|------|--------|-----------|---------|-----------|
| Global macro APIs (alternative.me, CoinGecko, Twelve Data, FRED) | Various — see §5.2 | 1d (daily series) | Layer 1 — Macro Environment | 900s global cache |
| Binance Futures public data (funding, OI, long/short ratios) | `GET /fapi/v1/fundingRate`, `/futures/data/*` | 1h | Layer 2 — Market Positioning | 300s per-symbol cache |
| 4h klines (21 candles ≈ 3.5 days) + order book (top 20) | `GET /api/v3/klines`, `GET /api/v3/depth` | 4h | Layer 3 — Entry Timing | 120s per-symbol cache |
| Daily klines (200 candles ≈ 6.5 months) | `GET /api/v3/klines` | 1d | Weekly Regime tab | On-demand when tab opened |
| Fear & Greed Index (30 days) | alternative.me `/fng/` | 1d | Layer 1 + Weekly Regime | Shared with L1 and regime caches |
| BTC Dominance | CoinGecko `/global` | Instantaneous | Layer 1 + Weekly Regime | Shared |

**Why these timeframes:**

- **Layer 3's 21-candle window:** entry timing only needs the last few days — volume divergence, recent structure, and ATR are all short-lookback by design. 21 four-hour candles (≈3.5 days) covers a 14-period ATR and a 10-candle structure read without dragging in stale price action.
- **Layer 1's 15-minute global cache:** macro indicators don't move within 15 minutes and several depend on rate-limited third-party keys; caching once for all coins avoids redundant calls on every coin switch.
- **Daily klines for regime:** the Weekly Regime tab answers a macro cycle question. Daily candles over ~6 months are the standard window for that read, and pairing them with Fear & Greed and BTC Dominance (both meaningless on a 4h chart) adds macro context.

---

## 8. Infrastructure & Deployment

| Component | Details |
|-----------|---------|
| VPS | Hostinger srv1052900.hstgr.cloud |
| OS | Rocky Linux / RHEL-based |
| Python | 3.x with virtualenv |
| Process management | systemd — `infinity-web.service` |
| Web server | Flask development server, port 5050 |
| Repository | GitHub — zerosanan/Infinity |
| Auto-deploy | `POST /deploy` webhook endpoint triggers `git pull` + `systemctl restart infinity-web` |
| Firewall | firewalld — port 5050 open |

**Environment variables (`.env`):**

| Variable | Required | Used by |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | Yes | AI Analysis Panel (§5.7), Weekly Regime AI narrative (§6) |
| `TWELVE_DATA_API_KEY` | Optional | Layer 1 DXY and VIX live feeds (§5.2); falls back to manual cards if absent |
| `FRED_API_KEY` | Optional | Layer 1 Fed Funds, Treasury Yield, CPI live feeds (§5.2); falls back to manual cards if absent |

**First-time setup:**
```bash
bash deploy/setup-vps.sh          # Clone, create venv, install service
nano ~/infinity/.env              # Fill in API keys
sudo systemctl start infinity-web
```

**Updates:**
```bash
# Via deploy webhook (triggered after git push):
POST /deploy

# Or manually on the VPS:
cd ~/infinity && git pull origin master && sudo systemctl restart infinity-web
```

---

## 9. Planned Extensions

- **Email/Telegram alerts on Market Signals transitions** — Claude key and regime detector exist, but nothing currently triggers an alert automatically on Layer 1/2/3 state transitions; re-wiring an alert (e.g. on Master Summary Bar reaching ALIGNED LONG/SHORT) is a possible follow-up
- **Historical charting for Market Signals** — the Signal History table (§5.10) stores verdict snapshots, but there is no time-series chart of Layer verdict history; a line/area chart overlaying L1/L2/L3 verdicts over time would add pattern context
- **More coins** — the five-asset set (BTC/ETH/SOL/ZEC/XAUT) is fixed in the current coin-tab strip; adding coins requires adding to `MS_SYMBOLS` in the UI and the backend's `supported_symbols` list
- **Persist checklist plan across reloads** — the Pre-Trade Checklist state is fully client-side; refreshing the page clears it. Persisting to `localStorage` per coin (similar to how direction is already persisted) would survive a reload
- **Layer 1 live feed expansion** — adding more automated data sources (e.g. real-time BTC dominance charting, live CME FedWatch probability from an API) to reduce the number of always-manual cards
- **Signal History visualization** — an equity-curve-style chart of the composite signal score over time, layered over price
- **Weekly Regime scheduling** — the regime detector runs on-demand when the tab is opened; a background job that pre-computes and caches the regime once daily would make the tab load instantly

---

## 10. Limitations & Honest Context

### 10.1 This Is an Analysis Tool, Not a Trading System

Nothing in this codebase places orders, manages positions, or connects to exchange accounts. The Layer 1/2/3 verdicts, the Master Summary Bar, the Pre-Trade Checklist, and the AI Analysis Panel are all advisory outputs — they describe the current state of the market as computed from public data and a language model's interpretation of that data. They do not constitute financial advice, and no signal output should be acted on without the user's own independent judgment.

### 10.2 Data Freshness and API Availability

Layer 1, 2, and 3 verdicts are cached and may lag real-time market conditions by up to their respective TTLs (900s / 300s / 120s). Manual cards in Layer 1 have no automatic staleness expiry — the user is responsible for keeping them current. If Binance's public APIs are unavailable or rate-limited, the affected layer will show cached data until the cache expires, then show an error state.

The AI Analysis Panel calls Claude's API synchronously on each click — if the API is down, slow, or the key is unset, the panel will show an error. There is no fallback response or cached AI output.

### 10.3 Layer 3 Signal Quality

Layer 3 uses only 21 four-hour candles and the top-20 order book. These are deliberately short lookbacks — "is the setup right in the next few hours" rather than "is this a good trade over the next week." The ATR, structure, and momentum reads can be noisy on a single 4-hour window, particularly for lower-liquidity assets (ZEC, XAUT). A `LONG` verdict from Layer 3 does not mean the asset will rise — it means the short-term indicators are currently tilted bullish.

### 10.4 Manual Layer 1 Cards Have No Validation

The dashboard accepts whatever value the user types into CME FedWatch, jobs report, Rainbow Chart band, and Global M2 cards. An incorrect value (misread, typo, or stale number) counts toward the Layer 1 verdict with no sanity check. The staleness warnings (§5.2) are the only guard.

### 10.5 AI Analysis is Language-Model Output

The AI Analysis Panel's responses are generated by a large language model from structured data. The model can and does make plausible-sounding errors — incorrect relationships between indicators, overconfident directional calls, or misreads of the data passed to it. The panel is a starting point for analysis, not the analysis itself. The disclaimer at the end of every response — "This is analytical context to support your own decision — not financial advice. You make the final call." — is not boilerplate; it reflects the actual epistemic status of the output.

### 10.6 Flask Development Server

The dashboard runs on Flask's development server (`web/app.py`, port 5050), which Flask explicitly marks as not suitable for production. It has limited concurrency (one request at a time per worker), no hardened error handling, and no SSL. For single-user, trusted-network use this is acceptable. For any multi-user or public-facing deployment, a WSGI server (gunicorn/waitress) behind a reverse proxy with HTTPS is required.

---

*Infinity — a market analysis dashboard for systematic, data-driven decision support. Section 10 is part of this document — read it before acting on any signal.*
