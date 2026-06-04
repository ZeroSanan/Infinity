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
    reference_price: Optional[float] = None

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
        return True

    @property
    def total_capital(self) -> float:
        return sum(self.order_sizes)


@dataclass
class ExecutedStep:
    """A single executed DCA buy step."""
    step_index: int
    dump_level: float
    order_size_usdt: float
    entry_price: float
    quantity: float
    order_id: str
    timestamp: str


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

    @property
    def next_step_index(self) -> int:
        return len(self.executed_steps)

    @property
    def steps_done(self) -> int:
        return len(self.executed_steps)
