from __future__ import annotations

import logging
import time

import requests

from src.models import Listing
from src.telegram_notifier import format_listing_message

LOGGER = logging.getLogger(__name__)

CALLMEBOT_ENDPOINT = "https://api.callmebot.com/whatsapp.php"
# CallMeBot is a free shared service; space out messages to avoid being throttled.
MIN_SECONDS_BETWEEN_MESSAGES = 3.0


class WhatsAppNotifier:
    def __init__(self, phone: str, apikey: str, timeout_seconds: int) -> None:
        self.phone = phone
        self.apikey = apikey
        self.timeout_seconds = timeout_seconds
        self._last_sent_at = 0.0

    def send_new_listing(self, listing: Listing) -> None:
        self.send_text(format_listing_message(listing))

    def send_healthcheck(self) -> None:
        self.send_text("✅ CROUS monitor healthcheck: WhatsApp notifications are working.")

    def send_text(self, text: str) -> None:
        wait = MIN_SECONDS_BETWEEN_MESSAGES - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            time.sleep(wait)

        response = requests.get(
            CALLMEBOT_ENDPOINT,
            params={"phone": self.phone, "text": text, "apikey": self.apikey},
            timeout=self.timeout_seconds,
        )
        self._last_sent_at = time.monotonic()

        body = response.text.lower()
        if response.status_code >= 400 or "error" in body or "invalid" in body:
            LOGGER.error(
                "WhatsApp send failed status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            raise RuntimeError(f"CallMeBot send failed (status={response.status_code})")
