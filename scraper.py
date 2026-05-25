import pandas as pd
import os
import time
import re
import logging
from datetime import date, timedelta, timezone, datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager
import gspread
from google.oauth2.service_account import Credentials

# ==============================
# ✅ PATHS
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
LOG_PATH = os.path.join(BASE_DIR, "scraper.log")

# ==============================
# ✅ TIMEZONE — ROOT CAUSE #1
# OneFootball renders times in the browser's local timezone.
# Headless Chrome has NO timezone by default, so it falls back to
# whatever the OS has — which changes per environment/run.
# We fix this by hardcoding WIB (UTC+7) in two places:
#   1. Chrome's --lang and timezone CDP command (forces the JS Date object)
#   2. Our own ISO datetime parser (converts UTC → WIB deterministically)
# ==============================
WIB = timezone(timedelta(hours=7))  # Asia/Jakarta

# ==============================
# ✅ LOGGING
# ==============================
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)

# ==============================
# ✅ DRIVER — with forced timezone
# ==============================
def get_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    # ROOT CAUSE FIX #1: force browser language/locale
    # This makes JS Intl and Date.toLocaleString() behave consistently
    options.add_argument("--lang=en-US")

    # Disable images for speed
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    # ROOT CAUSE FIX #2: set timezone via Chrome DevTools Protocol
    # This overrides the OS timezone inside the browser's JS engine
    # so Date objects always produce consistent UTC+7 output
    driver.execute_cdp_cmd(
        "Emulation.setTimezoneOverride",
        {"timezoneId": "Asia/Jakarta"}
    )

    return driver

# ==============================
# ✅ ISO DATETIME PARSER
# ROOT CAUSE FIX #3: read the raw UTC ISO string from the <time datetime="">
# attribute and convert it to WIB ourselves.
# This is 100% deterministic — no reliance on browser rendering or JS.
# Example: "2025-05-28T19:00:00Z" → "02:00" (WIB = UTC+7)
# ==============================
def iso_to_wib_time(iso_str):
    """Convert ISO 8601 UTC string to HH:MM in WIB (UTC+7)."""
    if not iso_str:
        return None
    # Handle formats: 2025-05-28T19:00:00Z  or  2025-05-28T19:00:00+00:00
    iso_str = iso_str.strip().replace("Z", "+00:00")
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        dt_wib = dt_utc.astimezone(WIB)
        return dt_wib.strftime("%H:%M")
    except Exception:
        return None

def iso_to_wib_date(iso_str):
    """Convert ISO 8601 UTC string to DD/MM/YYYY in WIB (UTC+7)."""
    if not iso_str:
        return None
    iso_str = iso_str.strip().replace("Z", "+00:00")
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        dt_wib = dt_utc.astimezone(WIB)
        return dt_wib.strftime("%d/%m/%Y")
    except Exception:
        return None

# ==============================
# ✅ TIME VALIDATOR
# ==============================
def is_valid_match_time(text):
    """True only for HH:MM within valid clock range."""
    text = str(text).strip()
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return False
    hh, mm = int(text[:2]), int(text[3:])
    return 0 <= hh <= 23 and 0 <= mm <= 59

# ==============================
# ✅ TIME EXTRACTION — all strategies in order
# ==============================
def extract_time_from_card(match_element):
    """
    Extract kickoff time from a match card anchor element.

    Priority order:
    1. <time datetime="ISO_STRING"> — parse UTC directly, convert to WIB
       This bypasses JS rendering entirely. Most reliable.
    2. data-testid="match-kickoff-time" or similar test IDs
    3. Visible text matching HH:MM with valid clock range
    """

    # ── Strategy 1: <time datetime="..."> ISO attribute ──────────────────
    # This is the most reliable because it's the raw UTC value from the API,
    # not affected by timezone rendering at all.
    try:
        time_tags = match_element.find_elements(By.XPATH, ".//time[@datetime]")
        for el in time_tags:
            dt_attr = el.get_attribute("datetime") or ""
            if "T" in dt_attr:  # Looks like ISO datetime, not just a date
                result = iso_to_wib_time(dt_attr)
                if result:
                    logging.info(f"TIME via <time datetime> ISO: {dt_attr} → {result} WIB")
                    return result
    except StaleElementReferenceException:
        pass
    except Exception as e:
        logging.debug(f"Strategy 1 error: {e}")

    # ── Strategy 2: data-testid attributes OneFootball uses ──────────────
    # OneFootball's React components use data-testid for QA hooks.
    # These are stable across deploys even when CSS classes change.
    try:
        for testid in ["match-kickoff-time", "kickoff-time", "match-time", "fixture-time"]:
            els = match_element.find_elements(
                By.XPATH, f".//*[@data-testid='{testid}']"
            )
            for el in els:
                t = el.text.strip()
                if is_valid_match_time(t):
                    logging.info(f"TIME via data-testid={testid}: {t}")
                    return t
    except Exception as e:
        logging.debug(f"Strategy 2 error: {e}")

    # ── Strategy 3: aria-label on the match card itself ───────────────────
    # OneFootball often puts a summary aria-label on the <a> tag like:
    # "Real Madrid vs Barcelona, 21:00, 28 May 2025"
    try:
        label = match_element.get_attribute("aria-label") or ""
        t = re.search(r"\b(\d{2}:\d{2})\b", label)
        if t and is_valid_match_time(t.group(1)):
            logging.info(f"TIME via card aria-label: {t.group(1)}")
            return t.group(1)
    except Exception as e:
        logging.debug(f"Strategy 3 error: {e}")

    # ── Strategy 4: leaf text nodes, strict HH:MM only ───────────────────
    # Last resort. We ONLY accept \d{2}:\d{2} pattern — this rejects
    # scores (1:0 is only 3 chars), stats, and any non-clock text.
    try:
        leaf_nodes = match_element.find_elements(
            By.XPATH,
            ".//*[not(*) and string-length(normalize-space(text()))=5 and contains(text(),':')]"
        )
        for el in leaf_nodes:
            t = el.text.strip()
            if is_valid_match_time(t):
                logging.info(f"TIME via leaf text: {t}")
                return t
    except Exception as e:
        logging.debug(f"Strategy 4 error: {e}")

    logging.warning("TIME not found in card")
    return "Unknown"

# ==============================
# ✅ DATE EXTRACTION from card
# ==============================
def extract_date_from_card(match_element, text_lines):
    """
    Extract match date. Priority:
    1. <time datetime="ISO"> — convert UTC to WIB date
    2. Text line matching DD/MM/YYYY
    3. Text line matching relative words (today/tomorrow)
    """
    # Strategy 1: ISO datetime attribute
    try:
        time_tags = match_element.find_elements(By.XPATH, ".//time[@datetime]")
        for el in time_tags:
            dt_attr = el.get_attribute("datetime") or ""
            if "T" in dt_attr:
                result = iso_to_wib_date(dt_attr)
                if result:
                    return result
    except Exception:
        pass

    # Strategy 2: text line DD/MM/YYYY
    for l in text_lines:
        if re.match(r"^\d{2}/\d{2}/\d{4}$", l):
            return l

    # Strategy 3: relative date words
    today = date.today()
    for l in text_lines:
        low = l.lower()
        if low == "today":
            return today.strftime("%d/%m/%Y")
        elif low == "tomorrow":
            return (today + timedelta(days=1)).strftime("%d/%m/%Y")

    return "Unknown"

# ==============================
# ✅ PARSER
# ==============================
def parse_match_card(match):
    try:
        # Re-fetch text to avoid stale data
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) < 2:
            return None

        home = lines[0]
        away = lines[1]

        # Skip non-match cards
        skip_keywords = ["advertisement", "sign in", "follow", "subscribe", "download"]
        if any(kw in home.lower() for kw in skip_keywords):
            return None

        # Skip if team names look like UI labels
        if len(home) < 2 or len(away) < 2:
            return None

        date_val = extract_date_from_card(match, lines)
        time_val = extract_time_from_card(match)

        return {
            "Home Team": home,
            "Away Team": away,
            "Date": date_val,
            "Time": time_val
        }

    except StaleElementReferenceException:
        logging.warning("Stale element skipped")
        return None
    except Exception as e:
        logging.error(f"Parse error: {e}")
        return None

# ==============================
# ✅ SCRAPE — with proper lazy-load wait
# ROOT CAUSE FIX #4: OneFootball uses a virtualized list.
# Only cards near the viewport are rendered. We must scroll slowly
# and wait for new cards to appear before scraping.
# ==============================
def scrape_competition(driver, name, url):
    logging.info(f"Scraping {name} → {url}")
    driver.get(url)

    # Wait for first batch of match cards
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/match/']"))
        )
    except TimeoutException:
        logging.error(f"Timeout waiting for match cards on {name}")
        return []

    # Give React time to fully hydrate
    time.sleep(3)

    # Scroll slowly to trigger virtualized list rendering
    # Fast scrolling jumps past unrendered cards — they never mount
    scroll_pause = 2.0
    last_count = 0
    stale_rounds = 0

    for scroll_round in range(20):  # max 20 scroll attempts
        driver.execute_script("window.scrollBy(0, 800)")
        time.sleep(scroll_pause)

        current_count = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']"))
        logging.info(f"Scroll {scroll_round+1}: {current_count} cards found")

        if current_count == last_count:
            stale_rounds += 1
            if stale_rounds >= 3:
                # Count hasn't changed in 3 scrolls — we've hit the bottom
                break
        else:
            stale_rounds = 0
            last_count = current_count

    # Scroll back to top and re-collect all elements
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(1)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")
    logging.info(f"Total match elements collected for {name}: {len(matches)}")

    results = []
    seen_keys = set()

    for m in matches:
        try:
            parsed = parse_match_card(m)
        except Exception as e:
            logging.warning(f"Card parse failed: {e}")
            continue

        if not parsed:
            continue

        # Deduplicate by (home, away, date)
        dedup_key = (parsed["Home Team"], parsed["Away Team"], parsed["Date"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        parsed["Competition"] = name
        results.append(parsed)

    logging.info(f"{name}: {len(results)} unique fixtures parsed")
    return results

# ==============================
# ✅ DATE NORMALIZATION (fallback for text-based dates)
# ==============================
def normalize_date(d):
    today = date.today()
    d = str(d).lower().strip()
    if d == "today":
        return today.strftime("%d/%m/%Y")
    elif d == "tomorrow":
        return (today + timedelta(days=1)).strftime("%d/%m/%Y")
    elif d == "yesterday":
        return (today - timedelta(days=1)).strftime("%d/%m/%Y")
    return d

# ==============================
# ✅ GET DATA
# ==============================
def get_data():
    driver = get_driver()

    competitions = {
        "UCL":  "https://onefootball.com/en/competition/uefa-champions-league-5/fixtures",
        "UECL": "https://onefootball.com/en/competition/uefa-conference-league-2762/fixtures",
    }

    all_data = []
    for name, url in competitions.items():
        try:
            all_data.extend(scrape_competition(driver, name, url))
        except Exception as e:
            logging.error(f"{name} scrape error: {e}")

    driver.quit()

    if not all_data:
        logging.warning("No data collected from any competition.")
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    df["VS"] = "VS"
    df["Date"] = df["Date"].apply(normalize_date)

    before = len(df)
    df = df[df["Time"].apply(is_valid_match_time)]
    after = len(df)
    logging.info(f"Time filter: kept {after} of {before} rows")

    return df

# ==============================
# ✅ MERGE LOGOS
# ==============================
def merge_logos(df):
    logo_url = (
        "https://docs.google.com/spreadsheets/d/"
        "1BLZ-YDZJqwk1LcSQ79bDOGcdue1OwdG4jrrXjSh6vKs"
        "/gviz/tq?tqx=out:csv"
    )
    try:
        logos = pd.read_csv(logo_url)
        team_col = [c for c in logos.columns if "team" in c.lower()][0]
        logo_col = [c for c in logos.columns if "logo" in c.lower()][0]
        logos = logos[[team_col, logo_col]].rename(
            columns={team_col: "Team", logo_col: "Logo"}
        )

        df = df.merge(logos, left_on="Home Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Home Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

        df = df.merge(logos, left_on="Away Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Away Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

        logging.info("Logos merged.")
    except Exception as e:
        logging.warning(f"Logo merge failed: {e}")
        df["Home Team Logo"] = ""
        df["Away Team Logo"] = ""

    return df

# ==============================
# ✅ GOOGLE SHEETS
# ==============================
def upload_to_sheets(df):
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_url(
        "https://docs.google.com/spreadsheets/d/1BhPU_hskjdgmuSHBcmoPhGxtUscpRLCRed_DITIIOq4/"
    )
    ws = sheet.sheet1
    ws.clear()
    df = df.fillna("").astype(str)
    ws.update([df.columns.tolist()] + df.values.tolist())
    logging.info(f"Uploaded {len(df)} rows to Google Sheets.")

# ==============================
# ✅ SAFE RUN
# ==============================
def safe_run(max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            df = get_data()
            if not df.empty:
                df = merge_logos(df)
                upload_to_sheets(df)
                logging.info(f"✅ SUCCESS on attempt {attempt}")
                return
            else:
                logging.warning(f"Attempt {attempt}: empty dataframe.")
        except Exception as e:
            logging.error(f"Attempt {attempt} failed: {e}")

        if attempt < max_retries:
            logging.info(f"Retrying in 15s...")
            time.sleep(15)

    logging.error("❌ ALL RETRIES FAILED")

# ==============================
# ✅ MAIN
# ==============================
if __name__ == "__main__":
    safe_run()
