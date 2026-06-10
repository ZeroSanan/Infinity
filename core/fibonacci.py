"""
Fibonacci Levels — auto-detected swing-based retracement & extension levels.

Scans the most recent `lookback` candles for the swing high and swing low,
then derives the standard Fibonacci retracement levels (23.6 / 38.2 / 50 /
61.8 / 78.6%) between them, plus extension levels (127.2 / 161.8%) projected
beyond the swing as potential support/resistance targets.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict

# (ratio, display label)
RETRACEMENT_RATIOS = [
    (0.236, "23.6%"), (0.382, "38.2%"), (0.5, "50%"), (0.618, "61.8%"), (0.786, "78.6%"),
]
EXTENSION_RATIOS = [
    (1.272, "127.2%"), (1.618, "161.8%"),
]


@dataclass
class FibLevel:
    ratio:    float
    label:    str
    price:    float
    kind:     str   # "retracement" | "extension"
    position: str   # "above_price" | "at_price" | "below_price"


@dataclass
class FibonacciAnalysis:
    swing_high:    float
    swing_low:     float
    direction:     str   # "UP" | "DOWN"
    current_price: float
    levels:        list  # list[FibLevel]

    def to_dict(self) -> dict:
        return asdict(self)


def from_binance_klines(klines: list) -> list[dict]:
    """Convert raw Binance kline rows [open_time, open, high, low, close, ...] to candle dicts."""
    return [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4])} for k in klines]


def _decimals(price: float) -> int:
    return 4 if price < 10 else (2 if price < 1000 else 0)


def _position(level_price: float, current_price: float) -> str:
    """Classify a level as above/at/below the current price (within 1% = "at")."""
    if not current_price:
        return "at_price"
    diff_pct = abs(level_price - current_price) / current_price * 100
    if diff_pct <= 1.0:
        return "at_price"
    return "above_price" if level_price > current_price else "below_price"


def compute_fibonacci(candles: list, lookback: int = 100) -> FibonacciAnalysis:
    """
    Auto-detect the swing high/low within the last `lookback` candles and
    compute Fibonacci retracement + extension levels relative to the
    current price.

    Each candle must support 'high' and 'low' keys (and optionally 'close',
    used as the current price — falls back to the last candle's high).
    """
    if not candles:
        raise ValueError("candles is empty")

    window = candles[-lookback:]
    highs  = [c["high"] for c in window]
    lows   = [c["low"]  for c in window]

    swing_high = max(highs)
    swing_low  = min(lows)

    # Most recent occurrence of each extreme determines the trend direction:
    # if the high was set more recently than the low, the latest move was
    # low → high, i.e. an UP trend (and vice versa for DOWN).
    high_idx = len(highs) - 1 - highs[::-1].index(swing_high)
    low_idx  = len(lows)  - 1 - lows[::-1].index(swing_low)
    direction = "UP" if high_idx > low_idx else "DOWN"

    last = candles[-1]
    current_price = float(last.get("close", last["high"]))
    dec  = _decimals(current_price)
    span = swing_high - swing_low

    levels = []
    for ratio, pct in RETRACEMENT_RATIOS:
        price = (swing_high - span * ratio) if direction == "UP" else (swing_low + span * ratio)
        price = round(price, dec)
        levels.append(FibLevel(
            ratio=ratio, label=f"Fib {pct} — ${price:,.{dec}f}",
            price=price, kind="retracement",
            position=_position(price, current_price),
        ))

    for ratio, pct in EXTENSION_RATIOS:
        if direction == "UP":
            price = swing_high + span * (ratio - 1)
        else:
            price = swing_low - span * (ratio - 1)
        price = round(price, dec)
        levels.append(FibLevel(
            ratio=ratio, label=f"Fib {pct} — ${price:,.{dec}f}",
            price=price, kind="extension",
            position=_position(price, current_price),
        ))

    levels.sort(key=lambda lv: lv.price, reverse=True)

    return FibonacciAnalysis(
        swing_high=round(swing_high, dec),
        swing_low=round(swing_low, dec),
        direction=direction,
        current_price=round(current_price, dec),
        levels=levels,
    )
