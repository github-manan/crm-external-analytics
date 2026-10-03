#!/usr/bin/env python3
"""Tracks whether the 3 data-quality fixes asked of the sales team are
actually happening, over time - instead of re-checking by eye each week.

The 3 metrics match the ask sent to the team (near-zero activity logging,
Lost_Reason misused for billing frequency instead of a loss reason,
incomplete closing dates on open deals). A 4th was added after finding, while
checking whether "upcoming renewals" from the original spec was covered,
that Subscription_End_Date - the field that question depends on - is filled
on only 1 of 30 Renewal-pipeline deals; same root cause, so tracked here too.

Snapshotted once a day into data_quality_snapshots (idempotent - re-running
the same day overwrites that day's rows, same pattern as snapshot.py).
Usage: python3 scripts/data_quality.py [YYYY-MM-DD]
"""

import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weekly import rows, one

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")

METRICS = {
    "recent_activity": {
        "label": "Open deals with activity logged in the last 30 days",
        "sql": """
            SELECT SUM(CASE WHEN last_activity_time >= datetime('now', '-30 days') THEN 1 ELSE 0 END) numerator,
                   COUNT(*) denominator
            FROM v_deals WHERE is_open = 1
        """,
    },
    "lost_reason_fill": {
        "label": "Closed Lost deals with a real loss reason recorded (Reason_For_Loss__s, not the misused Lost_Reason field)",
        "sql": """
            SELECT SUM(CASE WHEN json_extract(payload, '$.Reason_For_Loss__s') IS NOT NULL THEN 1 ELSE 0 END) numerator,
                   COUNT(*) denominator
            FROM records WHERE module = 'Deals' AND is_deleted = 0
              AND json_extract(payload, '$.Stage') = 'Closed Lost'
        """,
    },
    "closing_date_fill": {
        "label": "Open deals with a closing date set",
        "sql": """
            SELECT SUM(CASE WHEN closing_date IS NOT NULL THEN 1 ELSE 0 END) numerator,
                   COUNT(*) denominator
            FROM v_deals WHERE is_open = 1
        """,
    },
    "renewal_subscription_fill": {
        "label": "Renewal-pipeline deals with a subscription end date set",
        "sql": """
            SELECT SUM(CASE WHEN subscription_end_date IS NOT NULL THEN 1 ELSE 0 END) numerator,
                   COUNT(*) denominator
            FROM v_deals WHERE pipeline = 'Renewal'
        """,
    },
}


def _write_db():
    return sqlite3.connect(DB_PATH)


def snapshot(as_of=None):
    as_of = as_of or date.today().isoformat()
    conn = _write_db()
    conn.execute("DELETE FROM data_quality_snapshots WHERE snapshot_date = ?", (as_of,))
    for key, spec in METRICS.items():
        numerator, denominator = conn.execute(spec["sql"]).fetchone()
        numerator = numerator or 0
        pct = round(100 * numerator / denominator, 1) if denominator else None
        conn.execute(
            """INSERT INTO data_quality_snapshots (snapshot_date, metric_key, numerator, denominator, pct)
               VALUES (?, ?, ?, ?, ?)""",
            (as_of, key, numerator, denominator, pct),
        )
    conn.commit()
    conn.close()
    return as_of


def current():
    """Latest snapshot per metric, with how much it's moved since the
    earliest snapshot on record (not a fixed baseline date - this table
    only exists from whenever it was first run)."""
    latest_date = one("SELECT MAX(snapshot_date) AS d FROM data_quality_snapshots").get("d")
    if latest_date is None:
        return {"error": "no data-quality snapshot yet - run scripts/data_quality.py"}

    earliest_date = one("SELECT MIN(snapshot_date) AS d FROM data_quality_snapshots").get("d")
    has_trend = earliest_date != latest_date

    metrics = []
    for key, spec in METRICS.items():
        latest = one(
            "SELECT numerator, denominator, pct FROM data_quality_snapshots WHERE snapshot_date = ? AND metric_key = ?",
            (latest_date, key),
        )
        first = one(
            "SELECT pct FROM data_quality_snapshots WHERE snapshot_date = ? AND metric_key = ?",
            (earliest_date, key),
        )
        entry = {
            "metric": key,
            "label": spec["label"],
            "numerator": latest.get("numerator"),
            "denominator": latest.get("denominator"),
            "pct": latest.get("pct"),
            "tracking_since": earliest_date,
        }
        if has_trend and first.get("pct") is not None and latest.get("pct") is not None:
            change = round(latest["pct"] - first["pct"], 1)
            entry["change_pct_points"] = change
            entry["trend"] = (
                "improved" if change > 0 else "declined" if change < 0 else "unchanged"
            ) + f" by {abs(change)} points since {earliest_date}"
        else:
            entry["trend"] = (
                f"NO TREND YET - today ({latest_date}) is the first day this has ever been "
                f"recorded. Do not say this is improving, declining, or trending in any "
                f"direction - there is nothing to compare it to yet."
            )
        metrics.append(entry)

    result = {"as_of": latest_date, "tracking_since": earliest_date, "metrics": metrics}
    if not has_trend:
        result["note"] = (
            f"Tracking only started on {latest_date} - this is the first snapshot ever taken. "
            f"There is no history yet to say whether any of these numbers are improving."
        )
    return result


def history(metric_key, days=90):
    if metric_key not in METRICS:
        return {"error": f"unknown metric '{metric_key}' - one of {sorted(METRICS)}"}
    return rows(
        """SELECT snapshot_date, numerator, denominator, pct
           FROM data_quality_snapshots
           WHERE metric_key = ? AND snapshot_date >= date('now', ?)
           ORDER BY snapshot_date""",
        (metric_key, f"-{days} days"),
    )


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    as_of = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    snapshot(as_of)
    result = current()
    print(f"Data-quality snapshot {as_of}:")
    for m in result["metrics"]:
        trend = f" ({m['change_pct_points']:+.1f}pt since {m['tracking_since']})" if "change_pct_points" in m else ""
        print(f"  {m['label']}: {m['numerator']}/{m['denominator']} ({m['pct']}%){trend}")


if __name__ == "__main__":
    main()
