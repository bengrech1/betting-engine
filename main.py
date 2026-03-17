import os
import json
import gspread
import requests
import time
import datetime

print("Authenticating with GitHub Secrets...")

gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    exit()

credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

SHEET_NAME = "BETTING"
try:
    # FIX 1: Laser targets the BOT FEED tab specifically
    worksheet = gc.open(SHEET_NAME).worksheet("BOT FEED")
    print(f"Successfully connected to sheet: BOT FEED")
    print(f"🚨 I AM WRITING TO THIS EXACT URL: {worksheet.spreadsheet.url}")
except gspread.WorksheetNotFound:
    print(f"Error: Could not find a tab named 'BOT FEED'.")
    exit()

print("Fetching existing bets from the sheet to prevent duplicates...")
existing_rows = worksheet.get_all_values()
existing_bets = set()
for row in existing_rows:
    if len(row) >= 3:
        unique_id = f"{row[0]}_{row[1]}_{row[2]}"
        existing_bets.add(unique_id)
print(f"Found {len(existing_bets)} existing bets in the sheet.\n")

API_KEY = os.environ.get('ODDS_API_KEY')
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK')

if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    exit()

REGIONS = 'au,eu'
ODDS_FORMAT = 'decimal'
MIN_EDGE = 0.02 

SPORT_MARKETS = {
    'basketball_nba': 'h2h,player_points,player_rebounds,player_assists',
    'aussierules_afl': 'h2h,player_disposals',
    'rugbyleague_nrl': 'h2h,spreads,totals',
    'soccer_epl': 'h2h,spreads,totals'
}

profitable_bets_found = 0
new_bets_logged = 0

for sport, markets in SPORT_MARKETS.items():
    print(f"\nFetching schedule for {sport}...")
    events_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events/?apiKey={API_KEY}'
    events_response = requests.get(events_url)
    
    if events_response.status_code != 200:
        print(f"  Failed to fetch schedule for {sport}. Status: {events_response.status_code}")
        continue
        
    events = events_response.json()
    
    for event in events:
        commence_time = datetime.datetime.strptime(event['commence_time'], '%Y-%m-%dT%H:%M:%SZ')
        if commence_time < datetime.datetime.utcnow():
            continue 
            
        event_id = event['id']
        matchup = f"{event.get('home_team', 'Unknown')} vs {event.get('away_team', 'Unknown')}"
        print(f"  Scanning {matchup}...")
        
        odds_url = f'https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds/?apiKey={API_KEY}&regions={REGIONS}&markets={markets}&oddsFormat={ODDS_FORMAT}'
        odds_response = requests.get(odds_url)
        
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
                    
                    if len(pinnacle_outcomes) == 2:
                        ip_1 = 1 / pinnacle_outcomes[0]['price']
                        ip_2 = 1 / pinnacle_outcomes[1]['price']
                        total_market = ip_1 + ip_2
                        
                        tp_1 = ip_1 / total_market
                        tp_2 = ip_2 / total_market
                        
                        for bookie in game['bookmakers']:
                            # FIX 2: The New Upgraded Arsenal 
                            if bookie['title'] in ['Sportsbet', 'TAB', 'Betr', 'Bet365', 'PointsBet (AU)', 'PointsBet', 'Unibet']:
                                for b_market in bookie['markets']:
                                    if b_market['key'] == market_key:
                                        for idx, outcome in enumerate(b_market['outcomes']):
                                            tp = tp_1 if idx == 0 else tp_2
                                            local_odds = outcome['price']
                                            pinnacle_price = None
                                            for p_out in pinnacle_outcomes:
                                                if p_out['name'] == outcome['name'] and p_out.get('point') == outcome.get('point') and p_out.get('description') == outcome.get('description'):
                                                    pinnacle_price = p_out['price']
                                                    break
                                                
                                            if pinnacle_price is None:
                                                continue
                                            
                                            edge = (tp * local_odds) - 1
                                            
                                            if edge >= MIN_EDGE:
                                                profitable_bets_found += 1
                                                edge_formatted = f"+{round(edge*100, 2)}%"
                                                
                                                if 'description' in outcome:
                                                    selection = f"{outcome['description']} - {outcome['name']} {outcome.get('point', '')} ({market_key})"
                                                else:
                                                    selection = f"{outcome['name']} {outcome.get('point', '')} ({market_key})"
                                                
                                                bet_id = f"{matchup}_{bookie['title']}_{selection}"
                                                
                                                if bet_id not in existing_bets:
                                                    print(f"    🚨 NEW EDGE: {selection} at {bookie['title']} ({edge_formatted}) | Pinny: {pinnacle_price}")
                                                    
                                                    row_data = [
                                                        matchup, bookie['title'], selection, local_odds, 
                                                        "", pinnacle_price, "", "", "", "", "", edge_formatted
                                                    ]
                                                    
                                                    # FIX 3: Force paste into Row 2
                                                    worksheet.insert_row(row_data, 2)
                                                    existing_bets.add(bet_id)
                                                    new_bets_logged += 1
                                                    
                                                    if DISCORD_WEBHOOK:
                                                        discord_message = {
                                                            "content": f"🚨 **NEW EDGE: {edge_formatted}** 🚨\n**Game:** {matchup}\n**Bet:** {selection}\n**Bookie:** {bookie['title']} @ **${local_odds}**\n**Pinnacle:** ${pinnacle_price}"
                                                        }
                                                        requests.post(DISCORD_WEBHOOK, json=discord_message)
                                                    
                                                    time.sleep(1)
        else:
            print(f"    Failed to fetch odds for {matchup}. Status: {odds_response.status_code}")
            
        time.sleep(0.5) 

print(f"\nMaster Scan Complete! Found {profitable_bets_found} edges, logged {new_bets_logged} NEW bets.")
