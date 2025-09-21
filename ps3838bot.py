import re
import os
import json
import requests
from telethon import TelegramClient, events
from dotenv import load_dotenv

# --- CONFIG FILE HANDLING ---
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "base_stake": 5,
    "min_stake": 5,
    "odds_tolerance": 0.01,
    "allow_tennis": True,
    "allow_football": True
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

config = load_config()

# Load environment variables from .env file
load_dotenv()

api_id = int(os.getenv("API_ID", 0))                  # Telegram API ID (int)
api_hash = os.getenv("API_HASH", "")                  # Telegram API hash
telegram_channel = os.getenv("TELEGRAM_CHANNEL", "")  # Telegram channel/group

PS3838_API_URL = os.getenv("PS3838_API_URL", "https://api.ps3838.com/v3")
PS3838_USERNAME = os.getenv("PS3838_USERNAME", "")
PS3838_PASSWORD = os.getenv("PS3838_PASSWORD", "")

session = requests.Session()
session.auth = (PS3838_USERNAME, PS3838_PASSWORD)

# --- PARSING FUNCTION ---
def parse_message(message_text):
    sport = "Other"
    if "Tennis" in message_text:
        sport = "Tennis"
    elif "Football" in message_text or "Soccer" in message_text:
        sport = "Football"

    if (sport == "Tennis" and not config["allow_tennis"]) or \
       (sport == "Football" and not config["allow_football"]) or \
       sport == "Other":
        print(f"Ignored bet - sport not allowed: {sport}")
        return None

    match = re.search(r"(.+) vs (.+)", message_text)
    if not match:
        return None
    player1, player2 = match.groups()

    bet = re.search(r"(ML Match|HDP Match) : (.+) @ ([0-9.,]+) \(([0-9.]+) U\)", message_text)
    if not bet:
        return None
    market_type, selection, odds, stake_units = bet.groups()
    odds = float(odds.replace(",", "."))
    stake_units = float(stake_units)
    stake_eur = config["base_stake"] * stake_units

    if stake_eur < config["min_stake"]:
        print(f"Stake too small ({stake_eur} < {config['min_stake']}), ignored")
        return None

    cond = re.search(r"No bet under ([0-9.,]+)", message_text)
    min_odds = float(cond.group(1).replace(",", ".")) if cond else 0.0
    if odds + config["odds_tolerance"] < min_odds:
        print(f"Odds too low ({odds} < {min_odds}), ignored")
        return None

    return {
        "sport": sport,
        "player1": player1.strip(),
        "player2": player2.strip(),
        "market_type": market_type,
        "selection": selection.strip(),
        "odds": odds,
        "stake": round(stake_eur, 2),
        "min_odds": min_odds
    }

# --- CHECK LINE STATUS ---
def check_line_and_validate(bet_info):
    try:
        resp = session.get(
            f"{PS3838_API_URL}/odds",
            params={
                "sportId": 33 if bet_info["sport"] == "Tennis" else 29,  # tennis=33, soccer=29
                "oddsFormat": "DECIMAL"
            },
            timeout=10
        )
        data = resp.json()
        # 🔹 Very simplified: loop over events, find a matching one
        for league in data.get("leagues", []):
            for event in league.get("events", []):
                if bet_info["player1"].lower() in event.get("home", "").lower() or \
                   bet_info["player2"].lower() in event.get("away", "").lower():
                    # Example: check main moneyline odds
                    home_odds = event.get("periods", [{}])[0].get("moneyline", {}).get("home")
                    away_odds = event.get("periods", [{}])[0].get("moneyline", {}).get("away")

                    current_odds = home_odds if bet_info["selection"].lower() in event["home"].lower() else away_odds
                    if not current_odds:
                        return False

                    print(f"Checking odds: current={current_odds}, required>={bet_info['min_odds']}")
                    return current_odds + config["odds_tolerance"] >= bet_info["min_odds"]
        return False
    except Exception as e:
        print("Error checking line:", e)
        return False

# --- PLACE BET FUNCTION ---
def place_bet(bet_info):
    if not check_line_and_validate(bet_info):
        print("❌ Bet conditions not met, skipping placement.")
        return None

    url = f"{PS3838_API_URL}/bets/place"
    payload = {
        "sport": bet_info["sport"],
        "event": f"{bet_info['player1']} vs {bet_info['player2']}",
        "marketType": bet_info["market_type"],
        "selection": bet_info["selection"],
        "odds": bet_info["odds"],
        "stake": bet_info["stake"],
        "acceptBetterLine": True
    }

    try:
        response = session.post(url, json=payload, timeout=10)
        result = response.json()
        print(f"✅ Bet placed: {bet_info['selection']} ({bet_info['odds']}) "
              f"stake €{bet_info['stake']} -> {result}")
        return result
    except Exception as e:
        print(f"Error placing bet {bet_info['selection']}: {e}")
        return None


# --- TELEGRAM CLIENT ---
client = TelegramClient("session_ps3838", api_id, api_hash)

@client.on(events.NewMessage(chats=telegram_channel))
async def handler(event):
    message_text = event.message.message.strip()

    # --- Handle Commands ---
    if message_text.startswith("/"):
        parts = message_text.split()
        cmd = parts[0].lower()

        if cmd == "/help":
            help_text = (
                "📖 *Available Commands:*\n\n"
                "/help → Show this help message\n"
                "/stake <value> → Set base stake (minimum 5 EUR)\n"
                "/sports <tennis|football|both> → Enable betting on sports\n"
                "/odds <tolerance> → Set odds tolerance (e.g. 0.05)\n"
                "/showconfig → Show current configuration\n"
            )
            await event.reply(help_text, parse_mode="markdown")

        elif cmd == "/stake" and len(parts) > 1:
            try:
                stake = float(parts[1])
                if stake < config["min_stake"]:
                    await event.reply(f"⚠️ Minimum stake is {config['min_stake']} EUR.")
                else:
                    config["base_stake"] = stake
                    save_config(config)
                    await event.reply(f"✅ Base stake updated to €{stake}")
            except ValueError:
                await event.reply("⚠️ Invalid stake amount.")

        elif cmd == "/sports" and len(parts) > 1:
            choice = parts[1].lower()
            if choice == "tennis":
                config["allow_tennis"] = True
                config["allow_football"] = False
            elif choice == "football":
                config["allow_tennis"] = False
                config["allow_football"] = True
            elif choice == "both":
                config["allow_tennis"] = True
                config["allow_football"] = True
            else:
                await event.reply("⚠️ Use: /sports tennis | football | both")
                return
            save_config(config)
            await event.reply(f"✅ Sports updated: Tennis={config['allow_tennis']} Football={config['allow_football']}")

        elif cmd == "/odds" and len(parts) > 1:
            try:
                tol = float(parts[1])
                config["odds_tolerance"] = tol
                save_config(config)
                await event.reply(f"✅ Odds tolerance updated to {tol}")
            except ValueError:
                await event.reply("⚠️ Invalid number.")

        elif cmd == "/showconfig":
            cfg_text = json.dumps(config, indent=2)
            await event.reply(f"📌 Current Config:\n<pre>{cfg_text}</pre>", parse_mode="html")

        return  # don’t process commands as bets

    # --- Handle Bet Messages ---
    bet_info = parse_message(message_text)
    if bet_info:
        print(f"✅ Bet detected: {bet_info}")
        place_bet(bet_info)
    else:
        print("Message ignored (invalid, odds too low, stake too small, or sport not allowed)")


# --- HELP TEXT ---
HELP_TEXT = (
    "📖 *Available Commands:*\n\n"
    "/help → Show this help message\n"
    "/stake <value> → Set base stake (minimum 5 EUR)\n"
    "/sports <tennis|football|both> → Enable betting on sports\n"
    "/odds <tolerance> → Set odds tolerance (e.g. 0.05)\n"
    "/showconfig → Show current configuration\n"
)

# --- START BOT ---
print("PS3838 bot started, waiting for messages...")
client.start()

# Auto-send help message on startup
async def send_startup_help():
    await client.send_message(telegram_channel, HELP_TEXT, parse_mode="markdown")

client.loop.run_until_complete(send_startup_help())

client.run_until_disconnected()
