import os
import json
import re
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request
import requests

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"].strip()
CHAT_ID          = os.environ["CHAT_ID"].strip()
WEBHOOK_URL      = os.environ["WEBHOOK_URL"].strip()
PORT             = int(os.environ.get("PORT", 8080))
DATA_FILE        = "transactions.json"
LIMIT            = 100_000
WINDOW           = 24 * 3600
TRACKED_ACCOUNTS = {"0353", "3826"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
lock = threading.Lock()

# ── Persistence ───────────────────────────────────────────────────────────────
def load_txns():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []

def save_txns(txns):
    with open(DATA_FILE, "w") as f:
        json.dump(txns, f)

def prune(txns):
    cutoff = time.time() - WINDOW
    return [t for t in txns if t["ts"] > cutoff]

# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_sms(text):
    amt_match  = re.search(r"Rs\.(\d+(?:\.\d+)?)", text)
    acct_match = re.search(r"AC X(\d+)", text)
    if amt_match and acct_match:
        return {
            "amount":  int(float(amt_match.group(1))),
            "account": acct_match.group(1),
            "ts":      time.time()
        }
    return None

# ── Calculations ──────────────────────────────────────────────────────────────
def calc(txns, account):
    relevant = [t for t in txns if t["account"] == account]
    used     = sum(t["amount"] for t in relevant)
    avail    = max(0, LIMIT - used)
    if relevant:
        oldest_ts  = min(t["ts"] for t in relevant)
        release_at = oldest_ts + WINDOW
        oldest_amt = next(t["amount"] for t in relevant if t["ts"] == oldest_ts)
    else:
        release_at = None
        oldest_amt = 0
    return used, avail, release_at, oldest_amt

def fmt_inr(n):
    s = str(int(n))
    if len(s) <= 3:
        return "₹" + s
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + "," + result
        s = s[:-2]
    return "₹" + result

def fmt_release(release_at):
    """Returns date+time string in IST."""
    if not release_at:
        return "—"
    # IST = UTC+5:30
    ist = datetime.fromtimestamp(release_at, tz=timezone(timedelta(hours=5, minutes=30)))
    return ist.strftime("%-d %b, %-I:%M %p")

def status_bar(used):
    pct    = min(used / LIMIT, 1.0)
    filled = int(pct * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    emoji  = "🔴" if pct >= 0.95 else "🟡" if pct >= 0.70 else "🟢"
    return f"{emoji} [{bar}] {int(pct*100)}%"

def build_status_message(txns, trigger_account=None, trigger_amount=None):
    u353,  a353,  r353,  o353  = calc(txns, "0353")
    u3826, a3826, r3826, o3826 = calc(txns, "3826")

    lines = []

    # ── Header — visible in lock screen preview ───────────────────────────────
    if trigger_account and trigger_amount:
        lines.append(f"💳 {fmt_inr(trigger_amount)} debited · ••{trigger_account}")
    lines.append(f"••0353: {fmt_inr(a353)} free")
    lines.append(f"••3826: {fmt_inr(a3826)} free")

    # ── Detail ────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"*••0353*  {status_bar(u353)}")
    lines.append(f"Used: {fmt_inr(u353)}  Available: {fmt_inr(a353)}")
    if u353 > 0 and r353:
        lines.append(f"Releases: {fmt_release(r353)}  ({fmt_inr(o353)} frees up)")
    else:
        lines.append("Releases: —")

    lines.append("")
    lines.append(f"*••3826*  {status_bar(u3826)}")
    lines.append(f"Used: {fmt_inr(u3826)}  Available: {fmt_inr(a3826)}")
    if u3826 > 0 and r3826:
        lines.append(f"Releases: {fmt_release(r3826)}  ({fmt_inr(o3826)} frees up)")
    else:
        lines.append("Releases: —")

    ts = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%-d %b, %-I:%M %p IST")
    lines.append(f"\n_{ts}_")

    return "\n".join(lines)

# ── Telegram API ──────────────────────────────────────────────────────────────
BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send(text, parse_mode="Markdown"):
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode
    })
    log.info(f"send() status={r.status_code} ok={r.json().get('ok')}")

def set_webhook():
    url = f"{WEBHOOK_URL}/webhook"
    r = requests.post(f"{BASE}/setWebhook", json={"url": url})
    log.info(f"setWebhook → {url} response={r.json()}")

# ── Core SMS processing ───────────────────────────────────────────────────────
def process_sms(text):
    log.info(f"process_sms: {text[:80]!r}")
    txn = parse_sms(text)
    if not txn:
        log.warning("Parse failed")
        return False
    if txn["account"] not in TRACKED_ACCOUNTS:
        log.info(f"Ignoring untracked account: {txn['account']}")
        return False
    with lock:
        txns = prune(load_txns())
        txns.append(txn)
        save_txns(txns)
    log.info(f"Saved txn: {txn}")
    send(build_status_message(txns, txn["account"], txn["amount"]))
    return True

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "UPI Tracker running.", 200

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

@app.route("/sms", methods=["POST"])
def sms_endpoint():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    log.info(f"/sms received: {text[:80]!r}")
    if not text:
        return {"ok": False, "error": "no text"}, 400
    ok = process_sms(text)
    return {"ok": ok}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if update:
        msg    = update.get("message", {})
        text   = msg.get("text", "").strip()
        sender = str(msg.get("chat", {}).get("id", ""))
        log.info(f"Webhook: sender={sender!r} text={text[:60]!r}")
        if sender != CHAT_ID:
            return "ok", 200
        if text == "/status":
            with lock:
                txns = prune(load_txns())
                save_txns(txns)
            send(build_status_message(txns))
        elif text.startswith("/reset"):
            parts = text.split()
            with lock:
                txns = prune(load_txns())
                if len(parts) > 1 and parts[1] in ("353", "0353"):
                    txns = [t for t in txns if t["account"] != "0353"]
                    save_txns(txns)
                    send("✅ Cleared ••0353")
                elif len(parts) > 1 and parts[1] == "3826":
                    txns = [t for t in txns if t["account"] != "3826"]
                    save_txns(txns)
                    send("✅ Cleared ••3826")
                elif len(parts) > 1 and parts[1] == "all":
                    save_txns([])
                    send("✅ All cleared")
                else:
                    send("Usage: /reset 353 · /reset 3826 · /reset all")
    return "ok", 200

# ── Self-ping to keep Railway alive ──────────────────────────────────────────
def self_ping():
    while True:
        time.sleep(60)
        try:
            requests.get(f"{WEBHOOK_URL}/ping", timeout=10)
            log.info("self-ping ok")
        except Exception as e:
            log.warning(f"self-ping failed: {e}")

# ── Startup ───────────────────────────────────────────────────────────────────
log.info(f"Starting on port {PORT}")
set_webhook()
threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
