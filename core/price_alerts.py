"""
Price Alerts
============
The user names a specific price for a coin and a band around it (default 1%).
When price comes within the band, one Telegram message goes out and the alert
is done.

Storage: SQLite, the `price_alerts` table in data/infinity.db (see core/db.py).
No JSON file — this feature has no reason to repeat the level watcher's
flat-file exception.

Separate from the Resistance/Support Level Watcher (core/level_watcher.py) and
deliberately simpler:

  | | Level alerts | Price alerts |
  |-| ------------ | ------------ |
  | Levels come from | a per-coin watch list | one row per target |
  | Confirmation | two consecutive checks | **none** |
  | Label | geometry (resistance/support) | plain below/above target |
  | After firing | hysteresis re-arm | one-shot, status='triggered' |

There is **no two-check confirmation** here, and that is the point: the user
picked this exact price on purpose, so a minute of delay defeats the feature.
The level watcher confirms because its job is to filter an unsolicited stream;
a price alert is a standing request.

This module makes no HTTP calls. check_price_alerts() is handed prices and
candle windows the scheduler already fetched, which keeps it cheap to unit
test and keeps the whole tick down to one batched price request.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Optional

from core import db as _db
from core.telegram_notifier import send_telegram_message

# Band used when the caller does not supply one.
DEFAULT_ALERT_PCT = float(os.getenv("PRICE_ALERT_DEFAULT_PCT", "1.0"))

# Accepted range for alert_pct on write (mirrors the level watcher's bounds
# and the UI input).
MIN_ALERT_PCT = 0.1
MAX_ALERT_PCT = 20.0

STATUS_ACTIVE    = "active"
STATUS_TRIGGERED = "triggered"
STATUS_CANCELLED = "cancelled"
STATUSES         = (STATUS_ACTIVE, STATUS_TRIGGERED, STATUS_CANCELLED)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime(_TS_FORMAT)


def _num(value) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse a stored/incoming timestamp. Accepts the project's
    'YYYY-MM-DDTHH:MM:SSZ' form and ISO-8601 with an offset."""
    if not ts:
        return None
    raw = str(ts).strip()
    try:
        return datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def symbol_for(coin: str) -> str:
    """'BTC' -> 'BTCUSDT'. Already-suffixed input is passed through."""
    coin = (coin or "").strip().upper()
    return coin if coin.endswith("USDT") else coin + "USDT"


# ── Pure helpers ──────────────────────────────────────────────────────────────


def gap_pct(price: float, target: float) -> float:
    """Distance from price to target, as a % of the target."""
    return abs(price - target) / target * 100


def side_label(price: float, target: float) -> str:
    """Wording for the message. Deliberately not support/resistance — this
    feature makes no claim about what the price level means."""
    return "below target" if price < target else "above target"


def touched(target: float, price: float, window: Optional[tuple] = None,
             last_price: Optional[float] = None) -> bool:
    """True when price actually reached the target, not merely neared it.

    Three ways to tell, in order of reliability:
      1. The target lies inside the [low, high] of the candle window since the
         last check — catches a wick that pierced the target and came back,
         which a single end-of-interval sample cannot see.
      2. The sampled price is exactly the target.
      3. The target sits between the previous price and this one, so price
         crossed it between checks. Used when no window is available.
    """
    if window:
        low, high = window
        low, high = _num(low), _num(high)
        if low is not None and high is not None and low <= target <= high:
            return True
    if price == target:
        return True
    prev = _num(last_price)
    if prev is not None and (prev - target) * (price - target) < 0:
        return True
    return False


def validate(coin, target_price, alert_pct=None, note=None,
              notify_on_touch=None, expires_at=None) -> dict:
    """Validate an incoming create/edit payload.

    Raises ValueError with a message meant to be shown to the user verbatim.
    Returns the cleaned field dict.
    """
    clean: dict = {}

    coin = (coin or "").strip().upper()
    if not coin:
        raise ValueError("coin is required")
    if not coin.replace("USDT", "").isalnum():
        raise ValueError(f"invalid coin: {coin!r}")
    clean["coin"] = coin

    if isinstance(target_price, bool):
        raise ValueError("target_price must be a positive number")
    target = _num(target_price)
    if target is None:
        raise ValueError("target_price must be a positive number")
    if target <= 0:
        raise ValueError(f"target_price must be positive, got {target_price!r}")
    clean["target_price"] = target

    if alert_pct is None:
        clean["alert_pct"] = DEFAULT_ALERT_PCT
    else:
        pct = _num(alert_pct)
        if pct is None:
            raise ValueError("alert_pct must be a number")
        if not (MIN_ALERT_PCT <= pct <= MAX_ALERT_PCT):
            raise ValueError(f"alert_pct must be between {MIN_ALERT_PCT} and {MAX_ALERT_PCT}")
        clean["alert_pct"] = pct

    if note is not None:
        text = str(note).strip()
        if len(text) > 280:
            raise ValueError("note must be 280 characters or fewer")
        clean["note"] = text or None

    if notify_on_touch is not None:
        clean["notify_on_touch"] = bool(notify_on_touch)

    if expires_at is not None and str(expires_at).strip():
        parsed = parse_ts(expires_at)
        if parsed is None:
            raise ValueError("expires_at must be an ISO-8601 timestamp, e.g. 2026-10-20T12:00:00Z")
        if parsed <= _now():
            raise ValueError("expires_at must be in the future")
        clean["expires_at"] = parsed.strftime(_TS_FORMAT)

    return clean


# ── Message formatting ────────────────────────────────────────────────────────


def format_approach(coin: str, target: float, price: float,
                     gap: float, note: Optional[str] = None) -> str:
    """Plain text — no Markdown emphasis, so a note containing * or _ cannot
    break Telegram parsing."""
    lines = [
        f"🔔 {coin} price alert",
        f"Target: {target:,.8g}",
        f"Current: {price:,.8g} ({gap:.2f}% away, {side_label(price, target)})",
    ]
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def format_touch(coin: str, target: float, price: float) -> str:
    return (
        f"✅ {coin} reached {target:,.8g}\n"
        f"Current: {price:,.8g}"
    )


# ── Checker ───────────────────────────────────────────────────────────────────


def check_price_alerts(price_map: dict, window_map: Optional[dict] = None,
                        notify_fn: Optional[Callable[[str], bool]] = None,
                        alerts: Optional[list] = None) -> dict:
    """Evaluate every active alert against prices the caller already fetched.

    `price_map`  : {SYMBOL: price}
    `window_map` : {SYMBOL: (low, high)} for coins with active alerts
    `notify_fn`  : defaults to Telegram; a seam for tests
    `alerts`     : defaults to the active rows in the DB; a seam for tests

    Makes no HTTP calls. Each alert is handled independently — one bad row
    cannot stop the others.

    Returns a summary: counts plus the list of messages that went out.
    """
    notify     = notify_fn or send_telegram_message
    window_map = window_map or {}
    rows       = alerts if alerts is not None else _db.price_alerts_get_all(STATUS_ACTIVE)

    sent: list = []
    expired: list = []
    skipped = 0
    errors  = 0
    now     = _now()

    for row in rows:
        try:
            alert_id = row.get("id")
            coin     = (row.get("coin") or "").upper()
            target   = _num(row.get("target_price"))
            band     = _num(row.get("alert_pct")) or DEFAULT_ALERT_PCT
            if not coin or target is None or target <= 0:
                skipped += 1
                continue

            # Expiry first — an expired alert is cancelled silently.
            expires = parse_ts(row.get("expires_at"))
            if expires is not None and expires <= now:
                _db.price_alerts_update(alert_id, status=STATUS_CANCELLED)
                expired.append({"id": alert_id, "coin": coin, "target": target})
                continue

            symbol = symbol_for(coin)
            price  = _num(price_map.get(symbol))
            if price is None or price <= 0:
                skipped += 1
                continue

            window     = window_map.get(symbol)
            last_price = _num(row.get("last_price"))
            gap        = gap_pct(price, target)
            is_touched = touched(target, price, window, last_price)
            on_touch   = bool(row.get("notify_on_touch"))

            # Stage 1 — approach. Fires on the first qualifying check: within
            # the band, or a wick that already reached the target.
            if not row.get("approach_notified_at"):
                if gap > band and not is_touched:
                    _db.price_alerts_update(alert_id, last_price=price)
                    continue

                text = format_approach(coin, target, price, gap, row.get("note"))
                if not notify(text):
                    # Unconfigured or failed send: leave the row untouched so
                    # the next tick retries rather than losing the alert.
                    print(f"⚠️  Price alerts: {coin} target {target} approach not delivered")
                    continue

                fields = {"approach_notified_at": _now_iso(), "last_price": price}
                if on_touch:
                    # Stay active for the second stage.
                    pass
                else:
                    fields["status"] = STATUS_TRIGGERED
                    fields["triggered_at"] = _now_iso()
                _db.price_alerts_update(alert_id, **fields)
                sent.append({"id": alert_id, "coin": coin, "target": target,
                              "price": price, "gap_pct": round(gap, 2),
                              "stage": "approach"})
                print(f"🎯 Price alert: {coin} {gap:.2f}% from {target:,.8g} "
                      f"({side_label(price, target)})")
                continue

            # Stage 2 — touch, only for notify_on_touch alerts that have
            # already sent their approach message.
            if on_touch and not row.get("touched_notified_at"):
                if not is_touched:
                    _db.price_alerts_update(alert_id, last_price=price)
                    continue
                if not notify(format_touch(coin, target, price)):
                    print(f"⚠️  Price alerts: {coin} target {target} touch not delivered")
                    continue
                _db.price_alerts_update(
                    alert_id, touched_notified_at=_now_iso(), last_price=price,
                    status=STATUS_TRIGGERED, triggered_at=_now_iso())
                sent.append({"id": alert_id, "coin": coin, "target": target,
                              "price": price, "gap_pct": round(gap, 2),
                              "stage": "touch"})
                print(f"🎯 Price alert: {coin} reached {target:,.8g}")
                continue

            # Nothing left to send — close it out.
            _db.price_alerts_update(alert_id, status=STATUS_TRIGGERED,
                                     triggered_at=row.get("triggered_at") or _now_iso(),
                                     last_price=price)

        except Exception as exc:
            errors += 1
            print(f"⚠️  Price alerts: check failed for alert {row.get('id')}: {exc}")

    return {
        "checked": len(rows),
        "sent":    sent,
        "expired": expired,
        "skipped": skipped,
        "errors":  errors,
    }
