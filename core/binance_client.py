"""
Thin wrapper around python-binance.
Handles authentication, retries, and error normalisation.
"""
import time
import os
from typing import Optional, Dict, Any

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from core.logger import get_logger

# Human-readable translations for common Binance error codes
_BINANCE_ERROR_HINTS = {
    -2015: (
        "API key rejected by Binance (code -2015). "
        "Most likely cause: your IP address is not whitelisted. "
        "Fix: go to Binance → API Management → Edit → IP Access → add your IP, "
        "or set 'Unrestricted' if you don't need IP locking. "
        "Also check: correct key type (Testnet vs Live)."
    ),
    -2014: "API key format is invalid — check you copied the full key.",
    -1102: "Missing or malformed parameter in the request.",
    -1021: "Timestamp out of sync — check your system clock.",
    -1100: "Illegal characters in the API key or secret.",
}

logger = get_logger("binance_client")

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def _retry(fn, *args, **kwargs):
    """Call fn with retries on transient errors."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except BinanceRequestException as e:
            logger.warning(f"Request error (attempt {attempt}/{MAX_RETRIES}): {e}")
            last_exc = e
        except BinanceAPIException as e:
            # -1021 = timestamp out of sync — retry; others re-raise with hint
            if e.code == -1021:
                logger.warning(f"Timestamp error, retrying ({attempt}/{MAX_RETRIES})…")
                last_exc = e
            else:
                hint = _BINANCE_ERROR_HINTS.get(e.code)
                if hint:
                    raise BinanceAPIException(e.response, e.status_code, f'{{"code":{e.code},"msg":"{hint}"}}') from e
                raise
        time.sleep(RETRY_DELAY * attempt)
    raise last_exc


class BinanceSpotClient:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.testnet = testnet
        self.client = Client(api_key, secret_key, testnet=testnet)
        if testnet:
            logger.warning("⚠️  Running on TESTNET — no real funds will be used.")
        else:
            logger.info("✅  Connected to Binance LIVE Spot.")

    # ── Market Data ─────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """Fetch the latest ticker price for a symbol."""
        data = _retry(self.client.get_symbol_ticker, symbol=symbol)
        return float(data["price"])

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Return exchange info for a symbol (lot size, min notional, etc.)."""
        info = _retry(self.client.get_symbol_info, symbol=symbol)
        if info is None:
            raise ValueError(f"Symbol {symbol} not found on Binance.")
        return info

    def get_step_size(self, symbol: str) -> float:
        """Return the minimum quantity increment for a symbol."""
        info = self.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
        return 1e-8  # fallback

    # ── Account ─────────────────────────────────────────────────────────────

    def get_balance(self, asset: str) -> float:
        """Return free balance for an asset (e.g. 'USDT', 'BTC')."""
        account = _retry(self.client.get_account)
        for b in account["balances"]:
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    # ── Orders ──────────────────────────────────────────────────────────────

    def place_market_buy(self, symbol: str, usdt_amount: float) -> Dict[str, Any]:
        """
        Place a market buy order using a USDT quote amount.
        Returns the full order response.
        """
        logger.info(f"BUY  placing market buy | {symbol} | {usdt_amount:.2f} USDT")
        order = _retry(
            self.client.order_market_buy,
            symbol=symbol,
            quoteOrderQty=round(usdt_amount, 2),
        )
        logger.info(f"BUY  confirmed | orderId={order['orderId']} | status={order['status']}")
        return order

    def place_market_sell(self, symbol: str, quantity: float, step_size: float) -> Dict[str, Any]:
        """
        Place a market sell order for the full quantity.
        Quantity is floored to the symbol's lot step size.
        """
        qty = self._floor_quantity(quantity, step_size)
        logger.info(f"SELL placing market sell | {symbol} | qty={qty}")
        order = _retry(
            self.client.order_market_sell,
            symbol=symbol,
            quantity=qty,
        )
        logger.info(f"SELL confirmed | orderId={order['orderId']} | status={order['status']}")
        return order

    def get_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """Fetch a specific order by ID."""
        return _retry(self.client.get_order, symbol=symbol, orderId=order_id)

    def is_order_filled(self, symbol: str, order_id: int) -> bool:
        order = self.get_order(symbol, order_id)
        return order["status"] == "FILLED"

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _floor_quantity(quantity: float, step_size: float) -> float:
        """Floor a quantity to the exchange's lot step size."""
        precision = len(str(step_size).rstrip("0").split(".")[-1])
        factor = 10 ** precision
        return int(quantity * factor) / factor

    @staticmethod
    def parse_fill_price(order: Dict[str, Any]) -> float:
        """Calculate the weighted average fill price from an order response."""
        fills = order.get("fills", [])
        if not fills:
            return float(order.get("price", 0))
        total_qty = sum(float(f["qty"]) for f in fills)
        total_val = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        return total_val / total_qty if total_qty else 0.0

    @staticmethod
    def parse_fill_quantity(order: Dict[str, Any]) -> float:
        """Return total executed quantity from an order response."""
        return float(order.get("executedQty", 0))
