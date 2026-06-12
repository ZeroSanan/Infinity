"""
Persists and loads position state to/from JSON files in the data/ directory.
One file per strategy: data/{strategy_id}.json
"""
import json
import os
from datetime import datetime, timezone

from models.dca_config import PositionState, ExecutedStep
from core.logger import get_logger

logger = get_logger("state_manager")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _state_path(strategy_id: str) -> str:
    return os.path.join(DATA_DIR, f"{strategy_id}.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Serialisation ─────────────────────────────────────────────────────────────

def _step_to_dict(step: ExecutedStep) -> dict:
    return {
        "step_index":      step.step_index,
        "dump_level":      step.dump_level,
        "order_size_usdt": step.order_size_usdt,
        "entry_price":     step.entry_price,
        "quantity":        step.quantity,
        "order_id":        step.order_id,
        "timestamp":       step.timestamp,
        "side":            step.side,
    }


def _step_from_dict(d: dict) -> ExecutedStep:
    return ExecutedStep(
        step_index=d["step_index"],
        dump_level=d["dump_level"],
        order_size_usdt=d["order_size_usdt"],
        entry_price=d["entry_price"],
        quantity=d["quantity"],
        order_id=d["order_id"],
        timestamp=d["timestamp"],
        side=d.get("side", "LONG"),
    )


def _state_to_dict(state: PositionState) -> dict:
    return {
        "strategy_id":    state.strategy_id,
        "coin":           state.coin,
        "symbol":         state.symbol,
        "status":         state.status,
        "executed_steps": [_step_to_dict(s) for s in state.executed_steps],
        "total_invested": state.total_invested,
        "total_quantity": state.total_quantity,
        "average_entry":  state.average_entry,
        "reference_price":state.reference_price,
        "last_updated":   state.last_updated,
        "direction":         state.direction,
        "active_mode":       state.active_mode,
        "regime":            state.regime,
        "anchor_price":      state.anchor_price,
        "leverage":          state.leverage,
        "liquidation_price": state.liquidation_price,
    }


def _state_from_dict(d: dict) -> PositionState:
    return PositionState(
        strategy_id=d.get("strategy_id", d.get("symbol", "")),
        coin=d["coin"],
        symbol=d["symbol"],
        status=d["status"],
        executed_steps=[_step_from_dict(s) for s in d.get("executed_steps", [])],
        total_invested=d.get("total_invested", 0.0),
        total_quantity=d.get("total_quantity", 0.0),
        average_entry=d.get("average_entry", 0.0),
        reference_price=d.get("reference_price"),
        last_updated=d.get("last_updated", ""),
        direction=d.get("direction", "NONE"),
        active_mode=d.get("active_mode", "WAIT"),
        regime=d.get("regime", "NEUTRAL"),
        anchor_price=d.get("anchor_price"),
        leverage=d.get("leverage", 1),
        liquidation_price=d.get("liquidation_price"),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load_state(strategy_id: str, coin: str, symbol: str) -> PositionState:
    path = _state_path(strategy_id)
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        state = _state_from_dict(data)
        logger.debug(f"[{strategy_id}] State loaded: status={state.status}, steps={state.steps_done}")
        return state
    logger.debug(f"[{strategy_id}] No existing state — starting fresh.")
    return PositionState(strategy_id=strategy_id, coin=coin, symbol=symbol)


def save_state(state: PositionState):
    state.last_updated = _now()
    with open(_state_path(state.strategy_id), "w") as f:
        json.dump(_state_to_dict(state), f, indent=2)
    logger.debug(f"[{state.strategy_id}] State saved: status={state.status}")


def reset_state(state: PositionState):
    state.status = "WAITING"
    state.executed_steps = []
    state.total_invested = 0.0
    state.total_quantity = 0.0
    state.average_entry = 0.0
    state.direction = "NONE"
    state.liquidation_price = None
    save_state(state)
    logger.info(f"RESET | [{state.strategy_id}] Position reset — back to WAITING.")


def add_executed_step(
    state: PositionState,
    step_index: int,
    dump_level: float,
    order_size_usdt: float,
    entry_price: float,
    quantity: float,
    order_id: str,
    side: str = "LONG",
):
    step = ExecutedStep(
        step_index=step_index,
        dump_level=dump_level,
        order_size_usdt=order_size_usdt,
        entry_price=entry_price,
        quantity=quantity,
        order_id=str(order_id),
        timestamp=_now(),
        side=side,
    )
    state.executed_steps.append(step)
    state.total_invested += order_size_usdt
    state.total_quantity += quantity
    state.status = "ACTIVE"

    # True cost basis: total USDT spent / total coins held.
    state.average_entry = state.total_invested / state.total_quantity

    save_state(state)
    logger.info(
        f"STEP ADDED | [{state.strategy_id}] step={step_index+1} | "
        f"avg_entry={state.average_entry:.4f} | invested={state.total_invested:.2f}"
    )
