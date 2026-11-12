from __future__ import annotations

import logging
import random
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Iterable

from requests import RequestException

from src.config import Settings
from src.crous_client import CrousClient
from src.discord_notifier import DiscordNotifier
from src.geo_filter import IdfDistanceFilter, ZoneClassifier
from src.listing_details import ListingDetails, build_details
from src.models import Listing
from src.state_store import create_state_store
from src.telegram_notifier import TelegramNotifier

LOGGER = logging.getLogger(__name__)
# A run sends ~2s per alert (Telegram + Discord throttles); 40 keeps a cycle well under Vercel's 300s cap.
MAX_ALERTS_PER_CYCLE = 40
# Set once the first cycle on an empty state store has recorded existing listings without alerting.
INITIAL_SYNC_KEY = "initial_sync_at"


class MonitorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = CrousClient(
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            user_agent=settings.user_agent,
        )
        self.state_store = create_state_store(settings.state_db_path, settings.http_timeout_seconds)
        self.notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_ids=settings.telegram_chat_ids,
            timeout_seconds=settings.http_timeout_seconds,
        )
        self.idf_filter: IdfDistanceFilter | None = None
        if settings.filter_idf_max_distance_km is not None:
            self.idf_filter = IdfDistanceFilter(
                max_distance_km=settings.filter_idf_max_distance_km,
                state_store=self.state_store,
                timeout_seconds=settings.http_timeout_seconds,
            )
        self.zones = ZoneClassifier(self.state_store, settings.http_timeout_seconds)
        self.discord: DiscordNotifier | None = None
        if settings.discord_webhook_url:
            self.discord = DiscordNotifier(
                webhook_url=settings.discord_webhook_url,
                timeout_seconds=settings.http_timeout_seconds,
            )

    def poll_once(self, dry_run: bool = False) -> tuple[int, int]:
        listings: list[Listing] = []
        for search_url in self.settings.crous_search_urls:
            source_listings = self.client.fetch_listings(search_url)
            LOGGER.info("Fetched %s listing(s) from %s", len(source_listings), search_url)
            listings.extend(source_listings)

        filtered_listings = self._apply_filters(listings)
        skipped_count = len(listings) - len(filtered_listings)
        if skipped_count > 0:
            LOGGER.info("Filtered out %s listing(s) using configured rules", skipped_count)

        if not self.state_store.get_meta(INITIAL_SYNC_KEY):
            # A brand-new state store has no history; alerting every listing already online would
            # flood the chats, so the first cycle only records them and later cycles alert.
            recorded = self.state_store.filter_new(filtered_listings)
            self.state_store.set_meta(INITIAL_SYNC_KEY, datetime.now(timezone.utc).isoformat())
            LOGGER.info("Initial sync: recorded %s existing listing(s) without alerting", len(recorded))
            return len(filtered_listings), 0

        new_listings = self._take_new(filtered_listings)

        if dry_run:
            for listing in new_listings:
                LOGGER.info("[DRY-RUN] New listing: %s | %s", listing.title, listing.url)
            return len(filtered_listings), len(new_listings)

        for listing in new_listings:
            headline = self.zones.headline(listing)
            details = build_details(listing, self.state_store, self.settings.http_timeout_seconds)
            self.notifier.send_new_listing(listing, headline, details)
            LOGGER.info("Sent Telegram alert for: %s", listing.url)
            self._send_discord_listing(listing, headline, details)

        return len(filtered_listings), len(new_listings)

    def _take_new(self, listings: list[Listing]) -> list[Listing]:
        # filter_new marks listings seen, so a big backlog (first run, wider search) is capped per
        # cycle; the rest stay unseen and alert on later cycles, keeping each run inside the
        # serverless time limit.
        new_listings = self.state_store.filter_new(listings, limit=MAX_ALERTS_PER_CYCLE)
        if len(new_listings) == MAX_ALERTS_PER_CYCLE:
            LOGGER.info("Alert cap reached; remaining listings wait for the next cycle")
        return new_listings

    def sync_telegram_commands(self, dry_run: bool = False) -> int:
        raw_offset = self.state_store.get_meta("telegram_update_offset")
        offset = int(raw_offset) if raw_offset else 0
        updates = self.notifier.get_updates(offset=offset, limit=20)

        if not updates:
            return 0

        processed = 0
        max_update_id = offset - 1
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                max_update_id = max(max_update_id, update_id)

            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue

            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = str(chat.get("id", ""))
            if chat_id not in self.settings.telegram_chat_ids:
                continue

            text = str(message.get("text", "")).strip()
            if not text.startswith("/"):
                continue

            self._handle_telegram_command(text=text, chat_id=chat_id, dry_run=dry_run)
            processed += 1

        if max_update_id >= offset and not dry_run:
            self.state_store.set_meta("telegram_update_offset", str(max_update_id + 1))

        if processed:
            LOGGER.info("Processed %s Telegram command(s)", processed)
        return processed

    def send_healthcheck(self, dry_run: bool = False) -> None:
        if dry_run:
            LOGGER.info("[DRY-RUN] Telegram healthcheck would be sent")
            return
        self.notifier.send_healthcheck()
        LOGGER.info("Sent Telegram healthcheck message")
        if self.discord is not None:
            self.discord.send_healthcheck()
            LOGGER.info("Sent Discord healthcheck message")

    def maybe_send_scheduled_heartbeat(self, dry_run: bool = False) -> bool:
        if not self.settings.heartbeat_enabled:
            return False
        if not self.state_store.should_send_heartbeat(self.settings.heartbeat_interval_hours):
            return False
        if dry_run:
            LOGGER.info("[DRY-RUN] Telegram heartbeat would be sent")
            return True
        self.notifier.send_heartbeat()
        LOGGER.info("Sent scheduled heartbeat message")
        return True

    def register_success(self) -> None:
        self.state_store.reset_failure_count()

    def handle_failure(self, error: Exception, dry_run: bool = False) -> None:
        failure_count = self.state_store.register_failure()
        threshold = self.settings.error_alert_threshold
        if failure_count < threshold:
            return
        if not self.state_store.should_send_error_alert(self.settings.error_alert_cooldown_minutes):
            return
        if dry_run:
            LOGGER.warning(
                "[DRY-RUN] Error alert would be sent (consecutive_failures=%s)", failure_count
            )
            return
        self.notifier.send_error_alert(failure_count=failure_count, error_text=str(error))
        LOGGER.warning("Sent repeated-failure alert (consecutive_failures=%s)", failure_count)

    def run_cycle(self, dry_run: bool = False, test_telegram: bool = False) -> tuple[int, int]:
        """One full check, as used by --once, GitHub Actions and the Vercel endpoint."""
        try:
            self.sync_telegram_commands(dry_run=dry_run)
            if test_telegram:
                self.send_healthcheck(dry_run=dry_run)
            self.maybe_send_scheduled_heartbeat(dry_run=dry_run)
            total, new = self.poll_once(dry_run=dry_run)
            self.register_success()
            return total, new
        except Exception as exc:
            self.handle_failure(exc, dry_run=dry_run)
            raise

    def run_forever(self, dry_run: bool = False) -> None:
        failure_count = 0
        while True:
            try:
                self.sync_telegram_commands(dry_run=dry_run)
                self.maybe_send_scheduled_heartbeat(dry_run=dry_run)
                total_count, new_count = self.poll_once(dry_run=dry_run)
                failure_count = 0
                self.register_success()
                LOGGER.info("Poll finished: total=%s new=%s", total_count, new_count)
            except RequestException as exc:
                failure_count += 1
                self.handle_failure(exc, dry_run=dry_run)
                backoff = min(300, 2 ** min(failure_count, 8))
                LOGGER.warning("HTTP failure #%s (%s). Backing off for %ss", failure_count, exc, backoff)
                time.sleep(backoff)
            except Exception as exc:
                failure_count += 1
                self.handle_failure(exc, dry_run=dry_run)
                backoff = min(300, 2 ** min(failure_count, 8))
                LOGGER.exception("Unexpected failure #%s. Backing off for %ss", failure_count, backoff)
                time.sleep(backoff)

            sleep_seconds = _compute_sleep(
                interval=self.settings.poll_interval_seconds,
                jitter=self.settings.poll_jitter_seconds,
            )
            LOGGER.debug("Sleeping for %ss before next poll", sleep_seconds)
            time.sleep(sleep_seconds)

    def _send_discord_listing(self, listing: Listing, headline: str, details: ListingDetails) -> None:
        if self.discord is None:
            return
        # Discord is best-effort: the listing is already marked seen and alerted on Telegram.
        try:
            self.discord.send_new_listing(listing, headline, details)
            LOGGER.info("Sent Discord alert for: %s", listing.url)
        except Exception:
            LOGGER.exception("Discord alert failed for: %s", listing.url)

    def _apply_filters(self, listings: Iterable[Listing]) -> list[Listing]:
        output: list[Listing] = []
        max_price, keywords = self._get_effective_filters()

        for listing in listings:
            if max_price is not None and listing.price_eur is not None and listing.price_eur > max_price:
                continue

            if keywords:
                haystack = " ".join(
                    [
                        listing.title,
                        listing.city or "",
                        listing.residence or "",
                        listing.url,
                    ]
                ).lower()
                if not any(keyword in haystack for keyword in keywords):
                    continue

            if self.idf_filter is not None and not self.idf_filter.accepts(listing):
                continue

            output.append(listing)

        return output

    def _get_effective_filters(self) -> tuple[int | None, tuple[str, ...]]:
        max_price = self.settings.filter_max_price_eur
        keywords = self.settings.filter_include_keywords

        override_price = self.state_store.get_meta("filter_max_price_override")
        if override_price:
            max_price = int(override_price)

        override_keywords = self.state_store.get_meta("filter_keywords_override")
        if override_keywords:
            parsed_keywords = [item.strip().lower() for item in override_keywords.split(",") if item.strip()]
            keywords = tuple(OrderedDict.fromkeys(parsed_keywords).keys())

        return max_price, keywords

    def _handle_telegram_command(self, text: str, chat_id: str, dry_run: bool) -> None:
        command_token, _, remainder = text.partition(" ")
        command = command_token.split("@", maxsplit=1)[0].lower()
        arg = remainder.strip()

        if command == "/setmaxprice":
            if not arg.isdigit():
                self._reply("Usage: /setmaxprice <amount_in_eur>", chat_id=chat_id, dry_run=dry_run)
                return
            if not dry_run:
                self.state_store.set_meta("filter_max_price_override", arg)
            max_price, keywords = self._get_effective_filters()
            self._reply(self._format_filter_status(max_price, keywords), chat_id=chat_id, dry_run=dry_run)
            return

        if command == "/clearmaxprice":
            if not dry_run:
                self.state_store.set_meta("filter_max_price_override", "")
            max_price, keywords = self._get_effective_filters()
            self._reply(self._format_filter_status(max_price, keywords), chat_id=chat_id, dry_run=dry_run)
            return

        if command == "/setkeywords":
            if not arg:
                self._reply("Usage: /setkeywords keyword1,keyword2", chat_id=chat_id, dry_run=dry_run)
                return
            keywords = [item.strip().lower() for item in arg.split(",") if item.strip()]
            if not keywords:
                self._reply("Usage: /setkeywords keyword1,keyword2", chat_id=chat_id, dry_run=dry_run)
                return
            normalized = ",".join(OrderedDict.fromkeys(keywords).keys())
            if not dry_run:
                self.state_store.set_meta("filter_keywords_override", normalized)
            max_price, active_keywords = self._get_effective_filters()
            self._reply(
                self._format_filter_status(max_price, active_keywords),
                chat_id=chat_id,
                dry_run=dry_run,
            )
            return

        if command == "/clearkeywords":
            if not dry_run:
                self.state_store.set_meta("filter_keywords_override", "")
            max_price, keywords = self._get_effective_filters()
            self._reply(self._format_filter_status(max_price, keywords), chat_id=chat_id, dry_run=dry_run)
            return

        if command == "/showfilters":
            max_price, keywords = self._get_effective_filters()
            self._reply(self._format_filter_status(max_price, keywords), chat_id=chat_id, dry_run=dry_run)
            return

        if command == "/help":
            self._reply(
                "Commands:\n"
                "/showfilters\n"
                "/setmaxprice <eur>\n"
                "/clearmaxprice\n"
                "/setkeywords kw1,kw2\n"
                "/clearkeywords",
                chat_id=chat_id,
                dry_run=dry_run,
            )
            return

        self._reply(
            "Unknown command. Send /help for available commands.",
            chat_id=chat_id,
            dry_run=dry_run,
        )

    def _reply(self, text: str, chat_id: str, dry_run: bool) -> None:
        if dry_run:
            LOGGER.info("[DRY-RUN] Telegram reply: %s", text)
            return
        self.notifier.send_text(text, chat_id=chat_id)

    @staticmethod
    def _format_filter_status(max_price: int | None, keywords: tuple[str, ...]) -> str:
        price_text = f"<= {max_price} €" if max_price is not None else "none"
        keyword_text = ", ".join(keywords) if keywords else "none"
        return (
            "✅ Filters updated\n\n"
            f"Max price: {price_text}\n"
            f"Keywords: {keyword_text}"
        )


def _compute_sleep(interval: int, jitter: int) -> int:
    if jitter == 0:
        return interval
    delta = random.randint(-jitter, jitter)
    return max(30, interval + delta)


def print_new_listings_preview(listings: list[Listing]) -> None:
    for listing in listings:
        LOGGER.info("New listing preview: %s (%s)", listing.title, listing.url)