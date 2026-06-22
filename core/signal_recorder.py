"""
Signal History Recorder
========================
Persists periodic snapshots of the Market Signals Layer 1/2/3 indicators to
data/signal_history.json, so the dashboard can show how conditions have been
trending over the last 1-3 weeks instead of only the current snapshot.

Save cadence (driven by the APScheduler jobs in web/app.py):
  - Layer 1: every 6 hours, one shared global entry (macro moves slowly)
  - Layer 2: every 1 hour, per coin (funding/OI/long-short refresh hourly
    on Binance, so saving more often adds no information)
  - Layer 3: once per day at UTC 00:00, per coin (a daily structural
    summary only — order book/volume snapshots a few hours old are noise)

Standalone and side-effect-free beyond its own history file: each record_*
method is handed the already-fetched live response dict for that layer and
just extracts/persists fields from it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional


class SignalRecorder:
    HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "signal_history.json")
    RETENTION_DAYS = 90

    def __init__(self, history_file: Optional[str] = None):
        self.history_file = history_file or self.HISTORY_FILE

    # ── Recording ────────────────────────────────────────────────────────────

    def record_layer1(self, layer1_data: dict) -> None:
        """Called every 6 hours. Extracts fields from the live /api/layer1
        response dict and appends one global snapshot to history["layer1"]."""
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

        data = self._load()
        data["layer1"].append(snapshot)
        data["layer1"] = self._trim(data["layer1"])
        self._save(data)

    def record_layer2(self, symbol: str, layer2_data: dict, master_summary: Optional[str]) -> None:
        """Called every 1 hour per symbol. Extracts fields from the live
        /api/layer2/<symbol> response dict and appends to
        history["layer2"][symbol]."""
        funding = layer2_data.get("funding") or {}
        oi      = layer2_data.get("open_interest") or {}
        ls      = layer2_data.get("long_short") or {}
        glob    = ls.get("global") or {}
        top     = ls.get("top") or {}

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
            "verdict": (layer2_data.get("verdict") or {}).get("code"),
            "master_summary": master_summary,
        }

        data = self._load()
        series = data["layer2"].setdefault(symbol, [])
        series.append(snapshot)
        data["layer2"][symbol] = self._trim(series)
        self._save(data)

    def record_layer3(self, symbol: str, layer3_data: dict) -> None:
        """Called once per day at UTC 00:00 per symbol. Extracts fields from
        the live /api/layer3/<symbol> response dict and appends to
        history["layer3"][symbol]."""
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

        data = self._load()
        series = data["layer3"].setdefault(symbol, [])
        series.append(snapshot)
        data["layer3"][symbol] = self._trim(series)
        self._save(data)

    # ── Reading ──────────────────────────────────────────────────────────────

    def get_history(self, symbol: str, days: int = 7) -> dict:
        """Returns the last `days` worth of snapshots for all three layers
        for the given symbol. Layer 1 is global (not symbol-specific) but
        included in the response for completeness."""
        data = self._load()
        cutoff = _now_iso_minus(days)
        return {
            "layer1": [s for s in data["layer1"] if s.get("ts", "") >= cutoff],
            "layer2": [s for s in data["layer2"].get(symbol, []) if s.get("ts", "") >= cutoff],
            "layer3": [s for s in data["layer3"].get(symbol, []) if s.get("ts", "") >= cutoff],
        }

    # ── Storage ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load history file, return empty structure if not found."""
        if not os.path.exists(self.history_file):
            return {"layer1": [], "layer2": {}, "layer3": {}}
        try:
            with open(self.history_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"layer1": [], "layer2": {}, "layer3": {}}
        data.setdefault("layer1", [])
        data.setdefault("layer2", {})
        data.setdefault("layer3", {})
        return data

    def _save(self, data: dict) -> None:
        """Atomic write: write to .tmp then rename."""
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        tmp_path = self.history_file + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.history_file)

    def _trim(self, entries: list) -> list:
        """Remove entries older than RETENTION_DAYS."""
        cutoff = _now_iso_minus(self.RETENTION_DAYS)
        return [e for e in entries if e.get("ts", "") >= cutoff]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso_minus(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
