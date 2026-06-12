from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CoinConfig:
    """Configuration for a single DCA strategy."""
    id: str                          # unique strategy id (uuid short)
    name: str                        # user-defined label
    coin: str
    symbol: str
    enabled: bool
    step_count: int
    dump_levels: List[float]
    order_sizes: List[float]
    take_profit_percent: float
    stop_loss_percent: float = 0.0
    reference_price: Optional[float] = None

    # ── Mixed long/short engine (Binance USD-M Futures) ──────────────────────
    mode: str = "long"               # "long" (spot DCA) | "mixed" (auto long/short futures)
    bear_levels: List[float] = field(default_factory=list)        # positive pump %
    bear_order_sizes: List[float] = field(default_factory=list)
    bear_take_profit_percent: float = 0.0
    bull_stop_loss_percent: float = 0.0
    bear_stop_loss_percent: float = 0.0
    leverage: int = 1
    use_entry_indicator: bool = True
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    regime_interval: str = "1h"

    def validate(self):
        if len(self.dump_levels) != self.step_count:
            raise ValueError(
                f"[{self.name}] dump_levels count ({len(self.dump_levels)}) "
                f"must match step_count ({self.step_count})"
            )
        if len(self.order_sizes) != self.step_count:
            raise ValueError(
                f"[{self.name}] order_sizes count ({len(self.order_sizes)}) "
                f"must match step_count ({self.step_count})"
            )
        if self.take_profit_percent <= 0:
            raise ValueError(f"[{self.name}] take_profit_percent must be > 0")
        for i, level in enumerate(self.dump_levels):
            if level >= 0:
                raise ValueError(
                    f"[{self.name}] dump_levels[{i}] must be negative, got {level}"
                )

        if self.mode == "mixed":
            if not self.bear_levels:
                raise ValueError(f"[{self.name}] mode='mixed' requires bear_levels")
            if len(self.bear_levels) != len(self.bear_order_sizes):
                raise ValueError(
                    f"[{self.name}] bear_levels count ({len(self.bear_levels)}) "
                    f"must match bear_order_sizes count ({len(self.bear_order_sizes)})"
                )
            for i, level in enumerate(self.bear_levels):
                if level <= 0:
                    raise ValueError(
                        f"[{self.name}] bear_levels[{i}] must be positive, got {level}"
                    )
            if self.bear_take_profit_percent <= 0:
                raise ValueError(f"[{self.name}] bear_take_profit_percent must be > 0 for mode='mixed'")
            if self.leverage < 1:
                raise ValueError(f"[{self.name}] leverage must be >= 1")
        elif self.mode != "long":
            raise ValueError(f"[{self.name}] mode must be 'long' or 'mixed', got '{self.mode}'")

        return True

    @property
    def total_capital(self) -> float:
        return sum(self.order_sizes)

    @property
    def total_capital_bear(self) -> float:
        return sum(self.bear_order_sizes)


@dataclass
class ExecutedStep:
    """A single executed DCA buy/sell step."""
    step_index: int
    dump_level: float
    order_size_usdt: float
    entry_price: float
    quantity: float
    order_id: str
    timestamp: str
    side: str = "LONG"               # "LONG" | "SHORT" — which ladder this step belongs to


@dataclass
class PositionState:
    """Live state of an active DCA position."""
    strategy_id: str          # matches CoinConfig.id
    coin: str
    symbol: str
    status: str = "WAITING"   # WAITING | ACTIVE | EXITED
    executed_steps: List[ExecutedStep] = field(default_factory=list)
    total_invested: float = 0.0
    total_quantity: float = 0.0
    average_entry: float = 0.0
    reference_price: Optional[float] = None
    last_updated: str = ""

    # ── Mixed long/short engine fields ────────────────────────────────────────
    direction: str = "NONE"          # NONE | LONG | SHORT — current broker position
    active_mode: str = "WAIT"        # WAIT | BUY | SELL — regime-driven target direction
    regime: str = "NEUTRAL"          # last confirmed regime (BULLISH | BEARISH | NEUTRAL)
    anchor_price: Optional[float] = None
    leverage: int = 1
    liquidation_price: Optional[float] = None

    @property
    def next_step_index(self) -> int:
        return len(self.executed_steps)

    @property
    def steps_done(self) -> int:
        return len(self.executed_steps)


@dataclass
class DCAModel:
    """
    A shared 'DCA Model' template — bull (long) + bear (short) ladders plus
    regime/leverage settings, applied to one or more coin cards to turn them
    into auto-switching Mixed engines (Binance USD-M Futures).
    """
    id: str
    name: str
    bull_levels: List[float]                 # negative %, e.g. [-6, -9, -13, -18]
    bull_order_sizes: List[float]
    bull_take_profit_percent: float
    bear_levels: List[float]                 # positive %, e.g. [6, 9, 13, 18]
    bear_order_sizes: List[float]
    bear_take_profit_percent: float
    leverage: int = 1
    bull_stop_loss_percent: float = 0.0
    bear_stop_loss_percent: float = 0.0
    use_entry_indicator: bool = True
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    regime_interval: str = "1h"

    def validate(self):
        if len(self.bull_levels) != len(self.bull_order_sizes):
            raise ValueError("bull_levels and bull_order_sizes must be the same length")
        if len(self.bear_levels) != len(self.bear_order_sizes):
            raise ValueError("bear_levels and bear_order_sizes must be the same length")
        if not self.bull_levels or not self.bear_levels:
            raise ValueError("Both bull_levels and bear_levels are required")
        for i, lvl in enumerate(self.bull_levels):
            if lvl >= 0:
                raise ValueError(f"bull_levels[{i}] must be negative, got {lvl}")
        for i, lvl in enumerate(self.bear_levels):
            if lvl <= 0:
                raise ValueError(f"bear_levels[{i}] must be positive, got {lvl}")
        if self.bull_take_profit_percent <= 0:
            raise ValueError("bull_take_profit_percent must be > 0")
        if self.bear_take_profit_percent <= 0:
            raise ValueError("bear_take_profit_percent must be > 0")
        if self.leverage < 1:
            raise ValueError("leverage must be >= 1")
        return True
