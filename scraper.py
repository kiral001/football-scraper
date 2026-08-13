import pandas as pd
import os
import time
import re
import random
import string
import logging
import smtplib
import requests
from datetime import date, timedelta, timezone, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
# ✅ SHEET URLS
# ==============================
FIXTURES_SHEET_URL = "https://docs.google.com/spreadsheets/d/1BhPU_hskjdgmuSHBcmoPhGxtUscpRLCRed_DITIIOq4/"
READERS_SHEET_URL = "https://docs.google.com/spreadsheets/d/1e_cR5X9HCfszFVEbyq0VriX-KFjI0Wr6a9VIdHDV8Mo"

# ==============================
# ✅ ALERT CONFIG (from environment / GitHub Secrets)
# ==============================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # Gmail App Password, not your normal password

# Alert window: only fire for matches this many minutes out (1-2 hours)
NOTIFY_MIN_MINUTES = int(os.environ.get("NOTIFY_MIN_MINUTES", "60"))
NOTIFY_MAX_MINUTES = int(os.environ.get("NOTIFY_MAX_MINUTES", "120"))
 
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
        "EPL": "https://onefootball.com/en/competition/premier-league-9/fixtures",
        "UCL": "https://onefootball.com/en/competition/uefa-champions-league-5/fixtures",
        "FA Cup": "https://onefootball.com/en/competition/fa-cup-17/fixtures",
        "LaLiga": "https://onefootball.com/en/competition/laliga-10/fixtures",
        "Serie A": "https://onefootball.com/en/competition/serie-a-13/fixtures",
        "UEL": "https://onefootball.com/en/competition/uefa-europa-league-7/fixtures"
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
# ✅ MERGE LOGOS
# ==============================
def merge_logos(df):
    logo_url = (
        "https://docs.google.com/spreadsheets/d/"
        "1BhPU_hskjdgmuSHBcmoPhGxtUscpRLCRed_DITIIOq4"
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
# ✅ CLASSIFY CLUB SIZE
# ==============================
BIG_CLUBS = {
    "arsenal",
    "liverpool fc",
    "manchester united",
    "manchester city",
    "chelsea",
    "tottenham hotspur",
    "real madrid",
    "barcelona",
    "psg",
    "atlético de madrid",
    "napoli",
    "inter milan",
    "milan",
    "juventus",
    "bayern munich",
    "borussia dortmund"
}

def classify_club(team_name: str) -> str:
    """Return 'Big Club' if team is in the big clubs list, else 'Small Club'."""
    return "Big Club" if str(team_name).strip().lower() in BIG_CLUBS else "Small Club"

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
def get_sheets_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scope)
    return gspread.authorize(creds)

def preserve_notified_flags(df, ws):
    """
    Before wiping the sheet, read the existing 'Notified' values keyed by
    (Home Team, Away Team, Date) and carry them onto the freshly scraped
    rows, so already-alerted matches don't get alerted again.
    """
    df["Notified"] = "FALSE"
    try:
        existing = ws.get_all_records()
    except Exception as e:
        logging.warning(f"Could not read existing sheet for Notified carry-over: {e}")
        return df

    notified_map = {}
    for row in existing:
        key = (row.get("Home Team"), row.get("Away Team"), row.get("Date"))
        notified_map[key] = str(row.get("Notified", "")).strip().upper()

    def lookup(row):
        key = (row["Home Team"], row["Away Team"], row["Date"])
        return "TRUE" if notified_map.get(key) == "TRUE" else "FALSE"

    df["Notified"] = df.apply(lookup, axis=1)
    logging.info("Notified flags carried over from previous sheet state.")
    return df

def upload_to_sheets(client, df):
    sheet = client.open_by_url(FIXTURES_SHEET_URL)
    ws = sheet.sheet1
    df = preserve_notified_flags(df, ws)
    ws.clear()
    df = df.fillna("").astype(str)
    ws.update([df.columns.tolist()] + df.values.tolist())
    logging.info(f"Uploaded {len(df)} rows to Google Sheets.")
    return ws, df

# ==============================
# ✅ ALERTS — find matches due soon
# ==============================
def matches_due_soon(df):
    now = datetime.now(WIB)
    due = []
    for _, row in df.iterrows():
        if str(row.get("Notified", "")).strip().upper() == "TRUE":
            continue

        match_time_str = str(row.get("MatchTime", "")).strip()
        if not match_time_str:
            continue

        try:
            kickoff = datetime.strptime(match_time_str, "%m/%d/%Y %H:%M").replace(tzinfo=WIB)
        except ValueError:
            continue

        minutes_until = (kickoff - now).total_seconds() / 60
        if NOTIFY_MIN_MINUTES <= minutes_until <= NOTIFY_MAX_MINUTES:
            due.append(row)

    return due

# ==============================
# ✅ ALERTS — load reader contacts
# ==============================
def load_readers(client):
    """
    Pull email addresses from the 'info' tab, and Telegram chat IDs from an
    'info_telegram' tab (columns: Nama, Telegram Chat ID).

    NOTE: Telegram only lets a bot message users who have already messaged
    the bot at least once — readers need to /start your bot and you need to
    record their chat ID in 'info_telegram' (e.g. via a form field).
    """
    sheet = client.open_by_url(READERS_SHEET_URL)

    emails = []
    try:
        info_ws = sheet.worksheet("info")
        for row in info_ws.get_all_records():
            email = str(row.get("Email", "")).strip()
            if email:
                emails.append(email)
    except Exception as e:
        logging.warning(f"Could not load 'info' sheet for emails: {e}")

    chat_ids = []
    try:
        tg_ws = sheet.worksheet("info_telegram")
        for row in tg_ws.get_all_records():
            cid = str(row.get("Telegram Chat ID", "")).strip()
            if cid:
                chat_ids.append(cid)
    except Exception as e:
        logging.warning(f"'info_telegram' sheet not found or unreadable, skipping Telegram: {e}")

    return sorted(set(emails)), sorted(set(chat_ids))

# ==============================
# ✅ ALERTS — Telegram
# ==============================
def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set — skipping Telegram send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        if resp.status_code == 200:
            return True
        logging.error(f"Telegram send failed for {chat_id} ({resp.status_code}): {resp.text}")
    except Exception as e:
        logging.error(f"Telegram send error for {chat_id}: {e}")
    return False

# ==============================
# ✅ ALERTS — Gmail digest
# ==============================
def send_email_digest(recipients, subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        logging.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not recipients:
        logging.info("No email recipients — skipping email digest.")
        return

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["Subject"] = subject
    msg["Bcc"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
        logging.info(f"Email digest sent to {len(recipients)} recipients.")
    except Exception as e:
        logging.error(f"Email send failed: {e}")

# ==============================
# ✅ ALERTS — message builder
# ==============================
def build_alert_text(matches):
    lines = ["⚽ Matches kicking off soon:\n"]
    for m in matches:
        lines.append(
            f"{m.get('Home Team')} vs {m.get('Away Team')} — "
            f"{m.get('Time')} WIB ({m.get('Competition')})"
        )
    return "\n".join(lines)

# ==============================
# ✅ ALERTS — mark matches as notified
# ==============================
def mark_as_notified(ws, notified_ids):
    header = ws.row_values(1)
    if "Notified" not in header or "ID" not in header:
        logging.warning("'Notified' or 'ID' column missing — cannot mark rows as notified.")
        return

    id_col_index = header.index("ID") + 1
    notified_col_index = header.index("Notified") + 1

    id_column_values = ws.col_values(id_col_index)  # includes header at index 0
    for row_num, cell_id in enumerate(id_column_values[1:], start=2):
        if cell_id in notified_ids:
            ws.update_cell(row_num, notified_col_index, "TRUE")

# ==============================
# ✅ ALERTS — run the check-and-notify step
# ==============================
def run_alerts(client, ws, df):
    due = matches_due_soon(df)
    if not due:
        logging.info("No matches due for alerting right now.")
        return

    logging.info(f"{len(due)} match(es) due for alerts.")
    emails, chat_ids = load_readers(client)
    alert_text = build_alert_text(due)

    sent_telegram = 0
    for cid in chat_ids:
        if send_telegram_message(cid, alert_text):
            sent_telegram += 1
        time.sleep(0.3)  # gentle on Telegram rate limits
    logging.info(f"Telegram: sent to {sent_telegram}/{len(chat_ids)} chat IDs.")

    send_email_digest(emails, "⚽ Matches starting soon!", alert_text)

    notified_ids = {row.get("ID") for row in due if row.get("ID")}
    mark_as_notified(ws, notified_ids)
    logging.info(f"Marked {len(notified_ids)} match(es) as notified.")

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

                client = get_sheets_client()
                ws, df = upload_to_sheets(client, df)
                run_alerts(client, ws, df)

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
