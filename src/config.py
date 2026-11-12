from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    crous_search_urls: tuple[str, ...]
    telegram_bot_token: str
    telegram_chat_ids: tuple[str, ...]
    poll_interval_seconds: int
    poll_jitter_seconds: int
    http_timeout_seconds: int
    http_max_retries: int
    state_db_path: str
    log_level: str
    user_agent: str
    filter_max_price_eur: int | None
    filter_include_keywords: tuple[str, ...]
    filter_idf_max_distance_km: int | None
    heartbeat_enabled: bool
    heartbeat_interval_hours: int
    error_alert_threshold: int
    error_alert_cooldown_minutes: int
    discord_webhook_url: str | None
    discord_mention_user_ids: tuple[str, ...]


def _get_int(name: str, default: int, minimum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    value = int(raw_value)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_search_urls() -> tuple[str, ...]:
    raw_value = os.getenv("CROUS_SEARCH_URLS", "").strip()
    if raw_value:
        urls = [item.strip() for item in raw_value.split(",") if item.strip()]
        if not urls:
            raise ValueError("CROUS_SEARCH_URLS must contain at least one URL")
        return tuple(dict.fromkeys(urls))

    return (_get_required("CROUS_SEARCH_URL"),)


def _get_chat_ids() -> tuple[str, ...]:
    raw_value = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if raw_value:
        chat_ids = [item.strip() for item in raw_value.split(",") if item.strip()]
        if not chat_ids:
            raise ValueError("TELEGRAM_CHAT_IDS must contain at least one chat ID")
        return tuple(dict.fromkeys(chat_ids))

    return (_get_required("TELEGRAM_CHAT_ID"),)


def _get_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "true" if default else "false").strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _get_optional_int(name: str) -> int | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def _get_keywords(name: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return tuple()
    keywords = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    return tuple(dict.fromkeys(keywords))


def load_settings() -> Settings:
    load_dotenv()

    return Settings(
        crous_search_urls=_get_search_urls(),
        telegram_bot_token=_get_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_ids=_get_chat_ids(),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", default=180, minimum=30),
        poll_jitter_seconds=_get_int("POLL_JITTER_SECONDS", default=20, minimum=0),
        http_timeout_seconds=_get_int("HTTP_TIMEOUT_SECONDS", default=15, minimum=5),
        http_max_retries=_get_int("HTTP_MAX_RETRIES", default=2, minimum=0),
        state_db_path=os.getenv("STATE_DB_PATH", "data/state.db").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        user_agent=os.getenv(
            "USER_AGENT", "CROUS-BOT/1.0 (+personal-use; respectful polling)"
        ).strip(),
        filter_max_price_eur=_get_optional_int("FILTER_MAX_PRICE_EUR"),
        filter_include_keywords=_get_keywords("FILTER_INCLUDE_KEYWORDS"),
        filter_idf_max_distance_km=_get_optional_int("FILTER_IDF_MAX_DISTANCE_KM"),
        heartbeat_enabled=_get_bool("HEARTBEAT_ENABLED", default=False),
        heartbeat_interval_hours=_get_int("HEARTBEAT_INTERVAL_HOURS", default=168, minimum=1),
        error_alert_threshold=_get_int("ERROR_ALERT_THRESHOLD", default=3, minimum=1),
        error_alert_cooldown_minutes=_get_int(
            "ERROR_ALERT_COOLDOWN_MINUTES", default=180, minimum=1
        ),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None,
        discord_mention_user_ids=tuple(
            item.strip() for item in os.getenv("DISCORD_MENTION_USER_IDS", "").split(",") if item.strip()
        ),
    )