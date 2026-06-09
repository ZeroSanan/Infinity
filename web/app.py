"""
DCA Dashboard — Flask Web Server (multi-account, multi-strategy)
=================================================
Run with:  python3 web/app.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
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
from core.dca_engine import DCAEngine
from core.regime_detector import RegimeDetector
from core.state_manager import load_state, save_state, reset_state
from models.dca_config import CoinConfig
from utils.calculations import calc_dump_percent, calc_take_profit_price, calc_pnl

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ROOT          = os.path.join(os.path.dirname(__file__), "..")
ACCOUNTS_PATH = os.path.join(ROOT, "config", "accounts.json")
COINS_PATH    = os.path.join(ROOT, "config", "coins.json")

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

# ── Market signals cache ──────────────────────────────────────────────────────
_market_signals_cache: dict = {"data": None, "ts": 0.0}
_SIGNALS_TTL = 900  # seconds (15 min)

# ── In-memory state ───────────────────────────────────────────────────────────
_clients: dict = {}        # account_id -> BinanceSpotClient
_engines: dict = {}        # account_id -> { strategy_id -> DCAEngine }
_price_cache: dict = {}    # symbol -> float
_price_lock = threading.Lock()
_coin_configs: list = []   # list[CoinConfig]
_accounts: list = []       # list[dict]  (loaded from accounts.json)


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_PATH):
        return []
    with open(ACCOUNTS_PATH) as f:
        return json.load(f).get("accounts", [])


def save_accounts(accounts: list):
    with open(ACCOUNTS_PATH, "w") as f:
        json.dump({"accounts": accounts}, f, indent=2)


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
        )
        cfg.validate()
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

    if state.reference_price and price:
        dump_pct = calc_dump_percent(state.reference_price, price)

    if state.status == "ACTIVE" and state.average_entry:
        tp_price = calc_take_profit_price(state.average_entry, cfg.take_profit_percent)
        if price:
            pnl_usdt, pnl_pct = calc_pnl(state.average_entry, price, state.total_quantity)

    steps = []
    exec_map = {s.step_index: s for s in state.executed_steps}
    for i, (level, size) in enumerate(zip(cfg.dump_levels, cfg.order_sizes)):
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
        "tp_percent":     cfg.take_profit_percent,
        "pnl_usdt":       pnl_usdt,
        "pnl_pct":        pnl_pct,
        "steps_done":     state.steps_done,
        "step_count":     cfg.step_count,
        "steps":          steps,
        "total_capital":  cfg.total_capital,
    }


# ── Routes — Dashboard ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    any_connected = bool(_clients)
    usdt_totals = {}
    for aid, client in list(_clients.items()):
        try:
            usdt_totals[aid] = client.get_balance("USDT")
        except Exception:
            pass

    coins = [_coin_summary(cfg) for cfg in _coin_configs]
    return jsonify({
        "connected": any_connected,
        "usdt_totals": usdt_totals,
        "coins": coins,
        "account_count": len(_accounts),
        "deploy_time": _DEPLOY_TIME,
    })


@app.route("/api/market/signals")
def api_market_signals():
    """Bull/bear regime signals for BTC, ETH, XRP, SOL via public Binance klines."""
    import datetime as _dt
    global _market_signals_cache
    now = time.time()
    force = "bust" in request.args
    if not force and _market_signals_cache["data"] and now - _market_signals_cache["ts"] < _SIGNALS_TTL:
        return jsonify(_market_signals_cache["data"])

    try:
        import requests as _req
    except ImportError:
        return jsonify({"error": "requests not installed"}), 500

    # Preset level suggestions keyed by (regime, score).
    # Levels are stored as positive magnitudes; direction depends on regime.
    _PRESETS = {
        ("BULL", 4): {"name": "Conservative",  "levels": [6, 10, 15],          "tp": 5.0},
        ("BULL", 3): {"name": "Standard",       "levels": [8, 12, 18, 24],      "tp": 8.0},
        ("NEUTRAL",2):{"name": "Cautious",      "levels": [8, 12, 18],          "tp": 6.0},
        ("BEAR", 1): {"name": "Standard Short", "levels": [6, 10, 15],          "tp": 5.0},
        ("BEAR", 0): {"name": "Aggressive Short","levels": [8, 12, 18, 24],     "tp": 8.0},
    }

    # Load user-saved strategies from coins.json
    try:
        with open(COINS_PATH) as _f:
            _raw = json.load(_f).get("coins", [])
    except Exception:
        _raw = []

    def _best_saved(coin_name: str, regime: str):
        """Return the best matching saved strategy dict, or None."""
        matches = [s for s in _raw if s.get("coin", "").upper() == coin_name.upper()]
        if not matches:
            return None
        for s in matches:
            lvls = s.get("dump_levels", [])
            is_long  = lvls and all(l < 0 for l in lvls)
            is_short = lvls and all(l > 0 for l in lvls)
            if regime == "BULL"    and is_long:  return s
            if regime == "BEAR"    and is_short: return s
            if regime == "NEUTRAL":              return s
        return matches[0]

    coins = [
        ("BTC", "BTCUSDT"),
        ("ETH", "ETHUSDT"),
        ("XRP", "XRPUSDT"),
        ("SOL", "SOLUSDT"),
    ]

    def _ema(data: list, period: int) -> list:
        k = 2.0 / (period + 1)
        out = [data[0]]
        for p in data[1:]:
            out.append(p * k + out[-1] * (1 - k))
        return out

    def _rsi(closes: list, period: int = 14) -> float:
        diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in diffs[-period:]]
        losses = [max(-d, 0) for d in diffs[-period:]]
        ag = sum(gains) / period
        al = sum(losses) / period
        return 100.0 - (100.0 / (1 + ag / al)) if al else 100.0

    signals = []
    for coin, symbol in coins:
        try:
            resp = _req.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "4h", "limit": 200},
                timeout=8,
            )
            klines = resp.json()
            if not isinstance(klines, list) or len(klines) < 55:
                raise ValueError("insufficient data")

            closes  = [float(k[4]) for k in klines]
            highs   = [float(k[2]) for k in klines]
            lows    = [float(k[3]) for k in klines]
            price   = closes[-1]
            e50     = _ema(closes, 50)[-1]
            e200    = _ema(closes, 200)[-1]
            rsi     = _rsi(closes)
            n7      = min(42, len(closes) - 1)
            week_ret = (price - closes[-n7 - 1]) / closes[-n7 - 1] * 100

            s1 = price > e50
            s2 = e50   > e200
            s3 = rsi   > 55
            s4 = week_ret > 0

            score = int(s1) + int(s2) + int(s3) + int(s4)
            if   score >= 3: regime, rec = "BULL",    "Long DCA"
            elif score <= 1: regime, rec = "BEAR",    "Short DCA"
            else:            regime, rec = "NEUTRAL", "Mixed DCA"

            # ── Entry readiness ───────────────────────────────────────
            # Trend alone is not enough — RSI timing determines readiness
            if regime == "BULL":
                if rsi < 40:
                    action, entry_ready = "LONG NOW",  True
                    condition = f"RSI oversold at {rsi:.0f} — strong dip entry"
                elif rsi < 50:
                    action, entry_ready = "LONG NOW",  True
                    condition = f"RSI {rsi:.0f} — decent dip, good entry"
                elif rsi < 60:
                    action, entry_ready = "WATCH",     False
                    condition = f"Wait for RSI to dip below 50 (now {rsi:.0f})"
                else:
                    action, entry_ready = "WAIT",      False
                    condition = f"RSI too high for long entry — wait for pullback to RSI 50 (now {rsi:.0f})"
            elif regime == "BEAR":
                if rsi > 65:
                    action, entry_ready = "SHORT NOW", True
                    condition = f"RSI overbought at {rsi:.0f} — strong pump to short into"
                elif rsi > 55:
                    action, entry_ready = "SHORT NOW", True
                    condition = f"RSI {rsi:.0f} — elevated, decent short entry"
                elif rsi > 45:
                    action, entry_ready = "WATCH",     False
                    condition = f"Wait for RSI to rise above 55 (now {rsi:.0f})"
                else:
                    action, entry_ready = "WAIT",      False
                    condition = f"RSI too low for short — wait for bounce to RSI 55+ (now {rsi:.0f})"
            else:  # NEUTRAL
                action, entry_ready = "WATCH",     False
                condition = "No clear trend — wait for BULL or BEAR confirmation"

            # Strategy recommendation: prefer saved, fall back to preset
            saved = _best_saved(coin, regime)
            if saved:
                strat_name   = saved.get("name", coin + " Strategy")
                strat_levels = [abs(l) for l in saved.get("dump_levels", [])]
                strat_tp     = saved.get("take_profit_percent", 5.0)
                strat_source = "saved"
            else:
                preset_key   = (regime, score)
                preset       = _PRESETS.get(preset_key, _PRESETS.get(("BULL" if regime == "BULL" else "BEAR", 3 if regime == "BULL" else 1)))
                strat_name   = preset["name"]
                strat_levels = preset["levels"]
                strat_tp     = preset["tp"]
                strat_source = "suggested"

            # Suggested anchor = highest HIGH of last 14 days (84 × 4h candles)
            anchor = max(highs[-min(84, len(highs)):])

            dec = 4 if price < 10 else (2 if price < 1000 else 0)

            # DCA trigger prices based on anchor + recommended levels
            trigger_prices = [
                round(anchor * (1 - lvl / 100), dec)
                for lvl in strat_levels
            ]

            # Levels already passed = trigger price is above current price
            # (price has already fallen through them)
            levels_passed = sum(1 for tp in trigger_prices if tp > price)

            # If any levels are passed, offer a fresh-start anchor (current price)
            # so user can still deploy remaining capital from here
            fresh_anchor = round(price, dec)
            fresh_triggers = [
                round(price * (1 - lvl / 100), dec)
                for lvl in strat_levels
            ]

            # ── ML models ────────────────────────────────────────────
            try:
                from core.ml_signals import predict_direction, classify_regime, score_entry
                ml_dir    = predict_direction(closes, highs, lows)
                ml_regime = classify_regime(closes, highs, lows, score, regime)
                ml_entry  = score_entry(closes, highs, lows, rec)
            except Exception:
                ml_dir    = {"direction": "UNKNOWN", "confidence": 0.0}
                ml_regime = {"regime": regime, "confidence": 50.0, "agrees_with_rules": True}
                ml_entry  = {"score": 50, "grade": "C", "factors": []}

            signals.append({
                "coin":           coin,
                "price":          round(price, dec),
                "ema50":          round(e50,  2),
                "ema200":         round(e200, 2),
                "rsi":            round(rsi,  1),
                "week_ret":       round(week_ret, 2),
                "score":          score,
                "regime":         regime,
                "rec":            rec,
                "s1": s1, "s2": s2, "s3": s3, "s4": s4,
                "strat_name":     strat_name,
                "strat_levels":   strat_levels,
                "strat_tp":       strat_tp,
                "strat_source":   strat_source,
                "anchor":         round(anchor, dec),
                "trigger_prices": trigger_prices,
                "levels_passed":  levels_passed,
                "fresh_anchor":   fresh_anchor,
                "fresh_triggers": fresh_triggers,
                "ml_direction":   ml_dir,
                "ml_regime":      ml_regime,
                "ml_entry":       ml_entry,
                "action":         action,
                "entry_ready":    entry_ready,
                "condition":      condition,
            })
        except Exception as exc:
            signals.append({
                "coin": coin, "regime": "UNKNOWN", "rec": "—",
                "score": 0, "error": str(exc),
            })

    result = {
        "signals":   signals,
        "timestamp": _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    _market_signals_cache = {"data": result, "ts": now}

    # Persist to history DB (only on fresh computes, not cache hits)
    try:
        from core.signal_history import save_signals
        save_signals(signals)
    except Exception:
        pass

    return jsonify(result)


@app.route("/api/signals/history")
def api_signals_history():
    """Return persisted signal history for charting and analysis."""
    from core.signal_history import get_history, get_summary
    coin = request.args.get("coin")          # optional filter
    days = int(request.args.get("days", 7))
    rows = get_history(coin=coin, days=days)
    summary = get_summary(days=days)
    return jsonify({"rows": rows, "summary": summary, "days": days})


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
        account_id = next(iter(_clients.keys()), None)
    if not account_id:
        return jsonify({"error": "No Binance account connected"}), 503

    client = get_client(account_id)
    if not client:
        return jsonify({"error": "Could not connect account"}), 503

    # Check not already running
    for aid, engines in _engines.items():
        if strategy_id in engines:
            return jsonify({"ok": True, "message": f"Already running on account {aid}"})

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
        if csv_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                csv_file.save(tmp)
                tmp_path = tmp.name
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_and_prepare_data(tmp_path)
        elif dataset_name:
            data_path = os.path.join(ROOT, "algo-trading", "data", dataset_name)
            if not os.path.exists(data_path):
                return jsonify({"error": f"Dataset not found: {dataset_name}"}), 404
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                df = load_and_prepare_data(data_path)
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
    global _accounts, _coin_configs
    _accounts     = load_accounts()
    _coin_configs = load_coin_configs()

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
