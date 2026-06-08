"""
Core math formulas for the DCA system.
All functions are pure — no side effects.
"""
from typing import List, Tuple


def calc_dump_percent(reference_price: float, current_price: float) -> float:
    """
    How far (%) current price has fallen from the reference top.
    Negative value = dump. Positive = above reference.

    Formula: ((current - reference) / reference) * 100
    """
    if reference_price <= 0:
        raise ValueError("reference_price must be > 0")
    return ((current_price - reference_price) / reference_price) * 100


def calc_weighted_average_entry(
    executed_sizes: List[float],
    executed_prices: List[float],
) -> float:
    """
    Weighted average entry price across all executed steps.

    Formula: SUM(size * price) / SUM(size)
    """
    if not executed_sizes or not executed_prices:
        return 0.0
    if len(executed_sizes) != len(executed_prices):
        raise ValueError("sizes and prices lists must be the same length")

    total_value = sum(s * p for s, p in zip(executed_sizes, executed_prices))
    total_size = sum(executed_sizes)
    if total_size == 0:
        return 0.0
    return total_value / total_size


def calc_take_profit_price(average_entry: float, tp_percent: float) -> float:
    """
    Target exit price.

    Formula: average_entry * (1 + tp_percent / 100)
    """
    return average_entry * (1 + tp_percent / 100)


def calc_quantity(usdt_amount: float, price: float) -> float:
    """How many coins does USDT buy at given price."""
    if price <= 0:
        raise ValueError("price must be > 0")
    return usdt_amount / price


def calc_pnl(
    average_entry: float,
    exit_price: float,
    total_quantity: float,
) -> Tuple[float, float]:
    """
    Returns (pnl_usdt, pnl_percent).
    """
    pnl_usdt = (exit_price - average_entry) * total_quantity
    pnl_pct = ((exit_price - average_entry) / average_entry) * 100
    return pnl_usdt, pnl_pct


def should_buy(
    dump_percent: float,
    target_level: float,
    tolerance: float = 0.1,
) -> bool:
    """
    Returns True if current dump has reached (or passed) the target level.

    dump_percent  – current dump from reference (e.g. -11.3)
    target_level  – configured step level  (e.g. -10)
    tolerance     – how far below the level we still accept (default 0.1%)
    """
    return dump_percent <= target_level + tolerance


def should_take_profit(current_price: float, tp_price: float) -> bool:
    """Returns True when price has hit the take-profit target."""
    return current_price >= tp_price


def calc_stop_loss_price(reference_price: float, sl_percent: float) -> float:
    """
    SL threshold = reference_price × (1 - sl_percent / 100)
    Fixed price — does not change as levels fill.
    """
    return reference_price * (1 - sl_percent / 100)


def should_stop_loss(current_price: float, sl_price: float) -> bool:
    """Returns True when price has hit or passed the stop-loss threshold."""
    return current_price <= sl_price
