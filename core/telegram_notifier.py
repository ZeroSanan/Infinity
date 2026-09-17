"""
Telegram Notifier
=================
Thin wrapper over the Telegram Bot API sendMessage endpoint, using plain
`requests` (no telegram SDK dependency).

Configuration comes from two environment variables, matching the .env
pattern used by the other optional integrations:

  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     one numeric chat id, or a comma-separated list

Missing configuration is NOT an error. Exactly like TWELVE_DATA_API_KEY /
FRED_API_KEY being unset in Layer 1, an unconfigured notifier logs a warning
once per call and no-ops, so an install with no Telegram set up still runs
every other part of the dashboard normally.

This module is the single implementation of Telegram delivery; web/app.py's
_telegram_send() delegates here so the tiered signal alerts and the level
alerts cannot drift apart.
"""
from __future__ import annotations

import os

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT = 10  # seconds


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def chat_ids() -> list[str]:
    """TELEGRAM_CHAT_ID may hold a single id or a comma-separated list."""
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def is_configured() -> bool:
    """True when both a bot token and at least one chat id are present."""
    return bool(_token() and chat_ids())


def send_telegram_message(text: str, parse_mode: str | None = "Markdown") -> bool:
    """Send `text` to every configured chat id.

    Returns True if at least one chat accepted the message, False if the
    notifier is unconfigured or every send failed. Never raises — a dead
    Telegram API must not take down a scheduler job.
    """
    import requests as _req

    token = _token()
    ids   = chat_ids()
    if not token or not ids:
        print("⚠️  Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping notification")
        return False

    delivered = False
    for chat_id in ids:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = _req.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json=payload, timeout=SEND_TIMEOUT,
            )
            if resp.ok:
                delivered = True
            else:
                print(f"⚠️  Telegram: send to {chat_id} failed {resp.status_code} — {resp.text[:200]}")
        except Exception as exc:
            print(f"⚠️  Telegram: send to {chat_id} error — {exc}")

    return delivered
