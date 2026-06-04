"""
DCA Strategy Optimizer

Exhaustive grid search over strategy parameters.
Supports multiple ranking metrics — set "ranking_metric" in the config:
  "roi"         — highest total return (default)
  "roi_per_day" — ROI divided by avg trade duration; rewards fast-cycling strategies
  "composite"   — (ROI × win_rate) / avg_duration; balances all three dimensions

Imports DCAStrategy from the existing system — no modifications to any existing files.

Usage:
    cd algo-trading
    python optimizer.py                           # uses optimizer_config.json
    python optimizer.py my_optimizer_config.json  # custom config

Output:
    - Live progress with ETA during the run
    - Console table of top N strategies ranked by chosen metric
    - optimizer_results.csv  — full results for every combination
    - best_strategy_config.json — best config, ready for run_backtest.py
"""

import sys
import os
import json
import time
import csv
import multiprocessing
from pathlib import Path
from typing import List, Dict, Any, Optional
from itertools import product
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

# ── Import existing DCA system (read-only) ────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from dca_strategy import DCAStrategy, load_and_prepare_data


# ── Worker-process globals ────────────────────────────────────────────────────
# Each subprocess gets its own copy via the initializer — avoids re-reading disk.
_WORKER_DF: Optional[pd.DataFrame] = None


def _init_worker(df_records: list) -> None:
    """Load shared price data into each worker process once."""
    global _WORKER_DF
    _WORKER_DF = pd.DataFrame(df_records)
    _WORKER_DF["datetime"] = pd.to_datetime(_WORKER_DF["datetime"])


def _run_single_backtest(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Worker function: run one backtest.
    Relies on _WORKER_DF set by _init_worker — must be a module-level function.
    """
    global _WORKER_DF

    if params.get("mode") == "short":
        from short_dca_strategy import ShortDCAStrategy
        strategy = ShortDCAStrategy(
            initial_budget=params["initial_budget"],
            dca_levels=params["dca_levels"],
            take_profit_percent=params["take_profit_percent"],
            stop_loss_percent=params["stop_loss_percent"],
        )
    else:
        strategy = DCAStrategy(
            initial_budget=params["initial_budget"],
            dca_levels=params["dca_levels"],
            take_profit_percent=params["take_profit_percent"],
            stop_loss_percent=params["stop_loss_percent"],
        )

    try:
        strategy.run_backtest(_WORKER_DF)
        r = strategy.calculate_backtest_results()

        return {
            **params,
            "roi":                    round(r.total_roi, 4),
            "net_pnl":                round(r.net_pnl, 2),
            "total_trades":           r.total_trades,
            "win_rate":               round(r.win_rate, 2),
            "stop_loss_rate":         round(r.stop_loss_rate, 2),
            "avg_trade_pnl":          round(r.avg_trade_pnl, 2),
            "avg_trade_duration_days": round(r.avg_trade_duration_days, 2),
            "largest_loss":           round(r.largest_loss, 2),
            "largest_profit":         round(r.largest_profit, 2),
            "error":                  None,
        }
    except Exception as exc:
        return {**params, "roi": -9999.0, "error": str(exc)}


# ── Parameter generation ──────────────────────────────────────────────────────

def _generate_levels(first: float, step: float, n: int, mode: str = "long") -> List[float]:
    """
    Build evenly-spaced DCA levels.
    Long  (mode="long"):  first=-6, step=3, n=4  →  [-6, -9, -12, -15]  (deepening dumps)
    Short (mode="short"): first= 6, step=3, n=4  →  [ 6,  9,  12,  15]  (rising pumps)
    """
    if mode == "short":
        return [round(first + step * i, 2) for i in range(n)]
    return [round(first - step * i, 2) for i in range(n)]


def build_param_grid(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand config parameter_grid into a flat list of param dicts."""
    g      = config["parameter_grid"]
    budget = config["initial_budget"]
    mode   = config.get("mode", "long")
    combos: List[Dict[str, Any]] = []

    for first, step, n, tp, sl in product(
        g["first_level"],
        g["level_step"],
        g["num_levels"],
        g["take_profit_percent"],
        g["stop_loss_percent"],
    ):
        levels = _generate_levels(first, step, n, mode)
        # Skip configs where deepest level is beyond ±70%
        if mode == "short" and levels[-1] > 70:
            continue
        if mode == "long"  and levels[-1] < -70:
            continue
        combos.append({
            "mode":                 mode,
            "initial_budget":       budget,
            "dca_levels":           levels,
            "first_level":          first,
            "level_step":           step,
            "num_levels":           n,
            "take_profit_percent":  tp,
            "stop_loss_percent":    sl,
        })

    return combos


# ── Ranking metric ───────────────────────────────────────────────────────────

def _compute_score(result: Dict[str, Any], metric: str) -> float:
    """
    Score a backtest result for ranking.

    roi         — raw total ROI %
    roi_per_day — ROI % / avg_trade_duration_days  (return per day deployed)
    composite   — (ROI % × win_rate%) / avg_duration  (speed + quality combined)
    """
    roi = result.get("roi", -9999.0)
    dur = max(result.get("avg_trade_duration_days", 1) or 1, 0.1)
    win = result.get("win_rate", 0.0)

    if metric == "roi_per_day":
        return roi / dur
    elif metric == "composite":
        return (roi * (win / 100.0)) / dur
    else:
        return roi


# ── Config loading ────────────────────────────────────────────────────────────

def load_optimizer_config(path: str) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(config_path) as f:
        cfg = json.load(f)

    for key in ("csv_file", "initial_budget", "parameter_grid"):
        if key not in cfg:
            raise ValueError(f"Missing required key in config: '{key}'")

    for param in ("first_level", "level_step", "num_levels", "take_profit_percent", "stop_loss_percent"):
        if param not in cfg["parameter_grid"]:
            raise ValueError(f"Missing parameter_grid entry: '{param}'")

    return cfg


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_progress(done: int, total: int, elapsed: float) -> None:
    pct = done / total
    bar_len = 35
    filled = int(bar_len * pct)
    bar = "█" * filled + "░" * (bar_len - filled)
    eta = (elapsed / done) * (total - done) if done else 0
    print(
        f"\r  [{bar}] {done}/{total} ({pct*100:.0f}%)  ETA {eta:.0f}s    ",
        end="",
        flush=True,
    )


def _results_table(results: List[Dict], top_n: int, metric: str = "roi") -> str:
    valid = [r for r in results if r.get("error") is None]
    top = sorted(valid, key=lambda x: _compute_score(x, metric), reverse=True)[:top_n]

    metric_labels = {
        "roi":         "ROI%",
        "roi_per_day": "ROI%/Day",
        "composite":   "Score",
    }
    score_label = metric_labels.get(metric, "Score")

    col = {
        "rank":   5,
        "levels": 32,
        "tp":     6,
        "sl":     6,
        "roi":    9,
        "score":  10,
        "trades": 8,
        "wr":     9,
        "days":   9,
    }

    header = (
        f"{'#':<{col['rank']}}"
        f"{'DCA Levels':<{col['levels']}}"
        f"{'TP%':<{col['tp']}}"
        f"{'SL%':<{col['sl']}}"
        f"{'ROI%':>{col['roi']}}"
        f"{score_label:>{col['score']}}"
        f"{'Trades':>{col['trades']}}"
        f"{'WinRate%':>{col['wr']}}"
        f"{'AvgDays':>{col['days']}}"
    )
    sep = "─" * (sum(col.values()) + len(col) - 1)

    lines = [
        "",
        "=" * 96,
        f"  TOP {top_n} STRATEGIES  [ranked by: {metric}]",
        "=" * 96,
        header,
        sep,
    ]

    for rank, r in enumerate(top, 1):
        lvl_str = str(r["dca_levels"])
        if len(lvl_str) > col["levels"] - 1:
            lvl_str = lvl_str[: col["levels"] - 4] + "...]"
        score = _compute_score(r, metric)
        lines.append(
            f"{rank:<{col['rank']}}"
            f"{lvl_str:<{col['levels']}}"
            f"{r['take_profit_percent']:<{col['tp']}}"
            f"{r['stop_loss_percent']:<{col['sl']}}"
            f"{r['roi']:>+{col['roi']}.2f}%"
            f"{score:>{col['score']}.3f}"
            f"{r['total_trades']:>{col['trades']}}"
            f"{r['win_rate']:>{col['wr']}.1f}%"
            f"{r['avg_trade_duration_days']:>{col['days']}.1f}"
        )

    lines.append("=" * 96)
    return "\n".join(lines)


def _save_csv(results: List[Dict], path: str) -> None:
    if not results:
        return
    rows = []
    for r in results:
        row = dict(r)
        row["dca_levels"] = str(row["dca_levels"])
        rows.append(row)
    rows.sort(key=lambda x: x.get("roi", -9999), reverse=True)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Full results  → {path}")


def _save_best_config(best: Dict, csv_file: str, output_path: str) -> None:
    """Write the winning params as a strategy_config.json-compatible file."""
    n = best["num_levels"]
    equal_alloc = round(best["initial_budget"] / n, 2)
    config = {
        "csv_file":            csv_file,
        "start_date":          "",
        "end_date":            "",
        "initial_budget":      best["initial_budget"],
        "stop_loss_percent":   best["stop_loss_percent"],
        "dca_levels":          best["dca_levels"],
        "dca_allocations":     [equal_alloc] * n,
        "take_profit_percent": best["take_profit_percent"],
        "_optimizer_note": (
            f"Generated by optimizer.py | "
            f"ROI: {best.get('roi', 0):+.2f}% | "
            f"Win rate: {best.get('win_rate', 0):.1f}% | "
            f"Trades: {best.get('total_trades', 0)}"
        ),
    }
    with open(output_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Best config   → {output_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_optimizer(config_file: str = "optimizer_config.json") -> None:
    print("=" * 70)
    print("  DCA STRATEGY OPTIMIZER")
    print("=" * 70)

    cfg = load_optimizer_config(config_file)
    top_n        = cfg.get("top_n_results", 20)
    num_workers  = cfg.get("num_workers", max(1, multiprocessing.cpu_count() - 1))
    results_csv  = cfg.get("results_csv", "optimizer_results.csv")
    best_output  = cfg.get("best_config_output", "best_strategy_config.json")
    metric       = cfg.get("ranking_metric", "roi")
    mode         = cfg.get("mode", "long")

    # ── Load price data ───────────────────────────────────────────────────────
    print(f"\n  Data: {cfg['csv_file']}")
    df = load_and_prepare_data(cfg["csv_file"])

    if cfg.get("start_date"):
        df = df[df["datetime"] >= pd.to_datetime(cfg["start_date"])]
        print(f"  From: {cfg['start_date']}")
    if cfg.get("end_date"):
        df = df[df["datetime"] <= pd.to_datetime(cfg["end_date"])]
        print(f"  To:   {cfg['end_date']}")

    print(f"  Range: {df['datetime'].iloc[0]}  →  {df['datetime'].iloc[-1]}")
    print(f"  Candles: {len(df):,}")

    # ── Build parameter grid ──────────────────────────────────────────────────
    param_grid = build_param_grid(cfg)
    total = len(param_grid)
    grid = cfg["parameter_grid"]
    print(f"\n  Parameter grid:")
    print(f"    first_level:         {grid['first_level']}")
    print(f"    level_step:          {grid['level_step']}")
    print(f"    num_levels:          {grid['num_levels']}")
    print(f"    take_profit_percent: {grid['take_profit_percent']}")
    print(f"    stop_loss_percent:   {grid['stop_loss_percent']}")
    print(f"\n  Mode:               {mode.upper()}")
    print(f"  Total combinations: {total:,}")
    print(f"  Workers:            {num_workers}")

    # ── Serialize DataFrame for workers (sent once per process at startup) ────
    print("\n  Initializing worker pool...")
    df_records = df.assign(datetime=df["datetime"].astype(str)).to_dict("records")

    # ── Run grid search ───────────────────────────────────────────────────────
    results: List[Dict] = []
    done = 0
    t0 = time.time()

    print(f"  Running grid search  (Ctrl+C saves partial results)\n")

    try:
        with ProcessPoolExecutor(
            max_workers=num_workers,
            initializer=_init_worker,
            initargs=(df_records,),
        ) as executor:
            futures = {executor.submit(_run_single_backtest, p): p for p in param_grid}

            for future in as_completed(futures):
                results.append(future.result())
                done += 1
                if done % max(1, total // 200) == 0 or done == total:
                    _print_progress(done, total, time.time() - t0)

    except KeyboardInterrupt:
        print(f"\n\n  Interrupted — saving {len(results)} partial results...")

    elapsed = time.time() - t0
    print(f"\n\n  Finished {done}/{total} combinations in {elapsed:.1f}s")

    if not results:
        print("  No results to show.")
        return

    # ── Display and save ──────────────────────────────────────────────────────
    print(_results_table(results, top_n, metric))

    print("\n  Saving output:")
    _save_csv(results, results_csv)

    valid = [r for r in results if r.get("error") is None]
    if valid:
        best = max(valid, key=lambda x: _compute_score(x, metric))
        _save_best_config(best, cfg["csv_file"], best_output)

        best_score = _compute_score(best, metric)
        metric_labels = {"roi": "ROI%", "roi_per_day": "ROI%/Day", "composite": "Composite"}
        print(f"\n{'='*70}")
        print(f"  BEST STRATEGY SUMMARY  [metric: {metric}]")
        print(f"{'='*70}")
        print(f"  DCA Levels:   {best['dca_levels']}")
        print(f"  Take Profit:  +{best['take_profit_percent']}%")
        sl_label = "disabled" if best["stop_loss_percent"] == 0 else "enabled"
        print(f"  Stop Loss:    {best['stop_loss_percent']}% ({sl_label})")
        print(f"  {metric_labels.get(metric,'Score'):13} {best_score:.4f}")
        print(f"  ROI:          {best.get('roi', 0):+.2f}%")
        print(f"  Net P&L:      ${best.get('net_pnl', 0):,.2f}")
        print(f"  Total Trades: {best.get('total_trades', 0)}")
        print(f"  Win Rate:     {best.get('win_rate', 0):.1f}%")
        print(f"  Avg Duration: {best.get('avg_trade_duration_days', 0):.1f} days")
        print(f"{'='*70}")
        print(f"\n  Run the winner with the existing system:")
        print(f"    cp {best_output} strategy_config.json && python run_backtest.py")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows compatibility
    config_path = sys.argv[1] if len(sys.argv) > 1 else "optimizer_config.json"
    run_optimizer(config_path)
