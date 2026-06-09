"""
ML Signals — three lightweight models trained on-the-fly from Binance klines.

1. predict_direction  — RandomForest: will next 4h candle close UP or DOWN?
2. classify_regime    — GradientBoosting: BULL / NEUTRAL / BEAR with confidence %
3. score_entry        — Rule-weighted composite: 0-100 entry quality score
"""

from __future__ import annotations
import numpy as np


# ── Shared technical helpers ──────────────────────────────────────────────────

def _ema(data: list, period: int) -> list:
    k = 2.0 / (period + 1)
    out = [data[0]]
    for p in data[1:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def _rsi(closes: list, period: int = 14) -> float:
    diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0)  for d in diffs[-period:]]
    losses = [max(-d, 0) for d in diffs[-period:]]
    ag = sum(gains)  / period
    al = sum(losses) / period
    return 100.0 - (100.0 / (1 + ag / al)) if al else 100.0


def _features(closes: list, highs: list, lows: list, idx: int):
    """10-feature vector at candle `idx`. Returns None if not enough history."""
    c = closes[:idx + 1]
    h = highs[:idx + 1]
    l = lows[:idx + 1]
    if len(c) < 52:
        return None

    price    = c[-1]
    ema20    = _ema(c, 20)[-1]
    ema50    = _ema(c, 50)[-1]
    rsi14    = _rsi(c, 14)

    r1  = c[-1] / c[-2]  - 1
    r3  = c[-1] / c[-4]  - 1 if len(c) >= 4  else 0.0
    r7  = c[-1] / c[-8]  - 1 if len(c) >= 8  else 0.0
    r14 = c[-1] / c[-15] - 1 if len(c) >= 15 else 0.0

    win  = min(21, len(c))
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - win + 1, len(c))]
    vol  = float(np.std(rets)) if rets else 0.0

    h20      = max(h[-20:])
    l20      = min(l[-20:])
    price_pos = (price - l20) / (h20 - l20) if h20 != l20 else 0.5

    return [
        rsi14 / 100,
        r1, r3, r7, r14,
        vol,
        price_pos,
        price / ema20 - 1,
        price / ema50 - 1,
        ema20  / ema50 - 1,
    ]


# ── Model 1: Price direction ──────────────────────────────────────────────────

def predict_direction(closes: list, highs: list, lows: list) -> dict:
    """
    Predict whether the next 4h candle closes UP or DOWN.
    Returns {"direction": "UP"|"DOWN", "confidence": float, "note": str}
    """
    try:
        from sklearn.ensemble import RandomForestClassifier

        n = len(closes)
        if n < 80:
            return {"direction": "UNKNOWN", "confidence": 0.0, "note": "insufficient data"}

        X, y = [], []
        for i in range(60, n - 1):
            feat = _features(closes, highs, lows, i)
            if feat is None:
                continue
            X.append(feat)
            y.append(1 if closes[i + 1] > closes[i] else 0)

        if len(X) < 30 or len(set(y)) < 2:
            return {"direction": "UNKNOWN", "confidence": 0.0, "note": "insufficient variance"}

        X, y = np.array(X, dtype=float), np.array(y)
        clf = RandomForestClassifier(n_estimators=60, max_depth=4, random_state=42, n_jobs=1)
        clf.fit(X[:-5], y[:-5])

        feat_now = _features(closes, highs, lows, n - 1)
        if feat_now is None:
            return {"direction": "UNKNOWN", "confidence": 0.0}

        proba = clf.predict_proba([feat_now])[0]
        pred  = int(clf.predict([feat_now])[0])
        conf  = round(float(max(proba)) * 100, 1)

        return {"direction": "UP" if pred == 1 else "DOWN", "confidence": conf}

    except ImportError:
        return {"direction": "UNKNOWN", "confidence": 0.0, "note": "scikit-learn not installed"}
    except Exception as e:
        return {"direction": "UNKNOWN", "confidence": 0.0, "note": str(e)[:60]}


# ── Model 2: Regime classifier ────────────────────────────────────────────────

def classify_regime(
    closes: list, highs: list, lows: list,
    rule_score: int, rule_regime: str,
) -> dict:
    """
    Classify market regime with ML confidence.
    Labels are generated from rule-based signals (pseudo-labels).
    Returns {"regime": str, "confidence": float, "agrees_with_rules": bool}
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier

        n = len(closes)
        if n < 80:
            return {"regime": rule_regime, "confidence": 50.0, "agrees_with_rules": True}

        X, y = [], []
        for i in range(60, n - 1):
            feat = _features(closes, highs, lows, i)
            if feat is None:
                continue
            c = closes[:i + 1]

            e50  = _ema(c, 50)[-1]
            e200 = _ema(c, min(200, len(c)))[-1]
            rsi  = _rsi(c)
            n7   = min(42, len(c) - 1)
            ret7 = (c[-1] - c[-n7 - 1]) / c[-n7 - 1] * 100

            sc = int(c[-1] > e50) + int(e50 > e200) + int(rsi > 55) + int(ret7 > 0)
            label = 2 if sc >= 3 else (0 if sc <= 1 else 1)
            X.append(feat)
            y.append(label)

        if len(X) < 30 or len(set(y)) < 2:
            return {"regime": rule_regime, "confidence": 50.0 + rule_score * 8, "agrees_with_rules": True}

        X, y = np.array(X, dtype=float), np.array(y)
        clf = GradientBoostingClassifier(n_estimators=60, max_depth=3, random_state=42)
        clf.fit(X, y)

        feat_now = _features(closes, highs, lows, n - 1)
        if feat_now is None:
            return {"regime": rule_regime, "confidence": 50.0, "agrees_with_rules": True}

        proba    = clf.predict_proba([feat_now])[0]
        pred     = int(clf.predict([feat_now])[0])
        conf     = round(float(max(proba)) * 100, 1)
        label_map = {0: "BEAR", 1: "NEUTRAL", 2: "BULL"}
        ml_regime = label_map.get(pred, rule_regime)

        # Blend: weight ML confidence + rule-score alignment
        blended = round((conf * 0.65 + (rule_score / 4 * 100) * 0.35), 1)

        return {
            "regime":            ml_regime,
            "confidence":        blended,
            "agrees_with_rules": ml_regime == rule_regime,
        }

    except ImportError:
        return {"regime": rule_regime, "confidence": 50.0, "agrees_with_rules": True,
                "note": "scikit-learn not installed"}
    except Exception as e:
        return {"regime": rule_regime, "confidence": 50.0, "agrees_with_rules": True,
                "note": str(e)[:60]}


# ── Model 3: Entry quality score ──────────────────────────────────────────────

def score_entry(closes: list, highs: list, lows: list, rec: str) -> dict:
    """
    Score entry quality 0–100 based on current market conditions.
    rec: "Long DCA" | "Short DCA" | "Mixed DCA"
    Returns {"score": int, "grade": str, "factors": list[tuple]}
    """
    try:
        n = len(closes)
        if n < 50:
            return {"score": 50, "grade": "C", "factors": []}

        price = closes[-1]
        rsi   = _rsi(closes)
        ema50 = _ema(closes, 50)[-1]
        ema20 = _ema(closes, 20)[-1]

        h20 = max(highs[-20:])
        l20 = min(lows[-20:])
        pos = (price - l20) / (h20 - l20) if h20 != l20 else 0.5

        win  = min(21, n)
        rets = [closes[i] / closes[i - 1] - 1 for i in range(n - win + 1, n)]
        vol  = float(np.std(rets)) * 100 if rets else 2.0

        n7   = min(42, n - 1)
        ret7 = (closes[-1] - closes[-n7 - 1]) / closes[-n7 - 1] * 100

        score   = 50
        factors = []

        if rec == "Long DCA":
            # Ideal long entry: oversold RSI, price at range bottom, recent drop
            if rsi < 30:
                score += 25; factors.append(("RSI extremely oversold",  "+25", True))
            elif rsi < 40:
                score += 15; factors.append(("RSI oversold",            "+15", True))
            elif rsi < 50:
                score += 5;  factors.append(("RSI below midline",       "+5",  True))
            elif rsi > 70:
                score -= 20; factors.append(("RSI overbought — wait",   "−20", False))
            elif rsi > 60:
                score -= 10; factors.append(("RSI elevated",            "−10", False))
            else:
                factors.append((f"RSI {rsi:.0f} neutral",               "0",   None))

            if pos < 0.20:
                score += 20; factors.append(("Price at 20-day low",     "+20", True))
            elif pos < 0.40:
                score += 10; factors.append(("Price in lower range",    "+10", True))
            elif pos > 0.80:
                score -= 15; factors.append(("Price near 20-day high",  "−15", False))

            if ret7 < -15:
                score += 15; factors.append(("Big weekly drop",         "+15", True))
            elif ret7 < -7:
                score += 8;  factors.append(("Weekly decline",          "+8",  True))
            elif ret7 > 10:
                score -= 10; factors.append(("Price rising this week",  "−10", False))

            if price < ema20 < ema50:
                score += 5;  factors.append(("Below both EMAs — deep discount", "+5", True))

        else:  # Short DCA or Mixed
            # Ideal short entry: overbought RSI, price at range top, recent pump
            if rsi > 70:
                score += 25; factors.append(("RSI extremely overbought","+ 25", True))
            elif rsi > 60:
                score += 15; factors.append(("RSI overbought",          "+15", True))
            elif rsi > 50:
                score += 5;  factors.append(("RSI above midline",       "+5",  True))
            elif rsi < 30:
                score -= 20; factors.append(("RSI oversold — risky short","−20", False))
            elif rsi < 40:
                score -= 10; factors.append(("RSI low — weak short",    "−10", False))
            else:
                factors.append((f"RSI {rsi:.0f} neutral",               "0",   None))

            if pos > 0.80:
                score += 20; factors.append(("Price at 20-day high",    "+20", True))
            elif pos > 0.60:
                score += 10; factors.append(("Price in upper range",    "+10", True))
            elif pos < 0.20:
                score -= 15; factors.append(("Price at 20-day low",     "−15", False))

            if ret7 > 15:
                score += 15; factors.append(("Big weekly pump",         "+15", True))
            elif ret7 > 7:
                score += 8;  factors.append(("Weekly gain",             "+8",  True))
            elif ret7 < -10:
                score -= 10; factors.append(("Price falling this week", "−10", False))

            if price > ema20 > ema50:
                score += 5;  factors.append(("Above both EMAs — extended", "+5", True))

        # Volatility adjustment (both directions)
        if vol > 5:
            score -= 8;  factors.append(("Very high volatility",        "−8",  False))
        elif vol > 3:
            score -= 3;  factors.append(("Elevated volatility",         "−3",  False))
        elif vol < 1.2:
            score += 5;  factors.append(("Low volatility — stable",     "+5",  True))

        score = max(0, min(100, score))

        if   score >= 80: grade = "A"
        elif score >= 65: grade = "B"
        elif score >= 50: grade = "C"
        elif score >= 35: grade = "D"
        else:             grade = "F"

        return {"score": score, "grade": grade, "factors": factors[:4]}

    except Exception as e:
        return {"score": 50, "grade": "C", "factors": [], "note": str(e)[:60]}
