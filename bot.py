"""
Skyline GoIP — SIM Number Fetcher Bot
Always-On Mode + Google Sheets Integration
"""

import os
import re
import threading
import json
import requests
import gspread
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
from google.oauth2.service_account import Credentials

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
ALLOWED_ID     = int(os.getenv("ALLOWED_CHAT_ID", "0"))
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")
TOTAL_PORTS    = int(os.getenv("TOTAL_PORTS", "32"))
SHEET_ID       = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS   = os.getenv("GOOGLE_CREDENTIALS")
# ───────────────────────────────────────────────────────────────

TG_API   = f"https://api.telegram.org/bot{BOT_TOKEN}"
lock     = threading.Lock()
collected = []   
listening = True

# ── Google Sheets Setup ────────────────────────────────────────

def get_sheet():
    """Authenticates and returns the Google Sheet worksheet"""
    try:
        if not GOOGLE_CREDS or not SHEET_ID:
            print("[Sheets] Missing credentials or Spreadsheet ID.")
            return None
        
        creds_dict = json.loads(GOOGLE_CREDS)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(SHEET_ID)
        return sh.get_worksheet(0) # Returns the first tab
    except Exception as e:
        print(f"[Sheets] Error connecting: {e}")
        return None

def update_sheet_row(port, number, timestamp):
    """Updates a specific row in Google Sheets (Row = Port + 1 for header)"""
    worksheet = get_sheet()
    if not worksheet:
        return
    
    try:
        # Assuming Row 1 is header (Port, Number, Time)
        # Port 1 goes to Row 2, Port 2 to Row 3, etc.
        if str(port).isdigit():
            row_idx = int(port) + 1
            # Update cells: B (Number) and C (Time)
            worksheet.update_cell(row_idx, 1, f"Port {port}")
            worksheet.update_cell(row_idx, 2, number)
            worksheet.update_cell(row_idx, 3, timestamp)
            print(f"[Sheets] Updated Port {port} in Google Sheet.")
    except Exception as e:
        print(f"[Sheets] Failed to update row: {e}")

# ── Helpers ────────────────────────────────────────────────────

def normalize(number):
    n = re.sub(r'[\s\-]', '', str(number))
    if n.startswith('+92'):
        n = '0' + n[3:]
    elif n.startswith('92') and len(n) == 12:
        n = '0' + n[2:]
    elif len(n) == 10 and n.startswith('3'):
        n = '0' + n
    return n

def send_msg(chat_id, text, parse_mode="Markdown"):
    requests.post(f"{TG_API}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": parse_mode
    })

# ── Commands ────────────────────────────────────────────────────

def cmd_fetch(chat_id):
    send_msg(chat_id, "📡 *Bot is active!* Monitoring SIM numbers and updating Google Sheet.")

def cmd_send(chat_id):
    with lock:
        mapping = {e['port']: e['number'] for e in collected}
    lines = [mapping.get(str(p), "") for p in range(1, TOTAL_PORTS + 1)]
    full = "\n".join(lines)
    for i in range(0, len(full), 4000):
        send_msg(chat_id, f"`{full[i:i+4000]}`")
    send_msg(chat_id, f"✅ Collected: *{len(collected)}/{TOTAL_PORTS}*")

def cmd_status(chat_id):
    with lock:
        data = list(collected)
    if not data:
        send_msg(chat_id, "🔄 Bot is *Active*\n📭 No numbers collected yet.")
        return
    log_lines = [f"Port {e['port']} | {e['number']} | {e['time']}" for e in sorted(data, key=lambda x: int(x['port']) if x['port'].isdigit() else 999)]
    log_full = "📅 *Current Log:*\n\n" + "\n".join(log_lines)
    for i in range(0, len(log_full), 4000):
        send_msg(chat_id, f"`{log_full[i:i+4000]}`")

def cmd_clear(chat_id):
    with lock:
        collected.clear()
    send_msg(chat_id, "🗑 *Memory cleared!* Note: This does not clear your Google Sheet.")

# ── Receiver ──────────────────────────────────────────────────

@app.route("/sms", methods=["GET", "POST"])
def receive_sms():
    if not listening:
        return jsonify(ok=True)

    data = request.args if request.method == "GET" else (request.form or request.args)
    port = (data.get("port") or data.get("line") or data.get("channel") or "?")
    receiver_num = data.get("receiver") or data.get("to") or data.get("dest")
    text = (data.get("text") or data.get("msg") or data.get("message") or "")

    if not receiver_num and not text:
        try:
            body = request.get_json(force=True) or {}
            port = body.get("port", port)
            receiver_num = body.get("receiver") or body.get("to") or body.get("dest")
            text = body.get("text") or body.get("msg") or body.get("message") or ""
        except Exception:
            pass

    number = None
    if receiver_num and any(char.isdigit() for char in str(receiver_num)):
        potential = normalize(str(receiver_num))
        if len(potential) == 11 and potential.startswith("03"):
            number = potential

    if not number and text:
        match = re.search(r'(03\d{9})', text)
        if match: number = match.group(1)

    if number:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with lock:
            existing = [e["number"] for e in collected]
            if number not in existing:
                collected.append({"port": str(port), "number": number, "time": now})
                # Update the Google Sheet in a background thread to keep bot fast
                threading.Thread(target=update_sheet_row, args=(port, number, now)).start()
            else:
                for item in collected:
                    if item["number"] == number:
                        item["port"] = str(port)
                        item["time"] = now
                        threading.Thread(target=update_sheet_row, args=(port, number, now)).start()
                        
    return jsonify(ok=True)

# ── Telegram Webhook ────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data or "message" not in data:
        return jsonify(ok=True)
    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip().lower()
    if ALLOWED_ID and chat_id != ALLOWED_ID:
        return jsonify(ok=True)

    if   "/fetch" in text:  cmd_fetch(chat_id)
    elif "/send" in text:   cmd_send(chat_id)
    elif "/status" in text: cmd_status(chat_id)
    elif "/clear" in text:  cmd_clear(chat_id)
    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def index():
    return "✅ SIM Bot Running with Google Sheets Auto-Export."

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
