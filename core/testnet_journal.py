"""
Testnet Learning Journal — auto-logs completed TESTNET DCA trades.

This module is read/written only by web/app.py's testnet-tracking code path
and never touches live state, live config, or core/dca_engine.py. It is
driven by a snapshot-diffing approach (see web/app.py's _track_testnet_journal)
rather than a hook inside the engine: core/state_manager.py's reset_state()
wipes a position's ACTIVE data synchronously in the same call that follows a
TP exit, so there's no reliable window for an external poller to read the
"just exited" state straight off disk. Instead, app.py keeps the last-seen
ACTIVE snapshot for each testnet strategy in memory and treats an
ACTIVE -> WAITING transition as a completed trade, using that snapshot
(plus the engine's own TP-price formula) to reconstruct the exit.
"""
import csv
import io
import json
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)
JOURNAL_PATH = os.path.join(DATA_DIR, "testnet_journal.json")

MAX_STORED_ENTRIES = 500


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_journal() -> list:
    if not os.path.exists(JOURNAL_PATH):
        return []
    with open(JOURNAL_PATH) as f:
        return json.load(f).get("entries", [])


def append_entry(entry: dict):
    entries = load_journal()
    entries.append(entry)
    entries = entries[-MAX_STORED_ENTRIES:]
    with open(JOURNAL_PATH, "w") as f:
        json.dump({"entries": entries}, f, indent=2)


def _combo_key(entry: dict) -> str:
    sig = entry.get("signal_at_entry") or {}
    return f"{sig.get('l1_verdict', '?')} + {sig.get('l2_verdict', '?')} + {sig.get('l3_verdict', '?')}"


def compute_stats(entries: list) -> dict:
    if not entries:
        return {
            "total_trades": 0,
            "win_rate_pct": None,
            "avg_profit_win": None,
            "avg_loss_loss": None,
            "best_combo": None,
            "worst_combo": None,
        }

    wins = [e for e in entries if e.get("outcome") == "WIN"]
    losses = [e for e in entries if e.get("outcome") == "LOSS"]

    combo_stats: dict = {}
    for e in entries:
        key = _combo_key(e)
        bucket = combo_stats.setdefault(key, {"wins": 0, "total": 0})
        bucket["total"] += 1
        if e.get("outcome") == "WIN":
            bucket["wins"] += 1

    combos = [
        {"combo": k, "win_rate_pct": v["wins"] / v["total"] * 100, "trades": v["total"]}
        for k, v in combo_stats.items()
    ]
    best_combo = max(combos, key=lambda c: c["win_rate_pct"]) if combos else None
    worst_combo = min(combos, key=lambda c: c["win_rate_pct"]) if combos else None

    return {
        "total_trades": len(entries),
        "win_rate_pct": len(wins) / len(entries) * 100,
        "avg_profit_win": (sum(e["exit"]["pnl_usdt"] for e in wins) / len(wins)) if wins else None,
        "avg_loss_loss": (sum(e["exit"]["pnl_usdt"] for e in losses) / len(losses)) if losses else None,
        "best_combo": best_combo,
        "worst_combo": worst_combo,
    }


def entries_to_csv(entries: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "coin", "strategy", "l1_verdict", "l2_verdict", "l3_verdict", "master",
        "steps_filled", "avg_entry", "total_invested",
        "exit_price", "pnl_usdt", "pnl_pct", "duration_hours", "outcome",
    ])
    for e in entries:
        sig = e.get("signal_at_entry") or {}
        entry = e.get("entry") or {}
        exit_ = e.get("exit") or {}
        writer.writerow([
            e.get("timestamp"), e.get("coin"), e.get("strategy"),
            sig.get("l1_verdict"), sig.get("l2_verdict"), sig.get("l3_verdict"), sig.get("master"),
            entry.get("steps_filled"), entry.get("avg_entry"), entry.get("total_invested"),
            exit_.get("exit_price"), exit_.get("pnl_usdt"), exit_.get("pnl_pct"), exit_.get("duration_hours"),
            e.get("outcome"),
        ])
    return buf.getvalue()
