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

State is persisted to data/signal_state.json for transition detection.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional


DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
STATE_FILE = os.path.join(DATA_DIR, "signal_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "signal_config.json")

_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAUTUSDT"]

_DEFAULT_CONFIG = {
    "liq_proximity_pct":       3.0,
    "oi_consecutive_cycles":   2,
    "telegram_enabled":        True,
    "tp_achievability_in_msg": True,
}


# ── Config ───────────────────────────────────────────────────────────────────


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            merged = dict(_DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ── State persistence ─────────────────────────────────────────────────────────


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── OI trend: read Signal History ────────────────────────────────────────────


def _oi_trend_confirms(symbol: str, direction: str,
                        recorder, consecutive_required: int) -> bool:
    """Check last N consecutive Layer 2 snapshots for matching OI label."""
    try:
        history = recorder.get_history(symbol, days=1)
        snaps = history.get("layer2", [])
    except Exception:
        return False

    if len(snaps) < consecutive_required:
        return False

    recent = snaps[-consecutive_required:]
    if direction == "SHORT":
        # Exhaustion signal: longs closing / OI falling
        target_labels = {"Exhaustion — Longs Closing", "Exhaustion — Low Conviction"}
    else:
        # Strong new money entering for LONG
        target_labels = {"Strong — New Money Entering"}

    return all(s.get("oi_label") in target_labels for s in recent)


# ── Liquidation proximity ─────────────────────────────────────────────────────


def _liq_proximity_confirms(direction: str, liq_data: Optional[dict],
                              current_price: Optional[float],
                              threshold_pct: float) -> bool:
    """Check if nearest liquidation cluster in the expected direction is
    within threshold_pct of current price."""
    if not liq_data or not current_price or current_price <= 0:
        return False
    if direction == "SHORT":
        cluster = liq_data.get("below_price")
    else:
        cluster = liq_data.get("above_price")
    if cluster is None:
        return False
    try:
        distance_pct = abs(cluster - current_price) / current_price * 100
        return distance_pct <= threshold_pct
    except (TypeError, ZeroDivisionError):
        return False


# ── Six-criteria evaluation ───────────────────────────────────────────────────


def _evaluate_direction(
    direction: str,  # "LONG" or "SHORT"
    snapshot: dict,  # from _signal_snapshot()
    l2_data: dict,
    mm_data: Optional[dict],
    recorder,
    liq_data: Optional[dict],
    current_price: Optional[float],
    config: dict,
) -> tuple[int, dict]:
    """Return (criteria_count, criteria_detail_dict) for the given direction."""
    detail: dict[str, bool] = {}

    # 1. Master Summary Bar
    master = snapshot.get("master", "")
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
    if direction == "SHORT":
        # Crowd long-crowded (>65% long) → bearish contrarian
        detail["l2_crowding"] = global_long_pct > 65.0
    else:
        # Crowd short-crowded (>65% short) → bullish contrarian
        detail["l2_crowding"] = (100 - global_long_pct) > 65.0

    # 3. Position Ratio divergence
    pos = l2_data.get("position_ratio") or {}
    pos_significance = pos.get("significance")
    pos_direction    = pos.get("divergence_direction")
    if direction == "SHORT":
        detail["position_ratio_divergence"] = (
            pos_significance == "significant" and
            pos_direction == "shorts_larger_than_headcount"
        )
    else:
        detail["position_ratio_divergence"] = (
            pos_significance == "significant" and
            pos_direction == "longs_larger_than_headcount"
        )

    # 4. OI trend (reads Signal History)
    consecutive = config.get("oi_consecutive_cycles", 2)
    detail["oi_trend"] = _oi_trend_confirms(
        l2_data.get("symbol", ""), direction, recorder, consecutive)

    # 5. Market Mechanics
    taker    = (mm_data or {}).get("taker") or {}
    vol_rat  = (mm_data or {}).get("volume_ratio") or {}
    if direction == "SHORT":
        taker_ok  = (taker.get("status") == "ok" and
                     (taker.get("sell_pct") or 0) > 55)
        futures_ok = (vol_rat.get("status") == "ok" and
                      "Futures Dominant" in (vol_rat.get("label") or ""))
    else:
        taker_ok  = (taker.get("status") == "ok" and
                     (taker.get("buy_pct") or 0) > 55)
        futures_ok = (vol_rat.get("status") == "ok" and
                      "Spot Dominant" in (vol_rat.get("label") or ""))
    detail["market_mechanics"] = taker_ok or futures_ok

    # 6. Liquidation proximity
    threshold = config.get("liq_proximity_pct", 3.0)
    detail["liquidation_proximity"] = _liq_proximity_confirms(
        direction, liq_data, current_price, threshold)

    confirmed = sum(1 for v in detail.values() if v)
    return confirmed, detail


# ── Master gate check ─────────────────────────────────────────────────────────


def _master_contradicts(direction: str, master: str) -> bool:
    """Return True if Master Summary Bar directly contradicts the direction."""
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
        """Evaluate current market state for one symbol.

        Returns a result dict with tier (NONE/DEVELOPING/STRONG),
        direction (LONG/SHORT/NONE), criteria confirmed count, and
        per-criterion detail for both directions."""
        if config is None:
            config = load_config()

        long_count, long_detail = _evaluate_direction(
            "LONG", snapshot, l2_data, mm_data,
            self.recorder, liq_data, current_price, config)
        short_count, short_detail = _evaluate_direction(
            "SHORT", snapshot, l2_data, mm_data,
            self.recorder, liq_data, current_price, config)

        master = snapshot.get("master", "")

        # Determine dominant direction
        if long_count >= short_count and long_count >= 3:
            direction = "LONG"
            confirmed = long_count
            detail    = long_detail
        elif short_count > long_count and short_count >= 3:
            direction = "SHORT"
            confirmed = short_count
            detail    = short_detail
        else:
            # Ambiguous — pick the better one for display but tier as NONE
            direction = "LONG" if long_count >= short_count else "SHORT"
            confirmed = max(long_count, short_count)
            detail    = long_detail if long_count >= short_count else short_detail

        # Tier classification
        contradicted = _master_contradicts(direction, master)
        if confirmed >= 5 and not contradicted:
            tier = "STRONG"
        elif confirmed >= 3:
            tier = "DEVELOPING"
        else:
            tier = "NONE"

        # Reset direction label when tier is NONE
        if tier == "NONE":
            direction = "NONE"

        return {
            "symbol":            symbol,
            "tier":              tier,
            "direction":         direction,
            "criteria_confirmed": confirmed,
            "criteria_detail":   detail,
            "long_confirmed":    long_count,
            "short_confirmed":   short_count,
            "master_summary":    master,
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
        """Evaluate and write result to data/signal_state.json.
        Returns dict with 'result' and 'prev_tier' for transition detection."""
        if config is None:
            config = load_config()

        result = self.evaluate(symbol, snapshot, l2_data, mm_data,
                                liq_data, current_price, config)

        state = _load_state()
        prev  = state.get(symbol, {})
        prev_tier          = prev.get("tier", "NONE")
        prev_notified_tier = prev.get("last_notified_tier", "NONE")

        now = _now_iso()
        entry_at = prev.get("entered_tier_at", now)
        if result["tier"] != prev_tier:
            entry_at = now

        state[symbol] = {
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
        }
        _save_state(state)

        return {
            "result":    result,
            "prev_tier": prev_tier,
            "prev_notified_tier": prev_notified_tier,
        }

    def get_all_states(self) -> dict:
        """Return the full signal_state.json contents."""
        return _load_state()

    def mark_notified(self, symbol: str, tier: str) -> None:
        """Update last_notified_tier after a notification is sent."""
        state = _load_state()
        if symbol in state:
            state[symbol]["last_notified_tier"] = tier
            _save_state(state)
