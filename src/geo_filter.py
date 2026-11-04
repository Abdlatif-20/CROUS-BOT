from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def distance_between_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance in km between two (lon, lat) points in or near Île-de-France."""
    return math.hypot((a[0] - b[0]) * KM_PER_DEG_LON, (a[1] - b[1]) * KM_PER_DEG_LAT)


def geocode(address: str, state_store: StateStore, timeout_seconds: int) -> tuple[float, float] | None:
    """Return (lon, lat) for an address via api-adresse.data.gouv.fr, cached in SQLite meta."""
    # Cached: filtered-out listings are re-checked on every poll.
    cache_key = f"geocode:{address.lower()}"
    cached = state_store.get_meta(cache_key)
    if cached:
        lon, lat = cached.split(",")
        return float(lon), float(lat)

    try:
        response = requests.get(
            GEOCODER_ENDPOINT,
            params={"q": address, "limit": 1},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        features = response.json().get("features") or []
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("Geocoding failed for %s: %s", address, exc)
        return None

    if not features:
        return None
    lon, lat = features[0]["geometry"]["coordinates"]
    state_store.set_meta(cache_key, f"{lon},{lat}")
    return float(lon), float(lat)


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

        coordinates = geocode(address, self.state_store, self.timeout_seconds)
        if coordinates is None and postcode:
            town = CEDEX_REGEX.sub("", postcode.group(3))
            coordinates = geocode(f"{postcode.group(1)} {town}", self.state_store, self.timeout_seconds)
        if coordinates is None:
            # Fail open: a missed alert costs more than an extra one.
            LOGGER.warning("Could not locate listing, keeping it: %s | %s", listing.title, address)
            return True

        distance = distance_to_idf_km(*coordinates)
        LOGGER.debug("%s is %.1f km from Île-de-France", listing.title, distance)
        return distance <= self.max_distance_km



# École 42 Paris, 96 boulevard Bessières 75017 (api-adresse.data.gouv.fr), as (lon, lat).
ECOLE_42 = (2.318504, 48.896419)
# Transitous: free, keyless MOTIS router over French GTFS feeds (SNCF TER/TGV, IDFM). Fair use:
# identify the client and cache results, which we do per destination in meta.
TRANSIT_ENDPOINT = "https://api.transitous.org/api/v5/plan"
TRANSIT_USER_AGENT = "CROUS-BOT/1.0 (+https://github.com/Abdlatif-20/CROUS-BOT)"
NEARBY_MAX_TRANSIT_MINUTES = 90
# Nothing farther than this reaches 42 within 1h30 even by TGV (Lille is ~205 km), so skip routing.
TRANSIT_CHECK_MAX_KM = 250
PARIS_TZ = ZoneInfo("Europe/Paris")

ZONE_IDF = "Île-de-France"
ZONE_NEARBY = "Nearby Île-de-France"
ZONE_OTHER = "Not Île-de-France"


def _reference_departure() -> datetime:
    """Next weekday 08:00 Paris time, so journeys reflect a normal school-day morning."""
    day = datetime.now(PARIS_TZ).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(8, 0), tzinfo=PARIS_TZ)


def transit_minutes_from_42(
    destination: tuple[float, float], state_store: StateStore, timeout_seconds: int
) -> int | None:
    """Fastest public-transport journey from École 42 in minutes; -1 if no route; None on error."""
    lon, lat = destination
    cache_key = f"transit42:{lon:.4f},{lat:.4f}"
    cached = state_store.get_meta(cache_key)
    if cached:
        return int(cached)

    try:
        response = requests.get(
            TRANSIT_ENDPOINT,
            params={
                "fromPlace": f"{ECOLE_42[1]},{ECOLE_42[0]}",
                "toPlace": f"{lat},{lon}",
                "time": _reference_departure().isoformat(),
            },
            headers={"User-Agent": TRANSIT_USER_AGENT},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        itineraries = response.json().get("itineraries") or []
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("Transit routing failed for %s,%s: %s", lat, lon, exc)
        return None

    durations = [item["duration"] for item in itineraries if isinstance(item.get("duration"), int)]
    minutes = round(min(durations) / 60) if durations else -1
    state_store.set_meta(cache_key, str(minutes))
    return minutes


def locate(address: str, state_store: StateStore, timeout_seconds: int) -> tuple[float, float] | None:
    """Geocode an address, retrying with just postcode + town when the full address is not found."""
    coordinates = geocode(address, state_store, timeout_seconds)
    postcode = POSTCODE_REGEX.search(address)
    if coordinates is None and postcode:
        town = CEDEX_REGEX.sub("", postcode.group(3))
        coordinates = geocode(f"{postcode.group(1)} {town}", state_store, timeout_seconds)
    return coordinates


class ZoneClassifier:
    """Labels a listing Île-de-France, nearby (≤ 1h30 public transport from 42 Paris), or neither."""

    def __init__(self, state_store: StateStore, timeout_seconds: int) -> None:
        self.state_store = state_store
        self.timeout_seconds = timeout_seconds

    def zone(self, listing: Listing) -> str:
        address = listing.residence or listing.city or listing.title
        postcode = POSTCODE_REGEX.search(address)
        if postcode and postcode.group(2) in IDF_DEPARTMENTS:
            return ZONE_IDF

        coordinates = locate(address, self.state_store, self.timeout_seconds)
        if coordinates is None:
            LOGGER.warning("Could not locate listing for zone, tagging as not IDF: %s", address)
            return ZONE_OTHER
        if distance_to_idf_km(*coordinates) == 0:
            return ZONE_IDF
        if distance_between_km(coordinates, ECOLE_42) > TRANSIT_CHECK_MAX_KM:
            return ZONE_OTHER

        minutes = transit_minutes_from_42(coordinates, self.state_store, self.timeout_seconds)
        LOGGER.info("Transit from 42 to %s: %s min", address, minutes)
        if minutes is not None and 0 <= minutes <= NEARBY_MAX_TRANSIT_MINUTES:
            return ZONE_NEARBY
        return ZONE_OTHER

    def headline(self, listing: Listing) -> str:
        return f"🏠 Nouveau logement CROUS détecté ({self.zone(listing)})"
