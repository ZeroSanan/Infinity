"""
signal_lab.signal_fn — standalone, side-effect-free copy of the regime/entry
decision logic used by the live Mixed engine.

This module is a RESEARCH COPY. The indicator helpers, ``compute_regime_state``,
and ``score_entry`` below are copied verbatim from ``core/regime_live.py`` (the
live regime detector used by ``core/mixed_engine.py``) so they can be exercised
against arbitrary historical candle windows without:

  - any network/Binance calls (the original ``fetch_candles`` is NOT copied
    here — it is the only part of regime_live.py that makes live calls)
  - any engine, state file, or other side effects
  - any risk of touching the files the live trading bot reads

Nothing in ``core/`` is imported or modified by this module.

Candle window shape (matches ``core/regime_live.fetch_candles`` output and the
input to ``core.regime_live.compute_regime_state``):

    candles = {
        "open":   [float, ...],   # oldest -> newest
        "high":   [float, ...],
        "low":    [float, ...],
        "close":  [float, ...],
        "volume": [float, ...],   # optional; omit or pass [] to disable
                                   # the volume-trend signal and volume
                                   # factor in the entry indicator
    }

All lists must be the same length and in chronological order (index 0 is the
oldest candle, index -1 is the most recent / "current" candle). Only the
candles up to and including index -1 are used — i.e. callers should pass a
point-in-time slice ``candles[0:i+1]`` to evaluate the verdict "as of" candle i,
with no look-ahead into future candles.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Tuple


# ── Indicator helpers (copied from core/regime_live.py) ──────────────────────

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


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range over the last `period` candles. 0.0 if insufficient data."""
    n = len(closes)
    if n < period + 1:
        return 0.0
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, n)
    ]
    return sum(trs[-period:]) / period


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


# ── Regime state machine (copied from core/regime_live.py) ──────────────────

def compute_regime_state(
    candles: Dict[str, list],
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    atr_period: int = 14,
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
        indicators       dict of latest ema21/ema50/ema200/rsi/bb_upper/bb_lower/price/atr/atr_pct
        series           precomputed indicator series, for score_entry()
    """
    closes = candles["close"]
    highs = candles["high"]
    lows = candles.get("low", [])
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

    atr_val = _atr(highs, lows, closes, atr_period) if lows else 0.0
    atr_pct = (atr_val / closes[-1] * 100) if (atr_val > 0 and closes[-1]) else None

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
            "atr": atr_val,
            "atr_pct": atr_pct,
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


# ── Entry indicator (copied from core/regime_live.py) ────────────────────────

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


# ── New: standalone entry verdict ─────────────────────────────────────────────

ENTRY_SCORE_THRESHOLD = 4  # matches MixedEngine._arm_anchor() / mixed_dca_strategy.py


def get_verdict(
    candles: Dict[str, list],
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    atr_period: int = 14,
) -> dict:
    """
    Pure function: given a point-in-time candle window (index 0..i, oldest
    first), replay the regime state machine + entry indicator and return a
    single verdict for "right now" (the last candle in the window).

    verdict meanings:
      WAIT       - no confirmed regime direction yet (active_mode == "WAIT").
                   Mirrors the live engine sitting idle with no ladder armed.
      WATCH      - a regime direction is confirmed (active_mode == BUY/SELL,
                   i.e. confirmed BULLISH/BEARISH or an extremity override is
                   active) but the three-factor entry indicator has not fired
                   (entry score < 4). The live engine is waiting for an entry
                   signal before arming the ladder.
      LONG_NOW   - active_mode == "BUY" and the entry indicator has fired
                   (score >= 4). This is the moment MixedEngine._arm_anchor()
                   would set the long ladder's anchor price.
      SHORT_NOW  - active_mode == "SELL" and the entry indicator has fired.

    Returns:
        {
          "verdict":      "LONG_NOW" | "SHORT_NOW" | "WATCH" | "WAIT",
          "regime":       confirmed regime ("BULLISH" | "BEARISH" | "NEUTRAL"),
          "active_mode":  "BUY" | "SELL" | "WAIT",
          "rsi":          latest RSI(14), or None during warmup,
          "score":        latest regime score (-5..+5),
          "entry_score":  three-factor entry indicator score (0-5),
          "entry_signals": list of fired signal names (e.g. ["EMA_RETEST", "RSI_TURN"]),
          "override_type": "OVERBOUGHT" | "OVERSOLD" | None,
          "warmup_ok":    bool — False if fewer than 200 candles are available,
        }

    Note: this function reproduces ONLY the deterministic, rule-based logic
    in core/regime_live.py (regime score, 5-candle confirmation, extremity
    overrides, three-factor entry indicator) — the same logic that drives
    core/mixed_engine.py's active_mode and anchor-arming. The ML models in
    core/ml_signals.py (next-candle direction, ML regime classifier, entry
    quality score) are a separate, supplementary, non-deterministic overlay
    used only for dashboard display — they are NOT part of this verdict.
    """
    state = compute_regime_state(candles, rsi_overbought, rsi_oversold, atr_period)

    active_mode = state["active_mode"]
    indicators = state["indicators"]

    if active_mode == "WAIT":
        verdict = "WAIT"
        entry_score, entry_signals = 0, []
    else:
        entry_score, entry_signals = score_entry(active_mode, state["series"])
        if entry_score >= ENTRY_SCORE_THRESHOLD:
            verdict = "LONG_NOW" if active_mode == "BUY" else "SHORT_NOW"
        else:
            verdict = "WATCH"

    return {
        "verdict": verdict,
        "regime": state["confirmed"],
        "active_mode": active_mode,
        "rsi": indicators["rsi"],
        "score": state["score"],
        "entry_score": entry_score,
        "entry_signals": entry_signals,
        "override_type": state["override_type"],
        "warmup_ok": state["warmup_ok"],
    }
