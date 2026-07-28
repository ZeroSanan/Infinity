"""
Database module — SQLite backend for Infinity Market Signals Dashboard
======================================================================
Single shared database: data/infinity.db

Tables:
  layer1_manual          — Layer 1 manual input fields (global, not per-coin)
  liquidation_clusters   — AI analysis liq cluster entries (per-coin)
  liq24h_manual          — Market Mechanics 24h liquidations (per-coin)
  exchange_flow_manual   — Market Mechanics exchange net flow (per-coin)
  whale_orders           — Market Mechanics whale order watchlist (per-coin)
  checklist_state        — Pre-Trade Checklist direction + TP state (per-coin)
  position_ratio_manual  — Position Ratio manual fallback (per-coin)
  signal_history         — Layer 1/2/3 periodic snapshots (replaces signal_history.json)
  signal_state           — Tiered signal evaluation state (replaces signal_state.json)
  signal_config          — Signal system configuration (replaces signal_config.json)

Thread safety: SQLite WAL mode + new connection per operation (no shared state).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH  = os.path.join(DATA_DIR, "infinity.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS layer1_manual (
    field_key   TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS liquidation_clusters (
    symbol        TEXT PRIMARY KEY,
    cluster_below REAL,
    cluster_above REAL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS liq24h_manual (
    symbol     TEXT PRIMARY KEY,
    longs      REAL,
    shorts     REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exchange_flow_manual (
    symbol     TEXT PRIMARY KEY,
    direction  TEXT,
    size       TEXT,
    notes      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whale_orders (
    id         TEXT PRIMARY KEY,
    symbol     TEXT NOT NULL,
    price      REAL NOT NULL,
    size_str   TEXT,
    direction  TEXT,
    status     TEXT DEFAULT 'pending',
    notes      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checklist_state (
    symbol     TEXT PRIMARY KEY,
    direction  TEXT,
    tp_pct     REAL,
    entry_price REAL,
    stop_loss  REAL,
    position_size REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_ratio_manual (
    symbol     TEXT PRIMARY KEY,
    long_pct   REAL,
    short_pct  REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT,
    layer         INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_history_sym_layer ON signal_history (symbol, layer, recorded_at);

CREATE TABLE IF NOT EXISTS signal_state (
    symbol              TEXT PRIMARY KEY,
    tier                TEXT NOT NULL,
    direction           TEXT,
    criteria_confirmed  INTEGER,
    criteria_detail_json TEXT,
    long_confirmed      INTEGER,
    short_confirmed     INTEGER,
    master_summary      TEXT,
    entered_tier_at     TEXT,
    last_evaluated_at   TEXT,
    last_notified_tier  TEXT
);

CREATE TABLE IF NOT EXISTS signal_config (
    config_key  TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL
);
"""

_DEFAULT_CONFIG = {
    "liq_proximity_pct":       3.0,
    "oi_consecutive_cycles":   2,
    "telegram_enabled":        True,
    "tp_achievability_in_msg": True,
}


# ── Connection ────────────────────────────────────────────────────────────────


@contextmanager
def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    """Create all tables and indexes if they don't exist. Safe to call repeatedly."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cutoff(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Layer 1 manual ────────────────────────────────────────────────────────────


def l1_get_all() -> dict:
    """Return all Layer 1 manual fields as {field_key: value_dict}."""
    with _conn() as c:
        rows = c.execute("SELECT field_key, value_json FROM layer1_manual").fetchall()
    result = {}
    for row in rows:
        try:
            result[row["field_key"]] = json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def l1_upsert(field_key: str, value_dict: dict) -> None:
    """Upsert one Layer 1 manual field. value_dict is stored as JSON."""
    with _conn() as c:
        c.execute(
            "INSERT INTO layer1_manual (field_key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(field_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (field_key, json.dumps(value_dict), _now()),
        )


def l1_upsert_raw(field_key: str, value_json_str: str) -> None:
    """Upsert with a pre-serialized JSON string (used during flat-file migration)."""
    with _conn() as c:
        c.execute(
            "INSERT INTO layer1_manual (field_key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(field_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (field_key, value_json_str, _now()),
        )


# ── Liquidation clusters ──────────────────────────────────────────────────────


def liq_clusters_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT cluster_below, cluster_above, updated_at FROM liquidation_clusters WHERE symbol=?",
            (symbol,),
        ).fetchone()
    if not row:
        return {"cluster_below": None, "cluster_above": None, "updated_at": None}
    return dict(row)


def liq_clusters_upsert(symbol: str, cluster_below: Optional[float],
                          cluster_above: Optional[float]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO liquidation_clusters (symbol, cluster_below, cluster_above, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET cluster_below=excluded.cluster_below, "
            "cluster_above=excluded.cluster_above, updated_at=excluded.updated_at",
            (symbol, cluster_below, cluster_above, _now()),
        )


def liq_clusters_get_all() -> dict:
    """Return all liq clusters as {symbol: {cluster_below, cluster_above, updated_at}}."""
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, cluster_below, cluster_above, updated_at FROM liquidation_clusters"
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


# ── 24h Liquidations ──────────────────────────────────────────────────────────


def liq24h_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT longs, shorts, updated_at FROM liq24h_manual WHERE symbol=?", (symbol,)
        ).fetchone()
    if not row:
        return {"longs": None, "shorts": None, "updated_at": None}
    return dict(row)


def liq24h_upsert(symbol: str, longs: Optional[float], shorts: Optional[float]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO liq24h_manual (symbol, longs, shorts, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET longs=excluded.longs, shorts=excluded.shorts, updated_at=excluded.updated_at",
            (symbol, longs, shorts, _now()),
        )


def liq24h_get_all() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT symbol, longs, shorts, updated_at FROM liq24h_manual").fetchall()
    return {row["symbol"]: dict(row) for row in rows}


# ── Exchange net flow ─────────────────────────────────────────────────────────


def ef_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT direction, size, notes, updated_at FROM exchange_flow_manual WHERE symbol=?",
            (symbol,),
        ).fetchone()
    if not row:
        return {"direction": None, "size": None, "notes": None, "updated_at": None}
    return dict(row)


def ef_upsert(symbol: str, direction: Optional[str],
               size: Optional[str], notes: Optional[str]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO exchange_flow_manual (symbol, direction, size, notes, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET direction=excluded.direction, size=excluded.size, "
            "notes=excluded.notes, updated_at=excluded.updated_at",
            (symbol, direction, size, notes, _now()),
        )


def ef_get_all() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT symbol, direction, size, notes, updated_at FROM exchange_flow_manual").fetchall()
    return {row["symbol"]: dict(row) for row in rows}


# ── Whale orders ──────────────────────────────────────────────────────────────


def whale_get(symbol: str) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, symbol, price, size_str, direction, status, notes, created_at, updated_at "
            "FROM whale_orders WHERE symbol=? ORDER BY created_at ASC",
            (symbol,),
        ).fetchall()
    return [dict(r) for r in rows]


def whale_get_all() -> dict:
    """Return all whale orders grouped by symbol."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, symbol, price, size_str, direction, status, notes, created_at, updated_at "
            "FROM whale_orders ORDER BY symbol, created_at ASC"
        ).fetchall()
    result: dict = {}
    for row in rows:
        sym = row["symbol"]
        result.setdefault(sym, []).append(dict(row))
    return result


def whale_insert(symbol: str, order_id: str, price: float, size_str: Optional[str],
                  direction: Optional[str], notes: Optional[str]) -> None:
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO whale_orders "
            "(id, symbol, price, size_str, direction, status, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'pending',?,?,?)",
            (order_id, symbol, price, size_str, direction, notes, now, now),
        )


def whale_update_status(order_id: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE whale_orders SET status=?, updated_at=? WHERE id=?",
            (status, _now(), order_id),
        )


def whale_delete(order_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM whale_orders WHERE id=?", (order_id,))


# ── Checklist state ───────────────────────────────────────────────────────────


def checklist_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT direction, tp_pct, entry_price, stop_loss, position_size, updated_at "
            "FROM checklist_state WHERE symbol=?",
            (symbol,),
        ).fetchone()
    if not row:
        return {"direction": "long", "tp_pct": None, "entry_price": None,
                "stop_loss": None, "position_size": None, "updated_at": None}
    return dict(row)


def checklist_get_all() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, direction, tp_pct, entry_price, stop_loss, position_size, updated_at "
            "FROM checklist_state"
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def checklist_upsert(symbol: str, **kwargs) -> None:
    """Upsert checklist state. Only provided kwargs are written."""
    existing = checklist_get(symbol)
    for k, v in kwargs.items():
        if k in ("direction", "tp_pct", "entry_price", "stop_loss", "position_size"):
            existing[k] = v
    with _conn() as c:
        c.execute(
            "INSERT INTO checklist_state "
            "(symbol, direction, tp_pct, entry_price, stop_loss, position_size, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET direction=excluded.direction, tp_pct=excluded.tp_pct, "
            "entry_price=excluded.entry_price, stop_loss=excluded.stop_loss, "
            "position_size=excluded.position_size, updated_at=excluded.updated_at",
            (symbol, existing.get("direction"), existing.get("tp_pct"), existing.get("entry_price"),
             existing.get("stop_loss"), existing.get("position_size"), _now()),
        )


# ── Position ratio manual ─────────────────────────────────────────────────────


def position_ratio_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT long_pct, short_pct, updated_at FROM position_ratio_manual WHERE symbol=?",
            (symbol,),
        ).fetchone()
    if not row:
        return {"long_pct": None, "short_pct": None, "updated_at": None}
    return dict(row)


def position_ratio_get_all() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, long_pct, short_pct, updated_at FROM position_ratio_manual"
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def position_ratio_upsert(symbol: str, long_pct: Optional[float],
                            short_pct: Optional[float]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO position_ratio_manual (symbol, long_pct, short_pct, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET long_pct=excluded.long_pct, short_pct=excluded.short_pct, updated_at=excluded.updated_at",
            (symbol, long_pct, short_pct, _now()),
        )


# ── Signal history ────────────────────────────────────────────────────────────


def signal_history_insert(symbol: Optional[str], layer: int, snapshot: dict) -> None:
    recorded_at = snapshot.get("ts") or _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO signal_history (symbol, layer, snapshot_json, recorded_at) VALUES (?,?,?,?)",
            (symbol, layer, json.dumps(snapshot), recorded_at),
        )


def signal_history_get(symbol: Optional[str], layer: int, days: float) -> list:
    cutoff = _cutoff(days)
    with _conn() as c:
        if symbol is None:
            rows = c.execute(
                "SELECT snapshot_json FROM signal_history WHERE layer=? AND recorded_at>=? ORDER BY recorded_at ASC",
                (layer, cutoff),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT snapshot_json FROM signal_history WHERE symbol=? AND layer=? AND recorded_at>=? ORDER BY recorded_at ASC",
                (symbol, layer, cutoff),
            ).fetchall()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def signal_history_cleanup(retention_days: int = 90) -> None:
    cutoff = _cutoff(retention_days)
    with _conn() as c:
        c.execute("DELETE FROM signal_history WHERE recorded_at < ?", (cutoff,))


def signal_history_recent_layer2(symbol: str, n: int) -> list:
    """Return the last n Layer 2 snapshots for a symbol (newest last)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT snapshot_json FROM signal_history WHERE symbol=? AND layer=2 "
            "ORDER BY recorded_at DESC LIMIT ?",
            (symbol, n),
        ).fetchall()
    result = []
    for row in reversed(rows):
        try:
            result.append(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# ── Signal state ──────────────────────────────────────────────────────────────


def signal_state_get_all() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT * FROM signal_state").fetchall()
    result = {}
    for row in rows:
        d = dict(row)
        try:
            d["criteria_detail"] = json.loads(d.pop("criteria_detail_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["criteria_detail"] = {}
        result[d["symbol"]] = d
    return result


def signal_state_get(symbol: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM signal_state WHERE symbol=?", (symbol,)).fetchone()
    if not row:
        return {}
    d = dict(row)
    try:
        d["criteria_detail"] = json.loads(d.pop("criteria_detail_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["criteria_detail"] = {}
    return d


def signal_state_upsert(symbol: str, data: dict) -> None:
    criteria_json = json.dumps(data.get("criteria_detail", {}))
    with _conn() as c:
        c.execute(
            "INSERT INTO signal_state "
            "(symbol, tier, direction, criteria_confirmed, criteria_detail_json, "
            "long_confirmed, short_confirmed, master_summary, entered_tier_at, "
            "last_evaluated_at, last_notified_tier) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET tier=excluded.tier, direction=excluded.direction, "
            "criteria_confirmed=excluded.criteria_confirmed, criteria_detail_json=excluded.criteria_detail_json, "
            "long_confirmed=excluded.long_confirmed, short_confirmed=excluded.short_confirmed, "
            "master_summary=excluded.master_summary, entered_tier_at=excluded.entered_tier_at, "
            "last_evaluated_at=excluded.last_evaluated_at, last_notified_tier=excluded.last_notified_tier",
            (symbol, data.get("tier", "NONE"), data.get("direction"),
             data.get("criteria_confirmed"), criteria_json,
             data.get("long_confirmed"), data.get("short_confirmed"),
             data.get("master_summary"), data.get("entered_tier_at"),
             data.get("last_evaluated_at"), data.get("last_notified_tier")),
        )


def signal_state_set_notified(symbol: str, tier: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE signal_state SET last_notified_tier=? WHERE symbol=?", (tier, symbol)
        )


# ── Signal config ─────────────────────────────────────────────────────────────


def signal_config_get_all() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT config_key, value_json FROM signal_config").fetchall()
    cfg = dict(_DEFAULT_CONFIG)
    for row in rows:
        try:
            cfg[row["config_key"]] = json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return cfg


def signal_config_set(key: str, value: Any) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO signal_config (config_key, value_json) VALUES (?,?) "
            "ON CONFLICT(config_key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value)),
        )


def signal_config_set_all(cfg: dict) -> None:
    for k, v in cfg.items():
        signal_config_set(k, v)
