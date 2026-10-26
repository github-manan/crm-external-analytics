#!/usr/bin/env python3
"""Dump views and aggregates to static JSON files.

For a dashboard that ships as plain files with no server running - open
index.html and fetch ./data/v_deals.json. If the API server is running,
prefer that; this is the offline path.

Usage: python3 scripts/export_json.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "dashboard", "data")

# Aggregates worth precomputing, so a dashboard doesn't ship 8,844 leads to
# draw one bar chart.
AGGREGATES = {
    "bookings_fiscal": "SELECT * FROM v_bookings_fiscal ORDER BY fiscal_year",
    "bookings_monthly": """
        SELECT closing_month, fiscal_year, SUM(deals_won) AS deals_won,
               ROUND(SUM(revenue_inr), 2) AS revenue_inr
        FROM v_bookings_monthly GROUP BY closing_month, fiscal_year
        ORDER BY closing_month""",
    "open_pipeline": "SELECT * FROM v_open_pipeline ORDER BY pipeline, stage_order",
    "pipelines": """
        SELECT pipeline, COUNT(*) AS deals, SUM(is_won) AS won,
               SUM(is_open) AS open_deals,
               ROUND(SUM(CASE WHEN is_won = 1 THEN amount_inr END), 2) AS won_inr,
               ROUND(SUM(CASE WHEN is_open = 1 THEN amount_inr END), 2) AS open_inr
        FROM v_deals WHERE pipeline IS NOT NULL
        GROUP BY pipeline ORDER BY won_inr DESC NULLS LAST""",
    "seasonality": """
        SELECT CAST(substr(closing_date_effective, 6, 2) AS INTEGER) AS month,
               COUNT(*) AS deals_won, ROUND(SUM(amount_inr), 2) AS revenue_inr
        FROM v_deals WHERE is_won = 1
        GROUP BY month ORDER BY month""",
    "reps": """
        SELECT owner_name, fiscal_year_effective AS fiscal_year, SUM(is_won) AS deals_won,
               ROUND(SUM(CASE WHEN is_won = 1 THEN amount_inr END), 2) AS revenue_inr
        FROM v_deals WHERE owner_name IS NOT NULL AND fiscal_year_effective IS NOT NULL
        GROUP BY owner_name, fiscal_year_effective ORDER BY fiscal_year_effective, revenue_inr DESC NULLS LAST""",
    "pipeline_history": """
        SELECT snapshot_date, COUNT(*) AS open_deals,
               ROUND(SUM(amount_inr), 2) AS value_inr
        FROM pipeline_snapshots GROUP BY snapshot_date ORDER BY snapshot_date""",
}

# Full views a dashboard may want to filter client-side.
FULL_VIEWS = ["v_deals", "v_deal_history", "v_open_pipeline", "v_users"]


def main():
    conn = db.connect(read_only=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    def write(name, rows):
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=None, default=str)
        size = os.path.getsize(path) / 1024
        print(f"  {name + '.json':<26} {len(rows):>7} rows  {size:>8.1f} KB")

    for name, sql in AGGREGATES.items():
        write(name, conn.execute(sql).fetchall())
    for view in FULL_VIEWS:
        write(view, conn.execute(f"SELECT * FROM {view}").fetchall())

    counts = {
        name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        for name in db.view_names(conn)
    }
    write("meta", {
        "currency": "INR",
        "amount_field": "amount_inr",
        "fiscal_year": "April-March",
        "row_counts": counts,
        "generated_from": "Zoho CRM via scripts/extract.py",
    })

    conn.close()
    print(f"\nWritten to {OUT_DIR}/")


if __name__ == "__main__":
    main()
