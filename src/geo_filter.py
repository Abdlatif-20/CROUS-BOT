from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
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


# Estimated door-to-door public-transport minutes from École 42 Paris (96 bd Bessières, 75017;
# Porte de Clichy: M13, M14, RER C) to CROUS towns in departments bordering Île-de-France.
# Includes the metro ride to the Paris terminus and typical waiting. There is no free keyless
# transit router, so these are hand estimates; add towns here as new ones show up in the logs.
TRANSIT_MINUTES_FROM_42 = {
    # Oise (60), trains from Gare du Nord
    "CHANTILLY": 45, "CREIL": 60, "NOGENT SUR OISE": 65, "SENLIS": 75, "COMPIEGNE": 80,
    "BEAUVAIS": 105,
    # Eure (27), trains from Saint-Lazare (M14, one stop)
    "VERNON": 60, "VAL DE REUIL": 70, "EVREUX": 80, "LOUVIERS": 85,
    # Eure-et-Loir (28), trains from Montparnasse
    "DREUX": 80, "CHARTRES": 100,
    # Loiret (45), trains from Austerlitz / Bercy
    "MONTARGIS": 95, "ORLEANS": 95,
    # Aisne (02), trains from Gare de l'Est / Gare du Nord
    "CHATEAU THIERRY": 75, "SOISSONS": 100, "LAON": 120, "SAINT QUENTIN": 110,
    # Marne (51), TGV from Gare de l'Est
    "REIMS": 75, "EPERNAY": 100, "CHALONS EN CHAMPAGNE": 105,
    # Aube (10) and Yonne (89), trains from Gare de l'Est / Bercy
    "NOGENT SUR SEINE": 80, "SENS": 80, "TROYES": 110, "AUXERRE": 120,
}
NEARBY_MAX_TRANSIT_MINUTES = 90

ZONE_IDF = "Île-de-France"
ZONE_NEARBY = "Nearby Île-de-France"
ZONE_OTHER = "Not Île-de-France"


def _normalize_town(town: str) -> str:
    town = CEDEX_REGEX.sub("", town)
    ascii_town = unicodedata.normalize("NFKD", town).encode("ascii", "ignore").decode()
    return re.sub(r"[\s'-]+", " ", ascii_town).strip().upper()


def idf_zone(listing: Listing) -> str:
    """Île-de-France, nearby (≤ 1h30 public transport from 42 Paris), or neither."""
    address = listing.residence or listing.city or listing.title
    match = POSTCODE_REGEX.search(address)
    if match is None:
        return ZONE_OTHER
    if match.group(2) in IDF_DEPARTMENTS:
        return ZONE_IDF
    if match.group(2) in NEIGHBOUR_DEPARTMENTS:
        minutes = TRANSIT_MINUTES_FROM_42.get(_normalize_town(match.group(3)))
        if minutes is None:
            LOGGER.info("No transit estimate for %s, tagging as not nearby", match.group(3))
        elif minutes <= NEARBY_MAX_TRANSIT_MINUTES:
            return ZONE_NEARBY
    return ZONE_OTHER


def alert_headline(listing: Listing) -> str:
    return f"🏠 Nouveau logement CROUS détecté ({idf_zone(listing)})"
