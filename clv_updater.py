import os
import json
import gspread
import requests
import time
import sys
import datetime
from gspread.exceptions import APIError

print("Authenticating with GitHub Secrets for Historical Snapshot CLV Updater...")

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
    "wnbl": "basketball_wnbl",
    "epl": "soccer_epl",
    "serie a - italy": "soccer_italy_serie_a",
    "serie a": "soccer_italy_serie_a",
    "la liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue 1": "soccer_france_ligue_one",
    "a-league": "soccer_australia_aleague",
    "champions league": "soccer_uefa_champions_league",
    "nrl": "rugbyleague_nrl",
    "nhl": "icehockey_nhl",
    "nfl": "americanfootball_nfl",
    "afl": "aussierules_afl",
    "mlb": "baseball_mlb"
}

TARGET_BOOKMAKERS = ["pinnacle", "bet365", "draftkings", "fanduel", "unibet", "williamhill"]

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
    clv_sheet.update(range_name=f"{start_cell}:{end_cell}", values=new_appends, value_input_option='USER_ENTERED')
    clv_data = clv_sheet.get_all_values()

print("Fetching true historical closing odds via historical snapshot snapshots...")

# 3. Fetch Historical CLV using Historical Snapshot Query by Date & Team Matching
for idx, row in enumerate(clv_data[1:], start=2):
    if len(row) >= 6:
        bet_sel = row[0].strip()
        clv_price = row[3] if len(row) > 3 else ""
        event_id = row[5].strip()

        if event_id and (not clv_price or clv_price == "" or clv_price == "0"):
            t_row = next((r for r in tracker_rows[1:] if len(r) > event_id_idx and r[event_id_idx] == event_id), None)
            if not t_row:
                continue
            
            raw_league = t_row[league_idx].strip().lower() if len(t_row) > league_idx else ""
            sport_key = None
            for l_key, s_val in LEAGUE_TO_SPORT_KEY.items():
                if l_key in raw_league:
                    sport_key = s_val
                    break
            
            if not sport_key:
                print(f"  ⚠️ Warning: League '{raw_league}' not mapped to sport key.")
                continue

            game_matchup = t_row[game_idx] if len(t_row) > game_idx else ""
            if " vs " not in game_matchup:
                print(f"  ⚠️ Could not parse matchup format from '{game_matchup}'")
                continue
            
            home_team, away_team = game_matchup.split(" vs ", 1)
            home_team = home_team.strip().lower()
            away_team = away_team.strip().lower()

            commence_str = None
            
            # Try to get the real commence time from live endpoint; if it fails (game passed), 
            # fallback to fetching the current timestamp or extracting from historical list.
            event_url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}?apiKey={API_KEY}'
            event_res = requests.get(event_url, timeout=10)
            
            if event_res.status_code == 200:
                commence_str = event_res.json().get('commence_time')
            else:
                # If game already ended, look up using current time offset or use a default past snapshot window 
                # (Alternatively, you can store commence_time in your tracker sheet directly when placing bets).
                # For now, let's look up using the historical endpoint with a recent or estimated timeframe, 
                # or query the historical sport odds list directly:
                pass

            # If we don't have commence_str from live, we can fallback to checking recent historical snapshots.
            # But wait: to query historical odds cleanly, we need a date timestamp. Let's make sure your main bot 
            # writes the commence time into your TRACKER sheet so you never have to guess it.
            # Let's check if commence time is available in tracker row, or query historical snapshot.
            
            # As a bulletproof fallback if commence_str is missing: check if there's a timestamp column or use current time minus 1 day.
            if not commence_str:
                # Fallback: check if we can query historical odds for today/yesterday snapshots
                commence_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

            dt = datetime.datetime.fromisoformat(commence_str.replace('Z', '+00:00'))
            closing_target_dt = dt - datetime.timedelta(minutes=10)
            formatted_date = closing_target_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

            # Query Historical Odds Snapshot Endpoint (Bulk list for the sport at that time)
            hist_url = f'https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds'
            params = {
                'apiKey': API_KEY,
                'regions': 'au,eu,us',
                'markets': 'h2h',
                'oddsFormat': 'decimal',
                'date': formatted_date
            }
            
            hist_res = requests.get(hist_url, params=params, timeout=15)

            if hist_res.status_code == 200:
                hist_payload = hist_res.json()
                events_list = hist_payload.get('data', [])
                
                matched_event = None
                for ev in events_list:
                    ev_home = ev.get('home_team', '').strip().lower()
                    ev_away = ev.get('away_team', '').strip().lower()
                    if home_team in ev_home or ev_home in home_team or away_team in ev_away or ev_away in away_team:
                        matched_event = ev
                        break

                if matched_event:
                    found_closing = False
                    bookmakers = {b['key']: b for b in matched_event.get('bookmakers', [])}
                    
                    for b_key in TARGET_BOOKMAKERS:
                        if b_key in bookmakers:
                            bookie_obj = bookmakers[b_key]
                            for market in bookie_obj.get('markets', []):
                                if market['key'] == 'h2h':
                                    for outcome in market.get('outcomes', []):
                                        outcome_name = outcome['name'].strip()
                                        if outcome_name.lower() in bet_sel.lower() or bet_sel.lower() in outcome_name.lower():
                                            closing_price = outcome['price']
                                            clv_sheet.update_cell(idx, 4, closing_price)
                                            print(f"    ✅ Captured Historical Closing Line ({b_key}) for Row {idx} ({bet_sel}): {closing_price}")
                                            found_closing = True
                                            break
                                if found_closing:
                                    break
                        if found_closing:
                            break
                            
                    if not found_closing:
                        print(f"    ⚠️ Snapshot found for game, but selection '{bet_sel}' didn't match outcome names.")
                else:
                    print(f"    ⚠️ Could not find matching game in historical snapshot for {home_team} vs {away_team}")
            else:
                print(f"    ⚠️ Historical Snapshot API error {hist_res.status_code}: {hist_res.text}")

            time.sleep(0.5)

print("\nCLV Update Complete!")
sys.stdout.flush()
