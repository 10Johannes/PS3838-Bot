import re
import os
import json
import requests
import uuid
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

PS3838_API_URL = os.getenv("PS3838_API_URL", "https://api.ps3838.com")
PS3838_USERNAME = os.getenv("PS3838_USERNAME", "")
PS3838_PASSWORD = os.getenv("PS3838_PASSWORD", "")

session = requests.Session()
session.auth = (PS3838_USERNAME, PS3838_PASSWORD)

# --- HELPER: Log messages to console & Telegram ---
async def log_message(msg: str):
    print(msg)  # still log to console
    try:
        await client.send_message(telegram_channel, msg)
    except Exception as e:
        print(f"⚠️ Failed to send log to Telegram: {e}")

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

    # Match players
    match = re.search(r"(.+)\s+vs\s+(.+)", message_text)
    if not match:
        return None
    home, away = match.groups()

    # Extract the title line (the one immediately after the match)
    lines = message_text.splitlines()
    title = None
    for i, line in enumerate(lines):
        if re.search(r"(.+)\s+vs\s+(.+)", line):
            if i + 1 < len(lines):
                title_candidate = lines[i + 1].strip()
                # Make sure it looks like a title, not a date
                if not re.search(r"\d{1,2}:\d{2}", title_candidate):
                    title = title_candidate
            break

    # Extract bet info (supports ML and HDP with handicap)
    bet = re.search(
        r"(ML Match|HDP Match)\s*:\s*(.+?)(?:\s+([+-]?\d+(?:\.\d+)?))?\s*@\s*([0-9.,]+)\s*\(([0-9.]+)\s*U\)",
        message_text
    )
    if not bet:
        return None
    market_type, selection, handicap, odds, stake_units = bet.groups()
    odds = float(odds.replace(",", "."))
    stake_units = float(stake_units)
    stake_eur = config["base_stake"] * stake_units
    handicap = float(handicap) if handicap else None

    if stake_eur < config["min_stake"]:
        print(f"Stake too small ({stake_eur} < {config['min_stake']}), ignored")
        return None

    # Minimum odds condition
    cond = re.search(r"No bet under ([0-9.,]+)", message_text)
    min_odds = float(cond.group(1).replace(",", ".")) if cond else 0.0
    if odds + config["odds_tolerance"] < min_odds:
        print(f"Odds too low ({odds} < {min_odds}), ignored")
        return None
    
    # Temporary skip HDP matches
    if market_type == "HDP Match":
        print("HDP Match is not yet available.")
        return None

    return {
        "uuid": str(uuid.uuid4()),
        "sport": sport,
        "sportId": 33 if sport == "Tennis" else 29,
        "home": home.strip(),
        "away": away.strip(),
        "title": title,
        "market_type": market_type,
        "selection": selection.strip(),
        "selection_type": "home" if home.strip() == selection.strip() else "away",
        "handicap": handicap,
        "odds": odds,
        "stake": round(stake_eur, 2),
        "min_odds": min_odds
    }



# --- CHECK LINE STATUS ---
async def check_line_and_validate(bet_info):
    try:
        # --- Step 1: Get fixtures to find eventId ---
        fixtures_resp = session.get(
            f"{PS3838_API_URL}/v3/fixtures",
            params={"sportId": bet_info["sportId"]},
            timeout=10
        )

        if fixtures_resp.status_code != 200 or not fixtures_resp.text.strip():
            await log_message(f"⚠️ Fixtures API error: {fixtures_resp.status_code}, body={fixtures_resp.text[:200]}")
            return False

        fixtures_data = fixtures_resp.json()

                # Save odds response for debugging
        f_timestamp = "test"
        f_debug_file = f"debug_fixtures_{f_timestamp}.json"
        with open(f_debug_file, "w", encoding="utf-8") as f:
            json.dump(fixtures_data, f, indent=2, ensure_ascii=False)

        await log_message(f"📂 Fixtures response saved to {f_debug_file}")

        event_id = None
        for league in fixtures_data.get("league", []):
            league_name = league.get("name", "").strip()
            if league_name.lower() != bet_info["title"].lower():
                continue

            for event in league.get("events", []):
                home = event.get("home", "").strip()
                away = event.get("away", "").strip()
                parent_id = event.get("parentId", 0)  # default 0 if missing

                if parent_id == 0 and \
                home.lower() == bet_info["home"].lower() and \
                away.lower() == bet_info["away"].lower():
                    event_id = event.get("id")
                    bet_info["eventId"] = event_id
                    print(f"✅ Found event in fixtures! ID = {event_id}")
                    break
            if event_id:
                break


        if not event_id:
            await log_message("⚠️ No matching event found in fixtures")
            return False

        # --- Step 2: Get odds for that eventId ---
        odds_resp = session.get(
            f"{PS3838_API_URL}/v3/odds",
            params={"sportId": bet_info["sportId"]},
            timeout=10
        )

        if odds_resp.status_code != 200 or not odds_resp.text.strip():
            await log_message(f"⚠️ Odds API error: {odds_resp.status_code}, body={odds_resp.text[:200]}")
            return False

        odds_data = odds_resp.json()

        # Save odds response for debugging
        timestamp = "test"
        debug_file = f"debug_odds_{timestamp}.json"
        with open(debug_file, "w", encoding="utf-8") as f:
            json.dump(odds_data, f, indent=2, ensure_ascii=False)

        await log_message(f"📂 Odds response saved to {debug_file}")

        # --- Step 3: Find the same eventId in odds ---
        for league in odds_data.get("leagues", []):
            for event in league.get("events", []):
                if event.get("id") == event_id:
                    periods = event.get("periods", [])
                    if not periods:
                        await log_message(f"⚠️ No periods found for event {event_id}")
                        return False

                    p = periods[0]
                    bet_info["lineId"] = p.get("lineId")
                    bet_info["cutoff"] = p.get("cutoff")
                    bet_info["spreads"] = p.get("spreads", [])
                    bet_info["moneyline"] = p.get("moneyline", {})

                    print(f"✅ Found odds for event {event_id}: Line={bet_info['lineId']}")
                    return True

        await log_message(f"⚠️ Event {event_id} not found in odds response")
        return False

    except Exception as e:
        await log_message(f"Error checking line: {e}")
        return False



# --- PLACE BET FUNCTION ---
async def place_bet(bet_info):
    if not await check_line_and_validate(bet_info):
        await log_message("❌ Bet conditions not met, skipping placement.")
        return None

    url = f"{PS3838_API_URL}/v2/bets/place"
    payload = {
        "oddsFormat": "DECIMAL",
        "uniqueRequestId": bet_info["uuid"],
        "acceptBetterLine": True,
        "stake": bet_info["stake"],
        "winRiskStake": "RISK",
        "lineId": bet_info["lineId"],
        # "altLineId": ??? if bet_info["market_type"] == "ML Match" else ???,
        "altLineId": None,
        "pitcher1MustStart": True,
        "pitcher2MustStart": True,
        "fillType": "NORMAL",
        "sportId": bet_info["sportId"],
        "eventId": bet_info["eventId"],
        "periodNumber": 0,
        "betType": "MONEYLINE" if bet_info["market_type"] == "ML Match" else "SPREAD",
        "team": "TEAM1" if bet_info["selection_type"] == "home" else "TEAM2",
        "side": True,
        "handicap": None if bet_info["market_type"] == "ML Match" else bet_info["handicap"]
    }

    try:
        response = session.post(url, json=payload, timeout=10)
        result = response.json()
        await log_message(
            f"✅ Bet placed: {bet_info['selection']} ({bet_info['odds']}) "
            f"stake €{bet_info['stake']} -> {result}"
        )
        return result
    except Exception as e:
        await log_message(f"Error placing bet {bet_info['selection']}: {e}")
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
        await log_message(f"✅ Bet detected: {bet_info}")
        await place_bet(bet_info)
    else:
        await log_message("Message ignored (invalid, odds too low, stake too small, or sport not allowed)")


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
    await log_message("🤖 Bot started and ready!")
    await client.send_message(telegram_channel, HELP_TEXT, parse_mode="markdown")

client.loop.run_until_complete(send_startup_help())

client.run_until_disconnected()
