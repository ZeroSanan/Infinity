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
      "last_checked": "2026-10-14T08:00:00Z",
      "level_state": {
        "76321.68": {
          "state": "cooling",
          "pending_since": null,
          "notified_at": "2026-10-13T08:20:00Z",
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


Direction is decided once, at pending time
------------------------------------------
The label is fixed by GEOMETRY the moment a level goes `pending` — a level
above the price is RESISTANCE, a level below it is SUPPORT — and stored in
`level_state`. The confirming check reuses that stored label verbatim and
never recomputes it.

This is deliberate. An earlier version re-derived the label at send time from
(price vs last_price), so a price that crossed the level between the pending
check and the confirming check flipped the label: a level approached as
resistance, popped through, then ticking down, read as "falling toward a level
below" and sent SUPPORT. The stored label cannot flip.

A crossing is not an approach, so it is not reported as one. Before sending,
the confirming check verifies price is still on the side it was on at pending
time — using the interval's high/low, not just the last price, so a wick
through the level counts. If it crossed, a distinct "broke through" message is
sent instead and the level moves to `notified` (then `cooling`). Silence would
be the worst outcome: the level that just broke is the one that matters most.


Noise control
-------------
Five independent rules gate every approach alert.

1. DIRECTION + MINIMUM MOVE — price must have moved *toward* the level by at
   least MIN_MOVE_PCT since the previous check. A smaller move counts as no
   direction at all, so tick-to-tick jitter cannot qualify a level.

2. CONFIRMATION — a level must qualify on two consecutive checks before it
   fires (`armed -> pending -> notified`). If a pending level's gap widens
   beyond `alert_pct`, pending is dropped and confirmation starts over.

3. CROSS VALIDATION — see above. An approach that became a break is reported
   as a break.

4. NEAREST ONLY — at most one message per coin per check. A break outranks an
   approach; among equals the closest wins. The rest keep their state and can
   fire on a later check.

5. HYSTERESIS RE-ARM — after firing, a level cannot fire again until price has
   moved at least `REARM_DISTANCE_MULTIPLIER x alert_pct` away from it and
   then re-entered the band.


Per-level state machine
-----------------------
    armed ──(in band, approaching)──> pending ──(confirmed again)──> notified
      ^                                  │                              │
      │                                  │ (gap > alert_pct)            │ (next check)
      │                                  v                              v
      └────────(gap >= 2 x alert_pct)── armed <───────────────────── cooling

`armed` is the implicit default: a level with no stored state is armed.
A pending level that crossed also lands in `notified` — via the break message.


Price source
------------
The caller injects every live reading, and must source them all from the SAME
endpoint (web/app.py uses Binance's spot ticker exclusively, batched). Mixing
sources — the dashboard's Layer 3 cache holds a rounded 4h-kline close up to
120 s old — makes the price/last_price comparison meaningless on its own.

This module never imports web.app (app.py imports core.*, so the reverse would
be circular). See the `price_fn` and `window_fn` arguments on check_levels() —
the same dependency-injection shape SignalEvaluator uses for its recorder.

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

# Minimum move toward a level, as a % of the previous check's price, for the
# move to count as a direction at all. Anything smaller is jitter.
MIN_MOVE_PCT = float(os.getenv("LEVEL_MIN_MOVE_PCT", "0.1"))

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


def _num(value) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _log_transition(coin: str, key: str, old: str, new: str, price: float,
                     prev_price: Optional[float], direction: Optional[str],
                     gap_pct: float, note: str = "") -> None:
    """One line per state transition, so any wrong alert can be traced back to
    the exact prices and gap that produced it."""
    prev = f"{prev_price:,.8g}" if prev_price else "—"
    print(f"🔎 Level watcher: {coin} level={key} {old}->{new} "
          f"price={price:,.8g} last_price={prev} "
          f"direction={direction or '—'} gap={gap_pct:.2f}%"
          + (f" ({note})" if note else ""))


# ── Persistence ───────────────────────────────────────────────────────────────


def _migrate_entry(cfg: dict) -> dict:
    """Upgrade a v1 coin entry (flat `notified` map) to the state machine.

    A level that v1 had already alerted on becomes `cooling` rather than
    `armed`, so upgrading cannot produce a burst of repeat alerts for levels
    price happens to be sitting near at deploy time.
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
        val = _num(raw)
        if val is None:
            raise ValueError(f"invalid level: {raw!r}")
        if val <= 0:
            raise ValueError(f"levels must be positive numbers, got {raw!r}")
        clean.append(val)

    if alert_pct is None:
        pct = DEFAULT_ALERT_PCT
    else:
        pct = _num(alert_pct)
        if pct is None:
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


# ── Pure decision helpers ─────────────────────────────────────────────────────


def direction_at_pending(price: float, level: float,
                          prev_price: Optional[float] = None) -> str:
    """The label, fixed by geometry at the moment a level goes pending.

    Level above the price is resistance; level below it is support. Price
    sitting exactly on the level is the only ambiguous case, and there the
    move that brought it there decides.
    """
    if level > price:
        return DIR_RESISTANCE
    if level < price:
        return DIR_SUPPORT
    if prev_price is not None and prev_price > price:
        return DIR_SUPPORT
    return DIR_RESISTANCE


def moved_toward(price: float, prev_price: Optional[float], level: float,
                  min_move_pct: float = MIN_MOVE_PCT) -> bool:
    """True when price has moved toward `level` by at least `min_move_pct`
    since the previous check.

    Replaces the old sign-only test: any non-zero difference used to count as
    a direction, so tick-to-tick jitter qualified levels.
    """
    if prev_price is None or prev_price <= 0:
        return False
    move_pct = (price - prev_price) / prev_price * 100
    if abs(move_pct) < min_move_pct:
        return False
    if level > price:
        return move_pct > 0
    if level < price:
        return move_pct < 0
    return True  # already at the level


def crossed(level: float, direction: Optional[str], price: float,
             window: Optional[tuple] = None) -> bool:
    """True when price has reached or passed `level` since it went pending.

    `direction` is the stored label: `resistance` means price was below the
    level at pending time, `support` means it was above. `window` is the
    (low, high) of the interval since the last check — a wick that touched the
    level counts even if the last price is back on the original side, which is
    the case the last-price-only test used to miss entirely.
    """
    if direction == DIR_RESISTANCE and price >= level:
        return True
    if direction == DIR_SUPPORT and price <= level:
        return True

    if window:
        low, high = window
        low, high = _num(low), _num(high)
        if low is not None and high is not None and low <= level <= high:
            return True
    return False


# ── Message formatting ────────────────────────────────────────────────────────


def format_alert(coin: str, level: float, price: float,
                  gap_pct: float, direction: str) -> str:
    """Approach alert. Plain text — no Markdown emphasis, so a level like
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


def format_break_alert(coin: str, level: float, price: float,
                        direction: str) -> str:
    """Sent instead of an approach alert when price reached or passed the
    level between the pending check and the confirming one."""
    return (
        f"🟠 {coin} broke through {level:,.8g} (was {direction})\n"
        f"Current: {price:,.8g}"
    )


# ── Main check ────────────────────────────────────────────────────────────────


def check_levels(coin: str, price_fn: Callable[[str], Optional[float]],
                  notify_fn: Optional[Callable[[str], bool]] = None,
                  window_fn: Optional[Callable[[str], Optional[tuple]]] = None) -> dict:
    """Check one coin's levels against its live price and alert as needed.

    `price_fn` and `window_fn` are injected by the caller (web/app.py passes
    readers backed solely by Binance's spot ticker and 1m klines) so this
    module stays free of Flask and of circular imports. `notify_fn` defaults
    to Telegram and exists as a seam for testing.

    `window_fn(symbol)` returns the (low, high) covering the interval since the
    last check, and is called lazily — only when a pending level is about to be
    confirmed — so a quiet watch list costs no kline requests.

    Returns a summary dict; raises only if price_fn raises, which the
    scheduler job catches per coin.
    """
    notify = notify_fn or send_telegram_message
    coin   = (coin or "").strip().upper()
    symbol = symbol_for(coin)

    with _LOCK:
        cfg = _read_file().get(coin) or {}
    levels = cfg.get("levels") or []
    if not levels:
        return {"coin": coin, "status": "no_levels", "alerts": []}

    alert_pct  = cfg.get("alert_pct") or DEFAULT_ALERT_PCT
    rearm_pct  = alert_pct * REARM_DISTANCE_MULTIPLIER
    prev_price = _num(cfg.get("last_price"))

    price = price_fn(symbol)
    if price is None or price <= 0:
        return {"coin": coin, "status": "no_price", "alerts": []}

    state_map: dict[str, dict] = dict(cfg.get("level_state") or {})
    fired: list[dict] = []

    # window_fn is consulted at most once per check, and only if needed.
    _window: list = []  # [] = not fetched yet, [value] = fetched

    def window():
        if not _window:
            try:
                _window.append(window_fn(symbol) if window_fn else None)
            except Exception as exc:
                print(f"⚠️  Level watcher: {coin} range fetch failed — {exc}")
                _window.append(None)
        return _window[0]

    # ── Pass 1: housekeeping for every level ─────────────────────────────────
    # Runs for all levels, not just the nearest, because a cooling level is by
    # definition far from price and would otherwise never get the chance to
    # re-arm.
    breaks:     list[tuple] = []  # (gap, level, key, stored_direction)
    approaches: list[tuple] = []  # (gap, level, key)

    for raw in levels:
        level = _num(raw)
        if level is None or level <= 0:
            continue

        key     = level_key(level)
        gap_pct = abs(price - level) / level * 100
        entry   = dict(state_map.get(key) or _blank_state())
        state   = entry.get("state") or STATE_ARMED

        if state in (STATE_NOTIFIED, STATE_COOLING):
            if gap_pct >= rearm_pct:
                entry.update(state=STATE_ARMED, pending_since=None, direction=None)
                _log_transition(coin, key, state, STATE_ARMED, price, prev_price,
                                None, gap_pct, "cleared re-arm distance")
            elif state == STATE_NOTIFIED:
                entry["state"] = STATE_COOLING
                _log_transition(coin, key, STATE_NOTIFIED, STATE_COOLING, price,
                                prev_price, entry.get("direction"), gap_pct)
            state_map[key] = entry
            continue

        if state == STATE_PENDING:
            stored = entry.get("direction")
            # A pending level that crossed is resolved as a break, whatever the
            # gap now reads — checked before the gap test, because crossing
            # leaves the gap small and would otherwise look like a live approach.
            if stored and crossed(level, stored, price, window()):
                breaks.append((gap_pct, level, key, stored))
                state_map[key] = entry
                continue
            if gap_pct > alert_pct:
                entry.update(state=STATE_ARMED, pending_since=None, direction=None)
                _log_transition(coin, key, STATE_PENDING, STATE_ARMED, price,
                                prev_price, stored, gap_pct, "did not confirm")
                state_map[key] = entry
                continue

        state_map[key] = entry

        if gap_pct <= alert_pct and moved_toward(price, prev_price, level):
            approaches.append((gap_pct, level, key))

    # ── Pass 2: at most one message per coin per check ───────────────────────
    # A break outranks an approach — it is the more urgent news, and the level
    # it concerns was already committed to by a pending state.
    if breaks:
        gap_pct, level, key, stored = min(breaks, key=lambda c: c[0])
        entry = dict(state_map[key])
        if notify(format_break_alert(coin, level, price, stored)):
            entry.update(state=STATE_NOTIFIED, pending_since=None,
                         notified_at=_now_iso(), direction=stored)
            state_map[key] = entry
            fired.append({"level": level, "gap_pct": round(gap_pct, 2),
                          "direction": stored, "event": "break"})
            _log_transition(coin, key, STATE_PENDING, STATE_NOTIFIED, price,
                            prev_price, stored, gap_pct, "broke through")
        else:
            print(f"⚠️  Level watcher: {coin} break alert for level {key} not delivered")

    elif approaches:
        gap_pct, level, key = min(approaches, key=lambda c: c[0])
        entry = dict(state_map[key])
        state = entry.get("state") or STATE_ARMED

        if state == STATE_ARMED:
            # First confirming check. The label is fixed here and never recomputed.
            direction = direction_at_pending(price, level, prev_price)
            entry.update(state=STATE_PENDING, pending_since=_now_iso(),
                         direction=direction)
            state_map[key] = entry
            _log_transition(coin, key, STATE_ARMED, STATE_PENDING, price,
                            prev_price, direction, gap_pct)
        elif not entry.get("direction"):
            # Pre-upgrade entry with no stored label: set it now and take one
            # more check to confirm, rather than guessing at send time.
            direction = direction_at_pending(price, level, prev_price)
            entry["direction"] = direction
            state_map[key] = entry
            _log_transition(coin, key, STATE_PENDING, STATE_PENDING, price,
                            prev_price, direction, gap_pct, "label backfilled")
        else:
            direction = entry["direction"]  # stored at pending time, never recomputed
            if notify(format_alert(coin, level, price, gap_pct, direction)):
                entry.update(state=STATE_NOTIFIED, pending_since=None,
                             notified_at=_now_iso(), direction=direction)
                state_map[key] = entry
                fired.append({"level": level, "gap_pct": round(gap_pct, 2),
                              "direction": direction, "event": "approach"})
                _log_transition(coin, key, STATE_PENDING, STATE_NOTIFIED, price,
                                prev_price, direction, gap_pct, "approach alert")
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
