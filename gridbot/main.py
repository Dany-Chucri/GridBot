"""Entry point for GridBot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from gridbot.config import load_config
from gridbot.supervisor import Supervisor

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    """Configure structured JSON-ish logging (UTC, millisecond precision)."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = lambda *_: __import__("time").gmtime()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hyperliquid grid trading bot")
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=Path("config/gridbot.yaml"),
        help="Path to config file (default: config/gridbot.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use testnet endpoints",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config, testnet=args.testnet)
    supervisor = Supervisor(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, supervisor.request_shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: supervisor.request_shutdown())

    await supervisor.run()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger.info("GridBot %s starting", __import__("gridbot").__version__)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
