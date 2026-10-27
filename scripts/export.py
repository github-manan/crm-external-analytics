#!/usr/bin/env python3
"""Dump every view to CSV.

Power BI, Excel and Google Sheets all take CSV; Metabase and Tableau can
query the Postgres database directly and don't need this.

v_contacts includes real emails, in plain text, in a file this script never
cleans up - exports/ is gitignored so it won't leak via a commit, but
nothing deletes an old export either, so CSVs with contact emails can sit
on disk indefinitely across however many times this has been run. If
that's a problem for how this machine is handled/backed up, clear out
exports/ manually after you're done with whatever needed the CSV.

Usage:
    python3 scripts/export.py              # all views -> exports/
    python3 scripts/export.py v_deals      # one view
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "exports")


def main():
    conn = db.connect(read_only=True)
    available = db.view_names(conn)
    targets = sys.argv[1:] or available

    unknown = [t for t in targets if t not in available]
    if unknown:
        sys.exit(f"Unknown view(s): {', '.join(unknown)}\nAvailable: {', '.join(available)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    for view in targets:
        cursor = conn.execute(f"SELECT * FROM {view}")
        path = os.path.join(OUT_DIR, f"{view}.csv")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([d.name for d in cursor.description])
            rows = 0
            for row in cursor:
                writer.writerow(row.values())
                rows += 1
        size = os.path.getsize(path) / 1024
        print(f"  {view + '.csv':<28} {rows:>7} rows  {size:>8.1f} KB")

    conn.close()
    print(f"\nWritten to {OUT_DIR}/")


if __name__ == "__main__":
    main()
