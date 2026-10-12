import os
import json
import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")
BASE_URL = "https://www.rotrends.com/games?page_size=50&visits_max=500000&playing_min=100&page="
STATE_FILE = "seen_games.json"
EXCLUSION_WORDS = ["mod", "modded", "reuploaded", "reupload", "nsfw", "18+", "test", "español", "русский"]

def load_seen_games():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return []

def parse_number(text_val):
    text_val = text_val.strip().upper().replace(",", "").replace("+", "")
    if "K" in text_val: return float(text_val.replace("K", "")) * 1000
    if "M" in text_val: return float(text_val.replace("M", "")) * 1000000
    if "B" in text_val: return float(text_val.replace("B", "")) * 1000000000
    try: return float(text_val)
    except: return 0

def determine_tier(visits, active):
    if visits < 40000 and active >= 100: return ("🔥 GOD TIER GAME FOUND", "🏆 GOD TIER", 16711680)
    elif visits < 100000 and active >= 600: return ("🌟 GREAT GAME FOUND", "🥇 GREAT GAME", 16753920)
    elif visits < 200000 and active >= 800: return ("✨ GOOD GAME FOUND", "🥈 GOOD GAME", 16776960)
    elif visits <= 500000 and active >= 800: return ("👍 MID GAME FOUND", "🥉 MID GAME", 3447003)
    return None

def verify_roblox_visits(browser, url):
    if not url or "search/experiences" in url:
        return None 
    
    verify_page = browser.new_page()
    try:
        # TIMEOUT FIX: Don't wait forever, and only wait for the DOM, not images/network
        verify_page.goto(url, timeout=10000, wait_until="domcontentloaded")
        
        # SMART WAIT: Instead of sleeping 3.5 seconds every time, wait up to 5s specifically for the text to appear
        locator = verify_page.locator("li:has-text('Visits') >> .text-lead")
        locator.wait_for(timeout=5000)
        
        if locator.count() > 0:
            text_val = locator.first.inner_text()
            return parse_number(text_val)
    except Exception as e:
        pass
    finally:
        verify_page.close() 
        
    return None

def main():
    seen_games = load_seen_games()
    new_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        web_page = browser.new_page()

        page_num = 1
        
        # PAGE LIMIT: Only check the first 5 pages (up to 250 games max). 
        # This guarantees the script will finish within your 10-minute window.
        while page_num <= 5:
            target_url = f"{BASE_URL}{page_num}"
            
            try:
                web_page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                # SMART WAIT: Wait for the table to load instead of sleeping 3.5s
                web_page.wait_for_selector("table tbody tr", timeout=10000)
            except Exception:
                break # If page fails to load, stop scraping

            rows = web_page.locator("table tbody tr").all()
            if not rows:
                break

            for row in rows:
                cols = row.locator("td").all()
                if len(cols) >= 5:
                    game_name = cols[0].inner_text().strip()

                    # Skip games with missing names or excluded words immediately
                    if "No data" in game_name or not game_name: continue
                    if any(w in game_name.lower() for w in EXCLUSION_WORDS): continue
                    if game_name in seen_games: continue

                    game_url = None
                    links = cols[0].locator("a")
                    for i in range(links.count()):
                        href = links.nth(i).get_attribute("href")
                        if href and "roblox.com/games" in href:
                            game_url = href
                            break

                    if not game_url:
                        game_url = f"https://www.roblox.com/search/experiences?keyword={game_name.replace(' ', '%20')}"

                    visits_text = cols[1].inner_text().strip()
                    active_text = cols[2].inner_text().strip()
                    favorites_text = cols[4].inner_text().strip()

                    visits = parse_number(visits_text)
                    active = parse_number(active_text)

                    if visits == 0 or visits > 500000: continue

                    # Verify real visits
                    real_visits = verify_roblox_visits(browser, game_url)
                    if real_visits is not None:
                        if real_visits > 500000:
                            continue 
                        visits = real_visits
                        
                    tier_data = determine_tier(visits, active)
                    
                    if tier_data:
                        title, award, color = tier_data
                        description = (
                            f"🎮 **Game:** `{game_name}`\n"
                            f"👤 **Creator:** Unknown\n\n"
                            f"👥 **CCU:** {active_text}\n"
                            f"👁️ **Visits:** {visits_text}\n"
                            f"⭐ **Favorites:** {favorites_text}\n\n"
                            f"{award}\n\n"
                            f"🔗 [Open on Roblox]({game_url})"
                        )
                        new_alerts.append({"title": title, "description": description, "color": color})
                        seen_games.append(game_name)
            
            page_num += 1

        browser.close()

    if new_alerts:
        if WEBHOOK_URL:
            for embed in new_alerts:
                requests.post(WEBHOOK_URL, json={"embeds": [embed]})
        
        # Save memory only after all games are processed
        with open(STATE_FILE, "w") as f:
            json.dump(seen_games, f)

if __name__ == "__main__":
    main()
