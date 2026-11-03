from __future__ import annotations

import argparse
import logging

from src.config import load_settings
from src.log_redaction import configure_logging
from src.monitor import MonitorService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CROUS listing monitor with Telegram alerts")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send one Telegram healthcheck message during this run",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    settings = load_settings()
    log_level = logging.DEBUG if args.debug else getattr(logging, settings.log_level, logging.INFO)
    configure_logging(log_level, "%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    service = MonitorService(settings)

    if args.once:
        total, new = service.run_cycle(dry_run=args.dry_run, test_telegram=args.test_telegram)
        logging.getLogger(__name__).info("One-shot done: total=%s new=%s", total, new)
        return

    service.run_forever(dry_run=args.dry_run)


if __name__ == "__main__":
    main()