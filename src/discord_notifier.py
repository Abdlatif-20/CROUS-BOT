from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

from src.listing_details import ListingDetails, format_minutes
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

    def send_new_listing(self, listing: Listing, headline: str, details: ListingDetails) -> str | None:
        """Returns the Discord message id, kept so the alert can be deleted once the listing is gone."""
        fields = []
        if listing.price_eur is not None:
            fields.append({"name": "💶 Prix", "value": f"**{listing.price_eur} €** / mois", "inline": True})
        if details.postcode:
            fields.append(
                {"name": "📮 Code postal", "value": f"`{details.postcode}` · {details.department}", "inline": True}
            )
        if listing.city:
            fields.append({"name": "🏙️ Ville", "value": listing.city.title(), "inline": True})
        if details.address:
            fields.append(
                {"name": "📍 Adresse", "value": f"[{details.address}]({details.maps_url})", "inline": False}
            )
        if details.distance_from_paris_km is not None:
            fields.append(
                {
                    "name": "📏 Distance",
                    "value": f"~{details.distance_from_paris_km:.0f} km de Paris centre (à vol d'oiseau)",
                    "inline": False,
                }
            )
        if details.transit_minutes_from_42 is not None:
            fields.append(
                {
                    "name": "🏫 Depuis 42 Paris",
                    "value": f"~{format_minutes(details.transit_minutes_from_42)} en transports (semaine, départ 8h)",
                    "inline": False,
                }
            )
        if details.routes:
            routes = " · ".join(f"[{label}]({url})" for label, url in details.routes)
            fields.append({"name": "🧭 Itinéraire depuis ma position", "value": routes, "inline": False})
        fields.append({"name": "🔗 Annonce", "value": f"[Voir et réserver sur le CROUS]({listing.url})", "inline": False})

        embed = {
            "title": listing.title[:256],
            "url": listing.url,
            "color": EMBED_COLOR,
            "fields": fields,
            "footer": {"text": f"CROUS · annonce n°{listing.external_id}" if listing.external_id else "CROUS"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if listing.image_url:
            embed["image"] = {"url": listing.image_url}
        # @everyone pings every member; allowed_mentions is required or Discord renders it as plain text.
        return self._send(
            {
                "content": f"@everyone {headline}",
                "embeds": [embed],
                "allowed_mentions": {"parse": ["everyone"]},
            }
        )

    def send_healthcheck(self) -> None:
        self._send({"content": "✅ CROUS monitor healthcheck: Discord notifications are working."})

    def delete_message(self, message_id: str) -> bool:
        # Webhooks can delete their own messages at any age; 404 means someone already removed it.
        response = self._request_with_retry("DELETE", self._message_url(message_id))
        if response.status_code >= 400 and response.status_code != 404:
            LOGGER.warning("Discord delete failed status=%s", response.status_code)
            return False
        return True

    def _message_url(self, message_id: str) -> str:
        # Keep any query (e.g. ?thread_id=) after the /messages/<id> path.
        parts = urlsplit(self.webhook_url)
        return urlunsplit(parts._replace(path=f"{parts.path.rstrip('/')}/messages/{message_id}"))

    def _send(self, payload: dict) -> str | None:
        # wait=true makes Discord return the created message (and its id) instead of 204.
        response = self._request_with_retry("POST", self.webhook_url, payload, params={"wait": "true"})
        if response.status_code >= 400:
            LOGGER.error(
                "Discord send failed status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            response.raise_for_status()
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError):
            return None

    def _request_with_retry(
        self, method: str, url: str, payload: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        response = self._request_throttled(method, url, payload, params)
        if response.status_code == 429:
            retry_after = _get_retry_after(response)
            LOGGER.warning("Discord rate limit hit, retrying in %ss", retry_after)
            time.sleep(retry_after)
            response = self._request_throttled(method, url, payload, params)
        return response

    def _request_throttled(
        self, method: str, url: str, payload: dict | None, params: dict | None
    ) -> requests.Response:
        wait = MIN_SECONDS_BETWEEN_MESSAGES - (time.monotonic() - self._last_sent_at)
        if wait > 0:
            time.sleep(wait)
        response = requests.request(method, url, json=payload, params=params, timeout=self.timeout_seconds)
        self._last_sent_at = time.monotonic()
        return response


def _get_retry_after(response: requests.Response) -> float:
    try:
        retry_after = response.json().get("retry_after")
    except ValueError:
        retry_after = None
    return float(retry_after) if isinstance(retry_after, (int, float)) else 2.0
