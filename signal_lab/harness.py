"""
signal_lab.harness — signal-only backtest harness for signal_lab.signal_fn.

Walks a historical OHLCV series candle-by-candle, calling
``signal_fn.get_verdict()`` on the point-in-time window ``candles[0:i+1]`` at
each step (no look-ahead). Whenever the verdict is ``LONG_NOW`` /
``SHORT_NOW`` and no trade is currently open, it opens ONE fixed-size trade —
no DCA ladder, no averaging down, single position at a time — and simulates
its exit via take-profit / stop-loss / time horizon.

The goal is to measure whether the signal has real directional edge, by
comparing the strategy's results against three baselines:

  - random direction  : same entry timing, direction coin-flipped (averaged
                         over many seeds) — isolates whether ENTRY TIMING
                         alone has value.
  - inverted          : same entries, direction flipped — tests whether the
                         signal's directional CALL has *negative* edge.
  - buy & hold        : a single long position held over the whole tested
                         span, as a passive benchmark.

This module is part of ``signal_lab/`` (an isolated research area). It only
imports ``signal_lab.signal_fn`` (a standalone copy of the live regime/entry
logic, with no network or engine side effects) and, optionally,
``core.ml_signals.predict_direction`` (read-only, for ``--ml-filter``).
Nothing in ``core/``, ``models/``, ``config/``, or ``web/`` is imported for
its side effects or modified by this module.

Usage:
    python3 signal_lab/harness.py [path/to/candles.csv] [options]

If no CSV is given, the first *.csv in algo-trading/data/ is used; if that
directory doesn't exist either, a deterministic synthetic random-walk series
is generated so the harness can still run end-to-end.

Examples:
    python3 signal_lab/harness.py
    python3 signal_lab/harness.py algo-trading/data/BTCUSDT_4h.csv --max-candles 4000
    python3 signal_lab/harness.py --tp 4 --sl 2 --horizon 24 --exit-mode tpsl
    python3 signal_lab/harness.py --exit-mode horizon --horizon 6 --ml-filter
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from signal_fn import get_verdict  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "algo-trading", "data")

# for the optional, lazily-imported `core.ml_signals.predict_direction`
sys.path.insert(0, ROOT)

# get_verdict() reports warmup_ok=False below this many candles (EMA-200).
# We start the walk here so every verdict we act on is post-warmup.
WARMUP_CANDLES = 200

DEFAULT_SYNTHETIC_CANDLES = 1500


# ── Candle loading (mirrors test_signal_fn.py) ──────────────────────────────

def _find_csv(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    matches = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    return matches[0] if matches else None


def _load_candles_from_csv(path: str, max_candles: Optional[int]) -> Dict[str, list]:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lower()

    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing required column(s) {missing}; found {list(df.columns)}")

    if max_candles:
        df = df.tail(max_candles)
    df = df.reset_index(drop=True)

    candles = {
        "open": df["open"].astype(float).tolist(),
        "high": df["high"].astype(float).tolist(),
        "low": df["low"].astype(float).tolist(),
        "close": df["close"].astype(float).tolist(),
    }
    if "volume" in df.columns:
        candles["volume"] = df["volume"].astype(float).tolist()
    return candles


def _synthetic_candles(n: int, seed: int = 42) -> Dict[str, list]:
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


# ── Trade simulation ─────────────────────────────────────────────────────

def simulate_trade(
    candles: Dict[str, list],
    entry_index: int,
    direction: str,        # "LONG" | "SHORT"
    tp_pct: float,
    sl_pct: float,
    horizon: int,
    exit_mode: str,        # "tpsl" | "horizon"
    fee_pct: float,
) -> dict:
    """
    Simulate ONE fixed-size trade entered at the close of `entry_index`,
    held for at most `horizon` candles.

    exit_mode == "tpsl":    exit on whichever of TP / SL is touched first
                            (checked against each subsequent candle's
                            high/low), or at the close of the horizon-th
                            candle if neither is hit ("TIMEOUT"). If both TP
                            and SL fall within the same candle's range, SL is
                            assumed to have been hit first (conservative).
    exit_mode == "horizon": ignore TP/SL; always exit at the close of the
                            horizon-th candle ("HORIZON").

    If fewer than `horizon` candles remain after entry and neither TP nor SL
    is touched, the trade exits at the close of the last available candle
    and is flagged `incomplete=True` (exit_reason="END_OF_DATA") so it can be
    excluded from headline stats.
    """
    closes = candles["close"]
    highs = candles["high"]
    lows = candles["low"]
    n = len(closes)

    entry_price = closes[entry_index]
    if direction == "LONG":
        tp_price = entry_price * (1 + tp_pct / 100)
        sl_price = entry_price * (1 - sl_pct / 100)
    else:
        tp_price = entry_price * (1 - tp_pct / 100)
        sl_price = entry_price * (1 + sl_pct / 100)

    last_avail = min(entry_index + horizon, n - 1)
    incomplete = (last_avail - entry_index) < horizon

    exit_index = last_avail
    exit_price = closes[last_avail]
    exit_reason = "END_OF_DATA" if incomplete else (
        "HORIZON" if exit_mode == "horizon" else "TIMEOUT"
    )

    mfe_pct = 0.0  # max favorable excursion seen during the hold
    mae_pct = 0.0  # max adverse excursion seen during the hold

    for j in range(entry_index + 1, last_avail + 1):
        if direction == "LONG":
            fav = (highs[j] - entry_price) / entry_price * 100
            adv = (entry_price - lows[j]) / entry_price * 100
        else:
            fav = (entry_price - lows[j]) / entry_price * 100
            adv = (highs[j] - entry_price) / entry_price * 100
        mfe_pct = max(mfe_pct, fav)
        mae_pct = max(mae_pct, adv)

        if exit_mode == "tpsl":
            sl_hit = (lows[j] <= sl_price) if direction == "LONG" else (highs[j] >= sl_price)
            tp_hit = (highs[j] >= tp_price) if direction == "LONG" else (lows[j] <= tp_price)
            if sl_hit:
                exit_index, exit_price, exit_reason, incomplete = j, sl_price, "SL", False
                break
            if tp_hit:
                exit_index, exit_price, exit_reason, incomplete = j, tp_price, "TP", False
                break

    if direction == "LONG":
        gross_pct = (exit_price - entry_price) / entry_price * 100
    else:
        gross_pct = (entry_price - exit_price) / entry_price * 100
    net_pct = gross_pct - 2 * fee_pct

    return {
        "entry_index": entry_index,
        "exit_index": exit_index,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "bars_held": exit_index - entry_index,
        "gross_pct": gross_pct,
        "net_pct": net_pct,
        "exit_reason": exit_reason,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "incomplete": incomplete,
    }


# ── Point-in-time signal walk ────────────────────────────────────────────

def _ml_direction(candles: Dict[str, list], i: int) -> dict:
    """Read-only ML overlay: core.ml_signals.predict_direction() at candle i."""
    from core.ml_signals import predict_direction
    return predict_direction(
        candles["close"][: i + 1],
        candles["high"][: i + 1],
        candles["low"][: i + 1],
    )


def run_walk(candles: Dict[str, list], args: argparse.Namespace) -> dict:
    """
    Walk candles[WARMUP_CANDLES-1 .. n-2], calling get_verdict() on the
    point-in-time slice candles[0:i+1] at each step. Opens one fixed-size
    trade per LONG_NOW/SHORT_NOW signal when flat; signals seen while a
    trade is already open are NOT taken (single-position invariant — no
    averaging down) but are counted as diagnostics.
    """
    closes = candles["close"]
    n = len(closes)

    start = WARMUP_CANDLES - 1
    if start >= n - 1:
        raise ValueError(f"need at least {WARMUP_CANDLES + 1} candles, got {n}")

    trades: List[dict] = []
    verdict_counts = {"LONG_NOW": 0, "SHORT_NOW": 0, "WATCH": 0, "WAIT": 0}
    overlap_same = 0
    overlap_opp = 0

    open_trade: Optional[dict] = None
    open_until = -1

    t0 = time.time()
    total = n - 1 - start
    for i in range(start, n - 1):
        window = {k: v[: i + 1] for k, v in candles.items()}
        verdict = get_verdict(window, args.rsi_overbought, args.rsi_oversold, args.atr_period)
        v = verdict["verdict"]
        verdict_counts[v] += 1

        if open_trade is not None and i < open_until:
            if v in ("LONG_NOW", "SHORT_NOW"):
                sig_dir = "LONG" if v == "LONG_NOW" else "SHORT"
                if sig_dir == open_trade["direction"]:
                    overlap_same += 1
                else:
                    overlap_opp += 1
            if not args.quiet and (i - start) % 500 == 0:
                _progress(i - start, total, len(trades), t0)
            continue

        if open_trade is not None and i >= open_until:
            open_trade = None

        if v in ("LONG_NOW", "SHORT_NOW"):
            direction = "LONG" if v == "LONG_NOW" else "SHORT"
            trade = simulate_trade(
                candles, i, direction,
                args.tp, args.sl, args.horizon, args.exit_mode, args.fee,
            )
            trade["regime"] = verdict["regime"]
            trade["override_type"] = verdict["override_type"]
            trade["entry_score"] = verdict["entry_score"]
            trade["entry_signals"] = verdict["entry_signals"]
            if args.ml_filter:
                trade["ml"] = _ml_direction(candles, i)

            trades.append(trade)
            open_trade = trade
            open_until = trade["exit_index"]

        if not args.quiet and (i - start) % 500 == 0:
            _progress(i - start, total, len(trades), t0)

    return {
        "trades": trades,
        "verdict_counts": verdict_counts,
        "overlap_same": overlap_same,
        "overlap_opp": overlap_opp,
        "n_walked": total,
    }


def _progress(done: int, total: int, n_trades: int, t0: float) -> None:
    elapsed = time.time() - t0
    print(f"  ... {done}/{total} candles walked, {n_trades} trades so far, "
          f"{elapsed:.1f}s elapsed", file=sys.stderr)


# ── Aggregation ───────────────────────────────────────────────────────────

def _empty_summary() -> dict:
    return {
        "n": 0, "win_rate": None, "avg_net_pct": None, "total_net_pct": None,
        "expectancy": None, "profit_factor": None, "avg_win_pct": None,
        "avg_loss_pct": None, "avg_bars_held": None,
    }


def _summarize(trades: List[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return _empty_summary()

    wins = [t for t in trades if t["net_pct"] > 0]
    losses = [t for t in trades if t["net_pct"] <= 0]

    total_net = sum(t["net_pct"] for t in trades)
    avg_net = total_net / n
    win_rate = len(wins) / n * 100

    avg_win = (sum(t["net_pct"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["net_pct"] for t in losses) / len(losses)) if losses else 0.0

    gross_profit = sum(t["net_pct"] for t in wins)
    gross_loss = -sum(t["net_pct"] for t in losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    return {
        "n": n,
        "win_rate": win_rate,
        "avg_net_pct": avg_net,
        "total_net_pct": total_net,
        "expectancy": avg_net,
        "profit_factor": profit_factor,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "avg_bars_held": sum(t["bars_held"] for t in trades) / n,
    }


def _breakdown(trades: List[dict], key: str) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = {}
    for t in trades:
        groups.setdefault(str(t[key]), []).append(t)
    return {k: _summarize(v) for k, v in groups.items()}


# ── Baselines ─────────────────────────────────────────────────────────────

def random_baseline(candles: Dict[str, list], trades: List[dict], args: argparse.Namespace) -> dict:
    """
    Same entry points/timing as the strategy, but direction chosen uniformly
    at random (50/50 long/short) each time. Repeated for --random-seeds
    seeds and averaged — isolates whether ENTRY TIMING alone has edge,
    independent of the strategy's directional call.
    """
    if not trades:
        return _empty_summary()

    entry_indices = [t["entry_index"] for t in trades]
    summaries = []
    for seed in range(args.random_seeds):
        rng = random.Random(seed)
        sim_trades = [
            simulate_trade(
                candles, ei, "LONG" if rng.random() < 0.5 else "SHORT",
                args.tp, args.sl, args.horizon, args.exit_mode, args.fee,
            )
            for ei in entry_indices
        ]
        summaries.append(_summarize(sim_trades))

    avg: dict = {"n": len(entry_indices)}
    for k in ["win_rate", "avg_net_pct", "total_net_pct", "expectancy",
              "profit_factor", "avg_win_pct", "avg_loss_pct", "avg_bars_held"]:
        vals = [s[k] for s in summaries if s[k] is not None and s[k] != float("inf")]
        avg[k] = (sum(vals) / len(vals)) if vals else None
    return avg


def inverted_baseline(candles: Dict[str, list], trades: List[dict], args: argparse.Namespace) -> dict:
    """
    Same entries/timing as the strategy, but with direction flipped
    (LONG<->SHORT) — tests whether the strategy's directional CALL has
    negative edge (in which case inverting it would itself be profitable).
    """
    sim_trades = [
        simulate_trade(
            candles, t["entry_index"], "SHORT" if t["direction"] == "LONG" else "LONG",
            args.tp, args.sl, args.horizon, args.exit_mode, args.fee,
        )
        for t in trades
    ]
    return _summarize(sim_trades)


def buy_and_hold(candles: Dict[str, list], start_index: int, end_index: int, fee_pct: float) -> dict:
    """Benchmark: buy at the close of `start_index`, hold to the close of `end_index`."""
    closes = candles["close"]
    entry_price = closes[start_index]
    exit_price = closes[end_index]
    gross_pct = (exit_price - entry_price) / entry_price * 100
    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_pct": gross_pct,
        "net_pct": gross_pct - 2 * fee_pct,
        "bars_held": end_index - start_index,
    }


# ── Report ────────────────────────────────────────────────────────────────

def _fmt(v, fmt: str = "{:.2f}", default: str = "—") -> str:
    return default if v is None else fmt.format(v)


def _print_row(label: str, s: dict) -> None:
    print(
        f"{label:28s}"
        f"{_fmt(s['n'], '{:d}'):>8s}"
        f"{_fmt(s['win_rate']):>8s}"
        f"{_fmt(s['avg_net_pct']):>9s}"
        f"{_fmt(s['total_net_pct']):>11s}"
        f"{_fmt(s['profit_factor']):>8s}"
        f"{_fmt(s['avg_bars_held'], '{:.1f}'):>9s}"
    )


def _print_breakdown_table(groups: Dict[str, dict]) -> None:
    if not groups:
        print("  (no trades)")
        return
    print(f"  {'':14s}{'Trades':>8s}{'Win%':>8s}{'AvgNet%':>9s}{'TotalNet%':>11s}{'PF':>8s}{'AvgBars':>9s}")
    for label, s in sorted(groups.items()):
        print(
            f"  {label:14s}"
            f"{_fmt(s['n'], '{:d}'):>8s}"
            f"{_fmt(s['win_rate']):>8s}"
            f"{_fmt(s['avg_net_pct']):>9s}"
            f"{_fmt(s['total_net_pct']):>11s}"
            f"{_fmt(s['profit_factor']):>8s}"
            f"{_fmt(s['avg_bars_held'], '{:.1f}'):>9s}"
        )


def _print_ml_breakdown(trades: List[dict]) -> None:
    agree, disagree, unknown = [], [], []
    for t in trades:
        ml = t.get("ml") or {}
        ml_dir = ml.get("direction", "UNKNOWN")
        sig_dir = "UP" if t["direction"] == "LONG" else "DOWN"
        if ml_dir == "UNKNOWN":
            unknown.append(t)
        elif ml_dir == sig_dir:
            agree.append(t)
        else:
            disagree.append(t)

    _print_breakdown_table({
        "ML agrees": _summarize(agree),
        "ML disagrees": _summarize(disagree),
        "ML unavailable": _summarize(unknown),
    })


def print_report(candles: Dict[str, list], walk: dict, args: argparse.Namespace) -> None:
    trades = walk["trades"]
    complete = [t for t in trades if not t["incomplete"]]

    print("=" * 72)
    print("SIGNAL-ONLY BACKTEST -- signal_lab/signal_fn.get_verdict()")
    print("=" * 72)
    print(f"Candles in dataset      : {len(candles['close'])}")
    print(f"Walked (point-in-time)  : {walk['n_walked']}")
    print(f"Exit mode               : {args.exit_mode}  "
          f"(TP {args.tp}% / SL {args.sl}% / horizon {args.horizon} candles)")
    print(f"Fee per side            : {args.fee}%  (round trip {2 * args.fee}%)")
    print()

    print("--- Verdict frequency (every walked candle) ---")
    vc = walk["verdict_counts"]
    total_v = sum(vc.values()) or 1
    for k in ("LONG_NOW", "SHORT_NOW", "WATCH", "WAIT"):
        print(f"  {k:10s}: {vc[k]:6d}  ({vc[k] / total_v * 100:5.1f}%)")
    print()

    print(f"Signals -> trades opened : {len(trades)}")
    print(f"  complete                : {len(complete)}")
    print(f"  incomplete (ran off end): {len(trades) - len(complete)}")
    print("Overlapping signals while in position (not traded, diagnostic only):")
    print(f"  same direction          : {walk['overlap_same']}")
    print(f"  opposite direction      : {walk['overlap_opp']}")
    print()

    if len(complete) < 100:
        print(f"*** WARNING: only {len(complete)} completed trade(s) -- results below are")
        print("*** NOT statistically meaningful. Use a longer dataset / lower --max-candles")
        print("*** restriction, or a smaller --horizon, to collect more trades.")
        print()

    strat = _summarize(complete)
    inv = inverted_baseline(candles, complete, args)
    rnd = random_baseline(candles, complete, args)

    start_idx = complete[0]["entry_index"] if complete else WARMUP_CANDLES - 1
    end_idx = len(candles["close"]) - 1
    bh = buy_and_hold(candles, start_idx, end_idx, args.fee)

    print("--- Strategy vs. baselines (net of fees) ---")
    print(f"{'':28s}{'Trades':>8s}{'Win%':>8s}{'AvgNet%':>9s}{'TotalNet%':>11s}{'PF':>8s}{'AvgBars':>9s}")
    _print_row("Signal strategy", strat)
    _print_row("Inverted (flip direction)", inv)
    _print_row(f"Random direction (avg/{args.random_seeds})", rnd)
    print(
        f"{'Buy & hold (same span)':28s}"
        f"{1:>8d}{'—':>8s}{_fmt(bh['net_pct']):>9s}{_fmt(bh['net_pct']):>11s}"
        f"{'—':>8s}{bh['bars_held']:>9d}"
    )
    print()

    print("--- Breakdown by regime at entry ---")
    _print_breakdown_table(_breakdown(complete, "regime"))
    print()

    print("--- Breakdown by side ---")
    _print_breakdown_table(_breakdown(complete, "direction"))
    print()

    print("--- Breakdown by exit reason ---")
    _print_breakdown_table(_breakdown(complete, "exit_reason"))
    print()

    if args.ml_filter:
        print("--- ML overlay: core.ml_signals.predict_direction() agreement ---")
        if complete:
            _print_ml_breakdown(complete)
        else:
            print("  (no completed trades)")
        print()


# ── CLI ───────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Signal-only backtest harness for signal_lab.signal_fn.get_verdict(). "
                     "Opens ONE fixed-size trade per LONG_NOW/SHORT_NOW signal -- no DCA "
                     "ladder, no averaging down -- and compares the result against "
                     "random-direction, inverted, and buy-and-hold baselines.",
    )
    p.add_argument("csv", nargs="?", default=None,
                    help="path to an OHLCV CSV with open/high/low/close[/volume] columns "
                         f"(case-insensitive). If omitted, the first *.csv in {DATA_DIR} "
                         "is used, or a synthetic random-walk series if none is found.")
    p.add_argument("--max-candles", type=int, default=None,
                    help="keep only the most recent N candles. get_verdict() is O(window) "
                         "per call, so the walk is O(n^2) -- use this to bound runtime on "
                         "large datasets.")
    p.add_argument("--horizon", type=int, default=12,
                    help="max candles to hold a trade (default: 12).")
    p.add_argument("--exit-mode", choices=["tpsl", "horizon"], default="tpsl",
                    help="'tpsl' (default): exit on TP/SL touch, or at the horizon candle's "
                         "close if neither hits (TIMEOUT). 'horizon': ignore TP/SL, always "
                         "exit at the horizon candle's close.")
    p.add_argument("--tp", type=float, default=3.0, help="take-profit %% (default: 3.0).")
    p.add_argument("--sl", type=float, default=2.0, help="stop-loss %% (default: 2.0).")
    p.add_argument("--fee", type=float, default=0.1,
                    help="fee %% per side, deducted on both entry and exit (default: 0.1).")
    p.add_argument("--random-seeds", type=int, default=500,
                    help="number of seeds averaged for the random-direction baseline "
                         "(default: 500).")
    p.add_argument("--ml-filter", action="store_true",
                    help="tag each trade with core.ml_signals.predict_direction() and "
                         "report results split by ML agreement. SLOW: trains a fresh "
                         "RandomForest per trade.")
    p.add_argument("--rsi-overbought", type=float, default=70.0,
                    help="passed through to get_verdict() (default: 70.0).")
    p.add_argument("--rsi-oversold", type=float, default=30.0,
                    help="passed through to get_verdict() (default: 30.0).")
    p.add_argument("--atr-period", type=int, default=14,
                    help="passed through to get_verdict() (default: 14).")
    p.add_argument("--quiet", action="store_true", help="suppress progress output to stderr.")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    csv_path = _find_csv(args.csv)
    if csv_path and os.path.isfile(csv_path):
        print(f"Loading candles from: {csv_path}")
        candles = _load_candles_from_csv(csv_path, args.max_candles)
        source = csv_path
    else:
        n = args.max_candles or DEFAULT_SYNTHETIC_CANDLES
        print(f"No CSV found in {DATA_DIR} -- using a synthetic {n}-candle OHLCV series instead.")
        candles = _synthetic_candles(n)
        source = "synthetic"

    n = len(candles["close"])
    print(f"Source: {source}")
    print(f"Candles loaded: {n}")
    if n <= WARMUP_CANDLES:
        print(f"ERROR: need more than {WARMUP_CANDLES} candles, got {n}.")
        sys.exit(1)
    print()

    walk = run_walk(candles, args)
    print_report(candles, walk, args)


if __name__ == "__main__":
    main()
