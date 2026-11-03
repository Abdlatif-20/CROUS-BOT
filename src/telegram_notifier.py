from __future__ import annotations

import logging
import time

import requests

from src.models import Listing

LOGGER = logging.getLogger(__name__)

# Telegram allows roughly one message per second per chat; bursts beyond that get HTTP 429.
MIN_SECONDS_BETWEEN_MESSAGES = 1.1


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids: tuple[str, ...], timeout_seconds: int) -> None:
        self.chat_ids = chat_ids
        self.timeout_seconds = timeout_seconds
        self.base_endpoint = f"https://api.telegram.org/bot{bot_token}"
        self._last_sent_at = 0.0

    def send_new_listing(self, listing: Listing) -> None:
        message = format_listing_message(listing)
        if not listing.image_url:
            self._send_message(message)
            return
        for chat_id in self.chat_ids:
            payload = {"chat_id": chat_id, "photo": listing.image_url, "caption": message}
            response = self._post_with_retry("sendPhoto", payload)
            if response.status_code >= 400:
                # Telegram fetches the photo itself and rejects URLs it cannot load; the text still matters.
                LOGGER.warning("Telegram sendPhoto failed status=%s, sending text only", response.status_code)
                self._send_message(message, chat_id=chat_id)

    def send_healthcheck(self) -> None:
        self._send_message("✅ CROUS monitor healthcheck: Telegram notifications are working.")

    def send_heartbeat(self) -> None:
        self._send_message("💓 CROUS monitor heartbeat: workflow is running normally.")

    def send_error_alert(self, failure_count: int, error_text: str) -> None:
        message = (
            "⚠️ CROUS monitor warning\n\n"
            f"Consecutive failures: {failure_count}\n"
            f"Latest error: {error_text[:300]}"
        )
        self._send_message(message)

    def send_text(self, text: str, chat_id: str | None = None) -> None:
        self._send_message(text, chat_id=chat_id)

    def get_updates(self, offset: int, limit: int = 20) -> list[dict]:
        payload = {
            "offset": offset,
            "limit": limit,
            "allowed_updates": ["message", "edited_message"],
        }
        response = requests.get(
            f"{self.base_endpoint}/getUpdates",
            params=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            LOGGER.warning("Telegram getUpdates returned non-ok payload")
            return []
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def _send_message(self, message: str, chat_id: str | None = None) -> None:
        target_chat_ids = (chat_id,) if chat_id else self.chat_ids
        for target_chat_id in target_chat_ids:
            payload = {
                "chat_id": target_chat_id,
                "text": message,
                "disable_web_page_preview": True,
            }
            response = self._post_with_retry("sendMessage", payload)
            if response.status_code >= 400:
                LOGGER.error(
                    "Telegram send failed for chat_id=%s status=%s body=%s",
                    target_chat_id,
                    response.status_code,
                    response.text,
                )
                response.raise_for_status()


    def _post_with_retry(self, method: str, payload: dict) -> requests.Response:
        response = self._post_throttled(method, payload)
        if response.status_code == 429:
            retry_after = _get_retry_after(response)
            LOGGER.warning("Telegram rate limit hit, retrying in %ss", retry_after)
            time.sleep(retry_after)
            response = self._post_throttled(method, payload)
        return response

    def _post_throttled(self, method: str, payload: dict) -> requests.Response:
        wait = MIN_SECONDS_BETWEEN_MESSAGES - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            time.sleep(wait)
        response = requests.post(f"{self.base_endpoint}/{method}", json=payload, timeout=self.timeout_seconds)
        self._last_sent_at = time.monotonic()
        return response


def _get_retry_after(response: requests.Response) -> int:
    try:
        retry_after = response.json().get("parameters", {}).get("retry_after")
    except ValueError:
        retry_after = None
    return int(retry_after) if isinstance(retry_after, (int, float)) else 5


def format_listing_message(listing: Listing) -> str:
    lines = ["🏠 Nouveau logement CROUS détecté", "", f"Titre: {listing.title}"]
    if listing.price_eur is not None:
        lines.append(f"Prix: {listing.price_eur} €")
    if listing.city:
        lines.append(f"Ville: {listing.city}")
    if listing.residence:
        lines.append(f"Résidence: {listing.residence}")
    lines.extend(["", f"Voir: {listing.url}"])
    return "\n".join(lines)