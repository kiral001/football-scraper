import pandas as pd
import os
import time
import re
import logging
import requests  # ✅ ADDED ONLY
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
    text = str(text).strip()
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return False
    hh, mm = int(text[:2]), int(text[3:])
    return 0 <= hh <= 23 and 0 <= mm <= 59

# ==============================
# ✅ PARSER & SCRAPER (UNCHANGED)
# ==============================
def parse_match_card(match):
    try:
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            return None

        home = lines[0]
        away = lines[1]

        time_val = None
        for l in lines:
            if is_valid_match_time(l):
                time_val = l

        if not time_val:
            return None

        return {
            "Home Team": home,
            "Away Team": away,
            "Date": date.today().strftime("%m/%d/%Y"),
            "Time": time_val
        }
    except:
        return None

def get_data():
    driver = get_driver()
    driver.get("https://onefootball.com/en/competition/fifa-world-cup-12/fixtures")
    time.sleep(5)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")
    results = []

    for m in matches:
        parsed = parse_match_card(m)
        if parsed:
            parsed["Competition"] = "World Cup"
            results.append(parsed)

    driver.quit()

    df = pd.DataFrame(results)
    df["VS"] = "VS"

    df["MatchTime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format="%m/%d/%Y %H:%M",
        errors="coerce"
    ).dt.strftime("%m/%d/%Y %H:%M")

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

    EXCLUDED_FIELDS = {
        "Notify Time",
        "Trigger Now",
        "Notification Sent",
        "Ready to Notify"
    }

    records = []
    for _, row in df.iterrows():
        record = {
            k: v for k, v in row.to_dict().items()
            if k not in EXCLUDED_FIELDS
        }
        records.append({"fields": record})

    for i in range(0, len(records), 10):
        batch = records[i:i+10]
        try:
            res = requests.post(url, json={"records": batch}, headers=headers)

            # ✅ THIS IS THE ONLY REAL FIX YOU NEEDED
            logging.info(f"Airtable response: {res.status_code} - {res.text}")

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

            logging.info(f"Rows collected: {len(df)}")  # ✅ DEBUG ONLY

            if not df.empty:
                upload_to_sheets(df)
                upload_to_airtable(df)

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
