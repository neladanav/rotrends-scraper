import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")
BASE_URL = (
    "https://www.rotrends.com/games?page_size=50&visits_max=500000"
    "&playing_min=100&page="
)
STATE_FILE = Path(os.environ.get("SEEN_GAMES_FILE", "seen_games.json"))
SCAN_CURSOR_FILE = Path(os.environ.get("SCAN_CURSOR_FILE", "scan_cursor.json"))
MAX_PAGES = max(1, int(os.environ.get("MAX_PAGES", "2")))
ROTATING_MAX_PAGE = max(2, int(os.environ.get("ROTATING_MAX_PAGE", "20")))
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
VERIFY_ROBLOX_VISITS = os.environ.get("VERIFY_ROBLOX_VISITS", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
REQUEST_TIMEOUT = (5, 20)

# Keep the existing acquisition thresholds and exclusions unchanged.
EXCLUSION_WORDS = [
    "modded", "mod", "reuploaded", "reupload", "nsfw", "18+", "test",
    "español", "русский",
]


def parse_number(text_val):
    """Parse counts like 12.4K, 3M, 1.2B; invalid or missing values become zero."""
    text = str(text_val or "").strip().upper().replace(",", "").replace("+", "")
    match = re.fullmatch(r"\s*([-+]?\d*\.?\d+)\s*([KMB]?)\s*", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return number * multiplier[match.group(2)]


def determine_tier(visits, active):
    if visits < 40000 and active >= 100:
        return ("🔥 GOD TIER GAME FOUND", "🏆 GOD TIER", 16711680)
    if visits < 100000 and active >= 600:
        return ("🌟 GREAT GAME FOUND", "🥇 GREAT GAME", 16753920)
    if visits < 200000 and active >= 800:
        return ("✨ GOOD GAME FOUND", "🥈 GOOD GAME", 16776960)
    if visits <= 500000 and active >= 800:
        return ("👍 MID GAME FOUND", "🥉 MID GAME", 3447003)
    return None


def _normalize_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def _game_id_from_url(url):
    """Return a stable numeric Roblox game ID only for a Roblox hostname."""
    if not url:
        return None
    try:
        parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
        hostname = (parsed.hostname or "").lower()
        if hostname != "roblox.com" and not hostname.endswith(".roblox.com"):
            return None
        match = re.search(r"/games/(\d+)", parsed.path)
        return match.group(1) if match else None
    except (TypeError, ValueError):
        return None


def _is_numeric_creator(candidate):
    """Reject score/rating values accidentally read from the creator column."""
    cleaned = re.sub(r"^@", "", str(candidate or "")).strip()
    return bool(cleaned) and bool(re.fullmatch(r"[\d,.+%KMBkmb\s-]+", cleaned))


def extract_creator_from_title_cell(cell_text):
    """Extract an explicit @handle shown beneath a RoTrends game title."""
    lines = str(cell_text or "").splitlines()
    for line in lines[1:]:
        candidate = line.strip()
        if not candidate.startswith("@"):
            continue
        cleaned = candidate[1:].strip()
        if (
            cleaned
            and not _is_numeric_creator(cleaned)
            and cleaned.casefold() not in {
                "[object object]", "object object", "unknown", "null", "none"
            }
        ):
            return cleaned
    return None


def game_key(game_name, game_url=None):
    stable_id = _game_id_from_url(game_url)
    if stable_id:
        return f"roblox:{stable_id}"
    return f"name:{_normalize_name(game_name)}"


def load_seen_games():
    """Read previous state; accept the old list format without rewriting it yet."""
    if not STATE_FILE.exists():
        return set()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not safely read {STATE_FILE}; preserving it and aborting this run."
        ) from exc

    if isinstance(data, list):
        return {f"name:{_normalize_name(item)}" for item in data if str(item).strip()}
    if isinstance(data, dict) and isinstance(data.get("seen"), list):
        return {str(item) for item in data["seen"] if str(item).strip()}
    raise RuntimeError(f"Unexpected state format in {STATE_FILE}; refusing to overwrite it.")


def save_seen_games(seen_games):
    """Atomically replace state so interruption cannot leave truncated JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=STATE_FILE.name + ".", suffix=".tmp", dir=str(STATE_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "seen": sorted(seen_games)}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, STATE_FILE)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)



def load_scan_cursor():
    """Load the next rotating page; missing/corrupt cursor safely starts at page 2."""
    try:
        with SCAN_CURSOR_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        page = int(data.get("next_page", 2)) if isinstance(data, dict) else 2
        return page if 2 <= page <= ROTATING_MAX_PAGE else 2
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 2


def save_scan_cursor(next_page):
    """Persist the next rotating page atomically between scheduled runs."""
    page = int(next_page)
    if page < 2 or page > ROTATING_MAX_PAGE:
        page = 2
    SCAN_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=SCAN_CURSOR_FILE.name + ".", suffix=".tmp",
        dir=str(SCAN_CURSOR_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "next_page": page}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, SCAN_CURSOR_FILE)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def select_pages(next_page, pages_per_run, max_page):
    """Scan page 1 every run plus a bounded rotating window of deeper pages."""
    max_page = max(2, int(max_page))
    pages_per_run = max(1, int(pages_per_run))
    cursor = int(next_page)
    if cursor < 2 or cursor > max_page:
        cursor = 2
    pages = [1]
    while len(pages) < pages_per_run:
        if cursor not in pages:
            pages.append(cursor)
        cursor = 2 if cursor >= max_page else cursor + 1
    return pages


def is_excluded(game_name):
    """Use token boundaries to avoid excluding normal names containing 'mod'."""
    normalized = _normalize_name(game_name)
    for word in EXCLUSION_WORDS:
        if word in {"español", "русский", "nsfw", "18+"}:
            if word in normalized:
                return True
        elif re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", normalized):
            return True
    return False


def verify_roblox_visits(browser, url):
    """Best-effort independent visit check; None means unavailable, not zero."""
    if not url or "search/experiences" in url:
        return None

    verify_page = browser.new_page()
    try:
        verify_page.goto(url, timeout=20000, wait_until="domcontentloaded")
        # Roblox changes its markup; try common visible labels instead of one brittle selector.
        candidates = [
            verify_page.locator("li:has-text('Visits') .text-lead").first,
            verify_page.get_by_text(re.compile(r"\bVisits\b", re.I)).first,
        ]
        for locator in candidates:
            try:
                locator.wait_for(timeout=1500)
                text = locator.inner_text().strip()
                match = re.search(r"([\d,.]+\s*[KMB]?\+?)", text, re.I)
                if match:
                    parsed = parse_number(match.group(1))
                    if parsed > 0:
                        return parsed
            except Exception:
                continue
        return None
    except Exception as exc:
        print(f"Roblox visit verification unavailable: {type(exc).__name__}")
        return None
    finally:
        verify_page.close()


def post_discord_alert(alert):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK is not configured; refusing to mark alerts as sent.")
    response = requests.post(
        WEBHOOK_URL,
        json={"embeds": [alert]},
        timeout=REQUEST_TIMEOUT,
    )
    try:
        if response.status_code == 429:
            print(f"Discord rate limited the webhook: {response.text[:300]}")
        response.raise_for_status()
    finally:
        response.close()


def get_game_rows(page):
    """Wait for a usable table and support RoTrends table class changes."""
    selectors = ("table tbody tr.ant-table-row", "table tbody tr")
    last_error = None
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=5000)
            rows = page.locator(selector).all()
            if rows:
                return rows
        except Exception as exc:
            last_error = exc
    raise RuntimeError("No game table rows appeared on the RoTrends page.") from last_error


def main():
    if not DRY_RUN and not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK is missing. Set DRY_RUN=true to test without Discord.")

    seen_games = load_seen_games()
    delivered_keys = set()
    alerts_sent = 0
    qualified_count = 0
    scanned_count = 0
    seen_skips = 0
    pages_read = 0
    scan_cursor = load_scan_cursor() if not DRY_RUN else 2
    planned_pages = select_pages(scan_cursor, MAX_PAGES, ROTATING_MAX_PAGE)
    rotating_pages_completed = 0

    print(f"Mode: {'DRY RUN (no Discord messages or state writes)' if DRY_RUN else 'LIVE'}")
    print(f"Pages this run: {planned_pages} (page 1 stays hot; deeper pages rotate 2-{ROTATING_MAX_PAGE}); independent Roblox visit check: {VERIFY_ROBLOX_VISITS}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        web_page = browser.new_page()
        try:
            for page_num in planned_pages:
                target_url = f"{BASE_URL}{page_num}"
                print(f"\n--- Loading RoTrends page {page_num} ---")
                try:
                    response = web_page.goto(
                        target_url, timeout=30000, wait_until="domcontentloaded"
                    )
                    if response and response.status >= 400:
                        raise RuntimeError(f"RoTrends returned HTTP {response.status}")
                    web_page.wait_for_timeout(1500)
                    rows = get_game_rows(web_page)
                except Exception as exc:
                    print(f"Failed to read RoTrends page {page_num}: {exc}")
                    if page_num == 1:
                        raise RuntimeError("The first RoTrends page could not be read.") from exc
                    break

                pages_read += 1
                if page_num != 1:
                    rotating_pages_completed += 1
                print(f"Found {len(rows)} game rows on page {page_num}.")
                if not rows:
                    break

                for row in rows:
                    try:
                        cols = row.locator("td").all()
                        if DRY_RUN and os.environ.get("DEBUG_ROW_LAYOUT", "").strip().lower() in {"1", "true", "yes"} and scanned_count < 3:
                            print(f"[DEBUG ROW {scanned_count + 1}] columns=" + repr([cell.inner_text().strip()[:160] for cell in cols]))
                            print(f"[DEBUG TITLE CELL HTML {scanned_count + 1}] " + cols[0].inner_html()[:1200])
                        if len(cols) < 5:
                            print(f"Skipping row with only {len(cols)} columns.")
                            continue

                        # Prefer the actual linked game title; React-backed cells can stringify
                        # structured values as the literal "[object Object]".
                        game_name = ""
                        links = cols[0].locator("a")
                        for index in range(links.count()):
                            try:
                                linked_name = links.nth(index).inner_text().strip()
                            except Exception:
                                continue
                            if linked_name and linked_name.casefold() not in {
                                "[object object]", "object object", "no data"
                            }:
                                game_name = linked_name
                                break
                        if not game_name:
                            game_name = cols[0].inner_text().strip()
                        if (
                            not game_name
                            or game_name.casefold() in {"[object object]", "object object"}
                            or "No data" in game_name
                        ):
                            print("Skipping row with an invalid game title.")
                            continue
                        if is_excluded(game_name):
                            continue

                        game_url = None
                        for index in range(links.count()):
                            href = links.nth(index).get_attribute("href")
                            if href and re.search(r"(?:www\.)?roblox\.com/games/\d+", href, re.I):
                                game_url = (
                                    href if href.startswith(("http://", "https://"))
                                    else "https://www.roblox.com" + (href if href.startswith("/") else "/" + href)
                                )
                                break
                        if not game_url:
                            game_url = (
                                "https://www.roblox.com/search/experiences?keyword="
                                + quote(game_name)
                            )

                        key = game_key(game_name, game_url)
                        legacy_name_key = f"name:{_normalize_name(game_name)}"
                        if (
                            key in seen_games
                            or legacy_name_key in seen_games
                            or key in delivered_keys
                            or legacy_name_key in delivered_keys
                        ):
                            seen_skips += 1
                            if seen_skips <= 5:
                                print(f"[DEDUP] Already alerted/seen; suppressing repeat: {game_name}")
                            continue

                        visits_text = cols[1].inner_text().strip()
                        active_text = cols[2].inner_text().strip()
                        creator = "Unknown"
                        creator_candidates = []

                        # RoTrends currently places the creator handle beneath the game
                        # title in the first cell. Prefer this explicit handle over the
                        # numeric rating/score columns.
                        try:
                            title_cell_creator = extract_creator_from_title_cell(cols[0].inner_text())
                            if title_cell_creator:
                                creator_candidates.append(title_cell_creator)
                        except Exception:
                            pass

                        # Some layouts expose the creator as a linked profile in the title cell.
                        try:
                            title_links = cols[0].locator("a")
                            for creator_index in range(title_links.count()):
                                creator_link = title_links.nth(creator_index)
                                href = creator_link.get_attribute("href") or ""
                                if re.search(r"/(users|groups)/\\d+", href, re.I):
                                    for attr in ("aria-label", "title"):
                                        attr_value = creator_link.get_attribute(attr)
                                        if attr_value:
                                            creator_candidates.append(attr_value.strip())
                                    try:
                                        creator_candidates.append(creator_link.inner_text().strip())
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                        # Fallback to the historical creator column only when it contains
                        # a real textual handle, never a score or rating number.
                        if len(cols) > 3:
                            try:
                                creator_candidates.append(cols[3].inner_text().strip())
                            except Exception:
                                pass

                        for candidate in creator_candidates:
                            cleaned = re.sub(r"^@", "", candidate).strip()
                            if (
                                cleaned
                                and not _is_numeric_creator(cleaned)
                                and cleaned.casefold() not in {
                                    "[object object]", "object object", "unknown", "null", "none"
                                }
                            ):
                                creator = cleaned
                                break
                        favorites_text = cols[4].inner_text().strip()
                        visits = parse_number(visits_text)
                        active = parse_number(active_text)
                        scanned_count += 1
                        creator_display = f"@{creator}" if creator != "Unknown" else "Unknown"
                        print(f"Checking: {game_name} | Creator: {creator_display} | Visits: {visits:g} | CCU: {active:g}")

                        if visits <= 0 or visits > 500000:
                            continue

                        reported_source = "RoTrends"
                        if VERIFY_ROBLOX_VISITS:
                            real_visits = verify_roblox_visits(browser, game_url)
                            if real_visits is not None and real_visits > 0:
                                if real_visits > 500000:
                                    print(f"Filtered {game_name}: verified visits exceed 500K.")
                                    continue
                                visits = real_visits
                                reported_source = "Roblox page"
                            else:
                                print(f"Visit count for {game_name} is unverified; using RoTrends data.")

                        tier_data = determine_tier(visits, active)
                        if not tier_data:
                            continue

                        title, award, color = tier_data
                        qualified_count += 1
                        description = (
                            f"🎮 **Game:** `{game_name}`\n"
                            f"👤 **Creator:** {creator_display}\n\n"
                            f"👥 **CCU:** {active_text}\n"
                            f"👁️ **Visits:** {visits_text} ({reported_source} source)\n"
                            f"⭐ **Favorites:** {favorites_text}\n\n"
                            f"{award}\n\n"
                            f"🔗 [Open on Roblox]({game_url})"
                        )
                        alert = {"title": title, "description": description, "color": color}

                        if DRY_RUN:
                            print(f"[DRY RUN] Eligible: {game_name} | {creator_display} | {award} | visits={visits:g} | CCU={active:g}")
                            continue

                        try:
                            post_discord_alert(alert)
                        except (requests.RequestException, RuntimeError) as exc:
                            print(f"Discord delivery failed for {game_name}; not recording it as seen: {exc}")
                            continue

                        delivered_keys.add(key)
                        alerts_sent += 1
                        print(f"-> Alert delivered: {game_name} ({award})")
                    except Exception as exc:
                        print(f"Skipping a row due to an unexpected error: {type(exc).__name__}: {exc}")
        finally:
            browser.close()

    if DRY_RUN:
        print(
            f"\nDRY RUN SUMMARY: read {pages_read} page(s), scanned {scanned_count} "
            f"candidate row(s), found {qualified_count} eligible game(s). "
            "No Discord messages sent; state file was not changed."
        )
        return

    updated_seen = seen_games | delivered_keys
    save_seen_games(updated_seen)
    if rotating_pages_completed:
        next_cursor = scan_cursor
        for _ in range(rotating_pages_completed):
            next_cursor = 2 if next_cursor >= ROTATING_MAX_PAGE else next_cursor + 1
        save_scan_cursor(next_cursor)
        print(f"Saved rotating scan cursor: next deeper page {next_cursor}.")
    print(
        f"\nScanned {scanned_count} candidate rows; found {qualified_count} eligible game(s); "
        f"successfully sent {alerts_sent} Discord alert(s). Saved {len(updated_seen)} seen-game keys."
    )


if __name__ == "__main__":
    main()
