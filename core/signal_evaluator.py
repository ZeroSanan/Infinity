"""
Signal Evaluator — Tiered Signal System (Part 1 + Part 2)
==========================================================
Evaluates the current market state for each tracked symbol into one of three
tiers: NONE / DEVELOPING / STRONG, using six independent criteria.

Criteria (checked symmetrically for LONG and SHORT):
  1. Master Summary Bar state
  2. Layer 2 Global Long/Short crowding (contrarian signal)
  3. Position Ratio divergence (significance threshold: "significant")
  4. Open Interest trend (2+ consecutive cycles with matching label)
  5. Market Mechanics (Taker ratio OR Spot/Futures dominance)
  6. Liquidation cluster proximity (configurable threshold, default 3%)

Tier classification:
  STRONG    : 5 or 6 criteria confirm in same direction, Master Summary
              not directly contradicting
  DEVELOPING: 3 or 4 criteria confirm in same direction
  NONE      : 0-2 criteria, or split between directions

State is persisted to the SQLite database (data/infinity.db).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core import db as _db

# kept for import compatibility — callers that import these from signal_evaluator still work
def load_config() -> dict:
    return _db.signal_config_get_all()

def save_config(cfg: dict) -> None:
    _db.signal_config_set_all(cfg)

# DATA_DIR exposed for app.py import compatibility
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAUTUSDT"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── OI trend: read Signal History ────────────────────────────────────────────


def _oi_trend_confirms(symbol: str, direction: str,
                        recorder, consecutive_required: int) -> bool:
    """Check last N consecutive Layer 2 snapshots for matching OI label."""
    try:
        snaps = recorder.get_recent_layer2(symbol, consecutive_required)
    except Exception:
        return False

    if len(snaps) < consecutive_required:
        return False

    recent = snaps[-consecutive_required:]
    if direction == "SHORT":
        target_labels = {"Exhaustion — Longs Closing", "Exhaustion — Low Conviction"}
    else:
        target_labels = {"Strong — New Money Entering"}

    return all(s.get("oi_label") in target_labels for s in recent)


# ── Liquidation proximity ─────────────────────────────────────────────────────


def _liq_proximity_confirms(direction: str, liq_data: Optional[dict],
                              current_price: Optional[float],
                              threshold_pct: float) -> bool:
    if not liq_data or not current_price or current_price <= 0:
        return False
    cluster = liq_data.get("cluster_below") if direction == "SHORT" else liq_data.get("cluster_above")
    if cluster is None:
        return False
    try:
        distance_pct = abs(cluster - current_price) / current_price * 100
        return distance_pct <= threshold_pct
    except (TypeError, ZeroDivisionError):
        return False


# ── Six-criteria evaluation ───────────────────────────────────────────────────


def _evaluate_direction(
    direction: str,
    snapshot: dict,
    l2_data: dict,
    mm_data: Optional[dict],
    recorder,
    liq_data: Optional[dict],
    current_price: Optional[float],
    config: dict,
) -> tuple[int, dict]:
    detail: dict[str, bool] = {}

    # 1. Master Summary Bar
    master    = snapshot.get("master", "")
    l3_verdict = snapshot.get("l3_verdict", "")
    if direction == "SHORT":
        ms_ok = (master == "ALIGNED SHORT" or
                 (master == "CAUTION" and l3_verdict in ("SHORT", "WEAK_SHORT")))
    else:
        ms_ok = (master == "ALIGNED LONG" or
                 (master == "DEVELOPING" and l3_verdict in ("LONG", "WEAK_LONG")))
    detail["master_summary"] = ms_ok

    # 2. Layer 2 Global L/S crowding (contrarian)
    ls_global = (l2_data.get("long_short") or {}).get("global") or {}
    global_long_pct = ls_global.get("long_pct", 50.0) or 50.0
    detail["l2_crowding"] = (global_long_pct > 65.0 if direction == "SHORT"
                              else (100 - global_long_pct) > 65.0)

    # 3. Position Ratio divergence
    pos = l2_data.get("position_ratio") or {}
    if direction == "SHORT":
        detail["position_ratio_divergence"] = (
            pos.get("significance") == "significant" and
            pos.get("divergence_direction") == "shorts_larger_than_headcount"
        )
    else:
        detail["position_ratio_divergence"] = (
            pos.get("significance") == "significant" and
            pos.get("divergence_direction") == "longs_larger_than_headcount"
        )

    # 4. OI trend (reads Signal History)
    consecutive = config.get("oi_consecutive_cycles", 2)
    detail["oi_trend"] = _oi_trend_confirms(
        l2_data.get("symbol", ""), direction, recorder, consecutive)

    # 5. Market Mechanics
    taker   = (mm_data or {}).get("taker") or {}
    vol_rat = (mm_data or {}).get("volume_ratio") or {}
    if direction == "SHORT":
        taker_ok   = taker.get("status") == "ok" and (taker.get("sell_pct") or 0) > 55
        futures_ok = (vol_rat.get("status") == "ok" and
                      "Futures Dominant" in (vol_rat.get("label") or ""))
    else:
        taker_ok   = taker.get("status") == "ok" and (taker.get("buy_pct") or 0) > 55
        futures_ok = (vol_rat.get("status") == "ok" and
                      "Spot Dominant" in (vol_rat.get("label") or ""))
    detail["market_mechanics"] = taker_ok or futures_ok

    # 6. Liquidation proximity
    threshold = config.get("liq_proximity_pct", 3.0)
    detail["liquidation_proximity"] = _liq_proximity_confirms(
        direction, liq_data, current_price, threshold)

    confirmed = sum(1 for v in detail.values() if v)
    return confirmed, detail


def _master_contradicts(direction: str, master: str) -> bool:
    if direction == "SHORT" and master == "ALIGNED LONG":
        return True
    if direction == "LONG" and master == "ALIGNED SHORT":
        return True
    return False


# ── Main evaluator ────────────────────────────────────────────────────────────


class SignalEvaluator:
    def __init__(self, recorder):
        self.recorder = recorder

    def evaluate(
        self,
        symbol: str,
        snapshot: dict,
        l2_data: dict,
        mm_data: Optional[dict],
        liq_data: Optional[dict] = None,
        current_price: Optional[float] = None,
        config: Optional[dict] = None,
    ) -> dict:
        if config is None:
            config = load_config()

        long_count, long_detail = _evaluate_direction(
            "LONG", snapshot, l2_data, mm_data,
            self.recorder, liq_data, current_price, config)
        short_count, short_detail = _evaluate_direction(
            "SHORT", snapshot, l2_data, mm_data,
            self.recorder, liq_data, current_price, config)

        master = snapshot.get("master", "")

        if long_count >= short_count and long_count >= 3:
            direction = "LONG"
            confirmed = long_count
            detail    = long_detail
        elif short_count > long_count and short_count >= 3:
            direction = "SHORT"
            confirmed = short_count
            detail    = short_detail
        else:
            direction = "LONG" if long_count >= short_count else "SHORT"
            confirmed = max(long_count, short_count)
            detail    = long_detail if long_count >= short_count else short_detail

        contradicted = _master_contradicts(direction, master)
        if confirmed >= 5 and not contradicted:
            tier = "STRONG"
        elif confirmed >= 3:
            tier = "DEVELOPING"
        else:
            tier = "NONE"

        if tier == "NONE":
            direction = "NONE"

        return {
            "symbol":             symbol,
            "tier":               tier,
            "direction":          direction,
            "criteria_confirmed": confirmed,
            "criteria_detail":    detail,
            "long_confirmed":     long_count,
            "short_confirmed":    short_count,
            "master_summary":     master,
        }

    def evaluate_and_persist(
        self,
        symbol: str,
        snapshot: dict,
        l2_data: dict,
        mm_data: Optional[dict],
        liq_data: Optional[dict] = None,
        current_price: Optional[float] = None,
        config: Optional[dict] = None,
    ) -> dict:
        if config is None:
            config = load_config()

        result = self.evaluate(symbol, snapshot, l2_data, mm_data,
                                liq_data, current_price, config)

        prev = _db.signal_state_get(symbol)
        prev_tier          = prev.get("tier", "NONE")
        prev_notified_tier = prev.get("last_notified_tier", "NONE")

        now = _now_iso()
        entry_at = prev.get("entered_tier_at", now)
        if result["tier"] != prev_tier:
            entry_at = now

        _db.signal_state_upsert(symbol, {
            "tier":               result["tier"],
            "direction":          result["direction"],
            "criteria_confirmed": result["criteria_confirmed"],
            "criteria_detail":    result["criteria_detail"],
            "long_confirmed":     result["long_confirmed"],
            "short_confirmed":    result["short_confirmed"],
            "master_summary":     result["master_summary"],
            "entered_tier_at":    entry_at,
            "last_evaluated_at":  now,
            "last_notified_tier": prev_notified_tier,
        })

        return {
            "result":             result,
            "prev_tier":          prev_tier,
            "prev_notified_tier": prev_notified_tier,
        }

    def get_all_states(self) -> dict:
        return _db.signal_state_get_all()

    def mark_notified(self, symbol: str, tier: str) -> None:
        _db.signal_state_set_notified(symbol, tier)
