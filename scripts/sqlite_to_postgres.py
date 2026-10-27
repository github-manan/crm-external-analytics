#!/usr/bin/env python3
"""One-time copy of an existing SQLite database (data/crm.db, from before
the move to Postgres) into the Postgres database in DATABASE_URL.

Most of it could be re-pulled from Zoho, but pipeline_snapshots and
data_quality_snapshots can't - they're daily history that only exists
because the jobs ran on those days - so they have to be carried over.

Copies records, users, sync_state (so the next extract.py is a delta,
not a full re-pull), pipeline_snapshots and data_quality_snapshots, then
rebuilds the views. Refuses to run into a database that already has
records unless --replace is passed (which empties those tables first).

Usage:
    python3 scripts/sqlite_to_postgres.py [path/to/crm.db] [--replace]
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import db
import transform

TABLES = {
    "records": ["module", "id", "payload", "modified_time", "extracted_at", "is_deleted"],
    "users": ["id", "payload", "extracted_at"],
    "sync_state": ["module", "last_run_at", "watermark", "records_seen"],
    "pipeline_snapshots": ["snapshot_date", "deal_id", "deal_name", "stage", "pipeline",
                           "amount_inr", "probability", "closing_date", "owner_name"],
    "data_quality_snapshots": ["snapshot_date", "metric_key", "numerator", "denominator", "pct"],
}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    replace = "--replace" in sys.argv
    path = args[0] if args else os.path.join(db.ROOT, "data", "crm.db")
    if not os.path.exists(path):
        sys.exit(f"{path} not found")

    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = db.connect()
    db.ensure_schema(dst)

    existing = dst.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    if existing and not replace:
        sys.exit(f"Postgres already has {existing} records - pass --replace to overwrite them.")

    for table, columns in TABLES.items():
        has_table = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not has_table:
            print(f"  {table:<24} not in the SQLite file, skipped")
            continue
        dst.execute(f"TRUNCATE {table}")
        cols = ", ".join(columns)
        with dst.cursor() as cur, cur.copy(f"COPY {table} ({cols}) FROM STDIN") as copy:
            n = 0
            for row in src.execute(f"SELECT {cols} FROM {table}"):
                copy.write_row(row)
                n += 1
        print(f"  {table:<24} {n:>7} rows copied")

    dst.commit()
    dst.close()
    src.close()

    print("\nRebuilding views:")
    transform.main()


if __name__ == "__main__":
    main()
