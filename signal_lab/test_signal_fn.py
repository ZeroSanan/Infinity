"""
signal_lab.test_signal_fn — sanity check for signal_lab.signal_fn.get_verdict().

Loads a small slice of a historical OHLCV CSV from algo-trading/data/ (if one
exists) and prints the verdict for the last candle in that slice, proving
get_verdict() runs standalone — no Binance calls, no engine, no file writes.

If no CSV is found in algo-trading/data/ (e.g. in an environment without the
pre-downloaded historical datasets), a small synthetic OHLCV series is
generated instead so this script can still be run end-to-end.

Usage:
    python3 signal_lab/test_signal_fn.py [path/to/candles.csv]
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from signal_fn import get_verdict  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "algo-trading", "data")

# How many trailing candles to feed in as the point-in-time window.
WINDOW = 300


def _find_csv() -> str | None:
    if len(sys.argv) > 1:
        return sys.argv[1]
    matches = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    return matches[0] if matches else None


def _load_candles_from_csv(path: str) -> dict:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lower()

    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing required column(s) {missing}; found {list(df.columns)}")

    df = df.tail(WINDOW).reset_index(drop=True)

    candles = {
        "open": df["open"].astype(float).tolist(),
        "high": df["high"].astype(float).tolist(),
        "low": df["low"].astype(float).tolist(),
        "close": df["close"].astype(float).tolist(),
    }
    if "volume" in df.columns:
        candles["volume"] = df["volume"].astype(float).tolist()
    return candles


def _synthetic_candles(n: int = WINDOW, seed: int = 42) -> dict:
    """Deterministic random-walk OHLCV, used only when no CSV is available."""
    rng = np.random.default_rng(seed)
    price = 100.0
    opens, highs, lows, closes, vols = [], [], [], [], []
    for _ in range(n):
        o = price
        drift = rng.normal(0, 0.6)
        c = max(0.01, o + drift)
        h = max(o, c) + abs(rng.normal(0, 0.3))
        l = min(o, c) - abs(rng.normal(0, 0.3))
        v = abs(rng.normal(1000, 200))
        opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
        price = c
    return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}


def main():
    csv_path = _find_csv()

    if csv_path and os.path.isfile(csv_path):
        print(f"Loading candle window from: {csv_path}")
        candles = _load_candles_from_csv(csv_path)
        source = csv_path
    else:
        print(f"No CSV found in {DATA_DIR} — using a synthetic OHLCV series instead.")
        candles = _synthetic_candles()
        source = "synthetic"

    n = len(candles["close"])
    print(f"Source: {source}")
    print(f"Window size: {n} candles")
    print(f"Last close: {candles['close'][-1]:.4f}")

    verdict = get_verdict(candles)

    print("\n--- get_verdict() result (as of last candle) ---")
    for k, v in verdict.items():
        print(f"  {k:15s}: {v}")


if __name__ == "__main__":
    main()
