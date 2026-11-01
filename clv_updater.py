import os
import json
import gspread
import requests
import time
import sys
from datetime import datetime

print("Authenticating with GitHub Secrets for CLV Updater...")

gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    sys.exit(1)

credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

SHEET_NAME = "BETTING"
try:
    tracker_sheet = gc.open(SHEET_NAME).worksheet("TRACKER")
    clv_sheet = gc.open(SHEET_NAME).worksheet("CLV")
    print("Successfully connected to TRACKER and CLV tabs.")
except gspread.WorksheetNotFound as e:
    print(f"Error finding worksheet: {e}")
    sys.exit(1)

API_KEY = os.environ.get('ODDS_API_KEY')
if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    sys.exit(1)

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
except ValueError:
    game_idx, bookie_idx, selection_idx, odds_idx, event_id_idx = 0, 1, 2, 3, 13

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
        formula_edge = f"=IFERROR((C{target_row}/D{target_row})-1, \"\")"
        
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

print("Fetching true historical closing odds for concluded/past bets...")

# Get list of all sports to map event IDs to sport keys
sports_url = f'https://api.the-odds-api.com/v4/sports/?apiKey={API_KEY}'
sports_res = requests.get(sports_url, timeout=10)
sport_keys = [s['key'] for s in sports_res.json()] if sports_res.status_code == 200 else []

# Scan CLV sheet rows to find blanks in Column D (CLV)
for idx, row in enumerate(clv_data[1:], start=2):
    if len(row) >= 6:
        bet_sel = row[0].strip()
        clv_price = row[3] if len(row) > 3 else ""
        event_id = row[5].strip()

        if event_id and (not clv_price or clv_price == "" or clv_price == "0"):
            print(f"  Searching historical data for Event ID {event_id} ({bet_sel})...")
            
            found_closing = False
            for sport_key in sport_keys:
                # First, check event details to get exact commence time
                event_details_url = f'https://api.the-odds-api.com/v4/historical/sports/{sport_key}/events/{event_id}?apiKey={API_KEY}'
                detail_res = requests.get(event_details_url, timeout=10)
                
                if detail_res.status_code == 200:
                    event_info = detail_res.json()
                    commence_time = event_info.get('commence_time')
                    
                    if commence_time:
                        # Query historical odds at the exact commence time (closing line snapshot)
                        hist_odds_url = f'https://api.the-odds-api.com/v4/historical/sports/{sport_key}/events/{event_id}/odds?apiKey={API_KEY}&regions=eu,us,uk,au&markets=h2h&oddsFormat=decimal&date={commence_time}'
                        hist_res = requests.get(hist_odds_url, timeout=10)
                        
                        if hist_res.status_code == 200:
                            hist_data = hist_res.json()
                            # Historical endpoint returns data inside a snapshot wrapper
                            bookmakers = hist_data.get('data', {}).get('bookmakers', []) or hist_data.get('bookmakers', [])
                            
                            for bookmaker in bookmakers:
                                if bookmaker.get('key') == 'pinnacle':
                                    for market in bookmaker.get('markets', []):
                                        for outcome in market.get('outcomes', []):
                                            outcome_name = outcome['name'].strip()
                                            if outcome_name.lower() in bet_sel.lower() or bet_sel.lower() in outcome_name.lower():
                                                closing_price = outcome['price']
                                                clv_sheet.update_cell(idx, 4, closing_price)
                                                print(f"    ✅ Captured Pinnacle Closing Line for Row {idx}: {closing_price}")
                                                found_closing = True
                                                break
                                        if found_closing:
                                            break
                                if found_closing:
                                    break
                if found_closing:
                    break
                time.sleep(0.2)

print("\nCLV Update Complete!")
sys.stdout.flush()
