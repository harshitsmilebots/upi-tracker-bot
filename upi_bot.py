import os
import json
import re
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from flask_cors import CORS
import requests

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"].strip()
CHAT_ID          = os.environ["CHAT_ID"].strip()
WEBHOOK_URL      = os.environ["WEBHOOK_URL"].strip()
PORT             = int(os.environ.get("PORT", 8080))
# DATA_FILE points at the persistent Railway volume when DATA_DIR is set
# (set DATA_DIR=/data as a Railway env var once the volume is mounted at /data).
# Falls back to the working directory if no volume is configured.
DATA_DIR         = os.environ.get("DATA_DIR", "").strip()
DATA_FILE        = os.path.join(DATA_DIR, "transactions.json") if DATA_DIR else "transactions.json"
LIMIT            = 100_000
WINDOW           = 24 * 3600
TRACKED_ACCOUNTS = {"0353", "3826", "1183", "9421"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# Ensure the data directory exists (harmless if it already does)
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
log.info(f"Using data file: {DATA_FILE}")

app = Flask(__name__)
CORS(app)
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
    # ── Kotak format: "Sent Rs.X from Kotak Bank A/c XNNNN ... UPI Ref" ──
    if "UPI Ref" in text and ("Kotak Bank AC X" in text or "Kotak Bank A/c X" in text):
        amt_match  = re.search(r"Rs\.(\d+(?:\.\d+)?)", text)
        acct_match = re.search(r"A/?[Cc] X(\d+)", text)
        if amt_match and acct_match:
            return {
                "amount":  int(float(amt_match.group(1))),
                "account": acct_match.group(1),
                "ts":      time.time()
            }
    # ── SBI format: "Dear UPI user A/C XNNNN debited by X ... Refno" ──
    if "debited by" in text and "Refno" in text and "SBI" in text:
        amt_match  = re.search(r"debited by (\d+(?:\.\d+)?)", text)
        acct_match = re.search(r"A/[Cc] X(\d+)", text)
        if amt_match and acct_match:
            return {
                "amount":  int(float(amt_match.group(1))),
                "account": acct_match.group(1),
                "ts":      time.time()
            }
    log.info("Skipping unrecognized/non-UPI message")
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

def normalize_account(s):
    """Map a user-typed account token to a tracked account id, or None."""
    s = s.lstrip("•").lstrip("xX").strip()
    aliases = {
        "353": "0353", "0353": "0353",
        "3826": "3826",
        "1183": "1183",
        "9421": "9421",
    }
    return aliases.get(s)

def parse_manual_datetime(date_str):
    """
    Parse a user-typed date/time for /add into an epoch timestamp (IST).
    Accepts things like: '8 Oct 10:35am', '8 Oct 10:35', 'Oct 8 10:35am',
    '8 Oct', '10:35am' (today). Returns None if unparseable.
    Empty/blank -> now.
    """
    date_str = date_str.strip()
    if not date_str:
        return time.time()

    now = datetime.now(IST)
    year = now.year
    txt = date_str.lower().replace(",", " ")
    txt = " ".join(txt.split())  # collapse whitespace

    # Separate a trailing time (e.g. "10:35am" or "10:35") from the date part
    time_match = re.search(r"(\d{1,2}):(\d{2})\s*([ap]m)?", txt)
    hour, minute = None, None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm = time_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        txt = txt.replace(time_match.group(0), "").strip()

    # Parse the date part if present
    day, month = None, None
    months = {m.lower(): i for i, m in enumerate(
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
    dm = re.search(r"\b(\d{1,2})\b", txt)
    mm = re.search(r"\b(" + "|".join(months.keys()) + r")", txt)
    if dm:
        day = int(dm.group(1))
    if mm:
        month = months[mm.group(1)]

    # Build the datetime, filling in sensible defaults
    if day is None:
        day = now.day
    if month is None:
        month = now.month
    if hour is None:
        hour, minute = now.hour, now.minute

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=IST)
    except ValueError:
        return None
    # If the constructed date is in the future (e.g. user typed a day that
    # already passed into next month), roll back a month — but keep it simple:
    if dt.timestamp() > time.time() + 300:  # >5 min in the future
        # assume they meant last occurrence; step back a month
        prev_month = month - 1 or 12
        prev_year = year if month != 1 else year - 1
        try:
            dt = datetime(prev_year, prev_month, day, hour, minute, tzinfo=IST)
        except ValueError:
            pass
    return dt.timestamp()

def status_bar(used):
    pct    = min(used / LIMIT, 1.0)
    filled = int(pct * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    emoji  = "🔴" if pct >= 0.95 else "🟡" if pct >= 0.70 else "🟢"
    return f"{emoji} [{bar}] {int(pct*100)}%"

def build_status_message(txns, trigger_account=None, trigger_amount=None):
    # Fixed display order
    order = ["0353", "3826", "1183", "9421"]
    data = {a: calc(txns, a) for a in order}   # a -> (used, avail, release_at, oldest_amt)

    lines = []

    # Debit header (only on a transaction notification)
    if trigger_account and trigger_amount:
        lines.append(f"💳 {fmt_inr(trigger_amount)} debited · ••{trigger_account}")

    # ── Top block: all four, fixed order, "free" or "unused" ──
    lines.append("──────────────")
    for a in order:
        used, avail, _, _ = data[a]
        if used > 0:
            lines.append(f"••{a}: {fmt_inr(avail)} free")
        else:
            lines.append(f"••{a}: unused")
    lines.append("──────────────")

    # ── Detail blocks: only accounts with activity, fixed order ──
    for a in order:
        used, avail, _, _ = data[a]
        acct_txns = sorted([t for t in txns if t["account"] == a], key=lambda t: t["ts"])
        if not acct_txns:
            continue
        pct = min(round(used / LIMIT * 100), 100)
        emoji = "🔴" if pct >= 95 else "🟡" if pct >= 70 else "🟢"
        lines.append("")
        lines.append(f"*••{a}*  {emoji} {pct}%")
        for t in acct_txns:
            lines.append(f"{fmt_inr(t['amount'])}  → {fmt_release(t['ts'] + WINDOW)}")

    # ── Footer ──
    now_str = datetime.now(IST).strftime("%-d %b, %-I:%M %p IST")
    all_txns = [t for t in txns if t["account"] in TRACKED_ACCOUNTS]
    if all_txns:
        last_ts = max(t["ts"] for t in all_txns)
        last_str = datetime.fromtimestamp(last_ts, tz=IST).strftime("%-d %b, %-I:%M %p IST")
        lines.append(f"\n_Last txn: {last_str}_")
    lines.append(f"_{now_str}_")
    lines.append("\nRefresh: /status")

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
        elif "••9421" in line and "free" not in line:
            current_account = "9421"

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

@app.route("/data", methods=["GET"])
def data():
    """Returns live account data for the HTML dashboard."""
    with lock:
        txns = prune(load_txns())
    
    accounts = []
    for acct in ["0353", "3826", "1183", "9421"]:
        relevant = [t for t in txns if t["account"] == acct]
        used     = sum(t["amount"] for t in relevant)
        avail    = max(0, LIMIT - used)
        pct      = round(min(used / LIMIT, 1.0) * 100)
        txn_list = sorted(relevant, key=lambda t: t["ts"])
        accounts.append({
            "account":  acct,
            "used":     used,
            "available": avail,
            "pct":      pct,
            "transactions": [
                {
                    "amount":     t["amount"],
                    "ts":         t["ts"],
                    "releases_at": t["ts"] + WINDOW
                }
                for t in txn_list
            ]
        })
    
    last_ts = max((t["ts"] for t in txns), default=None)
    
    from flask import jsonify
    return jsonify({
        "accounts":    accounts,
        "last_txn_ts": last_ts,
        "server_ts":   time.time()
    })

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

        if text == "/help":
            help_text = (
                "*UPI Tracker — Commands*\n\n"
                "/status — current balances & transactions\n\n"
                "/add — log a missed transaction\n"
                "   `/add ACCOUNT AMOUNT [date time]`\n"
                "   • `/add 0353 21755` → logs it now\n"
                "   • `/add 0353 21755 8 Oct 10:35am` → logs at that time\n\n"
                "/remove — delete one txn\n"
                "   `/remove AMOUNT ACCOUNT` (e.g. `/remove 9873 353`)\n\n"
                "/reset — clear an account\n"
                "   `/reset 353 · 3826 · 1183 · 9421 · all`\n\n"
                "/sync — restore from a pasted status message\n\n"
                "Accounts: 353, 3826, 1183, 9421\n"
                "You can also paste a raw bank SMS to log it manually."
            )
            send(help_text)

        elif text == "/status":
            with lock:
                txns = prune(load_txns())
                save_txns(txns)
            send(build_status_message(txns))

        elif text.startswith("/add"):
            # /add ACCOUNT AMOUNT [date time]
            # e.g. /add 0353 21755            -> logs now
            #      /add 0353 21755 8 Oct 10:35am  -> logs at that time
            parts = text.split()
            if len(parts) < 3:
                send("Usage: /add ACCOUNT AMOUNT [date time]\n"
                     "Example: /add 0353 21755\n"
                     "Example: /add 0353 21755 8 Oct 10:35am")
            else:
                acct = normalize_account(parts[1])
                if not acct:
                    send(f"⚠️ Unknown account '{parts[1]}'. Use 353, 3826, 1183 or 9421.")
                else:
                    try:
                        amt = int(float(parts[2]))
                    except ValueError:
                        send(f"⚠️ '{parts[2]}' isn't a valid amount.")
                        amt = None
                    if amt is not None:
                        date_part = " ".join(parts[3:])
                        ts = parse_manual_datetime(date_part)
                        if ts is None:
                            send(f"⚠️ Couldn't read the date/time '{date_part}'.\n"
                                 "Try: /add 0353 21755 8 Oct 10:35am")
                        else:
                            with lock:
                                txns = prune(load_txns())
                                txns.append({"account": acct, "amount": amt, "ts": ts})
                                save_txns(txns)
                            when = "now" if not date_part else fmt_release(ts)
                            send(f"✅ Added {fmt_inr(amt)} to ••{acct} ({when})\n\n"
                                 + build_status_message(prune(load_txns())))

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
                elif len(parts) > 1 and parts[1] == "9421":
                    txns = [t for t in txns if t["account"] != "9421"]
                    save_txns(txns)
                    send("✅ Cleared ••9421")
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
                    acct = normalize_account(parts[2]) or "0353"
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

        elif ("Sent Rs." in text and ("Kotak Bank AC X" in text or "Kotak Bank A/c X" in text)) or ("debited by" in text and "Refno" in text and "SBI" in text):
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
