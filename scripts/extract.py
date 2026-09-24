#!/usr/bin/env python3
"""Pull Zoho CRM modules into a local SQLite database.

Design notes:
  - Records land as raw JSON. Metrics are defined later in SQL views, so a
    wrong metric definition never costs us a re-extraction.
  - First run is a full backfill; later runs pass If-Modified-Since and only
    fetch what changed.
  - Deletes are polled separately, because a delta sync never reports them and
    stale rows would quietly inflate every count.

Stdlib only. Usage:
    python3 scripts/extract.py            # incremental (full on first run)
    python3 scripts/extract.py --full     # ignore saved state, re-pull all
    python3 scripts/extract.py Deals      # one module only
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zoho

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")

# Modules worth pulling, given what this org actually populates.
MODULES = [
    "Leads", "Contacts", "Accounts", "Deals", "DealHistory",
    "Tasks", "Calls", "Events", "Campaigns", "Notes", "Marketing_Expense",
]

# Zoho rejects a delete poll on these; field trackers have no recycle bin.
NO_DELETE_POLL = {"DealHistory"}

# Extra query params per module. Zoho hides converted leads from the default
# list view, so without converted=both the extract silently misses every lead
# that ever became a contact - which is most of the interesting ones.
MODULE_PARAMS = {
    "Leads": {"converted": "both"},
}

PER_PAGE = 200
FIELD_LIMIT = 50  # Zoho caps the `fields` param


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            module        TEXT NOT NULL,
            id            TEXT NOT NULL,
            payload       TEXT NOT NULL,
            modified_time TEXT,
            extracted_at  TEXT NOT NULL,
            is_deleted    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (module, id)
        );
        CREATE INDEX IF NOT EXISTS idx_records_module_modified
            ON records (module, modified_time);

        CREATE TABLE IF NOT EXISTS sync_state (
            module          TEXT PRIMARY KEY,
            last_run_at     TEXT,
            watermark       TEXT,
            records_seen    INTEGER
        );

        CREATE TABLE IF NOT EXISTS users (
            id           TEXT PRIMARY KEY,
            payload      TEXT NOT NULL,
            extracted_at TEXT NOT NULL
        );
    """)
    return conn


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def field_list(module):
    """Field api_names for modules that demand an explicit `fields` param."""
    fields = zoho.api_get("/crm/v8/settings/fields", {"module": module})["fields"]
    return [f["api_name"] for f in fields][:FIELD_LIMIT]


def fetch_pages(module, modified_since=None):
    """Yield pages of records, following Zoho's page_token pagination."""
    params = {"per_page": PER_PAGE, **MODULE_PARAMS.get(module, {})}
    headers = {}
    if modified_since:
        headers["If-Modified-Since"] = modified_since

    explicit_fields = None
    page_token = None

    while True:
        call_params = dict(params)
        if page_token:
            call_params["page_token"] = page_token
        if explicit_fields:
            call_params["fields"] = ",".join(explicit_fields)

        try:
            result = zoho.api_get(f"/crm/v8/{module}", call_params, headers)
        except zoho.ZohoError as error:
            # Some modules (field trackers) require `fields`. Learn and retry once.
            if error.status == 400 and "REQUIRED_PARAM_MISSING" in error.body and not explicit_fields:
                explicit_fields = field_list(module)
                continue
            if error.status == 429:
                print("    rate limited, backing off 60s")
                time.sleep(60)
                continue
            raise

        if not result or not result.get("data"):
            return

        yield result["data"]

        info = result.get("info", {})
        if not info.get("more_records"):
            return
        page_token = info.get("next_page_token")
        if not page_token:
            return
        time.sleep(0.2)  # stay well clear of the concurrency cap


def upsert(conn, module, rows):
    stamp = now_iso()
    conn.executemany(
        """INSERT INTO records (module, id, payload, modified_time, extracted_at, is_deleted)
           VALUES (?, ?, ?, ?, ?, 0)
           ON CONFLICT(module, id) DO UPDATE SET
               payload       = excluded.payload,
               modified_time = excluded.modified_time,
               extracted_at  = excluded.extracted_at,
               is_deleted    = 0""",
        [
            (module, row["id"], json.dumps(row, separators=(",", ":")),
             row.get("Modified_Time") or row.get("Change_Log_Time__s"), stamp)
            for row in rows
        ],
    )


def mark_deleted(conn, module):
    """Flag records Zoho has in its recycle bin so counts stay honest."""
    if module in NO_DELETE_POLL:
        return 0
    flagged = 0
    page_token = None
    while True:
        params = {"type": "all", "per_page": PER_PAGE}
        if page_token:
            params["page_token"] = page_token
        try:
            result = zoho.api_get(f"/crm/v8/{module}/deleted", params)
        except zoho.ZohoError as error:
            if error.status in (400, 403, 404):
                return 0  # module doesn't support the endpoint
            raise
        if not result or not result.get("data"):
            return flagged

        ids = [row["id"] for row in result["data"]]
        cursor = conn.executemany(
            "UPDATE records SET is_deleted = 1 WHERE module = ? AND id = ?",
            [(module, record_id) for record_id in ids],
        )
        flagged += cursor.rowcount or 0

        info = result.get("info", {})
        if not info.get("more_records"):
            return flagged
        page_token = info.get("next_page_token")
        if not page_token:
            return flagged


def sync_users(conn):
    result = zoho.api_get("/crm/v8/users", {"type": "AllUsers", "per_page": 200})
    rows = result.get("users", [])
    stamp = now_iso()
    conn.executemany(
        """INSERT INTO users (id, payload, extracted_at) VALUES (?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               payload = excluded.payload, extracted_at = excluded.extracted_at""",
        [(u["id"], json.dumps(u, separators=(",", ":")), stamp) for u in rows],
    )
    conn.commit()
    print(f"Users            {len(rows):>6} synced")


def sync_module(conn, module, full=False):
    watermark = None
    if not full:
        row = conn.execute(
            "SELECT watermark FROM sync_state WHERE module = ?", (module,)
        ).fetchone()
        watermark = row[0] if row else None

    mode = "full" if not watermark else f"delta since {watermark[:16]}"
    print(f"{module:<16} {mode}")

    # Stamped before fetching so anything edited mid-run is caught next time.
    run_started = now_iso()

    total = 0
    for page in fetch_pages(module, modified_since=watermark):
        upsert(conn, module, page)
        conn.commit()  # keep partial progress if a later page fails
        total += len(page)
        print(f"    +{len(page)} (total {total})")

    deleted = mark_deleted(conn, module)

    conn.execute(
        """INSERT INTO sync_state (module, last_run_at, watermark, records_seen)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(module) DO UPDATE SET
               last_run_at = excluded.last_run_at,
               watermark   = excluded.watermark,
               records_seen = excluded.records_seen""",
        (module, now_iso(), run_started, total),
    )
    conn.commit()

    held = conn.execute(
        "SELECT COUNT(*) FROM records WHERE module = ? AND is_deleted = 0", (module,)
    ).fetchone()[0]
    note = f", {deleted} newly flagged deleted" if deleted else ""
    print(f"    done: {total} fetched, {held} held locally{note}\n")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    full = "--full" in sys.argv
    targets = args or MODULES

    unknown = [m for m in targets if m not in MODULES]
    if unknown:
        sys.exit(f"Unknown module(s): {', '.join(unknown)}\nKnown: {', '.join(MODULES)}")

    conn = connect()
    started = time.time()
    sync_users(conn)
    print()
    for module in targets:
        sync_module(conn, module, full=full)

    print(f"Database: {DB_PATH}")
    print(f"Elapsed:  {time.time() - started:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
