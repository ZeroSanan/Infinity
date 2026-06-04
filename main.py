"""
Dynamic Spot DCA System — Entry Point
======================================
Usage:
    python main.py                       # run all enabled coins
    python main.py --coin BTC            # run a single coin
    python main.py --coin BTC --set-ref  # set reference = current price, then run
    python main.py --coin BTC --ref 95000  # set manual reference price, then run
    python main.py --status              # print status summary and exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

from dotenv import load_dotenv

from core.binance_client import BinanceSpotClient
from core.dca_engine import DCAEngine
from core.logger import get_logger
from models.dca_config import CoinConfig

load_dotenv()
logger = get_logger("main")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "coins.json")


def load_configs() -> list[CoinConfig]:
    with open(CONFIG_PATH, "r") as f:
        data = json.load(f)

    configs = []
    for item in data["coins"]:
        cfg = CoinConfig(
            id=item["id"],
            name=item["name"],
            coin=item["coin"],
            symbol=item["symbol"],
            enabled=item.get("enabled", True),
            step_count=item["step_count"],
            dump_levels=item["dump_levels"],
            order_sizes=item["order_sizes"],
            take_profit_percent=item["take_profit_percent"],
            reference_price=item.get("reference_price"),
        )
        cfg.validate()
        configs.append(cfg)
    return configs


def build_client() -> BinanceSpotClient:
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret_key = os.getenv("BINANCE_SECRET_KEY", "")
    testnet = os.getenv("USE_TESTNET", "false").lower() == "true"

    if not api_key or not secret_key:
        logger.error(
            "BINANCE_API_KEY and BINANCE_SECRET_KEY must be set in your .env file."
        )
        sys.exit(1)

    return BinanceSpotClient(api_key, secret_key, testnet=testnet)


def run_engine(engine: DCAEngine):
    """Thread target."""
    engine.run()


def main():
    parser = argparse.ArgumentParser(description="Dynamic Spot DCA System")
    parser.add_argument("--coin", type=str, help="Run a single coin (e.g. BTC)")
    parser.add_argument("--set-ref", action="store_true",
                        help="Set reference price = current market price before running")
    parser.add_argument("--ref", type=float,
                        help="Set a manual reference price before running")
    parser.add_argument("--status", action="store_true",
                        help="Print position status summary and exit")
    args = parser.parse_args()

    configs = load_configs()

    # Filter to a single coin if requested
    if args.coin:
        configs = [c for c in configs if c.coin.upper() == args.coin.upper()]
        if not configs:
            logger.error(f"Coin '{args.coin}' not found in config/coins.json")
            sys.exit(1)
    else:
        configs = [c for c in configs if c.enabled]

    if not configs:
        logger.error("No enabled coins found. Check config/coins.json.")
        sys.exit(1)

    client = build_client()
    engines = [DCAEngine(cfg, client) for cfg in configs]

    # ── Status mode ─────────────────────────────────────────────────────
    if args.status:
        for eng in engines:
            print(eng.status_summary())
        return

    # ── Set reference price ──────────────────────────────────────────────────
    for eng in engines:
        if args.ref:
            eng.set_reference_price(args.ref)
        elif args.set_ref:
            eng.set_reference_to_current()

    # ── Run engines ─────────────────────────────────────────────────────
    if len(engines) == 1:
        engines[0].run()
    else:
        threads = [threading.Thread(target=run_engine, args=(e,), daemon=True)
                   for e in engines]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            logger.info("Shutting down all engines…")


if __name__ == "__main__":
    main()
