"""
Mixed DCA Strategy — Adaptive Bull/Bear Regime Switching with Extremity Overrides

Two-layer decision system:

Layer 1 — Regime (smoothed, 5-candle confirmation):
  BULLISH (score >= 2)  →  default active_mode = BUY
  BEARISH (score <= -2) →  default active_mode = SELL
  NEUTRAL               →  hold previous active_mode, no new trades

Layer 2 — Extremity overrides (single-candle activation):
  BULLISH + (RSI > overbought OR price > BB-upper)
    → active_mode flips to SELL
    → holds until raw score drops BELOW 2, then re-enters >= 2
    → then active_mode resets to BUY

  BEARISH + (RSI < oversold OR price < BB-lower)
    → active_mode flips to BUY
    → holds until raw score rises ABOVE -2, then re-enters <= -2
    → then active_mode resets to SELL

active_mode (not regime) is what actually gates trade entries.

Regime score signals (max ±5, or ±4 without volume):
  price vs EMA-50        +1 / -1
  price vs EMA-200       +1 / -1
  EMA-50 vs EMA-200      +1 golden / -1 death cross
  higher-high 2-week     +1 / -1
  volume 7d vs prior 7d  +1 / 0 / -1  (skipped if volume column absent)

All indicators implemented manually — no external TA libraries.
"""

import sys
from collections import deque
from pathlib import Path
from typing import List, Optional, Tuple
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dca_strategy import DCALevel


# ── Indicator helpers (all O(n)) ─────────────────────────────────────────────

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
    O(n) higher-high via sliding window maximum.
    True = second half of lookback has higher max than first half; None = warmup.
    """
    n    = len(highs)
    half = max(1, lookback // 2)
    result  = [None] * n
    roll_mx = [None] * n

    dq = deque()
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
        v7  = (prefix[i + 1] - prefix[i - 6])  / 7
        v7p = (prefix[i - 6] - prefix[i - 13]) / 7
        if v7p > 0:
            r = v7 / v7p
            if   r > 1.1: result[i] =  1
            elif r < 0.9: result[i] = -1
    return result


def _rsi_series(closes: list, period: int = 14) -> list:
    """
    Wilder RSI series. None for the first `period` candles.
    Uses the exact Wilder smoothing (EMA with alpha = 1/period).
    """
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result

    gains  = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    result[period] = 100.0 - 100.0 / (1 + rs)

    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        result[i + 1] = 100.0 - 100.0 / (1 + rs)

    return result


def _bb_series(
    prices: list, period: int = 20, num_std: float = 2.0
) -> Tuple[list, list]:
    """
    Bollinger Bands (upper, lower) in O(n) via prefix sums.
    Returns (bb_upper, bb_lower); both lists have None during warmup.
    """
    n       = len(prices)
    bb_u    = [None] * n
    bb_l    = [None] * n
    if n < period:
        return bb_u, bb_l

    psum  = [0.0] * (n + 1)
    psum2 = [0.0] * (n + 1)
    for i, p in enumerate(prices):
        psum[i + 1]  = psum[i]  + p
        psum2[i + 1] = psum2[i] + p * p

    for i in range(period - 1, n):
        s  = psum[i + 1]  - psum[i - period + 1]
        s2 = psum2[i + 1] - psum2[i - period + 1]
        mean = s / period
        var  = max(0.0, s2 / period - mean * mean)
        std  = var ** 0.5
        bb_u[i] = mean + num_std * std
        bb_l[i] = mean - num_std * std

    return bb_u, bb_l


# ── CSV loader ───────────────────────────────────────────────────────────────

def load_mixed_data(csv_file: str) -> pd.DataFrame:
    """Load CSV, preserving close + volume when available."""
    df = pd.read_csv(csv_file)
    df.columns = df.columns.str.strip().str.lower()

    _ts = [
        "open time", "open_time", "timestamp", "time",
        "date", "datetime", "close time", "close_time",
    ]
    ts_col = next((c for c in _ts if c in df.columns), None)
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
    Adaptive bull/bear DCA with extremity overrides.

    Usage:
        engine = MixedDCABacktest()
        result = engine.run(df, bull_config, bear_config,
                            initial_budget=10000,
                            rsi_overbought=70, rsi_oversold=30)
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
        df:             pd.DataFrame,
        bull_config:    dict,
        bear_config:    dict,
        initial_budget: float = 10_000.0,
        rsi_overbought: float = 70.0,
        rsi_oversold:   float = 30.0,
    ) -> dict:
        """
        Run mixed strategy on df (datetime, high, low, [close], [volume]).
        Returns flat result dict for /api/backtest/mixed.
        """
        df = df.reset_index(drop=True)
        n  = len(df)
        warnings_out: List[str] = []

        has_close  = "close"  in df.columns
        has_volume = "volume" in df.columns
        if not has_close:
            warnings_out.append(
                "No 'close' column — using (high+low)/2 for EMA/RSI/BB calculations."
            )
        if not has_volume:
            warnings_out.append(
                "No 'volume' column — volume-trend signal disabled."
            )

        prices = (
            df["close"].tolist() if has_close
            else [(h + l) / 2 for h, l in zip(df["high"], df["low"])]
        )
        highs = df["high"].tolist()
        lows  = df["low"].tolist()
        vols  = df["volume"].tolist() if has_volume else []
        dates = df["datetime"].tolist()

        # Pre-compute all indicator series
        ema50_s  = _ema(prices, 50)
        ema200_s = _ema(prices, 200)
        hh_s     = _hh_series(highs, lookback=336)
        vol_sig  = _vol_signal_series(vols) if has_volume else [0] * n
        rsi_s    = _rsi_series(prices, 14)
        bb_u_s, bb_l_s = _bb_series(prices, 20, 2.0)

        if ema200_s[-1] is None:
            warnings_out.append(
                f"Only {n} candles — EMA-200 warmup requires 200; "
                "regime quality reduced for early candles."
            )
        if n < 20:
            warnings_out.append(
                "Fewer than 20 candles — RSI/BB warmup not complete; "
                "overrides disabled until warmup."
            )

        # ── Strategy config ───────────────────────────────────────────────────
        bull_lvls   = [float(x) for x in bull_config.get("dump_levels", [-6, -9, -13, -18])]
        bull_allocs = [float(x) for x in bull_config.get("order_sizes",  [initial_budget / 4] * 4)]
        bull_tp     = float(bull_config.get("take_profit_percent", 10))
        bull_sl     = float(bull_config.get("stop_loss_percent") or 0)

        bear_lvls   = [float(x) for x in bear_config.get("pump_levels",  [6, 9, 13, 18])]
        bear_allocs = [float(x) for x in bear_config.get("order_sizes",  [initial_budget / 4] * 4)]
        bear_tp     = float(bear_config.get("take_profit_percent", 10))
        bear_sl     = float(bear_config.get("stop_loss_percent") or 0)

        # ── Position state ────────────────────────────────────────────────────
        in_trade:  bool            = False
        ttype:     Optional[str]   = None      # "LONG" | "SHORT"
        anchor:    Optional[float] = None
        t_start                    = None
        act_lvls:  List[DCALevel]  = []
        current_trade_from_override: bool = False

        # ── Regime + override state ───────────────────────────────────────────
        reg_buf        = deque(maxlen=5)
        confirmed      = "NEUTRAL"           # smoothed regime
        active_mode    = "WAIT"              # execution layer
        last_switch_at = -10

        override_active   = False
        override_type     = None             # "OVERBOUGHT" | "OVERSOLD"
        override_saw_exit = False            # raw score exited base-regime zone

        # ── Output accumulators ───────────────────────────────────────────────
        budget         = initial_budget
        trades_out     = []
        equity_curve   = [{"date": str(dates[0])[:10], "equity": round(budget, 2)}]
        regime_tl      = []
        override_events = []
        regime_sw       = 0
        score           = 0                  # kept in scope for closures

        # ── Inner helpers ─────────────────────────────────────────────────────

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
            nonlocal current_trade_from_override
            f = _filled(act_lvls)
            if not f:
                in_trade = False; ttype = None; act_lvls = []
                current_trade_from_override = False; return
            A   = sum(lv.budget_allocation for lv in f)
            Q   = sum(lv.budget_allocation / lv.fill_price for lv in f)
            pnl = (Q * px - A) if ttype == "LONG" else (A - Q * px)
            pct = pnl / A * 100 if A else 0
            dur = (dates[ci] - t_start).total_seconds() / 86400
            trades_out.append({
                "num":            len(trades_out) + 1,
                "type":           ttype,
                "regime":         confirmed,
                "active_mode":    active_mode,
                "from_override":  current_trade_from_override,
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
                "profit_loss":    round(pnl, 2),
                "profit_percent": round(pct, 2),
                "anchor_price":   round(anchor, 2),
                "stop_loss":      reason == "stop_loss",
                "reason":         reason,
            })
            budget += pnl
            equity_curve.append({"date": str(dates[ci])[:10], "equity": round(budget, 2)})
            in_trade = False; ttype = None; act_lvls = []
            current_trade_from_override = False

        def _bb_pos(ci):
            bbu = bb_u_s[ci]; bbl = bb_l_s[ci]
            if bbu is None or bbl is None:
                return "INSIDE"
            if prices[ci] > bbu: return "ABOVE_UPPER"
            if prices[ci] < bbl: return "BELOW_LOWER"
            return "INSIDE"

        def _log_override(ci, ov_type, am_before, am_after):
            rsi_v = rsi_s[ci]
            override_events.append({
                "date":               str(dates[ci])[:10],
                "type":               ov_type,
                "regime":             confirmed,
                "active_mode_before": am_before,
                "active_mode_after":  am_after,
                "rsi":                round(rsi_v, 2) if rsi_v is not None else None,
                "bb_position":        _bb_pos(ci),
                "regime_score":       score,
            })

        # ── Main candle loop ──────────────────────────────────────────────────

        for i in range(n):
            hi    = highs[i]
            lo    = lows[i]
            price = prices[i]

            # ── 1. Regime score ───────────────────────────────────────────────
            score = 0
            e50  = ema50_s[i]
            e200 = ema200_s[i]

            if e50  is not None: score += 1 if price > e50  else -1
            if e200 is not None: score += 1 if price > e200 else -1
            if e50  is not None and e200 is not None:
                score += 1 if e50 > e200 else -1

            hh = hh_s[i]
            if hh is not None:
                score += 1 if hh else -1

            score += vol_sig[i]

            raw = ("BULLISH" if score >= 2 else
                   "BEARISH" if score <= -2 else "NEUTRAL")

            # ── 2. Regime smoothing ───────────────────────────────────────────
            regime_just_switched = False
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
                regime_just_switched = True

                # Cancel any active override — regime context changed
                if override_active:
                    override_active   = False
                    override_type     = None
                    override_saw_exit = False

                # Force-close any open trade before switching
                if in_trade:
                    _close_trade(i, price, "regime_switch")

                confirmed  = new_r
                anchor     = hi if new_r == "BULLISH" else lo
                active_mode = "BUY" if new_r == "BULLISH" else "SELL"

            regime_tl.append({"date": str(dates[i])[:10], "regime": confirmed})

            # ── 3. Override logic (skip on regime-switch candle) ──────────────
            rsi_val = rsi_s[i]
            indicators_ready = (rsi_val is not None and bb_u_s[i] is not None)

            if not regime_just_switched and indicators_ready:
                if override_active:
                    # Check release condition
                    if override_type == "OVERBOUGHT":
                        if score < 2:
                            override_saw_exit = True
                        elif override_saw_exit:
                            # score re-entered >= 2 after being below 2 → release
                            am_before = active_mode
                            override_active = False; override_type = None
                            override_saw_exit = False
                            if in_trade:
                                _close_trade(i, price, "override_release")
                            active_mode = "BUY"
                            anchor = hi
                            _log_override(i, "OVERRIDE_RELEASED", am_before, "BUY")

                    else:  # OVERSOLD override
                        if score > -2:
                            override_saw_exit = True
                        elif override_saw_exit:
                            am_before = active_mode
                            override_active = False; override_type = None
                            override_saw_exit = False
                            if in_trade:
                                _close_trade(i, price, "override_release")
                            active_mode = "SELL"
                            anchor = lo
                            _log_override(i, "OVERRIDE_RELEASED", am_before, "SELL")

                elif confirmed in ("BULLISH", "BEARISH"):
                    # Check activation
                    bb_pos = _bb_pos(i)
                    if confirmed == "BULLISH":
                        if rsi_val > rsi_overbought or bb_pos == "ABOVE_UPPER":
                            am_before = active_mode
                            if in_trade:
                                _close_trade(i, price, "overbought_override")
                            override_active   = True
                            override_type     = "OVERBOUGHT"
                            override_saw_exit = False
                            active_mode       = "SELL"
                            anchor            = hi
                            _log_override(i, "OVERBOUGHT_OVERRIDE", am_before, "SELL")

                    else:  # BEARISH
                        if rsi_val < rsi_oversold or bb_pos == "BELOW_LOWER":
                            am_before = active_mode
                            if in_trade:
                                _close_trade(i, price, "oversold_override")
                            override_active   = True
                            override_type     = "OVERSOLD"
                            override_saw_exit = False
                            active_mode       = "BUY"
                            anchor            = lo
                            _log_override(i, "OVERSOLD_OVERRIDE", am_before, "BUY")

            # ── 4. Trade logic ────────────────────────────────────────────────
            if not in_trade:
                # No new entries during NEUTRAL or WAIT
                if confirmed == "NEUTRAL" or active_mode == "WAIT":
                    if anchor is None and confirmed != "NEUTRAL":
                        anchor = hi if active_mode == "BUY" else lo
                    continue

                if anchor is None:
                    anchor = hi if active_mode == "BUY" else lo
                    continue

                if active_mode == "BUY":
                    dump_pct = (lo - anchor) / anchor * 100
                    if dump_pct <= bull_lvls[0]:
                        in_trade = True; ttype = "LONG"
                        t_start  = dates[i]
                        current_trade_from_override = override_active
                        act_lvls = _build_long(anchor)
                        _fill_long(act_lvls, lo)
                    else:
                        anchor = max(anchor, hi)

                elif active_mode == "SELL":
                    pump_pct = (hi - anchor) / anchor * 100
                    if pump_pct >= bear_lvls[0]:
                        in_trade = True; ttype = "SHORT"
                        t_start  = dates[i]
                        current_trade_from_override = override_active
                        act_lvls = _build_short(anchor)
                        _fill_short(act_lvls, hi)
                    else:
                        anchor = min(anchor, lo)

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

        # Override stats
        ov_trades   = [t for t in trades_out if t["from_override"]]
        ov_winners  = [t for t in ov_trades if t["profit"] > 0]
        ov_pnl      = sum(t["profit"] for t in ov_trades)
        # Only count activation events (not releases)
        activations = [e for e in override_events
                       if e["type"] in ("OVERBOUGHT_OVERRIDE", "OVERSOLD_OVERRIDE")]

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
            # Override fields
            "override_events":         override_events,
            "total_overrides":         len(activations),
            "override_win_rate":       round(len(ov_winners) / len(ov_trades) * 100, 1) if ov_trades else 0.0,
            "override_pnl":            round(ov_pnl, 2),
        }
