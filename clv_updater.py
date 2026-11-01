import os
import json
import gspread
import requests
import time
import sys

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

# 1. Load existing A:F values from CLV tab
col_a = clv_sheet.col_values(1)  # Get all values in Column A
existing_clv_rows_count = len(col_a)

clv_data = clv_sheet.get_all_values()
existing_clv_keys = {}

# Map existing Event ID + Bet + Bookmaker to row index
for idx, row in enumerate(clv_data[1:], start=2):  # Skip header row
    if len(row) >= 6 and row[5]:  # Column F is Event ID
        key = f"{row[5]}_{row[1]}_{row[0]}".strip()  # EventID_Bookmaker_Bet
        existing_clv_keys[key] = {
            'row_index': idx,
            'clv_val': row[3] if len(row) > 3 else ""  # Column D is CLV
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
    
    matchup = row[game_idx]
    bookie = row[bookie_idx]
    selection = row[selection_idx]
    placed_odds = row[odds_idx]
    event_id = row[event_id_idx]

    if not event_id:
        continue

    clv_key = f"{event_id}_{bookie}_{selection}".strip()

    if clv_key not in existing_clv_keys:
        target_row = current_next_row + len(new_appends)
        # Use IFERROR to prevent #DIV/0! when Column D is blank
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
    
    # Refresh clv_data
    clv_data = clv_sheet.get_all_values()

print("Checking Pinnacle closing odds for pending bets...")

# Re-scan CLV sheet rows to find blanks in Column D
for idx, row in enumerate(clv_data[1:], start=2):
    if len(row) >= 6:
        bet_sel = row[0].strip()
        bookie_name = row[1]
        clv_price = row[3] if len(row) > 3 else ""
        event_id = row[5].strip()

        if event_id and (not clv_price or clv_price == "" or clv_price == "0"):
            print(f"  Fetching closing odds for Event ID {event_id} ({bet_sel})...")
            
            # Fetch event odds directly across regions
            event_odds_url = f'https://api.the-odds-api.com/v4/sports/upcoming/odds/?apiKey={API_KEY}&regions=eu,us,uk&eventIds={event_id}&bookmakers=pinnacle&oddsFormat=decimal'
            
            try:
                event_res = requests.get(event_odds_url, timeout=10)
                if event_res.status_code == 200:
                    events = event_res.json()
                    found_closing = False
                    
                    for event_data in events:
                        for bookmaker in event_data.get('bookmakers', []):
                            if bookmaker.get('key') == 'pinnacle':
                                for market in bookmaker.get('markets', []):
                                    for outcome in market.get('outcomes', []):
                                        outcome_name = outcome['name'].strip()
                                        # Clean matching logic
                                        if outcome_name.lower() in bet_sel.lower() or bet_sel.lower() in outcome_name.lower():
                                            closing_price = outcome['price']
                                            clv_sheet.update_cell(idx, 4, closing_price)
                                            print(f"    ✅ Updated CLV for Row {idx}: {closing_price}")
                                            found_closing = True
                                            break
                                    if found_closing:
                                        break
                            if found_closing:
                                break
                else:
                    print(f"    ⚠️ API response {event_res.status_code} for event {event_id}")
                time.sleep(0.3)
            except Exception as e:
                print(f"    ⚠️ Error fetching CLV for event {event_id}: {e}")

print("\nCLV Update Complete!")
sys.stdout.flush()
