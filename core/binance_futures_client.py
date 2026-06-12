"""
Thin wrapper around python-binance USD-M Futures (USDT-margined perpetuals).
Used by the Mixed long/short DCA engine for leveraged long AND short positions.

Handles authentication, retries, leverage/margin setup, position lookups,
and market order placement (open + reduce-only close), in both One-way and
Hedge position modes.
"""
from typing import Any, Dict, Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from core.binance_client import _retry
from core.logger import get_logger

logger = get_logger("binance_futures_client")

# Binance error code for "No need to change margin type."
_MARGIN_TYPE_UNCHANGED = -4046


class BinanceFuturesClient:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.testnet = testnet
        self.client = Client(api_key, secret_key, testnet=testnet)
        if testnet:
            logger.warning("⚠️  Running on FUTURES TESTNET — no real funds will be used.")
        else:
            logger.info("✅  Connected to Binance LIVE USD-M Futures.")

    # ── Market data ────────────────────────────────────────────────────────

    def get_mark_price(self, symbol: str) -> float:
        """Fetch the current mark price for a perpetual symbol."""
        data = _retry(self.client.futures_mark_price, symbol=symbol)
        return float(data["markPrice"])

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        info = _retry(self.client.futures_exchange_info)
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        raise ValueError(f"Symbol {symbol} not found on Binance USD-M Futures.")

    def get_step_size(self, symbol: str) -> float:
        """Return the minimum quantity increment for a futures symbol."""
        info = self.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
        return 1e-8  # fallback

    def get_min_notional(self, symbol: str) -> float:
        """Return the minimum order notional (USDT) for a futures symbol."""
        info = self.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "MIN_NOTIONAL":
                return float(f["notional"])
        return 0.0

    # ── Account / position ────────────────────────────────────────────────

    def get_balance(self, asset: str = "USDT") -> float:
        """Return futures wallet balance for an asset."""
        balances = _retry(self.client.futures_account_balance)
        for b in balances:
            if b["asset"] == asset:
                return float(b["balance"])
        return 0.0

    def get_position(self, symbol: str) -> Dict[str, Any]:
        """
        Return the (one-way mode) net position for a symbol.

        Returns dict with: position_amt (signed; >0 long, <0 short),
        entry_price, unrealized_pnl, liquidation_price, leverage, margin_type.
        """
        positions = _retry(self.client.futures_position_information, symbol=symbol)
        for p in positions:
            if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0:
                return {
                    "position_amt":      float(p["positionAmt"]),
                    "entry_price":       float(p["entryPrice"]),
                    "unrealized_pnl":    float(p["unRealizedProfit"]),
                    "liquidation_price": float(p["liquidationPrice"]),
                    "leverage":          int(p["leverage"]),
                    "margin_type":       p.get("marginType"),
                }
        # No open position — still return leverage/margin info from first match
        for p in positions:
            if p["symbol"] == symbol:
                return {
                    "position_amt":      0.0,
                    "entry_price":       0.0,
                    "unrealized_pnl":    0.0,
                    "liquidation_price": 0.0,
                    "leverage":          int(p["leverage"]),
                    "margin_type":       p.get("marginType"),
                }
        return {"position_amt": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0,
                "liquidation_price": 0.0, "leverage": 1, "margin_type": None}

    def get_position_mode(self) -> bool:
        """Returns True if the account uses Hedge Mode (dual-side positions)."""
        try:
            data = _retry(self.client.futures_get_position_mode)
            return bool(data.get("dualSidePosition", False))
        except Exception as e:
            logger.warning(f"get_position_mode failed, assuming One-way: {e}")
            return False

    # ── Setup ──────────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int):
        try:
            _retry(self.client.futures_change_leverage, symbol=symbol, leverage=leverage)
            logger.info(f"Leverage set | {symbol} | {leverage}x")
        except BinanceAPIException as e:
            logger.warning(f"set_leverage failed | {symbol} | {e}")

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            _retry(self.client.futures_change_margin_type, symbol=symbol, marginType=margin_type)
            logger.info(f"Margin type set | {symbol} | {margin_type}")
        except BinanceAPIException as e:
            if e.code == _MARGIN_TYPE_UNCHANGED:
                return
            logger.warning(f"set_margin_type failed | {symbol} | {e}")

    # ── Orders ─────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        position_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place a futures MARKET order.

        side:          "BUY" or "SELL"
        reduce_only:   set True when closing/reducing a position (One-way mode only)
        position_side: "LONG" or "SHORT" — required in Hedge mode, omit in One-way mode
        """
        params: Dict[str, Any] = dict(symbol=symbol, side=side, type="MARKET", quantity=quantity)
        if position_side:
            params["positionSide"] = position_side
        elif reduce_only:
            params["reduceOnly"] = True

        logger.info(f"{side} placing futures market order | {symbol} | qty={quantity} | params={params}")
        order = _retry(self.client.futures_create_order, **params)
        logger.info(f"{side} confirmed | orderId={order['orderId']} | status={order['status']}")
        return order

    def get_order(self, symbol: str, order_id) -> Dict[str, Any]:
        return _retry(self.client.futures_get_order, symbol=symbol, orderId=order_id)

    def is_order_filled(self, symbol: str, order_id) -> bool:
        order = self.get_order(symbol, order_id)
        return order["status"] == "FILLED"

    def fill_info(self, symbol: str, order: Dict[str, Any]) -> tuple:
        """
        Returns (fill_price, fill_qty) for a futures order response.
        Falls back to querying the order if avgPrice/executedQty are not
        yet populated in the immediate create_order response.
        """
        avg_price = float(order.get("avgPrice", 0) or 0)
        exec_qty = float(order.get("executedQty", 0) or 0)
        if avg_price == 0 or exec_qty == 0:
            o = self.get_order(symbol, order["orderId"])
            avg_price = float(o.get("avgPrice", 0) or 0)
            exec_qty = float(o.get("executedQty", 0) or 0)
        return avg_price, exec_qty

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def floor_quantity(quantity: float, step_size: float) -> float:
        """Floor a quantity to the exchange's lot step size."""
        precision = len(str(step_size).rstrip("0").split(".")[-1])
        factor = 10 ** precision
        return int(quantity * factor) / factor
