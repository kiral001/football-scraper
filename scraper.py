import pandas as pd
import os
import time
import re
import random
import string
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
# ✅ PATHS
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
LOG_PATH = os.path.join(BASE_DIR, "scraper.log")
 
# ==============================
# ✅ TIMEZONE
# ==============================
WIB = timezone(timedelta(hours=7))  # Asia/Jakarta
 
# ==============================
# ✅ AIRTABLE CONFIG (from GitHub Secrets / environment variables)
# ==============================
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")
 
# ==============================
# ✅ LOGGING
# ==============================
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
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
    # ── Strategy 1: <time datetime="..."> ISO attribute ──────────────────
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
 
    # ── Strategy 2: data-testid attributes OneFootball uses ──────────────
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
    try:
        label = match_element.get_attribute("aria-label") or ""
        t = re.search(r"\b(\d{2}:\d{2})\b", label)
        if t and is_valid_match_time(t.group(1)):
            logging.info(f"TIME via card aria-label: {t.group(1)}")
            return t.group(1)
    except Exception as e:
        logging.debug(f"Strategy 3 error: {e}")
 
    # ── Strategy 4: leaf text nodes, strict HH:MM only ───────────────────
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
# ✅ PARSER (✅ FILTER ADDED HERE ONLY)
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
 
        # ✅ FILTER BOTH HOME & AWAY
        if any(kw in home.lower() or kw in away.lower() for kw in skip_keywords):
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
# ✅ SHORT UNIQUE ID GENERATOR
# ==============================
def generate_short_id(length=7, existing_ids=None):
    """Generate a random alphanumeric ID (default 7 chars), avoiding collisions
    with any IDs already generated in the current batch."""
    if existing_ids is None:
        existing_ids = set()
    chars = string.ascii_uppercase + string.digits
    while True:
        new_id = "".join(random.choices(chars, k=length))
        if new_id not in existing_ids:
            return new_id
 
# ==============================
# ✅ GET DATA
# ==============================
def get_data():
    driver = get_driver()
    competitions = {
        "World Cup": "https://onefootball.com/en/competition/fifa-world-cup-12/fixtures"
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
 
    # ── ✅ combine Date + Time into MatchTime datetime column ────────
    df["MatchTime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format="%m/%d/%Y %H:%M",
        errors="coerce"
    ).dt.strftime("%m/%d/%Y %H:%M")
    logging.info("MatchTime column added.")
    # ─────────────────────────────────────────────────────────────────
 
    # ── ✅ generate short unique ID per fixture row ───────────────────
    existing_ids = set()
    ids = []
    for _ in range(len(df)):
        new_id = generate_short_id(length=7, existing_ids=existing_ids)
        existing_ids.add(new_id)
        ids.append(new_id)
    df.insert(0, "ID", ids)
    logging.info("ID column added.")
    # ─────────────────────────────────────────────────────────────────
 
    return df
 

# ==============================
# ✅ CLASSIFY CLUB SIZE
# ==============================
BIG_CLUBS = {
    "argentina",
    "brazil",
    "croatia",
    "england",
    "france",
    "germany",
    "japan",
    "netherlands",
    "portugal",
    "senegal",
    "spain",
    "uruguay"
}

def classify_club(team_name: str) -> str:
    """Return 'Big Team' if team is in the big clubs list, else 'Small Club'."""
    return "Big Team" if str(team_name).strip().lower() in BIG_CLUBS else "Small Club"

def add_club_classification(df: pd.DataFrame) -> pd.DataFrame:
    """Add Home Club Type and Away Club Type columns to the dataframe."""
    df["Home Club Type"] = df["Home Team"].apply(classify_club)
    df["Away Club Type"] = df["Away Team"].apply(classify_club)
    logging.info("Club classification columns added.")
    return df

# ==============================
# ✅ CLASSIFY MATCH TYPE
# ==============================
def add_match_type(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add Match Type column.
    'Big Match'     — both Home Club Type AND Away Club Type are 'Big Club'
    'Non Big Match' — any other combination
    """
    df["Match Type"] = df.apply(
        lambda row: "Big Match"
        if row["Home Club Type"] == "Big Club" and row["Away Club Type"] == "Big Club"
        else "Non Big Match",
        axis=1
    )
    logging.info("Match type column added.")
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
# ✅ AIRTABLE CLEAR ALL RECORDS
# ==============================
def clear_airtable_table():
    """
    Delete every existing record in the Airtable table before refilling
    it with freshly scraped data. Airtable allows deleting up to 10
    record IDs per request, so this fetches all records (paginated)
    and deletes them in batches of 10.
    """
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    }

    all_record_ids = []
    offset = None

    # ── Fetch all record IDs (paginated, 100 per page) ──────────────────
    while True:
        params = {"pageSize": 100, "fields[]": "Home Team"}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            logging.error(f"Airtable fetch-for-clear request error: {e}")
            break

        if resp.status_code != 200:
            logging.error(f"Airtable fetch-for-clear failed ({resp.status_code}): {resp.text}")
            break

        body = resp.json()
        all_record_ids.extend([rec["id"] for rec in body.get("records", [])])
        offset = body.get("offset")
        if not offset:
            break

    if not all_record_ids:
        logging.info("Airtable table already empty — nothing to clear.")
        return

    # ── Delete in batches of 10 (Airtable's max per delete request) ────
    deleted_count = 0
    for i in range(0, len(all_record_ids), 10):
        batch_ids = all_record_ids[i:i + 10]
        del_params = [("records[]", rid) for rid in batch_ids]
        try:
            resp = requests.delete(url, headers=headers, params=del_params, timeout=30)
            if resp.status_code == 200:
                deleted_count += len(batch_ids)
            else:
                logging.error(f"Airtable delete batch failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            logging.error(f"Airtable delete batch request error: {e}")
        time.sleep(0.25)

    logging.info(f"Airtable cleared: {deleted_count}/{len(all_record_ids)} records deleted.")

# ==============================
# ✅ AIRTABLE UPLOAD (CLEAR + REFILL)
# ==============================
def upload_to_airtable(df, batch_size=10):
    """
    Wipe the Airtable table clean, then re-upload all freshly scraped
    rows as brand-new records. This guarantees the table always exactly
    mirrors the latest scrape.

    Credentials are pulled from environment variables, which are
    populated via GitHub Actions secrets:
        AIRTABLE_API_KEY
        AIRTABLE_BASE_ID
        AIRTABLE_TABLE_NAME
    """
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_NAME):
        logging.warning("Airtable credentials missing — skipping Airtable upload.")
        return

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }

    # ── Step 1: erase everything currently in the table ─────────────────
    clear_airtable_table()

    # ── Step 2: refill with freshly scraped rows ─────────────────────────
    df_clean = df.fillna("").astype(str)
    records = df_clean.to_dict(orient="records")

    total_created = 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        payload = {
            "records": [{"fields": rec} for rec in batch],
            "typecast": True
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                total_created += len(batch)
                logging.info(f"Airtable batch {i // batch_size + 1}: created {len(batch)} records.")
            else:
                logging.error(
                    f"Airtable batch {i // batch_size + 1} failed "
                    f"({resp.status_code}): {resp.text}"
                )
        except Exception as e:
            logging.error(f"Airtable batch {i // batch_size + 1} request error: {e}")
        time.sleep(0.25)  # respect Airtable's 5 req/sec rate limit

    logging.info(f"Airtable refill complete: {total_created}/{len(records)} rows created.")

# ==============================
# ✅ SAFE RUN
# ==============================
def safe_run(max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            df = get_data()
            if not df.empty:
                df = merge_logos(df)
                df = add_club_classification(df)
                df = add_match_type(df)
                upload_to_sheets(df)
                upload_to_airtable(df)
                logging.info(f"✅ SUCCESS on attempt {attempt}")
                return
            else:
                logging.warning(f"Attempt {attempt}: empty dataframe.")
        except Exception as e:
            logging.error(f"Attempt {attempt} failed: {e}")
        if attempt < max_retries:
            logging.info("Retrying in 15s...")
            time.sleep(15)
    logging.error("❌ ALL RETRIES FAILED")

# ==============================
# ✅ MAIN
# ==============================
if __name__ == "__main__":
    safe_run()

# ==============================
# AUTH
# ==============================
creds = Credentials.from_service_account_file(
    CREDENTIALS_PATH,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
)
client = gspread.authorize(creds)
 
# ==============================
# OPEN FILE
# ==============================
sheet_url = "https://docs.google.com/spreadsheets/d/1e_cR5X9HCfszFVEbyq0VriX-KFjI0Wr6a9VIdHDV8Mo"
spreadsheet = client.open_by_url(sheet_url)
 
# SOURCE (Form responses tab) - referenced by title, safer than by_id
source_ws = spreadsheet.worksheet("Form Responses 1")
 
# TARGET (the "info" tab — NOT sheet1!)
target_ws = spreadsheet.worksheet("info")
 
# TARGET (the "info_wa" tab)
target_ws_wa = spreadsheet.worksheet("info_wa")
 
# ==============================
# LOAD DATA
# ==============================
data = source_ws.get_all_records()
df = pd.DataFrame(data)
 
# ==============================
# PROCESS — "info" sheet (Timestamp, Nama, Email, Country)
# ==============================
col = "Which country are you supporting?"
 
df_info = df[["Timestamp", "Nama", "Email", col]].copy()
 
# split by comma OR semicolon
df_info[col] = df_info[col].astype(str).str.split(r",|;")
 
# make rows
df_info = df_info.explode(col)
 
# clean spaces
df_info[col] = df_info[col].str.strip()
 
# remove empty
df_info = df_info[df_info[col] != ""]
 
# remove rows where Timestamp, Nama, or Email is blank
df_info["Timestamp"] = df_info["Timestamp"].astype(str).str.strip()
df_info["Nama"] = df_info["Nama"].astype(str).str.strip()
df_info["Email"] = df_info["Email"].astype(str).str.strip()
df_info = df_info[
    (df_info["Timestamp"] != "") &
    (df_info["Nama"] != "") &
    (df_info["Email"] != "")
]
 
# ==============================
# PROCESS — "info_wa" sheet (Timestamp, Nama, Phone Number, Country)
# ==============================
df_wa = df[["Timestamp", "Nama", "Phone Number", col]].copy()
 
# split by comma OR semicolon
df_wa[col] = df_wa[col].astype(str).str.split(r",|;")
 
# make rows
df_wa = df_wa.explode(col)
 
# clean spaces
df_wa[col] = df_wa[col].str.strip()
 
# remove empty
df_wa = df_wa[df_wa[col] != ""]
 
# remove rows where Timestamp, Nama, or Phone Number is blank
df_wa["Timestamp"] = df_wa["Timestamp"].astype(str).str.strip()
df_wa["Nama"] = df_wa["Nama"].astype(str).str.strip()
df_wa["Phone Number"] = df_wa["Phone Number"].astype(str).str.strip()
df_wa = df_wa[
    (df_wa["Timestamp"] != "") &
    (df_wa["Nama"] != "") &
    (df_wa["Phone Number"] != "")
]
 
# ==============================
# CLEAR TARGET ("info" only)
# ==============================
target_ws.clear()
 
# ==============================
# WRITE RESULT — "info"
# ==============================
target_ws.update(
    [df_info.columns.values.tolist()] + df_info.values.tolist()
)
 
print("DONE ✅ — 'info' sheet updated, 'Form Responses 1' untouched")
 
# ==============================
# CLEAR TARGET ("info_wa" only)
# ==============================
target_ws_wa.clear()
 
# ==============================
# WRITE RESULT — "info_wa"
# ==============================
target_ws_wa.update(
    [df_wa.columns.values.tolist()] + df_wa.values.tolist()
)
 
print("DONE ✅ — 'info_wa' sheet updated, 'Form Responses 1' untouched")
 
# ==============================
# AIRTABLE CONFIG (for "info" sheet)
# ==============================
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = "info"  # separate table name for "info"
 
# ==============================
# AIRTABLE CONFIG (for "info_wa" sheet)
# ==============================
AIRTABLE_TABLE_NAME_WA = "info_wa"  # separate table name for "info_wa"
 
# ==============================
# AIRTABLE CLEAR ALL RECORDS
# ==============================
def clear_airtable_table():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
 
    all_record_ids = []
    offset = None
 
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            print(f"Airtable fetch-for-clear request error: {e}")
            break
 
        if resp.status_code != 200:
            print(f"Airtable fetch-for-clear failed ({resp.status_code}): {resp.text}")
            break
 
        body = resp.json()
        all_record_ids.extend([rec["id"] for rec in body.get("records", [])])
        offset = body.get("offset")
        if not offset:
            break
 
    if not all_record_ids:
        print("Airtable 'info' table already empty — nothing to clear.")
        return
 
    deleted_count = 0
    for i in range(0, len(all_record_ids), 10):
        batch_ids = all_record_ids[i:i + 10]
        del_params = [("records[]", rid) for rid in batch_ids]
        try:
            resp = requests.delete(url, headers=headers, params=del_params, timeout=30)
            if resp.status_code == 200:
                deleted_count += len(batch_ids)
            else:
                print(f"Airtable delete batch failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Airtable delete batch request error: {e}")
        time.sleep(0.25)
 
    print(f"Airtable 'info' table cleared: {deleted_count}/{len(all_record_ids)} records deleted.")
 
# ==============================
# AIRTABLE CLEAR ALL RECORDS — "info_wa"
# ==============================
def clear_airtable_table_wa():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME_WA}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
 
    all_record_ids = []
    offset = None
 
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as e:
            print(f"Airtable fetch-for-clear request error (info_wa): {e}")
            break
 
        if resp.status_code != 200:
            print(f"Airtable fetch-for-clear failed (info_wa, {resp.status_code}): {resp.text}")
            break
 
        body = resp.json()
        all_record_ids.extend([rec["id"] for rec in body.get("records", [])])
        offset = body.get("offset")
        if not offset:
            break
 
    if not all_record_ids:
        print("Airtable 'info_wa' table already empty — nothing to clear.")
        return
 
    deleted_count = 0
    for i in range(0, len(all_record_ids), 10):
        batch_ids = all_record_ids[i:i + 10]
        del_params = [("records[]", rid) for rid in batch_ids]
        try:
            resp = requests.delete(url, headers=headers, params=del_params, timeout=30)
            if resp.status_code == 200:
                deleted_count += len(batch_ids)
            else:
                print(f"Airtable delete batch failed (info_wa, {resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Airtable delete batch request error (info_wa): {e}")
        time.sleep(0.25)
 
    print(f"Airtable 'info_wa' table cleared: {deleted_count}/{len(all_record_ids)} records deleted.")
 
# ==============================
# AIRTABLE UPLOAD (CLEAR + REFILL) — "info"
# ==============================
def upload_to_airtable(df, batch_size=10):
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_NAME):
        print("Airtable credentials missing — skipping Airtable upload.")
        return
 
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }
 
    clear_airtable_table()
 
    df_clean = df.fillna("").astype(str)
    records = df_clean.to_dict(orient="records")
 
    total_created = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        payload = {
            "records": [{"fields": rec} for rec in batch],
            "typecast": True
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                total_created += len(batch)
                print(f"Airtable batch {i // batch_size + 1}: created {len(batch)} records.")
            else:
                print(f"Airtable batch {i // batch_size + 1} failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Airtable batch {i // batch_size + 1} request error: {e}")
        time.sleep(0.25)
 
    print(f"Airtable 'info' refill complete: {total_created}/{len(records)} rows created.")
 
# ==============================
# AIRTABLE UPLOAD (CLEAR + REFILL) — "info_wa"
# ==============================
def upload_to_airtable_wa(df, batch_size=10):
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_NAME_WA):
        print("Airtable credentials missing — skipping Airtable upload (info_wa).")
        return
 
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME_WA}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }
 
    clear_airtable_table_wa()
 
    df_clean = df.fillna("").astype(str)
    records = df_clean.to_dict(orient="records")
 
    total_created = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        payload = {
            "records": [{"fields": rec} for rec in batch],
            "typecast": True
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                total_created += len(batch)
                print(f"Airtable batch {i // batch_size + 1} (info_wa): created {len(batch)} records.")
            else:
                print(f"Airtable batch {i // batch_size + 1} (info_wa) failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"Airtable batch {i // batch_size + 1} (info_wa) request error: {e}")
        time.sleep(0.25)
 
    print(f"Airtable 'info_wa' refill complete: {total_created}/{len(records)} rows created.")
 
# ==============================
# RUN AIRTABLE UPLOAD — "info"
# ==============================
upload_to_airtable(df_info)
print("DONE ✅ — Airtable 'info' table updated")
 
# ==============================
# RUN AIRTABLE UPLOAD — "info_wa"
# ==============================
upload_to_airtable_wa(df_wa)
print("DONE ✅ — Airtable 'info_wa' table updated")
 
