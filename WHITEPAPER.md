# Infinity — Market Signals Dashboard
## Technical White Paper

**Version:** 2.0 · **Status:** Living document · **Scope:** Full strategy and technical specification of the system as implemented.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Design Philosophy](#2-design-philosophy)
3. [System Architecture](#3-system-architecture)
4. [The Signal Model — Strategy](#4-the-signal-model--strategy)
   - [4.1 Layer 1 — Macro Environment](#41-layer-1--macro-environment)
   - [4.2 Layer 2 — Market Positioning](#42-layer-2--market-positioning)
   - [4.3 Market Mechanics](#43-market-mechanics)
   - [4.4 Layer 3 — Entry Timing](#44-layer-3--entry-timing)
   - [4.5 The Master Summary Bar](#45-the-master-summary-bar)
5. [The Tiered Signal System](#5-the-tiered-signal-system)
6. [The Pre-Trade Checklist & Take-Profit Methodology](#6-the-pre-trade-checklist--take-profit-methodology)
7. [The AI Analysis Layer](#7-the-ai-analysis-layer)
8. [Data Layer](#8-data-layer)
9. [Scheduling, Caching & Refresh Cadence](#9-scheduling-caching--refresh-cadence)
10. [HTTP API Surface](#10-http-api-surface)
11. [Telegram Alerting](#11-telegram-alerting)
12. [Resistance / Support Level Alerts](#12-resistance--support-level-alerts)
13. [Deployment & Operations](#13-deployment--operations)
14. [Security Considerations](#14-security-considerations)
15. [Limitations, Known Gaps & Honest Context](#15-limitations-known-gaps--honest-context)
16. [Roadmap](#16-roadmap)
17. [Appendix A — Formula Reference](#appendix-a--formula-reference)
18. [Appendix B — Threshold Reference](#appendix-b--threshold-reference)
19. [Appendix C — Glossary](#appendix-c--glossary)

---

## 1. Executive Summary

**Infinity** is a cryptocurrency market-analysis dashboard. It aggregates live data from Binance public spot and futures APIs, macro-economic feeds (FRED, Twelve Data, CoinGecko, alternative.me), and a Claude-powered narrative generator, and distils them into structured, three-layer signal reads for five assets: **BTC, ETH, SOL, ZEC, and XAUT** (Tether Gold).

The system **does not execute trades, manage positions, hold exchange credentials with trading permissions, or connect to any private account endpoint.** Every Binance call is a public, unauthenticated market-data call. Its purpose is to help a trader answer four questions before placing an order somewhere else:

| # | Question | Answered by |
|---|----------|-------------|
| 1 | Is the macro backdrop favourable for risk assets right now? | **Layer 1 — Macro Environment** |
| 2 | Is this coin's futures market crowded, and on which side? | **Layer 2 — Market Positioning** |
| 3 | *How* is price moving — real ownership or leverage? | **Market Mechanics** |
| 4 | Does the immediate price action favour entering long or short? | **Layer 3 — Entry Timing** |

On top of those reads sit four synthesis products:

- A **Master Summary Bar** that collapses the three layers into one of six alignment states.
- A **Tiered Signal System** that scores six independent criteria symmetrically for LONG and SHORT and classifies the result as `NONE` / `DEVELOPING` / `STRONG`, pushing **Telegram alerts** on state changes.
- A **Pre-Trade Checklist** that turns a read into a concrete plan — direction, entry, take profit (two independent scenarios that are cross-checked against each other), stop loss, position size, and risk/reward.
- An **AI Analysis Panel** that generates an on-demand narrative from the full dashboard state in one of two voices.

**Deployment:** a single Flask application (`web/app.py`, ~2,300 lines) running under systemd on a VPS, serving a two-tab single-page UI (**Market Signals**, **Checklist**) on port 5050, with an embedded APScheduler background scheduler and a SQLite (WAL) persistence layer.

---

## 2. Design Philosophy

| Principle | What it means in this codebase |
|-----------|-------------------------------|
| **Signal before execution** | No order is ever placed by this system. There is no trading client, no signed request, no position state. The analysis informs a decision the user executes elsewhere. |
| **Three independent layers, no gating** | Macro, positioning, and timing answer different questions from different data sources. None is a prerequisite for another; they are read together, and their *disagreement* is treated as information, not noise. |
| **Quantitative where possible, manual where not** | Anything computable from a live API is computed automatically. Indicators with no free live source (CME FedWatch, BTC Rainbow Chart, Non-Farm Payrolls, Global M2, liquidation clusters, exchange net flow, whale orders) are manual-entry cards with staleness warnings and a one-click link to the source. |
| **Degrade, never fail** | Every external fetch is individually wrapped. A dead API produces one error card, not a dead dashboard. Verdicts are computed from whatever returned successfully, with an explicit `INSUFFICIENT_DATA` floor. |
| **Transparency over confidence** | No single "buy/sell" number is presented. Every layer shows the individual signals that produced its verdict; the Tiered Signal card shows which of the six criteria fired and which did not; the checklist shows how many checks are green, not just go/no-go. |
| **Contrarian on crowding, confirmatory on flow** | Crowd positioning (funding, retail long/short, Fear & Greed) is read *against* the crowd. Order flow (taker aggression, volume confirmation, order-book imbalance) is read *with* the flow. These are deliberately different treatments of different data types. |
| **Advisory only** | Nothing in the system constitutes financial advice. Every AI response is required by its system prompt to end with an explicit disclaimer. |

---

## 3. System Architecture

### 3.1 Repository layout

```
Infinity/
├── core/
│   ├── __init__.py
│   ├── db.py                   # SQLite persistence layer — 10 tables, all DB access
│   ├── signal_recorder.py      # Periodic Layer 1/2/3 snapshot writer + reader
│   ├── signal_evaluator.py     # Six-criteria tier evaluation + state machine
│   ├── level_watcher.py        # Resistance/support level watch + alert state machine
│   └── telegram_notifier.py    # Telegram Bot API sender (shared by both alert producers)
├── web/
│   ├── app.py                  # Flask app: all routes, all fetchers, all scoring, scheduler
│   ├── templates/
│   │   ├── index.html          # The entire dashboard SPA (markup + CSS + JS, self-contained)
│   │   └── backtest.html       # Orphaned — no route serves it (see §15.6)
│   └── static/
│       ├── app.js              # Legacy, unreferenced (see §15.6)
│       └── style.css           # Legacy, unreferenced (see §15.6)
├── data/                       # Auto-created; infinity.db + resistance_levels.json (git-ignored)
├── deploy/
│   └── setup-vps.sh            # One-time VPS provisioning (clone, venv, systemd unit)
├── .github/workflows/deploy.yml
├── .env.example                # Environment variable template
├── requirements.txt
├── dynamic_spot_dca_system_spec.md   # Historical spec of the predecessor DCA bot
└── WHITEPAPER.md               # This document
```

### 3.2 Process model

One Python process does everything:

```
┌──────────────────────────────────────────────────────────────┐
│  systemd unit: infinity-web                                  │
│  └─ python web/app.py                                        │
│     ├─ Flask (werkzeug dev server), 0.0.0.0:5050             │
│     │  └─ threaded request handling → API routes             │
│     ├─ APScheduler BackgroundScheduler (UTC)                 │
│     │  ├─ record_l1  — every 6 hours                         │
│     │  ├─ record_l2  — every 1 hour  ──► evaluate + notify    │
│     │  └─ record_l3  — cron, 00:00 UTC daily                 │
│     ├─ In-memory TTL caches (L1 / L2 / L3 / Mechanics)       │
│     └─ SQLite WAL (data/infinity.db), connection-per-op      │
└──────────────────────────────────────────────────────────────┘
```

There is no worker queue, no Redis, no external state. Concurrency safety comes from three choices: SQLite in **WAL** mode, a **fresh connection per operation** (`core/db.py::_conn`, a context manager that commits on success and rolls back on exception), and caches that are plain dicts written under the GIL with last-write-wins semantics — acceptable because every cached value is an idempotent snapshot of external data.

### 3.3 Data flow

```
 External APIs                    Flask process                      Client
──────────────                 ──────────────────                 ──────────
Binance FAPI  ──┐                                             ┌─ Market Signals tab
Binance Spot  ──┤   ┌──► _get_layer2_data()  ──► cache 5m ──┐  │   ├─ Layer cards (4)
CoinGecko     ──┤   │    _get_market_mech..() ──► cache 5m ──┤  │   ├─ Master Summary Bar
alternative.me ─┼──►│    _get_layer3_data()  ──► cache 2m ──┼─►│   ├─ Tiered Signal card
FRED          ──┤   │    _get_layer1_data()  ──► cache 15m ─┘  │   └─ Signal History tables
Twelve Data   ──┘   │                                          └─ Checklist tab
                    │           │                                  ├─ Scenario 1 / 2 TP
                    │           ▼                                  ├─ R:R + 5 checks
                    │    SignalRecorder ──► signal_history         └─ AI Analysis panel
                    │           │
                    │           ▼
                    │    SignalEvaluator ──► signal_state ──► Telegram
                    │
Anthropic API ◄─────┘    /api/ai/analysis
```

### 3.4 Dependencies

| Package | Version | Role |
|---------|---------|------|
| `flask` | 3.0.3 | HTTP server and templating |
| `flask-cors` | 4.0.1 | CORS (currently wide open — see §14) |
| `requests` | 2.31.0 | All outbound HTTP, 8 s timeout on market data, 10 s on Telegram |
| `python-dotenv` | 1.0.0 | `.env` loading |
| `anthropic` | ≥0.25.0 | Claude API client for the AI Analysis panel |
| `APScheduler` | ≥3.10.0 | Background recording/evaluation jobs |

SQLite is used via the Python standard library. The frontend has **zero build step and zero runtime dependencies** — `index.html` is a self-contained SPA with inline CSS and JavaScript, including hand-rolled SVG sparklines and a hand-rolled Markdown renderer for AI output.

---

## 4. The Signal Model — Strategy

The four analytical panels below are the heart of the system. Each produces (a) per-indicator labels with a colour and a trinary signal in `{-1, 0, +1}`, and (b) a combined verdict code.

### 4.1 Layer 1 — Macro Environment

**Question: is the broad environment favourable for risk assets?**

Layer 1 is **global, not per-coin** — one macro read applies to every asset on the dashboard. Seven indicators are fetched **concurrently** through a `ThreadPoolExecutor` (one worker per fetcher) so total latency is bounded by the slowest feed rather than their sum, then cached for 15 minutes.

| Indicator | Source | Signal logic | Rationale |
|-----------|--------|--------------|-----------|
| **Fear & Greed Index** | alternative.me (30-day history) | ≤24 Extreme Fear → **+1**; 25–44 Fear → **+1**; 45–55 Neutral → 0; 56–74 Greed → **−1**; ≥75 Extreme Greed → **−1** | **Contrarian.** Crowd emotion is a fade signal: maximum fear marks capitulation lows, maximum greed marks distribution tops. |
| **BTC Dominance** | CoinGecko `/global` | 24 h change > +0.5 pts → "BTC Season" **−1**; < −0.5 pts → "Altcoin Season" **+1**; else 0 | Rising dominance means capital rotating *out* of the risk curve into BTC — a defensive posture. Falling dominance means risk appetite is expanding. |
| **DXY (US Dollar Index)** | Twelve Data, 1-day bars | 7-day change > +0.5 % → **−1** (headwind); < −0.5 % → **+1** (tailwind); else 0 | Crypto is priced in dollars and competes with dollars. Dollar strength drains global liquidity. |
| **Fed Funds Rate** | FRED `FEDFUNDS` (3 obs) | Current > value 3 months ago → Rising Cycle **−1**; current < → Cutting Cycle **+1**; equal → Holding 0 | Policy direction matters more than level. Cutting cycles historically precede risk-asset expansion. Card also surfaces the next FOMC date. |
| **US 10-Year Treasury Yield** | FRED `DGS10` (8 obs) | 7-day change < 0 **and** level < 4.0 % → **+1**; 7-day change > 0 **and** level > 4.5 % → **−1**; else 0 | The risk-free rate crypto competes with for capital. Note the signal requires *both* level and direction to agree, so a falling-but-still-high yield scores neutral. |
| **CPI (YoY)** | FRED `CPIAUCSL` (13 obs) | YoY falling **and** < 3 % → **+1**; YoY > 4 % **or** rising → **−1**; else 0 | Inflation is the input to Fed policy. The card also states the distance from the Fed's 2 % target explicitly. |
| **VIX** | Twelve Data, 1-day bars | < 15 Calm → **+1**; 15–25 Normal → 0; 25–30 Elevated Fear → 0; 30–40 High Fear → **−1**; > 40 Crisis → **−1** | Traditional-market fear gauge. Crypto has no independent volatility regime during equity stress — correlation goes to 1 in a crisis. |

**Server-side verdict** (`_layer1_verdict`):

```
signals = [each indicator with status == "ok"]
if len(signals) < 3          → INSUFFICIENT_DATA  ⚪
elif bullish_count >= 4      → FAVORABLE          🟢
elif bearish_count >= 4      → UNFAVORABLE        🔴
else                         → MIXED              🟡
```

#### Manual Layer 1 cards

Four macro inputs have no free live feed and are **always manual**; five live indicators additionally accept a manual value used as a **fallback when the live fetch is unavailable** (no API key, rate limit, or error).

| Card | Type | Signal logic |
|------|------|--------------|
| **CME FedWatch** (always manual) | Two probabilities | Cut probability > 60 % → **+1**; hike probability > 40 % → **−1**; else 0 |
| **Jobs Report / NFP** (always manual) | Number + direction toggle | Unemployment rising → **+1** (Fed cuts sooner); falling → **−1** (Fed stays hawkish); flat → 0 |
| **BTC Rainbow Chart** (always manual) | 9-band select | "Fire Sale" / "Buy" / "Accumulate" → **+1**; "Still Cheap" / "HODL" → 0; "Bubble" / "FOMO" / "Sell" / "Maximum Bubble" → **−1** |
| **Global M2 Growth** (always manual) | YoY % | > 5 % → **+1** (expanding liquidity); < 0 % → **−1** (contracting); else 0 |
| **DXY** (fallback) | Level | > 105 → **−1**; < 100 → **+1**; else 0 |
| **Fed Funds** (fallback) | Level + cycle toggle | Cutting → **+1**; rising → **−1**; holding → 0 |
| **10Y Yield** (fallback) | Level | < 3.5 % → **+1**; > 4.5 % → **−1**; else 0 |
| **CPI** (fallback) | YoY % | < 2.5 % → **+1**; > 4 % → **−1**; else 0 |
| **VIX** (fallback) | Level | < 15 → **+1**; > 30 → **−1**; > 25 → 0 (Elevated); else 0 |

Each manual card renders an **age badge** (`l1StalenessHTML`) so a value entered three weeks ago is visibly stale rather than silently authoritative, and links directly to its source (TradingView, FRED, CME, Blockchain Center, etc.).

> **Important asymmetry.** The browser recomputes the Layer 1 verdict across the *combined* live + manual set (up to 11 indicators) using a **≥6 bullish / ≥6 bearish** threshold, while the server computes it from live indicators only (max 7) using **≥4 / ≥4**. The badge you see in the browser and the verdict the background evaluator uses for Telegram alerts can therefore differ. See §15.1.

### 4.2 Layer 2 — Market Positioning

**Question: is this coin's futures market crowded, and on which side?**

All four indicators come from Binance USD-M futures public endpoints, per symbol, cached 5 minutes.

#### 4.2.1 Funding Rate

Endpoint: `/fapi/v1/fundingRate` (90 records; last 30 rendered as a sparkline).

| Current rate (8 h) | Label | Colour |
|--------------------|-------|--------|
| > 0.05 % | **HIGH — Longs Crowded** | red |
| 0.01 % – 0.05 % | Elevated | yellow |
| −0.01 % – 0.01 % | Neutral | green |
| < −0.01 % | **Negative — Shorts Crowded** | blue |

Funding is the periodic payment between perpetual longs and shorts that tethers the perp to spot. Persistently positive funding means longs are paying to hold — crowded, and structurally fragile because the position has a carrying cost. Negative funding means shorts are paying, which is the fuel for a squeeze. The card also computes a live **countdown to the next funding settlement** (00:00 / 08:00 / 16:00 UTC), because crowded positioning tends to resolve around settlement.

#### 4.2.2 Open Interest

Endpoint: `/futures/data/openInterestHist` (1 h period, 48 points, `sumOpenInterestValue`), cross-referenced with 1 h klines. 24-hour change is measured against the bar 25 positions back.

OI alone is directionless; OI **combined with price direction** is one of the most informative reads in derivatives:

| Price | OI | Label | Interpretation |
|-------|----|-------|----------------|
| Up | Up | **Strong — New Money Entering** 🟢 | New longs are funding the advance. Genuine trend. |
| Up | Down | **Weak — Short Covering Only** 🟠 | Rally is shorts closing, not buyers arriving. No new demand behind it. |
| Down | Up | **Strong Selling — New Shorts** 🔴 | New short positions are being opened into weakness. Genuine downtrend. |
| Down | Down | **Exhaustion — Longs Closing** 🟡 | Decline is longs capitulating, not new sellers. Selling pressure is finite. |

The card renders an **overlay chart** of the OI and price series so the relationship is visible, not just labelled.

#### 4.2.3 Long/Short Account Ratios

Two endpoints, 1 h period, 48 points:
- `/futures/data/globalLongShortAccountRatio` — **all accounts** (retail-dominated by headcount)
- `/futures/data/topLongShortAccountRatio` — **top accounts by margin balance**

Crowding label (applied to both): long % > 65 → "Longs Crowded" (red); short % > 65 → "Shorts Crowded" (blue); 45–55 → "Balanced" (green); otherwise "Neutral".

**Divergence detection** fires when top traders are positioned *opposite* to retail:

```
divergence  ⟺  |global_long_pct − top_long_pct| > 10 pts
               AND (global_long_pct − 50) × (top_long_pct − 50) < 0
```

The second condition is the strict part: the two cohorts must be on **opposite sides of 50 %**, not merely far apart. Retail 58 % long and top traders 70 % long is a 12-point gap but is *not* a divergence — both are long.

#### 4.2.4 Position Ratio Divergence — the dollar-weighted read

Endpoint: `/futures/data/topLongShortPositionRatio`. Despite the response field names (`longAccount` / `shortAccount`), this endpoint reports the **dollar size of positions held**, not the number of accounts holding them. Comparing it against the *account* ratio from §4.2.3 is the single most structurally interesting signal in Layer 2, and the reasoning is documented at length in `compute_position_account_divergence`:

> Total long notional always equals total short notional in a futures market — every contract has two sides. So if **66 %** of top-trader *accounts* are long but only **55 %** of top-trader *position value* is long, the arithmetic forces a conclusion: the long accounts are individually **smaller**, and the short accounts are individually **larger**. The traders carrying the biggest positions are more short than the headcount suggests. Dollar-weighted money is leaning short while account-weighted sentiment reads long.
>
> This reveals institutional positioning without anyone having to announce it.

```
gap = position_long_pct − account_long_pct

|gap| < 5 pts    → "minimal"      (no signal)
|gap| < 10 pts   → "meaningful"   (noted, not scored)
|gap| ≥ 10 pts   → "significant"  (scored in the verdict and in the Tiered Signal System)

gap > 0 → longs_larger_than_headcount   (structurally bullish)
gap < 0 → shorts_larger_than_headcount  (structurally bearish)
```

When the divergence is significant, the dashboard renders a plain-English note beneath the verdict badge, e.g. *"Position ratio shows big money 12.4 pts more SHORT than headcount — large capital leaning more bearish than account ratio alone suggests."*

#### 4.2.5 Layer 2 verdict

Two **override conditions** are checked first — these are the textbook crowded-trade setups and short-circuit the scoring:

```
funding > 0.05 % AND global_long > 65 %   → CAUTION_LONG   🔴
funding < −0.01 % AND global_short > 65 % → CAUTION_SHORT  🔵
```

Otherwise four bullish and four bearish conditions are tallied:

| Bullish | Bearish |
|---------|---------|
| Funding negative | Funding high |
| Shorts crowded | Longs crowded |
| OI label starts "Strong —" (new money entering) | OI label starts "Strong Selling" (new shorts) |
| Significant position divergence, longs larger | Significant position divergence, shorts larger |

If both tallies are non-zero → `MIXED` 🟡. Otherwise → `NEUTRAL` 🟢.

> Note the deliberate semantics: in this system `CAUTION_LONG` is the *bearish* Layer 2 state (it warns against being long), and `NEUTRAL` / `CAUTION_SHORT` are treated as bullish-permissive by the Master Summary Bar. Layer 2 is a **risk filter**, not a direction generator — its job is to say "this side is crowded", not "go this way".

### 4.3 Market Mechanics

**Question: *how* is price moving — who is initiating, and with what kind of money?**

Cached 5 minutes, deliberately matched to the Layer 2 TTL so that recorded snapshots of both stay in sync.

#### 4.3.1 Taker Buy/Sell Ratio

Endpoint: `/futures/data/takerlongshortRatio` (1 h, 48 points; 24-point sparkline).

```
buy_pct = buySellRatio / (1 + buySellRatio) × 100
```

Taker volume is volume from orders that **crossed the spread** — traders who wanted in *now* and paid for immediacy. It separates aggression from passive resting liquidity.

| buy_pct | Label |
|---------|-------|
| > 60 % | **Buyers Aggressive — Initiating Moves** 🟢 |
| 40–60 % | Balanced — No Clear Aggressor |
| < 40 % | **Sellers Aggressive — Initiating Moves** 🔴 |

#### 4.3.2 Spot vs Futures Volume

24-hour quote volume from Binance spot `/api/v3/ticker/24hr` and futures `/fapi/v1/ticker/24hr`.

```
ratio = futures_24h_usd / spot_24h_usd
```

| Ratio | Label | Meaning |
|-------|-------|---------|
| < 3 | **Spot Dominant — Real Ownership Driving** 🟢 | People are buying the asset, not renting exposure. Durable moves. |
| 3–8 | Balanced — Mixed Spot/Futures Activity | Normal market. |
| 8–15 | **Futures Dominant — Leveraged Speculation Driving** 🟠 | The move is leverage. Reverses violently. |
| ≥ 15 | **Extreme Futures Dominance — High Leverage Risk** 🔴 | Cascade conditions. |

#### 4.3.3 Manual mechanics inputs

Three additional per-coin manual panels persist to SQLite and feed the AI analysis context:

- **24 h Liquidations** (`liq24h_manual`) — long vs short liquidation totals.
- **Exchange Net Flow** (`exchange_flow_manual`) — direction (inflow/outflow), size, free-text notes. Coins leaving exchanges is supply leaving the sell side.
- **Whale Order Watchlist** (`whale_orders`) — a tracked list of large resting orders with price, size, direction, status (`pending` / filled / pulled) and notes, so a wall that gets *pulled* rather than filled is recorded as the information it is.

### 4.4 Layer 3 — Entry Timing

**Question: does the immediate price action favour entering right now?**

Source: Binance **spot** 4-hour klines, 21 candles (`/api/v3/klines`) plus spot order-book depth. Cached 2 minutes — the fastest-refreshing layer, because timing decays fastest.

Four indicators produce directional signals; a fifth (ATR) is deliberately non-directional and produces sizing context only.

#### 4.4.1 Volume Divergence

Current candle volume vs the 20-period average, combined with candle direction:

| Volume ratio | Candle | Signal | Label |
|--------------|--------|--------|-------|
| > 1.2× | Up | **+1** | Confirmed Move — Real Buyers |
| > 1.2× | Down | **−1** | Confirmed Selling — Real Pressure |
| < 0.8× | Up | **−1** | Weak Move — Low Conviction |
| < 0.8× | Down | **+1** | Exhaustion — Move Losing Steam |
| 0.8–1.2× | — | 0 | Normal Volume — No Strong Signal |

The two diagonal cases carry the insight: a **rally on thin volume is bearish**, and a **decline on thin volume is bullish**. Volume is the confirmation, not the direction.

#### 4.4.2 Price Structure

Over the last 10 candles, consecutive-run counters track higher lows and lower highs. A run of **2 or more** consecutive higher lows sets `higher_lows`; 2 or more consecutive lower highs sets `lower_highs`.

| State | Signal | Label |
|-------|--------|-------|
| Higher lows only | **+1** | Higher Lows — Buyers Getting Aggressive |
| Lower highs only | **−1** | Lower Highs — Sellers Getting Aggressive |
| Both | 0 | **Compression — Breakout Pending** 🟡 |
| Neither | 0 | No Clear Structure |

The "both" case is a triangle: range contracting from both sides, breakout direction unknown. Explicitly scored as neutral rather than forced into a direction.

#### 4.4.3 Momentum (Rate of Change)

On 4 h closes: `ROC6` spans 6 periods (24 h), `ROC14` spans 14 periods (56 h).

```
accelerating  ⟺  |ROC6| > |ROC14 / 2|
```

That is: is the *recent* rate of change outpacing the longer-run average rate? If short-term momentum is more than half the long-term move, the move is speeding up.

| Condition | Signal | Label |
|-----------|--------|-------|
| \|ROC6\| < 0.5 % | 0 | No Momentum |
| Positive + accelerating | **+1** | Bullish Momentum Building |
| Positive, not accelerating | 0 | Rally Slowing — Watch for Reversal |
| Negative + accelerating | **−1** | Bearish Momentum Building |
| Negative, not accelerating | 0 | Selling Slowing — Watch for Recovery |

Decelerating moves score **neutral, never directional** — a slowing rally is not a buy signal, and a slowing sell-off is not a short signal.

#### 4.4.4 Order Book Imbalance

Spot depth, top 20 levels each side, weighted by **notional** (`price × quantity`), not raw quantity:

| Bid share of total notional | Signal | Label |
|------------------------------|--------|-------|
| > 60 % | **+1** | Buy Pressure Dominant |
| 40–60 % | 0 | Balanced Order Book |
| < 40 % | **−1** | Sell Pressure Dominant |

This is the **weakest and most manipulable** signal in the system — spoofed walls appear and vanish — which is precisely why it is one vote of four rather than a standalone trigger. See §15.4.

#### 4.4.5 ATR(14) — Volatility Context

Mean True Range over the 14 most recent 4 h intervals (computed from the last 15 candles, so the newest, still-forming candle is included), expressed as a percentage of current price. **Signal is hard-coded to 0** — ATR never votes on direction. Its job is to calibrate distance:

| ATR % | Label | DCA step spacing guidance |
|-------|-------|---------------------------|
| < 1 % | Very Calm 😴 | Tight spacing (3–5 % steps) |
| 1–2 % | Normal | Standard spacing (5–10 % steps) |
| 2–4 % | Elevated | Wider spacing (10–15 % steps) |
| > 4 % | High Volatility ⚡ | Very wide spacing (15 %+) or wait for volatility to settle |

ATR is the unit of measurement for the entire take-profit methodology in §6 — it converts "is this target realistic?" from an opinion into an arithmetic question.

#### 4.4.6 Layer 3 verdict

The four directional signals are summed into a score in `[−4, +4]`:

| Score | Verdict |
|-------|---------|
| ≥ +2 | **LONG SIGNAL** 🟢 |
| +1 | WEAK LONG 🟡 |
| 0 | NEUTRAL ⚪ |
| −1 | WEAK SHORT 🟡 |
| ≤ −2 | **SHORT SIGNAL** 🔴 |
| no data | UNKNOWN |

The verdict object also carries the raw `bullish` / `bearish` / `neutral` / `total` counts, so the UI can show *2 of 4 signals bullish* rather than only the headline.

### 4.5 The Master Summary Bar

The Master Summary collapses the three layer verdicts into a single alignment state. It is computed **identically in two places** — `_signal_snapshot()` in `web/app.py` (server, feeds the Tiered Signal System) and `updateMasterSummary()` in `index.html` (browser, feeds the display and the checklist).

```
l1_bull = L1 == FAVORABLE
l1_bear = L1 == UNFAVORABLE
l2_bear = L2 == CAUTION_LONG
l2_bull = L2 in (NEUTRAL, CAUTION_SHORT)
l3_bull = L3 in (LONG, WEAK_LONG)
l3_bear = L3 in (SHORT, WEAK_SHORT)

l1_bull and l2_bull and l3_bull        → 🟢 ALIGNED LONG   — All layers bullish
l1_bear and l2_bear and l3_bear        → 🔴 ALIGNED SHORT  — All layers bearish
l1_bull and l3_bull and not l2_bear    → 🟡 DEVELOPING     — Missing L2 confirmation
L1 in (MIXED, INSUFFICIENT_DATA)       → 🟡 MIXED          — Macro not fully clear
l2_bear or l3_bear                     → 🟠 CAUTION        — Check individual layers
otherwise                              → ⚪ WAIT           — Layers not aligned
```

Evaluation order matters: the two full-alignment cases are tested first, then `DEVELOPING`, then macro ambiguity, then the caution catch-all. `ALIGNED SHORT` requires all three layers bearish and is therefore rare by construction — which is intentional, since the system's bias is that shorting into an unfavourable macro backdrop still requires positioning and timing confirmation.

---

## 5. The Tiered Signal System

The four panels above describe the market. The **Tiered Signal System** decides when that description is worth waking someone up for. It runs server-side, unattended, once per hour, for all five symbols, and is the only part of the system that reaches out to the user rather than waiting to be looked at.

**Module:** `core/signal_evaluator.py`. **State:** `signal_state` table. **Config:** `signal_config` table.

### 5.1 The six criteria

Each criterion is evaluated **twice and symmetrically** — once assuming LONG, once assuming SHORT — producing two independent scores out of 6.

| # | Criterion | LONG confirms when | SHORT confirms when |
|---|-----------|--------------------|---------------------|
| 1 | **Master Summary** | `ALIGNED LONG`, or `DEVELOPING` with L3 in (LONG, WEAK_LONG) | `ALIGNED SHORT`, or `CAUTION` with L3 in (SHORT, WEAK_SHORT) |
| 2 | **L2 Crowding** (contrarian) | Global **short** % > 65 | Global **long** % > 65 |
| 3 | **Position Ratio Divergence** | Significance `significant` **and** `longs_larger_than_headcount` | Significance `significant` **and** `shorts_larger_than_headcount` |
| 4 | **OI Trend** | Last *N* consecutive Layer 2 snapshots all labelled "Strong — New Money Entering" | Last *N* all labelled "Exhaustion — Longs Closing" or "Exhaustion — Low Conviction" |
| 5 | **Market Mechanics** | Taker buy % > 55 **OR** volume label contains "Spot Dominant" | Taker sell % > 55 **OR** volume label contains "Futures Dominant" |
| 6 | **Liquidation Proximity** | Cluster **above** price within threshold % | Cluster **below** price within threshold % |

Notes on the design:

- **Criterion 2 is deliberately contrarian.** Crowded shorts confirm a *long*. This is the same logic as the funding read: crowding is fuel, not direction.
- **Criterion 4 is the only one with memory.** It reads the last *N* Layer 2 snapshots back out of `signal_history` (default N = 2, i.e. two consecutive hourly cycles) and requires **all** of them to carry a matching OI label. A single hour of favourable OI does not count; the trend must persist. This is the mechanism that prevents one noisy reading from tipping a tier.
- **Criterion 5 uses OR, not AND.** Either aggression (taker) or capital type (spot/futures) confirming is enough — they are alternative views of the same question.
- **Criterion 6 depends on manual data.** Liquidation clusters have no free API; the trader enters them per coin. Distance is measured against the current price from the Layer 3 cache:
  ```
  distance_pct = |cluster − current_price| / current_price × 100
  confirms     ⟺ distance_pct ≤ liq_proximity_pct    (default 3 %, UI-settable up to 50 %)
  ```

### 5.2 Direction selection and tier classification

```python
if long_count >= short_count and long_count >= 3:   direction = LONG
elif short_count > long_count and short_count >= 3: direction = SHORT
else:                                               # neither side reaches 3
    direction = LONG if long_count >= short_count else SHORT
    confirmed = max(long_count, short_count)
```

Ties resolve to LONG. The final tier then applies a veto:

```
contradicted ⟺ (direction == SHORT and master == "ALIGNED LONG")
            or (direction == LONG  and master == "ALIGNED SHORT")

confirmed >= 5 and not contradicted   → STRONG      🔴
confirmed >= 3                        → DEVELOPING  🟡
otherwise                             → NONE        (direction forced to NONE)
```

The contradiction veto is narrow by design: it only blocks promotion to `STRONG` when the Master Summary is in **full** opposing alignment. A `CAUTION` or `MIXED` master does not veto — those states already mean "unclear", and blocking on them would make `STRONG` unreachable in exactly the choppy conditions where a 5-of-6 confluence is most informative.

### 5.3 Notification state machine

State per symbol lives in `signal_state`: current `tier`, `direction`, both counts, the full `criteria_detail` map, `entered_tier_at` (reset only when the tier actually changes, so "in STRONG since 14:00" is meaningful), `last_evaluated_at`, and `last_notified_tier`.

```python
should_notify = (
    new_tier == "STRONG"
    or (new_tier == "DEVELOPING" and prev_notified_tier != "DEVELOPING")
)
```

- **`DEVELOPING` fires once** on entry and stays quiet while it persists.
- **`STRONG` fires on every hourly evaluation** for as long as it holds. This is intentional for a 5-of-6 confluence but means a sustained STRONG state produces an hourly message — see §15.3.
- If `telegram_enabled` is false, the state is still marked notified so the queue does not back up and then flood when alerts are re-enabled.

### 5.4 Configuration

Stored in `signal_config`, editable from the dashboard (`/api/signal_config`), allow-listed server-side:

| Key | Default | Effect |
|-----|---------|--------|
| `liq_proximity_pct` | 3.0 | Criterion 6 distance threshold (UI range up to 50 %) |
| `oi_consecutive_cycles` | 2 | Criterion 4 required consecutive matching snapshots |
| `telegram_enabled` | true | Master switch for outbound alerts |
| `tp_achievability_in_msg` | true | Whether alerts include the TP achievability block |

---

## 6. The Pre-Trade Checklist & Take-Profit Methodology

The Checklist tab converts a market read into a written plan. Its central idea is that a take-profit target should be **derived from market structure, then separately tested against the trader's own intention** — and the two answers compared rather than blended.

### 6.1 Direction and entry

Direction (long/short) is selected per coin and persisted in `checklist_state`. Every calculation below mirrors symmetrically: for a short, "above" becomes "below", targets sit under the entry, and the stop sits above it.

Entry price defaults to the live Layer 3 price and can be overridden manually.

### 6.2 Scenario 1 — What the market is offering

Three target levels are derived from ATR and liquidation-cluster geometry. In the plain-English AI voice these are explained through a **fish-market analogy** — the small fish always in stock, the medium fish probably in stock today, the big fish only on a good day.

| Level | Derivation (long side) | Meaning |
|-------|------------------------|---------|
| **Minimum** | `entry × (1 + ATR%)` — 1× ATR | One normal candle's range. Nearly always reachable. |
| **Target** | Nearest cluster above `× 0.995`, or `entry × (1 + 2.5 × ATR%)` if no cluster entered | The realistic objective. Liquidation clusters are where forced liquidity sits, so price is *magnetically* drawn toward them; the 0.5 % haircut books profit just in front of the crowd. |
| **Stretch** | Second cluster above `× 0.995`, or `entry × (1 + 4 × ATR%)` | Strong-trend objective. Possible, not expected. |

When no cluster is entered, the card explicitly says so and prompts the user to add one for a more accurate target — the ATR-derived fallback is labelled as an estimate rather than presented as equivalent.

### 6.3 Scenario 2 — Is *your* target achievable?

The trader types the percentage they *want*. Three independent checks run:

| Check | Green | Yellow | Red |
|-------|-------|--------|-----|
| **Cluster** | Target sits within the nearest cluster distance — there is a liquidity pool to fuel the move | No cluster at that level | — |
| **ATR multiple** | ≤ 2× ATR — comfortably achievable | 2–4× — possible, needs a strong move | > 4× — very unlikely without a major catalyst |
| **Momentum** | Layer 3 verdict supports the direction | Layer 3 NEUTRAL | Layer 3 opposes the direction |

| Greens | Verdict |
|--------|---------|
| 3 | ✅ **Achievable** — cluster, volatility and momentum all support this target |
| 2 | 🟡 **Possible but not ideal** |
| 1 | ⚠️ **A stretch** — consider the Market Target instead |
| 0 | ❌ **Unlikely** — the market is offering *X %*; consider accepting that instead |

The zero-green message names the Scenario 1 Target explicitly. The system's opinion is not "no" — it is "here is the number the data supports".

### 6.4 Agreement analysis

The two scenarios are then compared, which is the step that makes the design worth having:

```
diff = desired_pct − |scenario1_target_pct|

|diff| ≤ 0.5 pts          → ✅ ALIGNED — your target matches what the market is offering
diff < −0.5               → 🟡 CONSERVATIVE — safe, but possibly leaving profit on the table
diff > 0.5, greens ≥ 2    → 🟡 AMBITIOUS BUT SUPPORTED — above average, but the data backs it
diff > 0.5, greens < 2    → 🟠 TARGET LIKELY TOO HIGH — consider scaling back expectations
```

Note the symmetry: the system flags a target that is too **conservative** as readily as one that is too greedy. Under-targeting is treated as an error, not as prudence.

### 6.5 Confirmed TP, stop, size, and risk/reward

The trader commits to one number — from Scenario 1 (one click per level), from Scenario 2, or typed manually. The source is recorded (`Market Target (Scenario 1)`, `Your 4.20% Target (Scenario 2)`, or `Manual entry`) and travels with the plan into the AI context and the database.

```
risk %   = (entry − stop) / entry × 100          (long; inverted for short)
reward % = (tp − entry)   / entry × 100          (long; inverted for short)
R:R      = 1 : reward / risk
```

With position size entered, risk and reward are also shown in **dollars**, which is the number that actually governs behaviour under stress.

### 6.6 The five checks

A progress bar tracks five gates:

1. **Signal alignment acknowledged** — an explicit checkbox. When the Master Summary reads `WAIT`, the label changes to a warning and the row highlights: *"⚠️ All layers show WAIT — no alignment detected. I understand this is a low-confidence setup and choose to proceed anyway."* The system never blocks the trade; it makes proceeding a deliberate, recorded act.
2. **Entry price set** (> 0)
3. **Take profit set** — and on the correct side of entry for the chosen direction
4. **Stop loss set** — and on the correct side of entry
5. **Position size set** (> 0)

Checks 3 and 4 are validated for *direction coherence*, not merely for presence: a "take profit" below entry on a long is rejected as an input error.

The plan is debounced (600 ms) and synced to the server via `/api/checklist/tp-plan`, so the background evaluator can read the trader's confirmed TP when composing Telegram alerts — this is how §11's achievability block knows what target to test.

---

## 7. The AI Analysis Layer

**Route:** `POST /api/ai/analysis` · **Model:** `claude-sonnet-4-6` · **Max tokens:** 1300

The route serialises the full dashboard state into a structured plain-text brief: current price, master verdict, every Layer 1 indicator with value and label, Layer 2 positioning with liquidation clusters, Layer 3 with ATR and its DCA implication, the complete take-profit plan (both scenarios, all checks, the confirmed TP and its source, and the agreement text), and — when present — DCA model levels.

### 7.1 Two voices

| Style | Section headers |
|-------|-----------------|
| **Professional** | `WHAT THE MARKET IS DOING` → `THE KEY TENSION` → `PROFESSIONAL ASSESSMENT` → `SUGGESTION` (LONG / SHORT / WAIT) |
| **Plain English** | `WHAT'S HAPPENING RIGHT NOW` → `WHAT'S PULLING IN DIFFERENT DIRECTIONS` → `WHAT AN EXPERIENCED TRADER WOULD THINK` → `IS YOUR PLAN GOOD?` (BUY / SELL / WAIT) |

Both prompts impose the same discipline: reference the specific numbers in front of you; no vague statements; state conviction level explicitly; and if the verdict is WAIT, name exactly what must change and which signal to watch. Both are required to end with the identical disclaimer line:

> ⚠️ This is analytical context to support your own decision — not financial advice. You make the final call.

**"The Key Tension" is the structurally important section.** The system is built so layers can disagree, and the prompt forces the model to surface the disagreement rather than average it away.

### 7.2 Conditional addenda

Two addenda are appended to the system prompt only when the corresponding data is present:

- **DCA addendum** — comment on whether step placement makes sense given current ATR and clusters; flag any step sitting above a cluster; reference specific dollar levels, never vague descriptions.
- **TP addendum** — adds a fifth section, `PLAN VALIDATION`, comparing the trader's confirmed TP against the market's target: aligned within 0.5 % (confirm they are reading the market correctly), higher (greed or momentum-justified stretch — reference the ATR multiple and L3 verdict), or lower (appropriately conservative or leaving profit behind). In the plain voice, the same analysis is rendered through the fish-market analogy with an explicit availability verdict.

### 7.3 Client-side rendering

Responses are parsed by a hand-rolled Markdown renderer (`aiMd` / `renderAiResponse`) that maps the known `## HEADERS` into styled sections. All content is HTML-escaped before insertion.

---

## 8. Data Layer

**Engine:** SQLite, WAL journal mode · **File:** `data/infinity.db` (git-ignored) · **Module:** `core/db.py` (single point of DB access)

### 8.1 Schema

| Table | Key | Purpose |
|-------|-----|---------|
| `layer1_manual` | `field_key` | Layer 1 manual cards (global). Values stored as JSON blobs. |
| `liquidation_clusters` | `symbol` | Cluster above / below price, per coin. Feeds criterion 6 and the TP methodology. |
| `liq24h_manual` | `symbol` | 24 h long/short liquidation totals. |
| `exchange_flow_manual` | `symbol` | Net flow direction, size, notes. |
| `whale_orders` | `id` | Whale order watchlist — price, size, direction, status, notes. |
| `checklist_state` | `symbol` | Direction, TP %, entry, stop, position size. |
| `position_ratio_manual` | `symbol` | Manual fallback for the position ratio. |
| `signal_history` | auto id | Layer 1/2/3 periodic snapshots, indexed `(symbol, layer, recorded_at)`. |
| `signal_state` | `symbol` | Tier, direction, both counts, criteria detail JSON, timestamps, last notified tier. |
| `signal_config` | `config_key` | Signal system configuration, JSON values, merged over defaults on read. |

Every table carries `updated_at` in ISO-8601 UTC (`%Y-%m-%dT%H:%M:%SZ`). Timestamps are the basis of every staleness badge in the UI.

### 8.2 Concurrency and safety

- **WAL mode** allows the scheduler thread to write while request threads read.
- **Connection per operation** via the `_conn()` context manager — commit on success, rollback on exception, close in `finally`. No shared connection, no cross-thread cursor.
- **Idempotent writes** — every write is an `INSERT … ON CONFLICT DO UPDATE` upsert (or `INSERT OR IGNORE` for whale orders), so a retried or duplicated call cannot corrupt state.
- **Config reads merge over defaults**, so a new config key ships with a working default without a migration.
- **JSON decode failures are swallowed per row**, not per query — one malformed snapshot cannot take out a whole history read.

### 8.3 Migration from flat files

Earlier versions persisted to flat JSON (`signal_history.json`, `signal_state.json`, `signal_config.json`, `checklist_tp.json`). `_migrate_flat_files_to_db()` runs once at boot, before the recorder and evaluator are constructed: each file, if present and not already migrated, is read into its table and then renamed, so the migration is self-disarming and safe to run on every start.

### 8.4 Retention

`signal_history` is pruned to **90 days** (`SignalRecorder.RETENTION_DAYS`), executed as part of the Layer 1 recording job — cleanup rides the lowest-frequency job rather than running on its own timer.

---

## 9. Scheduling, Caching & Refresh Cadence

### 9.1 Cache TTLs

| Layer | TTL | Rationale |
|-------|-----|-----------|
| Layer 1 | 900 s (15 min) | Macro moves in days. FRED is daily/monthly data. |
| Layer 2 | 300 s (5 min) | Binance refreshes funding/OI/long-short hourly; 5 min is already oversampling. |
| Market Mechanics | 300 s (5 min) | **Deliberately matched to Layer 2** so recorded snapshots pair correctly. |
| Layer 3 | 120 s (2 min) | Entry timing decays fastest; order book is near-real-time. |

Caches are per-symbol dicts of `{"data": …, "ts": …}` checked against `time.time()` on every read.

### 9.2 Background jobs

| Job | Schedule | Work |
|-----|----------|------|
| `record_l1` | Every 6 h | Snapshot Layer 1 → `signal_history`; prune records older than 90 days |
| `record_l2` | Every 1 h | Per symbol: snapshot Layer 2 + Mechanics + Master Summary → then **evaluate tier and notify** |
| `record_l3` | Cron, 00:00 UTC | Per symbol: snapshot Layer 3 — one daily structural datapoint |
| `check_levels` | Every 5 min | Per watched coin: compare live price against its levels, alert on approach (§12) |

The recording cadence is matched to the **information rate of the underlying data**, not to what the API would tolerate. Layer 3 records once daily because an order-book snapshot from six hours ago is noise in a history table — it is useful live and worthless historically.

Tier evaluation is **piggybacked on the Layer 2 job** rather than run on its own timer, which guarantees the evaluator always sees the freshest positioning data and that criterion 4's consecutive-cycle counting aligns exactly with the snapshot cadence it reads.

### 9.3 Client refresh

| What | Interval |
|------|----------|
| Layer 2 | 5 min |
| Layer 1 | 15 min |
| Layer 3 | 2 min |
| Signal History | 5 min |
| Signal State | 5 min |
| Layer 1 "last updated" label | 1 min |

Client intervals mirror server TTLs so a poll generally lands on fresh data rather than re-serving a cached response.

---

## 10. HTTP API Surface

### Pages
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | The dashboard SPA |

### Live signals
| Route | Method | Description |
|-------|--------|-------------|
| `/api/layer1` | GET | Macro environment (global), 7 indicators + verdict |
| `/api/layer2/<symbol>` | GET | Positioning: funding, OI, long/short, position ratio + verdict |
| `/api/layer3/<symbol>` | GET | Timing: volume, structure, momentum, order book, ATR + verdict + price |
| `/api/market_mechanics/<symbol>` | GET | Taker ratio, spot/futures volume ratio |

### History, state and configuration
| Route | Method | Description |
|-------|--------|-------------|
| `/api/signal_history/<symbol>` | GET | `?days=N` (clamped 1–21), all three layers |
| `/api/signal_state` | GET | Current tier state for all symbols |
| `/api/signal_state/evaluate` | POST | Force immediate evaluation of all symbols |
| `/api/signal_config` | GET / POST | Read / update allow-listed config keys |

### Checklist
| Route | Method | Description |
|-------|--------|-------------|
| `/api/checklist/<symbol>` | GET / POST | Per-coin checklist state |
| `/api/checklist` | GET | All coins |
| `/api/checklist/tp` | GET / POST | Saved TP state per coin |
| `/api/checklist/tp-plan` | POST | Full pre-trade plan; extracts and persists confirmed TP % |

### Manual data
| Route | Method | Description |
|-------|--------|-------------|
| `/api/manual/layer1` | GET / POST | Layer 1 manual fields (global) |
| `/api/manual/liquidation-clusters[/<symbol>]` | GET / POST | Clusters above/below price |
| `/api/manual/liq24h[/<symbol>]` | GET / POST | 24 h liquidation totals |
| `/api/manual/exchange-flow[/<symbol>]` | GET / POST | Exchange net flow |
| `/api/manual/whale-orders[/<symbol>[/<id>]]` | GET / POST / PUT / DELETE | Whale order watchlist |
| `/api/manual/position-ratio[/<symbol>]` | GET / POST | Position ratio manual fallback |

### Level alerts
| Route | Method | Description |
|-------|--------|-------------|
| `/api/levels` | GET | All watched coins with their levels and alert band |
| `/api/levels/<coin>` | GET / POST / DELETE | Read / replace / stop watching one coin's levels |
| `/api/levels/test-telegram` | POST | Send a test Telegram message |

### AI, settings and operations
| Route | Method | Description |
|-------|--------|-------------|
| `/api/ai/analysis` | POST | Generate narrative analysis via Claude |
| `/api/settings` | GET / POST | Read masked / set Anthropic API key (persists to `.env`, applies live) |
| `/deploy` | POST | Deploy webhook — requires matching `X-Deploy-Token` header |

---

## 11. Telegram Alerting

Alerts are sent to every chat ID in `TELEGRAM_CHAT_ID` (comma-separated list supported), Markdown-formatted, 10 s timeout, with per-chat failure isolation — one bad chat ID does not suppress the others.

### 11.1 Message anatomy

```
🔴 *BTC — STRONG SHORT SIGNAL*

*Criteria confirmed:*
  ✅ Master Summary
  ✅ L2 Crowd Positioning
  ✅ Position Ratio Divergence
  ✅ OI Trend
  ❌ Market Mechanics
  ✅ Liquidation Proximity

*TP Achievability* (saved target: -3.5%)
  L3 supports direction: ✅
  ATR multiple: 1.8× ✅
  Within cluster: ✅
  *Rating: ACHIEVABLE*

_📉 Open dashboard to review full breakdown_
```

All six criteria are always listed — the ones that **did not** fire are as informative as the ones that did, and hiding them would turn a 5-of-6 and a 6-of-6 into the same message.

### 11.2 TP achievability in alerts

When the trader has a saved TP for that coin, the alert runs `_tp_achievability_check()` — the server-side mirror of the Scenario 2 logic from §6.3 — against live Layer 3 and cluster data:

| Greens | Rating |
|--------|--------|
| 3 | ACHIEVABLE |
| 2 | POSSIBLE BUT NOT IDEAL |
| 1 | A STRETCH |
| 0 | UNLIKELY |

When the rating is "a stretch" or "unlikely" and cluster data exists, the message appends a **cluster-based realistic target** — it does not just reject the plan, it proposes the number the data supports. If a `STRONG` signal fires with no TP saved, the message prompts the trader to open the Checklist tab and set one.

---

## 12. Resistance / Support Level Alerts

**Purpose: tell the trader when price is approaching a level they care about, without them having to watch a chart.**

This feature is deliberately independent of the Layer 1/2/3 framework. It does not read a verdict, does not contribute to the Master Summary, and does not participate in tier evaluation. Its only shared dependency is the price reader described in §12.3. A trader marks the levels that matter — prior highs, breakdown points, range boundaries, whatever their own analysis produced — and the system watches them continuously.

**Modules:** `core/level_watcher.py` (state, evaluation, alert composition), `core/telegram_notifier.py` (delivery).
**Storage:** `data/resistance_levels.json`.

### 12.1 Why its coin list is separate

The dashboard's Layer 1/2/3 framework tracks five coins (`MS_SYMBOLS`), because each one costs several API calls and a full indicator computation per refresh. Level watching costs one price lookup per coin, so it scales to a much wider list cheaply.

The watched set is therefore **whatever is present in `resistance_levels.json`** — there is no hardcoded symbol list anywhere in the feature. Adding a coin through the UI adds it to the scheduler's loop on the next tick; deleting it removes it. The intended working size is roughly 20 coins, and nothing in the design caps it there.

### 12.2 Storage shape

```json
{
  "BTC": {
    "levels": [123238.74, 119805.78, 76321.68],
    "alert_pct": 3.0,
    "notified": {
      "76321.68": {"side": "below", "last_notified": "2026-09-17T08:00:00Z"}
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `levels` | Price levels to watch. Stored de-duplicated and sorted descending. |
| `alert_pct` | Per-coin alert band. Absent → `LEVEL_ALERT_DEFAULT_PCT` (default 3.0). |
| `notified` | Per-level notification state, keyed by a normalised level string. Drives cooldown and re-entry. |

Writes are **atomic** (temp file in the same directory, then `os.replace`) so a crash mid-write cannot truncate the watch list, and the whole read-modify-write cycle is guarded by a module-level lock because the scheduler thread and Flask request threads both mutate the file. A missing or corrupt file is treated as an empty watch list rather than an exception — the scheduler job must not die because a file was hand-edited badly.

This is the one part of the system that is **not** in SQLite. See §15.9.

### 12.3 Price lookup

`web/app.py::_current_price(symbol)` is injected into `check_levels()` rather than imported by it — `app.py` imports `core.*`, so a reverse import would be circular. It resolves a price in two steps:

1. **Layer 3 cache** — if a fresh entry exists for the symbol (within the 120 s Layer 3 TTL), its price is reused. For the five dashboard coins this is the common case and costs no request.
2. **Spot ticker fallback** — `GET /api/v3/ticker/price`, a single lightweight call. Watch-list coins outside the dashboard five have no Layer 3 entry, and running the full Layer 3 computation (klines + order book + five indicators) for ~20 coins every five minutes would be wasteful by orders of magnitude.

### 12.4 Evaluation

For each level on each watched coin:

```
gap_pct = |price − level| / level × 100
side    = "above" if price >= level else "below"
```

If `gap_pct > alert_pct` the price is outside the band: any stored notification for that level is **dropped**, and nothing is sent. If `gap_pct <= alert_pct`, an alert is sent unless suppressed by the rules in §12.5.

`side` is part of the alert identity, not decoration. A level approached from below (resistance) and later from above (the same level acting as support) are two different events, and each alerts once.

### 12.5 Re-notification rules

Two independent rules gate every alert:

| Rule | Behaviour |
|------|-----------|
| **Cooldown** | The same `(coin, level, side)` is not re-alerted within `NOTIFY_COOLDOWN_HOURS` (4 h, a module constant). Prevents a price hovering inside the band from alerting every five minutes. |
| **Band re-entry** | When price leaves the band, the stored notification is cleared. A genuine re-approach therefore alerts **immediately**, without waiting out the cooldown. |

Together these distinguish *still near the level* (quiet) from *came back to the level* (alert), which a cooldown alone cannot do.

Two further behaviours protect the state:

- **Failed delivery is not recorded.** If Telegram is unconfigured or the send fails, `notified` is left untouched, so the next cycle retries rather than silently swallowing the alert.
- **Editing levels preserves state.** Re-saving a coin keeps the `notified` entries for levels that survive the edit, so adding one level to a list does not re-fire alerts for the others. Removed levels have their state dropped.

### 12.6 Alert format

```
🔔 BTC approaching resistance/support
Level: 100,000
Current: 98,000 (2.00% away, from below)
```

Sent as plain text — unlike the tiered-signal alerts, which use Markdown — so a level or coin containing Markdown-significant characters cannot break rendering.

### 12.7 Telegram delivery

`core/telegram_notifier.py` is now the single implementation of Telegram delivery for the whole system; `web/app.py::_telegram_send()` delegates to it, so tiered-signal alerts (§11) and level alerts cannot drift apart. Behaviour is unchanged from §11: multi-chat fan-out over a comma-separated `TELEGRAM_CHAT_ID`, per-chat failure isolation, 10 s timeout.

Missing configuration is **not** an error. Exactly as an unset `TWELVE_DATA_API_KEY` degrades Layer 1 to manual cards, an unset `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` logs a warning and no-ops — every other part of the dashboard runs normally.

### 12.8 Schedule

A dedicated APScheduler job, `check_levels`, runs every **5 minutes** — matching the Layer 2 / Market Mechanics cadence, and comfortably inside the 4-hour cooldown. It is a separate job function, not piggybacked on the signal-recording jobs, so a slow level check cannot delay a Layer 2 snapshot or vice versa.

Each coin's check is individually wrapped: one coin's failed price fetch logs and continues, leaving the rest of the list to be checked. This is the same error-isolation posture as the Layer 2/3 recording jobs.

### 12.9 API surface

| Route | Method | Description |
|-------|--------|-------------|
| `/api/levels` | GET | All watched coins with `levels`, `alert_pct`, `level_count` |
| `/api/levels/<coin>` | GET | One coin's settings; empty defaults when not yet watched |
| `/api/levels/<coin>` | POST | Replace levels and `alert_pct` — `{"levels": [...], "alert_pct": 3.0}` |
| `/api/levels/<coin>` | DELETE | Stop watching a coin (404 if it was not watched) |
| `/api/levels/test-telegram` | POST | Send a test message to verify bot token and chat id |

**Validation on POST:** `levels` must be a list of positive, finite numbers; `alert_pct` must fall between 0.1 and 20. Failures return HTTP 400 with a specific message (`"alert_pct must be between 0.1 and 20.0"`), which the UI surfaces verbatim. Coin identifiers are upper-cased, so `/api/levels/btc` and `/api/levels/BTC` address the same entry.

### 12.10 UI

A collapsible **🔔 LEVEL ALERTS** panel on the Market Signals tab, below the Signal Status card, using the existing `toggleLayer()` collapse pattern and persisting its open/closed state to `localStorage` like the other panels. It provides a coin input backed by a `<datalist>` of already-watched coins, an alert-% input, a textarea for levels (one per line), a save button, a list of watched coins with edit and remove actions, and a "Send test Telegram message" button.

Typing a coin that is already watched loads its saved settings into the form, so saving edits that entry rather than replacing it with a blank one. No framework and no build step — plain functions in the existing inline script, consistent with the rest of the dashboard.

### 12.11 Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | For alerts | Bot token from @BotFather. Unset → alerts no-op with a warning. |
| `TELEGRAM_CHAT_ID` | For alerts | One chat id or a comma-separated list. |
| `LEVEL_ALERT_DEFAULT_PCT` | No | Fallback alert band for coins with no `alert_pct`. Default 3.0. |

---

## 13. Deployment & Operations

### 13.1 Provisioning

`deploy/setup-vps.sh` is a one-time script: clone to `~/infinity`, create a venv, install requirements, copy `.env.example` → `.env` (and stop to tell the operator to fill in keys), then install and enable a systemd unit:

```ini
[Service]
Type=simple
WorkingDirectory=$DEPLOY_PATH
EnvironmentFile=$DEPLOY_PATH/.env
ExecStart=$DEPLOY_PATH/venv/bin/python web/app.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
```

`Restart=always` with a 10-second back-off means a crash — including an unhandled exception in a scheduler job — self-heals without intervention, and all output goes to the journal (`journalctl -u infinity-web -f`).

### 13.2 Deployment

Two paths, both landing on `git pull && pip install -r requirements.txt && systemctl restart infinity-web`:

1. **Cron pull** — the VPS polls and auto-deploys within ~60 seconds of a push to `master` (this is what the GitHub Actions workflow documents rather than performs).
2. **Webhook** — `POST /deploy` with a matching `X-Deploy-Token` header spawns the same sequence on a daemon thread and returns immediately.

The dashboard displays the deploy time, read from `git log -1 --format=%ci` at process start, so the running version is visible in the UI.

### 13.3 Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | For AI panel | Claude API. Settable from the UI; written to `.env` and applied to the live process without a restart. |
| `FRED_API_KEY` | Recommended | Fed Funds, 10Y yield, CPI. Free. Without it those three cards fall back to manual entry. |
| `TWELVE_DATA_API_KEY` | Recommended | DXY and VIX. Free tier. Without it, or when the plan does not include the symbol, both fall back to manual entry. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | For alerts | Alert delivery for both the Tiered Signal System (§5) and level alerts (§12). Chat ID accepts a comma-separated list. |
| `LEVEL_ALERT_DEFAULT_PCT` | No | Fallback alert band for level alerts with no per-coin `alert_pct`. Default 3.0. |
| `DASHBOARD_PORT` | No | Default 5050 |
| `DEPLOY_TOKEN` | For webhook | Shared secret for `POST /deploy` |
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` / `USE_TESTNET` | **No** | Present in `.env.example` from the predecessor DCA bot. **Unused by the current system** — every Binance call is public and unauthenticated. |

Missing-key handling is graceful throughout: a fetcher with no key returns `{"status": "no_key"}`, which renders the manual card with a "check now" link instead of an error. Twelve Data plan restrictions (HTTP 400/403/404) are specifically mapped to `no_key` rather than surfaced as failures, because "your free plan does not include this symbol" and "you have no key" call for the same user action.

---

## 14. Security Considerations

| Area | Current state |
|------|---------------|
| **Exchange credentials** | No trading API is used. No signed request is ever made. Compromise of this host cannot move funds. |
| **API keys at rest** | Stored in `.env` (git-ignored, `EnvironmentFile` for systemd). The settings endpoint returns only a **masked** key (first 6 + dots + last 4). |
| **Deploy webhook** | Token-gated via `X-Deploy-Token`; rejects with 401 when `DEPLOY_TOKEN` is unset, so an unconfigured deployment fails closed. |
| **CORS** | `CORS(app)` is applied with default settings — **all origins allowed**. Acceptable only because the dashboard is expected to sit behind a private network, VPN, or reverse-proxy auth. |
| **Authentication** | **There is none.** Anyone who can reach port 5050 can read all data, write all manual fields, trigger evaluations, and consume Anthropic API credit through `/api/ai/analysis`. Access control must be provided by the network layer. |
| **SQL injection** | All queries are parameterised; no string interpolation of user input into SQL. |
| **XSS** | AI output and all user-entered manual values are HTML-escaped (`esc` / `aiEsc`) before insertion. |
| **Outbound** | Only to documented public APIs, Anthropic, and Telegram. All calls carry explicit timeouts (8 s market data, 10 s Telegram) so a hung upstream cannot exhaust request threads. |

**Deployment recommendation:** terminate TLS and enforce authentication at a reverse proxy (nginx/Caddy with basic auth or an identity provider), bind the Flask process to `127.0.0.1`, and never expose port 5050 directly.

---

## 15. Limitations, Known Gaps & Honest Context

This section is deliberately specific. Every item below is verifiable in the current source.

### 15.1 The Layer 1 verdict is computed two different ways

The browser (`recalcL1Verdict`) scores the combined live + manual indicator set (up to 11) with a **≥6 bullish / ≥6 bearish** threshold. The server (`_layer1_verdict`) scores live indicators only (max 7) with **≥4 / ≥4**, and never sees manual entries at all. Consequences:

- The badge in the browser and the verdict stored in `signal_history` can disagree.
- **Manual Layer 1 cards do not influence Telegram alerts**, because the Tiered Signal System reads the server-side Master Summary, which is built from the server-side Layer 1 verdict.

This is a real divergence, not a rounding difference. Unifying the two is the highest-value correctness fix available.

### 15.2 The AI Analysis panel is currently broken

`collectAiPayload()` in `index.html` references `_dcaLevelsData`, `_dcaSelectedId`, `_dcaModels`, `_dcaSide` and `_dcaMultiplier` — leftovers from a removed DCA Visualizer panel. **None of these variables is declared anywhere in the file.** Reading an undeclared identifier throws a `ReferenceError`, which is caught by `generateAiAnalysis`'s `try/catch` and rendered as "⚠️ ANALYSIS FAILED". The server route and both prompt sets are intact and correct; the panel fails before the request is ever sent. A one-line guard (or five `let` declarations) restores it.

### 15.3 `STRONG` alerts repeat hourly

`should_notify` returns true for **every** evaluation while the tier is `STRONG`, not only on entry. A confluence that persists for eight hours produces eight identical messages. `DEVELOPING` is correctly edge-triggered. Whether this is a feature or a nuisance depends on the operator; there is currently no cooldown to tune it.

### 15.4 Signal-quality caveats by indicator

- **Order-book imbalance** (Layer 3) reads only the top 20 levels of the **spot** book and is the most easily spoofed input in the system. It is one vote of four precisely for this reason.
- **BTC Dominance 24 h change** is computed from an **in-memory rolling window**, not a historical API. It is empty at boot, so the change reads 0 and the signal reads Neutral until the process has been running for roughly 24 hours. A restart resets it.
- **Volume divergence** evaluates a **partially formed** current 4 h candle against 20 completed ones, so early in a candle the ratio is structurally understated.
- **Layer 3 uses spot klines and the spot order book** while Layer 2 uses futures data. For XAUT in particular, futures liquidity differs materially from spot.
- **CPI and Fed Funds are monthly series.** A "live" macro card can legitimately be weeks old — this is a property of the data, not a bug, but it means Layer 1 cannot be timely by construction.

### 15.5 Manual inputs have no validation beyond type

Liquidation clusters, FedWatch probabilities, M2 growth and the rest are accepted as entered. A cluster typed with a misplaced decimal point silently corrupts criterion 6, the Scenario 1 Target, and the Scenario 2 cluster check simultaneously. Staleness is surfaced; plausibility is not checked.

### 15.6 Orphaned and stale artefacts

| Artefact | Status |
|----------|--------|
| `web/templates/backtest.html` (3,264 lines) | **No route serves it.** No `/api/backtest/*` endpoint exists. Dead in the current build. |
| `web/static/app.js`, `web/static/style.css` | **Not referenced** by `index.html`, which is fully self-contained. `app.js` calls `/api/set_reference`, which does not exist. Legacy from the DCA bot. |
| `CLAUDE.md` | Instructs keeping `signal_lab/signal_fn.py` in sync with `core/regime_live.py` and `core/mixed_engine.py`. **None of those files or the `signal_lab/` package exists** in this repository. |
| `README.md` | Documents the predecessor **DCA trading bot** (`main.py`, `config/coins.json`, `core/dca_engine.py`) — an entirely different system from what is deployed. |
| `dynamic_spot_dca_system_spec.md` | Historical spec for that same predecessor. Useful as provenance; not a description of this system. |
| `.env.example` Binance keys | Unused (§13.3). |

### 15.7 Infrastructure

- **Flask's development server** (`app.run`) is the production server. It is single-process and not hardened for public exposure. A WSGI server (gunicorn/uWSGI) behind nginx is the correct production setup — though note that the in-memory caches, the BTC-dominance rolling window, and the APScheduler jobs all assume **one process**, so moving to multi-worker gunicorn requires moving the scheduler out of the app process first.
- **No automated tests.** There is no test suite, and CI does not run one. The scoring functions (`_layer2_verdict`, `_layer3_verdict_calc`, `_layer1_verdict`, `_evaluate_direction`, `compute_position_account_divergence`) are pure and would be straightforward to cover.
- **Single point of failure.** One VPS, one process, one SQLite file. `data/` is git-ignored, so **the database is not backed up by the deploy mechanism.**

### 15.9 Level alerts store state outside the database

`data/resistance_levels.json` is the only persistent state in the system that does not live in `data/infinity.db`. Everything else — signal history, tier state, checklist state, every manual input — was migrated out of flat JSON files into SQLite (§8.3).

The level watcher works correctly as built: writes are atomic, the read-modify-write cycle is lock-guarded, and a corrupt file degrades to an empty watch list instead of crashing the scheduler. But it re-introduces the pattern the rest of the codebase moved away from, which means two storage mechanisms to back up, reason about and migrate. Consolidating it into a `resistance_levels` table would remove that split; the module's read/write functions are already isolated behind `_read_file()` / `_write_file()`, so the change is contained.

### 15.8 What this system is not

It does not execute trades, size positions automatically, manage open risk, track realised P&L, or backtest. It has no model of your portfolio. Every number it produces is an input to a human decision made somewhere else, and its own AI output is required to say so on every response.

---

## 16. Roadmap

Ordered by value-to-effort as the code stands:

| Priority | Item | Notes |
|----------|------|-------|
| **P0** | Fix the AI panel `ReferenceError` (§15.2) | Single-line guard; restores a headline feature |
| **P0** | Unify the two Layer 1 verdict calculations (§15.1) | Makes alerts and display agree; lets manual macro reach the evaluator |
| **P0** | Database backup | `data/infinity.db` is currently unprotected |
| **P1** | Authentication + bind to localhost behind a proxy (§14) | The single largest exposure |
| **P1** | Unit tests for the pure scoring functions | They are already side-effect-free |
| **P1** | Alert cooldown / re-notify interval for `STRONG` (§15.3) | Make repetition configurable |
| **P2** | Remove or wire up orphaned artefacts; rewrite `README.md` and `CLAUDE.md` (§15.6) | Documentation currently describes a system that no longer exists |
| **P2** | Signal outcome tracking | Record what price did after each `STRONG` signal — the prerequisite for ever knowing whether the six criteria work |
| **P2** | Backtesting over `signal_history` | 90 days of snapshots already accumulate; `backtest.html` suggests this was started |
| **P3** | Liquidation cluster API integration | Removes the largest manual dependency (criterion 6, Scenario 1 Target, Scenario 2 cluster check) |
| **P3** | WSGI server + scheduler extraction | Prerequisite for horizontal scaling |
| **P3** | Per-coin threshold tuning | Every threshold is currently global; ZEC and XAUT do not behave like BTC |

---

## Appendix A — Formula Reference

**Taker buy percentage**
```
buy_pct = buySellRatio / (1 + buySellRatio) × 100
```

**Spot/futures volume ratio**
```
ratio = futures_24h_quote_volume / spot_24h_quote_volume
```

**Position/account divergence**
```
gap = position_long_pct − account_long_pct
significance = minimal (|gap| < 5) | meaningful (< 10) | significant (≥ 10)
```

**Top-trader vs retail divergence**
```
divergence ⟺ |global_long − top_long| > 10  AND  (global_long − 50)(top_long − 50) < 0
```

**Rate of Change (4 h closes)**
```
ROC6  = (close[-1] − close[-7]) / close[-7] × 100
ROC14 = (close[-1] − close[-15]) / close[-15] × 100
accelerating ⟺ |ROC6| > |ROC14 / 2|
```

**ATR(14) on 4 h candles**
```
TR      = max(high − low, |high − prev_close|, |low − prev_close|)
ATR     = mean(last 14 TR)
ATR %   = ATR / current_price × 100
```

**Order-book imbalance (top 20 levels, notional-weighted)**
```
bid_pct = Σ(bid_price × bid_qty) / [Σ(bid_price × bid_qty) + Σ(ask_price × ask_qty)] × 100
```

**Layer 3 composite score**
```
score = signal(volume) + signal(structure) + signal(momentum) + signal(order_book)     ∈ [−4, +4]
```

**Take-profit levels (long; invert for short)**
```
minimum = entry × (1 + ATR%/100)
target  = cluster_above × 0.995         or   entry × (1 + 2.5 × ATR%/100)
stretch = cluster2_above × 0.995        or   entry × (1 + 4.0 × ATR%/100)
```

**Risk / reward (long; invert for short)**
```
risk %   = (entry − stop) / entry × 100
reward % = (tp − entry) / entry × 100
R:R      = 1 : (reward % / risk %)
```

**Liquidation proximity**
```
distance_pct = |cluster − current_price| / current_price × 100
confirms ⟺ distance_pct ≤ liq_proximity_pct
```

**ATR multiple (target realism)**
```
atr_multiple = |target %| / ATR %
≤ 2 green   |   2–4 yellow   |   > 4 red
```

---

## Appendix B — Threshold Reference

| Domain | Threshold | Value |
|--------|-----------|-------|
| Funding | Crowded long / crowded short | > 0.05 % / < −0.01 % |
| Long/short | Crowding | > 65 % either side |
| Long/short | Balanced band | 45–55 % |
| Long/short | Divergence gap | > 10 pts **and** opposite sides of 50 |
| Position ratio | Significant divergence | ≥ 10 pts |
| Taker | Aggression (display) | > 60 % / < 40 % |
| Taker | Aggression (criterion 5) | > 55 % |
| Volume ratio | Spot dominant / futures dominant / extreme | < 3 / 8–15 / ≥ 15 |
| Volume divergence | High / low volume | > 1.2× / < 0.8× average |
| Price structure | Run length for structure | ≥ 2 consecutive |
| Momentum | Dead zone | \|ROC6\| < 0.5 % |
| Order book | Imbalance | > 60 % / < 40 % bid share |
| ATR | Volatility bands | < 1 % / 1–2 % / 2–4 % / > 4 % |
| Layer 1 | Minimum indicators for a verdict | 3 |
| Layer 1 | Server verdict threshold | ≥ 4 bullish or bearish |
| Layer 1 | Client verdict threshold | ≥ 6 bullish or bearish |
| Layer 3 | Verdict threshold | \|score\| ≥ 2 |
| Tiers | STRONG / DEVELOPING | ≥ 5 criteria / ≥ 3 criteria |
| Tiers | OI consecutive cycles | 2 (configurable) |
| Tiers | Liquidation proximity | 3 % (configurable to 50 %) |
| TP | ATR multiple bands | ≤ 2 / ≤ 4 / > 4 |
| TP | Cluster haircut | 0.5 % |
| TP | Scenario agreement tolerance | 0.5 pts |
| History | Retention | 90 days |
| History | API query cap | 21 days |

---

## Appendix C — Glossary

**ATR (Average True Range)** — mean of the true ranges of the last 14 candles; the system's unit of "one normal move".

**Cluster (liquidation)** — a price level where a large volume of leveraged positions would be force-closed. Acts as a magnet: forced liquidity attracts price.

**Funding rate** — periodic payment between perpetual futures longs and shorts that keeps the perp tethered to spot. Positive means longs pay shorts.

**Open interest (OI)** — total notional value of open futures contracts. Rising OI means new positions; falling OI means positions closing.

**Taker volume** — volume from orders that crossed the spread and executed immediately. A proxy for aggression and urgency.

**Position ratio vs account ratio** — dollar-weighted positioning versus headcount-weighted positioning. Their divergence exposes whether large or small accounts hold each side.

**Tier** — the Tiered Signal System's classification of current confluence: `NONE`, `DEVELOPING` (3–4 of 6 criteria), or `STRONG` (5–6 of 6, un-contradicted).

**Master Summary** — the six-state collapse of the three layer verdicts: `ALIGNED LONG`, `ALIGNED SHORT`, `DEVELOPING`, `MIXED`, `CAUTION`, `WAIT`.

**Scenario 1 / Scenario 2** — the two independent take-profit derivations: what the market structurally offers, and whether the trader's own target survives three checks. Their comparison is the agreement analysis.

---

## Disclaimer

Infinity is an analysis and decision-support tool. It does not execute trades, hold funds, or provide financial advice. Every verdict, tier, alert and AI narrative it produces is analytical context for a decision made by a human being who bears the full risk of that decision. Cryptocurrency trading involves substantial risk of loss. Past signal behaviour does not predict future results.
