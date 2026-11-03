from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests

from src.geo_filter import CEDEX_REGEX, POSTCODE_REGEX, distance_between_km, geocode
from src.models import Listing
from src.state_store import StateStore

LOGGER = logging.getLogger(__name__)

# Discord webhooks allow about 5 requests per 2 seconds; stay well under it.
MIN_SECONDS_BETWEEN_MESSAGES = 1.0
EMBED_COLOR = 0x000091  # DSFR blue, matches the CROUS site
PARIS_CENTER = (2.3488, 48.8534)  # (lon, lat), Hôtel de Ville
MAPS_SEARCH_URL = "https://www.google.com/maps/search/?api=1&query={}"
# No origin: Google Maps starts from the viewer's current location and shows time and distance.
MAPS_DIRECTIONS_URL = "https://www.google.com/maps/dir/?api=1&destination={}&travelmode={}"
DEPARTMENT_NAMES = {
    "75": "Paris",
    "77": "Seine-et-Marne",
    "78": "Yvelines",
    "91": "Essonne",
    "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne",
    "95": "Val-d'Oise",
    "02": "Aisne",
    "10": "Aube",
    "27": "Eure",
    "28": "Eure-et-Loir",
    "45": "Loiret",
    "51": "Marne",
    "60": "Oise",
    "89": "Yonne",
}


class DiscordNotifier:
    def __init__(
        self, webhook_url: str, timeout_seconds: int, state_store: StateStore | None = None
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        # Used to geocode addresses for the distance field; None skips it.
        self.state_store = state_store
        self._last_sent_at = 0.0

    def send_new_listing(self, listing: Listing) -> None:
        address = listing.residence or listing.city
        postcode = POSTCODE_REGEX.search(address) if address else None

        fields = []
        if listing.price_eur is not None:
            fields.append({"name": "💶 Prix", "value": f"**{listing.price_eur} €** / mois", "inline": True})
        if postcode:
            department = postcode.group(2)
            name = DEPARTMENT_NAMES.get(department, f"dépt {department}")
            fields.append(
                {"name": "📮 Code postal", "value": f"`{postcode.group(1)}` · {name}", "inline": True}
            )
        if listing.city:
            fields.append({"name": "🏙️ Ville", "value": listing.city.title(), "inline": True})
        if address:
            query = quote_plus(address)
            fields.append(
                {
                    "name": "📍 Adresse",
                    "value": f"[{address}]({MAPS_SEARCH_URL.format(query)})",
                    "inline": False,
                }
            )
            distance = self._distance_from_paris_km(address, postcode)
            if distance is not None:
                fields.append(
                    {
                        "name": "📏 Distance",
                        "value": f"~{distance:.0f} km de Paris centre (à vol d'oiseau)",
                        "inline": False,
                    }
                )
            routes = " · ".join(
                f"[{label}]({MAPS_DIRECTIONS_URL.format(query, mode)})"
                for label, mode in [
                    ("🚆 Transports", "transit"),
                    ("🚲 Vélo", "bicycling"),
                    ("🚗 Voiture", "driving"),
                    ("🚶 À pied", "walking"),
                ]
            )
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
        self._send({"content": "🏠 Nouveau logement CROUS détecté", "embeds": [embed]})

    def _distance_from_paris_km(self, address: str, postcode: re.Match[str] | None) -> float | None:
        if self.state_store is None:
            return None
        coordinates = geocode(address, self.state_store, self.timeout_seconds)
        if coordinates is None and postcode:
            town = CEDEX_REGEX.sub("", postcode.group(3))
            coordinates = geocode(f"{postcode.group(1)} {town}", self.state_store, self.timeout_seconds)
        if coordinates is None:
            return None
        return distance_between_km(coordinates, PARIS_CENTER)

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
