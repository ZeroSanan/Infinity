"""
signal_lab.klines — standalone Binance public-kline fetcher for the Signal
Replay tool (web/app.py: POST /api/backtest/replay).

This is intentionally separate from core.regime_live.fetch_candles: it has
its own HTTP call, makes no engine/state-file side effects, and is never
imported by the live trading code. core/regime_live.py is not imported or
modified by this module.
"""
from __future__ import annotations

import requests

_KLINES_URL = "https://api.binance.com/api/v3/klines"

INTERVAL_MS = {
    "1m": 60_000, "3m": 3 * 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000,
    "30m": 30 * 60_000, "1h": 3_600_000, "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000, "6h": 6 * 3_600_000, "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000, "1d": 86_400_000, "3d": 3 * 86_400_000,
    "1w": 7 * 86_400_000,
}

# EMA-200 readiness — matches signal_fn.get_verdict()'s warmup_ok check.
WARMUP_CANDLES = 200

# signal_fn.get_verdict() is O(window) per call, so harness.run_walk() over n
# candles is O(n^2)-ish. Cap the total candle count so a Signal Scan request
# stays well within a typical web-request timeout.
MAX_SCAN_CANDLES = 2000


def fetch_replay_window(symbol: str, interval: str, t_ms: int, lookahead_n: int = 6,
                         warmup: int = WARMUP_CANDLES) -> tuple[dict, dict]:
    """
    Fetch a point-in-time candle window for Signal Replay, split into:

      - `history`: every candle whose CLOSE TIME is <= t_ms. This is the
        ONLY data that may be passed to signal_fn.get_verdict().
      - `future`: the first `lookahead_n` candles whose OPEN TIME is >= t_ms.
        Used ONLY to reveal what price did after T — never fed to the signal.

    No-look-ahead guard: the split is by each kline's own timestamps (never
    by array position), and is asserted below before returning.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    step_ms = INTERVAL_MS[interval]

    margin = 5
    start_time = t_ms - (warmup + margin) * step_ms
    end_time = t_ms + lookahead_n * step_ms
    limit = min(warmup + lookahead_n + 2 * margin, 1000)

    resp = requests.get(
        _KLINES_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
        },
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json()
    if isinstance(raw, dict):
        # Binance returns {"code": ..., "msg": ...} on error instead of a list.
        raise ValueError(raw.get("msg", "Binance API error"))
    if not raw:
        raise ValueError("No kline data returned for the requested window.")

    # ── No-look-ahead split — by timestamp, never by index ──────────────────
    history_raw = [k for k in raw if int(k[6]) <= t_ms]          # closeTime <= T
    future_raw = [k for k in raw if int(k[0]) >= t_ms][:lookahead_n]  # openTime >= T

    assert all(int(k[6]) <= t_ms for k in history_raw), \
        "look-ahead violation: a history candle closes after T"
    assert all(int(k[0]) >= t_ms for k in future_raw), \
        "look-ahead violation: a future candle opens before T"
    if history_raw and future_raw:
        assert int(history_raw[-1][6]) < int(future_raw[0][0]), \
            "look-ahead violation: history/future split overlaps at T"

    if not history_raw:
        raise ValueError("No candles closed at or before the chosen moment — pick a later date/time.")

    history = {
        "open":   [float(k[1]) for k in history_raw],
        "high":   [float(k[2]) for k in history_raw],
        "low":    [float(k[3]) for k in history_raw],
        "close":  [float(k[4]) for k in history_raw],
        "volume": [float(k[5]) for k in history_raw],
        "close_time": [int(k[6]) for k in history_raw],
    }
    future = {
        "open":   [float(k[1]) for k in future_raw],
        "high":   [float(k[2]) for k in future_raw],
        "low":    [float(k[3]) for k in future_raw],
        "close":  [float(k[4]) for k in future_raw],
        "volume": [float(k[5]) for k in future_raw],
        "open_time": [int(k[0]) for k in future_raw],
    }
    return history, future


def fetch_candle_range(symbol: str, interval: str, start_ms: int, end_ms: int,
                        warmup: int = WARMUP_CANDLES,
                        max_candles: int = MAX_SCAN_CANDLES) -> dict:
    """
    Fetch every candle covering [start_ms - warmup*step, end_ms], for the
    Signal Scan walk (signal_lab.harness.run_walk). Paginates Binance's
    1000-candle-per-request limit as needed. Returned dict is oldest-first
    and includes "open_time"/"close_time" so callers can map signal/trade
    indices back to real timestamps.

    Raises ValueError if the range would need more than `max_candles`
    candles (the walk is O(n^2)-ish — see MAX_SCAN_CANDLES).
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if end_ms <= start_ms:
        raise ValueError("end date must be after start date")
    step_ms = INTERVAL_MS[interval]

    fetch_start = start_ms - warmup * step_ms
    needed = (end_ms - fetch_start) // step_ms + 2
    if needed > max_candles:
        raise ValueError(
            f"Requested range needs ~{needed} candles at {interval} "
            f"(limit {max_candles}). Use a shorter date range or a larger interval."
        )

    all_klines: list = []
    cursor = fetch_start
    while cursor <= end_ms:
        resp = requests.get(
            _KLINES_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if isinstance(batch, dict):
            raise ValueError(batch.get("msg", "Binance API error"))
        if not batch:
            break
        all_klines.extend(batch)
        last_open = int(batch[-1][0])
        if last_open <= cursor:  # safety net against an infinite loop
            break
        cursor = last_open + step_ms
        if len(batch) < 1000:
            break

    if not all_klines:
        raise ValueError("No kline data returned for the requested range.")

    return {
        "open":       [float(k[1]) for k in all_klines],
        "high":       [float(k[2]) for k in all_klines],
        "low":        [float(k[3]) for k in all_klines],
        "close":      [float(k[4]) for k in all_klines],
        "volume":     [float(k[5]) for k in all_klines],
        "open_time":  [int(k[0]) for k in all_klines],
        "close_time": [int(k[6]) for k in all_klines],
    }
