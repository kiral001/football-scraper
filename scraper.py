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
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

# ==============================
# ✅ ✅ FIXED PARSER (KEY CHANGE)
# ==============================
def parse_match_card(match):
    try:
        text = match.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # DEBUG (very useful)
        logging.info(f"RAW LINES: {lines}")

        if len(lines) < 2:
            return None

        home = lines[0]
        away = lines[1]

        date_val = ""
        time_val = ""

        # ✅ FIX #2 → tie time to date position
        for i, l in enumerate(lines):
            # detect date (30/05/2026 format)
            if re.match(r"^\d{2}/\d{2}/\d{4}$", l):
                date_val = l

                # next line is usually time
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    if re.match(r"^\d{2}:\d{2}$", next_line):
                        time_val = next_line

                break

        # ✅ fallback (in case above fails)
        if not time_val:
            for l in lines:
                if re.match(r"^\d{2}:\d{2}$", l):
                    time_val = l
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
# ✅ SCRAPING
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
        logging.error(driver.page_source[:1000])
        return []

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(1)

    matches = driver.find_elements(By.CSS_SELECTOR, "a[href*='/match/']")
    logging.info(f"{name}: {len(matches)} raw elements")

    results = []
    for m in matches:
        parsed = parse_match_card(m)
        if parsed:
            parsed["Competition"] = name
            results.append(parsed)

    logging.info(f"{name}: {len(results)} parsed matches")
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
        logging.warning("No data collected!")
        return pd.DataFrame()

    df = pd.DataFrame(all_data)

    df["VS"] = "VS"
    df["Date"] = df["Date"].apply(normalize_date)

    df = df[df["Time"].str.match(r"^\d{2}:\d{2}$", na=False)]

    logging.info(f"Final rows: {len(df)}")
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

    if not os.path.exists(CREDENTIALS_PATH):
        logging.error("credentials.json missing")
        return

    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scope)
    client = gspread.authorize(creds)

    sheet = client.open_by_url(
        "https://docs.google.com/spreadsheets/d/1BhPU_hskjdgmuSHBcmoPhGxtUscpRLCRed_DITIIOq4/"
    )

    ws = sheet.sheet1
    ws.clear()

    df = df.fillna("").astype(str)
    ws.update([df.columns.tolist()] + df.values.tolist())

    logging.info(f"Uploaded {len(df)} rows")

# ==============================
# ✅ SAFE RUN
# ==============================
def safe_run(max_retries=3):
    for i in range(max_retries):
        try:
            logging.info(f"Attempt {i+1}")
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
