import os
import json
import gspread
import requests
import time
import sys
from gspread.exceptions import APIError

print("Authenticating with GitHub Secrets for CLV Updater...")

gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    sys.exit(1)

credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

SHEET_NAME = "BETTING"

def open_sheet_with_retry(gc, sheet_name, retries=3, delay=3):
    for attempt in range(retries):
        try:
            return gc.open(sheet_name)
        except Exception as e:
            print(f"  ⚠️ Google API error on attempt {attempt+1}/{retries}: {e}")
            if attempt == retries - 1:
                raise e
            time.sleep(delay)

try:
    print(f"Connecting to Google Sheet '{SHEET_NAME}'...")
    spreadsheet = open_sheet_with_retry(gc, SHEET_NAME)
    tracker_sheet = spreadsheet.worksheet("TRACKER")
    clv_sheet = spreadsheet.worksheet("CLV")
    print("Successfully connected to TRACKER and CLV tabs.")
except Exception as e:
    print(f"Critical error connecting to Google Sheets: {e}")
    sys.exit(1)

API_KEY = os.environ.get('ODDS_API_KEY')
if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    sys.exit(1)

LEAGUE_TO_SPORT_KEY = {
    "nbl": "basketball_nbl",
    "nba": "basketball_nba",
    "afl": "afl",
    "epl": "soccer_epl",
    "nrl": "rugbyleague_nrl",
    "wnbl": "basketball_wnbl"
}

# 1. Load existing CLV tab values
col_a = clv_sheet.col_values(1)
existing_clv_rows_count = len(col_a)
clv_data = clv_sheet.get_all_values()
existing_clv_keys = {}

for idx, row in enumerate(clv_data[1:], start=2):
    if len(row) >= 6 and row[5]:
        key = f"{row[5]}_{row[1]}_{row[0]}".strip()
        existing_clv_keys[key] = {
            'row_index': idx,
            'clv_val': row[3] if len(row) > 3 else ""
        }

# 2. Read bets from TRACKER tab
tracker_rows = tracker_sheet.get_all_values()
header = tracker_rows[0]

try:
    game_idx = header.index("Game")
    bookie_idx = header.index("Bookmaker")
    selection_idx = header.index("Selection")
    odds_idx = header.index("Odds")
    event_id_idx = header.index("Event ID")
    try:
        league_idx = header.index("League")
    except ValueError:
        league_idx = header.index("Sport") if "Sport" in header else 4
except ValueError as e:
    print(f"Header mapping error: {e}")
    game_idx, bookie_idx, selection_idx, odds_idx, event_id_idx, league_idx = 0, 1, 2, 3, 13, 4

print(f"Scanning {len(tracker_rows)-1} bets from TRACKER for CLV sync...")

new_appends = []
current_next_row = max(existing_clv_rows_count + 1, 2)

for row in tracker_rows[1:]:
    if len(row) <= event_id_idx:
        continue
    
    bookie = row[bookie_idx]
    selection = row[selection_idx]
    placed_odds = row[odds_idx]
    event_id = row[event_id_idx]

    if not event_id:
        continue

    clv_key = f"{event_id}_{bookie}_{selection}".strip()

    if clv_key not in existing_clv_keys:
        target_row = current_next_row + len(new_appends)
        formula_edge = f'=IFERROR((C{target_row}/D{target_row})-1, "")'
        
        new_row = [selection, bookie, float(placed_odds) if placed_odds else "", "", formula_edge, event_id]
        new_appends.append(new_row)
        existing_clv_keys[clv_key] = {
            'row_index': target_row,
            'clv_val': ""
        }

if new_appends:
    print(f"Appending {len(new_appends)} new bets starting at row {current_next_row}...")
    start_cell = f"A{current_next_row}"
    end_cell = f"F{current_next_row + len(new_appends) - 1}"
    clv_sheet.update(f"{start_cell}:{end_cell}", new_appends, value_input_option='USER_ENTERED')
    clv_data = clv_sheet.get_all_values()

print("Fetching true historical closing odds via mapped sport keys...")

# 3. Fetch Historical CLV
for idx, row in enumerate(clv_data[1:], start=2):
    if len(row) >= 6:
        bet_sel = row[0].strip()
        clv_price = row[3] if len(row) > 3 else ""
        event_id = row[5].strip()

        if event_id and (not clv_price or clv_price == "" or clv_price == "0"):
            t_row = next((r for r in tracker_rows[1:] if len(r) > event_id_idx and r[event_id_idx] == event_id), None)
            if not t_row:
                continue
            
            league_val = t_row[league_idx].strip().lower() if len(t_row) > league_idx else ""
            sport_key = LEAGUE_TO_SPORT_KEY.get(league_val)
            
            if not sport_key:
                print(f"  ⚠️ Warning: League '{league_val}' not mapped to a sport key in script dictionary.")
                continue

            # First, fetch event details/commence time from event endpoint or historical odds snapshot
            # Correct historical endpoint syntax:
            # We call the historical odds endpoint directly with ISO timestamp or query current event commence time
            print(f"  Querying historical archive for Event ID {event_id} under sport '{sport_key}'...")

            # Fetch event details using current/events endpoint if available, or fallback to historical request timestamp
            event_url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}?apiKey={API_KEY}'
            event_res = requests.get(event_url, timeout=10)
            
            commence_time = None
            if event_res.status_code == 200:
                commence_time = event_res.json().get('commence_time')
            
            # If historical query is needed and commence_time is found:
            if commence_time:
                hist_url = f'https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds'
                params = {
                    'apiKey': API_KEY,
                    'regions': 'eu,au,us,uk',
                    'markets': 'h2h',
                    'oddsFormat': 'decimal',
                    'eventIds': event_id,
                    'bookmakers': 'pinnacle',
                    'date': commence_time
                }
                hist_res = requests.get(hist_url, params=params, timeout=10)
                
                if hist_res.status_code == 200:
                    hist_json = hist_res.json()
                    events = hist_json.get('data', [])
                    
                    found_closing = False
                    for event_data in events:
                        for bookmaker in event_data.get('bookmakers', []):
                            if bookmaker.get('key') == 'pinnacle':
                                for market in bookmaker.get('markets', []):
                                    for outcome in market.get('outcomes', []):
                                        outcome_name = outcome['name'].strip()
                                        if outcome_name.lower() in bet_sel.lower() or bet_sel.lower() in outcome_name.lower():
                                            closing_price = outcome['price']
                                            clv_sheet.update_cell(idx, 4, closing_price)
                                            print(f"    ✅ Captured True Closing Line for Row {idx}: {closing_price}")
                                            found_closing = True
                                            break
                                    if found_closing:
                                        break
                            if found_closing:
                                break
                        if found_closing:
                            break
            else:
                print(f"  ⚠️ Could not determine commence_time for Event ID {event_id} (Status {event_res.status_code})")
            
            time.sleep(0.2)

print("\nCLV Update Complete!")
sys.stdout.flush()
