# Zoho CRM → local data layer

Pulls Zoho CRM into a local SQLite database with typed SQL views on top, so
dashboards and models can be built without touching the Zoho API or its
quirks. Extraction only — no dashboards live here.

**Owner of this layer:** Manan · **Consumer:** Ankit (dashboards/BI)

---

## Quick start

```bash
python3 scripts/extract.py      # pull records from Zoho (delta after first run)
python3 scripts/transform.py    # (re)build the SQL views
python3 scripts/snapshot.py     # freeze today's open pipeline
```

Or all three in order:

```bash
./scripts/daily.sh              # logs to logs/daily.log
```

Database lands at `data/crm.db`. Stdlib Python 3 only — nothing to install.

Then serve it to the dashboard:

```bash
python3 scripts/serve.py        # http://localhost:8420
```

Other output formats if needed:

```bash
python3 scripts/export.py       # every view -> exports/*.csv
python3 scripts/export_json.py  # aggregates -> dashboard/data/*.json
```

---

## Building the dashboard

The dashboard is hand-built — no BI platform. Two ways to get at the data.

### Option A: the JSON API (preferred)

```bash
python3 scripts/serve.py        # http://localhost:8420
```

Static files in `dashboard/` are served from the **same origin** as `/api/*`,
so put `index.html` there and `fetch('/api/...')` works with no CORS setup and
no build step. The database is opened **read-only** — nothing served can
modify data.

| Endpoint | Returns |
|---|---|
| `/api` | endpoint index |
| `/api/meta` | row counts, last sync, snapshot depth, and the caveat list |
| `/api/bookings/fiscal` | won deals + revenue by fiscal year |
| `/api/bookings/monthly` | monthly series; `?pipeline=Datasurfr` to filter |
| `/api/pipelines` | totals per pipeline (won, open, value) |
| `/api/pipeline/open` | open deals by pipeline × stage, with weighted value |
| `/api/pipeline/history` | open pipeline over time, from the snapshots |
| `/api/seasonality` | won revenue by calendar month |
| `/api/reps` | per-owner won/open; `?fiscal_year=FY2025-26` to filter |
| `/api/view/<name>` | any view as rows; `?limit=` (default 50k) |
| `/api/query?sql=SELECT...` | arbitrary read-only SELECT |

`/api/query` takes a single bare `SELECT` — anything else is rejected. Use it
for charts the fixed endpoints don't cover, and tell me if one is worth
promoting to a real endpoint.

```js
const res  = await fetch('/api/bookings/fiscal');
const { rows } = await res.json();
// rows: [{ fiscal_year: 'FY2024-25', deals_won: 318, revenue_inr: 481... }]
```

Money arrives in **rupees**. Divide by 10,000,000 for crores, 100,000 for
lakhs.

### Option B: static JSON files

```bash
python3 scripts/export_json.py  # -> dashboard/data/*.json
```

For a dashboard that ships as plain files with nothing running. Precomputed
aggregates (`bookings_fiscal`, `seasonality`, `reps`, …) plus full
`v_deals.json` (2.1 MB) for client-side filtering. Re-run after each refresh
or the numbers go stale — the API path can't go stale this way, which is why
it's preferred.

`dashboard/index.html` is a placeholder that checks the API is reachable and
lists the endpoints. Replace it.

---

## Read this before writing any query

Five things in this CRM will produce wrong numbers if you don't know about
them. The views already handle all five — which is why **you should query the
views, never the `records` table**.

### 1. `amount_inr` is the only safe money column

64 of 2,110 deals are in USD, the rest INR. Zoho quotes `Exchange_Rate`
home→deal (USD deals carry `0.011`), so converting to rupees means
**dividing**, not multiplying. Summing `Amount` raw adds dollars to rupees and
understates total bookings by about ₹17.5 cr.

`v_deals.amount_inr` does `Amount / Exchange_Rate`. Use it. `amount_original`
is kept only for reconciliation against Zoho's own screens.

### 2. `Created_Time` on Deals is the migration date, not the deal date

Every deal was created in Zoho on **2025-11-18** when the data was migrated.
The field is therefore useless for cohort, aging, or "deals created this month"
analysis, and it isn't exposed in `v_deals` at all.

Use `closing_date` for everything time-based. Real history runs
**March 2019 → May 2027**.

`v_leads.created_time` *is* genuine — Leads were not migrated the same way.

### 3. Fiscal years run April–March

`v_deals.fiscal_year` is derived from `closing_date` (e.g. `FY2026-27` =
Apr 2026 – Mar 2027). There is also a hand-entered `fiscal_year_field` from
the CRM — **the two disagree**: 208 deals are tagged `FY 2026-27` by hand
while only 24 fall there by closing date. Prefer the derived column and treat
the divergence as a data-quality signal.

This matters because **April alone is 35.6% of all bookings** and March
another 11.9% — nearly half the year lands in two months. Any monthly trend
chart without fiscal context will look broken.

### 4. Stage names have typos, and there are four separate pipelines

The CRM config itself contains `Demo (Pipleline)` and
`Negotation / Contract Finalization (Commit)`. `v_deals.stage` is cleaned;
`stage_raw` preserves the original for cross-checking against Zoho.

Deals span four pipelines — `Consulting`, `Datasurfr`, `MSS`, `Renewal` —
each with its own stage set. **Always group funnels by `pipeline`.** A blended
funnel across Consulting and Datasurfr means nothing.

Use `stage_order` to sort funnel charts; alphabetical order is wrong.

### 5. 336 deals have no `closing_date`

That's 16% of the book, and since every time-based view keys off
`closing_date`, those deals **silently drop out** of any trend chart. Mostly
open deals with no date set, but check before quoting a total:

```sql
SELECT stage, COUNT(*) FROM v_deals WHERE closing_date IS NULL GROUP BY stage;
```

Worse: **93 of them are `Closed Won`, worth ₹6.71 cr.** They are excluded from
`v_bookings_monthly` and `v_bookings_fiscal` because there is no date to
attribute them to:

| | Deals | Value |
|---|---|---|
| All Closed Won | 1,677 | ₹208.31 cr |
| Won, no closing date | 93 | ₹6.71 cr |
| Won, in bookings views | 1,584 | ₹201.60 cr |

That ₹6.71 cr gap is real, not a bug.

If a stakeholder asks why the dashboard total differs from Zoho's, this is
usually why. Fixing it means someone entering the missing closing dates in the
CRM.

### 6. `Lost_Reason` does not contain loss reasons

Its only values are `Monthly`, `Quartely`, `Half-Yearly` — billing frequency,
entered in the wrong field. Exposed as `lost_reason_misused` so nobody builds
a win/loss analysis on it by accident. **Why deals are lost is not currently
answerable from this data.**

---

## Views

| View | Rows | Notes |
|---|---|---|
| `v_deals` | 2,110 | The main table. Currency-normalized, fiscal years derived, stages cleaned. |
| `v_deal_history` | 2,488 | Stage transitions with durations. See caveat below. |
| `v_leads` | 8,844 | Has a real `created_time`. Quality is poor — see below. |
| `v_accounts` | 927 | |
| `v_contacts` | 1,840 | |
| `v_activities` | 241 | Tasks + Calls + Events unioned, with `activity_type`. |
| `v_campaigns` | 15 | |
| `v_marketing_expense` | 12 | Custom module; raw payload kept, fields not yet mapped. |
| `v_users` | 23 | For resolving owner IDs. |
| `v_pipeline_snapshot` | grows daily | Open pipeline as of each date. **Cannot be backfilled.** |
| `v_bookings_monthly` | 236 | Won deals by month × pipeline. Convenience aggregate. |
| `v_bookings_fiscal` | 9 | Won deals by fiscal year. |
| `v_open_pipeline` | 14 | Current open deals by pipeline × stage, with weighted value. |

### Sample queries

```sql
-- Bookings by fiscal year, in crores
SELECT fiscal_year, deals_won, ROUND(revenue_inr/10000000.0, 2) AS revenue_cr
FROM v_bookings_fiscal ORDER BY fiscal_year;

-- Open pipeline by stage, correctly ordered, one pipeline at a time
SELECT stage, deal_count, ROUND(value_inr/10000000.0, 2) AS value_cr, weighted_inr
FROM v_open_pipeline WHERE pipeline = 'Datasurfr' ORDER BY stage_order;

-- Rep performance this fiscal year
SELECT owner_name, COUNT(*) AS won, ROUND(SUM(amount_inr)/10000000.0, 2) AS cr
FROM v_deals WHERE is_won = 1 AND fiscal_year = 'FY2026-27'
GROUP BY owner_name ORDER BY cr DESC;

-- How the open pipeline has moved over time (needs snapshot history)
SELECT snapshot_date, COUNT(*) AS open_deals, ROUND(SUM(amount_inr)/10000000.0,2) AS cr
FROM v_pipeline_snapshot GROUP BY snapshot_date ORDER BY snapshot_date;
```

---

## What this data supports, and what it doesn't

**Works well:** bookings trend and fiscal pacing (7+ years), pipeline by
stage and pipeline, rep performance, April/fiscal seasonality, deal size
distribution, account and industry mix.

**Doesn't work — data isn't there:**

- **Activity-based leading indicators.** 6 Calls and 3 Events across 21 users.
  Reps aren't logging activity, so "which rep's activity predicts closed
  deals" is unanswerable. This is a process fix, not an engineering one.
- **Win/loss reasons.** See trap 5.
- **Stage velocity beyond a first cut.** `DealHistory` covers all 2,110 deals,
  but 1,850 have only a single row — migrated straight into their final stage.
  Only ~260 deals show real transitions. Enough for an overall velocity
  number, not enough to segment by pipeline *and* rep.
- **Lead conversion analysis.** 4,397 of 8,844 leads have no `lead_status` at
  all, 2,481 are `Junk Lead`, and the top `lead_source` is `D-0` (1,978) — a
  placeholder. Treat any lead funnel number as indicative only.

---

## Scheduling

`data/crm.db` is only as fresh as the last run, so the refresh has to be
scheduled rather than remembered.

**Windows** — `scripts/daily.ps1`, registered with Task Scheduler. Run once in
an elevated PowerShell:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\path\to\crm\scripts\daily.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
Register-ScheduledTask -TaskName "CRM daily refresh" -Action $action `
           -Trigger $trigger -Settings $set
```

`-StartWhenAvailable` matters: it runs a missed job once the machine is back,
rather than silently skipping that day's snapshot.

**macOS** — use `launchd`, not cron. Cron does not catch up after sleep, so a
laptop asleep at 2am loses that day permanently; launchd runs the job on wake.

**Linux / server** — `scripts/daily.sh` via cron:

```bash
0 2 * * * /path/to/crm/scripts/daily.sh
```

**The snapshot job is the one thing that must not be skipped.** Zoho keeps
only a deal's current state, and `DealHistory` records changes rather than
positions — so "what did the open pipeline look like on 1 June" is
unanswerable unless it was captured that day. Every day the cron doesn't run
is a permanent hole in that history.

A full re-pull takes ~70s; a delta takes ~1s.

---

## How it works

```
Zoho CRM API v8  ──extract.py──▶  records (raw JSON)  ──transform.py──▶  v_* views
                                        │                                    │
                                        │                              snapshot.py
                                        ▼                                    ▼
                                  sync_state                      pipeline_snapshots
```

Records land as **raw JSON** and metrics are defined in SQL views, so a wrong
metric definition costs a view rebuild rather than a re-extraction.

- **Delta sync.** `sync_state.watermark` holds each module's last run time,
  passed back as `If-Modified-Since`. First run is a full backfill.
  `--full` forces a re-pull.
- **Deletes.** A delta sync never reports deletions, so `records/deleted` is
  polled separately and rows are flagged `is_deleted = 1`. Views filter these
  out. Without this, deleted deals would quietly inflate every total.
- **Retries.** 5 attempts with exponential backoff on truncated responses,
  dropped connections and 5xx; 60s wait on a 429.
- **Pagination.** Follows Zoho's `next_page_token`, so there's no 2,000-record
  ceiling.

### Files

```
scripts/authorize.py   one-time OAuth setup, writes .env
scripts/zoho.py        API client: token refresh, retries, pagination
scripts/extract.py     Zoho -> records table
scripts/transform.py   records -> v_* views
scripts/snapshot.py    daily open-pipeline freeze
scripts/serve.py       read-only JSON API + static host for dashboard/
scripts/export.py      views -> exports/*.csv
scripts/export_json.py views -> dashboard/data/*.json
scripts/daily.sh       runs extract + transform + snapshot
dashboard/             the hand-built dashboard lives here
data/crm.db            the database (gitignored)
.env                   credentials, mode 600 (gitignored, never commit)
```

---

## Credentials

OAuth client is **CRM External Analytics** (Server-based Application,
`api-console.zoho.in`), read-only scopes:

```
ZohoCRM.modules.ALL.READ        ZohoCRM.settings.modules.READ
ZohoCRM.settings.fields.READ    ZohoCRM.users.READ
ZohoCRM.coql.READ
```

**These grant read and nothing else.** The `.READ` suffix is the operation,
so `modules.ALL.READ` means read on every module. Dropping it —
`ZohoCRM.modules.ALL` — would silently grant create, update and delete as
well. If you ever regenerate this token, keep the suffix.

Writing to the CRM is therefore impossible through this credential, not
merely discouraged. Zoho rejects the call at the API boundary.

`.env` holds a **non-expiring refresh token**. Treat it like a password. It is
gitignored and mode 600; don't move it into a shared drive or commit it.

To re-authorize from scratch (new machine, or rotated secret):

```bash
python3 scripts/authorize.py    # needs ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET in .env
```

Note: Zoho caps refresh tokens per client at ~20 and silently revokes the
oldest past that, so don't re-run this repeatedly.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `OAUTH_SCOPE_MISMATCH` | Endpoint needs a scope not in the list above. Add it and re-authorize. |
| `invalid_client` | Data center mismatch. This org is **India** — `accounts.zoho.in` / `www.zohoapis.in`. |
| Every run says `full` | `sync_state.watermark` is NULL. Check the table. |
| `INVALID_TOKEN` | Refresh token revoked. Re-run `authorize.py`. |
| Totals disagree with Zoho | Almost always currency — check you used `amount_inr`. |
| Rate limited | Credit budget: CRM → Setup → Developer Hub → APIs → API Usage. |
