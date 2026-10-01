"""
Resistance / Support Level Watcher
===================================
Watches a per-coin list of price levels and fires a Telegram alert when the
live price *approaches* one of them.

Storage: data/resistance_levels.json

  {
    "BTC": {
      "levels": [123238.74, 119805.78],
      "alert_pct": 3.0,
      "last_price": 76424.01,
      "last_checked": "2026-10-01T08:00:00Z",
      "level_state": {
        "76321.68": {
          "state": "cooling",
          "pending_since": null,
          "notified_at": "2026-09-15T08:20:00Z",
          "direction": "resistance"
        }
      }
    }
  }

`alert_pct` is per coin; coins that omit it fall back to DEFAULT_ALERT_PCT
(env LEVEL_ALERT_DEFAULT_PCT, default 3.0).

The watched-coin list is whatever is present in the JSON file — this feature
is deliberately NOT tied to the five-coin MS_SYMBOLS set the Layer 1/2/3
dashboard uses, so it can scale to ~20 coins independently.


Noise control
-------------
Four independent rules gate every alert. A level must pass all of them.

1. DIRECTION — the price must be moving *toward* the level. Direction is
   derived by comparing the current price against `last_price`, the price at
   the previous check. A level above a rising price is RESISTANCE; a level
   below a falling price is SUPPORT. A level that sits inside the band while
   price moves away from it never alerts.

2. CONFIRMATION — a level must qualify on two consecutive checks before it
   fires. The first qualifying check moves it `armed -> pending`; the second
   fires the alert. A single tick that happens to land inside the band is not
   enough. If a pending level's gap widens beyond `alert_pct`, the pending
   state is dropped and confirmation starts over.

3. NEAREST ONLY — at most one alert per coin per check. When several levels
   qualify at once, only the closest fires; the rest keep their state and can
   still fire on a later check once the nearest one has cleared.

4. HYSTERESIS RE-ARM — after firing, a level cannot fire again until price has
   moved at least `REARM_DISTANCE_MULTIPLIER x alert_pct` away from it (so 6%
   for a 3% band) and then re-entered the band. This replaces the fixed-time
   cooldown the first version used: a timer silences a genuine second approach
   and permits a meaningless one, whereas distance measures the thing actually
   worth reacting to.


Per-level state machine
-----------------------
    armed ──(in band, approaching)──> pending ──(confirmed again)──> notified
      ^                                  │                              │
      │                                  │ (gap > alert_pct)            │ (next check)
      │                                  v                              v
      └────────(gap >= 2 x alert_pct)── armed <───────────────────── cooling

`armed` is the implicit default: a level with no stored state is armed.


This module never imports web.app (app.py imports core.*, so the reverse
would be circular). The caller injects a price lookup — see the `price_fn`
argument on check_levels() — the same dependency-injection shape
SignalEvaluator uses for its recorder.

Side-effect scope: this file, and outbound Telegram messages. It does not
touch Layer 1/2/3 state, the SQLite database, or any verdict logic.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from core.telegram_notifier import send_telegram_message

DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
LEVELS_PATH = os.path.join(DATA_DIR, "resistance_levels.json")

# Fallback when a coin has no alert_pct of its own.
DEFAULT_ALERT_PCT = float(os.getenv("LEVEL_ALERT_DEFAULT_PCT", "3.0"))

# After firing, a level must get this many times `alert_pct` away before it can
# arm again. 2.0 with a 3% band means price must travel 6% from the level.
REARM_DISTANCE_MULTIPLIER = 2.0

# Accepted range for alert_pct on write (mirrors the UI input bounds).
MIN_ALERT_PCT = 0.1
MAX_ALERT_PCT = 20.0

# Per-level states. `armed` is also the default for a level with no entry.
STATE_ARMED    = "armed"
STATE_PENDING  = "pending"
STATE_NOTIFIED = "notified"
STATE_COOLING  = "cooling"

# Directional labels.
DIR_RESISTANCE = "resistance"
DIR_SUPPORT    = "support"

# Guards the read-modify-write cycle on the JSON file: the APScheduler job
# thread and Flask request threads both mutate it.
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def level_key(level: float) -> str:
    """Stable string key for a level ('76321.68', '4000'). Floats cannot be
    JSON object keys, and str(float) round-trips inconsistently, so levels
    are normalised to a trimmed fixed-point form."""
    return f"{float(level):.8f}".rstrip("0").rstrip(".") or "0"


def symbol_for(coin: str) -> str:
    """'BTC' -> 'BTCUSDT'. Already-suffixed input is passed through."""
    coin = (coin or "").strip().upper()
    return coin if coin.endswith("USDT") else coin + "USDT"


def _blank_state() -> dict:
    return {"state": STATE_ARMED, "pending_since": None,
            "notified_at": None, "direction": None}


# ── Persistence ───────────────────────────────────────────────────────────────


def _migrate_entry(cfg: dict) -> dict:
    """Upgrade a v1 coin entry (flat `notified` map) to the v2 state machine.

    A level that v1 had already alerted on becomes `cooling` rather than
    `armed`, so upgrading cannot produce a burst of repeat alerts for levels
    price happens to be sitting near at deploy time. It must clear the
    hysteresis distance first, exactly as a freshly fired level would.
    """
    if "notified" not in cfg:
        return cfg

    legacy = cfg.pop("notified") or {}
    state  = cfg.get("level_state") or {}
    for key, old in legacy.items():
        if key in state:
            continue
        state[key] = {
            "state":         STATE_COOLING,
            "pending_since": None,
            "notified_at":   (old or {}).get("last_notified"),
            "direction":     None,
        }
    cfg["level_state"] = state
    return cfg


def _read_file() -> dict:
    """Load the levels file. A missing file is an empty watch list; a corrupt
    one is treated the same way rather than crashing the scheduler job."""
    try:
        with open(LEVELS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️  Level watcher: could not read {LEVELS_PATH} — {exc}")
        return {}

    return {coin: _migrate_entry(cfg) for coin, cfg in data.items()
            if isinstance(cfg, dict)}


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
    """Replace a coin's level list and alert_pct.

    Per-level state for levels that survive the edit is preserved, so adding
    one level to a list does not re-arm (and re-alert) the others. `last_price`
    is kept too, so an edit does not cost the next check its direction read.
    """
    coin = (coin or "").strip().upper()
    if not coin:
        raise ValueError("coin is required")

    clean, pct = validate(levels, alert_pct)
    keep = {level_key(v) for v in clean}

    with _LOCK:
        data = _read_file()
        cfg  = data.get(coin) or {}
        prev_state = cfg.get("level_state") or {}
        data[coin] = {
            "levels":       clean,
            "alert_pct":    pct,
            "last_price":   cfg.get("last_price"),
            "last_checked": cfg.get("last_checked"),
            "level_state":  {k: v for k, v in prev_state.items() if k in keep},
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
                  gap_pct: float, direction: str) -> str:
    """Alert body. Plain text — no Markdown emphasis, so a level like
    1_000.5 or a coin with an underscore cannot break Telegram parsing."""
    if direction == DIR_RESISTANCE:
        emoji, label, motion = "🔴", "RESISTANCE", "rising"
    else:
        emoji, label, motion = "🟢", "SUPPORT", "falling"
    return (
        f"{emoji} {coin} approaching {label}\n"
        f"Level: {level:,.8g}\n"
        f"Current: {price:,.8g} ({gap_pct:.2f}% away, {motion})"
    )


def _approach(price: float, prev_price: Optional[float], level: float) -> Optional[str]:
    """Directional label when price is moving toward `level`, else None.

    Returns DIR_RESISTANCE for a rising price under the level, DIR_SUPPORT for
    a falling price over it. None means "do not alert": no previous price to
    compare against (the first check for a coin), a flat price, or a price
    moving away from this level.
    """
    if prev_price is None or prev_price <= 0 or price == prev_price:
        return None
    rising = price > prev_price
    if level > price:
        return DIR_RESISTANCE if rising else None
    if level < price:
        return DIR_SUPPORT if not rising else None
    # price sits exactly on the level — momentum decides the label
    return DIR_RESISTANCE if rising else DIR_SUPPORT


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
        cfg = _read_file().get(coin) or {}
    levels = cfg.get("levels") or []
    if not levels:
        return {"coin": coin, "status": "no_levels", "alerts": []}

    alert_pct  = cfg.get("alert_pct") or DEFAULT_ALERT_PCT
    rearm_pct  = alert_pct * REARM_DISTANCE_MULTIPLIER
    prev_price = cfg.get("last_price")
    try:
        prev_price = float(prev_price) if prev_price is not None else None
    except (TypeError, ValueError):
        prev_price = None

    price = price_fn(symbol_for(coin))
    if price is None or price <= 0:
        return {"coin": coin, "status": "no_price", "alerts": []}

    state_map: dict[str, dict] = dict(cfg.get("level_state") or {})
    fired: list[dict] = []

    # ── Pass 1: housekeeping for every level ─────────────────────────────────
    # Runs for all levels, not just the nearest, because a cooling level is by
    # definition far from price and would otherwise never get the chance to
    # re-arm. `candidates` collects what pass 2 may act on.
    candidates: list[tuple[float, float, str, str]] = []  # (gap, level, key, direction)

    for raw in levels:
        try:
            level = float(raw)
        except (TypeError, ValueError):
            continue
        if level <= 0:
            continue

        key     = level_key(level)
        gap_pct = abs(price - level) / level * 100
        entry   = dict(state_map.get(key) or _blank_state())
        state   = entry.get("state") or STATE_ARMED

        if state in (STATE_NOTIFIED, STATE_COOLING):
            if gap_pct >= rearm_pct:
                # Cleared the hysteresis distance — eligible again.
                entry.update(state=STATE_ARMED, pending_since=None, direction=None)
            elif state == STATE_NOTIFIED:
                # The alert went out on the previous check; it is now simply
                # waiting out the distance.
                entry["state"] = STATE_COOLING
            state_map[key] = entry
            continue

        if state == STATE_PENDING and gap_pct > alert_pct:
            # Did not confirm — the approach stalled or reversed out of band.
            entry.update(state=STATE_ARMED, pending_since=None, direction=None)
            state_map[key] = entry
            continue

        state_map[key] = entry

        if gap_pct <= alert_pct:
            direction = _approach(price, prev_price, level)
            if direction:
                candidates.append((gap_pct, level, key, direction))

    # ── Pass 2: advance only the nearest qualifying level ────────────────────
    if candidates:
        gap_pct, level, key, direction = min(candidates, key=lambda c: c[0])
        entry = dict(state_map[key])

        if (entry.get("state") or STATE_ARMED) == STATE_ARMED:
            # First confirming check — needs one more before it fires.
            entry.update(state=STATE_PENDING, pending_since=_now_iso(),
                         direction=direction)
            state_map[key] = entry
        elif notify(format_alert(coin, level, price, gap_pct, direction)):
            entry.update(state=STATE_NOTIFIED, pending_since=None,
                         notified_at=_now_iso(), direction=direction)
            state_map[key] = entry
            fired.append({"level": level, "gap_pct": round(gap_pct, 2),
                          "direction": direction})
        else:
            # Delivery failed (or Telegram is unconfigured) — stay pending so
            # the next check retries instead of silently dropping the alert.
            print(f"⚠️  Level watcher: {coin} alert for level {key} not delivered")

    # ── Persist ──────────────────────────────────────────────────────────────
    # Re-read under the lock so a concurrent POST /api/levels/<coin> is not
    # clobbered: only levels that still exist have their state written back.
    with _LOCK:
        data  = _read_file()
        entry = data.get(coin)
        if entry is not None:
            live = {level_key(v) for v in (entry.get("levels") or [])}
            entry["level_state"] = {k: v for k, v in state_map.items() if k in live}
            entry["last_price"]   = price
            entry["last_checked"] = _now_iso()
            _write_file(data)

    return {
        "coin":       coin,
        "status":     "ok",
        "price":      price,
        "prev_price": prev_price,
        "alert_pct":  alert_pct,
        "checked":    len(levels),
        "alerts":     fired,
    }
