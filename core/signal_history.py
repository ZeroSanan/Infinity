"""
Signal History — SQLite persistence for market signals and ML predictions.
Records every fresh signal computation (not cache hits) for later analysis.
"""

from __future__ import annotations
import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "signal_history.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """Create table if it doesn't exist."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT    NOT NULL,
                coin                TEXT    NOT NULL,
                price               REAL,
                regime              TEXT,
                score               INTEGER,
                rsi                 REAL,
                week_ret            REAL,
                rec                 TEXT,
                ml_direction        TEXT,
                ml_direction_conf   REAL,
                ml_regime           TEXT,
                ml_regime_conf      REAL,
                ml_agrees           INTEGER,
                entry_score         INTEGER,
                entry_grade         TEXT,
                action              TEXT,
                entry_ready         INTEGER
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_ts_coin
            ON signal_history (ts, coin)
        """)

        # Migrate older databases (pre-dating the action/entry_ready columns):
        # CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so add
        # any missing columns explicitly.
        existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(signal_history)")}
        for col, col_type in (("action", "TEXT"), ("entry_ready", "INTEGER")):
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signal_history ADD COLUMN {col} {col_type}")


def save_signals(signals: list):
    """Persist a list of signal dicts to the database."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for s in signals:
        if s.get("error"):
            continue
        ml_dir = s.get("ml_direction") or {}
        ml_reg = s.get("ml_regime")    or {}
        ml_ent = s.get("ml_entry")     or {}
        rows.append((
            ts,
            s.get("coin"),
            s.get("price"),
            s.get("regime"),
            s.get("score"),
            s.get("rsi"),
            s.get("week_ret"),
            s.get("rec"),
            ml_dir.get("direction"),
            ml_dir.get("confidence"),
            ml_reg.get("regime"),
            ml_reg.get("confidence"),
            int(bool(ml_reg.get("agrees_with_rules", True))),
            ml_ent.get("score"),
            ml_ent.get("grade"),
            s.get("action"),
            int(bool(s.get("entry_ready", False))),
        ))
    if not rows:
        return
    with _conn() as c:
        c.executemany("""
            INSERT INTO signal_history
            (ts, coin, price, regime, score, rsi, week_ret, rec,
             ml_direction, ml_direction_conf, ml_regime, ml_regime_conf,
             ml_agrees, entry_score, entry_grade, action, entry_ready)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)


def get_history(coin: Optional[str] = None, days: int = 7, limit: int = 500) -> list[dict]:
    """Return recent history rows as list of dicts."""
    query = """
        SELECT * FROM signal_history
        WHERE ts >= datetime('now', ?)
        {}
        ORDER BY ts DESC
        LIMIT ?
    """.format("AND coin = ?" if coin else "")

    params = [f"-{days} days"]
    if coin:
        params.append(coin.upper())
    params.append(limit)

    with _conn() as c:
        rows = c.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_summary(days: int = 7) -> list[dict]:
    """Return per-coin regime counts for the last N days."""
    with _conn() as c:
        rows = c.execute("""
            SELECT coin,
                   regime,
                   COUNT(*) as cnt,
                   ROUND(AVG(entry_score), 1) as avg_entry_score,
                   ROUND(AVG(rsi), 1)         as avg_rsi,
                   ROUND(AVG(price), 2)       as avg_price
            FROM signal_history
            WHERE ts >= datetime('now', ?)
            GROUP BY coin, regime
            ORDER BY coin, cnt DESC
        """, [f"-{days} days"]).fetchall()
    return [dict(r) for r in rows]


# Initialise on import
init_db()
