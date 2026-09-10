#!/usr/bin/env python3
"""Freeze today's open pipeline into pipeline_snapshots.

Zoho stores only a deal's current state, and DealHistory records changes rather
than positions. Neither can answer "what did the open pipeline look like on
1 June" after the fact, so that has to be captured as it happens. This is the
one table in the database that cannot be rebuilt from Zoho - it only accrues
if this job runs.

Idempotent: re-running on the same day overwrites that day's rows.
Usage: python3 scripts/snapshot.py [YYYY-MM-DD]
"""

import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")


def main():
    as_of = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    conn = sqlite3.connect(DB_PATH)

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_deals'"
    ).fetchone():
        sys.exit("v_deals missing - run scripts/transform.py first.")

    conn.execute("DELETE FROM pipeline_snapshots WHERE snapshot_date = ?", (as_of,))
    conn.execute(
        """INSERT INTO pipeline_snapshots
               (snapshot_date, deal_id, deal_name, stage, pipeline,
                amount_inr, probability, closing_date, owner_name)
           SELECT ?, id, deal_name, stage, pipeline,
                  amount_inr, probability, closing_date, owner_name
           FROM v_deals
           WHERE is_open = 1""",
        (as_of,),
    )
    conn.commit()

    rows, value = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(amount_inr), 0)
           FROM pipeline_snapshots WHERE snapshot_date = ?""",
        (as_of,),
    ).fetchone()
    days = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) FROM pipeline_snapshots"
    ).fetchone()[0]

    print(f"Snapshot {as_of}: {rows} open deals, {value/10_000_000:.2f} cr")
    print(f"History depth: {days} day(s) captured")
    conn.close()


if __name__ == "__main__":
    main()
