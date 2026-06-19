import pandas as pd
import os
import time
import re
import logging
import requests  # ✅ ADDED
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
    options.add_argument("--lang=en-US")
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    driver.execute_cdp_cmd(
        "Emulation.setTimezoneOverride",
        {"timezoneId": "Asia/Jakarta"}
    )
    return driver

# ==============================
# ✅ ISO DATETIME PARSER
# ==============================
def iso_to_wib_time(iso_str):
    """Convert ISO 8601 UTC string to HH:MM in WIB (UTC+7)."""
    if not iso_str:
        return None
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
        return dt_wib.strftime("%m/%d/%Y")
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
    try:
        time_tags = match_element.find_elements(By.XPATH, ".//time[@datetime]")
        for el in time_tags:
            dt_attr = el.get_attribute("datetime") or ""
            if "T" in dt_attr:
                result = iso_to_wib_time(dt_attr)
                if result:
                    logging.info(f"TIME via <time datetime> ISO: {dt_attr} → {result} WIB")
                    return result
    except StaleElementReferenceException:
        pass
    except Exception as e:
        logging.debug(f"Strategy 1 error: {e}")

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

    try:
        label = match_element.get_attribute("aria-label") or ""
        t = re.search(r"\b(\d{2}:\d{2})\b", label)
        if t and is_valid_match_time(t.group(1)):
            logging.info(f"TIME via card aria-label: {t.group(1)}")
            return t.group(1)
    except Exception as e:
        logging.debug(f"Strategy 3 error: {e}")

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

    for l in text_lines:
        if re.match(r"^\d{2}/\d{2}/\d{4}$", l):
            return l

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
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            return None
        home = lines[0]
        away = lines[1]
        skip_keywords = ["advertisement", "sign in", "follow", "subscribe", "download"]
        if any(kw in home.lower() for kw in skip_keywords):
            return None
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
# ==============================
def scrape_competition(driver, name, url):
    logging.info(f"Scraping {name} → {url}")
    driver.get(url)
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/match/']"))
        )
    except TimeoutException:
        logging.error(f"Timeout waiting for match cards on {name}")
        return []
    time.sleep(3)

    scroll_pause = 2.0
    last_count = 0
    stale_rounds = 0

    for scroll_round in range(20):
        driver.execute_script("window.scrollBy(0, 800)")
        time.sleep(scroll_pause)
        current_count = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']"))
        logging.info(f"Scroll {scroll_round+1}: {current_count} cards found")

        if current_count == last_count:
            stale_rounds += 1
            if stale_rounds >= 3:
                break
        else:
            stale_rounds = 0
            last_count = current_count

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

        dedup_key = (parsed["Home Team"], parsed["Away Team"], parsed["Date"])
        if dedup_key in seen_keys:
            continue

        seen_keys.add(dedup_key)
        parsed["Competition"] = name
        results.append(parsed)

    logging.info(f"{name}: {len(results)} unique fixtures parsed")
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
        "World Cup":  "https://onefootball.com/en/competition/fifa-world-cup-12/fixtures"
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

    df["MatchTime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format="%m/%d/%Y %H:%M",
        errors="coerce"
    ).dt.strftime("%m/%d/%Y %H:%M")

    logging.info("MatchTime column added.")
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
# ✅ AIRTABLE (ADDED ONLY)
# ==============================
def upload_to_airtable(df):
    API_KEY = os.getenv("AIRTABLE_API_KEY")
    BASE_ID = os.getenv("AIRTABLE_BASE_ID")
    TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")

    if not API_KEY or not BASE_ID or not TABLE_NAME:
        logging.error("Airtable credentials missing.")
        return

    url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_NAME}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    df = df.fillna("").astype(str)

    records = [{"fields": row.to_dict()} for _, row in df.iterrows()]

    for i in range(0, len(records), 10):
        batch = records[i:i+10]
        try:
            res = requests.post(url, json={"records": batch}, headers=headers)
            if res.status_code not in [200, 201]:
                logging.error(res.text)
        except Exception as e:
            logging.error(e)

    logging.info(f"Uploaded {len(df)} rows to Airtable.")

# ==============================
# ✅ SAFE RUN
# ==============================
def safe_run(max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            df = get_data()
            if not df.empty:
                upload_to_sheets(df)
                upload_to_airtable(df)  # ✅ ADDED LINE ONLY
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
