from __future__ import annotations

import html
import logging
import time

import requests

from src.listing_details import ListingDetails, format_minutes
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

    def send_new_listing(self, listing: Listing, headline: str, details: ListingDetails) -> list[tuple[str, int]]:
        """Returns (chat_id, message_id) per chat, kept so the alert can be removed once the listing is gone."""
        message = format_listing_message(listing, headline, details)
        if not listing.image_url:
            return self._send_message(message, parse_mode="HTML")
        sent: list[tuple[str, int]] = []
        for chat_id in self.chat_ids:
            payload = {"chat_id": chat_id, "photo": listing.image_url, "caption": message, "parse_mode": "HTML"}
            response = self._post_with_retry("sendPhoto", payload)
            if response.status_code >= 400:
                # Telegram fetches the photo itself and rejects URLs it cannot load; the text still matters.
                LOGGER.warning("Telegram sendPhoto failed status=%s, sending text only", response.status_code)
                sent.extend(self._send_message(message, chat_id=chat_id, parse_mode="HTML"))
                continue
            message_id = _message_id(response)
            if message_id is not None:
                sent.append((chat_id, message_id))
        return sent

    def remove_listing_message(self, chat_id: str, message_id: int) -> bool:
        response = self._post_with_retry("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        if response.status_code < 400:
            return True
        # Bots may only delete messages younger than 48h; flag older alerts with a reply instead.
        response = self._post_with_retry(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "❌ Ce logement n'est plus disponible.",
                "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True},
            },
        )
        if response.status_code >= 400:
            LOGGER.warning("Telegram could not remove or flag message status=%s", response.status_code)
            return False
        return True

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

    def _send_message(
        self, message: str, chat_id: str | None = None, parse_mode: str | None = None
    ) -> list[tuple[str, int]]:
        target_chat_ids = (chat_id,) if chat_id else self.chat_ids
        sent: list[tuple[str, int]] = []
        for target_chat_id in target_chat_ids:
            payload = {
                "chat_id": target_chat_id,
                "text": message,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            response = self._post_with_retry("sendMessage", payload)
            if response.status_code >= 400:
                LOGGER.error(
                    "Telegram send failed for chat_id=%s status=%s body=%s",
                    target_chat_id,
                    response.status_code,
                    response.text,
                )
                response.raise_for_status()
            message_id = _message_id(response)
            if message_id is not None:
                sent.append((target_chat_id, message_id))
        return sent

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


def _message_id(response: requests.Response) -> int | None:
    try:
        return int(response.json()["result"]["message_id"])
    except (ValueError, KeyError, TypeError):
        return None


def _get_retry_after(response: requests.Response) -> int:
    try:
        retry_after = response.json().get("parameters", {}).get("retry_after")
    except ValueError:
        retry_after = None
    return int(retry_after) if isinstance(retry_after, (int, float)) else 5


def format_listing_message(listing: Listing, headline: str, details: ListingDetails) -> str:
    """HTML (parse_mode=HTML) mirror of the Discord embed. Telegram caps photo captions at 1024
    visible characters; link URLs do not count, and this stays well under it."""

    def link(label: str, url: str) -> str:
        return f'<a href="{html.escape(url)}">{html.escape(label)}</a>'

    lines = [f"<b>{html.escape(headline)}</b>", "", f"<b>{html.escape(listing.title)}</b>"]
    if listing.price_eur is not None:
        lines.append(f"💶 Prix: <b>{listing.price_eur} €</b> / mois")
    if details.postcode:
        lines.append(f"📮 Code postal: <code>{details.postcode}</code> · {html.escape(details.department or '')}")
    if listing.city:
        lines.append(f"🏙️ Ville: {html.escape(listing.city.title())}")
    if details.address and details.maps_url:
        lines.append(f"📍 Adresse: {link(details.address, details.maps_url)}")
    if details.distance_from_paris_km is not None:
        lines.append(f"📏 Distance: ~{details.distance_from_paris_km:.0f} km de Paris centre (à vol d'oiseau)")
    if details.transit_minutes_from_42 is not None:
        lines.append(
            f"🏫 Depuis 42 Paris: ~{format_minutes(details.transit_minutes_from_42)} en transports (semaine, départ 8h)"
        )
    if details.routes:
        lines.append("🧭 Itinéraire depuis ma position: " + " · ".join(link(label, url) for label, url in details.routes))
    lines.extend(["", f"🔗 {link('Voir et réserver sur le CROUS', listing.url)}"])
    if listing.external_id:
        lines.append(f"<i>CROUS · annonce n°{listing.external_id}</i>")
    return "\n".join(lines)
