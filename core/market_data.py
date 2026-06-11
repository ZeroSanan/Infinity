"""
Market Data — fetches OHLC klines for the Market Signals engine.

Two sources are supported:
  - "binance"    crypto pairs via Binance public klines (no API key)
  - "twelvedata" stocks/forex/commodities via Twelve Data /time_series
                 (requires TWELVE_DATA_API_KEY in .env — free tier:
                 https://twelvedata.com/pricing)
"""

from __future__ import annotations

import os

import requests

_BINANCE_KLINES_URL    = "https://api.binance.com/api/v3/klines"
_TWELVE_DATA_TIMESERIES_URL = "https://api.twelvedata.com/time_series"


def _twelve_data_key() -> str:
    return os.environ.get("TWELVE_DATA_API_KEY", "")


def fetch_klines(source: str, symbol: str, limit: int = 200):
    """Return (closes, highs, lows) as ascending-time lists of floats.

    Raises ValueError if the source/symbol is misconfigured or returns
    insufficient data (fewer than 55 candles — the minimum needed for
    EMA50/RSI-based signals).
    """
    if source == "binance":
        resp = requests.get(
            _BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": "4h", "limit": limit},
            timeout=8,
        )
        klines = resp.json()
        if not isinstance(klines, list) or len(klines) < 55:
            raise ValueError("insufficient data")
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        return closes, highs, lows

    if source == "twelvedata":
        key = _twelve_data_key()
        if not key:
            raise ValueError("Twelve Data API key not configured")
        resp = requests.get(
            _TWELVE_DATA_TIMESERIES_URL,
            params={"symbol": symbol, "interval": "4h", "outputsize": limit, "apikey": key},
            timeout=8,
        )
        data = resp.json()
        values = data.get("values")
        if not values or len(values) < 55:
            raise ValueError(data.get("message") or "insufficient data")
        # Twelve Data returns most-recent-first; reverse to ascending time
        values = list(reversed(values))
        closes = [float(v["close"]) for v in values]
        highs  = [float(v["high"])  for v in values]
        lows   = [float(v["low"])   for v in values]
        return closes, highs, lows

    raise ValueError(f"unknown market data source: {source}")
