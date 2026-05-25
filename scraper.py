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

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    return driver

# ==============================
# ✅ ✅ FIXED PARSER (VISIBLE TIME ONLY)
# ==============================
def parse_match_card(match):
    try:
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) < 2:
            return None

        home = lines[0]
        away = lines[1]

        date_val = ""
        time_val = ""

        # ✅ extract date normally
        for l in lines:
            if re.match(r"^\d{2}/\d{2}/\d{4}$", l):
                date_val = l
                break

        # ✅ extract ALL possible time elements
        time_el = match.find_elements(By.XPATH, ".//*[contains(text(),':')]")

        # ✅ ONLY TAKE VISIBLE TIME
        for el in time_el:
            if el.is_displayed():
                t = el.text.strip()

                # debug log (optional but recommended)
                logging.info(f"TIME CANDIDATE: {t} | visible={el.is_displayed()}")

                if re.match(r"^\d{2}:\d{2}$", t):
                    time_val = t
                    break

        return {
            "Home Team": home,
            "Away Team": away,
            "Date": date_val or "Unknown",
            "Time": time_val or "Unknown"
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

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(1)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")

    results = []
    for m in matches:
        parsed = parse_match_card(m)
        if parsed:
            parsed["Competition"] = name
            results.append(parsed)

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
        "Premier League": "https://onefootball.com/en/competition/premier-league-9/fixtures",
        "LaLiga": "https://onefootball.com/en/competition/laliga-10/fixtures"
    }

    all_data = []

    for name, url in competitions.items():
        try:
            all_data.extend(scrape_competition(driver, name, url))
        except Exception as e:
            logging.error(f"{name} error: {e}")

    driver.quit()

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    df["VS"] = "VS"
    df["Date"] = df["Date"].apply(normalize_date)

    df = df[df["Time"].str.match(r"^\d{2}:\d{2}$", na=False)]

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

        logos = logos[[team_col, logo_col]]
        logos.columns = ["Team", "Logo"]

        df = df.merge(logos, left_on="Home Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Home Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

        df = df.merge(logos, left_on="Away Team", right_on="Team", how="left")
        df.rename(columns={"Logo": "Away Team Logo"}, inplace=True)
        df.drop(columns=["Team"], inplace=True)

    except Exception:
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
                logging.info("SUCCESS")
                return

        except Exception as e:
            logging.error(f"Attempt {i+1} failed: {e}")

        time.sleep(15)

    logging.error("ALL RETRIES FAILED")

# ==============================
# ✅ MAIN
# ==============================
if __name__ == "__main__":
    safe_run()
``
