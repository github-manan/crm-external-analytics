#!/usr/bin/env python3
"""Dump every view to CSV for tools that can't read SQLite directly.

Power BI, Excel and Google Sheets all take CSV; Metabase, Tableau and DuckDB
can query data/crm.db in place and don't need this.

Usage:
    python3 scripts/export.py              # all views -> exports/
    python3 scripts/export.py v_deals      # one view
"""

import csv
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")
OUT_DIR = os.path.join(ROOT, "exports")


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    conn = sqlite3.connect(DB_PATH)

    available = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        )
    ]
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
            writer.writerow([d[0] for d in cursor.description])
            rows = 0
            for row in cursor:
                writer.writerow(row)
                rows += 1
        size = os.path.getsize(path) / 1024
        print(f"  {view + '.csv':<28} {rows:>7} rows  {size:>8.1f} KB")

    conn.close()
    print(f"\nWritten to {OUT_DIR}/")


if __name__ == "__main__":
    main()
