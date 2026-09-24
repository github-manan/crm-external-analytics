#!/usr/bin/env python3
"""Read-only JSON API over the CRM views, for hand-built dashboards.

Serves two things:
  - /api/*        JSON endpoints backed by the SQL views
  - /             static files from dashboard/, so a frontend fetching
                  /api/... is same-origin and never hits CORS

The database is opened read-only, so nothing served here can modify data.

Usage:
    python3 scripts/serve.py           # http://localhost:8420
    python3 scripts/serve.py 9000      # custom port
"""

import http.server
import json
import os
import re
import sqlite3
import sys
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")
STATIC_DIR = os.path.join(ROOT, "dashboard")
DEFAULT_PORT = 8420

# Only a single bare SELECT is accepted on /api/query. Guards against a typo
# in a dashboard doing damage - though the read-only connection is the real
# protection.
SELECT_ONLY = re.compile(r"^\s*select\s", re.IGNORECASE)
FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|create|alter|replace|vacuum)\b",
    re.IGNORECASE,
)


def db():
    """Read-only connection. Rows come back as dicts."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql, params=()):
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def view_names():
    return [
        r["name"] for r in rows(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        )
    ]


# --- Endpoints --------------------------------------------------------------

def ep_meta(_):
    """Everything a dashboard needs to caption itself honestly."""
    counts = {v: rows(f"SELECT COUNT(*) AS n FROM {v}")[0]["n"] for v in view_names()}
    sync = rows("SELECT module, last_run_at, records_seen FROM sync_state ORDER BY module")
    snaps = rows(
        """SELECT COUNT(DISTINCT snapshot_date) AS days,
                  MIN(snapshot_date) AS first_day,
                  MAX(snapshot_date) AS last_day
           FROM pipeline_snapshots"""
    )[0]
    return {
        "currency": "INR",
        "amount_field": "amount_inr",
        "fiscal_year": "April-March",
        "row_counts": counts,
        "last_sync": sync,
        "snapshot_history": snaps,
        "caveats": [
            "Use amount_inr, never amount_original - 64 deals are USD.",
            "Deals have no usable Created_Time; it is the 2025-11-18 migration date.",
            "336 deals lack closing_date, 93 of them Closed Won (Rs 6.71 cr), and are "
            "absent from the bookings views.",
            "Group funnels by pipeline - Consulting, Datasurfr, MSS and Renewal have "
            "different stage sets.",
            "Sort stages by stage_order, not alphabetically.",
            "lost_reason_misused holds billing frequency, not loss reasons.",
        ],
    }


def ep_views(_):
    return {"views": view_names()}


def ep_view(query, name):
    if name not in view_names():
        return {"error": f"unknown view '{name}'", "views": view_names()}, 404
    limit = min(int(query.get("limit", ["50000"])[0]), 100000)
    return {"view": name, "rows": rows(f"SELECT * FROM {name} LIMIT ?", (limit,))}


def ep_bookings_fiscal(_):
    return {"rows": rows("SELECT * FROM v_bookings_fiscal ORDER BY fiscal_year")}


def ep_bookings_monthly(query):
    pipeline = query.get("pipeline", [None])[0]
    if pipeline:
        return {"rows": rows(
            "SELECT * FROM v_bookings_monthly WHERE pipeline = ? ORDER BY closing_month",
            (pipeline,))}
    return {"rows": rows(
        """SELECT closing_month, fiscal_year,
                  SUM(deals_won) AS deals_won,
                  ROUND(SUM(revenue_inr), 2) AS revenue_inr
           FROM v_bookings_monthly
           GROUP BY closing_month, fiscal_year
           ORDER BY closing_month""")}


def ep_pipeline_open(_):
    return {"rows": rows("SELECT * FROM v_open_pipeline ORDER BY pipeline, stage_order")}


def ep_pipeline_history(_):
    return {"rows": rows(
        """SELECT snapshot_date,
                  COUNT(*) AS open_deals,
                  ROUND(SUM(amount_inr), 2) AS value_inr,
                  ROUND(SUM(amount_inr * COALESCE(probability, 0) / 100.0), 2) AS weighted_inr
           FROM pipeline_snapshots
           GROUP BY snapshot_date
           ORDER BY snapshot_date""")}


def ep_reps(query):
    fy = query.get("fiscal_year", [None])[0]
    clause = "AND fiscal_year = ?" if fy else ""
    params = (fy,) if fy else ()
    return {"fiscal_year": fy or "all", "rows": rows(
        f"""SELECT owner_name,
                   SUM(is_won) AS deals_won,
                   ROUND(SUM(CASE WHEN is_won THEN amount_inr END), 2) AS revenue_inr,
                   SUM(is_open) AS deals_open,
                   ROUND(SUM(CASE WHEN is_open THEN amount_inr END), 2) AS pipeline_inr
            FROM v_deals
            WHERE owner_name IS NOT NULL {clause}
            GROUP BY owner_name
            ORDER BY revenue_inr DESC""", params)}


def ep_seasonality(_):
    return {"note": "Share of all-time won revenue by calendar month.", "rows": rows(
        """SELECT CAST(strftime('%m', closing_date) AS INTEGER) AS month,
                  COUNT(*) AS deals_won,
                  ROUND(SUM(amount_inr), 2) AS revenue_inr
           FROM v_deals
           WHERE is_won = 1 AND closing_date IS NOT NULL
           GROUP BY month ORDER BY month""")}


def ep_pipelines(_):
    return {"rows": rows(
        """SELECT pipeline,
                  COUNT(*) AS deals,
                  SUM(is_won) AS won,
                  SUM(is_open) AS open_deals,
                  ROUND(SUM(CASE WHEN is_won THEN amount_inr END), 2) AS won_inr,
                  ROUND(SUM(CASE WHEN is_open THEN amount_inr END), 2) AS open_inr
           FROM v_deals WHERE pipeline IS NOT NULL
           GROUP BY pipeline ORDER BY won_inr DESC""")}


def ep_query(query):
    """Arbitrary read-only SELECT, for charts the fixed endpoints don't cover."""
    sql = query.get("sql", [""])[0]
    if not sql:
        return {"error": "pass ?sql=SELECT..."}, 400
    if not SELECT_ONLY.match(sql) or FORBIDDEN.search(sql) or ";" in sql.rstrip(";"):
        return {"error": "only a single SELECT statement is allowed"}, 400
    try:
        return {"sql": sql, "rows": rows(sql)}
    except sqlite3.Error as error:
        return {"error": str(error)}, 400


# --- Weekly dashboard -------------------------------------------------------

def _wk(query):
    return query.get("week", [None])[0], query.get("owner", [None])[0]


def ep_weekly_summary(query):
    week, owner = _wk(query)
    return {
        "owner": owner or "All",
        "kpis": weekly.kpis(week, owner),
        "movement": weekly.movement_summary(week, owner),
        "target": weekly.target(week, owner),
    }


def ep_weekly_journey(query):
    week, owner = _wk(query)
    return weekly.journey(week, owner, query.get("scope", ["all"])[0])


def ep_weekly_sources(query):
    week, owner = _wk(query)
    return {"rows": weekly.lead_sources(week, owner, query.get("scope", ["week"])[0])}


def ep_weekly_movement(query):
    week, owner = _wk(query)
    return {"summary": weekly.movement_summary(week, owner),
            "rows": weekly.movement(week, owner)}


def ep_weekly_attention(query):
    week, owner = _wk(query)
    return {
        "stuck": weekly.stuck_deals(owner),
        "slipped": weekly.slipped_deals(week, owner),
        "closing_soon": weekly.closing_soon(owner),
    }


def ep_weekly_options(_):
    return {"owners": weekly.owners(), "weeks": weekly.weeks_available(),
            "current_week": weekly.week_bounds()[0]}


ROUTES = {
    "/api/weekly/summary": ep_weekly_summary,
    "/api/weekly/journey": ep_weekly_journey,
    "/api/weekly/sources": ep_weekly_sources,
    "/api/weekly/movement": ep_weekly_movement,
    "/api/weekly/attention": ep_weekly_attention,
    "/api/weekly/options": ep_weekly_options,
    "/api/meta": ep_meta,
    "/api/views": ep_views,
    "/api/bookings/fiscal": ep_bookings_fiscal,
    "/api/bookings/monthly": ep_bookings_monthly,
    "/api/pipeline/open": ep_pipeline_open,
    "/api/pipeline/history": ep_pipeline_history,
    "/api/reps": ep_reps,
    "/api/seasonality": ep_seasonality,
    "/api/pipelines": ep_pipelines,
    "/api/query": ep_query,
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api"):
            return super().do_GET()  # static file from dashboard/

        query = urllib.parse.parse_qs(parsed.query)
        status = 200

        if parsed.path in ROUTES:
            result = ROUTES[parsed.path](query)
        elif parsed.path.startswith("/api/view/"):
            result = ep_view(query, parsed.path[len("/api/view/"):])
        elif parsed.path in ("/api", "/api/"):
            result = {"endpoints": sorted(ROUTES) + ["/api/view/<view_name>"]}
        else:
            result = ({"error": f"no such endpoint '{parsed.path}'",
                       "endpoints": sorted(ROUTES)}, 404)

        if isinstance(result, tuple):
            result, status = result

        body = json.dumps(result, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {fmt % args}\n")


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    os.makedirs(STATIC_DIR, exist_ok=True)

    print(f"CRM data API on http://localhost:{port}")
    print(f"  endpoints:  http://localhost:{port}/api")
    print(f"  static dir: {STATIC_DIR}/  (put index.html here)")
    print("  database is opened read-only\n")

    http.server.ThreadingHTTPServer(("localhost", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
