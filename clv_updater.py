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

# 1. Load existing rows from CLV tab to check what's already there
clv_rows = clv_sheet.get_all_values()
# Map existing Event ID + Bet + Bookmaker to row index so we know what to update
existing_clv_keys = {}
for idx, row in enumerate(clv_rows[1:], start=2):  # Skip header row
    if len(row) >= 6 and row[5]:  # Column F is Event ID
        key = f"{row[5]}_{row[1]}_{row[0]}"  # EventID_Bookmaker_Bet
        existing_clv_keys[key] = {
            'row_index': idx,
            'clv_val': row[3]  # Column D is CLV[cite: 9]
        }

# 2. Read all bets from TRACKER tab
tracker_rows = tracker_sheet.get_all_values()
header = tracker_rows[0]

# Find column indices dynamically in TRACKER
try:
    game_idx = header.index("Game")
    bookie_idx = header.index("Bookmaker")
    selection_idx = header.index("Selection")
    odds_idx = header.index("Odds")
    event_id_idx = header.index("Event ID")
except ValueError:
    # Fallback default positions based on standard layout
    game_idx, bookie_idx, selection_idx, odds_idx, event_id_idx = 0, 1, 2, 3, 13

print(f"Scanning {len(tracker_rows)-1} bets from TRACKER for CLV sync...")

new_appends = []

for row in tracker_rows[1:]:
    if len(row) <= event_id_idx:
        continue
    
    matchup = row[game_idx]
    bookie = row[bookie_idx]
    selection = row[selection_idx]
    placed_odds = row[odds_idx]
    event_id = row[event_id_idx]

    if not event_id:
        continue  # Skip if no event ID recorded

    clv_key = f"{event_id}_{bookie}_{selection}"

    # If it's not yet in the CLV tab, queue it to be added
    if clv_key not in existing_clv_keys:
        # CLV Tab Structure: [BET (A), BOOKMAKER (B), PLACED ODDS (C), CLV (D), CLV EDGE % (E), EVENT ID (F)][cite: 9]
        # We use a formula for Column E: =(C2/D2)-1
        next_row_num = len(clv_rows) + len(new_appends) + 1
        formula_edge = f"=(C{next_row_num}/D{next_row_num})-1"
        
        new_row = [selection, bookie, placed_odds, "", formula_edge, event_id]
        new_appends.append(new_row)
        existing_clv_keys[clv_key] = {
            'row_index': next_row_num,
            'clv_val': ""
        }

if new_appends:
    print(f"Appending {len(new_appends)} new bets to the CLV tab...")
    clv_sheet.append_rows(new_appends, value_input_option='USER_ENTERED')
    # Refresh clv_rows after append
    clv_rows = clv_sheet.get_all_values()

print("Checking Pinnacle closing odds for pending bets...")

# Re-scan CLV sheet rows to find blanks in Column D (CLV)[cite: 9]
for idx, row in enumerate(clv_rows[1:], start=2):
    if len(row) >= 6:
        bet_sel = row[0]
        bookie_name = row[1]
        clv_price = row[3]  # Column D[cite: 9]
        event_id = row[5]   # Column F[cite: 9]

        if event_id and (not clv_price or clv_price == ""):
            print(f"  Fetching closing odds for Event ID {event_id} ({bet_sel} at {bookie_name})...")
            
            # Since we don't know the sport key directly from the ID, we can search via API or iterate sports if needed.
            # Alternatively, query the active sports list to locate the event:
            sports_url = f'https://api.the-odds-api.com/v4/sports/?apiKey={API_KEY}'
            try:
                sports_res = requests.get(sports_url, timeout=10)
                if sports_res.status_code == 200:
                    sports = sports_res.json()
                    found_closing = False
                    
                    for sport in sports:
                        sport_key = sport['key']
                        event_odds_url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds/?apiKey={API_KEY}&regions=au,eu&oddsFormat=decimal'
                        event_res = requests.get(event_odds_url, timeout=10)
                        
                        if event_res.status_code == 200:
                            event_data = event_res.json()
                            # Look for Pinnacle in bookmakers
                            for bookmaker in event_data.get('bookmakers', []):
                                if bookmaker['title'] == 'Pinnacle':
                                    for market in bookmaker.get('markets', []):
                                        for outcome in market.get('outcomes', []):
                                            # Match outcome name/point/description to find the exact closing price
                                            match_str = f"{outcome.get('description', '')} - {outcome['name']} {outcome.get('point', '')} ({market['key']})".strip()
                                            if outcome['name'] in bet_sel or outcome.get('description', '') in bet_sel:
                                                closing_price = outcome['price']
                                                # Write closing price to Column D (Cell D{idx})[cite: 9]
                                                clv_sheet.update_cell(idx, 4, closing_price)
                                                print(f"    ✅ Updated CLV for row {idx}: ${closing_price}")
                                                found_closing = True
                                                break
                                        if found_closing:
                                            break
                                if found_closing:
                                    break
                        if found_closing:
                            break
                        time.sleep(0.2)
                time.sleep(0.5)
            except Exception as e:
                print(f"    ⚠️ Error fetching CLV for event {event_id}: {e}")

print("\nCLV Update Complete!")
sys.stdout.flush()
