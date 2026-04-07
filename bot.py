"""
Skyline GoIP — SIM Number Fetcher Bot
══════════════════════════════════════
How to use:
  1. Send /fetch in Telegram → bot starts listening
  2. In modem panel, send MNP to correct shortcode per port:
       Ufone   → MNP to 667
       Jazz    → MNP to 7000
       Zong    → MNP to 310
       Telenor → MNP to 7421
  3. Carrier replies come in → modem forwards to this bot
  4. Send /send in Telegram → get full clean list
  5. Send /clear to reset and start fresh

Railway variables needed:
  BOT_TOKEN, ALLOWED_CHAT_ID, WEBHOOK_URL, TOTAL_PORTS
"""

import os
import re
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
ALLOWED_ID  = int(os.getenv("ALLOWED_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
TOTAL_PORTS = int(os.getenv("TOTAL_PORTS", "32"))
# ───────────────────────────────────────────────────────────────

TG_API   = f"https://api.telegram.org/bot{BOT_TOKEN}"
lock     = threading.Lock()
collected = []   # [{ "port": "1", "number": "03xxxxxxxxx" }]
listening = False

# ── Carrier SMS reply patterns ──────────────────────────────────
# Each carrier replies differently — we try all patterns
CARRIER_PATTERNS = [
    # Ufone: "Your Mobile Number is 0333xxxxxxx"
    r'(?:your\s+(?:mobile\s+)?(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    # Jazz: "Your Jazz number is 03xxxxxxxxx"
    r'(?:your\s+jazz\s+(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    # Zong: "Your number is 031xxxxxxxx"
    r'(?:your\s+(?:zong\s+)?(?:number|no\.?)\s+is\s*:?\s*)(\+?92\d{10}|0\d{10})',
    # Telenor: "Aapka number 034xxxxxxxx hai"
    r'(?:aapka\s+(?:telenor\s+)?number\s+)(\+?92\d{10}|0\d{10})',
    # Generic fallback: any Pakistani number in the SMS
    r'(\+92\d{10})',
    r'(92\d{10})',
    r'(0[3]\d{9})',
]

# ── Normalize number ────────────────────────────────────────────

def normalize(number):
    """Convert any format to 0xxxxxxxxxx"""
    number = re.sub(r'[\s\-]', '', str(number))
    if number.startswith('+92'):
        number = '0' + number[3:]
    elif number.startswith('92') and len(number) == 12:
        number = '0' + number[2:]
    elif len(number) == 10 and number.startswith('3'):
        number = '0' + number
    return number

def extract_number(text):
    """Try all carrier patterns to extract number from SMS reply."""
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
        {"command": "send",   "description": "📤 Send full number list"},
        {"command": "status", "description": "ℹ️ Numbers collected so far"},
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
        "✅ *Listening for SIM numbers!*\n\n"
        "Now go to modem panel and trigger an SMS (e.g., MNP check).\n"
        "The bot will automatically grab the SIM number from the metadata.\n\n"
        "Send /status to check progress.\n"
        "Send /send when done."
    )

def cmd_send(chat_id):
    with lock:
        mapping = {e['port']: e['number'] for e in collected}

    lines = []
    # Generate raw list for direct copy-paste (Line 1 = Port 1, etc.)
    for p in range(1, TOTAL_PORTS + 1):
        num = mapping.get(str(p), "missing")
        lines.append(num)

    full = "\n".join(lines)

    # Send in chunks (Telegram limit is 4096 chars)
    for i in range(0, len(full), 4000):
        send_msg(chat_id, f"`{full[i:i+4000]}`")

    with lock:
        count = len(collected)
    send_msg(chat_id, f"✅ Collected: *{count}/{TOTAL_PORTS}*")

def cmd_status(chat_id):
    with lock:
        count = len(collected)
        state = listening
    send_msg(chat_id,
        f"🔄 Listening: *{'Yes' if state else 'No'}*\n"
        f"📱 Collected: *{count}* numbers\n\n"
        f"Send /send to get the list."
    )

def cmd_clear(chat_id):
    global listening
    with lock:
        collected.clear()
        listening = False
    send_msg(chat_id, "🗑 Cleared! Send /fetch to start fresh.")

# ── SMS Receiver ────────────────────────────────────────────────

@app.route("/sms", methods=["GET", "POST"])
def receive_sms():
    if not listening:
        return jsonify(ok=True)

    data = request.args if request.method == "GET" else (request.form or request.args)

    port = (data.get("port") or data.get("line") or
            data.get("channel") or "?")
    text = (data.get("text") or data.get("msg") or
            data.get("message") or data.get("sms") or "")
    
    # NEW: Priority field from your modem logic
    receiver_num = data.get("receiver") or data.get("to") or data.get("dest")

    # Try JSON body payload
    if not text and not receiver_num:
        try:
            body = request.get_json(force=True) or {}
            port = body.get("port", port)
            text = body.get("text") or body.get("msg") or body.get("message") or ""
            receiver_num = body.get("receiver") or body.get("to") or body.get("dest")
        except Exception:
            pass

    print(f"[SMS] Port={port} | Receiver={receiver_num} | Text={text}")

    number = None
    
    # 1. OPTION A: Extract from 'receiver' metadata (Most reliable)
    if receiver_num and any(char.isdigit() for char in str(receiver_num)):
        potential = normalize(str(receiver_num))
        if len(potential) == 11 and potential.startswith("03"):
            number = potential
            print(f"[Match] Found in metadata: {number}")

    # 2. OPTION B: Extract from text (Carrier reply backup)
    if not number:
        number = extract_number(text)
        if number:
            print(f"[Match] Found in text: {number}")

    if not number:
        return jsonify(ok=True)

    with lock:
        # Check if number already in list
        existing = [e["number"] for e in collected]
        if number not in existing:
            collected.append({"port": str(port), "number": number})
            print(f"[Added] Port {port} → {number} (Total: {len(collected)})")
        else:
            # Update port if it changed but number is same
            for item in collected:
                if item["number"] == number:
                    item["port"] = str(port)
            print(f"[Update] Updated port for {number}")

    return jsonify(ok=True)

# ── Telegram Webhook ────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data or "message" not in data:
        return jsonify(ok=True)

    msg     = data["message"]
    chat_id = msg["chat"]["id"]
    text    = msg.get("text", "").strip()

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
            "/send — Get number list\n"
            "/status — Check progress\n"
            "/clear — Reset"
        )

    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def index():
    return "✅ SIM Bot running with Metadata support."

# ── Startup ─────────────────────────────────────────────────────

if __name__ == "__main__":
    set_webhook()
    set_commands()
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
