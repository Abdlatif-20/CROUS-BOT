from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote_plus

from src.geo_filter import (
    ECOLE_42,
    POSTCODE_REGEX,
    TRANSIT_CHECK_MAX_KM,
    distance_between_km,
    locate,
    transit_minutes_from_42,
)
from src.models import Listing
from src.state_store import StateStore

PARIS_CENTER = (2.3488, 48.8534)  # (lon, lat), Hôtel de Ville
MAPS_SEARCH_URL = "https://www.google.com/maps/search/?api=1&query={}"
# No origin: Google Maps starts from the viewer's current location and shows time and distance.
MAPS_DIRECTIONS_URL = "https://www.google.com/maps/dir/?api=1&destination={}&travelmode={}"
ROUTE_MODES = [
    ("🚆 Transports", "transit"),
    ("🚲 Vélo", "bicycling"),
    ("🚗 Voiture", "driving"),
    ("🚶 À pied", "walking"),
]
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


@dataclass(frozen=True)
class ListingDetails:
    """Everything an alert shows besides the headline, computed once and shared by all notifiers."""

    address: str | None = None
    postcode: str | None = None
    department: str | None = None
    maps_url: str | None = None
    distance_from_paris_km: float | None = None
    transit_minutes_from_42: int | None = None
    routes: list[tuple[str, str]] = field(default_factory=list)


def build_details(listing: Listing, state_store: StateStore, timeout_seconds: int) -> ListingDetails:
    address = listing.residence or listing.city
    if not address:
        return ListingDetails()

    postcode = POSTCODE_REGEX.search(address)
    department = None
    if postcode:
        department = DEPARTMENT_NAMES.get(postcode.group(2), f"dépt {postcode.group(2)}")

    distance = None
    minutes = None
    # Geocoding and routing are cached in meta, so ZoneClassifier's earlier lookups make these free.
    coordinates = locate(address, state_store, timeout_seconds)
    if coordinates is not None:
        distance = distance_between_km(coordinates, PARIS_CENTER)
        if distance_between_km(coordinates, ECOLE_42) <= TRANSIT_CHECK_MAX_KM:
            minutes = transit_minutes_from_42(coordinates, state_store, timeout_seconds)
            if minutes is not None and minutes < 0:
                minutes = None

    query = quote_plus(address)
    return ListingDetails(
        address=address,
        postcode=postcode.group(1) if postcode else None,
        department=department,
        maps_url=MAPS_SEARCH_URL.format(query),
        distance_from_paris_km=distance,
        transit_minutes_from_42=minutes,
        routes=[(label, MAPS_DIRECTIONS_URL.format(query, mode)) for label, mode in ROUTE_MODES],
    )


def format_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}" if hours else f"{rest} min"
