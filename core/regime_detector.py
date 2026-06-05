"""
Weekly Market Regime Detector

Fetches candle data, external sentiment/macro signals, computes technical
indicators from scratch, and scores the current market regime.

Importable standalone — no dependency on dca_engine or any app state.
"""
from __future__ import annotations

import json
import math
import os
import time
import datetime
from typing import Optional

import requests

_ROOT     = os.path.join(os.path.dirname(__file__), "..")
_DATA_DIR = os.path.join(_ROOT, "data")

_BINANCE_API  = "https://api.binance.com"
_COINGECKO    = "https://api.coingecko.com/api/v3"
_FNG_API      = "https://api.alternative.me/fng/"
_TIMEOUT      = 12  # seconds per request
_DOM_HIST     = os.path.join(_DATA_DIR, "btc_dominance_history.json")


class RegimeDetector:
    """
    Scores the market regime on a -7 … +7 scale and classifies it as
    BULLISH / NEUTRAL / BEARISH with a confidence level.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze(self, symbol: str = "BTCUSDT") -> dict:
        """
        Full regime analysis.  Returns a structured dict; never raises.
        On partial failure the result includes a 'warnings' list.
        """
        warnings: list[str] = []

        # ── 1. Candle data (Binance public REST) ──────────────────────────────
        candles = self._fetch_candles(symbol)
        if not candles or len(candles) < 50:
            return {"error": "Insufficient candle data from Binance", "symbol": symbol}

        closes  = [float(c[4]) for c in candles]
        highs   = [float(c[2]) for c in candles]
        lows    = [float(c[3]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        price   = closes[-1]

        # ── 2. External data ──────────────────────────────────────────────────
        fear_greed, w = self._fetch_fear_greed()
        if w:
            warnings.append(w)

        btc_dom, w = self._fetch_btc_dominance()
        if w:
            warnings.append(w)

        # ── 3. Indicators ─────────────────────────────────────────────────────
        ema50  = self._ema(closes, 50)
        ema200 = self._ema(closes, 200)
        atr    = self._atr(highs, lows, closes, 14)
        bb_upper, bb_mid, bb_lower = self._bollinger(closes, 20, 2.0)
        vol_trend    = self._volume_trend(volumes)
        higher_highs = self._higher_highs(closes)

        dom_falling = dom_rising = False
        if btc_dom is not None:
            dom_avg      = self._dominance_7d_avg(btc_dom)
            dom_falling  = btc_dom < dom_avg
            dom_rising   = btc_dom > dom_avg

        # ── 4. Score ──────────────────────────────────────────────────────────
        score   = 0
        signals = []

        def _add(cond: bool, pos_msg: str, neg_msg: str, skip: bool = False):
            nonlocal score
            if skip:
                return
            if cond:
                score += 1
                signals.append(pos_msg)
            else:
                score -= 1
                signals.append(neg_msg)

        _add(price > ema50,
             f"Price ${price:,.0f} above EMA50 ${ema50:,.0f} (+1)",
             f"Price ${price:,.0f} below EMA50 ${ema50:,.0f} (-1)")

        _add(price > ema200,
             f"Price ${price:,.0f} above EMA200 ${ema200:,.0f} (+1)",
             f"Price ${price:,.0f} below EMA200 ${ema200:,.0f} (-1)")

        _add(ema50 > ema200,
             f"Golden cross: EMA50 > EMA200 (+1)",
             f"Death cross: EMA50 < EMA200 (-1)")

        _add(higher_highs,
             "Higher highs confirmed (+1)",
             "No higher highs (-1)")

        if vol_trend > 1.1:
            score += 1
            signals.append(f"Volume surge {vol_trend:.2f}x vs prior week (+1)")
        elif vol_trend < 0.9:
            score -= 1
            signals.append(f"Volume declining {vol_trend:.2f}x vs prior week (-1)")

        if fear_greed is not None:
            if fear_greed > 55:
                score += 1
                signals.append(f"Fear & Greed bullish: {fear_greed}/100 (+1)")
            elif fear_greed < 45:
                score -= 1
                signals.append(f"Fear & Greed bearish: {fear_greed}/100 (-1)")

        if btc_dom is not None:
            if dom_falling:
                score += 1
                signals.append(f"BTC dominance falling: {btc_dom:.1f}% (+1)")
            elif dom_rising:
                score -= 1
                signals.append(f"BTC dominance rising: {btc_dom:.1f}% (-1)")

        # ── 5. Regime ─────────────────────────────────────────────────────────
        if score >= 3:
            regime         = "BULLISH"
            recommendation = "BUY_DCA"
        elif score <= -3:
            regime         = "BEARISH"
            recommendation = "SHORT_DCA"
        else:
            regime         = "NEUTRAL"
            recommendation = "WAIT"

        confidence = "HIGH" if abs(score) >= 5 else "MEDIUM" if abs(score) >= 3 else "LOW"

        result: dict = {
            "symbol":         symbol,
            "timestamp":      datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "score":          score,
            "regime":         regime,
            "recommendation": recommendation,
            "confidence":     confidence,
            "indicators": {
                "price":        round(price,    2),
                "ema50":        round(ema50,    2),
                "ema200":       round(ema200,   2),
                "atr":          round(atr,      2),
                "bb_upper":     round(bb_upper, 2),
                "bb_middle":    round(bb_mid,   2),
                "bb_lower":     round(bb_lower, 2),
                "volume_trend": round(vol_trend, 3),
                "fear_greed":   fear_greed,
                "btc_dominance": round(btc_dom, 2) if btc_dom is not None else None,
                "higher_highs": higher_highs,
            },
            "signals": signals,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    def get_ai_narrative(self, analysis: dict) -> str:
        """
        Calls Anthropic claude-sonnet-4-20250514 to produce a 3-paragraph
        analyst note.  Returns a plain string; never raises.
        """
        try:
            import anthropic as _anthropic  # already in requirements.txt

            client = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

            ind  = analysis.get("indicators", {})
            sigs = "\n".join(f"  - {s}" for s in analysis.get("signals", []))

            prompt = (
                f"You are a professional crypto market analyst. "
                f"Below is a quantitative regime analysis for {analysis.get('symbol','BTCUSDT')}.\n\n"
                f"Regime : {analysis.get('regime')} (score {analysis.get('score'):+d})\n"
                f"Confidence : {analysis.get('confidence')}\n"
                f"Recommendation : {analysis.get('recommendation')}\n"
                f"Timestamp : {analysis.get('timestamp')}\n\n"
                f"Key indicators:\n"
                f"  Price       ${ind.get('price', '?'):,.2f}\n"
                f"  EMA50       ${ind.get('ema50', '?'):,.2f}\n"
                f"  EMA200      ${ind.get('ema200', '?'):,.2f}\n"
                f"  ATR(14)     ${ind.get('atr', '?'):,.2f}\n"
                f"  BB upper    ${ind.get('bb_upper', '?'):,.2f}\n"
                f"  BB lower    ${ind.get('bb_lower', '?'):,.2f}\n"
                f"  Volume trend {ind.get('volume_trend', '?'):.2f}x\n"
                f"  Fear & Greed {ind.get('fear_greed', 'N/A')}/100\n"
                f"  BTC dom.    {ind.get('btc_dominance', 'N/A')}%\n"
                f"  Higher highs {ind.get('higher_highs', 'N/A')}\n\n"
                f"Signals fired:\n{sigs}\n\n"
                f"Write exactly 3 paragraphs — no headers, no bullets, no markdown:\n"
                f"Paragraph 1: What the technical picture says (EMAs, Bollinger Bands, ATR, price position, volume).\n"
                f"Paragraph 2: What sentiment and macro say (Fear & Greed index, BTC dominance trend).\n"
                f"Paragraph 3: The recommended posture and the two most important risks to monitor.\n\n"
                f"Be direct. Use the actual numbers. Write as a concise analyst note."
            )

            msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text
        except Exception as e:
            return f"[AI narrative unavailable: {e}]"

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def _fetch_candles(self, symbol: str, interval: str = "1d", limit: int = 200) -> list:
        try:
            r = requests.get(
                f"{_BINANCE_API}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    def _fetch_fear_greed(self) -> tuple[Optional[int], Optional[str]]:
        try:
            r = requests.get(_FNG_API, params={"limit": 1}, timeout=_TIMEOUT)
            r.raise_for_status()
            return int(r.json()["data"][0]["value"]), None
        except Exception as e:
            return None, f"Fear & Greed unavailable: {e}"

    def _fetch_btc_dominance(self) -> tuple[Optional[float], Optional[str]]:
        try:
            r = requests.get(f"{_COINGECKO}/global", timeout=_TIMEOUT)
            r.raise_for_status()
            val = r.json()["data"]["market_cap_percentage"]["btc"]
            return float(val), None
        except Exception as e:
            return None, f"BTC dominance unavailable: {e}"

    def _dominance_7d_avg(self, current: float) -> float:
        """
        Maintains a rolling 7-day history of BTC dominance readings in
        data/btc_dominance_history.json.  Returns the average; falls back to
        current value on first run (no direction info yet).
        """
        os.makedirs(_DATA_DIR, exist_ok=True)
        history: list[dict] = []

        if os.path.exists(_DOM_HIST):
            try:
                with open(_DOM_HIST) as f:
                    history = json.load(f)
            except Exception:
                history = []

        now          = int(time.time())
        cutoff       = now - 7 * 86400
        history.append({"ts": now, "value": current})
        history      = [h for h in history if h["ts"] >= cutoff]

        try:
            with open(_DOM_HIST, "w") as f:
                json.dump(history, f)
        except Exception:
            pass

        if len(history) < 2:
            return current  # neutral — no trend yet

        return sum(h["value"] for h in history) / len(history)

    # ── Indicator math ────────────────────────────────────────────────────────

    def _ema(self, prices: list, period: int) -> float:
        """EMA seeded with SMA of the first `period` values."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        k   = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _atr(self, highs: list, lows: list, closes: list, period: int = 14) -> float:
        """Average True Range over the last `period` candles."""
        if len(closes) < period + 1:
            return 0.0
        trs = [
            max(highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]))
            for i in range(1, len(closes))
        ]
        return sum(trs[-period:]) / period

    def _bollinger(self, prices: list, period: int = 20, mult: float = 2.0) -> tuple:
        """Returns (upper, middle, lower) Bollinger Bands."""
        if len(prices) < period:
            p = prices[-1] if prices else 0.0
            return p, p, p
        window   = prices[-period:]
        middle   = sum(window) / period
        variance = sum((x - middle) ** 2 for x in window) / period
        std      = math.sqrt(variance)
        return middle + mult * std, middle, middle - mult * std

    def _volume_trend(self, volumes: list) -> float:
        """Ratio: avg volume last 7 days / avg volume prior 7 days."""
        if len(volumes) < 14:
            return 1.0
        avg_recent = sum(volumes[-7:]) / 7
        avg_prior  = sum(volumes[-14:-7]) / 7
        return avg_recent / avg_prior if avg_prior else 1.0

    def _higher_highs(self, closes: list) -> bool:
        """
        True if the latest 3 weekly samples form an ascending sequence.
        Samples: closes[-22], closes[-15], closes[-8], closes[-1].
        """
        if len(closes) < 22:
            return False
        w1, w2, w3, w4 = closes[-22], closes[-15], closes[-8], closes[-1]
        return w4 > w3 > w2
