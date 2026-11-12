from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import requests

from src.models import Listing


class StateStore:
    """Dedup + key/value state. Subclasses provide storage; timing logic lives here."""

    def filter_new(self, listings: list[Listing], limit: int | None = None) -> list[Listing]:
        """Return unseen listings (at most `limit`, in order) and mark exactly those as seen."""
        raise NotImplementedError

    def get_meta(self, key: str) -> str | None:
        raise NotImplementedError

    def set_meta(self, key: str, value: str) -> None:
        raise NotImplementedError

    def acquire_lock(self, name: str, ttl_seconds: int) -> bool:
        """Claim a lock so overlapping runs skip instead of doubling requests. Local runs are one process."""
        return True

    def release_lock(self, name: str) -> None:
        pass

    def reset_failure_count(self) -> None:
        self.set_meta("consecutive_failures", "0")

    def register_failure(self) -> int:
        current = self.get_meta("consecutive_failures")
        current_count = int(current) if current else 0
        next_count = current_count + 1
        self.set_meta("consecutive_failures", str(next_count))
        return next_count

    def should_send_heartbeat(self, interval_hours: int) -> bool:
        key = "last_heartbeat_at"
        now = datetime.now(timezone.utc)
        raw_value = self.get_meta(key)
        if not raw_value:
            self.set_meta(key, now.isoformat())
            return False

        last_value = datetime.fromisoformat(raw_value)
        if now - last_value >= timedelta(hours=interval_hours):
            self.set_meta(key, now.isoformat())
            return True
        return False

    def should_send_error_alert(self, cooldown_minutes: int) -> bool:
        key = "last_error_alert_at"
        now = datetime.now(timezone.utc)
        raw_value = self.get_meta(key)
        if not raw_value:
            self.set_meta(key, now.isoformat())
            return True

        last_value = datetime.fromisoformat(raw_value)
        if now - last_value >= timedelta(minutes=cooldown_minutes):
            self.set_meta(key, now.isoformat())
            return True
        return False


class SqliteStateStore(StateStore):
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_listings (
                    fingerprint TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def filter_new(self, listings: list[Listing], limit: int | None = None) -> list[Listing]:
        now = datetime.now(timezone.utc).isoformat()
        new_items: list[Listing] = []
        with closing(sqlite3.connect(self.db_path)) as conn:
            for listing in listings:
                if limit is not None and len(new_items) >= limit:
                    break
                row = conn.execute(
                    "SELECT 1 FROM seen_listings WHERE fingerprint = ?",
                    (listing.fingerprint,),
                ).fetchone()
                if row:
                    continue

                conn.execute(
                    """
                    INSERT INTO seen_listings (fingerprint, title, url, first_seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (listing.fingerprint, listing.title, listing.url, now),
                )
                new_items.append(listing)

            conn.execute(
                """
                INSERT INTO meta (key, value)
                VALUES ('last_poll_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (now,),
            )
            conn.commit()
        return new_items

    def get_meta(self, key: str) -> str | None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()


class RedisStateStore(StateStore):
    """Upstash Redis over its REST API, for serverless hosts with no persistent disk (Vercel)."""

    SEEN_KEY = "crous:seen"
    META_PREFIX = "crous:meta:"

    def __init__(self, rest_url: str, rest_token: str, timeout_seconds: int = 15) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {rest_token}"

    def _pipeline(self, commands: list[list[str]]) -> list[object]:
        response = self.session.post(
            f"{self.rest_url}/pipeline", json=commands, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        results = response.json()
        for item in results:
            if "error" in item:
                raise RuntimeError(f"Upstash error: {item['error']}")
        return [item.get("result") for item in results]

    def filter_new(self, listings: list[Listing], limit: int | None = None) -> list[Listing]:
        # Upstash bills per command (free tier: 500K/month), so check every fingerprint with one
        # SMISMEMBER and claim the new ones with one SADD instead of a command per listing.
        # Callers hold the check lock, so nothing else can claim between the two calls.
        if not listings:
            return []
        flags = self._pipeline([["SMISMEMBER", self.SEEN_KEY, *(item.fingerprint for item in listings)]])[0]
        new_items = [listing for listing, seen in zip(listings, flags) if not seen]
        if limit is not None:
            new_items = new_items[:limit]
        if new_items:
            self._pipeline([["SADD", self.SEEN_KEY, *(item.fingerprint for item in new_items)]])
        return new_items

    def acquire_lock(self, name: str, ttl_seconds: int) -> bool:
        # NX + EX: only one caller wins, and a crashed run frees the lock when it expires.
        result = self._pipeline([["SET", f"{self.META_PREFIX}lock:{name}", "1", "NX", "EX", str(ttl_seconds)]])[0]
        return result == "OK"

    def release_lock(self, name: str) -> None:
        self._pipeline([["DEL", f"{self.META_PREFIX}lock:{name}"]])

    def get_meta(self, key: str) -> str | None:
        result = self._pipeline([["GET", f"{self.META_PREFIX}{key}"]])[0]
        return result if isinstance(result, str) else None

    def set_meta(self, key: str, value: str) -> None:
        self._pipeline([["SET", f"{self.META_PREFIX}{key}", value]])


def create_state_store(db_path: str, timeout_seconds: int) -> StateStore:
    # Vercel Marketplace Upstash sets KV_REST_API_*; a direct Upstash setup uses UPSTASH_REDIS_REST_*.
    rest_url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
    rest_token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")
    if rest_url and rest_token:
        return RedisStateStore(rest_url, rest_token, timeout_seconds)
    return SqliteStateStore(db_path)
