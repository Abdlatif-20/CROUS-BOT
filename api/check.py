from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from src.config import load_settings
from src.log_redaction import configure_logging
from src.monitor import MonitorService

configure_logging(logging.INFO, "%(levelname)s | %(name)s | %(message)s")
LOGGER = logging.getLogger(__name__)
# Matches maxDuration in vercel.json, so a run killed by the timeout cannot hold the lock longer.
LOCK_TTL_SECONDS = 300


def _is_authorized(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> bool:
    secret = os.getenv("CRON_SECRET", "")
    if not secret:
        return False
    # Vercel Cron sends "Authorization: Bearer <CRON_SECRET>"; external pingers can use ?key=.
    header = handler.headers.get("Authorization", "")
    provided = header[len("Bearer "):] if header.startswith("Bearer ") else query.get("key", [""])[0]
    return hmac.compare_digest(provided, secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if not _is_authorized(self, query):
            self._reply(401, {"ok": False, "error": "unauthorized"})
            return

        test_telegram = query.get("test", ["0"])[0] in {"1", "true"}
        try:
            service = MonitorService(load_settings())
            if query.get("classify", ["0"])[0] in {"1", "true"}:
                listings = service.classify_current()
                counts: dict[str, int] = {}
                for item in listings:
                    counts[item["zone"]] = counts.get(item["zone"], 0) + 1
                self._reply(200, {"ok": True, "zones": counts, "listings": listings})
                return
            # A slow run (alert backlog) can outlast the pinger's interval; skip rather than overlap,
            # which would double CROUS requests and trip Discord's webhook rate limit.
            if not service.state_store.acquire_lock("check", LOCK_TTL_SECONDS):
                LOGGER.info("Previous check still running, skipping")
                self._reply(200, {"ok": True, "skipped": "previous check still running"})
                return
            try:
                total, new = service.run_cycle(test_telegram=test_telegram)
            finally:
                service.state_store.release_lock("check")
        except Exception as exc:
            LOGGER.exception("Check failed")
            self._reply(500, {"ok": False, "error": type(exc).__name__})
            return
        self._reply(200, {"ok": True, "total": total, "new": new})

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The default access log prints the full request line, which would leak ?key= into logs.
        LOGGER.info("%s %s -> %s", self.command, urlparse(self.path).path, code)

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
