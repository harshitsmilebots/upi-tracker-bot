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
TRACKED_ACCOUNTS = {"0353", "3826", "1183"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
lock = threading.Lock()

IST = timezone(timedelta(hours=5, minutes=30))

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
    if "UPI Ref" not in text:
        log.info("Skipping non-UPI message")
        return None
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
    if not release_at:
        return "—"
    ist = datetime.fromtimestamp(release_at, tz=IST)
    return ist.strftime("%-d %b, %-I:%M %p")

def fmt_ts(ts):
    ist = datetime.fromtimestamp(ts, tz=IST)
    return ist.strftime("%-I:%M %p")

def status_bar(used):
    pct    = min(used / LIMIT, 1.0)
    filled = int(pct * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    emoji  = "🔴" if pct >= 0.95 else "🟡" if pct >= 0.70 else "🟢"
    return f"{emoji} [{bar}] {int(pct*100)}%"

def build_status_message(txns, trigger_account=None, trigger_amount=None):
    u353,  a353,  r353,  o353  = calc(txns, "0353")
    u3826, a3826, r3826, o3826 = calc(txns, "3826")
    u1183, a1183, r1183, o1183 = calc(txns, "1183")

    lines = []

    # Header — visible on lock screen
    if trigger_account and trigger_amount:
        lines.append(f"💳 {fmt_inr(trigger_amount)} debited · ••{trigger_account}")
    lines.append(f"••0353: {fmt_inr(a353)} free")
    lines.append(f"••3826: {fmt_inr(a3826)} free")
    lines.append(f"••1183: {fmt_inr(a1183)} free")
    lines.append("")

    # ••0353 detail
    lines.append(f"*••0353*  {status_bar(u353)}")
    lines.append(f"Used: {fmt_inr(u353)}  Available: {fmt_inr(a353)}")
    # Transactions
    t353 = sorted([t for t in txns if t["account"] == "0353"], key=lambda t: t["ts"])
    if t353:
        for t in t353:
            rel = fmt_release(t["ts"] + WINDOW)
            lines.append(f"{fmt_inr(t['amount'])}  → {rel}")
    else:
        lines.append("No transactions")

    lines.append("")

    # ••3826 detail
    lines.append(f"*••3826*  {status_bar(u3826)}")
    lines.append(f"Used: {fmt_inr(u3826)}  Available: {fmt_inr(a3826)}")
    # Transactions
    t3826 = sorted([t for t in txns if t["account"] == "3826"], key=lambda t: t["ts"])
    if t3826:
        for t in t3826:
            rel = fmt_release(t["ts"] + WINDOW)
            lines.append(f"{fmt_inr(t['amount'])}  → {rel}")
    else:
        lines.append("No transactions")

    lines.append("")

    # ••1183 detail
    lines.append(f"*••1183*  {status_bar(u1183)}")
    lines.append(f"Used: {fmt_inr(u1183)}  Available: {fmt_inr(a1183)}")
    t1183 = sorted([t for t in txns if t["account"] == "1183"], key=lambda t: t["ts"])
    if t1183:
        for t in t1183:
            lines.append(f"{fmt_inr(t['amount'])}  → {fmt_release(t['ts'] + WINDOW)}")
    else:
        lines.append("No transactions")

    now_str = datetime.now(IST).strftime("%-d %b, %-I:%M %p IST")
    all_txns = [t for t in txns if t["account"] in TRACKED_ACCOUNTS]
    if all_txns:
        last_ts = max(t["ts"] for t in all_txns)
        last_str = datetime.fromtimestamp(last_ts, tz=IST).strftime("%-d %b, %-I:%M %p IST")
        lines.append(f"\n_Last txn: {last_str}_")
    lines.append(f"_{now_str}_")

    return "\n".join(lines)

# ── Sync parser — reads a previously sent status message ─────────────────────
def parse_sync_message(text):
    """
    Parses a previously sent status message to restore transactions.
    Looks for lines like:  9:07 AM  ₹24,895  → frees 27 May, 9:07 AM
    Reconstructs ts from the release time (release_ts - 24h = original ts).
    """
    txns = []
    current_account = None

    for line in text.split("\n"):
        # Detect which account block we're in
        if "••0353" in line and "free" not in line:
            current_account = "0353"
        elif "••3826" in line and "free" not in line:
            current_account = "3826"
        elif "••1183" in line and "free" not in line:
            current_account = "1183"

        # Match transaction lines:  9:07 AM  ₹24,895  → frees 27 May, 9:07 AM
        m = re.search(r"→ (\d+ \w+, \d+:\d+ [AP]M)", line)
        amt_m = re.search(r"₹([\d,]+)", line)
        if m and amt_m and current_account:
            try:
                release_str = m.group(1)
                # Parse release time — add current year
                year = datetime.now(IST).year
                release_dt = datetime.strptime(f"{release_str} {year}", "%d %b, %I:%M %p %Y")
                release_dt = release_dt.replace(tzinfo=IST)
                # If release is in the past relative to now+24h window, it might be next year — unlikely
                original_ts = release_dt.timestamp() - WINDOW
                amount = int(amt_m.group(1).replace(",", ""))
                # Only include if still within 24h window
                if original_ts > time.time() - WINDOW:
                    txns.append({
                        "account": current_account,
                        "amount":  amount,
                        "ts":      original_ts
                    })
            except Exception as e:
                log.warning(f"Could not parse sync line: {line!r} — {e}")

    return txns

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

@app.route("/backfill", methods=["POST"])
def backfill():
    """Overwrites entire database."""
    data = request.get_json(silent=True) or {}
    txns = data.get("transactions", [])
    if not txns:
        return {"ok": False, "error": "no transactions"}, 400
    with lock:
        save_txns(txns)
    log.info(f"Backfilled {len(txns)} transactions")
    pruned = prune(txns)
    send(build_status_message(pruned))
    return {"ok": True, "count": len(txns)}, 200

@app.route("/merge", methods=["POST"])
def merge():
    """Appends transactions to existing database without wiping."""
    data = request.get_json(silent=True) or {}
    new_txns = data.get("transactions", [])
    if not new_txns:
        return {"ok": False, "error": "no transactions"}, 400
    with lock:
        existing = prune(load_txns())
        merged = existing + new_txns
        save_txns(merged)
    log.info(f"Merged {len(new_txns)} transactions, total now {len(merged)}")
    send(build_status_message(prune(merged)))
    return {"ok": True, "added": len(new_txns), "total": len(merged)}, 200

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
                elif len(parts) > 1 and parts[1] == "1183":
                    txns = [t for t in txns if t["account"] != "1183"]
                    save_txns(txns)
                    send("✅ Cleared ••1183")
                elif len(parts) > 1 and parts[1] == "all":
                    save_txns([])
                    send("✅ All cleared")
                else:
                    send("Usage: /reset 353 · /reset 3826 · /reset all")

        elif text.startswith("/remove"):
            parts = text.split()
            if len(parts) == 3:
                try:
                    amt  = int(parts[1])
                    acct = "0353" if parts[2] in ("353", "0353") else "3826"
                    with lock:
                        txns = prune(load_txns())
                        matches = [t for t in txns if t["account"] == acct and t["amount"] == amt]
                        if matches:
                            latest = max(matches, key=lambda t: t["ts"])
                            txns.remove(latest)
                            save_txns(txns)
                            send(f"✅ Removed {fmt_inr(amt)} from ••{acct}\n" + build_status_message(txns))
                        else:
                            send(f"⚠️ No match found for {fmt_inr(amt)} on ••{acct}")
                except:
                    send("Usage: /remove AMOUNT ACCOUNT\nExample: /remove 9873 353")
            else:
                send("Usage: /remove AMOUNT ACCOUNT\nExample: /remove 9873 353")

        elif text == "/sync":
            send("Send me the last correct status message and I'll restore from it.")

        elif "→" in text and ("••0353" in text or "••3826" in text):
            # This is a previously sent status message being forwarded back for sync
            txns = parse_sync_message(text)
            if txns:
                with lock:
                    save_txns(txns)
                log.info(f"Synced {len(txns)} transactions from message")
                send(f"✅ Restored {len(txns)} transactions\n\n" + build_status_message(prune(txns)))
            else:
                send("⚠️ Couldn't parse any transactions from that message.")

        elif "Sent Rs." in text and "Kotak Bank AC X" in text:
            threading.Thread(target=process_sms, args=(text,)).start()

    return "ok", 200

# ── Self-ping ─────────────────────────────────────────────────────────────────
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
