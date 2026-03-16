import os
import json
import gspread
import requests
import time

print("Authenticating with GitHub Secrets...")

# 1. Load the Bot Password from the Secret Vault
gcp_credentials_json = os.environ.get('GCP_CREDENTIALS')
if not gcp_credentials_json:
    print("Error: GCP_CREDENTIALS secret is missing!")
    exit()

# Parse the JSON string back into a dictionary to log into Google
credentials_dict = json.loads(gcp_credentials_json)
gc = gspread.service_account_from_dict(credentials_dict)

# 2. Sheet Connection
SHEET_NAME = "BETTING"
try:
    worksheet = gc.open(SHEET_NAME).sheet1
    print(f"Successfully connected to sheet: {SHEET_NAME}\n")
except gspread.SpreadsheetNotFound:
    print(f"Error: Could not find a Google Sheet named '{SHEET_NAME}'.")
    exit()

# 3. API Setup
API_KEY = os.environ.get('ODDS_API_KEY')
if not API_KEY:
    print("Error: ODDS_API_KEY secret is missing!")
    exit()

REGIONS = 'au,eu'
ODDS_FORMAT = 'decimal'
MIN_EDGE = 0.02

# Targeting H2H for this first cloud test. We will upgrade to Player Props next.
TARGET_SPORTS = ['basketball_nba', 'aussierules_afl']
TARGET_MARKET = 'h2h'

profitable_bets_found = 0

# 4. The Master Loop
for sport in TARGET_SPORTS:
    print(f"Scanning {sport}...")
    url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={API_KEY}&regions={REGIONS}&markets={TARGET_MARKET}&oddsFormat={ODDS_FORMAT}'
    
    response = requests.get(url)
    
    if response.status_code == 200:
        odds_json = response.json()
        
        for game in odds_json:
            matchup = f"{game.get('home_team', 'Unknown')} vs {game.get('away_team', 'Unknown')}"
            
            pinnacle_odds = {}
            for bookie in game['bookmakers']:
                if bookie['title'] == 'Pinnacle':
                    for outcome in bookie['markets'][0]['outcomes']:
                        pinnacle_odds[outcome['name']] = outcome['price']
            
            if pinnacle_odds and len(pinnacle_odds) == 2:
                teams = list(pinnacle_odds.keys())
                team_1, team_2 = teams[0], teams[1]
                
                ip_1 = 1 / pinnacle_odds[team_1]
                ip_2 = 1 / pinnacle_odds[team_2]
                total_market = ip_1 + ip_2
                
                tp_1 = ip_1 / total_market
                tp_2 = ip_2 / total_market
                
                for bookie in game['bookmakers']:
                    if bookie['title'] in ['Sportsbet', 'TAB', 'Neds', 'Ladbrokes', 'Betr']:
                        for outcome in bookie['markets'][0]['outcomes']:
                            team = outcome['name']
                            local_odds = outcome['price']
                            tp = tp_1 if team == team_1 else tp_2
                            
                            edge = (tp * local_odds) - 1
                            
                            if edge >= MIN_EDGE:
                                profitable_bets_found += 1
                                edge_formatted = f"+{round(edge*100, 2)}%"
                                print(f"  🚨 {sport} EDGE: {team} at {bookie['title']} ({edge_formatted})")
                                
                                row_data = [
                                    matchup, bookie['title'], team, local_odds, 
                                    "", "", "", "", "", "", "", edge_formatted
                                ]
                                worksheet.append_row(row_data)
                                time.sleep(1)
    else:
        print(f"  Failed to fetch data for {sport}.")
        
print(f"\nMaster Scan Complete! Logged {profitable_bets_found} high-value bets.")
