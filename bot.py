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
MAX_PAGES = max(1, int(os.environ.get("MAX_PAGES", "4")))
REQUEST_TIMEOUT = (5, 20)

# Keep the existing acquisition thresholds unchanged.
EXCLUSION_WORDS = [
    "modded", "mod", "reuploaded", "reupload", "nsfw", "18+", "test",
    "español", "русский",
]


def parse_number(text_val):
    """Parse plain counts and abbreviated counts such as 12.4K, 3M, or 1.2B."""
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
    elif visits < 100000 and active >= 600:
        return ("🌟 GREAT GAME FOUND", "🥇 GREAT GAME", 16753920)
    elif visits < 200000 and active >= 800:
        return ("✨ GOOD GAME FOUND", "🥈 GOOD GAME", 16776960)
    elif visits <= 500000 and active >= 800:
        return ("👍 MID GAME FOUND", "🥉 MID GAME", 3447003)
    return None


def _normalize_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def _game_id_from_url(url):
    """Return a stable Roblox place/universe-like numeric ID when present in a URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
        if not (parsed.hostname or "").lower().endswith("roblox.com"):
            return None
        match = re.search(r"/games/(\d+)", parsed.path)
        return match.group(1) if match else None
    except (TypeError, ValueError):
        return None


def game_key(game_name, game_url=None):
    stable_id = _game_id_from_url(game_url)
    if stable_id:
        return f"roblox:{stable_id}"
    return f"name:{_normalize_name(game_name)}"


def load_seen_games():
    """Read previous state; migrate the old list-of-names format in memory."""
    if not STATE_FILE.exists():
        return set()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Fail closed: don't treat every game as new if history is unreadable.
        raise RuntimeError(
            f"Could not safely read {STATE_FILE}; preserving it and aborting this run."
        ) from exc

    if isinstance(data, list):
        return {f"name:{_normalize_name(item)}" for item in data if str(item).strip()}
    if isinstance(data, dict) and isinstance(data.get("seen"), list):
        return {str(item) for item in data["seen"] if str(item).strip()}
    raise RuntimeError(f"Unexpected state format in {STATE_FILE}; refusing to overwrite it.")


def save_seen_games(seen_games):
    """Atomically replace state so interruption cannot leave a truncated JSON file."""
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


def is_excluded(game_name):
    """Use token boundaries so normal words containing 'mod' aren't auto-rejected."""
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
        locator = verify_page.locator("li:has-text('Visits') .text-lead").first
        locator.wait_for(timeout=5000)
        return parse_number(locator.inner_text())
    except Exception as exc:
        print(f"Roblox visit verification unavailable for {url}: {exc}")
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


def main():
    seen_games = load_seen_games()
    # New keys are only persisted after the corresponding webhook request succeeds.
    delivered_keys = set()
    alerts_sent = 0
    scanned_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        web_page = browser.new_page()
        try:
            for page_num in range(1, MAX_PAGES + 1):
                target_url = f"{BASE_URL}{page_num}"
                print(f"\n--- Loading page {page_num}/{MAX_PAGES} ---")
                try:
                    web_page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                    web_page.wait_for_timeout(2500)
                    web_page.wait_for_selector(
                        "table tbody tr.ant-table-row", timeout=10000
                    )
                except Exception as exc:
                    print(f"Failed to load table on page {page_num}: {exc}")
                    # Avoid writing potentially incomplete state if the first page failed.
                    if page_num == 1:
                        raise RuntimeError("The first RoTrends page could not be read.") from exc
                    break

                rows = web_page.locator("table tbody tr.ant-table-row").all()
                print(f"Found {len(rows)} games on page {page_num}.")
                if not rows:
                    break

                for row in rows:
                    try:
                        cols = row.locator("td").all()
                        if len(cols) < 5:
                            continue

                        game_name = cols[0].inner_text().strip()
                        if not game_name or "No data" in game_name:
                            continue
                        if is_excluded(game_name):
                            continue

                        game_url = None
                        links = cols[0].locator("a")
                        for index in range(links.count()):
                            href = links.nth(index).get_attribute("href")
                            if href and "roblox.com/games/" in href:
                                game_url = href if href.startswith("http") else "https://www.roblox.com" + href
                                break
                        if not game_url:
                            game_url = (
                                "https://www.roblox.com/search/experiences?keyword="
                                + quote(game_name)
                            )

                        key = game_key(game_name, game_url)
                        # Match legacy name-only history as well as new stable-ID keys,
                        # preventing a one-time duplicate burst after state migration.
                        legacy_name_key = f"name:{_normalize_name(game_name)}"
                        if (
                            key in seen_games
                            or legacy_name_key in seen_games
                            or key in delivered_keys
                            or legacy_name_key in delivered_keys
                        ):
                            continue

                        visits_text = cols[1].inner_text().strip()
                        active_text = cols[2].inner_text().strip()
                        favorites_text = cols[4].inner_text().strip()
                        visits = parse_number(visits_text)
                        active = parse_number(active_text)
                        scanned_count += 1
                        print(f"Checking: {game_name} | Visits: {visits} | CCU: {active}")

                        if visits <= 0 or visits > 500000:
                            continue

                        reported_source = "RoTrends"
                        real_visits = verify_roblox_visits(browser, game_url)
                        if real_visits is not None and real_visits > 0:
                            if real_visits > 500000:
                                continue
                            visits = real_visits
                            reported_source = "Roblox page"
                        else:
                            print(f"Visit count for {game_name} is unverified; using RoTrends data.")

                        tier_data = determine_tier(visits, active)
                        if not tier_data:
                            continue

                        title, award, color = tier_data
                        description = (
                            f"🎮 **Game:** `{game_name}`\n"
                            "👤 **Creator:** Unknown\n\n"
                            f"👥 **CCU:** {active_text}\n"
                            f"👁️ **Visits:** {visits_text} ({reported_source} source)\n"
                            f"⭐ **Favorites:** {favorites_text}\n\n"
                            f"{award}\n\n"
                            f"🔗 [Open on Roblox]({game_url})"
                        )
                        alert = {"title": title, "description": description, "color": color}
                        try:
                            post_discord_alert(alert)
                        except requests.RequestException as exc:
                            print(f"Discord delivery failed for {game_name}; not recording it as seen: {exc}")
                            continue
                        except RuntimeError as exc:
                            print(f"Discord delivery skipped for {game_name}: {exc}")
                            continue

                        delivered_keys.add(key)
                        alerts_sent += 1
                        print(f"-> Alert delivered: {game_name} ({award})")
                    except Exception as exc:
                        # A malformed row should not discard other valid rows.
                        print(f"Skipping a row due to an unexpected error: {exc}")
        finally:
            browser.close()

    updated_seen = seen_games | delivered_keys
    save_seen_games(updated_seen)
    print(
        f"\nScanned {scanned_count} candidate rows; successfully sent {alerts_sent} "
        f"Discord alert(s). Saved {len(updated_seen)} seen-game keys."
    )


if __name__ == "__main__":
    main()
