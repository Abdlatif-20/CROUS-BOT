from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Listing:
    title: str
    url: str
    price_eur: int | None = None
    city: str | None = None
    residence: str | None = None
    external_id: str | None = None
    # Not part of the fingerprint: a new photo alone should not re-alert.
    image_url: str | None = None

    @property
    def fingerprint(self) -> str:
        base = "|".join(
            [
                self.external_id or "",
                self.url,
                self.title.strip().lower(),
                str(self.price_eur or ""),
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()