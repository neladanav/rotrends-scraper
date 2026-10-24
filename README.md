# RoTrends -> Scoutbase collector

The existing cron-job.org dispatch still invokes `.github/workflows/scraper.yml` on `main` every 15 minutes. There is no new scraper schedule.

The Action opens the original filtered RoTrends table, reads page 1 plus a rotating deeper page, resolves Roblox universe IDs and sends every collected row to Scoutbase. The default batch is two pages so each job stays bounded; the deeper cursor continues across runs until a verified empty table resets it to page 2. Missing tables, repeated pages, rate limits or failed ingestion leave the unacknowledged cursor in place and fail the Action honestly. An optional `ROTATING_MAX_PAGE` retains support for an explicit bounded sweep; the default is no cumulative page cap.

Scoutbase stages observations and uses its existing private worker to verify current Roblox metrics and upsert by universe ID. Prior human decisions, notes, claims, content checks, contact ownership and historical observations are retained. A delivery acknowledgment means queued, not yet verified or acquisition-approved.

Authentication uses GitHub Actions OIDC with audience `dt-scoutbase-ingest`. Scoutbase verifies GitHub's signature and restricts repository ID, owner ID, branch, event and workflow path. No Supabase database key or reusable ingestion secret is stored in this repository. Keep `id-token: write` on the existing scrape job and keep its path/ref unchanged. Tokens are held in memory and never logged or committed.

`scan_cursor.json` records acknowledged page progress and failures. `identity_cache.json` caches only public place-to-universe mappings. Neither file suppresses recollection. Discord notification code, webhook configuration and notification-deduplication state have been removed.

Run unit tests with `python -m unittest discover -s tests -v`. `DRY_RUN=true python bot.py` performs read-only source parsing/identity checks without delivery or saved-state changes. This does not bypass provider controls: HTTP failures/rate limits stop the batch, and missing/invalid counts remain unknown rather than zero.
