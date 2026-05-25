import pandas as pd
import os
import time
import re
import logging
from datetime import date, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
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
# ✅ DRIVER
# ==============================
def get_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # Prevent lazy-load issues by disabling image loading (speeds up scraping too)
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    return driver

# ==============================
# ✅ TIME EXTRACTION — FIXED
# The root bug: the old code used contains(text(),':') which matches
# scores (1:0, 2:3), stats (45:55), and other colon-containing text.
# Fix: use a strict regex ONLY for HH:MM clock format (00:00–23:59)
# AND explicitly exclude score-like patterns (single digits around colon).
# ==============================
def is_valid_match_time(text):
    """
    Returns True only for proper HH:MM kickoff times.
    Rejects: scores like 1:0, 2:3 | stats like 45:55 | anything else.
    """
    text = text.strip()
    # Must be exactly HH:MM — two digits, colon, two digits
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return False
    hh, mm = int(text[:2]), int(text[3:])
    # Valid clock time only — hour 0-23, minute 0-59
    if hh > 23 or mm > 59:
        return False
    return True

def extract_time_from_card(match_element):
    """
    Extracts kickoff time from a match card element.

    Strategy (in order of reliability):
    1. Look for elements with time-specific aria attributes
    2. Look for <time> HTML tags
    3. Scan ALL leaf text nodes and apply strict HH:MM validation
       with clock-range check (rejects scores, stats, etc.)
    """
    # --- Strategy 1: aria-label with time pattern ---
    try:
        time_els = match_element.find_elements(
            By.XPATH,
            ".//*[@aria-label and contains(@aria-label, ':')]"
        )
        for el in time_els:
            label = el.get_attribute("aria-label") or ""
            t = re.search(r"\b(\d{2}:\d{2})\b", label)
            if t and is_valid_match_time(t.group(1)):
                logging.info(f"TIME via aria-label: {t.group(1)}")
                return t.group(1)
    except Exception:
        pass

    # --- Strategy 2: <time> HTML tags ---
    try:
        time_tags = match_element.find_elements(By.XPATH, ".//time")
        for el in time_tags:
            # Check datetime attribute first (most reliable)
            dt_attr = el.get_attribute("datetime") or ""
            t = re.search(r"\b(\d{2}:\d{2})\b", dt_attr)
            if t and is_valid_match_time(t.group(1)):
                logging.info(f"TIME via <time datetime>: {t.group(1)}")
                return t.group(1)
            # Fallback: visible text of <time> tag
            t = re.search(r"\b(\d{2}:\d{2})\b", el.text)
            if t and is_valid_match_time(t.group(1)):
                logging.info(f"TIME via <time> text: {t.group(1)}")
                return t.group(1)
    except Exception:
        pass

    # --- Strategy 3: All text nodes, strict validation ---
    # This is the fallback. We collect ALL text, then validate strictly.
    # is_valid_match_time() will reject scores (1:0 won't match \d{2}:\d{2})
    # and invalid clock values (hour > 23, minute > 59).
    try:
        all_text_nodes = match_element.find_elements(
            By.XPATH,
            # Leaf text nodes only — avoids picking up container text that
            # concatenates children (which caused the original double-match bug)
            ".//*[not(*) and contains(text(), ':')]"
        )
        for el in all_text_nodes:
            t = el.text.strip()
            logging.info(f"TIME CANDIDATE (Strategy 3): '{t}' | displayed={el.is_displayed()}")
            if is_valid_match_time(t):
                logging.info(f"TIME accepted: {t}")
                return t
    except Exception:
        pass

    return "Unknown"

# ==============================
# ✅ PARSER
# ==============================
def parse_match_card(match):
    try:
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) < 2:
            return None

        # Team names are always the first two non-empty lines
        home = lines[0]
        away = lines[1]

        # Skip cards that look like ads, headers, or non-match content
        if any(kw in home.lower() for kw in ["advertisement", "sign in", "follow"]):
            return None

        # Extract date
        date_val = "Unknown"
        for l in lines:
            if re.match(r"^\d{2}/\d{2}/\d{4}$", l):
                date_val = l
                break

        # Extract time using the fixed method
        time_val = extract_time_from_card(match)

        return {
            "Home Team": home,
            "Away Team": away,
            "Date": date_val,
            "Time": time_val
        }

    except Exception as e:
        logging.error(f"Parse error: {e}")
        return None

# ==============================
# ✅ SCRAPE
# ==============================
def scrape_competition(driver, name, url):
    logging.info(f"Scraping {name} → {url}")
    driver.get(url)

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/match/']"))
        )
    except TimeoutException:
        logging.error(f"Timeout on {name}")
        return []

    # Scroll to trigger lazy loading of all fixture cards
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(1)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")
    logging.info(f"Found {len(matches)} raw match elements for {name}")

    results = []
    seen_keys = set()  # Deduplicate by (home, away, date) tuple

    for m in matches:
        parsed = parse_match_card(m)
        if not parsed:
            continue

        # Deduplication key — OneFootball often renders duplicate anchors
        dedup_key = (parsed["Home Team"], parsed["Away Team"], parsed["Date"])
        if dedup_key in seen_keys:
            logging.info(f"Duplicate skipped: {dedup_key}")
            continue
        seen_keys.add(dedup_key)

        parsed["Competition"] = name
        results.append(parsed)

    logging.info(f"{name}: {len(results)} unique fixtures collected")
    return results

# ==============================
# ✅ DATE NORMALIZATION
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
        "UCL": "https://onefootball.com/en/competition/uefa-champions-league-5/fixtures",
        "UECL": "https://onefootball.com/en/competition/uefa-conference-league-2762/fixtures"
    }
    all_data = []
    for name, url in competitions.items():
        try:
            all_data.extend(scrape_competition(driver, name, url))
        except Exception as e:
            logging.error(f"{name} error: {e}")
    driver.quit()

    if not all_data:
        logging.warning("No data collected.")
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    df["VS"] = "VS"
    df["Date"] = df["Date"].apply(normalize_date)

    # Only keep rows with valid kickoff times
    before = len(df)
    df = df[df["Time"].apply(is_valid_match_time)]
    after = len(df)
    logging.info(f"Rows kept after time filter: {after}/{before}")

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
        logos = logos[[team_col, logo_col]].rename(columns={team_col: "Team", logo_col: "Logo"})

        df = df.merge(logos, left_on="Home Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Home Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

        df = df.merge(logos, left_on="Away Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Away Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

        logging.info("Logos merged successfully.")
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
        "https://www.googleapis.com/auth/drive"
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
    for i in range(max_retries):
        try:
            df = get_data()
            if not df.empty:
                df = merge_logos(df)
                upload_to_sheets(df)
                logging.info("✅ SUCCESS")
                return
            else:
                logging.warning(f"Attempt {i+1}: empty dataframe, retrying...")
        except Exception as e:
            logging.error(f"Attempt {i+1} failed: {e}")
        time.sleep(15)
    logging.error("❌ ALL RETRIES FAILED")

# ==============================
# ✅ MAIN
# ==============================
if __name__ == "__main__":
    safe_run()
