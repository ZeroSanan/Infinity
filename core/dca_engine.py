"""
DCA Engine — the brain of the system.

One DCAEngine instance is created per coin.
Call engine.tick() on every price poll cycle.
"""
import time
from datetime import datetime, timezone

from models.dca_config import CoinConfig, PositionState
from core.binance_client import BinanceSpotClient
from core.state_manager import load_state, save_state, reset_state, add_executed_step
from core.logger import get_logger, log_order, log_position
from utils.calculations import (
    calc_dump_percent,
    calc_take_profit_price,
    calc_stop_loss_price,
    calc_pnl,
    should_buy,
    should_take_profit,
    should_stop_loss,
)

POLL_INTERVAL = 10  # seconds between price checks


class DCAEngine:
    def __init__(self, config: CoinConfig, client: BinanceSpotClient):
        self.cfg = config
        self.client = client
        self.logger = get_logger(f"engine.{config.id}")
        self.state: PositionState = load_state(config.id, config.coin, config.symbol)
        self._step_size = client.get_step_size(config.symbol)

        # Seed reference price from config if not already persisted
        if self.state.reference_price is None and config.reference_price:
            self.state.reference_price = config.reference_price
            save_state(self.state)

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self):
        """Blocking loop — runs until interrupted."""
        self.logger.info(
            f"▶  [{self.cfg.name}] DCA engine started | "
            f"coin={self.cfg.coin} | steps={self.cfg.step_count} | TP={self.cfg.take_profit_percent}%"
        )
        try:
            while True:
                self.tick()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            self.logger.info(f"[{self.cfg.name}] Engine stopped by user.")

    def tick(self):
        """Single evaluation cycle — called once per poll."""
        try:
            price = self.client.get_price(self.cfg.symbol)
        except Exception as e:
            self.logger.error(f"[{self.cfg.name}] Failed to fetch price: {e}")
            return

        state = self.state

        # ── Ensure reference price is set ────────────────────────────────────
        if state.reference_price is None:
            self.logger.warning(
                f"WAIT | [{self.cfg.name}] No reference price set. "
                f"Set it via set_reference_price(). Current price: {price:.4f}"
            )
            return

        dump_pct = calc_dump_percent(state.reference_price, price)

        self.logger.debug(
            f"[{self.cfg.name}] price={price:.4f} | "
            f"ref={state.reference_price:.4f} | dump={dump_pct:.2f}%"
        )

        # ── Check TP and SL (only when position is active) ───────────────────
        if state.status == "ACTIVE":
            tp_price = calc_take_profit_price(
                state.average_entry, self.cfg.take_profit_percent
            )
            if should_take_profit(price, tp_price):
                self._execute_take_profit(price, tp_price)
                return

            # SL activates from the first fill onward
            if (
                self.cfg.stop_loss_percent > 0
                and state.steps_done >= 1
            ):
                sl_price = calc_stop_loss_price(
                    state.reference_price, self.cfg.stop_loss_percent
                )
                if should_stop_loss(price, sl_price):
                    self._execute_stop_loss(price, sl_price)
                    return

            log_position(
                self.logger,
                self.cfg.name,
                state.average_entry,
                state.total_quantity,
                state.total_invested,
                tp_price,
                state.steps_done,
            )

        # ── Check for next DCA buy step ──────────────────────────────────────
        next_idx = state.next_step_index
        if next_idx >= self.cfg.step_count:
            self.logger.info(
                f"[{self.cfg.name}] All {self.cfg.step_count} steps executed. "
                f"Waiting for TP at {calc_take_profit_price(state.average_entry, self.cfg.take_profit_percent):.4f}."
            )
            return

        target_level = self.cfg.dump_levels[next_idx]
        if should_buy(dump_pct, target_level):
            self._execute_buy(next_idx, price)

    # ── Execution ────────────────────────────────────────────────────────────

    def _execute_buy(self, step_index: int, current_price: float):
        state = self.state
        usdt = self.cfg.order_sizes[step_index]
        level = self.cfg.dump_levels[step_index]

        self.logger.info(
            f"BUY  [{self.cfg.name}] Step {step_index+1}/{self.cfg.step_count} | "
            f"dump={calc_dump_percent(state.reference_price, current_price):.2f}% "
            f"(target {level}%) | size={usdt} USDT"
        )

        # Safety: check balance
        balance = self.client.get_balance("USDT")
        if balance < usdt:
            self.logger.error(
                f"[{self.cfg.name}] Insufficient USDT balance: "
                f"need {usdt:.2f}, have {balance:.2f}"
            )
            return

        try:
            order = self.client.place_market_buy(self.cfg.symbol, usdt)
        except Exception as e:
            self.logger.error(f"[{self.cfg.name}] Buy order failed: {e}")
            return

        fill_price = BinanceSpotClient.parse_fill_price(order)
        fill_qty = BinanceSpotClient.parse_fill_quantity(order)
        order_id = str(order["orderId"])

        if fill_price == 0 or fill_qty == 0:
            self.logger.error(
                f"[{self.cfg.name}] Order {order_id} returned zero fill — skipping."
            )
            return

        add_executed_step(
            state=state,
            step_index=step_index,
            dump_level=level,
            order_size_usdt=usdt,
            entry_price=fill_price,
            quantity=fill_qty,
            order_id=order_id,
        )

        log_order(self.logger, "BUY", self.cfg.symbol, fill_price, usdt, fill_qty, order_id)

    def _execute_take_profit(self, current_price: float, tp_price: float):
        state = self.state

        self.logger.info(
            f"PROFIT [{self.cfg.name}] TP triggered | "
            f"price={current_price:.4f} >= tp={tp_price:.4f}"
        )

        step_size = self._step_size
        try:
            order = self.client.place_market_sell(
                self.cfg.symbol, state.total_quantity, step_size
            )
        except Exception as e:
            self.logger.error(f"[{self.cfg.name}] Sell order failed: {e}")
            return

        exit_price = BinanceSpotClient.parse_fill_price(order)
        exit_qty = BinanceSpotClient.parse_fill_quantity(order)
        order_id = str(order["orderId"])

        pnl_usdt, pnl_pct = calc_pnl(state.average_entry, exit_price, exit_qty)

        log_order(
            self.logger, "SELL", self.cfg.symbol,
            exit_price, exit_qty * exit_price, exit_qty, order_id
        )
        self.logger.info(
            f"PROFIT [{self.cfg.name}] "
            f"PnL={pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%) | "
            f"avg_entry={state.average_entry:.4f} | exit={exit_price:.4f} | "
            f"steps_done={state.steps_done}"
        )

        state.status = "EXITED"
        save_state(state)
        reset_state(state)

    def _execute_stop_loss(self, current_price: float, sl_price: float):
        state = self.state

        self.logger.warning(
            f"STOP-LOSS [{self.cfg.name}] triggered | "
            f"price={current_price:.4f} <= sl={sl_price:.4f} | "
            f"ref={state.reference_price:.4f} | sl%={self.cfg.stop_loss_percent}%"
        )

        step_size = self._step_size
        try:
            order = self.client.place_market_sell(
                self.cfg.symbol, state.total_quantity, step_size
            )
        except Exception as e:
            self.logger.error(f"[{self.cfg.name}] Stop-loss sell failed: {e}")
            return

        exit_price = BinanceSpotClient.parse_fill_price(order)
        exit_qty   = BinanceSpotClient.parse_fill_quantity(order)
        order_id   = str(order["orderId"])

        pnl_usdt, pnl_pct = calc_pnl(state.average_entry, exit_price, exit_qty)

        log_order(
            self.logger, "SELL(SL)", self.cfg.symbol,
            exit_price, exit_qty * exit_price, exit_qty, order_id
        )
        self.logger.warning(
            f"STOP-LOSS [{self.cfg.name}] closed | "
            f"PnL={pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%) | "
            f"avg_entry={state.average_entry:.4f} | exit={exit_price:.4f} | "
            f"steps_done={state.steps_done}"
        )

        state.status = "EXITED"
        save_state(state)
        reset_state(state)

    # ── Configuration helpers ────────────────────────────────────────────────

    def set_reference_price(self, price: float):
        """Manually set the reference top price."""
        self.state.reference_price = price
        save_state(self.state)
        self.logger.info(f"[{self.cfg.name}] Reference price set to {price:.4f}")

    def set_reference_to_current(self):
        """Use the live market price as the reference top."""
        price = self.client.get_price(self.cfg.symbol)
        self.set_reference_price(price)

    def status_summary(self) -> str:
        s = self.state
        ref = s.reference_price or 0
        lines = [
            f"╔══ [{self.cfg.name}] ({self.cfg.coin}) Status: {s.status} ══",
            f"║  Reference price : {ref:.4f}",
            f"║  Steps done      : {s.steps_done}/{self.cfg.step_count}",
        ]
        if s.status == "ACTIVE":
            tp = calc_take_profit_price(s.average_entry, self.cfg.take_profit_percent)
            lines += [
                f"║  Average entry   : {s.average_entry:.4f}",
                f"║  Total invested  : {s.total_invested:.2f} USDT",
                f"║  Total quantity  : {s.total_quantity:.6f}",
                f"║  TP target price : {tp:.4f}",
            ]
        lines.append("╚" + "═" * 40)
        return "\n".join(lines)
