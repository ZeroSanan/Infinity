"""
DCA Dashboard — Flask Web Server (multi-account, multi-strategy)
=================================================
Run with:  python3 web/app.py
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ALGO_PATH = os.path.join(os.path.dirname(__file__), "..", "algo-trading")
if _ALGO_PATH not in sys.path:
    sys.path.insert(0, _ALGO_PATH)

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from dotenv import load_dotenv

from binance.exceptions import BinanceAPIException
from core.binance_client import BinanceSpotClient
from core.binance_futures_client import BinanceFuturesClient
from core.dca_engine import DCAEngine
from core.mixed_engine import MixedEngine
from core.regime_detector import RegimeDetector
from core.state_manager import load_state, save_state, reset_state
from models.dca_config import CoinConfig, DCAModel
from utils.calculations import calc_dump_percent, calc_take_profit_price, calc_pnl

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ROOT            = os.path.join(os.path.dirname(__file__), "..")
ACCOUNTS_PATH   = os.path.join(ROOT, "config", "accounts.json")
COINS_PATH      = os.path.join(ROOT, "config", "coins.json")
DCA_MODELS_PATH = os.path.join(ROOT, "config", "dca_models.json")

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# ── Deploy timestamp (captured once at startup) ───────────────────────────────
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

# ── In-memory state ───────────────────────────────────────────────────────────
_clients: dict = {}          # account_id -> BinanceSpotClient
_futures_clients: dict = {}  # account_id -> BinanceFuturesClient
_engines: dict = {}          # account_id -> { strategy_id -> DCAEngine | MixedEngine }
_price_cache: dict = {}      # symbol -> float
_price_lock = threading.Lock()
_coin_configs: list = []   # list[CoinConfig]
_accounts: list = []       # list[dict]  (loaded from accounts.json)
_dca_models: list = []     # list[dict]  (loaded from dca_models.json)

_layer2_cache: dict = {}   # symbol -> {"data": dict, "ts": float}
_LAYER2_TTL = 300  # seconds (5 min)

_layer3_cache: dict = {}   # symbol -> {"data": dict, "ts": float}
_LAYER3_TTL = 120  # seconds (2 min)

_layer1_cache: dict = {}   # {"data": dict, "ts": float}
_LAYER1_TTL = 900  # seconds (15 min)
_btc_dom_history: list = []  # [(timestamp, btc_dominance_pct), ...] rolling 48h window


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_PATH):
        return []
    with open(ACCOUNTS_PATH) as f:
        return json.load(f).get("accounts", [])


def save_accounts(accounts: list):
    with open(ACCOUNTS_PATH, "w") as f:
        json.dump({"accounts": accounts}, f, indent=2)


def load_dca_models() -> list:
    if not os.path.exists(DCA_MODELS_PATH):
        return []
    with open(DCA_MODELS_PATH) as f:
        return json.load(f).get("models", [])


def save_dca_models(models: list):
    with open(DCA_MODELS_PATH, "w") as f:
        json.dump({"models": models}, f, indent=2)


def load_coin_configs() -> list:
    if not os.path.exists(COINS_PATH):
        return []
    with open(COINS_PATH) as f:
        data = json.load(f)

    changed = False
    out = []
    coins = data.get("coins", [])
    for item in coins:
        # Auto-migrate: add id + name if missing
        if not item.get("id"):
            item["id"] = str(uuid.uuid4())[:8]
            changed = True
        if not item.get("name"):
            item["name"] = item["coin"]
            changed = True
        try:
            cfg = CoinConfig(
                id=item["id"],
                name=item["name"],
                coin=item["coin"],
                symbol=item["symbol"],
                enabled=item.get("enabled", True),
                step_count=item["step_count"],
                dump_levels=item["dump_levels"],
                order_sizes=item["order_sizes"],
                take_profit_percent=item["take_profit_percent"],
                reference_price=item.get("reference_price"),
                mode=item.get("mode", "long"),
                bear_levels=item.get("bear_levels", []),
                bear_order_sizes=item.get("bear_order_sizes", []),
                bear_take_profit_percent=item.get("bear_take_profit_percent", 0.0),
                bull_stop_loss_percent=item.get("bull_stop_loss_percent", 0.0),
                bear_stop_loss_percent=item.get("bear_stop_loss_percent", 0.0),
                leverage=item.get("leverage", 1),
                use_entry_indicator=item.get("use_entry_indicator", True),
                rsi_overbought=item.get("rsi_overbought", 70.0),
                rsi_oversold=item.get("rsi_oversold", 30.0),
                regime_interval=item.get("regime_interval", "1h"),
                atr_based_spacing=item.get("atr_based_spacing", False),
                atr_period=item.get("atr_period", 14),
            )
            cfg.validate()
        except (KeyError, ValueError) as e:
            print(f"⚠️  Skipping invalid coin config '{item.get('name', item.get('id'))}': {e}")
            continue
        out.append(cfg)

    if changed:
        data["coins"] = coins
        with open(COINS_PATH, "w") as f:
            json.dump(data, f, indent=2)

    return out


# ── Client management ─────────────────────────────────────────────────────────

def get_client(account_id: str) -> Optional[BinanceSpotClient]:
    if account_id in _clients:
        return _clients[account_id]
    acct = next((a for a in _accounts if a["id"] == account_id), None)
    if not acct:
        return None
    try:
        client = BinanceSpotClient(acct["api_key"], acct["secret_key"], testnet=acct.get("testnet", False))
        _clients[account_id] = client
        return client
    except Exception:
        return None


def get_futures_client(account_id: str) -> Optional[BinanceFuturesClient]:
    """Return (and cache) a USD-M Futures client for the given account."""
    if account_id in _futures_clients:
        return _futures_clients[account_id]
    acct = next((a for a in _accounts if a["id"] == account_id), None)
    if not acct:
        return None
    try:
        client = BinanceFuturesClient(acct["api_key"], acct["secret_key"], testnet=acct.get("testnet", False))
        _futures_clients[account_id] = client
        return client
    except Exception:
        return None


def connect_account(account_id: str) -> tuple:
    """Returns (client | None, error_str | None)"""
    acct = next((a for a in _accounts if a["id"] == account_id), None)
    if not acct:
        return None, "Account not found"
    try:
        client = BinanceSpotClient(acct["api_key"], acct["secret_key"], testnet=acct.get("testnet", False))
        _clients[account_id] = client
        bal = client.get_balance("USDT")
        return client, None
    except Exception as e:
        return None, str(e)


def mask_key(k: str) -> str:
    if not k or k.startswith("your_"):
        return ""
    return k[:6] + "•" * (len(k) - 10) + k[-4:] if len(k) > 10 else "•" * len(k)


def _friendly_error(e: Exception) -> str:
    """Return a human-readable error string, with extra detail for known Binance codes."""
    if isinstance(e, BinanceAPIException):
        msg = e.message or str(e)
        if e.code == -2015:
            return (
                "IP address not whitelisted (Binance error -2015). "
                "Go to Binance → API Management → Edit your key → "
                "set IP Access to 'Unrestricted', or add your current IP to the whitelist. "
                "Also confirm you're using the correct key type (Testnet vs Live)."
            )
        if e.code == -2014:
            return "Invalid API key format — check you copied the full key without extra spaces."
        return f"Binance error {e.code}: {msg}"
    return str(e)


# ── Price poller ──────────────────────────────────────────────────────────────

def _price_poller():
    while True:
        symbols_seen = set()
        for cfg in _coin_configs:
            if cfg.symbol in symbols_seen:
                continue
            for aid, client in list(_clients.items()):
                try:
                    p = client.get_price(cfg.symbol)
                    with _price_lock:
                        _price_cache[cfg.symbol] = p
                    symbols_seen.add(cfg.symbol)
                    break
                except Exception:
                    pass
        time.sleep(5)


# ── Coin summary helper ───────────────────────────────────────────────────────

def _coin_summary(cfg: CoinConfig) -> dict:
    with _price_lock:
        price = _price_cache.get(cfg.symbol, 0.0)

    state = load_state(cfg.id, cfg.coin, cfg.symbol)

    dump_pct = tp_price = pnl_usdt = pnl_pct = None
    liq_distance_pct = None

    if state.reference_price and price:
        dump_pct = calc_dump_percent(state.reference_price, price)

    is_mixed = cfg.mode == "mixed"
    is_short = is_mixed and state.direction == "SHORT"

    if state.status == "ACTIVE" and state.average_entry:
        if is_short:
            tp_price = state.average_entry * (1 - cfg.bear_take_profit_percent / 100)
            if price:
                pnl_usdt = (state.average_entry - price) * state.total_quantity
                pnl_pct = (pnl_usdt / state.total_invested * 100) if state.total_invested else 0.0
        else:
            tp_price = calc_take_profit_price(state.average_entry, cfg.take_profit_percent)
            if price:
                pnl_usdt, pnl_pct = calc_pnl(state.average_entry, price, state.total_quantity)

    if state.liquidation_price and price:
        if is_short:
            liq_distance_pct = (state.liquidation_price - price) / price * 100
        else:
            liq_distance_pct = (price - state.liquidation_price) / price * 100

    # Which ladder to display: bear ladder while SHORT (or armed for a SELL
    # entry), bull ladder otherwise (default for "long" mode too).
    if is_mixed and (is_short or state.active_mode == "SELL"):
        levels, sizes, tp_pct = cfg.bear_levels, cfg.bear_order_sizes, cfg.bear_take_profit_percent
        total_capital = cfg.total_capital_bear
    else:
        levels, sizes, tp_pct = cfg.dump_levels, cfg.order_sizes, cfg.take_profit_percent
        total_capital = cfg.total_capital

    steps = []
    exec_map = {s.step_index: s for s in state.executed_steps}
    for i, (level, size) in enumerate(zip(levels, sizes)):
        ex = exec_map.get(i)
        steps.append({
            "index": i, "dump_level": level, "order_size": size,
            "hit": ex is not None,
            "entry_price": ex.entry_price if ex else None,
            "quantity":    ex.quantity    if ex else None,
            "order_id":    ex.order_id    if ex else None,
            "timestamp":   ex.timestamp   if ex else None,
        })

    # Is an engine running for this strategy?
    engine_running = any(cfg.id in engines for engines in _engines.values())
    engine_account = next(
        (aid for aid, engines in _engines.items() if cfg.id in engines),
        None
    )

    return {
        "strategy_id":    cfg.id,
        "strategy_name":  cfg.name,
        "coin":           cfg.coin,
        "symbol":         cfg.symbol,
        "enabled":        cfg.enabled,
        "status":         state.status,
        "engine_running": engine_running,
        "engine_account": engine_account,
        "price":          price,
        "reference_price": state.reference_price,
        "dump_pct":       dump_pct,
        "average_entry":  state.average_entry  if state.status == "ACTIVE" else None,
        "total_invested": state.total_invested  if state.status == "ACTIVE" else None,
        "total_quantity": state.total_quantity  if state.status == "ACTIVE" else None,
        "tp_price":       tp_price,
        "tp_percent":     tp_pct,
        "pnl_usdt":       pnl_usdt,
        "pnl_pct":        pnl_pct,
        "steps_done":     state.steps_done,
        "step_count":     len(levels),
        "steps":          steps,
        "total_capital":  total_capital,
        # ── Mixed long/short engine (Binance USD-M Futures) ────────────────
        "mode":              cfg.mode,
        "direction":         state.direction,
        "active_mode":       state.active_mode,
        "regime":            state.regime,
        "anchor_price":      state.anchor_price,
        "leverage":          cfg.leverage if is_mixed else None,
        "liquidation_price": state.liquidation_price,
        "liq_distance_pct":  liq_distance_pct,
        "atr_based_spacing": cfg.atr_based_spacing if is_mixed else False,
        "atr_period":        cfg.atr_period if is_mixed else None,
    }


# ── Routes — Dashboard ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    global _coin_configs
    any_connected = bool(_clients)
    usdt_totals = {}
    for aid, client in list(_clients.items()):
        try:
            usdt_totals[aid] = client.get_balance("USDT")
        except Exception:
            pass

    # Reload from coins.json so newly-added/edited strategies appear on the
    # dashboard immediately, even in other worker processes.
    try:
        _coin_configs = load_coin_configs()
    except Exception as e:
        print(f"⚠️  Failed to reload coin configs: {e}")

    coins = [_coin_summary(cfg) for cfg in _coin_configs]
    return jsonify({
        "connected": any_connected,
        "usdt_totals": usdt_totals,
        "coins": coins,
        "account_count": len(_accounts),
        "deploy_time": _DEPLOY_TIME,
    })


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    """Send a test message to verify TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
    from core.telegram_notifier import is_configured, send_message
    if not is_configured():
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}), 400
    ok = send_message("✅ Infinity dashboard is connected to this chat.")
    if not ok:
        return jsonify({"ok": False, "error": "Telegram API request failed"}), 502
    return jsonify({"ok": True})


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


def _layer2_verdict(funding: Optional[dict], oi: Optional[dict], ls: Optional[dict]) -> dict:
    """Combine funding, open interest, and long/short ratio into one verdict."""
    funding_high = bool(funding and funding["current"] > 0.0005)
    funding_negative = bool(funding and funding["current"] < -0.0001)
    longs_crowded = bool(ls and ls["global"]["long_pct"] > 65)
    shorts_crowded = bool(ls and ls["global"]["short_pct"] > 65)

    if funding_high and longs_crowded:
        return {"code": "CAUTION_LONG", "label": "CAUTION LONG", "emoji": "🔴", "color": "red"}
    if funding_negative and shorts_crowded:
        return {"code": "CAUTION_SHORT", "label": "CAUTION SHORT", "emoji": "🔵", "color": "blue"}

    bullish = sum([
        funding_negative,
        shorts_crowded,
        bool(oi and oi["label"].startswith("Strong —")),
    ])
    bearish = sum([
        funding_high,
        longs_crowded,
        bool(oi and oi["label"].startswith("Strong Selling")),
    ])

    if bullish and bearish:
        return {"code": "MIXED", "label": "MIXED", "emoji": "🟡", "color": "yellow"}
    return {"code": "NEUTRAL", "label": "NEUTRAL", "emoji": "🟢", "color": "green"}


@app.route("/api/layer2/<symbol>")
def api_layer2(symbol):
    """Layer 2 (Market Positioning): funding rate, open interest, and
    long/short ratios from Binance public futures data, plus a combined
    verdict. Cached per symbol for _LAYER2_TTL seconds."""
    symbol = symbol.upper()
    now = time.time()
    cached = _layer2_cache.get(symbol)
    if cached and now - cached["ts"] < _LAYER2_TTL:
        return jsonify(cached["data"])

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

    result["verdict"] = _layer2_verdict(
        result["funding"] if "error" not in result["funding"] else None,
        result["open_interest"] if "error" not in result["open_interest"] else None,
        result["long_short"] if "error" not in result["long_short"] else None,
    )
    result["timestamp"] = int(now * 1000)

    _layer2_cache[symbol] = {"data": result, "ts": now}
    return jsonify(result)


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


@app.route("/api/layer3/<symbol>")
def api_layer3(symbol):
    """Layer 3 (Entry Timing): volume divergence, price structure, momentum,
    order book imbalance, and ATR volatility from Binance public API.
    Cached per symbol for _LAYER3_TTL seconds."""
    symbol = symbol.upper()
    now    = time.time()
    cached = _layer3_cache.get(symbol)
    if cached and now - cached["ts"] < _LAYER3_TTL:
        return jsonify(cached["data"])

    result: dict = {"symbol": symbol}

    try:
        klines = _layer3_klines(symbol, limit=21)
    except Exception as exc:
        result["error"]     = str(exc)
        result["timestamp"] = int(now * 1000)
        return jsonify(result)

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
    return jsonify(result)


# ── AI Analysis route ──────────────────────────────────────────────────────────

@app.route("/api/ai/analysis", methods=["POST"])
def api_ai_analysis():
    """Generate a professional trading analysis via Claude API from dashboard data."""
    import anthropic as _anthropic

    payload = request.get_json(force=True, silent=True) or {}
    coin    = payload.get("coin", "BTC")
    price   = payload.get("price")
    master  = payload.get("master_verdict", "Unknown")
    l1      = payload.get("layer1", {})
    l2      = payload.get("layer2", {})
    l3      = payload.get("layer3", {})

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
    lines.extend(["", "Please provide your professional analysis."])

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
        # Insert before the "Please provide..." line that was already added
        lines = lines[:-2] + dca_lines + ["", "Please provide your professional analysis."]

    user_msg = "\n".join(lines)

    system_prompt = (
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

    if dca:
        system_prompt += (
            "\n\nWhen DCA model levels are provided, incorporate them specifically into your "
            "SUGGESTION section. Comment on whether the step placement makes sense given "
            "the current ATR and any liquidation clusters. Flag any step sitting above a "
            "cluster and recommend the adjusted multiplier. Reference specific dollar "
            "levels ('$65,785'), not vague descriptions."
        )

    try:
        client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg    = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return jsonify({"ok": True, "text": msg.content[0].text})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── DCA Model Level Visualizer route ──────────────────────────────────────────

@app.route("/api/market/dca-levels", methods=["POST"])
def api_market_dca_levels():
    """Calculate ATR-calibrated DCA entry levels anchored to live price.
    No caching — real-time price on every call."""
    import requests as _req

    payload    = request.get_json(force=True, silent=True) or {}
    symbol     = payload.get("symbol", "BTCUSDT").upper()
    model_id   = payload.get("model_id", "")
    side       = payload.get("side", "long").lower()
    multiplier = float(payload.get("multiplier", 1.5))
    liq_below  = payload.get("liq_cluster_below")
    liq_above  = payload.get("liq_cluster_above")

    model = next((m for m in _dca_models if m.get("id") == model_id), None)
    if not model:
        return jsonify({"error": "Model not found"}), 404

    # Live price
    try:
        r = _req.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=6,
        )
        r.raise_for_status()
        current_price = float(r.json()["price"])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    # ATR from 4h klines (reuse Layer 3 helper)
    try:
        klines   = _layer3_klines(symbol, limit=15)
        atr_data = _layer3_atr(klines)
        atr      = atr_data["atr"]
        atr_pct  = atr_data["atr_pct"]
    except Exception as exc:
        return jsonify({"error": f"ATR: {exc}"}), 502

    if side == "long":
        order_sizes = model.get("bull_order_sizes", [])
        tp_pct      = float(model.get("bull_take_profit_percent", 5.0))
        direction   = -1
    else:
        order_sizes = model.get("bear_order_sizes", [])
        tp_pct      = float(model.get("bear_take_profit_percent", 5.0))
        direction   = 1

    if not order_sizes:
        return jsonify({"error": f"Model has no {side} order sizes"}), 400

    steps: list[dict] = []
    for i, size in enumerate(order_sizes):
        step_pct   = (i + 1) * atr_pct * multiplier
        step_price = round(current_price * (1 + direction * step_pct / 100), 2)
        pct_from   = round((step_price - current_price) / current_price * 100, 2)

        warning = None
        if side == "long" and liq_below is not None:
            lb = float(liq_below)
            if lb > 0:
                if step_price > lb:
                    warning = f"⚠️ Above ${lb:,.0f} cluster"
                elif step_price >= lb * 0.995:
                    warning = f"🎯 Just below ${lb:,.0f} cluster"
        if side == "short" and liq_above is not None:
            la = float(liq_above)
            if la > 0:
                if step_price < la:
                    warning = f"⚠️ Below ${la:,.0f} cluster"
                elif step_price <= la * 1.005:
                    warning = f"🎯 Just above ${la:,.0f} cluster"

        steps.append({
            "index": i + 1, "price": step_price,
            "pct_from_anchor": pct_from, "size_usdt": size,
            "cluster_warning": warning,
        })

    total_capital = sum(s["size_usdt"] for s in steps)
    total_coins   = sum(s["size_usdt"] / s["price"] for s in steps if s["price"] > 0)
    avg_entry     = round(total_capital / total_coins, 2) if total_coins > 0 else current_price
    tp_sign       = 1 if side == "long" else -1
    tp_price      = round(avg_entry * (1 + tp_sign * tp_pct / 100), 2)
    tp_from_avg   = round(tp_sign * tp_pct, 2)
    tp_from_cur   = round((tp_price - current_price) / current_price * 100, 2)

    warn_steps = [s for s in steps if (s.get("cluster_warning") or "").startswith("⚠️")]
    suggested  = None
    if warn_steps and atr_pct > 0:
        if side == "long" and liq_below:
            lb = float(liq_below)
            target_pct = (current_price - lb * 0.995) / current_price * 100
            suggested  = round(max(0.5, min(3.0, target_pct / atr_pct)), 1)
        elif side == "short" and liq_above:
            la = float(liq_above)
            target_pct = (la * 1.005 - current_price) / current_price * 100
            suggested  = round(max(0.5, min(3.0, target_pct / atr_pct)), 1)

    return jsonify({
        "current_price": current_price, "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 4), "anchor": current_price,
        "side": side, "multiplier": multiplier, "steps": steps,
        "avg_entry": avg_entry, "tp_price": tp_price,
        "tp_pct_from_avg": tp_from_avg, "tp_pct_from_current": tp_from_cur,
        "total_capital": total_capital,
        "cluster_warnings": warn_steps, "suggested_multiplier": suggested,
    })


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


@app.route("/api/layer1")
def api_layer1():
    """Layer 1 (Macro Environment): Fear & Greed, BTC Dominance, DXY, Fed
    Funds Rate, 10Y Treasury Yield, CPI, and VIX, plus a combined macro
    verdict. Cached for _LAYER1_TTL seconds."""
    now = time.time()
    cached = _layer1_cache.get("data")
    if cached and now - _layer1_cache["ts"] < _LAYER1_TTL:
        return jsonify(cached)

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
    return jsonify(result)


@app.route("/api/running")
def api_running():
    """Return all currently running engine sessions."""
    acct_map = {a["id"]: a["name"] for a in _accounts}
    result = []
    for aid, engines in _engines.items():
        for strategy_id, engine in engines.items():
            result.append({
                "account_id":    aid,
                "account_name":  acct_map.get(aid, aid),
                "strategy_id":   strategy_id,
                "strategy_name": engine.cfg.name,
                "coin":          engine.cfg.coin,
                "symbol":        engine.cfg.symbol,
                "status":        engine.state.status,
                "steps_done":    engine.state.steps_done,
                "step_count":    engine.cfg.step_count,
            })
    return jsonify({"running": result})


@app.route("/api/price/<symbol>")
def api_price(symbol):
    """Return current price for a symbol — tries cache first, then live fetch."""
    symbol = symbol.upper()
    with _price_lock:
        cached = _price_cache.get(symbol)
    if cached:
        return jsonify({"symbol": symbol, "price": cached})
    # Live fetch via any connected client
    for client in list(_clients.values()):
        try:
            p = client.get_price(symbol)
            with _price_lock:
                _price_cache[symbol] = p
            return jsonify({"symbol": symbol, "price": p})
        except Exception:
            pass
    return jsonify({"error": "Price unavailable — no connected account or unknown symbol"}), 503


@app.route("/api/set_reference", methods=["POST"])
def api_set_reference():
    data        = request.json or {}
    strategy_id = data.get("strategy_id", "")
    price       = data.get("price")

    cfg = next((c for c in _coin_configs if c.id == strategy_id), None)
    if not cfg:
        return jsonify({"error": "Unknown strategy"}), 404

    if price is None:
        client = next(iter(_clients.values()), None)
        if not client:
            return jsonify({"error": "No Binance account connected"}), 503
        price = client.get_price(cfg.symbol)

    state = load_state(cfg.id, cfg.coin, cfg.symbol)
    state.reference_price = float(price)
    save_state(state)

    # Update live engine if running
    for engines in _engines.values():
        if cfg.id in engines:
            engines[cfg.id].state.reference_price = float(price)

    return jsonify({"ok": True, "reference_price": float(price)})


@app.route("/api/start_engine", methods=["POST"])
def api_start_engine():
    data            = request.json or {}
    strategy_id     = data.get("strategy_id", "")
    account_id      = data.get("account_id", "")
    reference_price = data.get("reference_price")   # entry point set in Start modal

    cfg = next((c for c in _coin_configs if c.id == strategy_id), None)
    if not cfg:
        return jsonify({"error": "Unknown strategy"}), 404

    if not account_id:
        account_id = _accounts[0]["id"] if _accounts else None
    if not account_id:
        return jsonify({"error": "No Binance account connected"}), 503

    # Check not already running
    for aid, engines in _engines.items():
        if strategy_id in engines:
            return jsonify({"ok": True, "message": f"Already running on account {aid}"})

    if cfg.mode == "mixed":
        client = get_futures_client(account_id)
        if not client:
            return jsonify({"error": "Could not connect futures account"}), 503
        engine = MixedEngine(cfg, client)
    else:
        client = get_client(account_id)
        if not client:
            return jsonify({"error": "Could not connect account"}), 503
        engine = DCAEngine(cfg, client)

    # Apply entry point (reference price) — overrides whatever was persisted
    if reference_price is not None:
        try:
            engine.state.reference_price = float(reference_price)
            save_state(engine.state)
        except (TypeError, ValueError):
            pass

    _engines.setdefault(account_id, {})[strategy_id] = engine

    t = threading.Thread(target=engine.run, daemon=True)
    t.start()

    return jsonify({"ok": True, "message": f"Engine started for {cfg.name}",
                    "reference_price": engine.state.reference_price})


@app.route("/api/stop_engine", methods=["POST"])
def api_stop_engine():
    data        = request.json or {}
    strategy_id = data.get("strategy_id", "")

    cfg = next((c for c in _coin_configs if c.id == strategy_id), None)
    if not cfg:
        return jsonify({"error": "Unknown strategy"}), 404

    for engines in _engines.values():
        if strategy_id in engines:
            del engines[strategy_id]
            break

    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    data        = request.json or {}
    strategy_id = data.get("strategy_id", "")

    cfg = next((c for c in _coin_configs if c.id == strategy_id), None)
    if not cfg:
        return jsonify({"error": "Unknown strategy"}), 404

    state = load_state(cfg.id, cfg.coin, cfg.symbol)
    reset_state(state)
    return jsonify({"ok": True})


# ── Routes — Accounts ─────────────────────────────────────────────────────────

@app.route("/api/accounts", methods=["GET"])
def api_accounts_get():
    result = []
    for acct in _accounts:
        aid = acct["id"]
        connected = aid in _clients
        bal = None
        if connected:
            try: bal = _clients[aid].get_balance("USDT")
            except Exception: connected = False
        result.append({
            "id":             aid,
            "name":           acct.get("name", "Unnamed"),
            "api_key_masked": mask_key(acct.get("api_key", "")),
            "testnet":        acct.get("testnet", False),
            "connected":      connected,
            "usdt_balance":   bal,
            "engine_count":   len(_engines.get(aid, {})),
        })
    return jsonify({"accounts": result})


@app.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    global _accounts
    data       = request.json or {}
    name       = data.get("name", "").strip()
    api_key    = data.get("api_key", "").strip()
    secret_key = data.get("secret_key", "").strip()
    testnet    = bool(data.get("testnet", False))

    if not name:       return jsonify({"error": "Account name is required"}), 400
    if not api_key:    return jsonify({"error": "API key is required"}), 400
    if not secret_key: return jsonify({"error": "Secret key is required"}), 400

    new_acct = {
        "id":         str(uuid.uuid4())[:8],
        "name":       name,
        "api_key":    api_key,
        "secret_key": secret_key,
        "testnet":    testnet,
    }

    # Test connection before saving
    try:
        client = BinanceSpotClient(api_key, secret_key, testnet=testnet)
        bal = client.get_balance("USDT")
        _accounts.append(new_acct)
        save_accounts(_accounts)
        _clients[new_acct["id"]] = client
        return jsonify({"ok": True, "id": new_acct["id"], "usdt_balance": bal})
    except Exception as e:
        return jsonify({"error": _friendly_error(e)}), 400


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_accounts_update(account_id):
    global _accounts
    data = request.json or {}
    acct = next((a for a in _accounts if a["id"] == account_id), None)
    if not acct:
        return jsonify({"error": "Account not found"}), 404

    if data.get("name"):        acct["name"]       = data["name"].strip()
    if data.get("api_key"):     acct["api_key"]    = data["api_key"].strip()
    if data.get("secret_key"):  acct["secret_key"] = data["secret_key"].strip()
    if "testnet" in data:       acct["testnet"]    = bool(data["testnet"])

    save_accounts(_accounts)

    # Force reconnect
    _clients.pop(account_id, None)
    try:
        client = BinanceSpotClient(acct["api_key"], acct["secret_key"], testnet=acct.get("testnet", False))
        _clients[account_id] = client
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)}), 400

    bal = client.get_balance("USDT")
    return jsonify({"ok": True, "usdt_balance": bal})


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_accounts_delete(account_id):
    global _accounts
    before = len(_accounts)
    _accounts = [a for a in _accounts if a["id"] != account_id]
    if len(_accounts) == before:
        return jsonify({"error": "Account not found"}), 404

    save_accounts(_accounts)
    _clients.pop(account_id, None)
    _engines.pop(account_id, None)
    return jsonify({"ok": True})


@app.route("/api/accounts/<account_id>/test", methods=["POST"])
def api_accounts_test(account_id):
    """Test connection for a saved account."""
    acct = next((a for a in _accounts if a["id"] == account_id), None)
    if not acct:
        return jsonify({"error": "Account not found"}), 404
    try:
        client = BinanceSpotClient(acct["api_key"], acct["secret_key"], testnet=acct.get("testnet", False))
        _clients[account_id] = client
        bal = client.get_balance("USDT")
        return jsonify({"ok": True, "usdt_balance": bal})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


@app.route("/api/accounts/test_new", methods=["POST"])
def api_accounts_test_new():
    """Test unsaved credentials."""
    data       = request.json or {}
    api_key    = data.get("api_key", "").strip()
    secret_key = data.get("secret_key", "").strip()
    testnet    = bool(data.get("testnet", False))
    if not api_key or not secret_key:
        return jsonify({"error": "Both keys required"}), 400
    try:
        client = BinanceSpotClient(api_key, secret_key, testnet=testnet)
        bal = client.get_balance("USDT")
        return jsonify({"ok": True, "usdt_balance": bal})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


# ── Routes — Strategies ───────────────────────────────────────────────────────

@app.route("/api/strategies", methods=["GET"])
def api_strategies_get():
    if not os.path.exists(COINS_PATH):
        return jsonify({"coins": []})
    with open(COINS_PATH) as f:
        return jsonify(json.load(f))


@app.route("/api/strategies", methods=["POST"])
def api_strategies_create():
    """Create a new strategy — always appends, never replaces."""
    global _coin_configs
    data = request.json or {}

    required = ["coin", "symbol", "step_count", "dump_levels", "order_sizes", "take_profit_percent"]
    for fld in required:
        if fld not in data:
            return jsonify({"error": f"Missing field: {fld}"}), 400

    if len(data["dump_levels"]) != data["step_count"]:
        return jsonify({"error": "dump_levels count must match step_count"}), 400
    if len(data["order_sizes"]) != data["step_count"]:
        return jsonify({"error": "order_sizes count must match step_count"}), 400
    if any(d >= 0 for d in data["dump_levels"]):
        return jsonify({"error": "All dump_levels must be negative"}), 400

    coin = data["coin"].upper()
    name = data.get("name", "").strip() or coin

    with open(COINS_PATH) as f:
        cfg_data = json.load(f)

    new_entry = {
        "id":                  str(uuid.uuid4())[:8],
        "name":                name,
        "coin":                coin,
        "symbol":              data["symbol"].upper(),
        "enabled":             bool(data.get("enabled", True)),
        "step_count":          int(data["step_count"]),
        "dump_levels":         [float(x) for x in data["dump_levels"]],
        "order_sizes":         [float(x) for x in data["order_sizes"]],
        "take_profit_percent": float(data["take_profit_percent"]),
        "reference_price":     data.get("reference_price") or None,
    }

    cfg_data.setdefault("coins", []).append(new_entry)
    with open(COINS_PATH, "w") as f:
        json.dump(cfg_data, f, indent=2)

    _coin_configs = load_coin_configs()
    return jsonify({"ok": True, "id": new_entry["id"], "name": name, "coin": coin})


@app.route("/api/strategies/<strategy_id>", methods=["PUT"])
def api_strategies_update(strategy_id):
    """Update an existing strategy by id."""
    global _coin_configs
    data = request.json or {}

    with open(COINS_PATH) as f:
        cfg_data = json.load(f)

    coins = cfg_data.get("coins", [])
    idx = next((i for i, c in enumerate(coins) if c.get("id") == strategy_id), None)
    if idx is None:
        return jsonify({"error": "Strategy not found"}), 404

    entry = coins[idx]
    if "name" in data:                entry["name"]                = data["name"].strip() or entry["name"]
    if "coin" in data:                entry["coin"]                = data["coin"].upper()
    if "symbol" in data:              entry["symbol"]              = data["symbol"].upper()
    if "enabled" in data:             entry["enabled"]             = bool(data["enabled"])
    if "step_count" in data:          entry["step_count"]          = int(data["step_count"])
    if "dump_levels" in data:         entry["dump_levels"]         = [float(x) for x in data["dump_levels"]]
    if "order_sizes" in data:         entry["order_sizes"]         = [float(x) for x in data["order_sizes"]]
    if "take_profit_percent" in data: entry["take_profit_percent"] = float(data["take_profit_percent"])
    if "reference_price" in data:     entry["reference_price"]     = data["reference_price"] or None

    # ── Mixed long/short engine fields ──────────────────────────────────────
    if "mode" in data:                     entry["mode"]                     = data["mode"]
    if "bear_levels" in data:              entry["bear_levels"]              = [float(x) for x in data["bear_levels"]]
    if "bear_order_sizes" in data:         entry["bear_order_sizes"]         = [float(x) for x in data["bear_order_sizes"]]
    if "bear_take_profit_percent" in data: entry["bear_take_profit_percent"] = float(data["bear_take_profit_percent"])
    if "bull_stop_loss_percent" in data:   entry["bull_stop_loss_percent"]   = float(data["bull_stop_loss_percent"])
    if "bear_stop_loss_percent" in data:   entry["bear_stop_loss_percent"]   = float(data["bear_stop_loss_percent"])
    if "leverage" in data:                 entry["leverage"]                 = int(data["leverage"])
    if "use_entry_indicator" in data:      entry["use_entry_indicator"]      = bool(data["use_entry_indicator"])
    if "rsi_overbought" in data:           entry["rsi_overbought"]           = float(data["rsi_overbought"])
    if "rsi_oversold" in data:             entry["rsi_oversold"]             = float(data["rsi_oversold"])
    if "regime_interval" in data:          entry["regime_interval"]          = data["regime_interval"]
    if "atr_based_spacing" in data:        entry["atr_based_spacing"]        = bool(data["atr_based_spacing"])
    if "atr_period" in data:               entry["atr_period"]               = int(data["atr_period"])

    coins[idx] = entry
    cfg_data["coins"] = coins
    with open(COINS_PATH, "w") as f:
        json.dump(cfg_data, f, indent=2)

    _coin_configs = load_coin_configs()
    return jsonify({"ok": True})


@app.route("/api/strategies/<strategy_id>", methods=["DELETE"])
def api_strategies_delete(strategy_id):
    global _coin_configs
    with open(COINS_PATH) as f:
        cfg_data = json.load(f)

    before = len(cfg_data.get("coins", []))
    cfg_data["coins"] = [c for c in cfg_data.get("coins", []) if c.get("id") != strategy_id]

    if len(cfg_data["coins"]) == before:
        return jsonify({"error": f"Strategy '{strategy_id}' not found"}), 404

    with open(COINS_PATH, "w") as f:
        json.dump(cfg_data, f, indent=2)

    _coin_configs = load_coin_configs()
    return jsonify({"ok": True})


# ── Routes — DCA Models ───────────────────────────────────────────────────────

@app.route("/api/dca_models", methods=["GET"])
def api_dca_models_get():
    return jsonify({"models": _dca_models})


@app.route("/api/dca_models", methods=["POST"])
def api_dca_models_create():
    """Create a new DCA Model template."""
    global _dca_models
    data = request.json or {}

    required = ["name", "bull_levels", "bull_order_sizes", "bull_take_profit_percent",
                 "bear_levels", "bear_order_sizes", "bear_take_profit_percent"]
    for fld in required:
        if fld not in data:
            return jsonify({"error": f"Missing field: {fld}"}), 400

    name = data["name"].strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    try:
        model = DCAModel(
            id=str(uuid.uuid4())[:8],
            name=name,
            bull_levels=[float(x) for x in data["bull_levels"]],
            bull_order_sizes=[float(x) for x in data["bull_order_sizes"]],
            bull_take_profit_percent=float(data["bull_take_profit_percent"]),
            bear_levels=[float(x) for x in data["bear_levels"]],
            bear_order_sizes=[float(x) for x in data["bear_order_sizes"]],
            bear_take_profit_percent=float(data["bear_take_profit_percent"]),
            leverage=int(data.get("leverage", 1)),
            bull_stop_loss_percent=float(data.get("bull_stop_loss_percent", 0.0)),
            bear_stop_loss_percent=float(data.get("bear_stop_loss_percent", 0.0)),
            use_entry_indicator=bool(data.get("use_entry_indicator", True)),
            rsi_overbought=float(data.get("rsi_overbought", 70.0)),
            rsi_oversold=float(data.get("rsi_oversold", 30.0)),
            regime_interval=data.get("regime_interval", "1h"),
            atr_based_spacing=bool(data.get("atr_based_spacing", False)),
            atr_period=int(data.get("atr_period", 14)),
        )
        model.validate()
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    _dca_models.append(asdict(model))
    save_dca_models(_dca_models)
    return jsonify({"ok": True, "id": model.id, "name": model.name})


@app.route("/api/dca_models/<model_id>", methods=["PUT"])
def api_dca_models_update(model_id):
    """Update an existing DCA Model template."""
    global _dca_models
    data = request.json or {}

    idx = next((i for i, m in enumerate(_dca_models) if m.get("id") == model_id), None)
    if idx is None:
        return jsonify({"error": "Model not found"}), 404

    entry = dict(_dca_models[idx])
    try:
        if "name" in data:
            new_name = data["name"].strip()
            entry["name"] = new_name or entry["name"]
        if "bull_levels" in data:
            entry["bull_levels"] = [float(x) for x in data["bull_levels"]]
        if "bull_order_sizes" in data:
            entry["bull_order_sizes"] = [float(x) for x in data["bull_order_sizes"]]
        if "bull_take_profit_percent" in data:
            entry["bull_take_profit_percent"] = float(data["bull_take_profit_percent"])
        if "bear_levels" in data:
            entry["bear_levels"] = [float(x) for x in data["bear_levels"]]
        if "bear_order_sizes" in data:
            entry["bear_order_sizes"] = [float(x) for x in data["bear_order_sizes"]]
        if "bear_take_profit_percent" in data:
            entry["bear_take_profit_percent"] = float(data["bear_take_profit_percent"])
        if "leverage" in data:
            entry["leverage"] = int(data["leverage"])
        if "bull_stop_loss_percent" in data:
            entry["bull_stop_loss_percent"] = float(data["bull_stop_loss_percent"])
        if "bear_stop_loss_percent" in data:
            entry["bear_stop_loss_percent"] = float(data["bear_stop_loss_percent"])
        if "use_entry_indicator" in data:
            entry["use_entry_indicator"] = bool(data["use_entry_indicator"])
        if "rsi_overbought" in data:
            entry["rsi_overbought"] = float(data["rsi_overbought"])
        if "rsi_oversold" in data:
            entry["rsi_oversold"] = float(data["rsi_oversold"])
        if "regime_interval" in data:
            entry["regime_interval"] = data["regime_interval"]
        if "atr_based_spacing" in data:
            entry["atr_based_spacing"] = bool(data["atr_based_spacing"])
        if "atr_period" in data:
            entry["atr_period"] = int(data["atr_period"])

        DCAModel(**entry).validate()
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    _dca_models[idx] = entry
    save_dca_models(_dca_models)
    return jsonify({"ok": True})


@app.route("/api/dca_models/<model_id>", methods=["DELETE"])
def api_dca_models_delete(model_id):
    global _dca_models
    before = len(_dca_models)
    _dca_models = [m for m in _dca_models if m.get("id") != model_id]
    if len(_dca_models) == before:
        return jsonify({"error": "Model not found"}), 404

    save_dca_models(_dca_models)
    return jsonify({"ok": True})


@app.route("/api/dca_models/<model_id>/apply", methods=["POST"])
def api_dca_models_apply(model_id):
    """
    Apply a DCA Model template to one or more coin cards, switching them to
    mode="mixed" (auto long/short on Binance USD-M Futures).

    Body: { "strategy_ids": [...] }  or  { "apply_to_all": true }
    """
    global _coin_configs
    data = request.json or {}

    model_dict = next((m for m in _dca_models if m.get("id") == model_id), None)
    if not model_dict:
        return jsonify({"error": "Model not found"}), 404

    strategy_ids = data.get("strategy_ids") or []
    apply_to_all = bool(data.get("apply_to_all", False))

    with open(COINS_PATH) as f:
        cfg_data = json.load(f)
    coins = cfg_data.get("coins", [])

    if apply_to_all:
        targets = coins
    else:
        if not strategy_ids:
            return jsonify({"error": "strategy_ids or apply_to_all required"}), 400
        targets = [c for c in coins if c.get("id") in strategy_ids]
        if not targets:
            return jsonify({"error": "No matching strategies found"}), 404

    fields = {
        "mode":                     "mixed",
        "step_count":               len(model_dict["bull_levels"]),
        "dump_levels":              model_dict["bull_levels"],
        "order_sizes":              model_dict["bull_order_sizes"],
        "take_profit_percent":      model_dict["bull_take_profit_percent"],
        "bear_levels":              model_dict["bear_levels"],
        "bear_order_sizes":         model_dict["bear_order_sizes"],
        "bear_take_profit_percent": model_dict["bear_take_profit_percent"],
        "leverage":                 model_dict.get("leverage", 1),
        "bull_stop_loss_percent":   model_dict.get("bull_stop_loss_percent", 0.0),
        "bear_stop_loss_percent":   model_dict.get("bear_stop_loss_percent", 0.0),
        "use_entry_indicator":      model_dict.get("use_entry_indicator", True),
        "rsi_overbought":           model_dict.get("rsi_overbought", 70.0),
        "rsi_oversold":             model_dict.get("rsi_oversold", 30.0),
        "regime_interval":          model_dict.get("regime_interval", "1h"),
        "atr_based_spacing":        model_dict.get("atr_based_spacing", False),
        "atr_period":               model_dict.get("atr_period", 14),
    }

    applied = []
    for entry in targets:
        merged = {**entry, **fields}
        try:
            cfg = CoinConfig(
                id=merged["id"], name=merged["name"], coin=merged["coin"], symbol=merged["symbol"],
                enabled=merged.get("enabled", True), step_count=merged["step_count"],
                dump_levels=merged["dump_levels"], order_sizes=merged["order_sizes"],
                take_profit_percent=merged["take_profit_percent"], reference_price=merged.get("reference_price"),
                mode=merged["mode"], bear_levels=merged["bear_levels"], bear_order_sizes=merged["bear_order_sizes"],
                bear_take_profit_percent=merged["bear_take_profit_percent"],
                bull_stop_loss_percent=merged["bull_stop_loss_percent"],
                bear_stop_loss_percent=merged["bear_stop_loss_percent"],
                leverage=merged["leverage"], use_entry_indicator=merged["use_entry_indicator"],
                rsi_overbought=merged["rsi_overbought"], rsi_oversold=merged["rsi_oversold"],
                regime_interval=merged["regime_interval"],
                atr_based_spacing=merged["atr_based_spacing"], atr_period=merged["atr_period"],
            )
            cfg.validate()
        except (ValueError, KeyError) as e:
            return jsonify({"error": f"[{entry.get('name', entry.get('id'))}] {e}"}), 400
        entry.update(fields)
        applied.append(entry.get("name", entry.get("id")))

    cfg_data["coins"] = coins
    with open(COINS_PATH, "w") as f:
        json.dump(cfg_data, f, indent=2)

    _coin_configs = load_coin_configs()
    return jsonify({"ok": True, "applied": applied})


# ── Routes — Logs ─────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    import glob
    from datetime import datetime
    log_dir = os.path.join(ROOT, "logs")
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    files   = glob.glob(os.path.join(log_dir, f"dca_{today}.log"))
    if not files:
        return jsonify({"lines": []})
    with open(files[0]) as f:
        lines = f.readlines()
    n = int(request.args.get("n", 100))
    return jsonify({"lines": [l.rstrip() for l in lines[-n:]]})


# ── Routes — Backtest ─────────────────────────────────────────────────────────

@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/backtest/presets")
def api_backtest_presets():
    path = os.path.join(ROOT, "algo-trading", "top_strategies.json")
    if not os.path.exists(path):
        return jsonify({"strategies": []})
    with open(path) as f:
        return jsonify(json.load(f))


@app.route("/api/backtest/datasets")
def api_backtest_datasets():
    data_dir = os.path.join(ROOT, "algo-trading", "data")
    datasets = []
    if os.path.exists(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.endswith(".csv"):
                continue
            path = os.path.join(data_dir, fname)
            size_mb = round(os.path.getsize(path) / 1024 / 1024, 1)
            with open(path) as fp:
                rows = sum(1 for _ in fp) - 1
            datasets.append({"name": fname, "size_mb": size_mb, "rows": rows})
    return jsonify({"datasets": datasets})


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    tmp_path = None
    try:
        import pandas as pd
        from dca_strategy import DCAStrategy, load_and_prepare_data

        # Support both multipart (with file upload) and plain JSON
        if request.content_type and "multipart" in request.content_type:
            params = json.loads(request.form.get("params", "{}"))
            csv_file = request.files.get("csv_file")
        else:
            params = request.json or {}
            csv_file = None

        mode                = params.get("mode", "long")
        initial_budget      = float(params.get("initial_budget", 1000))
        _raw_levels         = [float(x) for x in params.get("dca_levels", [-6, -9, -12, -16, -20, -24])]
        # Normalize sign: long needs negative, short needs positive
        if mode == "short":
            dca_levels = [abs(l) for l in _raw_levels]
        else:
            dca_levels = [-abs(l) for l in _raw_levels]
        dca_allocations_raw = params.get("dca_allocations")
        dca_allocations     = [float(x) for x in dca_allocations_raw] if dca_allocations_raw else None
        take_profit_percent = float(params.get("take_profit_percent", 5.0))
        stop_loss_percent   = float(params.get("stop_loss_percent", 0.0))
        start_date          = params.get("start_date") or None
        end_date            = params.get("end_date") or None
        dataset_name        = params.get("dataset")

        # Mean-Reversion Scalp needs close prices for EMA/RSI — use the
        # mixed-strategy loader (keeps close/open/volume when present).
        if mode == "mr_scalp":
            from mixed_dca_strategy import load_mixed_data as _load_data
        else:
            _load_data = load_and_prepare_data

        if csv_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                csv_file.save(tmp)
                tmp_path = tmp.name
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                df = _load_data(tmp_path)
        elif dataset_name:
            data_path = os.path.join(ROOT, "algo-trading", "data", dataset_name)
            if not os.path.exists(data_path):
                return jsonify({"error": f"Dataset not found: {dataset_name}"}), 404
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                df = _load_data(data_path)
        else:
            return jsonify({"error": "No dataset specified"}), 400

        if start_date:
            df = df[df["datetime"] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df["datetime"] <= pd.to_datetime(end_date)]
        if len(df) == 0:
            return jsonify({"error": "No candles in the specified date range"}), 400

        if mode == "short":
            from short_dca_strategy import ShortDCAStrategy
            strategy = ShortDCAStrategy(
                initial_budget=initial_budget,
                budget_per_level=dca_allocations,
                dca_levels=dca_levels,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
            )
        elif mode == "mr_scalp":
            from mr_scalp_strategy import MRScalpStrategy
            if stop_loss_percent <= 0:
                return jsonify({"error": "Stop Loss % is required for the Mean-Reversion Scalp (e.g. 1)."}), 400
            _position_size = params.get("position_size")
            strategy = MRScalpStrategy(
                initial_budget=initial_budget,
                position_size=float(_position_size) if _position_size else None,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
                ema_fast=int(params.get("ema_fast", 50)),
                ema_slow=int(params.get("ema_slow", 200)),
                rsi_period=int(params.get("rsi_period", 14)),
                rsi_long_level=float(params.get("rsi_long_level", 45)),
                rsi_short_level=float(params.get("rsi_short_level", 55)),
                allow_short=bool(params.get("allow_short", True)),
            )
        else:
            strategy = DCAStrategy(
                initial_budget=initial_budget,
                budget_per_level=dca_allocations,
                dca_levels=dca_levels,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
            )
        trades = strategy.run_backtest(df)
        results = strategy.calculate_backtest_results()

        # Build equity curve
        equity = initial_budget
        equity_curve = [{"date": df["datetime"].iloc[0].isoformat(), "equity": round(equity, 2)}]
        for trade in trades:
            equity += trade.profit_loss
            equity_curve.append({"date": trade.end_time.isoformat(), "equity": round(equity, 2)})

        trades_data = []
        for i, t in enumerate(trades):
            dur = (t.end_time - t.start_time).total_seconds() / 86400
            trades_data.append({
                "num":            i + 1,
                "start":          t.start_time.isoformat(),
                "end":            t.end_time.isoformat(),
                "duration_days":  round(dur, 2),
                "anchor_price":   round(t.anchor_price, 2),
                "deepest_level":  t.deepest_level,
                "levels_filled":  t.dca_levels_filled,
                "total_invested": round(t.total_invested, 2),
                "exit_price":     round(t.exit_price, 2),
                "profit_loss":    round(t.profit_loss, 2),
                "profit_percent": round(t.profit_percent, 2),
                "stop_loss":      t.stop_loss_triggered,
            })

        return jsonify({
            "ok": True,
            "results": {
                "initial_budget":        results.initial_budget,
                "final_budget":          round(results.final_budget, 2),
                "total_trades":          results.total_trades,
                "winning_trades":        results.winning_trades,
                "losing_trades":         results.losing_trades,
                "stopped_out_trades":    results.stopped_out_trades,
                "total_profit":          round(results.total_profit, 2),
                "total_loss":            round(results.total_loss, 2),
                "net_pnl":               round(results.net_pnl, 2),
                "total_roi":             round(results.total_roi, 2),
                "win_rate":              round(results.win_rate, 1),
                "stop_loss_rate":        round(results.stop_loss_rate, 1),
                "avg_trade_pnl":         round(results.avg_trade_pnl, 2),
                "largest_loss":          round(results.largest_loss, 2),
                "largest_profit":        round(results.largest_profit, 2),
                "avg_loss_magnitude":    round(results.avg_loss_magnitude, 2),
                "avg_profit_magnitude":  round(results.avg_profit_magnitude, 2),
                "avg_trade_duration_days": round(results.avg_trade_duration_days, 2),
                "total_test_days":       round(results.total_test_days, 1),
            },
            "trades":       trades_data,
            "equity_curve": equity_curve,
            "data_range": {
                "start":   df["datetime"].iloc[0].isoformat(),
                "end":     df["datetime"].iloc[-1].isoformat(),
                "candles": len(df),
            },
            "trade_extremes": ({
                "longest":  max(trades_data, key=lambda t: t["duration_days"],  default=None),
                "shortest": min(trades_data, key=lambda t: t["duration_days"],  default=None),
                "best":     max(trades_data, key=lambda t: t["profit_loss"],    default=None),
                "worst":    min(trades_data, key=lambda t: t["profit_loss"],    default=None),
            } if trades_data else None),
            "deploy_time": _DEPLOY_TIME,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.route("/api/backtest/mixed", methods=["POST"])
def api_backtest_mixed():
    tmp_path = None
    try:
        import pandas as pd
        from mixed_dca_strategy import MixedDCABacktest, load_mixed_data
        import io, contextlib

        if request.content_type and "multipart" in request.content_type:
            params   = json.loads(request.form.get("params", "{}"))
            csv_file = request.files.get("csv_file")
        else:
            params   = request.json or {}
            csv_file = None

        initial_budget       = float(params.get("initial_budget", 10000))
        bull_config          = params.get("bull_config", {})
        bear_config          = params.get("bear_config", {})
        # Normalize level signs (user may enter positive magnitudes)
        if "dump_levels" in bull_config:
            bull_config["dump_levels"] = [-abs(l) for l in bull_config["dump_levels"]]
        if "dump_levels" in bear_config:
            bear_config["dump_levels"] = [abs(l) for l in bear_config["dump_levels"]]
        start_date           = params.get("date_from") or params.get("start_date") or None
        end_date             = params.get("date_to")   or params.get("end_date")   or None
        dataset_name         = params.get("dataset")
        use_entry_indicator  = bool(params.get("use_entry_indicator", True))
        rsi_overbought       = float(params.get("rsi_overbought", 70))
        rsi_oversold         = float(params.get("rsi_oversold",   30))

        if csv_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                csv_file.save(tmp)
                tmp_path = tmp.name
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_mixed_data(tmp_path)
        elif dataset_name:
            data_path = os.path.join(ROOT, "algo-trading", "data", dataset_name)
            if not os.path.exists(data_path):
                return jsonify({"error": f"Dataset not found: {dataset_name}"}), 404
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_mixed_data(data_path)
        else:
            return jsonify({"error": "No dataset specified"}), 400

        if start_date:
            df = df[df["datetime"] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df["datetime"] <= pd.to_datetime(end_date)]
        if len(df) == 0:
            return jsonify({"error": "No candles in the specified date range"}), 400

        engine = MixedDCABacktest()
        result = engine.run(df, bull_config, bear_config, initial_budget,
                            use_entry_indicator=use_entry_indicator,
                            rsi_overbought=rsi_overbought,
                            rsi_oversold=rsi_oversold)
        result["data_range"] = {
            "start":   df["datetime"].iloc[0].isoformat(),
            "end":     df["datetime"].iloc[-1].isoformat(),
            "candles": len(df),
        }
        trades = result.get("trades", [])
        if trades:
            longest  = max(trades, key=lambda t: t["duration_days"])
            shortest = min(trades, key=lambda t: t["duration_days"])
            best     = max(trades, key=lambda t: t["profit_loss"])
            worst    = min(trades, key=lambda t: t["profit_loss"])
            result["trade_extremes"] = {
                "longest":  {"num": longest["num"],  "duration_days": longest["duration_days"],  "start": longest["start"]},
                "shortest": {"num": shortest["num"], "duration_days": shortest["duration_days"], "start": shortest["start"]},
                "best":     {"num": best["num"],     "profit_loss": best["profit_loss"],         "start": best["start"]},
                "worst":    {"num": worst["num"],    "profit_loss": worst["profit_loss"],        "start": worst["start"]},
            }
        result["deploy_time"] = _DEPLOY_TIME
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Realism backtest wrapper (additive, post-processing only) ─────────────────
# Pure helpers that wrap the EXISTING engines (DCAStrategy / ShortDCAStrategy /
# MRScalpStrategy / MixedDCABacktest) and post-process their output. No engine
# logic is modified here.

def _normalize_trade(t, mode):
    """Normalize a Trade dataclass (long/short/mr_scalp) or a mixed-mode trade
    dict into a common shape: start, end, invested, profit_loss, profit_percent."""
    if mode == "mixed":
        import pandas as pd
        return {
            "start":          pd.to_datetime(t["start"]),
            "end":            pd.to_datetime(t["end"]),
            "invested":       float(t["invested"]),
            "profit_loss":    float(t["profit_loss"]),
            "profit_percent": float(t["profit_percent"]),
        }
    return {
        "start":          t.start_time,
        "end":            t.end_time,
        "invested":       float(t.total_invested),
        "profit_loss":    float(t.profit_loss),
        "profit_percent": float(t.profit_percent),
    }


def _run_strategy_trades(mode, params, df):
    """Run the existing engine for `mode` on `df` with `params` and return
    (normalized_trades, initial_budget). Mirrors the param handling in
    /api/backtest/run and /api/backtest/mixed — does not alter engine logic."""
    if mode == "mixed":
        from mixed_dca_strategy import MixedDCABacktest

        initial_budget = float(params.get("initial_budget", 10000))
        bull_config = dict(params.get("bull_config", {}))
        bear_config = dict(params.get("bear_config", {}))
        if "dump_levels" in bull_config:
            bull_config["dump_levels"] = [-abs(l) for l in bull_config["dump_levels"]]
        if "dump_levels" in bear_config:
            bear_config["dump_levels"] = [abs(l) for l in bear_config["dump_levels"]]

        engine = MixedDCABacktest()
        result = engine.run(
            df, bull_config, bear_config, initial_budget,
            use_entry_indicator=bool(params.get("use_entry_indicator", True)),
            rsi_overbought=float(params.get("rsi_overbought", 70)),
            rsi_oversold=float(params.get("rsi_oversold", 30)),
        )
        raw_trades = result.get("trades", [])
    else:
        from dca_strategy import DCAStrategy

        initial_budget = float(params.get("initial_budget", 1000))
        _raw_levels = [float(x) for x in params.get("dca_levels", [-6, -9, -12, -16, -20, -24])]
        dca_levels = [abs(l) for l in _raw_levels] if mode == "short" else [-abs(l) for l in _raw_levels]
        dca_allocations_raw = params.get("dca_allocations")
        dca_allocations = [float(x) for x in dca_allocations_raw] if dca_allocations_raw else None
        take_profit_percent = float(params.get("take_profit_percent", 5.0))
        stop_loss_percent   = float(params.get("stop_loss_percent", 0.0))

        if mode == "short":
            from short_dca_strategy import ShortDCAStrategy
            strategy = ShortDCAStrategy(
                initial_budget=initial_budget,
                budget_per_level=dca_allocations,
                dca_levels=dca_levels,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
            )
        elif mode == "mr_scalp":
            from mr_scalp_strategy import MRScalpStrategy
            if stop_loss_percent <= 0:
                raise ValueError("Stop Loss % is required for the Mean-Reversion Scalp (e.g. 1).")
            _position_size = params.get("position_size")
            strategy = MRScalpStrategy(
                initial_budget=initial_budget,
                position_size=float(_position_size) if _position_size else None,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
                ema_fast=int(params.get("ema_fast", 50)),
                ema_slow=int(params.get("ema_slow", 200)),
                rsi_period=int(params.get("rsi_period", 14)),
                rsi_long_level=float(params.get("rsi_long_level", 45)),
                rsi_short_level=float(params.get("rsi_short_level", 55)),
                allow_short=bool(params.get("allow_short", True)),
            )
        else:
            strategy = DCAStrategy(
                initial_budget=initial_budget,
                budget_per_level=dca_allocations,
                dca_levels=dca_levels,
                take_profit_percent=take_profit_percent,
                stop_loss_percent=stop_loss_percent,
            )
        raw_trades = strategy.run_backtest(df)

    return [_normalize_trade(t, mode) for t in raw_trades], initial_budget


def _fee_adjusted_pnl(trades, fee_pct):
    """Return copies of `trades` with `net_profit_loss` / `net_profit_percent`
    added, after deducting a round-trip `fee_pct` cost from each trade's
    invested amount."""
    adjusted = []
    for t in trades:
        cost = t["invested"] * (fee_pct / 100.0)
        net_pnl = t["profit_loss"] - cost
        net_pct = (net_pnl / t["invested"] * 100.0) if t["invested"] else 0.0
        nt = dict(t)
        nt["net_profit_loss"] = net_pnl
        nt["net_profit_percent"] = net_pct
        adjusted.append(nt)
    return adjusted


def _summarize_trades(trades, initial_budget, pnl_key="profit_loss"):
    """ROI / win-rate / max-drawdown summary for a list of normalized trades
    using the given P&L field (gross or fee-adjusted)."""
    n = len(trades)
    if n == 0:
        return {"trade_count": 0, "win_rate": 0.0, "roi_pct": 0.0, "net_pnl": 0.0, "max_drawdown_pct": 0.0}

    net_pnl = sum(t[pnl_key] for t in trades)
    wins = sum(1 for t in trades if t[pnl_key] > 0)

    equity = initial_budget
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += t[pnl_key]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    return {
        "trade_count": n,
        "win_rate": round(wins / n * 100.0, 1),
        "roi_pct": round(net_pnl / initial_budget * 100.0, 2) if initial_budget else 0.0,
        "net_pnl": round(net_pnl, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def _risk_metrics(trades, initial_budget, pnl_key="profit_loss"):
    """Max drawdown %, longest losing streak (consecutive losing trades), and
    longest time-underwater (days from an equity peak until that peak is
    recovered) from a list of normalized trades."""
    if not trades:
        return {"max_drawdown_pct": 0.0, "longest_losing_streak": 0, "longest_underwater_days": 0.0}

    equity = initial_budget
    peak = equity
    peak_date = trades[0]["start"]
    max_dd = 0.0
    underwater_since = None
    longest_underwater = 0.0
    streak = 0
    longest_streak = 0

    for t in trades:
        equity += t[pnl_key]
        if equity >= peak:
            if underwater_since is not None:
                days = (t["end"] - underwater_since).total_seconds() / 86400.0
                longest_underwater = max(longest_underwater, days)
                underwater_since = None
            peak = equity
            peak_date = t["end"]
        else:
            if underwater_since is None:
                underwater_since = peak_date
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)

        if t[pnl_key] < 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0

    if underwater_since is not None:
        days = (trades[-1]["end"] - underwater_since).total_seconds() / 86400.0
        longest_underwater = max(longest_underwater, days)

    return {
        "max_drawdown_pct": round(max_dd, 2),
        "longest_losing_streak": longest_streak,
        "longest_underwater_days": round(longest_underwater, 2),
    }


def _buy_and_hold_roi(df):
    """Buy-and-hold ROI % over `df`'s close prices, or None if no 'close' column."""
    if "close" not in df.columns or len(df) == 0:
        return None
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    if first == 0:
        return None
    return (last - first) / first * 100.0


@app.route("/api/backtest/realism", methods=["POST"])
def api_backtest_realism():
    """Cost-adjusted, risk-aware view of a backtest: fee-adjusted ROI,
    walk-forward (in-sample vs out-of-sample) split, buy-and-hold baseline,
    drawdown/risk metrics, and a low-sample-size flag. Reuses the existing
    engines as-is via _run_strategy_trades(); does not modify them."""
    tmp_path = None
    try:
        import pandas as pd
        from mixed_dca_strategy import load_mixed_data
        import io, contextlib

        if request.content_type and "multipart" in request.content_type:
            params   = json.loads(request.form.get("params", "{}"))
            csv_file = request.files.get("csv_file")
        else:
            params   = request.json or {}
            csv_file = None

        mode    = params.get("mode", "long")
        fee_pct = float(params.get("fee_pct", 0.1))

        start_date   = params.get("start_date") or None
        end_date     = params.get("end_date") or None
        is_start     = params.get("is_start") or None
        is_end       = params.get("is_end") or None
        oos_start    = params.get("oos_start") or None
        oos_end      = params.get("oos_end") or None
        dataset_name = params.get("dataset")

        # Use the mixed-data loader for every mode — it's a superset (adds
        # open/close/volume when present, needed for the buy-and-hold
        # baseline) and the long/short/mr_scalp engines only read
        # datetime/high/low (plus close for mr_scalp), so extra columns
        # are harmless.
        if csv_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                csv_file.save(tmp)
                tmp_path = tmp.name
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_mixed_data(tmp_path)
        elif dataset_name:
            data_path = os.path.join(ROOT, "algo-trading", "data", dataset_name)
            if not os.path.exists(data_path):
                return jsonify({"error": f"Dataset not found: {dataset_name}"}), 404
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_mixed_data(data_path)
        else:
            return jsonify({"error": "No dataset specified"}), 400

        if len(df) == 0:
            return jsonify({"error": "Dataset is empty"}), 400

        # ── Main run (gross vs fee-adjusted) ─────────────────────────────
        main_df = df
        if start_date:
            main_df = main_df[main_df["datetime"] >= pd.to_datetime(start_date)]
        if end_date:
            main_df = main_df[main_df["datetime"] <= pd.to_datetime(end_date)]
        if len(main_df) == 0:
            return jsonify({"error": "No candles in the specified date range"}), 400

        main_trades, initial_budget = _run_strategy_trades(mode, params, main_df)
        main_trades_net = _fee_adjusted_pnl(main_trades, fee_pct)

        gross_summary = _summarize_trades(main_trades, initial_budget, pnl_key="profit_loss")
        net_summary   = _summarize_trades(main_trades_net, initial_budget, pnl_key="net_profit_loss")
        risk          = _risk_metrics(main_trades_net, initial_budget, pnl_key="net_profit_loss")

        # ── Buy & hold baseline over the same range ──────────────────────
        bh_roi = _buy_and_hold_roi(main_df)

        # ── Walk-forward / out-of-sample split ───────────────────────────
        if not any([is_start, is_end, oos_start, oos_end]):
            mid = main_df["datetime"].iloc[len(main_df) // 2]
            is_df  = main_df[main_df["datetime"] < mid]
            oos_df = main_df[main_df["datetime"] >= mid]
        else:
            is_df = main_df
            if is_start:
                is_df = is_df[is_df["datetime"] >= pd.to_datetime(is_start)]
            if is_end:
                is_df = is_df[is_df["datetime"] <= pd.to_datetime(is_end)]
            oos_df = main_df
            if oos_start:
                oos_df = oos_df[oos_df["datetime"] >= pd.to_datetime(oos_start)]
            if oos_end:
                oos_df = oos_df[oos_df["datetime"] <= pd.to_datetime(oos_end)]

        walk_forward = {"in_sample": None, "out_of_sample": None, "overfit_warning": False}
        if len(is_df) > 0 and len(oos_df) > 0:
            is_trades, is_budget   = _run_strategy_trades(mode, params, is_df)
            oos_trades, oos_budget = _run_strategy_trades(mode, params, oos_df)
            is_summary  = _summarize_trades(is_trades, is_budget, pnl_key="profit_loss")
            oos_summary = _summarize_trades(oos_trades, oos_budget, pnl_key="profit_loss")
            is_risk  = _risk_metrics(is_trades, is_budget, pnl_key="profit_loss")
            oos_risk = _risk_metrics(oos_trades, oos_budget, pnl_key="profit_loss")
            walk_forward["in_sample"] = {
                **is_summary,
                "max_drawdown_pct": is_risk["max_drawdown_pct"],
                "range": {"start": is_df["datetime"].iloc[0].isoformat(), "end": is_df["datetime"].iloc[-1].isoformat()},
            }
            walk_forward["out_of_sample"] = {
                **oos_summary,
                "max_drawdown_pct": oos_risk["max_drawdown_pct"],
                "range": {"start": oos_df["datetime"].iloc[0].isoformat(), "end": oos_df["datetime"].iloc[-1].isoformat()},
            }
            walk_forward["overfit_warning"] = oos_summary["roi_pct"] < (is_summary["roi_pct"] / 2)
        else:
            walk_forward["error"] = "Not enough candles in one or both walk-forward ranges."

        return jsonify({
            "ok":   True,
            "mode": mode,
            "fee_pct":         fee_pct,
            "initial_budget":  initial_budget,
            "trade_count":     gross_summary["trade_count"],
            "low_sample_size": gross_summary["trade_count"] < 100,
            "gross": gross_summary,
            "net":   net_summary,
            "risk":  risk,
            "buy_and_hold_roi_pct": (round(bh_roi, 2) if bh_roi is not None else None),
            "walk_forward": walk_forward,
            "data_range": {
                "start":   main_df["datetime"].iloc[0].isoformat(),
                "end":     main_df["datetime"].iloc[-1].isoformat(),
                "candles": len(main_df),
            },
            "deploy_time": _DEPLOY_TIME,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.route("/api/backtest/replay", methods=["POST"])
def api_backtest_replay():
    """Signal Replay: ask the live signal logic (via the standalone
    signal_lab.signal_fn copy of core.regime_live) for a LONG/SHORT/WAIT
    verdict at a chosen historical moment T, using only candles that closed
    at or before T, then reveal what price actually did over the next N
    candles. Pure research tool — does not touch the live engines, config,
    or core/regime_live.py."""
    from datetime import datetime, timezone
    from signal_lab.signal_fn import get_verdict
    from signal_lab.klines import fetch_replay_window, INTERVAL_MS

    try:
        params = request.json or {}
        symbol = (params.get("symbol") or "").strip().upper()
        interval = params.get("interval") or "4h"
        dt_str = params.get("datetime")
        lookahead_n = int(params.get("lookahead_n") or 6)

        if not symbol:
            return jsonify({"error": "symbol is required"}), 400
        if interval not in INTERVAL_MS:
            return jsonify({"error": f"Unsupported interval: {interval}. Choose one of {sorted(INTERVAL_MS)}"}), 400
        if not dt_str:
            return jsonify({"error": "datetime is required"}), 400
        if not (1 <= lookahead_n <= 100):
            return jsonify({"error": "lookahead_n must be between 1 and 100"}), 400

        try:
            t = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": f"Invalid datetime: {dt_str!r}"}), 400
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t_ms = int(t.timestamp() * 1000)

        # ── No-look-ahead: history = candles closed at/before T, future =
        # candles opening at/after T. fetch_replay_window() performs and
        # asserts this split by timestamp before returning either half.
        history, future = fetch_replay_window(symbol, interval, t_ms, lookahead_n)

        result = get_verdict(history)
        verdict = {"LONG_NOW": "LONG", "SHORT_NOW": "SHORT", "WATCH": "WAIT", "WAIT": "WAIT"}[result["verdict"]]

        price_at_t = history["close"][-1]
        outcome = None
        if future["close"]:
            price_at_t_plus_n = future["close"][-1]
            pct_move = (price_at_t_plus_n - price_at_t) / price_at_t * 100.0
            outcome = {
                "price_at_t": price_at_t,
                "price_at_t_plus_n": price_at_t_plus_n,
                "pct_move": round(pct_move, 3),
                "candles_available": len(future["close"]),
                "candles_requested": lookahead_n,
                "long_correct": pct_move > 0,
                "short_correct": pct_move < 0,
            }

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "t_iso": t.astimezone(timezone.utc).isoformat(),
            "t_actual_close_iso": datetime.fromtimestamp(history["close_time"][-1] / 1000, tz=timezone.utc).isoformat(),
            "history_candles": len(history["close"]),
            "warmup_ok": result["warmup_ok"],
            "verdict": verdict,
            "verdict_raw": result["verdict"],
            "regime": result["regime"],
            "active_mode": result["active_mode"],
            "score": result["score"],
            "entry_score": result["entry_score"],
            "entry_signals": result["entry_signals"],
            "outcome": outcome,
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


def _sanitize_floats(d: dict) -> dict:
    """Replace inf/-inf/nan with None so jsonify's output stays valid JSON
    (JSON has no Infinity/NaN literals — JS JSON.parse would choke on
    signal_lab.harness's profit_factor=inf when there are no losing trades)."""
    import math
    return {
        k: (None if isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in d.items()
    }


@app.route("/api/backtest/signal-scan", methods=["POST"])
def api_backtest_signal_scan():
    """Signal Scan: walk the live signal logic (signal_lab.signal_fn, via
    signal_lab.harness.run_walk) candle-by-candle over a historical date
    range — at each step only candles[0:i+1] are visible, exactly mirroring
    the live engine's point-in-time view (no look-ahead). Every LONG_NOW /
    SHORT_NOW verdict opens one fixed-size TP/SL/horizon trade while flat.
    Returns each signal/trade plus summary stats vs. inverted / random /
    buy-and-hold baselines. Pure research tool — does not touch the live
    engines, config, or core/regime_live.py."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from signal_lab import harness
    from signal_lab.klines import fetch_candle_range, INTERVAL_MS

    try:
        params = request.json or {}
        symbol = (params.get("symbol") or "").strip().upper()
        interval = params.get("interval") or "4h"
        start_str = params.get("start_date")
        end_str = params.get("end_date")
        tp = float(params.get("tp_pct") or 3.0)
        sl = float(params.get("sl_pct") or 2.0)
        horizon = int(params.get("horizon") or 12)

        if not symbol:
            return jsonify({"error": "symbol is required"}), 400
        if interval not in INTERVAL_MS:
            return jsonify({"error": f"Unsupported interval: {interval}. Choose one of {sorted(INTERVAL_MS)}"}), 400
        if not start_str or not end_str:
            return jsonify({"error": "start_date and end_date are required"}), 400
        if not (0 < tp <= 50):
            return jsonify({"error": "tp_pct must be between 0 and 50"}), 400
        if not (0 < sl <= 50):
            return jsonify({"error": "sl_pct must be between 0 and 50"}), 400
        if not (1 <= horizon <= 500):
            return jsonify({"error": "horizon must be between 1 and 500"}), 400

        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": f"Invalid start_date/end_date: {start_str!r} / {end_str!r}"}), 400
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # ── fetch_candle_range fetches [start_ms - warmup*step, end_ms] so the
        # walk has a full EMA-200 warmup before the requested range begins.
        candles = fetch_candle_range(symbol, interval, start_ms, end_ms)
        n = len(candles["close"])
        if n <= harness.WARMUP_CANDLES:
            return jsonify({
                "error": f"Only {n} candles available for this range — need more than "
                         f"{harness.WARMUP_CANDLES} (EMA-200 warmup). Pick a longer range "
                         f"or a smaller interval.",
            }), 400

        args = SimpleNamespace(
            rsi_overbought=70.0, rsi_oversold=30.0, atr_period=14,
            tp=tp, sl=sl, horizon=horizon, exit_mode="tpsl", fee=0.1,
            random_seeds=200, ml_filter=False, quiet=True,
        )

        # ── No-look-ahead: run_walk() calls get_verdict() on candles[0:i+1] at
        # each step i — identical point-in-time discipline to fetch_replay_window.
        walk = harness.run_walk(candles, args)
        trades = walk["trades"]
        complete = [t for t in trades if not t["incomplete"]]

        summary = harness._summarize(complete)
        inverted = harness.inverted_baseline(candles, complete, args)
        rnd = harness.random_baseline(candles, complete, args)
        start_idx = complete[0]["entry_index"] if complete else (harness.WARMUP_CANDLES - 1)
        bh = harness.buy_and_hold(candles, start_idx, n - 1, args.fee)

        close_times = candles["close_time"]

        def iso(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

        signals = [{
            "entry_time": iso(close_times[t["entry_index"]]),
            "exit_time": iso(close_times[t["exit_index"]]),
            "direction": t["direction"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "exit_reason": t["exit_reason"],
            "net_pct": t["net_pct"],
            "bars_held": t["bars_held"],
            "regime": t["regime"],
            "incomplete": t["incomplete"],
        } for t in trades]

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "candles_used": n,
            "walked": walk["n_walked"],
            "range_start_iso": iso(close_times[0]),
            "range_end_iso": iso(close_times[-1]),
            "verdict_counts": walk["verdict_counts"],
            "overlap_same": walk["overlap_same"],
            "overlap_opp": walk["overlap_opp"],
            "signals": signals,
            "signal_count": len(trades),
            "complete_count": len(complete),
            "low_sample_size": len(complete) < 20,
            "summary": _sanitize_floats(summary),
            "baselines": {
                "inverted": _sanitize_floats(inverted),
                "random": _sanitize_floats(rnd),
                "buy_and_hold": bh,
            },
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


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


# ── Routes — Regime Detector ─────────────────────────────────────────────────

@app.route("/api/regime")
def api_regime():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    try:
        detector = RegimeDetector()
        analysis = detector.analyze(symbol)
        if "error" in analysis:
            return jsonify({"error": analysis["error"]}), 503
        analysis["narrative"] = detector.get_ai_narrative(analysis)
        return jsonify(analysis)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/regime/confirm", methods=["POST"])
def api_regime_confirm():
    data = request.json or {}
    state = {
        "confirmed": bool(data.get("confirmed", False)),
        "strategy":  data.get("strategy", ""),
        "regime":    data.get("regime", ""),
        "score":     data.get("score", 0),
        "timestamp": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "regime_state.json"), "w") as f:
        json.dump(state, f, indent=2)
    return jsonify({"ok": True, "state": state})


@app.route("/api/regime/state")
def api_regime_state():
    path = os.path.join(ROOT, "data", "regime_state.json")
    if not os.path.exists(path):
        return jsonify({"confirmed": False, "strategy": None, "timestamp": None})
    with open(path) as f:
        return jsonify(json.load(f))


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
             "&& systemctl restart infinity "
             "&& systemctl restart infinity-web"],
        )

    threading.Thread(target=run_deploy, daemon=True).start()
    return jsonify({"ok": True, "message": "Deploy started"}), 200


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def start():
    global _accounts, _coin_configs, _dca_models
    _accounts     = load_accounts()
    _coin_configs = load_coin_configs()
    _dca_models   = load_dca_models()

    # Auto-connect all saved accounts
    for acct in _accounts:
        try:
            client = BinanceSpotClient(
                acct["api_key"], acct["secret_key"],
                testnet=acct.get("testnet", False)
            )
            _clients[acct["id"]] = client
        except Exception as e:
            print(f"⚠️  Could not connect account '{acct['name']}': {e}")

    # Start price poller
    threading.Thread(target=_price_poller, daemon=True).start()

    # Pre-warm price cache
    for cfg in _coin_configs:
        if cfg.symbol in _price_cache:
            continue
        for client in list(_clients.values()):
            try:
                p = client.get_price(cfg.symbol)
                with _price_lock:
                    _price_cache[cfg.symbol] = p
                break
            except Exception:
                pass

    port = int(os.getenv("DASHBOARD_PORT", 5050))
    print(f"\n🌐  Dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# Bootstrap when loaded by Gunicorn (not __main__)
_accounts     = load_accounts()
_coin_configs = load_coin_configs()
_dca_models   = load_dca_models()
for _acct in _accounts:
    try:
        _clients[_acct["id"]] = BinanceSpotClient(
            _acct["api_key"], _acct["secret_key"],
            testnet=_acct.get("testnet", False)
        )
    except Exception:
        pass
threading.Thread(target=_price_poller, daemon=True).start()


if __name__ == "__main__":
    start()
