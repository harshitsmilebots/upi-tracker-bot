import os
import json
import re
import time
import logging
from datetime import datetime, timezone
import requests

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]
DATA_FILE = "transactions.json"
LIMIT     = 100_000
WINDOW    = 24 * 3600  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

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
            "account": acct_match.group(1),   # "0353" or "3826"
            "ts":      time.time()
        }
    return None

# ── Calculations ──────────────────────────────────────────────────────────────
def calc(txns, account):
    relevant = [t for t in txns if t["account"] == account]
    used     = sum(t["amount"] for t in relevant)
    avail    = max(0, LIMIT - used)
    # oldest transaction → when it releases
    if relevant:
        oldest_ts   = min(t["ts"] for t in relevant)
        release_in  = (oldest_ts + WINDOW) - time.time()
        release_in  = max(0, release_in)
        oldest_amt  = next(t["amount"] for t in relevant if t["ts"] == oldest_ts)
    else:
        release_in = 0
        oldest_amt = 0
    return used, avail, release_in, oldest_amt

def fmt_inr(n):
    # Indian number formatting: 1,00,000
    s = str(int(n))
    if len(s) <= 3:
        return "₹" + s
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + "," + result
        s = s[:-2]
    return "₹" + result

def fmt_time(seconds):
    if seconds <= 0:
        return "now"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h == 0:
        return f"{m}m"
    return f"{h}h {m}m"

def status_bar(used, limit=LIMIT):
    pct   = min(used / limit, 1.0)
    filled = int(pct * 10)
    bar   = "█" * filled + "░" * (10 - filled)
    if pct >= 0.95:
        emoji = "🔴"
    elif pct >= 0.70:
        emoji = "🟡"
    else:
        emoji = "🟢"
    return f"{emoji} [{bar}] {int(pct*100)}%"

def build_status_message(txns, trigger_account=None, trigger_amount=None):
    u353,  a353,  r353,  o353  = calc(txns, "0353")
    u3826, a3826, r3826, o3826 = calc(txns, "3826")

    lines = []

    if trigger_account and trigger_amount:
        lines.append(f"💳 *{fmt_inr(trigger_amount)} debited · ••{trigger_account}*\n")

    # ••0353
    lines.append(f"*••0353*")
    lines.append(status_bar(u353))
    lines.append(f"Used:      {fmt_inr(u353)}")
    lines.append(f"Available: {fmt_inr(a353)}")
    if u353 > 0 and r353 > 0:
        lines.append(f"Next release: {fmt_time(r353)}  ({fmt_inr(o353)} frees up)")
    else:
        lines.append(f"Next release: —")

    lines.append("")

    # ••3826
    lines.append(f"*••3826*")
    lines.append(status_bar(u3826))
    lines.append(f"Used:      {fmt_inr(u3826)}")
    lines.append(f"Available: {fmt_inr(a3826)}")
    if u3826 > 0 and r3826 > 0:
        lines.append(f"Next release: {fmt_time(r3826)}  ({fmt_inr(o3826)} frees up)")
    else:
        lines.append(f"Next release: —")

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines.append(f"\n_Updated {ts}_")

    return "\n".join(lines)

# ── Telegram API ──────────────────────────────────────────────────────────────
BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send(text, parse_mode="Markdown"):
    requests.post(f"{BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode
    })

def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    r = requests.get(f"{BASE}/getUpdates", params=params, timeout=35)
    return r.json().get("result", [])

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("Bot starting…")
    send("🤖 UPI Tracker bot is online. Send /status anytime.")
    offset = None

    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                sender = str(msg.get("chat", {}).get("id", ""))

                # Only respond to your own chat
                if sender != CHAT_ID:
                    continue

                if text == "/status":
                    txns = prune(load_txns())
                    save_txns(txns)
                    send(build_status_message(txns))

                elif text.startswith("/reset"):
                    # /reset 353  or  /reset 3826  or  /reset all
                    parts = text.split()
                    txns  = prune(load_txns())
                    if len(parts) > 1 and parts[1] in ("353", "0353"):
                        txns = [t for t in txns if t["account"] != "0353"]
                        save_txns(txns)
                        send("✅ Transactions cleared for ••0353")
                    elif len(parts) > 1 and parts[1] == "3826":
                        txns = [t for t in txns if t["account"] != "3826"]
                        save_txns(txns)
                        send("✅ Transactions cleared for ••3826")
                    elif len(parts) > 1 and parts[1] == "all":
                        save_txns([])
                        send("✅ All transactions cleared")
                    else:
                        send("Usage: /reset 353 · /reset 3826 · /reset all")

                elif "Sent Rs." in text and "Kotak Bank AC X" in text:
                    # SMS forwarded from Shortcut
                    txn = parse_sms(text)
                    if txn:
                        txns = prune(load_txns())
                        txns.append(txn)
                        save_txns(txns)
                        send(build_status_message(txns, txn["account"], txn["amount"]))
                    else:
                        send("⚠️ Couldn't parse that SMS. Format unexpected.")

        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
