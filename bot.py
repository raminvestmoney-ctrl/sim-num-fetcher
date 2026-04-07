"""
Skyline GoIP — SIM Number Fetcher Bot
══════════════════════════════════════
How to use:
  1. Send /fetch → bot starts listening
  2. Send MNP to correct shortcode per port in modem panel
  3. Carrier replies → bot collects numbers
  4. Send /send → get clean 32-line list (missing ports shown)
  5. Send /clear to reset
"""

import os
import re
import json
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
ALLOWED_ID     = int(os.getenv("ALLOWED_CHAT_ID", "0"))
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")
TOTAL_PORTS    = int(os.getenv("TOTAL_PORTS", "32"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# ── Google Sheets client ────────────────────────────────────────
_sheets_client = None
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    _raw_creds = os.getenv("GOOGLE_CREDENTIALS", "")
    if _raw_creds and SPREADSHEET_ID:
        _creds_info = json.loads(_raw_creds)
        _creds = service_account.Credentials.from_service_account_info(
            _creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_client = build("sheets", "v4", credentials=_creds, cache_discovery=False)
        print("[Sheets] ✅ Google Sheets client initialised")
    else:
        print("[Sheets] ⚠️  GOOGLE_CREDENTIALS or SPREADSHEET_ID not set — sheet sync disabled")
except Exception as _e:
    print(f"[Sheets] ❌ Failed to initialise Google Sheets client: {_e}")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
lock = threading.Lock()

collected = []      # [{ "port": "1", "number": "03xxxxxxxxx" }]
listening = False

# ── Carrier SMS reply patterns ──────────────────────────────────
CARRIER_PATTERNS = [
    r'(?:your\s+(?:mobile\s+)?(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    r'(?:your\s+jazz\s+(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    r'(?:your\s+(?:zong\s+)?(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    r'(?:aapka\s+(?:telenor\s+)?number\s+)(\+?92\d{10}|0\d{10})',
    r'(\+92\d{10})',
    r'(92\d{10})',
    r'(0[3]\d{9})',
]

# ── Normalize number ────────────────────────────────────────────
def normalize(number):
    number = re.sub(r'[\s\-]', '', number)
    if number.startswith('+92'):
        number = '0' + number[3:]
    elif number.startswith('92') and len(number) == 12:
        number = '0' + number[2:]
    return number

def extract_number(text):
    for pattern in CARRIER_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize(match.group(1))
    return None

# ── Telegram ────────────────────────────────────────────────────
def send_msg(chat_id, text, parse_mode="Markdown"):
    requests.post(f"{TG_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    })

def set_commands():
    requests.post(f"{TG_API}/setMyCommands", json={"commands": [
        {"command": "fetch",  "description": "📡 Start collecting SIM numbers"},
        {"command": "send",   "description": "📤 Send 32-line list"},
        {"command": "status", "description": "ℹ️ Check progress"},
        {"command": "clear",  "description": "🗑 Clear list and reset"},
    ]})

def set_webhook():
    if WEBHOOK_URL:
        r = requests.post(f"{TG_API}/setWebhook", json={"url": f"{WEBHOOK_URL}/webhook"})
        print(f"[Webhook] {r.json()}")

# ── Commands ────────────────────────────────────────────────────
def cmd_fetch(chat_id):
    global listening
    with lock:
        listening = True
    send_msg(chat_id,
        "✅ *Listening for SMS replies!*\n\n"
        "Now send MNP to correct shortcode per port:\n"
        "• Ufone → `667`\n"
        "• Jazz → `7000`\n"
        "• Zong → `310`\n"
        "• Telenor → `7421`\n\n"
        "Send /status to check progress."
    )

def cmd_send(chat_id):
    with lock:
        data = list(collected)

    # Create map: port → number
    port_map = {item["port"]: item["number"] for item in data}

    # Build exactly TOTAL_PORTS lines
    lines = []
    for p in range(1, TOTAL_PORTS + 1):
        port_str = str(p)
        number = port_map.get(port_str, "missing")
        lines.append(number)

    full = "\n".join(lines)

    # Send the clean list
    for i in range(0, len(full), 4000):
        send_msg(chat_id, f"`{full[i:i+4000]}`")

    collected_count = len(data)
    send_msg(chat_id,
        f"✅ *{collected_count}* / *{TOTAL_PORTS}* ports loaded\n"
        f"📤 Full 32-line list sent above (missing ports shown as `missing`)"
    )

def cmd_status(chat_id):
    with lock:
        count = len(collected)
        state = listening
    send_msg(chat_id,
        f"🔄 Listening: *{'Yes' if state else 'No'}*\n"
        f"📱 Collected: *{count}* / *{TOTAL_PORTS}* ports\n\n"
        f"Send /send to get the full list."
    )

def cmd_clear(chat_id):
    global listening
    with lock:
        collected.clear()
        listening = False
    send_msg(chat_id, "🗑 Cleared! Send /fetch to start fresh.")

# ── Google Sheets helper ────────────────────────────────────────
def append_to_sheet(port, number):
    """Append a [port, number, timestamp] row to the configured Google Sheet."""
    if not _sheets_client or not SPREADSHEET_ID:
        return
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = {"values": [[str(port), number, timestamp]]}
        _sheets_client.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        print(f"[Sheets] ✅ Appended row → Port {port} | {number} | {timestamp}")
    except Exception as e:
        print(f"[Sheets] ❌ Failed to append row: {e}")

# ── SMS Receiver ────────────────────────────────────────────────
@app.route("/sms", methods=["GET", "POST"])
def receive_sms():
    if not listening:
        return jsonify(ok=True)

    data = request.args if request.method == "GET" else (request.form or request.args)

    port = (data.get("port") or data.get("line") or data.get("channel") or "?")
    text = (data.get("text") or data.get("msg") or data.get("message") or data.get("sms") or "")

    # Try JSON body
    if not text:
        try:
            body = request.get_json(force=True) or {}
            port = body.get("port", port)
            text = body.get("text") or body.get("msg") or body.get("message") or ""
        except Exception:
            pass

    print(f"[SMS] Port={port} | Text={text}")

    number = extract_number(text)
    if not number:
        print(f"[SMS] No number found in: {text}")
        return jsonify(ok=True)

    new_entry = False
    with lock:
        existing = [e["number"] for e in collected]
        if number not in existing:
            collected.append({"port": str(port), "number": number})
            print(f"[SMS] ✅ Port {port} → {number} (total: {len(collected)})")
            new_entry = True
        else:
            print(f"[SMS] ⚠️ Duplicate skipped: {number}")

    if new_entry:
        append_to_sheet(port, number)

    return jsonify(ok=True)

# ── Telegram Webhook ────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data or "message" not in data:
        return jsonify(ok=True)

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if ALLOWED_ID and chat_id != ALLOWED_ID:
        send_msg(chat_id, "⛔ Unauthorized.")
        return jsonify(ok=True)

    cmd = text.split()[0].lower().lstrip("/").split("@")[0]

    if   cmd == "fetch":  cmd_fetch(chat_id)
    elif cmd == "send":   cmd_send(chat_id)
    elif cmd == "status": cmd_status(chat_id)
    elif cmd == "clear":  cmd_clear(chat_id)
    else:
        send_msg(chat_id,
            "/fetch — Start listening\n"
            "/send — Get 32-line list\n"
            "/status — Check progress\n"
            "/clear — Reset"
        )

    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def index():
    return "✅ SIM Bot running."

# ── Startup ─────────────────────────────────────────────────────
if __name__ == "__main__":
    set_webhook()
    set_commands()
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
