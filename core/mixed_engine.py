"""
Mixed Long/Short DCA Engine — live trading on Binance USD-M Futures.

One MixedEngine instance is created per coin whose CoinConfig.mode == "mixed".
It auto-switches between LONG and SHORT DCA ladders based on the regime
detected by core.regime_live (EMA50/EMA200, RSI, higher-highs, volume, and
Bollinger-band extremity overrides — mirrors algo-trading/mixed_dca_strategy.py).

Call engine.tick() on every poll cycle.
"""
import time

from models.dca_config import CoinConfig, PositionState
from core.binance_futures_client import BinanceFuturesClient
from core.state_manager import save_state, load_state, add_executed_step
from core.logger import get_logger, log_order
from core.regime_live import fetch_candles, compute_regime_state, score_entry
from utils.calculations import calc_quantity

POLL_INTERVAL = 30           # seconds between regime/price checks
CANDLE_LIMIT = 500


class MixedEngine:
    def __init__(self, config: CoinConfig, client: BinanceFuturesClient):
        self.cfg = config
        self.client = client
        self.logger = get_logger(f"mixed.{config.id}")
        self.state: PositionState = load_state(config.id, config.coin, config.symbol)
        self._step_size = client.get_step_size(config.symbol)
        self._hedge_mode = client.get_position_mode()

        if self.state.leverage != config.leverage:
            client.set_leverage(config.symbol, config.leverage)
            self.state.leverage = config.leverage
            save_state(self.state)

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self):
        """Blocking loop — runs until interrupted."""
        self.logger.info(
            f"▶  [{self.cfg.name}] Mixed engine started | {self.cfg.symbol} | "
            f"leverage={self.cfg.leverage}x | hedge_mode={self._hedge_mode}"
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
            candles = fetch_candles(self.cfg.symbol, interval=self.cfg.regime_interval, limit=CANDLE_LIMIT)
            price = self.client.get_mark_price(self.cfg.symbol)
        except Exception as e:
            self.logger.error(f"[{self.cfg.name}] Failed to fetch market data: {e}")
            return

        regime = compute_regime_state(candles, self.cfg.rsi_overbought, self.cfg.rsi_oversold)
        state = self.state
        state.regime = regime["confirmed"]
        new_mode = regime["active_mode"]

        self.logger.debug(
            f"[{self.cfg.name}] price={price:.4f} | regime={state.regime} | "
            f"active_mode={state.active_mode}->{new_mode} | direction={state.direction} | "
            f"score={regime['score']}"
        )

        # ── 1. Regime/mode change — close any open position in the old direction ──
        if new_mode != state.active_mode:
            self.logger.info(
                f"REGIME [{self.cfg.name}] mode change: {state.active_mode} -> {new_mode} "
                f"(confirmed={state.regime}, override={regime['override_type']})"
            )
            if state.direction != "NONE":
                self._close_position(price, reason="mode_change")
            state.active_mode = new_mode
            state.anchor_price = None
            save_state(state)

        if state.active_mode == "WAIT":
            save_state(state)
            return

        # ── 2. Arm the ladder (set anchor) if no position is open ──────────────
        if state.direction == "NONE":
            if not self._arm_anchor(price, regime):
                save_state(state)
                return

        # ── 3. Check for DCA ladder fills ──────────────────────────────────────
        self._check_ladder_fills(price)

        # ── 4. Stop-loss / take-profit (only if a position is open) ────────────
        if state.direction != "NONE":
            if not self._check_stop_loss(price):
                self._check_take_profit(price)

        save_state(state)

    # ── Ladder arming ────────────────────────────────────────────────────────

    def _arm_anchor(self, price: float, regime: dict) -> bool:
        """
        Set state.anchor_price for a fresh ladder in the current active_mode.
        Returns False if we should wait (no anchor set yet).
        """
        state = self.state
        cfg = self.cfg

        if cfg.use_entry_indicator:
            ind = regime["indicators"]
            if ind["ema21"] is None:
                return False
            sc, sigs = score_entry(state.active_mode, regime["series"])
            if sc < 4:
                return False
            if state.anchor_price is None:
                self.logger.info(
                    f"ENTRY [{self.cfg.name}] indicator fired (score={sc}, signals={sigs}) "
                    f"-> anchor={ind['ema21']:.4f}"
                )
            state.anchor_price = ind["ema21"]
            return True

        # No entry indicator — track a rolling anchor (extreme price)
        if state.anchor_price is None:
            state.anchor_price = price
        elif state.active_mode == "BUY":
            state.anchor_price = max(state.anchor_price, price)
        else:
            state.anchor_price = min(state.anchor_price, price)
        return True

    # ── Ladder fills ─────────────────────────────────────────────────────────

    def _check_ladder_fills(self, price: float):
        state = self.state
        cfg = self.cfg

        if state.active_mode == "BUY":
            levels, sizes, side_label = cfg.dump_levels, cfg.order_sizes, "LONG"
        else:
            levels, sizes, side_label = cfg.bear_levels, cfg.bear_order_sizes, "SHORT"

        next_idx = state.steps_done
        if next_idx >= len(levels):
            return

        anchor = state.anchor_price
        if anchor is None or anchor <= 0:
            return

        pct_move = (price - anchor) / anchor * 100
        target = levels[next_idx]
        triggered = pct_move <= target if side_label == "LONG" else pct_move >= target

        if triggered:
            self._execute_entry(next_idx, price, side_label, levels, sizes)

    def _execute_entry(self, step_index: int, price: float, side_label: str, levels: list, sizes: list):
        state = self.state
        cfg = self.cfg

        usdt = sizes[step_index]
        level = levels[step_index]
        order_side = "BUY" if side_label == "LONG" else "SELL"
        position_side = "LONG" if side_label == "LONG" else "SHORT"

        notional = usdt * cfg.leverage
        quantity = calc_quantity(notional, price)
        quantity = self.client.floor_quantity(quantity, self._step_size)
        if quantity <= 0:
            self.logger.error(f"[{cfg.name}] Computed quantity rounds to zero — skipping step {step_index + 1}.")
            return

        self.logger.info(
            f"{order_side} [{cfg.name}] {side_label} step {step_index + 1}/{len(levels)} | "
            f"level={level}% | size={usdt} USDT x{cfg.leverage} | qty={quantity}"
        )

        try:
            pos_side = position_side if self._hedge_mode else None
            order = self.client.place_market_order(cfg.symbol, order_side, quantity, position_side=pos_side)
        except Exception as e:
            self.logger.error(f"[{cfg.name}] Order failed: {e}")
            return

        fill_price, fill_qty = self.client.fill_info(cfg.symbol, order)
        order_id = str(order["orderId"])

        if fill_price == 0 or fill_qty == 0:
            self.logger.error(f"[{cfg.name}] Order {order_id} returned zero fill — skipping.")
            return

        add_executed_step(
            state=state,
            step_index=step_index,
            dump_level=level,
            order_size_usdt=usdt,
            entry_price=fill_price,
            quantity=fill_qty,
            order_id=order_id,
            side=side_label,
        )
        state.direction = side_label

        log_order(self.logger, order_side, cfg.symbol, fill_price, usdt, fill_qty, order_id)
        self._refresh_liquidation_price()

    # ── Stop-loss / take-profit ──────────────────────────────────────────────

    def _check_stop_loss(self, price: float) -> bool:
        state = self.state
        cfg = self.cfg

        if state.direction == "LONG" and cfg.bull_stop_loss_percent > 0:
            sl_price = state.anchor_price * (1 - cfg.bull_stop_loss_percent / 100)
            if price <= sl_price:
                self.logger.warning(f"STOP-LOSS [{cfg.name}] LONG | price={price:.4f} <= sl={sl_price:.4f}")
                self._close_position(price, reason="stop_loss")
                state.anchor_price = price
                return True

        elif state.direction == "SHORT" and cfg.bear_stop_loss_percent > 0:
            sl_price = state.anchor_price * (1 + cfg.bear_stop_loss_percent / 100)
            if price >= sl_price:
                self.logger.warning(f"STOP-LOSS [{cfg.name}] SHORT | price={price:.4f} >= sl={sl_price:.4f}")
                self._close_position(price, reason="stop_loss")
                state.anchor_price = price
                return True

        return False

    def _check_take_profit(self, price: float):
        state = self.state
        cfg = self.cfg

        if state.direction == "LONG":
            tp_price = state.average_entry * (1 + cfg.take_profit_percent / 100)
            hit = price >= tp_price
        else:
            tp_price = state.average_entry * (1 - cfg.bear_take_profit_percent / 100)
            hit = price <= tp_price

        if hit:
            self.logger.info(
                f"PROFIT [{self.cfg.name}] TP triggered ({state.direction}) | "
                f"price={price:.4f} | tp={tp_price:.4f}"
            )
            self._close_position(price, reason="take_profit")
            # Re-arm the ladder in the same direction from the exit price
            state.anchor_price = price

    # ── Position close ──────────────────────────────────────────────────────

    def _close_position(self, price: float, reason: str):
        state = self.state
        cfg = self.cfg

        if state.total_quantity <= 0:
            self._reset_position()
            return

        if state.direction == "LONG":
            order_side, position_side = "SELL", "LONG"
        else:
            order_side, position_side = "BUY", "SHORT"

        quantity = self.client.floor_quantity(state.total_quantity, self._step_size)
        if quantity <= 0:
            self.logger.error(f"[{cfg.name}] Close quantity rounds to zero — clearing state without an order.")
            self._reset_position()
            return

        try:
            pos_side = position_side if self._hedge_mode else None
            order = self.client.place_market_order(
                cfg.symbol, order_side, quantity,
                reduce_only=not self._hedge_mode, position_side=pos_side,
            )
        except Exception as e:
            self.logger.error(f"[{cfg.name}] Close order failed: {e}")
            return

        exit_price, exit_qty = self.client.fill_info(cfg.symbol, order)
        order_id = str(order["orderId"])

        if state.direction == "LONG":
            pnl_usdt = (exit_price - state.average_entry) * state.total_quantity
        else:
            pnl_usdt = (state.average_entry - exit_price) * state.total_quantity
        pnl_pct = (pnl_usdt / state.total_invested * 100) if state.total_invested else 0.0

        log_order(self.logger, order_side, cfg.symbol, exit_price, exit_qty * exit_price, exit_qty, order_id)
        self.logger.info(
            f"CLOSE [{cfg.name}] {state.direction} | reason={reason} | "
            f"PnL={pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}% on margin) | "
            f"avg_entry={state.average_entry:.4f} | exit={exit_price:.4f}"
        )

        self._reset_position()

    def _reset_position(self):
        state = self.state
        state.direction = "NONE"
        state.executed_steps = []
        state.total_invested = 0.0
        state.total_quantity = 0.0
        state.average_entry = 0.0
        state.liquidation_price = None
        state.status = "WAITING"

    def _refresh_liquidation_price(self):
        try:
            pos = self.client.get_position(self.cfg.symbol)
            self.state.liquidation_price = pos["liquidation_price"] or None
        except Exception as e:
            self.logger.warning(f"[{self.cfg.name}] Failed to refresh liquidation price: {e}")

    # ── Status ───────────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        s = self.state
        lines = [
            f"╔══ [{self.cfg.name}] ({self.cfg.coin}) Mixed Engine ══",
            f"║  Regime         : {s.regime}",
            f"║  Active mode    : {s.active_mode}",
            f"║  Direction      : {s.direction}",
            f"║  Anchor price   : {s.anchor_price}",
            f"║  Leverage       : {s.leverage}x",
        ]
        if s.direction != "NONE":
            lines += [
                f"║  Average entry  : {s.average_entry:.4f}",
                f"║  Total invested : {s.total_invested:.2f} USDT (margin)",
                f"║  Total quantity : {s.total_quantity:.6f}",
                f"║  Liquidation    : {s.liquidation_price}",
            ]
        lines.append("╚" + "═" * 40)
        return "\n".join(lines)
