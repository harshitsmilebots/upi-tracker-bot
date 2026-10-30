# Fleet Ledger — Roadmap

Running backlog of improvements. Built and deployed in batches, not one-by-one.
This doc lives on the `roadmap` branch so updating it doesn't trigger a Railway
deploy; it gets merged into `main` when we next ship features.

Last updated: 2026-10-30

---

## Current state (shipped)

- **UPI limit tracker** — 4 Kotak/SBI accounts, 24h rolling limits, Telegram bot
  (`/status`, `/add`, `/remove`, `/reset`, `/fleet`, `/help`), live alerts.
- **Permanent debit ledger** (`debits_log.json`) — every tracked debit kept
  forever (payee + bank ref), separate from the 24h limit log. Reconciliation
  data starts 2026-10-21.
- **FASTag** — balance tracking, two-tier low alerts (₹10.5k / ₹6k), toll log,
  personal-vehicle exclusion (reg suffix 1000/9936, via `EXCLUDE_VEHICLE_SUFFIXES`).
- **Fuel** — WhatsApp export parsed by Claude (or any LLM via the embedded
  prompt) → `/import` → per-truck litres/spend. Reconciliation (date+amount ±1d,
  one-to-one, ambiguous→review).
- **Dashboard** (served at `/`, token-gated via `DASH_TOKEN`) — Status/UPI,
  Utilisation (heatmap + daily active trucks), FASTag (daily toll spend), Fuel,
  Reimbursement (CSV export). Chart.js, light/dark (system), month nav, PWA with
  app-style bottom nav.
- **Web push** — custom RFC 8291 + VAPID (no pywebpush), mirrors Telegram
  messages to the installed PWA. Env: `VAPID_PUBLIC`, `VAPID_PRIVATE_B64`.

---

## Near-term polish

- [ ] **IST correctness audit.** Sweep server + client for any naive/UTC date
      math; pin dashboard date formatting explicitly to IST (currently uses the
      viewer's device timezone — fine on an IST phone, wrong elsewhere).
- [ ] **FASTag runway prediction.** From recent toll burn rate, project when the
      pool hits ₹10.5k: "at ₹X/day, reaches ₹10.5k in ~N days (~DD Mon)."
      Decision: averaging window — default **7-day trailing incl. idle days**
      (idle stretches correctly extend runway); 14-day optional.
- [ ] **Editable reimbursement.** Add a persisted overrides layer (keyed per
      debit) so manual edits survive re-import/recompute. Per row: change
      classification (business/personal/review), set expense type/category,
      assign a truck number. CSV reflects edits.
      - Decision: category list — proposed *Fuel, Repair, Parts, Driver advance,
        Toll, Misc, Personal* (confirm/adjust).
      - Decision: truck assignment — free-text last-4 vs pick from known trucks.

## Parked (earlier)

- [ ] **Financial-year view** — month-by-month totals (spend, litres, tolls)
      across the FY, with charts.
- [ ] **Scope web push** — limit pushes to *proactive* alerts (debit landed,
      FASTag threshold) rather than every `send()` incl. on-demand `/status`.

---

## v2 — Unified personal + business expense tracker

Generalize from a fleet tool into **one tagged expense ledger over multiple
sources**. Reimbursement/fuel/company-reports all become filtered views.

### Sources (all via the same Shortcut → `/sms` pipe)
- [x] UPI debits (done — permanent ledger)
- [ ] **Credit-card expenses** — parse card SMS (e.g. "INR 99 spent on Kotak
      Credit Card x4931 at AMAZON").
- [ ] **Autopays / recurring** — parse mandate-created + execution SMS (e.g. the
      Netflix auto-pay notices).

### Tags (on every transaction)
- [ ] **Company** — WTET, NEPL, PLIPL (Pitambara, current), + personal
      (confirm full set + which is personal).
- [ ] **Category** — fuel, repair, autopay, etc. (shared with reimbursement list).
- [ ] Truck (where relevant), business/personal, reimbursable.

### Intelligence
- [ ] **Recurring-aware tagging** — tag an autopay/recurring payment once; the
      tag auto-applies to future same-payee+amount instances. This is what kills
      the once-a-FY slog.
- [ ] **Rules + overrides** — payee/VPA → company+category rules for the bulk;
      manual overrides for exceptions (shared mechanism with editable reimbursement).

### Reporting
- [ ] **Accountant export** — filter by company + period (month or **FY Apr–Mar**)
      → CSV and a formatted **PDF statement**. Current reimbursement CSV becomes a
      special case.

### Open decisions / dependencies for v2
- [ ] Card + autopay **SMS samples** (a few real ones each) to build parsers.
- [ ] Confirm **company list** and which bucket is personal.
- [ ] Do all card/autopay SMS land on the **same phone** that already forwards?
- [ ] What the **accountant needs** in the PDF (columns, grouping by
      company/category, totals, any GST breakout).
