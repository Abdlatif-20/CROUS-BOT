from __future__ import annotations

import json
import logging
import re
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.models import Listing

LOGGER = logging.getLogger(__name__)

PRICE_REGEX = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€")
ACCOMMODATION_ID_REGEX = re.compile(r"/accommodations/(\d+)")
POSTCODE_CITY_REGEX = re.compile(r"\b\d{5}\s+(.+)$")
MAX_PAGES = 10


class CrousClient:
    def __init__(self, *, timeout_seconds: int, max_retries: int, user_agent: str) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
                "Cache-Control": "no-cache",
            }
        )
        retry = Retry(
            total=max_retries,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def fetch_listings(self, search_url: str) -> list[Listing]:
        listings: list[Listing] = []
        page_url: str | None = search_url
        visited: set[str] = set()
        while page_url and page_url not in visited and len(visited) < MAX_PAGES:
            visited.add(page_url)
            response = self.session.get(page_url, timeout=self.timeout_seconds)
            response.raise_for_status()
            # CROUS omits charset in Content-Type; requests would fall back to ISO-8859-1 and mangle "€".
            if "charset" not in response.headers.get("Content-Type", "").lower():
                response.encoding = "utf-8"
            listings.extend(parse_listings(response.text, base_url=page_url))
            page_url = find_next_page_url(response.text, base_url=page_url)
        return _unique_by_fingerprint(listings)


def parse_listings(html: str, *, base_url: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()
    if "aucun logement trouvé" in page_text:
        return []

    json_listings = _parse_ld_json(soup, base_url)
    if json_listings:
        return _unique_by_fingerprint(json_listings)

    dsfr_listings = _parse_dsfr_cards(soup, base_url)
    if dsfr_listings:
        return _unique_by_fingerprint(dsfr_listings)

    card_listings = _parse_card_like_elements(soup, base_url)
    return _unique_by_fingerprint(card_listings)


def find_next_page_url(html: str, *, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("a.fr-pagination__link--next[href]")
    if link is None or link.get("aria-disabled") == "true":
        return None
    return urljoin(base_url, link["href"])


def _parse_dsfr_cards(soup: BeautifulSoup, base_url: str) -> list[Listing]:
    listings: list[Listing] = []
    for card in soup.select("div.fr-card"):
        anchor = card.select_one(".fr-card__title a[href]")
        if anchor is None:
            continue

        title = anchor.get_text(" ", strip=True)
        if not title:
            continue

        listing_url = urljoin(base_url, anchor["href"])
        id_match = ACCOMMODATION_ID_REGEX.search(listing_url)

        price = None
        for badge in card.select(".fr-badge"):
            price = _extract_price(badge.get_text(" ", strip=True))
            if price is not None:
                break

        address_node = card.select_one(".fr-card__desc")
        address = address_node.get_text(" ", strip=True) if address_node else ""
        city_match = POSTCODE_CITY_REGEX.search(address)

        listings.append(
            Listing(
                title=title,
                url=listing_url,
                price_eur=price,
                city=city_match.group(1) if city_match else _clean_optional_text(address),
                residence=_clean_optional_text(address),
                external_id=id_match.group(1) if id_match else None,
            )
        )
    return listings


def _parse_ld_json(soup: BeautifulSoup, base_url: str) -> list[Listing]:
    listings: list[Listing] = []
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        payload = script.string or script.get_text(strip=True)
        if not payload:
            continue

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        for node in _iter_nodes(data):
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in {"Offer", "Product", "Residence", "Apartment"}:
                continue

            title = (node.get("name") or "").strip()
            if not title:
                continue

            raw_url = node.get("url") or ""
            listing_url = urljoin(base_url, raw_url) if raw_url else base_url
            offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}

            price = None
            raw_price = offers.get("price") if offers else None
            if isinstance(raw_price, (str, int, float)):
                try:
                    price = int(float(raw_price))
                except ValueError:
                    price = None

            listings.append(
                Listing(
                    title=title,
                    url=listing_url,
                    price_eur=price,
                    city=_clean_optional_text(node.get("addressLocality")),
                    residence=_clean_optional_text(node.get("brand")),
                    external_id=_clean_optional_text(node.get("identifier")),
                )
            )
    return listings


def _iter_nodes(payload: object) -> Iterable[object]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_nodes(item)
        return

    if isinstance(payload, dict):
        yield payload
        for key in ["@graph", "itemListElement", "mainEntity", "items"]:
            nested = payload.get(key)
            if nested is not None:
                yield from _iter_nodes(nested)


def _parse_card_like_elements(soup: BeautifulSoup, base_url: str) -> list[Listing]:
    listings: list[Listing] = []
    candidates = soup.find_all("a", href=True)

    for anchor in candidates:
        href = anchor.get("href", "")
        if not href:
            continue
        if "/logement/" not in href and "residence" not in href.lower():
            continue

        listing_url = urljoin(base_url, href)
        container = _closest_container(anchor)
        text_block = container.get_text(" ", strip=True) if container else anchor.get_text(" ", strip=True)
        title = anchor.get_text(" ", strip=True)
        if not title and container:
            heading = container.find(["h2", "h3", "h4"])
            title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue

        price = _extract_price(text_block)
        external_id = _extract_external_id(container, listing_url)

        listings.append(
            Listing(
                title=title,
                url=listing_url,
                price_eur=price,
                external_id=external_id,
            )
        )

    return listings


def _closest_container(anchor: Tag) -> Tag | None:
    return anchor.find_parent(["article", "li", "div"])


def _extract_price(text: str) -> int | None:
    match = PRICE_REGEX.search(text)
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")))


def _extract_external_id(container: Tag | None, fallback_url: str) -> str | None:
    if container is None:
        return None
    for attr_name in ["data-id", "id", "data-logement-id"]:
        value = container.get(attr_name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    fallback_match = re.search(r"(\d{3,})", fallback_url)
    if fallback_match:
        return fallback_match.group(1)
    LOGGER.debug("No external_id found for listing URL: %s", fallback_url)
    return None


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        maybe_name = value.get("name")
        if isinstance(maybe_name, str) and maybe_name.strip():
            return maybe_name.strip()
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _unique_by_fingerprint(items: list[Listing]) -> list[Listing]:
    output: list[Listing] = []
    seen: set[str] = set()
    for item in items:
        fingerprint = item.fingerprint
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        output.append(item)
    return output