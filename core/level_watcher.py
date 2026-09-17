"""
Resistance / Support Level Watcher
===================================
Watches a per-coin list of price levels and fires a Telegram alert when the
live price comes within `alert_pct` of one of them.

Storage: data/resistance_levels.json

  {
    "BTC": {
      "levels": [123238.74, 119805.78],
      "alert_pct": 3.0,
      "notified": {
        "76321.68": {"side": "below", "last_notified": "2026-09-17T08:00:00Z"}
      }
    }
  }

`alert_pct` is per coin; coins that omit it fall back to DEFAULT_ALERT_PCT
(env LEVEL_ALERT_DEFAULT_PCT, default 3.0).

The watched-coin list is whatever is present in the JSON file — this feature
is deliberately NOT tied to the five-coin MS_SYMBOLS set the Layer 1/2/3
dashboard uses, so it can scale to ~20 coins independently.

Re-notification rules (both must allow it before an alert is sent):
  1. Cooldown — the same (coin, level, side) is not re-notified within
     NOTIFY_COOLDOWN_HOURS.
  2. Band re-entry — when price leaves the alert band entirely the stored
     notification for that level is dropped, so a genuine re-approach
     alerts immediately rather than waiting out the cooldown.

`side` is which side of the level the price sits on, so a level that is
approached from below and later from above alerts once for each approach.

This module never imports web.app (app.py imports core.*, so the reverse
would be circular). The caller injects a price lookup — see the
`price_fn` argument on check_levels() — the same dependency-injection shape
SignalEvaluator uses for its recorder.

Side-effect scope: this file, and outbound Telegram messages. It does not
touch Layer 1/2/3 state, the SQLite database, or any verdict logic.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from core.telegram_notifier import send_telegram_message

DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
LEVELS_PATH = os.path.join(DATA_DIR, "resistance_levels.json")

# Fallback when a coin has no alert_pct of its own.
DEFAULT_ALERT_PCT = float(os.getenv("LEVEL_ALERT_DEFAULT_PCT", "3.0"))

# Do not re-alert the same (coin, level, side) more often than this.
NOTIFY_COOLDOWN_HOURS = 4

# Accepted range for alert_pct on write (mirrors the UI input bounds).
MIN_ALERT_PCT = 0.1
MAX_ALERT_PCT = 20.0

# Guards the read-modify-write cycle on the JSON file: the APScheduler job
# thread and Flask request threads both mutate it.
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp; return None if absent or unreadable so a
    corrupt value fails open (alert allowed) rather than silencing a level."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def level_key(level: float) -> str:
    """Stable string key for a level ('76321.68', '4000'). Floats cannot be
    JSON object keys, and str(float) round-trips inconsistently, so levels
    are normalised to a trimmed fixed-point form."""
    return f"{float(level):.8f}".rstrip("0").rstrip(".") or "0"


def symbol_for(coin: str) -> str:
    """'BTC' -> 'BTCUSDT'. Already-suffixed input is passed through."""
    coin = (coin or "").strip().upper()
    return coin if coin.endswith("USDT") else coin + "USDT"


# ── Persistence ───────────────────────────────────────────────────────────────


def _read_file() -> dict:
    """Load the levels file. A missing file is an empty watch list; a corrupt
    one is treated the same way rather than crashing the scheduler job."""
    try:
        with open(LEVELS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️  Level watcher: could not read {LEVELS_PATH} — {exc}")
        return {}


def _write_file(data: dict) -> None:
    """Atomic write — temp file in the same directory, then os.replace, so a
    crash mid-write cannot leave a truncated levels file behind."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = LEVELS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, LEVELS_PATH)


# ── Read API ──────────────────────────────────────────────────────────────────


def get_all() -> dict:
    """Every watched coin with its levels, alert_pct and level_count."""
    with _LOCK:
        data = _read_file()
    return {
        coin: {
            "levels":      cfg.get("levels") or [],
            "alert_pct":   cfg.get("alert_pct") or DEFAULT_ALERT_PCT,
            "level_count": len(cfg.get("levels") or []),
        }
        for coin, cfg in data.items()
    }


def get_coin(coin: str) -> dict:
    """One coin's settings, with empty defaults when it is not yet watched."""
    coin = (coin or "").strip().upper()
    with _LOCK:
        cfg = _read_file().get(coin) or {}
    return {
        "coin":      coin,
        "levels":    cfg.get("levels") or [],
        "alert_pct": cfg.get("alert_pct") or DEFAULT_ALERT_PCT,
        "watched":   bool(cfg),
    }


def watched_coins() -> list[str]:
    """Coin list the scheduler job iterates — driven purely by file content."""
    with _LOCK:
        return sorted(_read_file().keys())


# ── Write API ─────────────────────────────────────────────────────────────────


def validate(levels, alert_pct) -> tuple[list[float], float]:
    """Validate a POST body. Raises ValueError with a user-facing message."""
    if not isinstance(levels, list):
        raise ValueError("levels must be a list of positive numbers")

    clean: list[float] = []
    for raw in levels:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"invalid level: {raw!r}")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"invalid level: {raw!r}")
        if val <= 0 or val != val or val in (float("inf"), float("-inf")):
            raise ValueError(f"levels must be positive numbers, got {raw!r}")
        clean.append(val)

    if alert_pct is None:
        pct = DEFAULT_ALERT_PCT
    else:
        try:
            pct = float(alert_pct)
        except (TypeError, ValueError):
            raise ValueError("alert_pct must be a number")
        if not (MIN_ALERT_PCT <= pct <= MAX_ALERT_PCT):
            raise ValueError(f"alert_pct must be between {MIN_ALERT_PCT} and {MAX_ALERT_PCT}")

    # De-duplicate (a repeated level would alert twice) and sort descending.
    clean = sorted({level_key(v): v for v in clean}.values(), reverse=True)
    return clean, pct


def set_coin(coin: str, levels: list, alert_pct=None) -> dict:
    """Replace a coin's level list and alert_pct. Notification state for
    levels that survive the edit is preserved, so re-saving the same list
    does not re-trigger alerts the cooldown had already suppressed."""
    coin = (coin or "").strip().upper()
    if not coin:
        raise ValueError("coin is required")

    clean, pct = validate(levels, alert_pct)
    keep = {level_key(v) for v in clean}

    with _LOCK:
        data = _read_file()
        cfg  = data.get(coin) or {}
        prev_notified = cfg.get("notified") or {}
        data[coin] = {
            "levels":    clean,
            "alert_pct": pct,
            "notified":  {k: v for k, v in prev_notified.items() if k in keep},
        }
        _write_file(data)

    return {"coin": coin, "levels": clean, "alert_pct": pct, "level_count": len(clean)}


def delete_coin(coin: str) -> bool:
    """Drop a coin from the watch list. False if it was not being watched."""
    coin = (coin or "").strip().upper()
    with _LOCK:
        data = _read_file()
        if coin not in data:
            return False
        del data[coin]
        _write_file(data)
    return True


# ── Alerting ──────────────────────────────────────────────────────────────────


def format_alert(coin: str, level: float, price: float,
                  gap_pct: float, side: str) -> str:
    """Alert body. Plain text — no Markdown emphasis, so a level like
    1_000.5 or a coin with an underscore cannot break Telegram parsing."""
    return (
        f"🔔 {coin} approaching resistance/support\n"
        f"Level: {level:,.8g}\n"
        f"Current: {price:,.8g} ({gap_pct:.2f}% away, from {side})"
    )


def _cooldown_active(prev: dict, side: str, now: datetime) -> bool:
    """True when this level+side was alerted recently enough to stay quiet."""
    if not prev or prev.get("side") != side:
        return False
    last = _parse_iso(prev.get("last_notified"))
    if last is None:
        return False
    return (now - last) < timedelta(hours=NOTIFY_COOLDOWN_HOURS)


def check_levels(coin: str, price_fn: Callable[[str], Optional[float]],
                  notify_fn: Optional[Callable[[str], bool]] = None) -> dict:
    """Check one coin's levels against its live price and alert as needed.

    `price_fn` is injected by the caller (web/app.py passes the Layer 3
    price reader) so this module stays free of Flask and of circular imports.
    `notify_fn` defaults to Telegram and exists as a seam for testing.

    Returns a summary dict; raises only if price_fn raises, which the
    scheduler job catches per coin.
    """
    notify = notify_fn or send_telegram_message
    coin   = (coin or "").strip().upper()

    with _LOCK:
        cfg = dict(_read_file().get(coin) or {})
    levels = cfg.get("levels") or []
    if not levels:
        return {"coin": coin, "status": "no_levels", "alerts": []}

    alert_pct = cfg.get("alert_pct") or DEFAULT_ALERT_PCT
    symbol    = symbol_for(coin)

    price = price_fn(symbol)
    if price is None or price <= 0:
        return {"coin": coin, "status": "no_price", "alerts": []}

    now      = _now()
    now_iso  = _now_iso()
    fired: list[dict] = []
    # Changes are accumulated and applied under the lock at the end, so a
    # concurrent POST /api/levels/<coin> cannot be clobbered by this job.
    to_set:   dict[str, dict] = {}
    to_clear: list[str] = []

    for level in levels:
        try:
            level = float(level)
        except (TypeError, ValueError):
            continue
        if level <= 0:
            continue

        key     = level_key(level)
        gap_pct = abs(price - level) / level * 100
        side    = "above" if price >= level else "below"

        if gap_pct > alert_pct:
            # Outside the band — forget any prior alert so the next approach
            # is eligible immediately, independent of the cooldown.
            to_clear.append(key)
            continue

        with _LOCK:
            prev = ((_read_file().get(coin) or {}).get("notified") or {}).get(key)
        if _cooldown_active(prev, side, now):
            continue

        if notify(format_alert(coin, level, price, gap_pct, side)):
            to_set[key] = {"side": side, "last_notified": now_iso}
            fired.append({"level": level, "gap_pct": round(gap_pct, 2), "side": side})
        else:
            # Delivery failed (or Telegram is unconfigured) — leave the state
            # untouched so the next cycle retries instead of silently skipping.
            print(f"⚠️  Level watcher: {coin} alert for level {key} not delivered")

    if to_set or to_clear:
        with _LOCK:
            data = _read_file()
            entry = data.get(coin)
            if entry is not None:
                notified = entry.get("notified") or {}
                for key in to_clear:
                    notified.pop(key, None)
                notified.update(to_set)
                entry["notified"] = notified
                _write_file(data)

    return {
        "coin":      coin,
        "status":    "ok",
        "price":     price,
        "alert_pct": alert_pct,
        "checked":   len(levels),
        "alerts":    fired,
    }
