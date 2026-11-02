from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from src.config import load_settings
from src.monitor import MonitorService

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
LOGGER = logging.getLogger(__name__)


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
            total, new = service.run_cycle(test_telegram=test_telegram)
        except Exception as exc:
            LOGGER.exception("Check failed")
            self._reply(500, {"ok": False, "error": type(exc).__name__})
            return
        self._reply(200, {"ok": True, "total": total, "new": new})

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
