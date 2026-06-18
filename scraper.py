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
# ✅ AIRTABLE CONFIG
# ==============================
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
BASE_ID = "appgs6fF2J7EaPV34"
TABLE_NAME = "Sheet1"

# ==============================
# ✅ PATHS
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
LOG_PATH = os.path.join(BASE_DIR, "scraper.log")

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
# ✅ ISO PARSER
# ==============================
def iso_to_wib_time(iso_str):
    if not iso_str:
        return None
    iso_str = iso_str.strip().replace("Z", "+00:00")
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        return dt_utc.astimezone(WIB).strftime("%H:%M")
    except:
        return None

def iso_to_wib_date(iso_str):
    if not iso_str:
        return None
    iso_str = iso_str.strip().replace("Z", "+00:00")
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        return dt_utc.astimezone(WIB).strftime("%m/%d/%Y")
    except:
        return None

# ==============================
# ✅ VALIDATOR
# ==============================
def is_valid_match_time(text):
    text = str(text).strip()
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return False
    hh, mm = int(text[:2]), int(text[3:])
    return 0 <= hh <= 23 and 0 <= mm <= 59

# ==============================
# ✅ PARSER (with filter)
# ==============================
def parse_match_card(match):
    try:
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) < 2:
            return None

        home = lines[0]
        away = lines[1]

        skip_keywords = [
            "advertisement", "sign in", "follow", "subscribe", "download",
            "winner", "loser", "group"
        ]

        if any(kw in home.lower() or kw in away.lower() for kw in skip_keywords):
            return None

        return {
            "Home Team": home,
            "Away Team": away,
            "Date": extract_date_from_card(match, lines),
            "Time": extract_time_from_card(match)
        }

    except:
        return None

# ==============================
# ✅ SCRAPER
# ==============================
def scrape_competition(driver, name, url):
    driver.get(url)

    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/match/']"))
    )

    time.sleep(3)

    for _ in range(20):
        driver.execute_script("window.scrollBy(0, 800)")
        time.sleep(2)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")

    results = []
    seen = set()

    for m in matches:
        parsed = parse_match_card(m)
        if not parsed:
            continue

        key = (parsed["Home Team"], parsed["Away Team"], parsed["Date"])
        if key in seen:
            continue

        seen.add(key)
        parsed["Competition"] = name
        results.append(parsed)

    return results

# ==============================
# ✅ GET DATA
# ==============================
def get_data():
    driver = get_driver()

    data = scrape_competition(
        driver,
        "World Cup",
        "https://onefootball.com/en/competition/fifa-world-cup-12/fixtures"
    )

    driver.quit()

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df = df[df["Time"].apply(is_valid_match_time)]

    return df

# ==============================
# ✅ AIRTABLE UPDATE FUNCTION
# ==============================
def update_airtable(df):
    url = f"https://api.airtable.com/v0/{appgs6fF2J7EaPV34appgs6fF2J7EaPV34}/{Sheet1}"

    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }

    for _, row in df.iterrows():
        data = {
            "fields": {
                "Home Team": row["Home Team"],
                "Away Team": row["Away Team"],
                "Date": row["Date"],
                "Time": row["Time"]
            }
        }

        response = requests.post(url, json=data, headers=headers)

        if response.status_code != 200:
            logging.warning(f"Airtable error: {response.text}")

# ==============================
# ✅ MAIN
# ==============================
if __name__ == "__main__":
    df = get_data()

    print(df)

    if not df.empty:
        update_airtable(df)
