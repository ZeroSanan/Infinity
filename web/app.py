"""
Market Analysis Dashboard — Flask Web Server
=============================================
Run with:  python3 web/app.py
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

from core.signal_recorder import SignalRecorder
from core.signal_evaluator import (
    SignalEvaluator, load_config as load_signal_config,
    save_config as save_signal_config, DATA_DIR as SIGNAL_DATA_DIR,
)
from core import db as _db

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ROOT     = os.path.join(os.path.dirname(__file__), "..")
ENV_PATH = os.path.join(ROOT, ".env")

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

def _get_deploy_time() -> str:
    try:
        import subprocess
        ts = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"],
            cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
        return ts
    except Exception:
        import datetime
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S +0000")

_DEPLOY_TIME = _get_deploy_time()

# ── In-memory caches ─────────────────────────────────────────────────────────
_layer2_cache: dict = {}   # symbol -> {"data": dict, "ts": float}
_LAYER2_TTL = 300  # seconds (5 min)

_layer3_cache: dict = {}   # symbol -> {"data": dict, "ts": float}
_LAYER3_TTL = 120  # seconds (2 min)

_layer1_cache: dict = {}   # {"data": dict, "ts": float}
_LAYER1_TTL = 900  # seconds (15 min)
_btc_dom_history: list = []  # [(timestamp, btc_dominance_pct), ...] rolling 48h window


def _verdict_code(cache_entry) -> str:
    if not cache_entry:
        return "UNKNOWN"
    data = cache_entry.get("data") if isinstance(cache_entry, dict) and "data" in cache_entry else cache_entry
    verdict = (data or {}).get("verdict")
    if isinstance(verdict, dict):
        return verdict.get("code", "UNKNOWN")
    return verdict or "UNKNOWN"


def _signal_snapshot(symbol: str) -> dict:
    l1 = _verdict_code(_layer1_cache if _layer1_cache.get("data") else None)
    l2 = _verdict_code(_layer2_cache.get(symbol))
    l3 = _verdict_code(_layer3_cache.get(symbol))

    l1_bull, l1_bear = l1 == "FAVORABLE", l1 == "UNFAVORABLE"
    l2_bear, l2_bull = l2 == "CAUTION_LONG", l2 in ("NEUTRAL", "CAUTION_SHORT")
    l3_bull = l3 in ("LONG", "WEAK_LONG")
    l3_bear = l3 in ("SHORT", "WEAK_SHORT")

    if l1_bull and l2_bull and l3_bull:
        master = "ALIGNED LONG"
    elif l1_bear and l2_bear and l3_bear:
        master = "ALIGNED SHORT"
    elif l1_bull and l3_bull and not l2_bear:
        master = "DEVELOPING"
    elif l1 in ("MIXED", "INSUFFICIENT_DATA"):
        master = "MIXED"
    elif l2_bear or l3_bear:
        master = "CAUTION"
    else:
        master = "WAIT"

    return {"l1_verdict": l1, "l2_verdict": l2, "l3_verdict": l3, "master": master}


@app.route("/")
def index():
    return render_template("index.html")



# ── Routes — Layer 2 (Market Positioning) ─────────────────────────────────────

_BINANCE_FAPI = "https://fapi.binance.com"


def _layer2_funding(symbol: str) -> dict:
    """Current funding rate, sparkline history, and next-funding countdown."""
    import requests
    from datetime import datetime, timedelta

    data = requests.get(
        f"{_BINANCE_FAPI}/fapi/v1/fundingRate",
        params={"symbol": symbol, "limit": 90}, timeout=8,
    ).json()
    if not isinstance(data, list) or not data:
        raise ValueError("no funding rate data")

    rates = [float(d["fundingRate"]) for d in data]
    current = rates[-1]

    if current > 0.0005:
        label, color = "HIGH — Longs Crowded", "red"
    elif current >= 0.0001:
        label, color = "Elevated", "yellow"
    elif current >= -0.0001:
        label, color = "Neutral", "green"
    else:
        label, color = "Negative — Shorts Crowded", "blue"

    # Funding settles every 8h at 00:00 / 08:00 / 16:00 UTC
    now_utc = datetime.utcnow()
    boundary_hour = ((now_utc.hour // 8) + 1) * 8
    next_funding = (now_utc + timedelta(days=boundary_hour // 24)).replace(
        hour=boundary_hour % 24, minute=0, second=0, microsecond=0)
    seconds_until = (next_funding - now_utc).total_seconds()

    return {
        "current": current,
        "current_pct": current * 100,
        "history": rates[-30:],
        "next_funding_time": next_funding.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds_until_next": int(seconds_until),
        "label": label,
        "color": color,
    }


def _layer2_open_interest(symbol: str) -> dict:
    """Current open interest (USD), 24h change, and price/OI direction combo."""
    import requests

    oi_data = requests.get(
        f"{_BINANCE_FAPI}/futures/data/openInterestHist",
        params={"symbol": symbol, "contractType": "PERPETUAL", "period": "1h", "limit": 48},
        timeout=8,
    ).json()
    if not isinstance(oi_data, list) or not oi_data:
        raise ValueError("no open interest data")

    oi_values = [float(d["sumOpenInterestValue"]) for d in oi_data]
    current = oi_values[-1]
    prev = oi_values[-25] if len(oi_values) >= 25 else oi_values[0]
    change_pct = ((current - prev) / prev * 100) if prev else 0.0
    oi_direction = "up" if change_pct >= 0 else "down"

    klines = requests.get(
        f"{_BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1h", "limit": 48},
        timeout=8,
    ).json()
    prices = [float(k[4]) for k in klines] if isinstance(klines, list) else []
    if prices:
        price_prev = prices[-25] if len(prices) >= 25 else prices[0]
        price_direction = "up" if prices[-1] >= price_prev else "down"
    else:
        price_direction = None

    if price_direction == "up" and oi_direction == "up":
        label, color = "Strong — New Money Entering", "green"
    elif price_direction == "up" and oi_direction == "down":
        label, color = "Weak — Short Covering Only", "orange"
    elif price_direction == "down" and oi_direction == "up":
        label, color = "Strong Selling — New Shorts", "red"
    elif price_direction == "down" and oi_direction == "down":
        label, color = "Exhaustion — Longs Closing", "yellow"
    else:
        label, color = "Unavailable", "grey"

    return {
        "current_usd": current,
        "current_billions": current / 1e9,
        "change_24h_pct": change_pct,
        "direction": oi_direction,
        "price_direction": price_direction,
        "history": oi_values,
        "price_history": prices,
        "label": label,
        "color": color,
    }


def _layer2_long_short(symbol: str) -> dict:
    """Global retail and top-trader long/short account ratios."""
    import requests

    global_data = requests.get(
        f"{_BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
        params={"symbol": symbol, "period": "1h", "limit": 48}, timeout=8,
    ).json()
    top_data = requests.get(
        f"{_BINANCE_FAPI}/futures/data/topLongShortAccountRatio",
        params={"symbol": symbol, "period": "1h", "limit": 48}, timeout=8,
    ).json()
    if not isinstance(global_data, list) or not global_data:
        raise ValueError("no global long/short ratio data")
    if not isinstance(top_data, list) or not top_data:
        raise ValueError("no top trader long/short ratio data")

    g, t = global_data[-1], top_data[-1]
    global_long = float(g["longAccount"]) * 100
    global_short = float(g["shortAccount"]) * 100
    top_long = float(t["longAccount"]) * 100
    top_short = float(t["shortAccount"]) * 100

    def _crowd_label(long_pct, short_pct):
        if long_pct > 65:
            return "Longs Crowded", "red"
        if short_pct > 65:
            return "Shorts Crowded", "blue"
        if 45 <= long_pct <= 55:
            return "Balanced", "green"
        return "Neutral", "grey"

    global_label, global_color = _crowd_label(global_long, global_short)
    top_label, top_color = _crowd_label(top_long, top_short)

    divergence = False
    divergence_message = None
    if abs(global_long - top_long) > 10 and (global_long - 50) * (top_long - 50) < 0:
        divergence = True
        divergence_message = "Top traders positioned opposite to retail"

    return {
        "global": {"long_pct": global_long, "short_pct": global_short,
                    "label": global_label, "color": global_color},
        "top": {"long_pct": top_long, "short_pct": top_short,
                "label": top_label, "color": top_color},
        "divergence": divergence,
        "divergence_message": divergence_message,
    }


def _layer2_position_ratio(symbol: str) -> dict:
    """Top-trader long/short ratio by POSITION SIZE (dollar-weighted).

    Distinct from _layer2_long_short's "top" ratio, which is account
    headcount. Despite the response's field names (longAccount/shortAccount),
    this endpoint — Binance's "Top Trader Long Short Position Ratio" — reports
    the $ size of positions held, not the number of accounts holding them.
    """
    import requests

    data = requests.get(
        f"{_BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
        params={"symbol": symbol, "period": "1h", "limit": 48}, timeout=8,
    ).json()
    if not isinstance(data, list) or not data:
        raise ValueError("no position ratio data")

    latest = data[-1]
    return {
        "status": "ok",
        "long_pct": float(latest["longAccount"]) * 100,
        "short_pct": float(latest["shortAccount"]) * 100,
        "ratio": float(latest["longShortRatio"]),
    }


def compute_position_account_divergence(account_long_pct: float, position_long_pct: float) -> dict:
    """
    WHY POSITION RATIO DIVERGENCE IS SIGNIFICANT:

    Total long $ always equals total short $ in futures markets
    (every contract has two sides)

    Therefore: if 66% of TOP TRADER ACCOUNTS are long
    but only 55% of TOP TRADER POSITION VALUE is long
    it means: the LONG accounts are smaller (less capital per account)
              the SHORT accounts are larger (more capital per account)

    Translation: The few traders with BIG positions are actually
                 MORE SHORT than the headcount would suggest.
                 Real money (dollar-weighted) is leaning SHORT
                 even though more individual accounts are LONG.

    This is one of the most powerful divergence signals available
    because it reveals institutional positioning without them
    having to announce it publicly.

    The opposite (position_long > account_long by significant margin):
    = The big money accounts are carrying heavier long exposure
    = Despite similar headcount, they're committing more capital to longs
    = Structurally more bullish than the ratio alone shows
    """
    gap = position_long_pct - account_long_pct
    abs_gap = abs(gap)

    if abs_gap < 5:
        significance = "minimal"
    elif abs_gap < 10:
        significance = "meaningful"
    else:
        significance = "significant"

    direction = "longs_larger_than_headcount" if gap > 0 else "shorts_larger_than_headcount"

    return {
        "gap_pct": round(gap, 2),
        "abs_gap_pct": round(abs_gap, 2),
        "significance": significance,
        "direction": direction,
    }


def _position_ratio_note(position_ratio: dict) -> Optional[str]:
    """Plain-English summary of the position-vs-account divergence, shown
    below the Layer 2 verdict badge when the divergence is significant."""
    significance = position_ratio.get("significance")
    if significance is None:
        return None
    if significance != "significant":
        return "Position ratio aligned with account ratio"

    direction = position_ratio.get("divergence_direction")
    gap = abs(position_ratio.get("divergence_from_account") or 0)
    if direction == "shorts_larger_than_headcount":
        return (f"Position ratio shows big money {gap:.1f}pts more SHORT than headcount — "
                f"large capital leaning more bearish than account ratio alone suggests")
    return (f"Position ratio shows big money {gap:.1f}pts more LONG than headcount — "
            f"large capital leaning more bullish than account ratio alone suggests")


def _layer2_verdict(funding: Optional[dict], oi: Optional[dict], ls: Optional[dict],
                     position: Optional[dict] = None) -> dict:
    """Combine funding, open interest, long/short ratio, and (when available)
    the position-ratio divergence into one verdict."""
    funding_high = bool(funding and funding["current"] > 0.0005)
    funding_negative = bool(funding and funding["current"] < -0.0001)
    longs_crowded = bool(ls and ls["global"]["long_pct"] > 65)
    shorts_crowded = bool(ls and ls["global"]["short_pct"] > 65)

    if funding_high and longs_crowded:
        return {"code": "CAUTION_LONG", "label": "CAUTION LONG", "emoji": "🔴", "color": "red"}
    if funding_negative and shorts_crowded:
        return {"code": "CAUTION_SHORT", "label": "CAUTION SHORT", "emoji": "🔵", "color": "blue"}

    position_bullish = bool(position and position.get("significance") == "significant"
                             and position.get("divergence_direction") == "longs_larger_than_headcount")
    position_bearish = bool(position and position.get("significance") == "significant"
                             and position.get("divergence_direction") == "shorts_larger_than_headcount")

    bullish = sum([
        funding_negative,
        shorts_crowded,
        bool(oi and oi["label"].startswith("Strong —")),
        position_bullish,
    ])
    bearish = sum([
        funding_high,
        longs_crowded,
        bool(oi and oi["label"].startswith("Strong Selling")),
        position_bearish,
    ])

    if bullish and bearish:
        return {"code": "MIXED", "label": "MIXED", "emoji": "🟡", "color": "yellow"}
    return {"code": "NEUTRAL", "label": "NEUTRAL", "emoji": "🟢", "color": "green"}


def _get_layer2_data(symbol: str) -> dict:
    """Layer 2 (Market Positioning): funding rate, open interest, and
    long/short ratios from Binance public futures data, plus a combined
    verdict. Cached per symbol for _LAYER2_TTL seconds."""
    symbol = symbol.upper()
    now = time.time()
    cached = _layer2_cache.get(symbol)
    if cached and now - cached["ts"] < _LAYER2_TTL:
        return cached["data"]

    result: dict = {"symbol": symbol}

    try:
        result["funding"] = _layer2_funding(symbol)
    except Exception as exc:
        result["funding"] = {"error": str(exc)}

    try:
        result["open_interest"] = _layer2_open_interest(symbol)
    except Exception as exc:
        result["open_interest"] = {"error": str(exc)}

    try:
        result["long_short"] = _layer2_long_short(symbol)
    except Exception as exc:
        result["long_short"] = {"error": str(exc)}

    try:
        result["position_ratio"] = _layer2_position_ratio(symbol)
    except Exception as exc:
        result["position_ratio"] = {"status": "error", "long_pct": None, "short_pct": None, "error": str(exc)}

    position_ratio = result["position_ratio"]
    long_short = result["long_short"]
    if position_ratio.get("status") == "ok" and "error" not in long_short:
        divergence = compute_position_account_divergence(
            long_short["top"]["long_pct"], position_ratio["long_pct"])
        position_ratio["divergence_from_account"] = divergence["gap_pct"]
        position_ratio["divergence_direction"] = divergence["direction"]
        position_ratio["significance"] = divergence["significance"]
    else:
        position_ratio["divergence_from_account"] = None
        position_ratio["divergence_direction"] = None
        position_ratio["significance"] = None
    position_ratio["note"] = _position_ratio_note(position_ratio)

    result["verdict"] = _layer2_verdict(
        result["funding"] if "error" not in result["funding"] else None,
        result["open_interest"] if "error" not in result["open_interest"] else None,
        result["long_short"] if "error" not in result["long_short"] else None,
        position_ratio if position_ratio.get("status") == "ok" else None,
    )
    result["timestamp"] = int(now * 1000)

    _layer2_cache[symbol] = {"data": result, "ts": now}
    return result


@app.route("/api/layer2/<symbol>")
def api_layer2(symbol):
    return jsonify(_get_layer2_data(symbol))


# ── Routes — Market Mechanics ─────────────────────────────────────────────────
# Surfaces raw order-flow and exchange-activity indicators that answer HOW price
# is moving (not just WHERE it is):
#   - Taker buy/sell ratio: who is initiating moves (buyers vs sellers aggressive)
#   - Spot vs futures volume: real ownership vs leveraged speculation driving price

_BINANCE_SPOT = "https://api.binance.com"
_mm_cache: dict = {}
_MM_TTL = 300  # seconds — matches _LAYER2_TTL so recordings stay in sync


def _mm_taker_ratio(symbol: str) -> dict:
    """Taker buy/sell volume ratio (1h periods, last 48h).

    buySellRatio > 1 = more aggressive buy volume than sell volume.
    buy_pct = buySellRatio / (1 + buySellRatio) * 100.
    """
    import requests
    data = requests.get(
        f"{_BINANCE_FAPI}/futures/data/takerlongshortRatio",
        params={"symbol": symbol, "period": "1h", "limit": 48}, timeout=8,
    ).json()
    if not isinstance(data, list) or not data:
        raise ValueError("no taker ratio data")
    latest = data[-1]
    ratio = float(latest["buySellRatio"])
    buy_pct = ratio / (1 + ratio) * 100
    sell_pct = 100 - buy_pct
    # 24-point sparkline (raw ratio, last 24h)
    history = [float(d["buySellRatio"]) for d in data[-24:]]
    if buy_pct > 60:
        label, color = "Buyers Aggressive — Initiating Moves", "green"
    elif buy_pct < 40:
        label, color = "Sellers Aggressive — Initiating Moves", "red"
    else:
        label, color = "Balanced — No Clear Aggressor", "grey"
    return {
        "status": "ok",
        "buy_pct": round(buy_pct, 2),
        "sell_pct": round(sell_pct, 2),
        "ratio": round(ratio, 4),
        "label": label,
        "color": color,
        "history": history,
    }


def _mm_spot_volume(symbol: str) -> dict:
    """24h spot USD volume from Binance spot 24hr ticker."""
    import requests
    data = requests.get(
        f"{_BINANCE_SPOT}/api/v3/ticker/24hr",
        params={"symbol": symbol}, timeout=8,
    ).json()
    return {"status": "ok", "volume_usd": float(data["quoteVolume"])}


def _mm_futures_volume(symbol: str) -> dict:
    """24h perpetual futures USD volume from Binance futures 24hr ticker."""
    import requests
    data = requests.get(
        f"{_BINANCE_FAPI}/fapi/v1/ticker/24hr",
        params={"symbol": symbol}, timeout=8,
    ).json()
    return {"status": "ok", "volume_usd": float(data["quoteVolume"])}


def _get_market_mechanics_data(symbol: str) -> dict:
    """Market Mechanics: taker buy/sell ratio + spot/futures 24h volume.
    Cached per symbol for _MM_TTL seconds (same as Layer 2 to stay in sync)."""
    symbol = symbol.upper()
    now = time.time()
    cached = _mm_cache.get(symbol)
    if cached and now - cached["ts"] < _MM_TTL:
        return cached["data"]

    result: dict = {"symbol": symbol}

    try:
        result["taker"] = _mm_taker_ratio(symbol)
    except Exception as exc:
        result["taker"] = {"status": "error", "error": str(exc)}

    spot_usd, fut_usd = None, None
    try:
        sv = _mm_spot_volume(symbol)
        spot_usd = sv["volume_usd"]
        result["spot_volume"] = sv
    except Exception as exc:
        result["spot_volume"] = {"status": "error", "error": str(exc)}

    try:
        fv = _mm_futures_volume(symbol)
        fut_usd = fv["volume_usd"]
        result["futures_volume"] = fv
    except Exception as exc:
        result["futures_volume"] = {"status": "error", "error": str(exc)}

    if spot_usd and fut_usd and spot_usd > 0:
        ratio = round(fut_usd / spot_usd, 2)
        if ratio < 3:
            label, color = "Spot Dominant — Real Ownership Driving", "green"
        elif ratio < 8:
            label, color = "Balanced — Mixed Spot/Futures Activity", "grey"
        elif ratio < 15:
            label, color = "Futures Dominant — Leveraged Speculation Driving", "orange"
        else:
            label, color = "Extreme Futures Dominance — High Leverage Risk", "red"
        result["volume_ratio"] = {
            "status": "ok",
            "spot_usd": spot_usd,
            "futures_usd": fut_usd,
            "ratio": ratio,
            "label": label,
            "color": color,
        }
    else:
        result["volume_ratio"] = {"status": "error"}

    result["timestamp"] = int(now * 1000)
    _mm_cache[symbol] = {"data": result, "ts": now}
    return result


@app.route("/api/market_mechanics/<symbol>")
def api_market_mechanics(symbol):
    return jsonify(_get_market_mechanics_data(symbol))



# ── Routes — Layer 3 (Entry Timing) ────────────────────────────────────────────

def _layer3_klines(symbol: str, limit: int = 21) -> list:
    """Fetch 4h klines from Binance spot public API (no auth required)."""
    import requests
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval=4h&limit={limit}")
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    return [
        {
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        }
        for k in resp.json()
    ]


def _layer3_volume_divergence(klines: list) -> dict:
    """Volume vs 20-period average with price direction verdict."""
    if len(klines) < 21:
        return {"error": "insufficient data"}
    avg_vol  = sum(k["volume"] for k in klines[:20]) / 20
    curr     = klines[20]
    curr_vol = curr["volume"]
    ratio    = curr_vol / avg_vol if avg_vol > 0 else 1.0
    price_up = curr["close"] >= curr["open"]

    if ratio > 1.2:
        if price_up:
            signal, color, label = 1,  "green",  "Confirmed Move — Real Buyers"
        else:
            signal, color, label = -1, "red",    "Confirmed Selling — Real Pressure"
    elif ratio < 0.8:
        if price_up:
            signal, color, label = -1, "orange", "Weak Move — Low Conviction"
        else:
            signal, color, label = 1,  "yellow", "Exhaustion — Move Losing Steam"
    else:
        signal, color, label = 0, "grey", "Normal Volume — No Strong Signal"

    return {
        "ratio":    round(ratio, 2),
        "price_up": price_up,
        "label":    label,
        "color":    color,
        "signal":   signal,
    }


def _layer3_price_structure(klines: list) -> dict:
    """Higher lows / lower highs structure from last 10 candles."""
    if len(klines) < 10:
        return {"error": "insufficient data"}
    recent = klines[-10:]
    lows   = [k["low"]  for k in recent]
    highs  = [k["high"] for k in recent]

    hl_run = lh_run = 0
    for i in range(1, len(lows)):
        hl_run = hl_run + 1 if lows[i]  > lows[i-1]  else 0
        lh_run = lh_run + 1 if highs[i] < highs[i-1] else 0

    higher_lows = hl_run >= 2
    lower_highs = lh_run >= 2

    if higher_lows and lower_highs:
        signal, color, label = 0,  "yellow", "Compression — Breakout Pending"
    elif higher_lows:
        signal, color, label = 1,  "green",  "Higher Lows — Buyers Getting Aggressive"
    elif lower_highs:
        signal, color, label = -1, "red",    "Lower Highs — Sellers Getting Aggressive"
    else:
        signal, color, label = 0,  "grey",   "No Clear Structure"

    return {
        "label":       label,
        "color":       color,
        "signal":      signal,
        "higher_lows": higher_lows,
        "lower_highs": lower_highs,
        "lows":        [round(v, 4) for v in lows],
        "highs":       [round(v, 4) for v in highs],
    }


def _layer3_momentum(klines: list) -> dict:
    """Rate of Change (6 and 14 period) momentum on 4h closes."""
    if len(klines) < 15:
        return {"error": "insufficient data"}
    closes = [k["close"] for k in klines[-15:]]
    roc6   = (closes[-1] - closes[-7]) / closes[-7]  * 100 if closes[-7]  else 0
    roc14  = (closes[-1] - closes[0])  / closes[0]   * 100 if closes[0]   else 0

    positive     = roc6 > 0
    near_zero    = abs(roc6) < 0.5
    accelerating = abs(roc6) > abs(roc14 / 2)

    if near_zero:
        signal, color, label = 0,  "grey",   "No Momentum"
    elif positive and accelerating:
        signal, color, label = 1,  "green",  "Bullish Momentum Building"
    elif positive:
        signal, color, label = 0,  "yellow", "Rally Slowing — Watch for Reversal"
    elif not positive and accelerating:
        signal, color, label = -1, "red",    "Bearish Momentum Building"
    else:
        signal, color, label = 0,  "yellow", "Selling Slowing — Watch for Recovery"

    return {
        "roc6":         round(roc6,  2),
        "roc14":        round(roc14, 2),
        "label":        label,
        "color":        color,
        "signal":       signal,
        "accelerating": accelerating,
    }


def _layer3_order_book(symbol: str) -> dict:
    """Order book bid/ask imbalance from top 20 levels (spot)."""
    import requests
    url  = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20"
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    data = resp.json()

    bid_val = sum(float(p) * float(q) for p, q in data["bids"])
    ask_val = sum(float(p) * float(q) for p, q in data["asks"])
    total   = bid_val + ask_val
    bid_pct = bid_val / total * 100 if total > 0 else 50

    if bid_pct > 60:
        signal, color, label = 1,  "green", "Buy Pressure Dominant"
    elif bid_pct < 40:
        signal, color, label = -1, "red",   "Sell Pressure Dominant"
    else:
        signal, color, label = 0,  "grey",  "Balanced Order Book"

    def _fmt(v: float) -> str:
        return f"${v/1_000_000:.1f}M" if v >= 1_000_000 else f"${v/1_000:.0f}K"

    return {
        "bid_pct": round(bid_pct, 1),
        "ask_pct": round(100 - bid_pct, 1),
        "bid_val": _fmt(bid_val),
        "ask_val": _fmt(ask_val),
        "label":   label,
        "color":   color,
        "signal":  signal,
    }


def _layer3_atr(klines: list) -> dict:
    """ATR(14) volatility context — no directional signal, DCA sizing guide."""
    if len(klines) < 15:
        return {"error": "insufficient data"}
    recent = klines[-15:]
    trs    = []
    for i in range(1, len(recent)):
        prev  = recent[i-1]["close"]
        h, lo = recent[i]["high"], recent[i]["low"]
        trs.append(max(h - lo, abs(h - prev), abs(lo - prev)))

    atr     = sum(trs[-14:]) / 14
    price   = recent[-1]["close"]
    atr_pct = atr / price * 100 if price > 0 else 0

    if atr_pct < 1:
        color, label = "blue",   "Very Calm"
        dca_note     = "DCA steps: use tight spacing (3–5% steps)"
        vol_flag     = "😴 Low Volatility — tight DCA steps ok"
    elif atr_pct < 2:
        color, label = "green",  "Normal"
        dca_note     = "DCA steps: use standard spacing (5–10% steps)"
        vol_flag     = ""
    elif atr_pct < 4:
        color, label = "yellow", "Elevated"
        dca_note     = "DCA steps: use wider spacing (10–15% steps)"
        vol_flag     = ""
    else:
        color, label = "red",    "High Volatility"
        dca_note     = "DCA steps: use very wide spacing (15%+ steps) or wait for volatility to settle"
        vol_flag     = "⚡ High Volatility — wider DCA steps recommended"

    def _fmt_usd(v: float) -> str:
        return f"${v:,.0f}" if v >= 1000 else f"${v:.2f}"

    return {
        "atr":         round(atr, 2),
        "atr_usd":     _fmt_usd(atr),
        "atr_pct":     round(atr_pct, 2),
        "label":       label,
        "color":       color,
        "dca_note":    dca_note,
        "vol_flag":    vol_flag,
        "atr_history": [round(t, 4) for t in trs[-14:]],
        "signal":      0,
    }


def _layer3_verdict_calc(vol: Optional[dict], structure: Optional[dict],
                          momentum: Optional[dict], ob: Optional[dict]) -> dict:
    """Combine 4 directional Layer 3 signals into a verdict."""
    signals = [
        d["signal"] for d in [vol, structure, momentum, ob]
        if d and "error" not in d
    ]
    total   = len(signals)
    bullish = signals.count(1)
    bearish = signals.count(-1)
    neutral = total - bullish - bearish
    score   = sum(signals)

    if total == 0:
        v = {"code": "UNKNOWN",     "label": "Unavailable", "emoji": "—",  "color": "grey"}
    elif score >= 2:
        v = {"code": "LONG",        "label": "LONG SIGNAL",  "emoji": "🟢", "color": "green"}
    elif score <= -2:
        v = {"code": "SHORT",       "label": "SHORT SIGNAL", "emoji": "🔴", "color": "red"}
    elif score == 1:
        v = {"code": "WEAK_LONG",   "label": "WEAK LONG",    "emoji": "🟡", "color": "yellow"}
    elif score == -1:
        v = {"code": "WEAK_SHORT",  "label": "WEAK SHORT",   "emoji": "🟡", "color": "yellow"}
    else:
        v = {"code": "NEUTRAL",     "label": "NEUTRAL",      "emoji": "⚪", "color": "grey"}

    v.update({"bullish": bullish, "bearish": bearish, "neutral": neutral, "total": total})
    return v


def _get_layer3_data(symbol: str) -> dict:
    """Layer 3 (Entry Timing): volume divergence, price structure, momentum,
    order book imbalance, and ATR volatility from Binance public API.
    Cached per symbol for _LAYER3_TTL seconds."""
    symbol = symbol.upper()
    now    = time.time()
    cached = _layer3_cache.get(symbol)
    if cached and now - cached["ts"] < _LAYER3_TTL:
        return cached["data"]

    result: dict = {"symbol": symbol}

    try:
        klines = _layer3_klines(symbol, limit=21)
    except Exception as exc:
        result["error"]     = str(exc)
        result["timestamp"] = int(now * 1000)
        return result

    for key, fn in [
        ("volume_divergence", lambda: _layer3_volume_divergence(klines)),
        ("price_structure",   lambda: _layer3_price_structure(klines)),
        ("momentum",          lambda: _layer3_momentum(klines)),
        ("atr",               lambda: _layer3_atr(klines)),
    ]:
        try:
            result[key] = fn()
        except Exception as exc:
            result[key] = {"error": str(exc)}

    try:
        result["order_book"] = _layer3_order_book(symbol)
    except Exception as exc:
        result["order_book"] = {"error": str(exc)}

    def _ok(k):
        return result.get(k) if result.get(k) and "error" not in result[k] else None

    result["verdict"]   = _layer3_verdict_calc(
        _ok("volume_divergence"), _ok("price_structure"),
        _ok("momentum"),          _ok("order_book"),
    )
    result["price"]     = round(klines[-1]["close"], 2) if klines else None
    result["timestamp"] = int(now * 1000)

    _layer3_cache[symbol] = {"data": result, "ts": now}
    return result


@app.route("/api/layer3/<symbol>")
def api_layer3(symbol):
    return jsonify(_get_layer3_data(symbol))


# ── AI Analysis route ──────────────────────────────────────────────────────────

_PROFESSIONAL_SYSTEM_PROMPT = (
    "You are a professional cryptocurrency trader with deep expertise in technical analysis, "
    "market microstructure, macro economics, and derivatives markets. You analyze trading setups "
    "across three layers: macro environment (Layer 1), market positioning (Layer 2), and entry "
    "timing (Layer 3).\n\n"
    "You are direct, specific, and honest. You do not give vague or generic analysis. You always "
    "reference the specific numbers in front of you. You explain your reasoning in plain language "
    "that a developing trader can understand and learn from.\n\n"
    "You structure every response in exactly four sections with these exact headers:\n\n"
    "## WHAT THE MARKET IS DOING\n"
    "A clear, plain-English picture of current conditions combining all three layers. "
    "2-3 sentences maximum. Specific numbers only, no vague statements.\n\n"
    "## THE KEY TENSION\n"
    "What signals are agreeing and what is conflicting. Why the conflict matters. "
    "What it tells you about the market's uncertainty or conviction. 2-3 sentences.\n\n"
    "## PROFESSIONAL ASSESSMENT\n"
    "What an experienced trader would conclude from this exact combination of signals. "
    "Be specific about conviction level (high/medium/low) and why. Reference the most important "
    "2-3 signals driving the conclusion. 3-4 sentences.\n\n"
    "## SUGGESTION\n"
    "One of: LONG / SHORT / WAIT.\n"
    "If LONG or SHORT:\n"
    "  - Entry approach (immediate or wait for X)\n"
    "  - Step spacing recommendation (use the ATR data provided)\n"
    "  - The specific signal change that would be your exit or stop signal\n"
    "  - If liquidation clusters were provided, incorporate them into the entry/exit logic\n"
    "If WAIT:\n"
    "  - Exactly what needs to change in the data before action is warranted\n"
    "  - Which specific signal to watch\n\n"
    "End every response with this exact line:\n"
    "\"⚠️ This is analytical context to support your own decision — not financial advice. "
    "You make the final call.\""
)

_PROFESSIONAL_DCA_ADDENDUM = (
    "\n\nWhen DCA model levels are provided, incorporate them specifically into your "
    "SUGGESTION section. Comment on whether the step placement makes sense given "
    "the current ATR and any liquidation clusters. Flag any step sitting above a "
    "cluster and recommend the adjusted multiplier. Reference specific dollar "
    "levels ('$65,785'), not vague descriptions."
)

_PROFESSIONAL_TP_ADDENDUM = (
    "\n\nWhen a TAKE PROFIT ANALYSIS is provided, add a fifth section, titled exactly "
    "## PLAN VALIDATION, after the SUGGESTION section. In it:\n"
    "- Compare the trader's Confirmed TP to the market's Target TP from Scenario 1.\n"
    "- If they are aligned within 0.5%, say so explicitly and confirm the trader is reading "
    "the market correctly.\n"
    "- If the trader's TP is higher than the Target TP, assess whether that's greed or a "
    "momentum-justified stretch — reference the ATR multiple and the Layer 3 momentum verdict.\n"
    "- If the trader's TP is lower than the Target TP, assess whether that's appropriately "
    "conservative or leaving profit on the table without good reason.\n"
    "- Always reference specific dollar levels, not vague descriptions.\n"
    "- Explicitly state whether the TP placement is logical given where the liquidation clusters sit."
)

_PLAIN_SYSTEM_PROMPT = (
    "You are a friendly, plain-spoken trading guide who explains crypto market setups the way "
    "you'd explain them to a smart friend who has never traded before. You avoid jargon — when "
    "you must use a trading term, you explain it immediately in everyday language.\n\n"
    "You are still direct and honest — you do not sugar-coat a bad setup. You always reference "
    "the specific numbers in front of you, just explained simply.\n\n"
    "You structure every response in exactly four sections with these exact headers:\n\n"
    "## WHAT'S HAPPENING RIGHT NOW\n"
    "A clear, plain-English picture of current conditions combining all three layers. "
    "2-3 sentences maximum. Specific numbers only, no vague statements.\n\n"
    "## WHAT'S PULLING IN DIFFERENT DIRECTIONS\n"
    "What signals agree and what's fighting each other, and why that matters. 2-3 sentences.\n\n"
    "## WHAT AN EXPERIENCED TRADER WOULD THINK\n"
    "What a seasoned trader would conclude from this exact combination of signals. Be specific "
    "about how confident they'd be (high/medium/low) and why. 3-4 sentences.\n\n"
    "## IS YOUR PLAN GOOD?\n"
    "One of: BUY / SELL / WAIT, explained simply.\n"
    "If BUY or SELL:\n"
    "  - When to get in (now, or wait for X)\n"
    "  - How far apart to space DCA steps (use the ATR data provided)\n"
    "  - The specific thing that would make you bail out\n"
    "  - If liquidation clusters were provided, weave them into the entry/exit thinking\n"
    "If WAIT:\n"
    "  - Exactly what needs to change before it's worth acting\n"
    "  - Which specific signal to keep an eye on\n\n"
    "End every response with this exact line:\n"
    "\"⚠️ This is analytical context to support your own decision — not financial advice. "
    "You make the final call.\""
)

_PLAIN_DCA_ADDENDUM = (
    "\n\nWhen DCA model levels are provided, weave them into your IS YOUR PLAN GOOD? section "
    "in plain language. Say whether the step placement makes sense given the current volatility "
    "and any liquidation clusters. Flag any step sitting above a cluster and suggest the wider "
    "spacing in everyday terms. Reference specific dollar levels ('$65,785'), not vague descriptions."
)

_PLAIN_TP_ADDENDUM = (
    "\n\nWhen a TAKE PROFIT ANALYSIS is provided, explain it using this fish market analogy:\n\n"
    "THE FISH MARKET — there are three sizes of fish on offer today:\n"
    "- The Minimum TP is the small fish — always available, a normal-sized catch.\n"
    "- The Target TP is the medium fish — what's most likely in stock today, a realistic catch.\n"
    "- The Stretch TP is the big fish — possible on a great day, but not guaranteed to be there.\n\n"
    "THE SHOP WITH A BUDGET — when judging the trader's desired target, talk about how many of "
    "the three checks (cluster, volatility, momentum) support it, framed as availability:\n"
    "- 3 or 2 checks green: the fish is in stock today.\n"
    "- 1 check green: the fish is in the back, possible but not guaranteed.\n"
    "- 0 checks green: the fish is not available today.\n\n"
    "In the ## IS YOUR PLAN GOOD? section, always explicitly answer this question: "
    "'Is the fish the trader wants available at this market today?' Reference the trader's "
    "Confirmed TP, which fish size it's closest to, and whether it's in stock using the "
    "language above."
)


def _format_tp_plan_lines(plan: dict) -> list[str]:
    """Build the TAKE PROFIT ANALYSIS block for the AI user message from a
    Pre-Trade Checklist plan dict (see collectPreTradePlan() in index.html)."""
    lines: list[str] = ["", "TAKE PROFIT ANALYSIS:"]

    s1 = plan.get("scenario1") or {}
    lines.append("Scenario 1 — What the market is offering:")
    for key, name in (("minimum", "Minimum"), ("target", "Target"), ("stretch", "Stretch")):
        lvl = s1.get(key)
        if lvl and lvl.get("price"):
            lines.append(f"- {name} TP: ${lvl['price']:,.2f} ({lvl.get('pct', 0):+.2f}%) — {lvl.get('label', '')}")

    s2 = plan.get("scenario2")
    if s2 and s2.get("desired_pct"):
        lines.append("")
        lines.append(
            f"Scenario 2 — Trader's desired target: {s2['desired_pct']:.2f}% (${s2.get('price', 0):,.2f})"
        )
        for chk in s2.get("checks") or []:
            if chk.get("text"):
                lines.append(f"- {chk['text']}")
        verdict = s2.get("verdict")
        if verdict and verdict.get("text"):
            lines.append(f"Overall: {verdict['text']}")

    confirmed = plan.get("confirmed") or {}
    if confirmed.get("price"):
        lines.append("")
        lines.append(
            f"CONFIRMED TP: ${confirmed['price']:,.2f} ({confirmed.get('pct', 0):+.2f}%) "
            f"— {confirmed.get('source_label') or 'Manual entry'}"
        )

    agreement_text = plan.get("agreement_text")
    if agreement_text:
        lines.append("")
        lines.append(f"AGREEMENT BETWEEN SCENARIOS: {agreement_text}")

    return lines


@app.route("/api/ai/analysis", methods=["POST"])
def api_ai_analysis():
    """Generate a trading analysis via Claude API from dashboard data."""
    import anthropic as _anthropic

    payload = request.get_json(force=True, silent=True) or {}
    coin    = payload.get("coin", "BTC")
    price   = payload.get("price")
    master  = payload.get("master_verdict", "Unknown")
    l1      = payload.get("layer1", {})
    l2      = payload.get("layer2", {})
    l3      = payload.get("layer3", {})
    style   = payload.get("style", "professional")
    plan    = payload.get("pre_trade_plan")

    # ── Format price ──────────────────────────────────────────────────────────
    if isinstance(price, (int, float)):
        price_str = f"${price:,.2f}"
    elif price:
        price_str = str(price)
    else:
        price_str = "not available"

    # ── Build user message ────────────────────────────────────────────────────
    lines: list[str] = [
        f"Analyze this trading setup for {coin}:", "",
        f"CURRENT PRICE: {price_str}", "",
        f"MASTER VERDICT: {master}", "",
    ]

    # Layer 1
    lines.append(f"LAYER 1 — MACRO ENVIRONMENT ({l1.get('verdict', 'Unknown')}):")
    indicators = l1.get("indicators", [])
    if indicators:
        for ind in indicators:
            lines.append(f"- {ind['name']}: {ind['value']} — {ind['label']}")
    else:
        lines.append("Note: No Layer 1 data available")
    mc = l1.get("missing_count", 0)
    if mc:
        lines.append(f"Note: {mc} Layer 1 indicator(s) not yet entered")
    lines.append("")

    # Layer 2
    lines.append(f"LAYER 2 — MARKET POSITIONING ({l2.get('verdict', 'Unknown')}):")
    has_l2 = False
    if l2.get("funding") and l2["funding"].get("value") not in (None, "—"):
        lines.append(f"- Funding Rate: {l2['funding']['value']} — {l2['funding']['label']}")
        has_l2 = True
    if l2.get("oi") and l2["oi"].get("value") not in (None, "—"):
        oi = l2["oi"]
        chg = f" ({oi['change']})" if oi.get("change") and oi["change"] != "—" else ""
        lines.append(f"- Open Interest: {oi['value']}{chg} — {oi['label']}")
        has_l2 = True
    if l2.get("ls_global") and l2["ls_global"].get("long") not in (None, "—"):
        g = l2["ls_global"]
        lines.append(f"- Long/Short Ratio (Retail): {g['long']}% Long / {g['short']}% Short — {g['label']}")
        has_l2 = True
    if l2.get("ls_top") and l2["ls_top"].get("long") not in (None, "—"):
        t = l2["ls_top"]
        lines.append(f"- Long/Short Ratio (Top Traders): {t['long']}% Long / {t['short']}% Short — {t['label']}")
        has_l2 = True
    if l2.get("liq_below"):
        lines.append(f"- Liquidation cluster BELOW price: ${l2['liq_below']}")
    if l2.get("liq_above"):
        lines.append(f"- Liquidation cluster ABOVE price: ${l2['liq_above']}")
    if not has_l2:
        lines.append("Note: No Layer 2 data available")
    lines.append("")

    # Layer 3
    if not l3.get("available", True):
        lines.extend(["LAYER 3 — ENTRY TIMING:",
                       "Note: Layer 3 data unavailable — analyze with Layer 1 and Layer 2 only."])
    else:
        score_info = f", {l3['score']} signals" if l3.get("score") else ""
        lines.append(f"LAYER 3 — ENTRY TIMING ({l3.get('verdict', 'Unknown')}{score_info}):")
        if l3.get("volume") and l3["volume"]:
            v = l3["volume"]
            lines.append(f"- Volume Divergence: {v.get('ratio','?')}x average — {v.get('label','')}")
        if l3.get("structure"):
            lines.append(f"- Price Structure: {l3['structure'].get('label','')}")
        if l3.get("momentum"):
            m = l3["momentum"]
            lines.append(f"- Momentum: {m.get('roc6','?')}% (24h) / {m.get('roc14','?')}% (56h) — {m.get('label','')}")
        if l3.get("ob"):
            ob = l3["ob"]
            lines.append(f"- Order Book: Bids {ob.get('bid_pct','?')}% / Asks {ob.get('ask_pct','?')}% — {ob.get('label','')}")
        if l3.get("atr"):
            atr = l3["atr"]
            lines.append(f"- ATR Volatility: {atr.get('atr_pct','?')}% — {atr.get('label','')}")
            if atr.get("dca_note"):
                lines.append(f"  DCA implication: {atr['dca_note']}")

    # Take Profit plan (Pre-Trade Checklist), if provided
    if plan:
        lines.extend(_format_tp_plan_lines(plan))

    # DCA model levels (if provided by the visualizer panel)
    dca = payload.get("dca_model")
    if dca:
        dca_lines: list[str] = [
            "",
            f"SELECTED DCA MODEL: {dca.get('name', 'Unknown')} ({dca.get('side', 'long').upper()} side)",
            f"ATR MULTIPLIER: {dca.get('multiplier', 1.5)}×",
            "",
            "CALCULATED ENTRY LEVELS:",
        ]
        for step in dca.get("steps", []):
            cw  = f"  {step['cluster_warning']}" if step.get("cluster_warning") else ""
            dca_lines.append(
                f"- Step {step['index']}: ${step.get('price', 0):,.2f}"
                f" ({step.get('pct_from_anchor', 0):+.2f}%)"
                f" — ${step.get('size_usdt', 0):,.0f} USDT{cw}"
            )
        tp = dca.get("tp_price")
        if tp:
            dca_lines.append(
                f"- Take Profit: ${tp:,.2f}"
                f" ({dca.get('tp_pct_from_avg', 0):+.1f}% from avg entry,"
                f" {dca.get('tp_pct_from_current', 0):+.1f}% from current)"
            )
        dca_lines.extend([
            f"- Total capital if all filled: ${dca.get('total_capital', 0):,.0f}",
            f"- Avg entry if all steps fill: ${dca.get('avg_entry', 0):,.2f}",
        ])
        if dca.get("suggested_multiplier"):
            dca_lines.extend(["",
                f"CLUSTER INTERACTION: Consider {dca['suggested_multiplier']}× multiplier "
                f"to place steps below liquidation clusters."])
        lines.extend(dca_lines)

    closing = "Please provide your analysis." if style == "plain" else "Please provide your professional analysis."
    lines.extend(["", closing])
    user_msg = "\n".join(lines)

    if style == "plain":
        system_prompt = _PLAIN_SYSTEM_PROMPT
        if dca:
            system_prompt += _PLAIN_DCA_ADDENDUM
        if plan:
            system_prompt += _PLAIN_TP_ADDENDUM
    else:
        system_prompt = _PROFESSIONAL_SYSTEM_PROMPT
        if dca:
            system_prompt += _PROFESSIONAL_DCA_ADDENDUM
        if plan:
            system_prompt += _PROFESSIONAL_TP_ADDENDUM

    try:
        client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg    = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1300,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return jsonify({"ok": True, "text": msg.content[0].text})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



# ── Routes — Layer 1 (Macro Environment) ───────────────────────────────────────

_FOMC_2026_DATES = [
    "2026-01-29", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-07-30", "2026-09-17", "2026-10-29", "2026-12-10",
]


def _next_fomc_date() -> str:
    """Next upcoming FOMC meeting date from the published 2026 schedule."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for d in _FOMC_2026_DATES:
        if d >= today:
            return d
    return _FOMC_2026_DATES[-1]


def _next_monthly_release_date(day: int) -> str:
    """Next occurrence of `day`-of-month on/after today (UTC)."""
    today = datetime.utcnow().date()
    year, month = today.year, today.month
    candidate = today.replace(day=min(day, calendar.monthrange(year, month)[1]))
    if candidate < today:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        candidate = datetime(year, month, min(day, calendar.monthrange(year, month)[1])).date()
    return candidate.isoformat()


def _next_jobs_report_date() -> str:
    """First Friday of next month (BLS Non-Farm Payrolls schedule)."""
    today = datetime.utcnow().date()
    year, month = today.year, today.month + 1
    if month > 12:
        month, year = 1, year + 1
    d = datetime(year, month, 1).date()
    while d.weekday() != 4:  # Friday
        d += timedelta(days=1)
    return d.isoformat()


def _layer1_fear_greed() -> dict:
    """Fear & Greed Index (alternative.me) — contrarian crowd-emotion signal."""
    import requests

    data = requests.get("https://api.alternative.me/fng/", params={"limit": 30}, timeout=8).json()
    items = data.get("data") or []
    if not items:
        raise ValueError("no fear/greed data")

    values = [int(d["value"]) for d in items]
    current = values[0]
    history = list(reversed(values))  # oldest -> newest

    if current <= 24:
        label, color, signal = "Extreme Fear", "red", 1
    elif current <= 44:
        label, color, signal = "Fear", "orange", 1
    elif current <= 55:
        label, color, signal = "Neutral", "grey", 0
    elif current <= 74:
        label, color, signal = "Greed", "ltgreen", -1
    else:
        label, color, signal = "Extreme Greed", "green", -1

    return {
        "status": "ok",
        "value": current,
        "label": label,
        "color": color,
        "signal": signal,
        "history": history,
    }


def _layer1_btc_dominance() -> dict:
    """BTC Dominance (CoinGecko) — risk-on/off signal within crypto.

    CoinGecko's /global endpoint has no historical dominance series, so a
    rolling in-memory history of recent samples (capped at 48h) is used to
    approximate the 24h change."""
    import requests

    data = requests.get("https://api.coingecko.com/api/v3/global", timeout=8).json()
    pct = float(data["data"]["market_cap_percentage"]["btc"])

    now = time.time()
    _btc_dom_history.append((now, pct))
    cutoff = now - 48 * 3600
    while _btc_dom_history and _btc_dom_history[0][0] < cutoff:
        _btc_dom_history.pop(0)

    target = now - 24 * 3600
    prev_pct = pct
    for ts, val in _btc_dom_history:
        if ts <= target:
            prev_pct = val
        else:
            break
    change_24h = pct - prev_pct

    if change_24h > 0.5:
        label, color, signal = "BTC Season", "orange", -1
    elif change_24h < -0.5:
        label, color, signal = "Altcoin Season", "blue", 1
    else:
        label, color, signal = "Neutral", "grey", 0

    return {
        "status": "ok",
        "value": pct,
        "change_24h": change_24h,
        "label": label,
        "color": color,
        "signal": signal,
    }


def _layer1_dxy() -> dict:
    """DXY (US Dollar Index) via Twelve Data — dollar strength vs. crypto."""
    key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not key:
        return {"status": "no_key"}

    import requests

    data = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "DXY", "interval": "1day", "outputsize": 30, "apikey": key},
        timeout=8,
    ).json()
    values = data.get("values")
    if not values:
        if data.get("status") == "error" and data.get("code") in (400, 403, 404):
            # DXY is not available on Twelve Data's free plan — fall back to
            # the manual check-now card rather than "Data unavailable".
            return {"status": "no_key"}
        raise ValueError(data.get("message") or "no DXY data")

    closes = [float(v["close"]) for v in reversed(values)]  # oldest -> newest
    current = closes[-1]
    prev_7d = closes[-8] if len(closes) >= 8 else closes[0]
    change_pct = (current - prev_7d) / prev_7d * 100 if prev_7d else 0.0

    if change_pct > 0.5:
        label, color, signal = "Dollar Strengthening — Headwind", "red", -1
    elif change_pct < -0.5:
        label, color, signal = "Dollar Weakening — Tailwind", "green", 1
    else:
        label, color, signal = "Neutral", "grey", 0

    return {
        "status": "ok",
        "value": current,
        "change_7d_pct": change_pct,
        "history": closes,
        "label": label,
        "color": color,
        "signal": signal,
    }


def _fred_observations(series_id: str, limit: int) -> Optional[list]:
    """Fetch FRED observations as [(date, value), ...] in descending date
    order, skipping missing ('.') values. Returns None if FRED_API_KEY unset."""
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        return None

    import requests

    data = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": key, "sort_order": "desc",
                "limit": limit, "file_type": "json"},
        timeout=8,
    ).json()
    obs = data.get("observations")
    if not obs:
        raise ValueError(data.get("error_message") or f"no {series_id} data")

    parsed = [(o["date"], float(o["value"])) for o in obs if o.get("value") not in (None, ".", "")]
    if not parsed:
        raise ValueError(f"no usable {series_id} observations")
    return parsed


def _layer1_fed_funds() -> dict:
    """Fed Funds Rate (FRED) — current policy stance and cycle direction."""
    obs = _fred_observations("FEDFUNDS", 3)
    if obs is None:
        return {"status": "no_key"}

    values = [v for _, v in obs]
    current = values[0]
    if len(values) >= 3 and current > values[2]:
        cycle, color, signal, label = "Rising Cycle", "red", -1, "Tightening — Risk Off"
    elif len(values) >= 3 and current < values[2]:
        cycle, color, signal, label = "Cutting Cycle", "green", 1, "Easing — Risk On"
    else:
        cycle, color, signal, label = "Holding", "grey", 0, "On Hold — Watch Direction"

    return {
        "status": "ok",
        "value": current,
        "cycle": cycle,
        "label": label,
        "color": color,
        "signal": signal,
        "next_fomc": _next_fomc_date(),
    }


def _layer1_yield_10y() -> dict:
    """US 10-Year Treasury Yield (FRED) — the risk-free rate crypto competes with."""
    obs = _fred_observations("DGS10", 8)
    if obs is None:
        return {"status": "no_key"}

    values = [v for _, v in obs]
    current = values[0]
    prev = values[-1]
    change_7d = current - prev

    if current > 4.5 and change_7d > 0:
        label, color = "High Yield — Headwind for Crypto", "red"
    elif current < 3.5:
        label, color = "Low Yield — Tailwind for Crypto", "green"
    else:
        label, color = "Moderate", "grey"

    if change_7d < 0 and current < 4:
        signal = 1
    elif change_7d > 0 and current > 4.5:
        signal = -1
    else:
        signal = 0

    return {
        "status": "ok",
        "value": current,
        "change_7d": change_7d,
        "label": label,
        "color": color,
        "signal": signal,
    }


def _layer1_cpi() -> dict:
    """CPI year-over-year inflation (FRED) — distance from the Fed's 2% target."""
    obs = _fred_observations("CPIAUCSL", 13)
    if obs is None:
        return {"status": "no_key"}

    values = [v for _, v in obs]
    if len(values) < 13:
        raise ValueError("insufficient CPI history")

    current, prev_month, year_ago = values[0], values[1], values[12]
    yoy = (current - year_ago) / year_ago * 100
    direction = "rising" if current > prev_month else "falling"

    diff = yoy - 2.0
    if abs(diff) < 0.05:
        vs_target = "At Fed's 2% target"
    elif diff > 0:
        vs_target = f"{diff:.1f}% above Fed's 2% target"
    else:
        vs_target = f"{abs(diff):.1f}% below Fed's 2% target"

    if yoy > 4:
        label, color = "High Inflation — Fed Hawkish", "red"
    elif yoy >= 2:
        label, color = "Elevated — Watch Direction", "yellow"
    else:
        label, color = "At/Below Target — Fed Dovish", "green"

    if direction == "falling" and yoy < 3:
        signal = 1
    elif yoy > 4 or direction == "rising":
        signal = -1
    else:
        signal = 0

    return {
        "status": "ok",
        "value": yoy,
        "direction": direction,
        "vs_target": vs_target,
        "label": label,
        "color": color,
        "signal": signal,
        "next_release": _next_monthly_release_date(12),
    }


def _layer1_vix() -> dict:
    """VIX (Twelve Data) — traditional-market fear gauge, correlated with crypto risk."""
    key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not key:
        return {"status": "no_key"}

    import requests

    data = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "VIX", "interval": "1day", "outputsize": 7, "apikey": key},
        timeout=8,
    ).json()
    values = data.get("values")
    if not values:
        if data.get("status") == "error" and data.get("code") in (400, 403, 404):
            # VIX is not available on Twelve Data's free plan — fall back to
            # the manual check-now card rather than "Data unavailable".
            return {"status": "no_key"}
        raise ValueError(data.get("message") or "no VIX data")

    closes = [float(v["close"]) for v in reversed(values)]
    current = closes[-1]
    change_7d = current - closes[0]

    if current < 15:
        label, color, signal = "Calm — Risk On", "green", 1
    elif current <= 25:
        label, color, signal = "Normal", "grey", 0
    elif current <= 30:
        label, color, signal = "Elevated Fear", "yellow", 0
    elif current <= 40:
        label, color, signal = "High Fear — Risk Off", "red", -1
    else:
        label, color, signal = "Crisis Level", "darkred", -1

    return {
        "status": "ok",
        "value": current,
        "change_7d": change_7d,
        "history": closes,
        "label": label,
        "color": color,
        "signal": signal,
    }


_LAYER1_INDICATORS = (
    "fear_greed", "btc_dominance", "dxy", "fed_funds", "yield_10y", "cpi", "vix",
)


def _layer1_verdict(result: dict) -> dict:
    """Combine the live macro indicators that returned data into one verdict."""
    signals = [
        result[key]["signal"]
        for key in _LAYER1_INDICATORS
        if result.get(key, {}).get("status") == "ok"
    ]

    if len(signals) < 3:
        return {"code": "INSUFFICIENT_DATA", "label": "INSUFFICIENT DATA", "emoji": "⚪", "color": "grey"}

    bullish = sum(1 for s in signals if s > 0)
    bearish = sum(1 for s in signals if s < 0)

    if bullish >= 4:
        return {"code": "FAVORABLE", "label": "MACRO FAVORABLE", "emoji": "🟢", "color": "green"}
    if bearish >= 4:
        return {"code": "UNFAVORABLE", "label": "MACRO UNFAVORABLE", "emoji": "🔴", "color": "red"}
    return {"code": "MIXED", "label": "MIXED", "emoji": "🟡", "color": "yellow"}


def _get_layer1_data() -> dict:
    """Layer 1 (Macro Environment): Fear & Greed, BTC Dominance, DXY, Fed
    Funds Rate, 10Y Treasury Yield, CPI, and VIX, plus a combined macro
    verdict. Cached for _LAYER1_TTL seconds."""
    now = time.time()
    cached = _layer1_cache.get("data")
    if cached and now - _layer1_cache["ts"] < _LAYER1_TTL:
        return cached

    fetchers = {
        "fear_greed":    _layer1_fear_greed,
        "btc_dominance": _layer1_btc_dominance,
        "dxy":           _layer1_dxy,
        "fed_funds":     _layer1_fed_funds,
        "yield_10y":     _layer1_yield_10y,
        "cpi":           _layer1_cpi,
        "vix":           _layer1_vix,
    }

    result: dict = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {name: pool.submit(fn) for name, fn in fetchers.items()}
        for name, fut in futures.items():
            try:
                result[name] = fut.result()
            except Exception as exc:
                result[name] = {"status": "error", "error": str(exc)}

    result["dates"] = {
        "next_fomc": _next_fomc_date(),
        "next_jobs_report": _next_jobs_report_date(),
    }
    result["verdict"] = _layer1_verdict(result)
    result["timestamp"] = int(now * 1000)

    _layer1_cache["data"] = result
    _layer1_cache["ts"] = now
    return result


@app.route("/api/layer1")
def api_layer1():
    return jsonify(_get_layer1_data())


# ── Signal History Recording ────────────────────────────────────────────────
# Persists periodic Layer 1/2/3 snapshots (see core/signal_recorder.py) so the
# dashboard can show how indicators have been trending, on top of the
# snapshot-only live cards above. Purely additive — reads already-cached/live
# layer data and writes to its own SQLite table; never touches live trading state.

_SIGNAL_HISTORY_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAUTUSDT"]

_FLAT_FILES = {
    "signal_history": os.path.join(SIGNAL_DATA_DIR, "signal_history.json"),
    "signal_state":   os.path.join(SIGNAL_DATA_DIR, "signal_state.json"),
    "signal_config":  os.path.join(SIGNAL_DATA_DIR, "signal_config.json"),
    "checklist_tp":   os.path.join(SIGNAL_DATA_DIR, "checklist_tp.json"),
}


def _migrate_flat_files_to_db() -> None:
    """One-time migration: copy flat JSON files into SQLite tables, then rename them.

    Each file is only migrated if it exists and has not already been renamed to
    '.migrated'.  Safe to call repeatedly — it is a no-op when files are absent.
    """
    # signal_history.json  →  signal_history table
    src = _FLAT_FILES["signal_history"]
    if os.path.exists(src):
        try:
            with open(src) as f:
                hist = json.load(f)
            # format: {symbol: [{layer, ts, ...}, ...]} or [{}] for layer 1 (symbol=None)
            if isinstance(hist, dict):
                for sym, entries in hist.items():
                    if not isinstance(entries, list):
                        continue
                    symbol = None if sym in ("layer1", "global", "") else sym
                    for entry in entries:
                        if isinstance(entry, dict):
                            layer = int(entry.get("layer", 2))
                            _db.signal_history_insert(symbol, layer, entry)
            elif isinstance(hist, list):
                for entry in hist:
                    if isinstance(entry, dict):
                        layer = int(entry.get("layer", 1))
                        sym = entry.get("symbol")
                        _db.signal_history_insert(sym, layer, entry)
            os.rename(src, src + ".migrated")
        except Exception as exc:
            print(f"[migration] signal_history.json skipped: {exc}")

    # signal_state.json  →  signal_state table
    src = _FLAT_FILES["signal_state"]
    if os.path.exists(src):
        try:
            with open(src) as f:
                states = json.load(f)
            if isinstance(states, dict):
                for sym, state in states.items():
                    if isinstance(state, dict):
                        _db.signal_state_upsert(sym, state)
            os.rename(src, src + ".migrated")
        except Exception as exc:
            print(f"[migration] signal_state.json skipped: {exc}")

    # signal_config.json  →  signal_config table
    src = _FLAT_FILES["signal_config"]
    if os.path.exists(src):
        try:
            with open(src) as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                _db.signal_config_set_all(cfg)
            os.rename(src, src + ".migrated")
        except Exception as exc:
            print(f"[migration] signal_config.json skipped: {exc}")

    # checklist_tp.json  →  checklist_state table
    src = _FLAT_FILES["checklist_tp"]
    if os.path.exists(src):
        try:
            with open(src) as f:
                tp_data = json.load(f)
            if isinstance(tp_data, dict):
                for sym, entry in tp_data.items():
                    if isinstance(entry, dict) and entry.get("tp_pct") is not None:
                        _db.checklist_upsert(sym, tp_pct=entry["tp_pct"])
            os.rename(src, src + ".migrated")
        except Exception as exc:
            print(f"[migration] checklist_tp.json skipped: {exc}")


# Initialise DB and run one-time flat-file migration before creating recorder/evaluator
_db.init_db()
_migrate_flat_files_to_db()

recorder  = SignalRecorder()
evaluator = SignalEvaluator(recorder)


# ── TP state helpers (now reads/writes DB via checklist_state table) ─────────


def _load_tp_state() -> dict:
    """Return {symbol: {tp_pct, entry_price, stop_loss, position_size, updated_at}}."""
    rows = _db.checklist_get_all()
    result = {}
    for sym, row in rows.items():
        if row.get("tp_pct") is not None:
            result[sym] = {
                "tp_pct":        row["tp_pct"],
                "entry_price":   row.get("entry_price"),
                "stop_loss":     row.get("stop_loss"),
                "position_size": row.get("position_size"),
                "saved_at":      row.get("updated_at"),
            }
    return result


@app.route("/api/checklist/tp", methods=["GET"])
def api_checklist_tp_get():
    """GET /api/checklist/tp — return all saved per-coin TP state."""
    return jsonify(_load_tp_state())


@app.route("/api/checklist/tp", methods=["POST"])
def api_checklist_tp_save():
    """POST /api/checklist/tp — save TP data for one coin."""
    payload = request.json or {}
    symbol = (payload.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    _db.checklist_upsert(symbol,
        tp_pct=payload.get("tp_pct"),
        entry_price=payload.get("entry_price"),
        stop_loss=payload.get("stop_loss"),
        position_size=payload.get("position_size"),
    )
    return jsonify({"ok": True})


@app.route("/api/checklist/tp-plan", methods=["POST"])
def api_checklist_tp_plan():
    """POST /api/checklist/tp-plan — accepts the full pre-trade plan from the
    Checklist tab and extracts the confirmed TP% for server-side persistence."""
    payload = request.json or {}
    coin    = (payload.get("coin") or "").upper()
    plan    = payload.get("plan") or {}
    if not coin or not plan:
        return jsonify({"ok": True})
    symbol    = coin + "USDT" if not coin.endswith("USDT") else coin
    confirmed = plan.get("confirmed") or {}
    tp_pct    = confirmed.get("pct")
    if tp_pct is None:
        return jsonify({"ok": True})
    _db.checklist_upsert(symbol,
        tp_pct=tp_pct,
        entry_price=plan.get("entry_price"),
        stop_loss=plan.get("stop_loss"),
        position_size=plan.get("position_size"),
    )
    return jsonify({"ok": True})


# ── Manual data routes ────────────────────────────────────────────────────────


@app.route("/api/manual/layer1", methods=["GET"])
def api_manual_l1_get():
    """GET /api/manual/layer1 — all Layer 1 manual fields."""
    return jsonify(_db.l1_get_all())


@app.route("/api/manual/layer1", methods=["POST"])
def api_manual_l1_save():
    """POST /api/manual/layer1 — upsert one field.
    Body: { field_key: str, value: object }"""
    payload = request.json or {}
    key = payload.get("field_key", "")
    val = payload.get("value")
    if not key or val is None:
        return jsonify({"error": "field_key and value required"}), 400
    _db.l1_upsert(key, val)
    return jsonify({"ok": True})


@app.route("/api/manual/liquidation-clusters/<symbol>", methods=["GET"])
def api_liq_clusters_get(symbol):
    return jsonify(_db.liq_clusters_get(symbol.upper()))


@app.route("/api/manual/liquidation-clusters/<symbol>", methods=["POST"])
def api_liq_clusters_save(symbol):
    payload = request.json or {}
    _db.liq_clusters_upsert(
        symbol.upper(),
        payload.get("cluster_below"),
        payload.get("cluster_above"),
    )
    return jsonify({"ok": True})


@app.route("/api/manual/liquidation-clusters", methods=["GET"])
def api_liq_clusters_all():
    return jsonify(_db.liq_clusters_get_all())


@app.route("/api/manual/liq24h/<symbol>", methods=["GET"])
def api_liq24h_get(symbol):
    return jsonify(_db.liq24h_get(symbol.upper()))


@app.route("/api/manual/liq24h/<symbol>", methods=["POST"])
def api_liq24h_save(symbol):
    payload = request.json or {}
    _db.liq24h_upsert(
        symbol.upper(),
        payload.get("longs"),
        payload.get("shorts"),
    )
    return jsonify({"ok": True})


@app.route("/api/manual/liq24h", methods=["GET"])
def api_liq24h_all():
    return jsonify(_db.liq24h_get_all())


@app.route("/api/manual/exchange-flow/<symbol>", methods=["GET"])
def api_ef_get(symbol):
    return jsonify(_db.ef_get(symbol.upper()))


@app.route("/api/manual/exchange-flow/<symbol>", methods=["POST"])
def api_ef_save(symbol):
    payload = request.json or {}
    _db.ef_upsert(
        symbol.upper(),
        payload.get("direction"),
        payload.get("size"),
        payload.get("notes"),
    )
    return jsonify({"ok": True})


@app.route("/api/manual/exchange-flow", methods=["GET"])
def api_ef_all():
    return jsonify(_db.ef_get_all())


@app.route("/api/manual/whale-orders/<symbol>", methods=["GET"])
def api_whale_get(symbol):
    return jsonify(_db.whale_get(symbol.upper()))


@app.route("/api/manual/whale-orders/<symbol>", methods=["POST"])
def api_whale_add(symbol):
    payload = request.json or {}
    order_id = str(payload.get("id") or int(datetime.utcnow().timestamp() * 1000))
    price    = payload.get("price")
    if not price:
        return jsonify({"error": "price required"}), 400
    _db.whale_insert(
        symbol.upper(), order_id, float(price),
        payload.get("size") or payload.get("size_str"),
        payload.get("direction"),
        payload.get("notes"),
    )
    return jsonify({"ok": True})


@app.route("/api/manual/whale-orders/<symbol>/<order_id>", methods=["PUT"])
def api_whale_update(symbol, order_id):
    payload = request.json or {}
    status  = payload.get("status")
    if not status:
        return jsonify({"error": "status required"}), 400
    _db.whale_update_status(order_id, status)
    return jsonify({"ok": True})


@app.route("/api/manual/whale-orders/<symbol>/<order_id>", methods=["DELETE"])
def api_whale_delete(symbol, order_id):
    _db.whale_delete(order_id)
    return jsonify({"ok": True})


@app.route("/api/manual/whale-orders", methods=["GET"])
def api_whale_all():
    return jsonify(_db.whale_get_all())


@app.route("/api/checklist/<symbol>", methods=["GET"])
def api_checklist_get(symbol):
    return jsonify(_db.checklist_get(symbol.upper()))


@app.route("/api/checklist/<symbol>", methods=["POST"])
def api_checklist_save(symbol):
    payload = request.json or {}
    allowed = ("direction", "tp_pct", "entry_price", "stop_loss", "position_size")
    kwargs  = {k: v for k, v in payload.items() if k in allowed}
    _db.checklist_upsert(symbol.upper(), **kwargs)
    return jsonify({"ok": True})


@app.route("/api/checklist", methods=["GET"])
def api_checklist_all():
    return jsonify(_db.checklist_get_all())


@app.route("/api/manual/position-ratio/<symbol>", methods=["GET"])
def api_position_ratio_manual_get(symbol):
    return jsonify(_db.position_ratio_get(symbol.upper()))


@app.route("/api/manual/position-ratio/<symbol>", methods=["POST"])
def api_position_ratio_manual_save(symbol):
    payload = request.json or {}
    _db.position_ratio_upsert(
        symbol.upper(),
        payload.get("long_pct"),
        payload.get("short_pct"),
    )
    return jsonify({"ok": True})


@app.route("/api/manual/position-ratio", methods=["GET"])
def api_position_ratio_manual_all():
    return jsonify(_db.position_ratio_get_all())


# ── Telegram notification sender ─────────────────────────────────────────────

def _telegram_send(text: str) -> None:
    """Send a Telegram message to all configured chat IDs.
    TELEGRAM_CHAT_ID may be a single ID or comma-separated list."""
    import requests as _req
    token    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_ids_raw:
        print("⚠️  Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping notification")
        return
    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
    for chat_id in chat_ids:
        try:
            resp = _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if not resp.ok:
                print(f"⚠️  Telegram: send to {chat_id} failed {resp.status_code} — {resp.text[:200]}")
        except Exception as exc:
            print(f"⚠️  Telegram: send to {chat_id} error — {exc}")


def _tp_achievability_check(symbol: str, tp_pct: float, l3_data: dict,
                              liq_data: Optional[dict]) -> dict:
    """Reuse Scenario 2 logic: check ATR multiple, cluster distance, L3 verdict.
    Returns dict with check results and plain-language rating."""
    atr_section = l3_data.get("atr") or {}
    atr_pct     = atr_section.get("atr_pct")
    l3_verdict  = (l3_data.get("verdict") or {}).get("code", "UNKNOWN")

    checks = {}

    # Check 1: L3 supports the direction implied by TP sign
    tp_is_long = tp_pct > 0
    l3_supports = (
        (tp_is_long  and l3_verdict in ("LONG", "WEAK_LONG")) or
        (not tp_is_long and l3_verdict in ("SHORT", "WEAK_SHORT"))
    )
    checks["l3_supports"] = l3_supports

    # Check 2: ATR multiple (<=2x green / <=4x yellow / >4x red)
    tp_abs = abs(tp_pct)
    if atr_pct and atr_pct > 0:
        atr_multiple = tp_abs / atr_pct
        checks["atr_ok"] = atr_multiple <= 2.0
        checks["atr_possible"] = atr_multiple <= 4.0
        checks["atr_multiple"] = round(atr_multiple, 1)
    else:
        checks["atr_ok"] = None
        checks["atr_possible"] = None
        checks["atr_multiple"] = None

    # Check 3: TP sits within nearest cluster distance
    current_price = l3_data.get("price")
    if liq_data and current_price and current_price > 0:
        if tp_is_long:
            cluster = liq_data.get("above_price")
        else:
            cluster = liq_data.get("below_price")
        if cluster:
            cluster_pct = abs(cluster - current_price) / current_price * 100
            checks["within_cluster"] = tp_abs <= cluster_pct
            checks["cluster_pct"]   = round(cluster_pct, 2)
        else:
            checks["within_cluster"] = None
            checks["cluster_pct"]    = None
    else:
        checks["within_cluster"] = None
        checks["cluster_pct"]    = None

    # Rating (3 green = achievable, etc.)
    greens = sum(1 for k in ("l3_supports", "atr_ok", "within_cluster")
                 if checks.get(k) is True)
    if greens == 3:
        rating = "achievable"
    elif greens == 2:
        rating = "possible but not ideal"
    elif greens == 1:
        rating = "a stretch"
    else:
        rating = "unlikely"

    return {"checks": checks, "greens": greens, "rating": rating}


def _format_telegram_message(symbol: str, tier: str, direction: str,
                               detail: dict, tp_state: Optional[dict],
                               l3_data: Optional[dict],
                               liq_data: Optional[dict],
                               include_tp: bool) -> str:
    coin = symbol.replace("USDT", "")
    tier_emoji = "🔴" if tier == "STRONG" else "🟡"
    dir_emoji  = "📈" if direction == "LONG" else "📉"

    lines = [
        f"{tier_emoji} *{coin} — {tier} {direction} SIGNAL*",
        "",
        "*Criteria confirmed:*",
    ]

    labels = {
        "master_summary":          "Master Summary",
        "l2_crowding":             "L2 Crowd Positioning",
        "position_ratio_divergence": "Position Ratio Divergence",
        "oi_trend":                "OI Trend",
        "market_mechanics":        "Market Mechanics",
        "liquidation_proximity":   "Liquidation Proximity",
    }
    for key, label in labels.items():
        icon = "✅" if detail.get(key) else "❌"
        lines.append(f"  {icon} {label}")

    lines.append("")

    # TP achievability (Part 5)
    tp = (tp_state or {}).get(symbol)
    if include_tp and tp and tp.get("tp_pct") is not None:
        tp_pct = tp["tp_pct"]
        if l3_data and not l3_data.get("error"):
            ach = _tp_achievability_check(symbol, tp_pct, l3_data, liq_data)
            lines += [
                f"*TP Achievability* (saved target: {tp_pct:+.1f}%)",
                f"  L3 supports direction: {'✅' if ach['checks']['l3_supports'] else '❌'}",
            ]
            atr_m = ach["checks"].get("atr_multiple")
            atr_ok = ach["checks"].get("atr_ok")
            atr_pos = ach["checks"].get("atr_possible")
            if atr_m is not None:
                atr_icon = "✅" if atr_ok else ("⚠️" if atr_pos else "❌")
                lines.append(f"  ATR multiple: {atr_m}× {atr_icon}")
            clust = ach["checks"].get("within_cluster")
            if clust is not None:
                lines.append(f"  Within cluster: {'✅' if clust else '❌'}")
            lines.append(f"  *Rating: {ach['rating'].upper()}*")
            if ach["rating"] in ("a stretch", "unlikely"):
                cpct = ach["checks"].get("cluster_pct")
                if cpct:
                    lines.append(f"  Cluster-based realistic target: {cpct:.1f}%")
        lines.append("")
    elif include_tp and tier == "STRONG" and (not tp or tp.get("tp_pct") is None):
        lines.append("💡 Open Checklist tab to set a TP target for achievability analysis.")
        lines.append("")

    lines.append(f"_{dir_emoji} Open dashboard to review full breakdown_")
    return "\n".join(lines)


# ── Signal evaluation + notification (piggybacked on layer2 recording job) ───


def _evaluate_and_notify(symbol: str, snapshot: dict,
                          l2_data: dict, mm_data: Optional[dict]) -> None:
    """Evaluate tier for one symbol, persist state, and send Telegram if needed."""
    config = load_signal_config()

    # Read liquidation cluster data if available (stored in localStorage —
    # no server side data, so liq_data is not available in background loop)
    liq_data = None
    current_price: Optional[float] = None
    try:
        l3 = _layer3_cache.get(symbol, {}).get("data") or {}
        current_price = l3.get("price")
    except Exception:
        pass

    transition = evaluator.evaluate_and_persist(
        symbol, snapshot, l2_data, mm_data,
        liq_data, current_price, config)

    result    = transition["result"]
    prev_tier = transition["prev_tier"]
    prev_notified_tier = transition["prev_notified_tier"]
    new_tier  = result["tier"]

    # Notification logic
    should_notify = (
        new_tier == "STRONG" or
        (new_tier == "DEVELOPING" and prev_notified_tier != "DEVELOPING")
    )

    if not should_notify:
        return

    if not config.get("telegram_enabled", True):
        evaluator.mark_notified(symbol, new_tier)
        return

    include_tp = config.get("tp_achievability_in_msg", True)
    tp_state   = _load_tp_state()
    l3_data    = (_layer3_cache.get(symbol) or {}).get("data")

    text = _format_telegram_message(
        symbol, new_tier, result["direction"],
        result["criteria_detail"], tp_state, l3_data, liq_data, include_tp)

    _telegram_send(text)
    evaluator.mark_notified(symbol, new_tier)


def _record_layer1_job():
    try:
        data = _get_layer1_data()
        if data and data.get("verdict"):
            recorder.record_layer1(data)
    except Exception as exc:
        print(f"⚠️  Signal history: layer1 recording failed: {exc}")


def _record_layer2_job():
    for symbol in _SIGNAL_HISTORY_SYMBOLS:
        try:
            data = _get_layer2_data(symbol)
            if data and data.get("verdict"):
                snapshot_data = _signal_snapshot(symbol)
                master = snapshot_data["master"]
                try:
                    mechanics = _get_market_mechanics_data(symbol)
                except Exception:
                    mechanics = None
                recorder.record_layer2(symbol, data, master, mechanics)
                try:
                    _evaluate_and_notify(symbol, snapshot_data, data, mechanics)
                except Exception as exc:
                    print(f"⚠️  Signal evaluator: failed for {symbol}: {exc}")
        except Exception as exc:
            print(f"⚠️  Signal history: layer2 recording failed for {symbol}: {exc}")


def _record_layer3_job():
    for symbol in _SIGNAL_HISTORY_SYMBOLS:
        try:
            data = _get_layer3_data(symbol)
            if data and not data.get("error"):
                recorder.record_layer3(symbol, data)
        except Exception as exc:
            print(f"⚠️  Signal history: layer3 recording failed for {symbol}: {exc}")


@app.route("/api/signal_history/<symbol>")
def api_signal_history(symbol):
    """GET /api/signal_history/<symbol>?days=7  (days capped at 21)

    Returns the last N days of Layer 1/2/3 snapshots for the given symbol.
    Layer 1 is global but included in the response for context."""
    symbol = symbol.upper()
    days = int(request.args.get("days", 7))
    days = max(1, min(days, 21))
    return jsonify(recorder.get_history(symbol, days))


@app.route("/api/signal_state")
def api_signal_state():
    """GET /api/signal_state — current tier state for all tracked coins."""
    return jsonify(evaluator.get_all_states())


@app.route("/api/signal_state/evaluate", methods=["POST"])
def api_signal_state_evaluate():
    """POST /api/signal_state/evaluate — trigger an immediate evaluation for
    all symbols (useful for testing; background job runs automatically)."""
    results = {}
    for symbol in _SIGNAL_HISTORY_SYMBOLS:
        try:
            data     = _get_layer2_data(symbol)
            snapshot = _signal_snapshot(symbol)
            try:
                mechanics = _get_market_mechanics_data(symbol)
            except Exception:
                mechanics = None
            l3_raw = (_layer3_cache.get(symbol) or {}).get("data")
            current_price = (l3_raw or {}).get("price")
            config   = load_signal_config()
            out = evaluator.evaluate_and_persist(
                symbol, snapshot, data, mechanics, None, current_price, config)
            results[symbol] = out["result"]
        except Exception as exc:
            results[symbol] = {"error": str(exc)}
    return jsonify(results)


@app.route("/api/signal_config", methods=["GET"])
def api_signal_config_get():
    """GET /api/signal_config — current signal system configuration."""
    return jsonify(load_signal_config())


@app.route("/api/signal_config", methods=["POST"])
def api_signal_config_save():
    """POST /api/signal_config — update one or more config fields."""
    payload = request.json or {}
    cfg = load_signal_config()
    allowed = {"liq_proximity_pct", "oi_consecutive_cycles",
               "telegram_enabled", "tp_achievability_in_msg"}
    for k, v in payload.items():
        if k in allowed:
            cfg[k] = v
    save_signal_config(cfg)
    return jsonify({"ok": True, "config": cfg})



# ── Routes — Settings (API keys stored in .env) ───────────────────────────────

ENV_PATH = os.path.join(ROOT, ".env")


def _read_env_file() -> dict:
    """Parse .env into a key→value dict, preserving order."""
    pairs = {}
    if not os.path.exists(ENV_PATH):
        return pairs
    with open(ENV_PATH) as f:
        for line in f:
            line = line.rstrip("\n")
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                pairs[k.strip()] = v.strip()
    return pairs


def _write_env_file(pairs: dict):
    """Write a key→value dict back to .env, one KEY=VALUE per line."""
    lines = [f"{k}={v}" for k, v in pairs.items()]
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    masked = (key[:6] + "•" * (len(key) - 10) + key[-4:]) if len(key) > 10 else ("•" * len(key) if key else "")
    return jsonify({
        "anthropic_key_set":    bool(key),
        "anthropic_key_masked": masked,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.json or {}
    new_key = data.get("anthropic_api_key", "").strip()

    if not new_key:
        return jsonify({"error": "API key cannot be empty"}), 400

    # Persist to .env
    pairs = _read_env_file()
    pairs["ANTHROPIC_API_KEY"] = new_key
    _write_env_file(pairs)

    # Apply to running process immediately (no restart needed)
    os.environ["ANTHROPIC_API_KEY"] = new_key

    masked = new_key[:6] + "•" * (len(new_key) - 10) + new_key[-4:] if len(new_key) > 10 else "•" * len(new_key)
    return jsonify({"ok": True, "anthropic_key_masked": masked})


# ── Route — Deploy webhook ────────────────────────────────────────────────────

@app.route("/deploy", methods=["POST"])
def webhook_deploy():
    expected = os.getenv("DEPLOY_TOKEN", "")
    token    = request.headers.get("X-Deploy-Token", "")
    if not expected or token != expected:
        return jsonify({"error": "Unauthorized"}), 401

    def run_deploy():
        subprocess.run(
            ["bash", "-c",
             "cd /root/infinity && git pull origin master "
             "&& source venv/bin/activate "
             "&& pip install -r requirements.txt "
             "&& systemctl restart infinity-web"],
        )

    threading.Thread(target=run_deploy, daemon=True).start()
    return jsonify({"ok": True, "message": "Deploy started"}), 200



# ── Bootstrap ─────────────────────────────────────────────────────────────────

_signal_scheduler = BackgroundScheduler(timezone="UTC")
_signal_scheduler.add_job(_record_layer1_job, "interval", hours=6, id="record_l1")
_signal_scheduler.add_job(_record_layer2_job, "interval", hours=1, id="record_l2")
_signal_scheduler.add_job(_record_layer3_job, "cron", hour=0, minute=0, id="record_l3")
_signal_scheduler.start()


def start():
    port = int(os.getenv("DASHBOARD_PORT", 5050))
    print(f"\n🌐  Dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start()
