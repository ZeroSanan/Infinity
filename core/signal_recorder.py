"""
Signal History Recorder
========================
Persists periodic snapshots of the Market Signals Layer 1/2/3 indicators to
the SQLite database (data/infinity.db), signal_history table.

Save cadence (driven by the APScheduler jobs in web/app.py):
  - Layer 1: every 6 hours, one shared global entry (macro moves slowly)
  - Layer 2: every 1 hour, per coin (funding/OI/long-short refresh hourly
    on Binance, so saving more often adds no information)
  - Layer 3: once per day at UTC 00:00, per coin (a daily structural
    summary only — order book/volume snapshots a few hours old are noise)

Standalone and side-effect-free beyond its own history table: each record_*
method is handed the already-fetched live response dict for that layer and
just extracts/persists fields from it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core import db as _db


class SignalRecorder:
    RETENTION_DAYS = 90

    def __init__(self, history_file: Optional[str] = None):
        # history_file argument kept for backward compatibility — ignored
        _db.init_db()

    # ── Recording ────────────────────────────────────────────────────────────

    def record_layer1(self, layer1_data: dict) -> None:
        """Called every 6 hours. Extracts fields from the live /api/layer1
        response dict and appends one global snapshot to signal_history."""
        def field(key, attr):
            return (layer1_data.get(key) or {}).get(attr)

        indicator_keys = ("fear_greed", "btc_dominance", "dxy", "fed_funds",
                           "yield_10y", "cpi", "vix")
        signals = [field(k, "signal") for k in indicator_keys if field(k, "status") == "ok"]

        snapshot = {
            "ts": _now_iso(),
            "layer": 1,
            "fear_greed_value": field("fear_greed", "value"),
            "fear_greed_label": field("fear_greed", "label"),
            "btc_dominance": field("btc_dominance", "value"),
            "btc_dominance_signal": field("btc_dominance", "signal"),
            "dxy_value": field("dxy", "value"),
            "dxy_signal": field("dxy", "signal"),
            "vix_value": field("vix", "value"),
            "vix_signal": field("vix", "signal"),
            "fed_funds_rate": field("fed_funds", "value"),
            "fed_funds_signal": field("fed_funds", "signal"),
            "us_10y_yield": field("yield_10y", "value"),
            "us_10y_signal": field("yield_10y", "signal"),
            "cpi_yoy": field("cpi", "value"),
            "cpi_signal": field("cpi", "signal"),
            "verdict": (layer1_data.get("verdict") or {}).get("code"),
            "signals_counted": len(signals),
            "bullish_count": sum(1 for s in signals if s and s > 0),
            "bearish_count": sum(1 for s in signals if s and s < 0),
        }
        _db.signal_history_insert(None, 1, snapshot)
        _db.signal_history_cleanup(self.RETENTION_DAYS)

    def record_layer2(self, symbol: str, layer2_data: dict, master_summary: Optional[str],
                      mechanics_data: Optional[dict] = None) -> None:
        """Called every 1 hour per symbol."""
        funding  = layer2_data.get("funding") or {}
        oi       = layer2_data.get("open_interest") or {}
        ls       = layer2_data.get("long_short") or {}
        glob     = ls.get("global") or {}
        top      = ls.get("top") or {}
        position = layer2_data.get("position_ratio") or {}
        mech     = mechanics_data or {}
        taker    = mech.get("taker") or {}
        vol      = mech.get("volume_ratio") or {}

        snapshot = {
            "ts": _now_iso(),
            "coin": symbol[:-4] if symbol.endswith("USDT") else symbol,
            "symbol": symbol,
            "layer": 2,
            "funding_rate": funding.get("current"),
            "oi_usd": oi.get("current_usd"),
            "oi_change_24h_pct": oi.get("change_24h_pct"),
            "oi_label": oi.get("label"),
            "global_long_pct": glob.get("long_pct"),
            "global_short_pct": glob.get("short_pct"),
            "top_trader_long_pct": top.get("long_pct"),
            "top_trader_short_pct": top.get("short_pct"),
            "divergence": bool(ls.get("divergence")),
            "position_long_pct": position.get("long_pct"),
            "position_short_pct": position.get("short_pct"),
            "position_account_gap": position.get("divergence_from_account"),
            "position_divergence_direction": position.get("divergence_direction"),
            "position_divergence_significance": position.get("significance"),
            "taker_buy_pct": taker.get("buy_pct") if taker.get("status") == "ok" else None,
            "taker_sell_pct": taker.get("sell_pct") if taker.get("status") == "ok" else None,
            "taker_label": taker.get("label") if taker.get("status") == "ok" else None,
            "spot_volume_24h": vol.get("spot_usd") if vol.get("status") == "ok" else None,
            "futures_volume_24h": vol.get("futures_usd") if vol.get("status") == "ok" else None,
            "spot_futures_ratio": vol.get("ratio") if vol.get("status") == "ok" else None,
            "volume_label": vol.get("label") if vol.get("status") == "ok" else None,
            "verdict": (layer2_data.get("verdict") or {}).get("code"),
            "master_summary": master_summary,
        }
        _db.signal_history_insert(symbol, 2, snapshot)

    def record_layer3(self, symbol: str, layer3_data: dict) -> None:
        """Called once per day at UTC 00:00 per symbol."""
        vol       = layer3_data.get("volume_divergence") or {}
        structure = layer3_data.get("price_structure") or {}
        momentum  = layer3_data.get("momentum") or {}
        ob        = layer3_data.get("order_book") or {}
        atr       = layer3_data.get("atr") or {}
        verdict   = layer3_data.get("verdict") or {}

        total_score = None
        if "bullish" in verdict and "bearish" in verdict:
            total_score = verdict["bullish"] - verdict["bearish"]

        snapshot = {
            "ts": _now_iso(),
            "coin": symbol[:-4] if symbol.endswith("USDT") else symbol,
            "symbol": symbol,
            "layer": 3,
            "volume_divergence_signal": vol.get("signal"),
            "volume_divergence_label": vol.get("label"),
            "price_structure_signal": structure.get("signal"),
            "price_structure_label": structure.get("label"),
            "momentum_signal": momentum.get("signal"),
            "momentum_label": momentum.get("label"),
            "order_book_signal": ob.get("signal"),
            "order_book_label": ob.get("label"),
            "atr_pct": atr.get("atr_pct"),
            "atr_label": atr.get("label"),
            "total_score": total_score,
            "verdict": verdict.get("code"),
        }
        _db.signal_history_insert(symbol, 3, snapshot)

    # ── Reading ──────────────────────────────────────────────────────────────

    def get_history(self, symbol: str, days: int = 7) -> dict:
        """Returns the last `days` worth of snapshots for all three layers."""
        return {
            "layer1": _db.signal_history_get(None, 1, days),
            "layer2": _db.signal_history_get(symbol, 2, days),
            "layer3": _db.signal_history_get(symbol, 3, days),
        }

    def get_recent_layer2(self, symbol: str, n: int) -> list:
        """Return the last n Layer 2 snapshots for OI trend checks."""
        return _db.signal_history_recent_layer2(symbol, n)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
