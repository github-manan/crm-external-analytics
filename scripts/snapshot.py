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
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import data_quality
import db


def main():
    as_of = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    conn = db.connect()

    if "v_deals" not in db.view_names(conn):
        sys.exit("v_deals missing - run scripts/transform.py first.")

    db.execute(conn, "DELETE FROM pipeline_snapshots WHERE snapshot_date = ?", (as_of,))
    db.execute(
        conn,
        """INSERT INTO pipeline_snapshots
               (snapshot_date, deal_id, deal_name, stage, pipeline,
                amount_inr, probability, closing_date, owner_name)
           SELECT ?::text, id, deal_name, stage, pipeline,
                  amount_inr, probability, closing_date, owner_name
           FROM v_deals
           WHERE is_open = 1""",
        (as_of,),
    )
    conn.commit()

    totals = db.execute(
        conn,
        """SELECT COUNT(*) AS n, COALESCE(SUM(amount_inr), 0) AS v
           FROM pipeline_snapshots WHERE snapshot_date = ?""",
        (as_of,),
    ).fetchone()
    rows, value = totals["n"], totals["v"]
    days = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) AS n FROM pipeline_snapshots"
    ).fetchone()["n"]

    print(f"Snapshot {as_of}: {rows} open deals, {value/10_000_000:.2f} cr")
    print(f"History depth: {days} day(s) captured")
    conn.close()

    data_quality.snapshot(as_of)


if __name__ == "__main__":
    main()
