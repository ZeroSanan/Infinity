"""
Mixed DCA Strategy — Adaptive Bull/Bear Regime Switching

Runs a long (buy-the-dip) DCA strategy during BULLISH regimes and a short
(sell-the-rally) DCA strategy during BEARISH regimes, switching adaptively
based on price-only indicators — no external API calls, safe for historical
simulation.

Regime score per candle (max ±5, or ±4 without volume):
  price vs EMA-50        +1 / -1
  price vs EMA-200       +1 / -1
  EMA-50 vs EMA-200      +1 golden / -1 death cross
  higher-high 2w window  +1 / -1
  volume 7d vs prior 7d  +1 / 0 / -1  (skipped if volume column absent)

Smoothing: regime switches only after 5 consecutive candles of the same
non-neutral score.  NEUTRAL clears the pending buffer (no switch).
A mid-trade regime switch force-closes the open position at the current
candle price before starting the new strategy direction.
"""

import sys
from collections import deque
from pathlib import Path
from typing import List, Optional
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dca_strategy import DCALevel


# ── Indicator helpers ────────────────────────────────────────────────────────

def _ema(prices: list, period: int) -> list:
    """Exponential moving average; None during warmup."""
    k      = 2.0 / (period + 1)
    result = [None] * len(prices)
    if len(prices) < period:
        return result
    result[period - 1] = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def _hh_series(highs: list, lookback: int = 336) -> list:
    """
    Pre-compute higher-high signal for every index.
    True  = second half of the lookback window has a higher max than the first.
    None  = insufficient history (< 10 candles in window).
    """
    n      = len(highs)
    result = [None] * n
    for i in range(9, n):
        window = highs[max(0, i - lookback + 1): i + 1]
        mid    = len(window) // 2
        if mid >= 1:
            result[i] = max(window[mid:]) > max(window[:mid])
    return result


# ── CSV loader ───────────────────────────────────────────────────────────────

def load_mixed_data(csv_file: str) -> pd.DataFrame:
    """
    Load CSV preserving close + volume when available.
    Uses the same timestamp-column detection as load_and_prepare_data.
    """
    df = pd.read_csv(csv_file)
    df.columns = df.columns.str.strip().str.lower()

    _ts_candidates = [
        "open time", "open_time", "timestamp", "time",
        "date", "datetime", "close time", "close_time",
    ]
    ts_col = next((c for c in _ts_candidates if c in df.columns), None)
    if ts_col is None:
        for c in df.columns:
            try:
                pd.to_datetime(df[c].iloc[:5])
                ts_col = c
                break
            except Exception:
                pass
    if ts_col is None:
        raise ValueError(f"No timestamp column found. Columns: {list(df.columns)}")

    df["datetime"] = pd.to_datetime(df[ts_col])
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
    df = df.dropna(subset=["datetime"])

    needed = ["datetime", "high", "low"]
    for col in ["close", "volume"]:
        if col in df.columns:
            needed.append(col)
    for col in ["high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low"])
    return df[needed].copy()


# ── Engine ───────────────────────────────────────────────────────────────────

class MixedDCABacktest:
    """
    Runs both a long DCA strategy (BULLISH regime) and a short DCA strategy
    (BEARISH regime) on the same dataset, switching adaptively.

    Usage:
        engine = MixedDCABacktest()
        result = engine.run(df, bull_config, bear_config, initial_budget=10000)
    """

    PRESETS = {
        "conservative": {
            "name": "Conservative Mixed",
            "bull": {
                "dump_levels": [-6, -10, -15],
                "order_sizes": [1500, 2000, 2750],
                "take_profit_percent": 8,
                "stop_loss_percent": 0,
            },
            "bear": {
                "pump_levels": [6, 10, 15],
                "order_sizes": [1500, 2000, 2750],
                "take_profit_percent": 8,
                "stop_loss_percent": 0,
            },
        },
        "aggressive": {
            "name": "Aggressive Mixed",
            "bull": {
                "dump_levels": [-8, -13, -20, -28],
                "order_sizes": [1500, 2000, 2750, 5500],
                "take_profit_percent": 12,
                "stop_loss_percent": 0,
            },
            "bear": {
                "pump_levels": [8, 13, 20, 28],
                "order_sizes": [1500, 2000, 2750, 5500],
                "take_profit_percent": 12,
                "stop_loss_percent": 0,
            },
        },
        "asymmetric": {
            "name": "Asymmetric",
            "bull": {
                "dump_levels": [-6, -9, -13, -18],
                "order_sizes": [1500, 2000, 2750, 5500],
                "take_profit_percent": 10,
                "stop_loss_percent": 0,
            },
            "bear": {
                "pump_levels": [5, 8, 12],
                "order_sizes": [1500, 2000, 2750],
                "take_profit_percent": 6,
                "stop_loss_percent": 0,
            },
        },
    }

    def run(
        self,
        df: pd.DataFrame,
        bull_config: dict,
        bear_config: dict,
        initial_budget: float = 10_000.0,
    ) -> dict:
        """
        Run mixed strategy on df.

        df columns required: datetime, high, low
        df columns optional: close (improves EMA accuracy), volume (adds volume signal)

        Returns a flat result dict compatible with the /api/backtest/mixed endpoint.
        """
        df = df.reset_index(drop=True)
        n  = len(df)
        warnings_out: List[str] = []

        has_close  = "close"  in df.columns
        has_volume = "volume" in df.columns
        if not has_close:
            warnings_out.append(
                "No 'close' column — using (high+low)/2 midpoint for EMA calculations."
            )
        if not has_volume:
            warnings_out.append(
                "No 'volume' column — volume-trend signal disabled (±1 point omitted)."
            )

        prices = (
            df["close"].tolist() if has_close
            else [(h + l) / 2 for h, l in zip(df["high"], df["low"])]
        )
        highs = df["high"].tolist()
        lows  = df["low"].tolist()
        vols  = df["volume"].tolist() if has_volume else []
        dates = df["datetime"].tolist()

        # Pre-compute indicator series
        ema50_s  = _ema(prices, 50)
        ema200_s = _ema(prices, 200)
        hh_s     = _hh_series(highs, lookback=336)

        if ema200_s[-1] is None:
            warnings_out.append(
                f"Only {n} candles — EMA-200 warmup requires 200. "
                "Regime quality is reduced for the first portion of the dataset."
            )

        # Parse bull config
        bull_lvls   = [float(x) for x in bull_config.get("dump_levels", [-6, -9, -13, -18])]
        bull_allocs = [float(x) for x in bull_config.get("order_sizes",  [initial_budget / 4] * 4)]
        bull_tp     = float(bull_config.get("take_profit_percent", 10))
        bull_sl     = float(bull_config.get("stop_loss_percent") or 0)

        # Parse bear config
        bear_lvls   = [float(x) for x in bear_config.get("pump_levels", [6, 9, 13, 18])]
        bear_allocs = [float(x) for x in bear_config.get("order_sizes", [initial_budget / 4] * 4)]
        bear_tp     = float(bear_config.get("take_profit_percent", 10))
        bear_sl     = float(bear_config.get("stop_loss_percent") or 0)

        # ── Position state ────────────────────────────────────────────────────
        in_trade:  bool            = False
        ttype:     Optional[str]   = None     # "LONG" | "SHORT"
        anchor:    Optional[float] = None
        t_start                    = None
        act_lvls:  List[DCALevel]  = []

        budget       = initial_budget
        trades_out   = []
        equity_curve = [{"date": str(dates[0])[:10], "equity": round(budget, 2)}]
        regime_tl    = []
        regime_sw    = 0

        reg_buf        = deque(maxlen=5)
        confirmed      = "NEUTRAL"
        last_switch_at = -10

        # ── Position helpers ──────────────────────────────────────────────────

        def _build_long(anch: float) -> List[DCALevel]:
            return [
                DCALevel(i + 1, bull_lvls[i], anch * (1 + bull_lvls[i] / 100), bull_allocs[i])
                for i in range(len(bull_lvls))
            ]

        def _build_short(anch: float) -> List[DCALevel]:
            return [
                DCALevel(i + 1, bear_lvls[i], anch * (1 + bear_lvls[i] / 100), bear_allocs[i])
                for i in range(len(bear_lvls))
            ]

        def _fill_long(lvls, lo):
            for lv in lvls:
                if not lv.filled and lo <= lv.entry_price:
                    lv.filled = True; lv.fill_price = lv.entry_price

        def _fill_short(lvls, hi):
            for lv in lvls:
                if not lv.filled and hi >= lv.entry_price:
                    lv.filled = True; lv.fill_price = lv.entry_price

        def _filled(lvls):
            return [lv for lv in lvls if lv.filled]

        def _avg_px(lvls):
            f = _filled(lvls)
            if not f:
                return None
            A = sum(lv.budget_allocation for lv in f)
            Q = sum(lv.budget_allocation / lv.fill_price for lv in f)
            return A / Q if Q else None

        def _tp_long(lvls):
            a = _avg_px(lvls); return (a * (1 + bull_tp / 100)) if a else None

        def _tp_short(lvls):
            a = _avg_px(lvls); return (a * (1 - bear_tp / 100)) if a else None

        def _sl_long(anch):
            return (anch * (1 - bull_sl / 100)) if bull_sl > 0 else None

        def _sl_short(anch):
            return (anch * (1 + bear_sl / 100)) if bear_sl > 0 else None

        def _close_trade(ci: int, px: float, reason: str):
            nonlocal budget, in_trade, ttype, anchor, t_start, act_lvls
            f = _filled(act_lvls)
            if not f:
                in_trade = False; ttype = None; act_lvls = []; return
            A   = sum(lv.budget_allocation for lv in f)
            Q   = sum(lv.budget_allocation / lv.fill_price for lv in f)
            pnl = (Q * px - A) if ttype == "LONG" else (A - Q * px)
            pct = pnl / A * 100 if A else 0
            dur = (dates[ci] - t_start).total_seconds() / 86400
            trades_out.append({
                "num":            len(trades_out) + 1,
                "type":           ttype,
                "regime":         confirmed,
                "entry_date":     str(t_start)[:10],
                "exit_date":      str(dates[ci])[:10],
                "start":          str(t_start),
                "end":            str(dates[ci]),
                "duration_days":  round(dur, 2),
                "levels_filled":  len(f),
                "invested":       round(A, 2),
                "avg_entry":      round(A / Q, 2) if Q else 0,
                "exit_price":     round(px, 2),
                "profit":         round(pnl, 2),
                "profit_pct":     round(pct, 2),
                # Fields kept compatible with the single-mode chart/table renderers:
                "profit_loss":    round(pnl, 2),
                "profit_percent": round(pct, 2),
                "anchor_price":   round(anchor, 2),
                "stop_loss":      reason == "stop_loss",
                "reason":         reason,
            })
            budget += pnl
            equity_curve.append({"date": str(dates[ci])[:10], "equity": round(budget, 2)})
            in_trade = False; ttype = None; act_lvls = []

        # ── Main candle loop ──────────────────────────────────────────────────

        for i in range(n):
            hi    = highs[i]
            lo    = lows[i]
            price = prices[i]

            # 1. Compute regime score
            score = 0
            e50   = ema50_s[i]
            e200  = ema200_s[i]

            if e50  is not None: score += 1 if price > e50  else -1
            if e200 is not None: score += 1 if price > e200 else -1
            if e50  is not None and e200 is not None:
                score += 1 if e50 > e200 else -1

            hh = hh_s[i]
            if hh is not None:
                score += 1 if hh else -1

            if has_volume and i >= 13:
                v7  = sum(vols[i - 6 : i + 1]) / 7
                v7p = sum(vols[i - 13: i - 6]) / 7
                if v7p > 0:
                    if   v7 > v7p * 1.1: score += 1
                    elif v7 < v7p * 0.9: score -= 1

            raw = ("BULLISH" if score >= 2 else
                   "BEARISH" if score <= -2 else "NEUTRAL")

            # 2. Regime smoothing (5 consecutive candles, NEUTRAL resets buffer)
            if raw == "NEUTRAL":
                reg_buf.clear()
            else:
                reg_buf.append(raw)

            if (len(reg_buf) == 5
                    and len(set(reg_buf)) == 1
                    and list(reg_buf)[0] != confirmed
                    and (i - last_switch_at) >= 5):
                new_r          = list(reg_buf)[0]
                regime_sw     += 1
                last_switch_at = i
                reg_buf.clear()

                # Force-close any open trade before switching
                if in_trade:
                    _close_trade(i, price, "regime_switch")
                # Reset anchor for new regime direction
                anchor    = hi if new_r == "BULLISH" else lo
                confirmed = new_r

            regime_tl.append({"date": str(dates[i])[:10], "regime": confirmed})

            # 3. Trade logic
            if not in_trade:
                if anchor is None:
                    if confirmed != "NEUTRAL":
                        anchor = hi if confirmed == "BULLISH" else lo
                    continue

                if confirmed == "BULLISH":
                    dump_pct = (lo - anchor) / anchor * 100
                    if dump_pct <= bull_lvls[0]:
                        in_trade = True; ttype = "LONG"
                        t_start  = dates[i]
                        act_lvls = _build_long(anchor)
                        _fill_long(act_lvls, lo)
                    else:
                        anchor = max(anchor, hi)

                elif confirmed == "BEARISH":
                    pump_pct = (hi - anchor) / anchor * 100
                    if pump_pct >= bear_lvls[0]:
                        in_trade = True; ttype = "SHORT"
                        t_start  = dates[i]
                        act_lvls = _build_short(anchor)
                        _fill_short(act_lvls, hi)
                    else:
                        anchor = min(anchor, lo)
                # NEUTRAL: wait for regime to confirm before trading

            else:  # In trade
                if ttype == "LONG":
                    _fill_long(act_lvls, lo)
                    sl = _sl_long(anchor)
                    if sl and lo <= sl:
                        _close_trade(i, sl, "stop_loss"); anchor = hi; continue
                    tp = _tp_long(act_lvls)
                    if tp and hi >= tp:
                        _close_trade(i, tp, "take_profit"); anchor = hi

                else:  # SHORT
                    _fill_short(act_lvls, hi)
                    sl = _sl_short(anchor)
                    if sl and hi >= sl:
                        _close_trade(i, sl, "stop_loss"); anchor = lo; continue
                    tp = _tp_short(act_lvls)
                    if tp and lo <= tp:
                        _close_trade(i, tp, "take_profit"); anchor = lo

        # ── Aggregate results ─────────────────────────────────────────────────
        total   = len(trades_out)
        winners = [t for t in trades_out if t["profit"] > 0]
        losers  = [t for t in trades_out if t["profit"] <= 0]
        bull_t  = [t for t in trades_out if t["type"] == "LONG"]
        bear_t  = [t for t in trades_out if t["type"] == "SHORT"]
        net_pnl = sum(t["profit"] for t in trades_out)
        durs    = [t["duration_days"] for t in trades_out]

        test_days = (
            round((dates[-1] - dates[0]).total_seconds() / 86400, 1)
            if len(dates) >= 2 else 0.0
        )

        return {
            "ok":                      True,
            "initial_budget":          initial_budget,
            "final_budget":            round(budget, 2),
            "total_roi":               round((budget - initial_budget) / initial_budget * 100, 2),
            "net_pnl":                 round(net_pnl, 2),
            "total_profit":            round(sum(t["profit"] for t in winners), 2),
            "total_loss":              round(abs(sum(t["profit"] for t in losers)), 2),
            "win_rate":                round(len(winners) / total * 100, 1) if total else 0.0,
            "total_trades":            total,
            "winning_trades":          len(winners),
            "losing_trades":           len(losers),
            "stopped_out_trades":      sum(1 for t in trades_out if t["stop_loss"]),
            "stop_loss_rate":          round(sum(1 for t in trades_out if t["stop_loss"]) / total * 100, 1) if total else 0.0,
            "bull_trades":             len(bull_t),
            "bear_trades":             len(bear_t),
            "regime_switches":         regime_sw,
            "avg_trade_duration_days": round(sum(durs) / len(durs), 2) if durs else 0.0,
            "avg_trade_pnl":           round(net_pnl / total, 2) if total else 0.0,
            "largest_profit":          round(max((t["profit"] for t in trades_out), default=0), 2),
            "largest_loss":            round(min((t["profit"] for t in trades_out), default=0), 2),
            "total_test_days":         test_days,
            "equity_curve":            equity_curve,
            "regime_timeline":         regime_tl,
            "trades":                  trades_out,
            "warnings":                warnings_out,
        }
