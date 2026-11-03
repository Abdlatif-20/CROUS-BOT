# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Python bot that polls CROUS housing search pages (`trouverunlogement.lescrous.fr`), detects unseen listings, and alerts via Telegram. Dependencies: `requests`, `beautifulsoup4`, `python-dotenv` (see `requirements.txt`). No test suite, linter, or build step exists.

## Commands

Run from repo root (imports are `src.*`, so always use `python -m src.main`):

```bash
pip install -r requirements.txt
cp .env.example .env                                  # then fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID(S), CROUS_SEARCH_URL(S)
python -m src.main --once --dry-run --debug           # one cycle, no Telegram sends (parser check)
python -m src.main --once --test-telegram             # one cycle + healthcheck message
python -m src.main                                    # continuous loop
```

## Architecture

- `src/main.py` — CLI. `--once` runs a single cycle: `sync_telegram_commands` → optional healthcheck → `maybe_send_scheduled_heartbeat` → `poll_once` → `register_success`; on exception calls `handle_failure` then re-raises. Without `--once`, `MonitorService.run_forever` loops the same steps with exponential backoff on failure and interval ± jitter sleep (min 30s).
- `src/monitor.py` — `MonitorService` orchestrates everything: fetch all search URLs, apply filters, dedup, notify. Also implements Telegram command handling (`/setmaxprice`, `/setkeywords`, `/showfilters`, etc.) by polling `getUpdates`; commands are accepted only from chat IDs in `TELEGRAM_CHAT_IDS`.
- `src/crous_client.py` — HTTP session with urllib3 retries. `fetch_listings` follows `a.fr-pagination__link--next` (24 cards/page, max `MAX_PAGES`) and forces UTF-8 because CROUS sends `Content-Type: text/html` without charset (otherwise `€` is mangled and prices parse as `None`). `parse_listings` tries JSON-LD, then DSFR cards (`div.fr-card`, link `/tools/<id>/accommodations/<n>`, price in `.fr-badge` like `265,99 €`), then a generic `<a>` scan. Returns `[]` if page says "aucun logement trouvé".
- Search URLs embed a yearly tool ID (`/tools/48/` as of Nov 2026). On a 404, `fetch_listings` reads the current ID from the CROUS homepage and retries, so stale URLs keep working; update the env URL anyway to skip the extra request.
- `src/telegram_notifier.py` — throttles sends to ~1/s per process and retries once on HTTP 429 using `retry_after`. Matters because `filter_new` marks listings seen *before* sending, so a failed send loses that alert.
- `src/geo_filter.py` — optional `FILTER_IDF_MAX_DISTANCE_KM`. Postcodes in IDF departments pass immediately, departments not bordering IDF are rejected immediately; neighbouring-department addresses are geocoded via api-adresse.data.gouv.fr (cached in `meta` as `geocode:<address>`) and measured against `src/idf_boundary.json` (simplified IDF outline, ~300 m tolerance). Fails open (keeps the listing) when geocoding fails.
- `src/discord_notifier.py` — optional Discord webhook sender (enabled when `DISCORD_WEBHOOK_URL` set). Mirrors listing alerts (as embeds) and healthchecks only. Embed shows price, postcode (inline code) + department name, address linked to Google Maps, straight-line distance from Paris centre (geocoded via `geo_filter.geocode`, same SQLite cache; omitted if geocoding fails), and Maps directions links (transit/bike/car/walk, origin = viewer's location); failures are caught in `MonitorService._send_discord_listing` so they never block Telegram. Throttled to 1/s, retries once on 429.
- `MonitorService._take_new` claims listings via `filter_new` in batches of `DEDUP_BATCH_SIZE` and stops once `MAX_ALERTS_PER_CYCLE` (40) new ones are claimed; the rest stay unseen and alert on later cycles. Keeps a big backlog inside Vercel's 300s `maxDuration`.
- `src/models.py` — `Listing` dataclass; `fingerprint` = sha256 of external_id|url|title|price. A price or title change produces a new fingerprint and re-alerts.
- `api/check.py` — Vercel entrypoint (`pyproject.toml` `[tool.vercel] entrypoint`). Auth via `CRON_SECRET` (Bearer header or `?key=`), `?test=1` adds a healthcheck, calls `MonitorService.run_cycle()` under a Redis lock (`acquire_lock`, TTL 300s) so overlapping pings skip instead of running twice. Vercel installs deps from `pyproject.toml`, **not** `requirements.txt` — keep both in sync. `vercel.json` pins region `cdg1`. Deployed as project `crous-bot` (`crous-bot-ten.vercel.app`), triggered every 5 min by cron-job.org because Hobby cron is daily-only.
- `src/log_redaction.py` — `configure_logging` (used by `main.py` and `api/check.py`) masks secret env values (bot token, webhook URL, `CRON_SECRET`, Redis token) in every log line, including tracebacks where requests echoes request URLs.
- `src/state_store.py` — `StateStore` base holds timing logic; `SqliteStateStore` (local, `STATE_DB_PATH`) or `RedisStateStore` (Upstash REST, chosen by `create_state_store` when `KV_REST_API_URL`/`TOKEN` or `UPSTASH_REDIS_REST_URL`/`TOKEN` are set). SQLite schema: `seen_listings` (dedup) and `meta` (key/value). `meta` holds runtime state: Telegram update offset, filter overrides set via Telegram commands, consecutive failure count, last heartbeat / error alert timestamps, geocode cache. Redis mirrors it as set `crous:seen` + keys `crous:meta:<key>`.
- `src/config.py` — `load_settings()` reads env into a frozen `Settings`. Plural env vars (`CROUS_SEARCH_URLS`, `TELEGRAM_CHAT_IDS`, comma-separated) override singular ones.

### Behaviors worth knowing

- Filters run **before** dedup, so filtered-out listings are not recorded as seen and will alert later if filters loosen.
- Effective filters = env defaults (`FILTER_MAX_PRICE_EUR`, `FILTER_INCLUDE_KEYWORDS`) overridden by `meta` values set via Telegram commands (empty string = cleared override, falls back to env).
- `--dry-run` skips Telegram sends and meta writes from commands, but `poll_once` still calls `StateStore.filter_new`, which **inserts listings into `seen_listings`**. A dry run against the real DB will suppress future alerts for those listings.
- Heartbeat: first call only records a timestamp; it sends once `HEARTBEAT_INTERVAL_HOURS` elapse. Error alerts fire after `ERROR_ALERT_THRESHOLD` consecutive failures, rate-limited by `ERROR_ALERT_COOLDOWN_MINUTES`.

## Deployment

- **GitHub Actions** (`.github/workflows/crous-monitor.yml`): cron every 5 min, runs `--once`. State DB lives at `.state/state.db` and persists via `actions/cache` (saved under `crous-state-<run_id>`, restored by prefix). Cache eviction resets dedup history. Secrets: `CROUS_SEARCH_URL(S)`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID(S)`, optional `DISCORD_WEBHOOK_URL`; other settings are hardcoded in the workflow `env`. Manual dispatch input `test_telegram=true` adds `--test-telegram`. New env settings must be added to both `config.py` and the workflow `env` block (and `.env.example` / README).
- **Termux/Android** (`deploy/termux/`): `start_monitor.sh` runs continuous mode from `$HOME/crous-bot`, logging to `logs/monitor.log`.
