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
- `src/crous_client.py` — HTTP session with urllib3 retries. `fetch_listings` follows `a.fr-pagination__link--next` (24 cards/page, max `MAX_PAGES`) and forces UTF-8 because CROUS sends `Content-Type: text/html` without charset (otherwise `€` is mangled and prices parse as `None`). `parse_listings` tries JSON-LD, then DSFR cards (`div.fr-card`, link `/tools/<id>/accommodations/<n>`, price in `.fr-badge` like `265,99 €`, first `img[src]` URL-encoded into `Listing.image_url`), then a generic `<a>` scan. Returns `[]` if page says "aucun logement trouvé".
- Search URLs embed a yearly tool ID (`/tools/48/` as of Nov 2026). On a 404, `fetch_listings` reads the current ID from the CROUS homepage and retries, so stale URLs keep working; update the env URL anyway to skip the extra request.
- `src/telegram_notifier.py` — listings with `image_url` go out via `sendPhoto` (caption = message text, `parse_mode=HTML` mirroring the Discord embed fields), falling back to `sendMessage` if Telegram cannot load the photo; Discord embeds set `image`. `image_url` is excluded from the fingerprint. throttles sends to ~1/s per process and retries once on HTTP 429 using `retry_after`. Matters because `filter_new` marks listings seen *before* sending, so a failed send loses that alert.
- `src/geo_filter.py` — optional `FILTER_IDF_MAX_DISTANCE_KM`. Postcodes in IDF departments pass immediately, departments not bordering IDF are rejected immediately; neighbouring-department addresses are geocoded via api-adresse.data.gouv.fr (cached in `meta` as `geocode:<address>`) and measured against `src/idf_boundary.json` (simplified IDF outline, ~300 m tolerance). Fails open (keeps the listing) when geocoding fails.
- Alert headline comes from `geo_filter.ZoneClassifier.headline` (called in `MonitorService.poll_once`, passed to both notifiers): `(Île-de-France)` for IDF postcodes/coordinates; `(Nearby Île-de-France)` when the fastest public-transport journey from École 42 Paris (`ECOLE_42`), departing next weekday 08:00, is ≤ 90 min; else `(Not Île-de-France)`. Journeys come from Transitous (`api.transitous.org`, free keyless MOTIS router over French GTFS), only for places within `TRANSIT_CHECK_MAX_KM` (250) of 42, cached per destination in meta as `transit42:<lon>,<lat>` (`-1` = no route). Routing/geocoding failure tags the listing not IDF (not cached, retried next time). `zoneinfo` needs the `tzdata` dependency on Vercel.
- `src/listing_details.py` — `build_details` computes once per alert (in `poll_once`) what both notifiers show: postcode + department name, Google Maps address link, straight-line distance from Paris centre, public-transport minutes from École 42 (same cached Transitous lookup as `ZoneClassifier`, places ≤ 250 km only), Maps directions links (origin = viewer's location). Missing pieces (geocoding/routing failure) are just omitted.
- `src/discord_notifier.py` — optional Discord webhook sender (enabled when `DISCORD_WEBHOOK_URL` set). Mirrors listing alerts (embed built from `ListingDetails`, photo as embed image) and healthchecks only; failures are caught in `MonitorService._send_discord_listing` so they never block Telegram. Throttled to 1/s, retries once on 429.
- First cycle on a state store without meta `initial_sync_at` (new Redis DB, fresh SQLite) records all current listings as seen without alerting, then sets the key; alerts start from the next cycle. Delete the key to resync silently.
- `MonitorService._take_new` calls `filter_new(listings, limit=MAX_ALERTS_PER_CYCLE)` (40): only the first 40 unseen listings are marked seen and alerted; the rest stay unseen and alert on later cycles. Keeps a big backlog inside Vercel's 300s `maxDuration`.
- `src/models.py` — `Listing` dataclass; `fingerprint` = sha256 of external_id|url|title|price. A price or title change produces a new fingerprint and re-alerts.
- `api/check.py` — Vercel entrypoint (`pyproject.toml` `[tool.vercel] entrypoint`). Auth via `CRON_SECRET` (Bearer header or `?key=`), `?test=1` adds a healthcheck, calls `MonitorService.run_cycle()` under a Redis lock (`acquire_lock`, TTL 300s) so overlapping pings skip instead of running twice. Vercel installs deps from `pyproject.toml`, **not** `requirements.txt` — keep both in sync. `vercel.json` pins region `cdg1`. Deployed as project `crous-bot` (`crous-bot-ten.vercel.app`), triggered every 2 min by cron-job.org because Hobby cron is daily-only.
- `src/log_redaction.py` — `configure_logging` (used by `main.py` and `api/check.py`) masks secret env values (bot token, webhook URL, `CRON_SECRET`, Redis token) in every log line, including tracebacks where requests echoes request URLs.
- `src/state_store.py` — `StateStore` base holds timing logic and `acquire_lock`/`release_lock`. Backends picked by `create_state_store`: `TursoStateStore` when `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` are set (production; libSQL HTTP `/v2/pipeline`, same `seen_listings`/`meta` schema as SQLite, lock = atomic upsert on meta `lock:<name>` with an expiry timestamp), else `RedisStateStore` (Upstash REST, `KV_REST_API_*` / `UPSTASH_REDIS_REST_*`), else `SqliteStateStore` (local, `STATE_DB_PATH`). `meta` holds runtime state: Telegram update offset, filter overrides set via Telegram commands, consecutive failure count, last heartbeat / error alert timestamps, geocode and transit caches, `initial_sync_at`. Production moved off Upstash because its free tier (500K commands/month, billed per command) ran out; Neon's free tier (100 CU-hours) also can't stay awake for 2-min pings. Turso free is billed per row (500M reads/month), and a cycle reads ~170 rows. Keep dedup to one query per cycle on every backend.
- `src/config.py` — `load_settings()` reads env into a frozen `Settings`. Plural env vars (`CROUS_SEARCH_URLS`, `TELEGRAM_CHAT_IDS`, comma-separated) override singular ones.

### Behaviors worth knowing

- Filters run **before** dedup, so filtered-out listings are not recorded as seen and will alert later if filters loosen.
- Effective filters = env defaults (`FILTER_MAX_PRICE_EUR`, `FILTER_INCLUDE_KEYWORDS`) overridden by `meta` values set via Telegram commands (empty string = cleared override, falls back to env).
- `--dry-run` skips Telegram sends and meta writes from commands, but `poll_once` still calls `StateStore.filter_new`, which **inserts listings into `seen_listings`**. A dry run against the real DB will suppress future alerts for those listings.
- Heartbeat: first call only records a timestamp; it sends once `HEARTBEAT_INTERVAL_HOURS` elapse. Error alerts fire after `ERROR_ALERT_THRESHOLD` consecutive failures, rate-limited by `ERROR_ALERT_COOLDOWN_MINUTES`.

## Deployment

- **GitHub Actions** (`.github/workflows/crous-monitor.yml`): cron every 5 min, runs `--once`. State DB lives at `.state/state.db` and persists via `actions/cache` (saved under `crous-state-<run_id>`, restored by prefix). Cache eviction resets dedup history. Secrets: `CROUS_SEARCH_URL(S)`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID(S)`, optional `DISCORD_WEBHOOK_URL`; other settings are hardcoded in the workflow `env`. Manual dispatch input `test_telegram=true` adds `--test-telegram`. New env settings must be added to both `config.py` and the workflow `env` block (and `.env.example` / README).
- **Termux/Android** (`deploy/termux/`): `start_monitor.sh` runs continuous mode from `$HOME/crous-bot`, logging to `logs/monitor.log`.
