"""
Live regime detection for the Mixed long/short DCA engine.

Ports the regime-scoring + 5-candle confirmation + extremity-override state
machine from algo-trading/mixed_dca_strategy.py (backtest) to operate on a
rolling window of live klines fetched from Binance's public REST API.

Stateless: replays the full candle window on every call, so it is
restart-safe and needs no persisted regime buffers. A 500-candle window
gives ample warmup (EMA-200 needs 200) and convergence time for the
5-candle confirmation logic.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Tuple

import requests

_KLINES_URL = "https://api.binance.com/api/v3/klines"


# ── Indicator helpers (ported from algo-trading/mixed_dca_strategy.py) ──────

def _ema(prices: list, period: int) -> list:
    k = 2.0 / (period + 1)
    result = [None] * len(prices)
    if len(prices) < period:
        return result
    result[period - 1] = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def _hh_series(highs: list, lookback: int = 336) -> list:
    """O(n) higher-high via sliding-window maximum (monotone deque)."""
    n = len(highs)
    half = max(1, lookback // 2)
    result = [None] * n
    roll_mx = [None] * n
    dq: deque = deque()
    for i in range(n):
        while dq and dq[0] < i - half + 1:
            dq.popleft()
        while dq and highs[dq[-1]] <= highs[i]:
            dq.pop()
        dq.append(i)
        if i >= half - 1:
            roll_mx[i] = highs[dq[0]]
    for i in range(lookback - 1, n):
        rm_r = roll_mx[i]
        rm_o = roll_mx[i - half]
        if rm_r is not None and rm_o is not None:
            result[i] = rm_r > rm_o
    return result


def _vol_signal_series(vols: list) -> list:
    """O(n) volume-trend signal (+1/0/-1) via prefix sums."""
    n = len(vols)
    result = [0] * n
    if n < 14:
        return result
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(vols):
        prefix[i + 1] = prefix[i] + v
    for i in range(13, n):
        v7 = (prefix[i + 1] - prefix[i - 6]) / 7
        v7p = (prefix[i - 6] - prefix[i - 13]) / 7
        if v7p > 0:
            r = v7 / v7p
            if r > 1.1:
                result[i] = 1
            elif r < 0.9:
                result[i] = -1
    return result


def _rsi_series(closes: list, period: int = 14) -> list:
    """Wilder RSI. None during warmup."""
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    result[period] = 100.0 - 100.0 / (1 + (avg_g / avg_l if avg_l else 100.0))
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        result[i + 1] = 100.0 - 100.0 / (1 + (avg_g / avg_l if avg_l else 100.0))
    return result


def _bb_series(prices: list, period: int = 20, num_std: float = 2.0) -> Tuple[list, list]:
    """Bollinger Bands (upper, lower) in O(n) via prefix sums."""
    n = len(prices)
    bb_u = [None] * n
    bb_l = [None] * n
    if n < period:
        return bb_u, bb_l
    psum = [0.0] * (n + 1)
    psum2 = [0.0] * (n + 1)
    for i, p in enumerate(prices):
        psum[i + 1] = psum[i] + p
        psum2[i + 1] = psum2[i] + p * p
    for i in range(period - 1, n):
        s = psum[i + 1] - psum[i - period + 1]
        s2 = psum2[i + 1] - psum2[i - period + 1]
        mean = s / period
        std = max(0.0, s2 / period - mean * mean) ** 0.5
        bb_u[i] = mean + num_std * std
        bb_l[i] = mean - num_std * std
    return bb_u, bb_l


def _vol10_avg_series(vols: list) -> list:
    """10-period volume moving average via prefix sums."""
    n = len(vols)
    result = [None] * n
    if n < 10:
        return result
    psum = [0.0] * (n + 1)
    for i, v in enumerate(vols):
        psum[i + 1] = psum[i] + v
    for i in range(9, n):
        result[i] = (psum[i + 1] - psum[i - 9]) / 10
    return result


# ── Live candle fetch ─────────────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str = "1h", limit: int = 500) -> Dict[str, list]:
    """Fetch recent klines from Binance's public REST API (no auth needed)."""
    resp = requests.get(
        _KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()
    return {
        "open":   [float(k[1]) for k in klines],
        "high":   [float(k[2]) for k in klines],
        "low":    [float(k[3]) for k in klines],
        "close":  [float(k[4]) for k in klines],
        "volume": [float(k[5]) for k in klines],
    }


# ── Regime state machine (Layers 1, 2, 4 of the backtest Mixed engine) ──────

def compute_regime_state(
    candles: Dict[str, list],
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
) -> Dict[str, Any]:
    """
    Replay the regime score + 5-candle confirmation smoothing + extremity
    override state machine over `candles`, returning the FINAL state as of
    the last candle.

    Returns:
        confirmed        BULLISH | BEARISH | NEUTRAL
        active_mode      BUY | SELL | WAIT
        override_active  bool
        override_type    "OVERBOUGHT" | "OVERSOLD" | None
        score            latest regime score (-5..+5)
        warmup_ok        bool — False if fewer than 200 candles (EMA-200 not ready)
        indicators       dict of latest ema21/ema50/ema200/rsi/bb_upper/bb_lower/price
        series           precomputed indicator series, for score_entry()
    """
    closes = candles["close"]
    highs = candles["high"]
    vols = candles.get("volume", [])
    n = len(closes)

    ema21_s = _ema(closes, 21)
    ema50_s = _ema(closes, 50)
    ema200_s = _ema(closes, 200)
    hh_s = _hh_series(highs, lookback=336)
    vol_sig = _vol_signal_series(vols) if vols else [0] * n
    rsi_s = _rsi_series(closes, 14)
    bb_u_s, bb_l_s = _bb_series(closes, 20, 2.0)
    vol10_s = _vol10_avg_series(vols) if vols else [None] * n

    reg_buf: deque = deque(maxlen=5)
    confirmed = "NEUTRAL"
    active_mode = "WAIT"
    last_switch_at = -10
    override_active = False
    override_type = None
    override_saw_exit = False
    score = 0

    for i in range(n):
        price = closes[i]

        # ── Layer 1 — regime score ───────────────────────────────────────
        score = 0
        e50, e200 = ema50_s[i], ema200_s[i]
        if e50 is not None:
            score += 1 if price > e50 else -1
        if e200 is not None:
            score += 1 if price > e200 else -1
        if e50 is not None and e200 is not None:
            score += 1 if e50 > e200 else -1
        hh = hh_s[i]
        if hh is not None:
            score += 1 if hh else -1
        score += vol_sig[i]

        raw = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"

        # ── Layer 2 — 5-candle confirmation smoothing ───────────────────
        if raw == "NEUTRAL":
            reg_buf.clear()
        else:
            reg_buf.append(raw)

        regime_just_switched = False
        if (len(reg_buf) == 5 and len(set(reg_buf)) == 1
                and list(reg_buf)[0] != confirmed and (i - last_switch_at) >= 5):
            new_r = list(reg_buf)[0]
            last_switch_at = i
            reg_buf.clear()
            regime_just_switched = True

            if override_active:
                override_active = False
                override_type = None
                override_saw_exit = False

            confirmed = new_r
            active_mode = "BUY" if new_r == "BULLISH" else "SELL"

        # ── Warmup guard ──────────────────────────────────────────────────
        if ema200_s[i] is None:
            continue

        # ── Layer 4 — extremity overrides ────────────────────────────────
        rsi_val = rsi_s[i]
        indicators_ready = rsi_val is not None and bb_u_s[i] is not None

        if not regime_just_switched and indicators_ready:
            bb_pos = "INSIDE"
            if bb_u_s[i] is not None and price > bb_u_s[i]:
                bb_pos = "ABOVE_UPPER"
            elif bb_l_s[i] is not None and price < bb_l_s[i]:
                bb_pos = "BELOW_LOWER"

            if override_active:
                if override_type == "OVERBOUGHT":
                    if score < 2:
                        override_saw_exit = True
                    elif override_saw_exit:
                        override_active = False
                        override_type = None
                        override_saw_exit = False
                        active_mode = "BUY"
                else:  # OVERSOLD
                    if score > -2:
                        override_saw_exit = True
                    elif override_saw_exit:
                        override_active = False
                        override_type = None
                        override_saw_exit = False
                        active_mode = "SELL"

            elif confirmed in ("BULLISH", "BEARISH"):
                if confirmed == "BULLISH" and (rsi_val > rsi_overbought or bb_pos == "ABOVE_UPPER"):
                    override_active = True
                    override_type = "OVERBOUGHT"
                    override_saw_exit = False
                    active_mode = "SELL"
                elif confirmed == "BEARISH" and (rsi_val < rsi_oversold or bb_pos == "BELOW_LOWER"):
                    override_active = True
                    override_type = "OVERSOLD"
                    override_saw_exit = False
                    active_mode = "BUY"

    return {
        "confirmed": confirmed,
        "active_mode": active_mode,
        "override_active": override_active,
        "override_type": override_type,
        "score": score,
        "warmup_ok": ema200_s[-1] is not None,
        "indicators": {
            "price": closes[-1],
            "ema21": ema21_s[-1],
            "ema50": ema50_s[-1],
            "ema200": ema200_s[-1],
            "rsi": rsi_s[-1],
            "bb_upper": bb_u_s[-1],
            "bb_lower": bb_l_s[-1],
        },
        "series": {
            "close": closes,
            "open": candles.get("open", []),
            "high": highs,
            "low": candles.get("low", []),
            "volume": vols,
            "rsi": rsi_s,
            "ema21": ema21_s,
            "ema50": ema50_s,
            "vol10": vol10_s,
        },
    }


# ── Layer 3 — entry indicator (latest candle only) ──────────────────────────

def score_entry(active_mode: str, series: Dict[str, list]) -> Tuple[int, List[str]]:
    """
    Entry indicator score for the latest candle (mirrors
    _entry_score_buy/_entry_score_sell from the backtest).

    active_mode: "BUY" or "SELL"
    series:      the "series" dict returned by compute_regime_state()

    Returns (score, signals_fired). Entry is confirmed when score >= 4.
    """
    closes = series["close"]
    opens = series["open"]
    highs = series["high"]
    lows = series["low"]
    vols = series["volume"]
    rsi_s = series["rsi"]
    ema21_s = series["ema21"]
    ema50_s = series["ema50"]
    vol10_s = series["vol10"]

    i = len(closes) - 1
    rsi = rsi_s[i]
    e21 = ema21_s[i]
    e50 = ema50_s[i]
    if rsi is None or e21 is None or e50 is None:
        return 0, []

    p = closes[i]
    sc = 0
    sigs: List[str] = []

    if active_mode == "BUY":
        if abs(p - e21) / e21 <= 0.015 and p > e50:
            sc += 2
            sigs.append("EMA_RETEST")
        if rsi > 45:
            for back in range(1, 4):
                if i >= back:
                    prev = rsi_s[i - back]
                    if prev is not None and prev < 45:
                        sc += 2
                        sigs.append("RSI_TURN")
                        break
        if vols:
            v10 = vol10_s[i]
            if v10 and v10 > 0 and vols[i] > 1.5 * v10:
                green = (closes[i] > opens[i]) if opens else (closes[i] > (highs[i] + lows[i]) / 2)
                if green:
                    sc += 1
                    sigs.append("VOLUME")
    else:  # SELL
        if abs(p - e21) / e21 <= 0.015 and p < e50:
            sc += 2
            sigs.append("EMA_RETEST")
        if rsi < 55:
            for back in range(1, 4):
                if i >= back:
                    prev = rsi_s[i - back]
                    if prev is not None and prev > 55:
                        sc += 2
                        sigs.append("RSI_TURN")
                        break
        if vols:
            v10 = vol10_s[i]
            if v10 and v10 > 0 and vols[i] > 1.5 * v10:
                red = (closes[i] < opens[i]) if opens else (closes[i] < (highs[i] + lows[i]) / 2)
                if red:
                    sc += 1
                    sigs.append("VOLUME")

    return sc, sigs
