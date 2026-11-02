from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

import requests

from src.models import Listing
from src.state_store import StateStore

LOGGER = logging.getLogger(__name__)

IDF_DEPARTMENTS = frozenset({"75", "77", "78", "91", "92", "93", "94", "95"})
# Departments bordering Île-de-France; any place near IDF is in one of these or in IDF itself.
NEIGHBOUR_DEPARTMENTS = frozenset({"02", "10", "27", "28", "45", "51", "60", "89"})
GEOCODER_ENDPOINT = "https://api-adresse.data.gouv.fr/search/"
POSTCODE_REGEX = re.compile(r"\b((\d{2})\d{3})\s+(.+)$")
CEDEX_REGEX = re.compile(r"\s+cedex.*$", re.IGNORECASE)
BOUNDARY_PATH = Path(__file__).with_name("idf_boundary.json")

# Equirectangular projection around Île-de-France's latitude; error is negligible at this scale.
KM_PER_DEG_LON = 111.32 * math.cos(math.radians(48.7))
KM_PER_DEG_LAT = 110.57


def _load_ring() -> list[tuple[float, float]]:
    data = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
    return [(lon * KM_PER_DEG_LON, lat * KM_PER_DEG_LAT) for lon, lat in data["ring"]]


IDF_RING_KM = _load_ring()


def distance_to_idf_km(lon: float, lat: float) -> float:
    """Distance in km from a point to Île-de-France; 0 when inside."""
    px, py = lon * KM_PER_DEG_LON, lat * KM_PER_DEG_LAT
    inside = False
    best = math.inf
    ring = IDF_RING_KM
    for i in range(len(ring)):
        ax, ay = ring[i - 1]
        bx, by = ring[i]
        if (ay > py) != (by > py) and px < ax + (py - ay) * (bx - ax) / (by - ay):
            inside = not inside
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return 0.0 if inside else best


class IdfDistanceFilter:
    def __init__(self, max_distance_km: int, state_store: StateStore, timeout_seconds: int) -> None:
        self.max_distance_km = max_distance_km
        self.state_store = state_store
        self.timeout_seconds = timeout_seconds

    def accepts(self, listing: Listing) -> bool:
        address = listing.residence or listing.city or listing.title
        postcode = POSTCODE_REGEX.search(address)
        if postcode:
            department = postcode.group(2)
            if department in IDF_DEPARTMENTS:
                return True
            if department not in NEIGHBOUR_DEPARTMENTS:
                return False

        coordinates = self._geocode(address)
        if coordinates is None and postcode:
            town = CEDEX_REGEX.sub("", postcode.group(3))
            coordinates = self._geocode(f"{postcode.group(1)} {town}")
        if coordinates is None:
            # Fail open: a missed alert costs more than an extra one.
            LOGGER.warning("Could not locate listing, keeping it: %s | %s", listing.title, address)
            return True

        distance = distance_to_idf_km(*coordinates)
        LOGGER.debug("%s is %.1f km from Île-de-France", listing.title, distance)
        return distance <= self.max_distance_km

    def _geocode(self, address: str) -> tuple[float, float] | None:
        # Cached in SQLite: filtered-out listings are re-checked on every poll.
        cache_key = f"geocode:{address.lower()}"
        cached = self.state_store.get_meta(cache_key)
        if cached:
            lon, lat = cached.split(",")
            return float(lon), float(lat)

        try:
            response = requests.get(
                GEOCODER_ENDPOINT,
                params={"q": address, "limit": 1},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            features = response.json().get("features") or []
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Geocoding failed for %s: %s", address, exc)
            return None

        if not features:
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        self.state_store.set_meta(cache_key, f"{lon},{lat}")
        return float(lon), float(lat)
