from __future__ import annotations

import logging
import os

# Env vars whose values must never reach logs. Request URLs embed some of them (Telegram puts the
# bot token in the path, a Discord webhook URL is itself the credential), and requests echoes the
# URL in HTTPError / ConnectionError messages, which end up in logged tracebacks.
SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_WEBHOOK_URL",
    "CRON_SECRET",
    "KV_REST_API_TOKEN",
    "UPSTASH_REDIS_REST_TOKEN",
)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        secrets = []
        for name in SECRET_ENV_VARS:
            value = os.getenv(name, "").strip()
            if len(value) >= 8:
                secrets.append(value)
                # A webhook URL may be logged without its scheme, or just its token segment.
                if "/" in value:
                    secrets.append(value.rstrip("/").rsplit("/", 1)[-1])
        self.secrets = sorted(set(secrets), key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text


def configure_logging(level: int, fmt: str) -> None:
    logging.basicConfig(level=level, format=fmt)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(fmt))
