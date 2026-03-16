import os
import json
import gspread
import requests
import time

print("Authenticating with GitHub Secrets...")

gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    exit()

credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

SHEET_NAME = "BETTING"
try:
    worksheet = gc.open(SHEET_NAME).sheet1
    print(f"Successfully connected to sheet: {SHEET_NAME}\n")
except gspread.SpreadsheetNotFound:
    print(f"Error: Could not find a Google Sheet named '{SHEET_NAME}'.")
    exit()

API_KEY = os.environ.get('ODDS_API_KEY')
if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    exit()

REGIONS = 'au,eu'
ODDS_FORMAT = 'decimal'
MIN_EDGE = 0.02 

# 1. The Market Dictionary: Targets specific props for specific sports
SPORT_MARKETS = {
    'basketball_nba': 'h2h,player_points,player_rebounds,player_assists',
    'aussierules_afl': 'h2h,player_disposals',
    'soccer_epl': 'h2h'
}

profitable_bets_found = 0

for sport, markets in SPORT_MARKETS.items():
    print(f"Scanning {sport} for markets: {markets}...")
    url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={API_KEY}&regions={REGIONS}&markets={markets}&oddsFormat={ODDS_FORMAT}'
    
    response = requests.get(url)
    
    if response.status_code == 200:
        odds_json = response.json()
        
        for game in odds_json:
            matchup = f"{game.get('home_team', 'Unknown')} vs {game.get('away_team', 'Unknown')}"
            
            # Group all bookies by market first
            for market_data in game.get('bookmakers', []):
                for market in market_data.get('markets', []):
                    market_key = market['key']
                    
                    # We need to find Pinnacle's odds for this specific market to calculate the true probability
                    pinnacle_outcomes = []
                    for b in game['bookmakers']:
                        if b['title'] == 'Pinnacle':
                            for m in b['markets']:
                                if m['key'] == market_key:
                                    pinnacle_outcomes = m['outcomes']
                                    break
                    
                    # Only proceed if Pinnacle has priced this exact market and it's a 2-way market (like Over/Under)
                    if len(pinnacle_outcomes) == 2:
                        ip_1 = 1 / pinnacle_outcomes[0]['price']
                        ip_2 = 1 / pinnacle_outcomes[1]['price']
                        total_market = ip_1 + ip_2
                        
                        tp_1 = ip_1 / total_market
                        tp_2 = ip_2 / total_market
                        
                        for bookie in game['bookmakers']:
                            if bookie['title'] in ['Sportsbet', 'TAB', 'Neds', 'Ladbrokes', 'Betr']:
                                for b_market in bookie['markets']:
                                    if b_market['key'] == market_key:
                                        # Match the outcomes
                                        for idx, outcome in enumerate(b_market['outcomes']):
                                            # Try to match the exact outcome to Pinnacle's TP
                                            # (Assuming order matches for simplicity in this script)
                                            tp = tp_1 if idx == 0 else tp_2
                                            local_odds = outcome['price']
                                            
                                            edge = (tp * local_odds) - 1
                                            
                                            if edge >= MIN_EDGE:
                                                profitable_bets_found += 1
                                                edge_formatted = f"+{round(edge*100, 2)}%"
                                                
                                                # 2. The Selection Formatter
                                                # If it's a prop, it has a 'description' (Player Name) and 'point' (The Line)
                                                if 'description' in outcome:
                                                    selection = f"{outcome['description']} - {outcome['name']} {outcome.get('point', '')} ({market_key})"
                                                else:
                                                    selection = f"{outcome['name']} ({market_key})"
                                                
                                                print(f"  🚨 EDGE: {selection} at {bookie['title']} ({edge_formatted})")
                                                
                                                # Pushes to Sheets: Matchup (A), Bookie (B), Selection (C), Odds (D), Edge (L)
                                                row_data = [
                                                    matchup, bookie['title'], selection, local_odds, 
                                                    "", "", "", "", "", "", "", edge_formatted
                                                ]
                                                worksheet.append_row(row_data)
                                                time.sleep(1)
    else:
        print(f"  Failed to fetch data for {sport}.")

print(f"\nMaster Scan Complete! Logged {profitable_bets_found} high-value bets.")
