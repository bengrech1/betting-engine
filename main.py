import os
import json
import gspread
import requests
import time
import datetime
import sys

print("Authenticating with GitHub Secrets...")

gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    sys.exit(1)

credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

SHEET_NAME = "BETTING"
try:
    worksheet = gc.open(SHEET_NAME).worksheet("BOT FEED")
    print(f"Successfully connected to sheet: BOT FEED")
except gspread.WorksheetNotFound:
    print(f"Error: Could not find a tab named 'BOT FEED'.")
    sys.exit(1)

print("Fetching existing bets from BOTH sheets to prevent duplicates...")
existing_bets = set()

# Scan the BOT FEED
existing_rows = worksheet.get_all_values()
for row in existing_rows:
    if len(row) >= 3:
        existing_bets.add(f"{row[0]}_{row[1]}_{row[2]}")

# Scan the TRACKER
try:
    tracker_sheet = gc.open(SHEET_NAME).worksheet("TRACKER")
    tracker_rows = tracker_sheet.get_all_values()
    for row in tracker_rows:
        if len(row) >= 3:
            existing_bets.add(f"{row[0]}_{row[1]}_{row[2]}")
except gspread.WorksheetNotFound:
    print("No TRACKER tab found yet, skipping.")

print(f"Found {len(existing_bets)} total historical bets.\n")

API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK')

if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    sys.exit(1)

REGIONS = 'au,eu'
ODDS_FORMAT = 'decimal'
MIN_EDGE = 0.015 

# --- DYNAMIC GLOBAL ACTIVE SPORTS FETCHER ---
print("Scanning The Odds API for ALL active global sports...")
active_sports_url = f'https://api.the-odds-api.com/v4/sports/?apiKey={API_KEY}'
SPORT_MARKETS = {}

try:
    active_response = requests.get(active_sports_url, timeout=10)
    if active_response.status_code == 200:
        sports_data = active_response.json()
        
        for sport in sports_data:
            # If the sport is currently active, add it to the scan list automatically
            if sport.get('active', False):
                key = sport['key']
                group = sport.get('group', '').lower()
                
                # Automatically assign core liquid markets based on sport type to maximize edge volume safely
                if 'soccer' in group or 'football' in group or 'basketball' in group:
                    SPORT_MARKETS[key] = 'h2h,spreads,totals'
                elif 'tennis' in group:
                    SPORT_MARKETS[key] = 'h2h'
                else:
                    # Default for all other sports (cricket, rugby, hockey, baseball, etc.)
                    SPORT_MARKETS[key] = 'h2h,spreads,totals'
                    
        print(f"Success! Automatically loaded {len(SPORT_MARKETS)} active sports worldwide for maximum volume scanning.")
    else:
        print("Failed to fetch active sports list. Using fallback.")
        SPORT_MARKETS = {'basketball_nba': 'h2h,spreads,totals', 'soccer_epl': 'h2h,spreads,totals'}
except Exception as e:
    print(f"Error fetching active sports: {e}. Using fallback.")
    SPORT_MARKETS = {'basketball_nba': 'h2h,spreads,totals', 'soccer_epl': 'h2h,spreads,totals'}
# ---------------------------------------------

profitable_bets_found = 0
new_bets_logged = 0

for sport, markets in SPORT_MARKETS.items():
    print(f"\nFetching schedule for {sport}...")
    events_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events/?apiKey={API_KEY}'
    
    try:
        events_response = requests.get(events_url, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"  API Timeout or Error fetching schedule for {sport}: {e}")
        continue
        
    if events_response.status_code != 200:
        print(f"  Failed to fetch schedule for {sport}. Status: {events_response.status_code}")
        continue
        
    events = events_response.json()
    
    for event in events:
        commence_time = datetime.datetime.strptime(event['commence_time'], '%Y-%m-%dT%H:%M:%SZ')
        now_utc = datetime.datetime.utcnow()
        
        # THE MONEY SAVER: Only scan games starting in the next 48 hours
        cutoff_time = now_utc + datetime.timedelta(hours=48)
        
        if commence_time < now_utc or commence_time > cutoff_time:
            continue 
            
        event_id = event['id']
        matchup = f"{event.get('home_team', 'Unknown')} vs {event.get('away_team', 'Unknown')}"
        print(f"  Scanning {matchup}...")
        
        # Pass the commenceTimeTo parameter to save API credits on the prop markets
        time_limit_str = cutoff_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        odds_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds/?apiKey={API_KEY}&regions={REGIONS}&markets={markets}&oddsFormat={ODDS_FORMAT}&commenceTimeTo={time_limit_str}'
        
        try:
            odds_response = requests.get(odds_url, timeout=10)
        except requests.exceptions.RequestException:
            print(f"    API hung up on {matchup}. Skipping to next game...")
            continue
            
        if odds_response.status_code == 200:
            game = odds_response.json()
            
            for market_data in game.get('bookmakers', []):
                for market in market_data.get('markets', []):
                    market_key = market['key']
                    
                    pinnacle_outcomes = []
                    for b in game['bookmakers']:
                        if b['title'] == 'Pinnacle':
                            for m in b['markets']:
                                if m['key'] == market_key:
                                    pinnacle_outcomes = m['outcomes']
                                    break
                    
                    if not pinnacle_outcomes:
                        continue
                    
                    total_market_implied = sum((1 / p['price']) for p in pinnacle_outcomes)
                    
                    for bookie in game['bookmakers']:
                        if bookie['title'] in ['Sportsbet', 'TAB', 'Betr', 'Bet365', 'PointsBet (AU)', 'PointsBet', 'Unibet', 'Neds', 'Ladbrokes', 'Bet365 AU', 'PlayUp', 'TABtouch']:
                            for b_market in bookie['markets']:
                                if b_market['key'] == market_key:
                                    for outcome in b_market['outcomes']:
                                        local_odds = outcome['price']
                                        
                                        pinnacle_price = None
                                        for p_out in pinnacle_outcomes:
                                            if p_out['name'] == outcome['name'] and p_out.get('point') == outcome.get('point') and p_out.get('description') == outcome.get('description'):
                                                pinnacle_price = p_out['price']
                                                break
                                                
                                        if pinnacle_price is None:
                                            continue
                                            
                                        raw_prob = 1 / pinnacle_price
                                        true_prob = raw_prob / total_market_implied
                                        edge = (true_prob * local_odds) - 1
                                        
                                        if edge >= MIN_EDGE:
                                            profitable_bets_found += 1
                                            edge_formatted = f"+{round(edge*100, 2)}%"
                                            
                                            if 'description' in outcome:
                                                selection = f"{outcome['description']} - {outcome['name']} {outcome.get('point', '')} ({market_key})"
                                            else:
                                                selection = f"{outcome['name']} {outcome.get('point', '')} ({market_key})"
                                                
                                            bet_id = f"{matchup}_{bookie['title']}_{selection}"
                                            
                                            if bet_id not in existing_bets:
                                                print(f"    🚨 NEW EDGE: {selection} at {bookie['title']} @ ${local_odds} ({edge_formatted}) \vert{} Pinny:${pinnacle_price}")
                                                
                                                stake_formula = f'=IFERROR(ROUND(({edge} / (D2 - 1) / 2) * BANKROLL!$B$4, 2), 0)'
                                                
                                                row_data = [
                                                    matchup, bookie['title'], selection, local_odds, 
                                                    stake_formula, pinnacle_price, "", "", "", "", "", edge_formatted
                                                ]
                                                
                                                worksheet.insert_row(row_data, 2, value_input_option='USER_ENTERED')
                                                existing_bets.add(bet_id)
                                                new_bets_logged += 1
                                                
                                                if DISCORD_WEBHOOK:
                                                    discord_message = {
                                                        "content": f"🚨 **NEW EDGE: {edge_formatted}** 🚨\n**Game:** {matchup}\n**Bet:** {selection}\n**Bookie:** {bookie['title']} @ **${local_odds}**\n**Pinnacle:** ${pinnacle_price}"
                                                    }
                                                    requests
