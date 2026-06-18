import pandas as pd
import os
import time
import re
import logging
import requests
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
# ✅ PATHS & ENV
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
LOG_PATH = os.path.join(BASE_DIR, "scraper.log")

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = "appgs6fF2J7EaPV34"
AIRTABLE_TABLE_NAME = "schedule"

# ==============================
# ✅ TIMEZONE
# ==============================
WIB = timezone(timedelta(hours=7))

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
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

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
# ✅ TIME PARSER
# ==============================
def iso_to_wib_time(iso_str):
    if not iso_str:
        return None
    iso_str = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(WIB).strftime("%H:%M")
    except:
        return None

def iso_to_wib_date(iso_str):
    if not iso_str:
        return None
    iso_str = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(WIB).strftime("%d/%m/%Y")
    except:
        return None

def is_valid_match_time(text):
    if not re.fullmatch(r"\d{2}:\d{2}", str(text)):
        return False
    hh, mm = int(text[:2]), int(text[3:])
    return 0 <= hh <= 23 and 0 <= mm <= 59

# ==============================
# ✅ EXTRACTORS
# ==============================
def extract_time_from_card(match):
    try:
        time_tags = match.find_elements(By.XPATH, ".//time[@datetime]")
        for el in time_tags:
            t = iso_to_wib_time(el.get_attribute("datetime"))
            if t:
                return t
    except:
        pass
    return "Unknown"

def extract_date_from_card(match):
    try:
        time_tags = match.find_elements(By.XPATH, ".//time[@datetime]")
        for el in time_tags:
            d = iso_to_wib_date(el.get_attribute("datetime"))
            if d:
                return d
    except:
        pass
    return "Unknown"

# ==============================
# ✅ PARSER
# ==============================
def parse_match_card(match):
    try:
        lines = [l.strip() for l in match.text.split("\n") if l.strip()]
        if len(lines) < 2:
            return None

        return {
            "Home Team": lines[0],
            "Away Team": lines[1],
            "Date": extract_date_from_card(match),
            "Time": extract_time_from_card(match)
        }
    except:
        return None

# ==============================
# ✅ SCRAPER
# ==============================
def scrape_competition(driver, name, url):
    driver.get(url)

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/match/']"))
        )
    except TimeoutException:
        return []

    time.sleep(3)

    for _ in range(15):
        driver.execute_script("window.scrollBy(0, 800)")
        time.sleep(2)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")

    results = []
    for m in matches:
        parsed = parse_match_card(m)
        if parsed:
            parsed["Competition"] = name
            results.append(parsed)

    return results

# ==============================
# ✅ GET DATA
# ==============================
def get_data():
    driver = get_driver()

    competitions = {
        "UCL": "https://onefootball.com/en/competition/uefa-champions-league-5/fixtures",
        "UECL": "https://onefootball.com/en/competition/uefa-conference-league-2762/fixtures",
    }

    all_data = []
    for name, url in competitions.items():
        all_data.extend(scrape_competition(driver, name, url))

    driver.quit()

    df = pd.DataFrame(all_data)

    df = df[df["Time"].apply(is_valid_match_time)]

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

# ==============================
# ✅ AIRTABLE
# ==============================
def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }

def clear_airtable():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"

    while True:
        res = requests.get(url, headers=airtable_headers()).json()
        records = res.get("records", [])

        if not records:
            break

        ids = [r["id"] for r in records]

        requests.delete(
            url,
            headers=airtable_headers(),
            params={"records[]": ids}
        )

def upload_to_airtable(df):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"

    records = []
    for _, row in df.iterrows():
        records.append({
            "fields": {
                "Home Team": row["Home Team"],
                "Away Team": row["Away Team"],
                "Date": row["Date"],
                "Time": row["Time"],
                "Competition": row["Competition"]
            }
        })

    for i in range(0, len(records), 10):
        batch = records[i:i+10]
        requests.post(url, json={"records": batch}, headers=airtable_headers())

# ==============================
# ✅ MAIN
# ==============================
def run():
    df = get_data()

    if df.empty:
        logging.warning("No data")
        return

    upload_to_sheets(df)

    clear_airtable()
    upload_to_airtable(df)

    logging.info("SUCCESS")

if __name__ == "__main__":
    run()
