import os
import json
import re
import time
import base64
import tempfile
import logging
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, send_file
from flask_cors import CORS
import struct
import requests
from urllib.parse import urlparse
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

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
FASTAG_FILE      = os.path.join(DATA_DIR, "fastag.json") if DATA_DIR else "fastag.json"
FASTAG_LOG_FILE  = os.path.join(DATA_DIR, "fastag_log.json") if DATA_DIR else "fastag_log.json"
DEBITS_LOG_FILE  = os.path.join(DATA_DIR, "debits_log.json") if DATA_DIR else "debits_log.json"
EXPENSES_FILE    = os.path.join(DATA_DIR, "expenses_log.json") if DATA_DIR else "expenses_log.json"
OVERRIDES_FILE   = os.path.join(DATA_DIR, "reimb_overrides.json") if DATA_DIR else "reimb_overrides.json"
LIMIT            = 100_000
WINDOW           = 24 * 3600
TRACKED_ACCOUNTS = {"0353", "3826", "1183", "9421"}

# FASTag alert thresholds
FASTAG_T1        = 10_500   # early warning — "recharge soon"
FASTAG_T2        = 5_000    # danger line — trucks risk being stopped at toll
FASTAG_DANGER    = FASTAG_T2
FASTAG_LOG_DAYS  = 400      # keep ~13 months of toll history
# The blind spot: Harshit checks ~8am; a recharge takes up to ~3h to reflect
# (≈noon). So from late evening to noon the balance only falls — overnight is
# where a silent drop below the danger line stops trucks.
MORNING_HOUR     = 8        # hour he checks balance
RECHARGE_REFLECT = 12       # hour a nightly recharge has reflected by
DASH_TOKEN       = os.environ.get("DASH_TOKEN", "").strip()  # guards read endpoints
FLEET_SIZE       = int(os.environ.get("FLEET_SIZE", "45"))   # total trucks, for utilisation %

# Reimbursement expense categories (served to the dashboard dropdown; edit freely)
REIMB_CATEGORIES = ["Fuel", "Toll", "Repair", "Parts", "Tyres",
                    "Driver advance", "Insurance", "RTO/Permit", "Misc", "Personal"]

# ── Web push (VAPID) ─────────────────────────────────────────────────────────
VAPID_PUBLIC     = os.environ.get("VAPID_PUBLIC", "").strip()
VAPID_SUB        = os.environ.get("VAPID_SUB", "mailto:harshit@smilebots.com").strip()
PUSH_SUBS_FILE   = os.path.join(DATA_DIR, "push_subs.json") if DATA_DIR else "push_subs.json"
_VAPID_KEY = None
_vpb64 = os.environ.get("VAPID_PRIVATE_B64", "").strip()
if _vpb64:
    try:
        _pem = base64.b64decode(_vpb64)
        _VAPID_KEY = serialization.load_pem_private_key(_pem, password=None)
    except Exception as _e:
        _VAPID_KEY = None
# Personal vehicles that share the FASTag account — excluded from fleet analytics.
# Match by registration suffix (editable via env, comma-separated).
EXCLUDE_VEHICLE_SUFFIXES = tuple(
    s.strip() for s in os.environ.get("EXCLUDE_VEHICLE_SUFFIXES", "1000,9936").split(",") if s.strip()
)

def is_fleet_vehicle(v):
    """False for personal vehicles sharing the tag account (by reg suffix)."""
    if not v:
        return False
    return not any(v.endswith(suf) for suf in EXCLUDE_VEHICLE_SUFFIXES)

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

# ── Permanent debit ledger (NOT pruned — this is the reconciliation history) ──
# The 24h transactions.json drives the rolling limit; this keeps every debit
# forever so reimbursement reconciliation has a month of history to match against.
def load_debits_log():
    if os.path.exists(DEBITS_LOG_FILE):
        try:
            with open(DEBITS_LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_debits_log(log):
    with open(DEBITS_LOG_FILE, "w") as f:
        json.dump(log, f)

def append_debit_log(entry):
    """Append one debit to the permanent ledger, de-duping resent SMS."""
    log = load_debits_log()
    for e in log:
        # strong de-dup on bank reference when present, else account+amount+time
        if entry.get("ref") and e.get("ref") == entry.get("ref"):
            return log
        if (not entry.get("ref")
                and e.get("account") == entry.get("account")
                and e.get("amount") == entry.get("amount")
                and abs(e.get("ts", 0) - entry.get("ts", 0)) < 90):
            return log
    log.append(entry)
    save_debits_log(log)
    return log

# ── Parsed business expenses (from WhatsApp exports, parsed by Claude) ────────
def load_expenses():
    if os.path.exists(EXPENSES_FILE):
        try:
            with open(EXPENSES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_expenses(rows):
    with open(EXPENSES_FILE, "w") as f:
        json.dump(rows, f)

def merge_expenses(new_rows):
    """Merge parsed expense rows, de-duping on a stable id (or date+amount+raw)."""
    existing = load_expenses()
    seen = set()
    for e in existing:
        seen.add(e.get("id") or f"{e.get('date')}|{e.get('amount')}|{e.get('raw','')[:40]}")
    added = 0
    for r in new_rows:
        key = r.get("id") or f"{r.get('date')}|{r.get('amount')}|{r.get('raw','')[:40]}"
        if key in seen:
            continue
        seen.add(key)
        existing.append(r)
        added += 1
    save_expenses(existing)
    return added, len(existing)

def _ist_day(ts):
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d")

# ── Reimbursement overrides (manual edits that survive re-import/recompute) ────
# Keyed per debit by bank ref (or account|amount|ts when ref is absent). Each
# value may carry: classification (business|personal|review), category, truck, note.
def debit_key(e):
    if e.get("ref"):
        return "ref:" + str(e["ref"])
    return f"{e.get('account')}|{e.get('amount')}|{int(e.get('ts', 0))}"

def load_overrides():
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_overrides(ov):
    with open(OVERRIDES_FILE, "w") as f:
        json.dump(ov, f)

def known_trucks():
    """Distinct fleet trucks seen in toll + fuel data, for the truck picker."""
    regs = {e.get("vehicle") for e in load_fastag_log()
            if e.get("vehicle") and is_fleet_vehicle(e.get("vehicle"))}
    out = []
    for r in sorted(regs):
        out.append({"reg": r, "last4": r[-4:]})
    return out

# ── FASTag persistence ────────────────────────────────────────────────────────
def load_fastag():
    if os.path.exists(FASTAG_FILE):
        try:
            with open(FASTAG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"balance": None, "ts": None, "alert_t1": False, "alert_t2": False}

def save_fastag(d):
    with open(FASTAG_FILE, "w") as f:
        json.dump(d, f)

def load_fastag_log():
    if os.path.exists(FASTAG_LOG_FILE):
        try:
            with open(FASTAG_LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_fastag_log(log):
    with open(FASTAG_LOG_FILE, "w") as f:
        json.dump(log, f)

def append_fastag_log(entry):
    """Append one toll crossing, de-duping resent SMS and pruning old history."""
    log = load_fastag_log()
    for e in log:
        if (e.get("vehicle") == entry.get("vehicle")
                and e.get("plaza") == entry.get("plaza")
                and e.get("toll") == entry.get("toll")
                and abs(e.get("ts", 0) - entry.get("ts", 0)) < 90):
            return log  # duplicate SMS — already logged
    log.append(entry)
    cutoff = time.time() - FASTAG_LOG_DAYS * 24 * 3600
    log = [e for e in log if e.get("ts", 0) > cutoff]
    save_fastag_log(log)
    return log

def parse_fastag(text):
    """Extract balance, vehicle, plaza and crossing time from a Kotak FASTag SMS.

    Sample:
    Kotak FASTag:Rs.715.00 Toll paid at Nanduri for CG04MC6292 on
    12-10-2026@12:42 .Bal.45763.60.Visit https://fastag.kotak.bank.in
    """
    if "FASTag" not in text:
        return None
    bal_match = re.search(r"Bal\.(\d+(?:\.\d+)?)", text)
    if not bal_match:
        return None
    toll_match = re.search(r"Rs\.(\d+(?:\.\d+)?)", text)
    # Plaza + vehicle together: "Toll paid at <plaza> for <VEHICLE> on"
    pv = re.search(r"Toll paid at (.+?) for ([A-Z0-9]+) on", text)
    plaza   = pv.group(1).strip() if pv else None
    vehicle = pv.group(2).strip() if pv else None
    # Use the SMS's own crossing time ("on 12-10-2026@12:42") when present,
    # since a forwarded SMS may arrive late.
    dtm = re.search(r"on (\d{2})-(\d{2})-(\d{4})@(\d{2}):(\d{2})", text)
    if dtm:
        d, mo, y, hh, mm = map(int, dtm.groups())
        try:
            ts = datetime(y, mo, d, hh, mm, tzinfo=IST).timestamp()
        except ValueError:
            ts = time.time()
    else:
        ts = time.time()
    return {
        "balance": float(bal_match.group(1)),
        "toll":    float(toll_match.group(1)) if toll_match else None,
        "vehicle": vehicle,
        "plaza":   plaza,
        "ts":      ts,
    }

def process_fastag(text):
    """
    Update stored FASTag balance silently. Alert only on threshold crossings:
    - first time balance <= T1 (10,500)
    - first time balance <= T2 (6,000)
    Both re-arm once a recharge lifts balance back above T1.
    """
    parsed = parse_fastag(text)
    if not parsed:
        return False

    bal   = parsed["balance"]

    # Log the toll crossing for fleet analytics (only real toll events)
    if parsed.get("vehicle"):
        append_fastag_log({
            "vehicle": parsed["vehicle"],
            "plaza":   parsed["plaza"],
            "toll":    parsed["toll"],
            "ts":      parsed["ts"],
        })

    state = load_fastag()
    state["balance"] = bal
    state["ts"]      = parsed["ts"]

    notify = None
    if bal > FASTAG_T1:
        # Recharged / healthy — re-arm both alerts for the next cycle
        state["alert_t1"] = False
        state["alert_t2"] = False
    else:
        if bal <= FASTAG_T2 and not state.get("alert_t2"):
            state["alert_t2"] = True
            state["alert_t1"] = True   # skip the milder one if we blew past it
            notify = "t2"
        elif bal <= FASTAG_T1 and not state.get("alert_t1"):
            state["alert_t1"] = True
            notify = "t1"

    save_fastag(state)

    if notify == "t2":
        send(f"🔴 *FASTag critically low: {fmt_inr(bal)}*\n"
             f"Recharge now to avoid trucks being stopped at toll.")
    elif notify == "t1":
        send(f"⚠️ *FASTag low: {fmt_inr(bal)}*\n"
             f"Time to recharge soon.")
    return True

def fastag_line():
    """One-line FASTag balance for status / debit messages, or None if unknown."""
    state = load_fastag()
    if state.get("balance") is not None:
        return f"FASTag: {fmt_inr(state['balance'])}"
    return None

def build_fleet_message():
    """Exact fleet metrics from the toll log (no trip inference)."""
    log = [e for e in load_fastag_log() if is_fleet_vehicle(e.get("vehicle"))]
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    def day_key(ts):  return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d")
    def mon_key(ts):  return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m")

    today_e = [e for e in log if day_key(e["ts"]) == today]
    month_e = [e for e in log if mon_key(e["ts"]) == month]

    def summarize(entries):
        trucks = sorted({e["vehicle"] for e in entries if e.get("vehicle")})
        spend  = sum((e.get("toll") or 0) for e in entries)
        return trucks, spend, len(entries)

    t_trucks, t_spend, t_count = summarize(today_e)
    m_trucks, m_spend, m_count = summarize(month_e)

    lines = ["*🚚 Fleet — FASTag*"]
    lines.append(f"\n*Today · {now.strftime('%-d %b')}*")
    lines.append(f"Trucks running: {len(t_trucks)}")
    lines.append(f"Toll crossings: {t_count}")
    lines.append(f"Toll spend: {fmt_inr(t_spend)}")
    if t_trucks:
        lines.append("· " + ", ".join(t_trucks))
    lines.append(f"\n*This month · {now.strftime('%b %Y')}*")
    lines.append(f"Active trucks: {len(m_trucks)}")
    lines.append(f"Toll crossings: {m_count}")
    lines.append(f"Toll spend: {fmt_inr(m_spend)}")
    ft = fastag_line()
    if ft:
        lines.append(f"\n{ft}")
        rw = fastag_runway()
        if rw and rw["daily_burn"] > 0:
            lines.append(f"Burn ~{fmt_inr(rw['daily_burn'])}/day")
            if rw["days_to_danger"] is not None:
                dd = datetime.now(IST) + timedelta(days=rw["days_to_danger"])
                lines.append(f"Hits {fmt_inr(FASTAG_DANGER)} in ~{rw['days_to_danger']:g}d ({dd.strftime('%-d %b')})")
    if not log:
        lines.append("\n_No toll data yet — it builds up as FASTag SMS arrive._")
    return "\n".join(lines)

# ── FASTag runway & morning-risk projection ──────────────────────────────────
# The whole FASTag wallet is drained by every vehicle on the account (fleet AND
# the personal ones), so burn-rate math counts ALL toll crossings — unlike the
# fleet-utilisation views, which exclude personal vehicles.
def _hourly_burn(window_days=14):
    """Average toll spend per clock-hour (0..23) over the last `window_days`.
    Returns a list of 24 floats (₹/hour averaged across the window)."""
    cutoff = time.time() - window_days * 86400
    hour_total = [0.0] * 24
    for e in load_fastag_log():
        if e.get("ts", 0) < cutoff or not e.get("toll"):
            continue
        h = datetime.fromtimestamp(e["ts"], tz=IST).hour
        hour_total[h] += e["toll"]
    return [x / window_days for x in hour_total]

def _project_to_hour(target_hour, window_days=14):
    """Project the FASTag balance forward to the next `target_hour` IST, using
    the historical spend in that clock window. Returns a dict or None."""
    st = load_fastag()
    bal = st.get("balance")
    if bal is None:
        return None
    hour_avg = _hourly_burn(window_days)
    daily = sum(hour_avg)
    now = datetime.now(IST)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    # Sum the average burn across the clock-hours between now and target.
    expected, cur = 0.0, now
    while cur < target:
        nxt = min(target, cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        expected += hour_avg[cur.hour] * ((nxt - cur).total_seconds() / 3600.0)
        cur = nxt
    return {"balance": bal, "projected": bal - expected, "expected": expected,
            "daily": daily, "target_ts": target.timestamp()}

def fastag_runway():
    """Days until the wallet reaches the warning / danger lines at current burn."""
    proj = _project_to_hour(MORNING_HOUR)
    if not proj:
        return None
    bal, daily = proj["balance"], proj["daily"]
    def days_to(thr):
        if bal <= thr:
            return 0.0
        return round((bal - thr) / daily, 1) if daily > 0 else None
    return {"balance": bal, "daily_burn": round(daily),
            "days_to_warn": days_to(FASTAG_T1), "days_to_danger": days_to(FASTAG_DANGER),
            "warn": FASTAG_T1, "danger": FASTAG_DANGER,
            "morning_projected": round(proj["projected"])}

def fastag_night_watch():
    """Once each evening, project tomorrow 8am. If the wallet is likely to fall
    below the danger line before a recharge could reflect, push a heads-up so
    Harshit can top up tonight. Deduped per day via fastag state (survives restarts)."""
    while True:
        try:
            now = datetime.now(IST)
            # Fire in the 9pm–11pm window; recharge reflects by noon, so night is the last safe moment.
            if 21 <= now.hour <= 23:
                st = load_fastag()
                today = now.strftime("%Y-%m-%d")
                if st.get("balance") is not None and st.get("risk_alert_day") != today:
                    proj = _project_to_hour(MORNING_HOUR)
                    if proj and proj["projected"] < FASTAG_DANGER:
                        send(f"🌙 *Recharge FASTag tonight*\n"
                             f"Now {fmt_inr(proj['balance'])} · projected ~{fmt_inr(max(0, proj['projected']))} by {MORNING_HOUR}am.\n"
                             f"That's below the {fmt_inr(FASTAG_DANGER)} danger line, and a recharge won't reflect till ~noon — top up before bed to keep trucks moving.")
                        st["risk_alert_day"] = today
                        save_fastag(st)
        except Exception as e:
            log.warning(f"night watch: {e}")
        time.sleep(1800)  # every 30 min

# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_sms(text):
    # ── Kotak format: "Sent Rs.X from Kotak Bank A/c XNNNN to <payee> on <date> ... UPI Ref <ref>" ──
    if "UPI Ref" in text and ("Kotak Bank AC X" in text or "Kotak Bank A/c X" in text):
        amt_match  = re.search(r"Rs\.(\d+(?:\.\d+)?)", text)
        acct_match = re.search(r"A/?[Cc] X(\d+)", text)
        if amt_match and acct_match:
            payee = re.search(r"\bto (.+?) on \d{2}-\d{2}-\d{2}", text)
            ref   = re.search(r"UPI Ref\s*(\d+)", text)
            return {
                "amount":  int(float(amt_match.group(1))),
                "account": acct_match.group(1),
                "payee":   payee.group(1).strip() if payee else None,
                "ref":     ref.group(1) if ref else None,
                "ts":      time.time()
            }
    # ── SBI format: "Dear UPI user A/C XNNNN debited by X ... trf to <payee> Refno <ref>" ──
    if "debited by" in text and "Refno" in text and "SBI" in text:
        amt_match  = re.search(r"debited by (\d+(?:\.\d+)?)", text)
        acct_match = re.search(r"A/[Cc] X(\d+)", text)
        if amt_match and acct_match:
            payee = re.search(r"trf to (.+?)\s+Refno", text)
            ref   = re.search(r"Refno\s*(\d+)", text)
            return {
                "amount":  int(float(amt_match.group(1))),
                "account": acct_match.group(1),
                "payee":   payee.group(1).strip() if payee else None,
                "ref":     ref.group(1) if ref else None,
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
    ft = fastag_line()
    if ft:
        lines.append(ft)
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
    lines.append("\n/status, /help")

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

# ── Web push helpers ─────────────────────────────────────────────────────────
def load_subs():
    if os.path.exists(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_subs(subs):
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(subs, f)

def _plain(text):
    """Strip Telegram markdown for the push body."""
    t = re.sub(r'[*_`]', '', text or '')
    return t.strip()

def _b64u_dec(s):
    s = s.replace('-', '+').replace('_', '/'); s += '=' * (-len(s) % 4)
    return base64.b64decode(s)

def _b64u_enc(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()

def _vapid_header(endpoint):
    u = urlparse(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    header = _b64u_enc(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(',', ':')).encode())
    claims = _b64u_enc(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUB}, separators=(',', ':')).encode())
    signing_input = f"{header}.{claims}".encode()
    der = _VAPID_KEY.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    jwt = f"{header}.{claims}." + _b64u_enc(sig)
    return f"vapid t={jwt}, k={VAPID_PUBLIC}"

def _encrypt_payload(payload, p256dh_b64, auth_b64):
    """RFC 8291 aes128gcm web-push payload encryption."""
    ua_pub_bytes = _b64u_dec(p256dh_b64)             # 65 bytes
    auth         = _b64u_dec(auth_b64)               # 16 bytes
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_priv = ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = as_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ua_pub)
    key_info = b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes
    ikm   = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=key_info).derive(shared)
    salt  = os.urandom(16)
    cek   = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # 0x02 = last-record delimiter
    header = salt + struct.pack("!L", 4096) + bytes([len(as_pub_bytes)]) + as_pub_bytes
    return header + ct

def send_push(text):
    if not (VAPID_PUBLIC and _VAPID_KEY):
        return
    subs = load_subs()
    if not subs:
        return
    data = text.encode('utf-8')
    dead = []
    for s in subs:
        try:
            ep   = s["endpoint"]
            keys = s.get("keys", {})
            enc  = _encrypt_payload(data, keys["p256dh"], keys["auth"])
            headers = {
                "Authorization":    _vapid_header(ep),
                "Content-Encoding": "aes128gcm",
                "Content-Type":     "application/octet-stream",
                "TTL":              "600",
            }
            r = requests.post(ep, data=enc, headers=headers, timeout=10)
            if r.status_code in (404, 410):
                dead.append(s)
            elif r.status_code >= 400:
                log.warning(f"push {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log.warning(f"push error: {e}")
    if dead:
        save_subs([s for s in subs if s not in dead])
        log.info(f"pruned {len(dead)} dead push subs")

def send(text, parse_mode="Markdown"):
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode
    })
    log.info(f"send() status={r.status_code} ok={r.json().get('ok')}")
    # mirror the same message to the dashboard PWA via web push
    try:
        threading.Thread(target=send_push, args=(_plain(text),), daemon=True).start()
    except Exception:
        pass

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
        # Permanent ledger for reconciliation (unpruned)
        append_debit_log({
            "account": txn["account"],
            "amount":  txn["amount"],
            "payee":   txn.get("payee"),
            "ref":     txn.get("ref"),
            "ts":      txn["ts"],
        })
    log.info(f"Saved txn: {txn}")
    send(build_status_message(txns, txn["account"], txn["amount"]))
    return True

# ── Routes ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/", methods=["GET"])
def home():
    """Serve the dashboard; fall back to a health string if the file is missing."""
    index = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index):
        return send_file(index)
    return "Fleet Ledger running.", 200

@app.route("/sw.js", methods=["GET"])
def sw_js():
    return send_file(os.path.join(BASE_DIR, "sw.js"), mimetype="application/javascript")

@app.route("/manifest.json", methods=["GET"])
def manifest():
    return send_file(os.path.join(BASE_DIR, "manifest.json"), mimetype="application/manifest+json")

@app.route("/icon-192.png", methods=["GET"])
def icon192():
    return send_file(os.path.join(BASE_DIR, "icon-192.png"), mimetype="image/png")

@app.route("/icon-512.png", methods=["GET"])
def icon512():
    return send_file(os.path.join(BASE_DIR, "icon-512.png"), mimetype="image/png")

@app.route("/push/key", methods=["GET"])
def push_key():
    return {"key": VAPID_PUBLIC, "enabled": bool(VAPID_PUBLIC and _VAPID_KEY)}, 200

@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    sub = request.get_json(silent=True)
    if not sub or "endpoint" not in sub:
        return {"ok": False, "error": "bad subscription"}, 400
    with lock:
        subs = load_subs()
        if not any(s.get("endpoint") == sub["endpoint"] for s in subs):
            subs.append(sub)
            save_subs(subs)
    return {"ok": True, "count": len(load_subs())}, 200

@app.route("/push/test", methods=["POST"])
def push_test():
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    send_push("🔔 Fleet Ledger\nPush is working — this is a test.")
    return {"ok": True}, 200

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

def _authed():
    """True if the request carries the dashboard token (when one is configured)."""
    if not DASH_TOKEN:
        return False  # closed until a token is set
    supplied = request.args.get("token") or request.headers.get("X-Token", "")
    return supplied == DASH_TOKEN

@app.route("/debits", methods=["GET"])
def debits():
    """Permanent debit ledger for reconciliation. Token-guarded.
    Optional ?since=<epoch> and ?accounts=1183,9421 filters."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    with lock:
        log = load_debits_log()
    since = request.args.get("since")
    if since:
        try:
            cutoff = float(since)
            log = [e for e in log if e.get("ts", 0) >= cutoff]
        except ValueError:
            pass
    accts = request.args.get("accounts")
    if accts:
        want = {a.strip() for a in accts.split(",")}
        log = [e for e in log if e.get("account") in want]
    from flask import jsonify
    return jsonify({"count": len(log), "debits": sorted(log, key=lambda e: e.get("ts", 0))})

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
    fastag = load_fastag()
    return jsonify({
        "accounts":    accounts,
        "last_txn_ts": last_ts,
        "fastag":      {"balance": fastag.get("balance"), "ts": fastag.get("ts"),
                        "runway": fastag_runway(),
                        "warn": FASTAG_T1, "danger": FASTAG_DANGER},
        "server_ts":   time.time()
    })

@app.route("/fastaghistory", methods=["GET"])
def fastag_history():
    """Daily toll spend with daily fuel spend overlaid, last ?days=N (default 30).
    Token-guarded. Lets the fuel↔toll correlation be eyeballed."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    from flask import jsonify
    try:
        days = max(7, min(120, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    now = datetime.now(IST)
    labels, idx = [], {}
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        idx[d] = len(labels); labels.append(d)
    toll = [0.0] * days
    for e in load_fastag_log():
        if not e.get("toll"):
            continue
        d = _ist_day(e["ts"])
        if d in idx:
            toll[idx[d]] += e["toll"]
    fuel = [0.0] * days
    for r in load_expenses():
        if r.get("kind") != "fuel":
            continue
        d = str(r.get("date", ""))
        if d in idx:
            fuel[idx[d]] += (r.get("amount") or 0)
    return jsonify({
        "labels": labels,
        "toll": [round(x) for x in toll],
        "fuel": [round(x) for x in fuel],
        "runway": fastag_runway(),
        "server_ts": time.time(),
    })

@app.route("/fleetdata", methods=["GET"])
def fleetdata():
    """Utilisation from the FASTag toll log for a month (?month=YYYY-MM, default current).
    Token-guarded. Returns per-truck per-day crossing counts + daily unique-active counts."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    from flask import jsonify
    month = request.args.get("month") or datetime.now(IST).strftime("%Y-%m")
    y, m = int(month[:4]), int(month[5:7])
    # days in month
    if m == 12:
        nxt = datetime(y + 1, 1, 1, tzinfo=IST)
    else:
        nxt = datetime(y, m + 1, 1, tzinfo=IST)
    start = datetime(y, m, 1, tzinfo=IST).timestamp()
    end   = nxt.timestamp()
    ndays = int((nxt - datetime(y, m, 1, tzinfo=IST)).days)

    with lock:
        log = [e for e in load_fastag_log()
               if start <= e.get("ts", 0) < end and is_fleet_vehicle(e.get("vehicle"))]

    def day_of(ts):
        return datetime.fromtimestamp(ts, tz=IST).day  # 1..ndays

    trucks = sorted({e["vehicle"] for e in log})
    matrix = {t: [0] * ndays for t in trucks}
    for e in log:
        matrix[e["vehicle"]][day_of(e["ts"]) - 1] += 1

    per_day = [0] * ndays
    per_day_spend = [0] * ndays
    for d in range(ndays):
        per_day[d] = sum(1 for t in trucks if matrix[t][d] > 0)
    for e in log:
        per_day_spend[day_of(e["ts"]) - 1] += (e.get("toll") or 0)
    per_day_spend = [round(x) for x in per_day_spend]

    per_truck_days = {t: sum(1 for c in matrix[t] if c > 0) for t in trucks}
    active_truck_days = sum(per_truck_days.values())
    seen = len(trucks)
    # Average over days that actually had toll activity — not calendar days —
    # so partial months and monsoon idle days don't drag it down.
    active_days = sum(1 for d in range(ndays) if per_day[d] > 0)
    avg_active = round(active_truck_days / active_days, 1) if active_days else 0

    return jsonify({
        "month": month, "ndays": ndays, "fleet_size": FLEET_SIZE,
        "trucks": trucks,
        "matrix": matrix,                       # vehicle -> [counts per day]
        "per_day_active": per_day,              # unique active trucks each day
        "per_day_spend": per_day_spend,         # toll spend each day
        "per_truck_days": per_truck_days,       # vehicle -> active day count
        "summary": {
            "avg_per_active_day": avg_active,   # avg trucks on days that ran
            "active_days": active_days,
            "active_truck_days": active_truck_days,
            "trucks_seen": seen,
            "idle_est": max(0, FLEET_SIZE - seen),
            "busiest": max(per_day) if per_day else 0,
            "toll_spend": round(sum((e.get("toll") or 0) for e in log)),
            "crossings": len(log),
        },
        "server_ts": time.time(),
    })

@app.route("/spend", methods=["GET"])
def spend():
    """Per-day UPI spend per account for a month (?month=YYYY-MM). Token-guarded.
    From the permanent debit ledger."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    from flask import jsonify
    month = request.args.get("month") or datetime.now(IST).strftime("%Y-%m")
    y, m = int(month[:4]), int(month[5:7])
    nxt = datetime(y + 1, 1, 1, tzinfo=IST) if m == 12 else datetime(y, m + 1, 1, tzinfo=IST)
    start = datetime(y, m, 1, tzinfo=IST).timestamp()
    end   = nxt.timestamp()
    ndays = (nxt - datetime(y, m, 1, tzinfo=IST)).days
    order = ["0353", "3826", "1183", "9421"]

    with lock:
        log = [e for e in load_debits_log() if start <= e.get("ts", 0) < end]

    # per_day_total[d] and per_day_by_acct[acct][d]
    per_day_total = [0] * ndays
    by_acct = {a: [0] * ndays for a in order}
    month_total_by_acct = {a: 0 for a in order}
    for e in log:
        a = e.get("account")
        if a not in by_acct:
            continue
        d = datetime.fromtimestamp(e["ts"], tz=IST).day - 1
        amt = e.get("amount", 0)
        by_acct[a][d] += amt
        per_day_total[d] += amt
        month_total_by_acct[a] += amt

    return jsonify({
        "month": month, "ndays": ndays, "accounts": order,
        "per_day_total": [round(x) for x in per_day_total],
        "per_day_by_account": {a: [round(x) for x in by_acct[a]] for a in order},
        "month_total_by_account": {a: round(month_total_by_acct[a]) for a in order},
        "ledger_starts": min((e["ts"] for e in load_debits_log()), default=None),
        "server_ts": time.time(),
    })

@app.route("/import", methods=["POST"])
def import_expenses():
    """Accept Claude-parsed business-expense rows (JSON). Token-guarded.
    Body: {"rows": [{id?, date 'YYYY-MM-DD', ts?, amount, kind 'fuel'|'other',
                     truck?, rate?, litres?, vendor?, raw?}, ...]}"""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "no rows"}, 400
    # normalise ts from date if missing
    for r in rows:
        if not r.get("ts") and r.get("date"):
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=IST)
                r["ts"] = d.timestamp()
            except Exception:
                pass
    with lock:
        added, total = merge_expenses(rows)
    log.info(f"/import: +{added} expenses (total {total})")
    return {"ok": True, "added": added, "total": total}, 200

@app.route("/fuel", methods=["GET"])
def fuel():
    """Per-truck fuel summary for a month (?month=YYYY-MM). Token-guarded."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    from flask import jsonify
    month = request.args.get("month") or datetime.now(IST).strftime("%Y-%m")
    with lock:
        rows = [r for r in load_expenses()
                if r.get("kind") == "fuel" and str(r.get("date", "")).startswith(month)]
    trucks = {}
    for r in rows:
        t = r.get("truck") or "—"
        d = trucks.setdefault(t, {"truck": t, "fuellings": 0, "litres": 0.0, "spend": 0})
        d["fuellings"] += 1
        d["litres"] += (r.get("litres") or 0)
        d["spend"]  += (r.get("amount") or 0)
    out = sorted(trucks.values(), key=lambda x: -x["spend"])
    for d in out:
        d["litres"] = round(d["litres"], 1)
        d["avg_rate"] = round(d["spend"] / d["litres"], 2) if d["litres"] else None
    tot_spend  = sum(d["spend"] for d in out)
    tot_litres = round(sum(d["litres"] for d in out), 1)
    return jsonify({
        "month": month, "trucks": out,
        "total_spend": tot_spend, "total_litres": tot_litres,
        "fuellings": len(rows),
        "avg_rate": round(tot_spend / tot_litres, 2) if tot_litres else None,
        "server_ts": time.time(),
    })

@app.route("/reimbursement", methods=["GET"])
def reimbursement():
    """Reconcile personal-account (1183/9421) debits against business expenses
    from the chat, by amount + date (±1 day), one-to-one. Token-guarded."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    from flask import jsonify
    month = request.args.get("month") or datetime.now(IST).strftime("%Y-%m")
    personal = {"1183", "9421"}

    with lock:
        debits   = [e for e in load_debits_log()
                    if e.get("account") in personal and str(_ist_day(e["ts"])).startswith(month)]
        expenses = list(load_expenses())
        overrides = load_overrides()

    # index expenses by amount for quick lookup; track consumption (one-to-one)
    exp_by_amt = {}
    for i, x in enumerate(expenses):
        exp_by_amt.setdefault(round(x.get("amount", -1)), []).append(i)
    consumed = set()
    DAY = 86400

    results = []
    for e in sorted(debits, key=lambda x: x["ts"]):
        amt = round(e.get("amount", 0))
        cands = [i for i in exp_by_amt.get(amt, [])
                 if i not in consumed and abs(expenses[i].get("ts", 0) - e["ts"]) <= 1.5 * DAY]
        auto_truck = None
        auto_cat = None
        if len(cands) == 1:
            consumed.add(cands[0])
            x = expenses[cands[0]]
            status = "business"
            match = x.get("truck") or x.get("vendor") or "business expense"
            auto_truck = x.get("truck")
            if x.get("kind") == "fuel":
                auto_cat = "Fuel"
        elif len(cands) == 0:
            status = "personal"; match = None
        else:
            status = "review"; match = f"{len(cands)} possible matches"

        key = debit_key(e)
        ov = overrides.get(key, {})
        # Manual edits win over the auto-reconciliation.
        status   = ov.get("classification", status)
        category = ov.get("category", auto_cat)
        truck    = ov.get("truck", auto_truck)
        results.append({
            "key": key,
            "ts": e["ts"], "date": _ist_day(e["ts"]), "account": e["account"],
            "amount": e["amount"], "payee": e.get("payee"),
            "status": status, "match": match,
            "category": category, "truck": truck,
            "note": ov.get("note"),
            "edited": bool(ov),
        })

    tot = {"business": 0, "personal": 0, "review": 0}
    for r in results:
        tot[r["status"]] = tot.get(r["status"], 0) + r["amount"]
    return jsonify({
        "month": month, "rows": results, "totals": tot,
        "has_expenses": len(expenses) > 0,
        "categories": REIMB_CATEGORIES,
        "known_trucks": known_trucks(),
        "server_ts": time.time(),
    })

@app.route("/reimbursement/override", methods=["POST"])
def reimbursement_override():
    """Set or clear manual edits on one debit row. Token-guarded.
    Body: {"key": <row key>, "classification"?, "category"?, "truck"?, "note"?}
    An empty-string value clears that field; clearing all removes the override."""
    if not _authed():
        return {"ok": False, "error": "unauthorized"}, 403
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    if not key:
        return {"ok": False, "error": "no key"}, 400
    allowed = ("classification", "category", "truck", "note")
    with lock:
        ov = load_overrides()
        cur = dict(ov.get(key, {}))
        for k in allowed:
            if k in body:
                v = body[k]
                if v in ("", None):
                    cur.pop(k, None)
                else:
                    cur[k] = v
        if cur:
            ov[key] = cur
        else:
            ov.pop(key, None)
        save_overrides(ov)
    return {"ok": True, "key": key, "override": ov.get(key, {})}, 200

@app.route("/sms", methods=["POST"])
def sms_endpoint():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    log.info(f"/sms received: {text[:80]!r}")
    if not text:
        return {"ok": False, "error": "no text"}, 400
    ok = process_sms(text)
    return {"ok": ok}, 200

@app.route("/fastag", methods=["POST"])
def fastag_endpoint():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    log.info(f"/fastag received: {text[:80]!r}")
    if not text:
        return {"ok": False, "error": "no text"}, 400
    ok = process_fastag(text)
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
                "/fleet — today & month: trucks running, toll spend\n\n"
                "/sync — restore from a pasted status message\n\n"
                "Accounts: 353, 3826, 1183, 9421\n"
                "FASTag balance shows in every status; you're alerted when it "
                "drops below ₹10,500, then again below ₹5,000. At night you also "
                "get a recharge-tonight heads-up if it's likely to fall below "
                "₹5,000 by morning (before a recharge could reflect).\n\n"
                "You can also paste a raw bank or FASTag SMS to log it manually."
            )
            send(help_text)

        elif text == "/status":
            with lock:
                txns = prune(load_txns())
                save_txns(txns)
            send(build_status_message(txns))

        elif text == "/fleet":
            send(build_fleet_message())

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

        elif "FASTag" in text and "Bal." in text:
            threading.Thread(target=process_fastag, args=(text,)).start()

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
threading.Thread(target=fastag_night_watch, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
