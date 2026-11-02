from __future__ import annotations

import logging
import time

import requests

from src.models import Listing

LOGGER = logging.getLogger(__name__)

# Discord webhooks allow about 5 requests per 2 seconds; stay well under it.
MIN_SECONDS_BETWEEN_MESSAGES = 1.0
EMBED_COLOR = 0x000091  # DSFR blue, matches the CROUS site


class DiscordNotifier:
    def __init__(self, webhook_url: str, timeout_seconds: int) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self._last_sent_at = 0.0

    def send_new_listing(self, listing: Listing) -> None:
        fields = []
        if listing.price_eur is not None:
            fields.append({"name": "Prix", "value": f"{listing.price_eur} €", "inline": True})
        if listing.city:
            fields.append({"name": "Ville", "value": listing.city, "inline": True})
        if listing.residence:
            fields.append({"name": "Adresse", "value": listing.residence, "inline": False})

        embed = {
            "title": listing.title[:256],
            "url": listing.url,
            "color": EMBED_COLOR,
            "fields": fields,
        }
        self._send({"content": "🏠 Nouveau logement CROUS détecté", "embeds": [embed]})

    def send_healthcheck(self) -> None:
        self._send({"content": "✅ CROUS monitor healthcheck: Discord notifications are working."})

    def _send(self, payload: dict) -> None:
        response = self._post_throttled(payload)
        if response.status_code == 429:
            retry_after = _get_retry_after(response)
            LOGGER.warning("Discord rate limit hit, retrying in %ss", retry_after)
            time.sleep(retry_after)
            response = self._post_throttled(payload)
        if response.status_code >= 400:
            LOGGER.error(
                "Discord send failed status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            response.raise_for_status()

    def _post_throttled(self, payload: dict) -> requests.Response:
        wait = MIN_SECONDS_BETWEEN_MESSAGES - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            time.sleep(wait)
        response = requests.post(self.webhook_url, json=payload, timeout=self.timeout_seconds)
        self._last_sent_at = time.monotonic()
        return response


def _get_retry_after(response: requests.Response) -> float:
    try:
        retry_after = response.json().get("retry_after")
    except ValueError:
        retry_after = None
    return float(retry_after) if isinstance(retry_after, (int, float)) else 2.0
