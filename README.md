# RoTrends -> Scoutbase collector

The existing cron-job.org dispatch still invokes `.github/workflows/scraper.yml` on `main` every 15 minutes. There is no new scraper schedule.

The Action opens the original filtered RoTrends table, reads up to three consecutive pages, resolves Roblox universe IDs and sends every collected row to Scoutbase. A new sweep runs 1–3, then 4–6, then 7–9, and continues until a verified empty table resets the next run to page 1. Existing version-2 cursor progress is retained when upgrading; it does not restart at page 1. No last page is guessed from a short table. An optional `ROTATING_MAX_PAGE` retains support for an explicitly configured bounded sweep; the default is no cumulative page cap.

Every page advances the cursor only after Scoutbase acknowledges delivery. Missing tables, repeated pages, rate limits or failed ingestion leave the unacknowledged page in place and fail the Action honestly. Repeating an uncertain delivery is safe because ingestion is idempotent. Pausing collection retains progress. The Linux runner enforces a seven-minute in-process collection budget; requests and retry waits respect the remaining time, and a budget stop saves progress for the next scheduled run. The collection step has an eight-minute safety limit inside the existing ten-minute job limit, leaving room for checkpoint commits. The existing serialized concurrency group prevents overlapping collectors. GitHub queue/setup delays mean the 15-minute trigger is a target, not an exact execution-time guarantee.

Scoutbase stages observations and uses its existing private worker to verify current Roblox metrics and upsert by universe ID. Prior human decisions, notes, claims, content checks, contact ownership and historical observations are retained. A delivery acknowledgment means queued, not yet verified or acquisition-approved.

Authentication uses GitHub Actions OIDC with audience `dt-scoutbase-ingest`. Scoutbase verifies GitHub's signature and restricts repository ID, owner ID, branch, event and workflow path. No Supabase database key or reusable ingestion secret is stored in this repository. Keep `id-token: write` on the existing scrape job and keep its path/ref unchanged. Tokens are held in memory and never logged or committed.

`scan_cursor.json` records acknowledged page progress and failures. `identity_cache.json` caches only public place-to-universe mappings. Neither file suppresses recollection. Discord notification code, webhook configuration and notification-deduplication state have been removed.

Run unit tests with `python -m unittest discover -s tests -v`. `DRY_RUN=true python bot.py` performs read-only source parsing/identity checks without delivery or saved-state changes. This does not bypass provider controls: HTTP failures/rate limits stop the batch, and missing/invalid counts remain unknown rather than zero.

