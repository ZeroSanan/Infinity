"""
Telegram Notifier — alerts for actionable Market Signals (LONG NOW / SHORT NOW).

Setup:
  1. Create a bot via @BotFather on Telegram and copy its token.
  2. Send any message to the bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates
     to find your numeric chat id.
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
     (TELEGRAM_CHAT_ID accepts a comma-separated list to notify multiple chats)
"""

from __future__ import annotations

import html
import json
import os

import requests

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_notify_state.json")

# Maps an actionable `action` value to the verb/emoji used in the alert.
_ACTIONABLE = {
    "LONG NOW":  ("BUY",  "\U0001F7E2"),   # green circle
    "SHORT NOW": ("SELL", "\U0001F534"),   # red circle
}


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _chat_ids() -> list[str]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def is_configured() -> bool:
    return bool(_bot_token() and _chat_ids())


def send_message(text: str) -> bool:
    """Send an HTML-formatted message to every configured Telegram chat.

    Returns True if it was delivered to at least one chat.
    """
    token, chat_ids = _bot_token(), _chat_ids()
    if not (token and chat_ids):
        return False
    ok = False
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
            ok = ok or resp.ok
        except Exception:
            pass
    return ok


def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _fmt_price(coin: str, price) -> str:
    if price is None:
        return "—"
    dec = 4 if (coin == "XRP" or price < 10) else 2
    return f"${price:,.{dec}f}"


def notify_actionable_signals(signals: list) -> None:
    """Send a Telegram alert when a coin newly enters LONG NOW or SHORT NOW.

    Last-notified action per coin is persisted to STATE_PATH so an alert
    only fires on the *transition* into an actionable state — not on every
    poll while the same signal remains active.
    """
    if not is_configured():
        return

    state = _load_state()
    changed = False

    for s in signals:
        coin = s.get("coin")
        if not coin:
            continue
        action = None if s.get("error") else s.get("action")

        if action not in _ACTIONABLE:
            if state.get(coin) != action:
                state[coin] = action
                changed = True
            continue

        if state.get(coin) == action:
            continue

        verb, emoji = _ACTIONABLE[action]
        entry = s.get("ml_entry") or {}

        text = (
            f"{emoji} <b>{verb} SIGNAL — {html.escape(coin)}</b>\n"
            f"Price: {_fmt_price(coin, s.get('price'))}\n"
            f"Regime: {html.escape(str(s.get('regime')))} ({s.get('score')}/4) | RSI: {s.get('rsi')}\n"
            f"Entry grade: {entry.get('grade', '—')} ({entry.get('score', '—')}/100)\n"
            f"{html.escape(s.get('condition', ''))}"
        )

        if send_message(text):
            state[coin] = action
            changed = True

    if changed:
        _save_state(state)
