"""
Logging module — writes to both console (coloured) and log files.
"""
import logging
import os
from datetime import datetime
from colorama import Fore, Style, init

init(autoreset=True)

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    Fore.CYAN,
        logging.INFO:     Fore.WHITE,
        logging.WARNING:  Fore.YELLOW,
        logging.ERROR:    Fore.RED,
        logging.CRITICAL: Fore.MAGENTA,
    }
    SPECIAL = {
        "BUY":    Fore.GREEN + Style.BRIGHT,
        "SELL":   Fore.RED   + Style.BRIGHT,
        "PROFIT": Fore.GREEN + Style.BRIGHT,
        "WAIT":   Fore.CYAN,
        "RESET":  Fore.YELLOW + Style.BRIGHT,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, Fore.WHITE)
        msg = super().format(record)
        for keyword, kcolor in self.SPECIAL.items():
            if keyword in msg:
                color = kcolor
                break
        return color + msg + Style.RESET_ALL


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # ── Console handler (coloured) ──────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(ColorFormatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(ch)

    # ── File handler (plain text, one file per day) ─────────────────────────
    today = datetime.utcnow().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"dca_{today}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    return logger


# Convenience helpers used throughout the system
def log_order(logger, action: str, symbol: str, price: float,
              usdt: float, qty: float, order_id: str):
    logger.info(
        f"{action} | {symbol} | price={price:.4f} | "
        f"usdt={usdt:.2f} | qty={qty:.6f} | id={order_id}"
    )


def log_position(logger, symbol: str, avg_entry: float,
                 total_qty: float, total_invested: float,
                 tp_price: float, steps_done: int):
    logger.info(
        f"POSITION | {symbol} | avg_entry={avg_entry:.4f} | "
        f"qty={total_qty:.6f} | invested={total_invested:.2f} USDT | "
        f"tp_target={tp_price:.4f} | steps={steps_done}"
    )
