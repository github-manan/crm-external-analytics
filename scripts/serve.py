#!/usr/bin/env python3
"""Read-only JSON API over the CRM views, for hand-built dashboards.

Serves two things:
  - /api/*        JSON endpoints backed by the SQL views
  - /             static files from dashboard/, so a frontend fetching
                  /api/... is same-origin and never hits CORS

The database is opened read-only, so nothing served here can modify data.

No login of its own: the app sits behind internal.datasurfr.ai, whose
Microsoft login decides who can reach it at all, and every viewer sees
all the data (current_session() returns VIEWER). Per-rep scoping is
still wired through scoped_owner() and chatbot.py, so it can come back by
having current_session() build the session from the identity the platform
passes in. Never expose this port directly - only behind that platform.

Usage:
    python3 scripts/serve.py                     # http://localhost:8420
    python3 scripts/serve.py 9000                 # custom port
"""

import base64
import errno
import http.server
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import weekly
import ist
import leads
import segments
import contacts
import data_quality
import chatbot
import adoption
import db
import documents

# Who's asking. There's no login, so it's always the same full-access
# viewer. If the platform later passes the signed-in user, build their
# session here ({"username", "display_name", "owner_name", "is_admin"})
# and the owner scoping below and in chatbot.py applies again.
VIEWER = {"username": "viewer", "display_name": "Viewer", "owner_name": None, "is_admin": True}


def current_session():
    return VIEWER


def scoped_owner(query):
    """The owner filter an endpoint should actually use: whatever the
    client asked for, UNLESS the logged-in user isn't an admin - in which
    case their own owner_name always wins, regardless of what the request
    asked for. This is the real enforcement of "a rep only ever sees their
    own data" - it happens here in Python, not by trusting the frontend or
    the chatbot's tool-call arguments to behave. Use this for anything that
    names or breaks down individuals (rep comparisons, weekly/IST views) -
    there's no opting out of this one.
    """
    session = current_session()
    if session and not session.get("is_admin"):
        return session["owner_name"]
    return query.get("owner", [None])[0]


def scoped_owner_optional(query):
    """Same default as scoped_owner (a non-admin sees their own data), but
    a non-admin can explicitly ask for the company-wide figure with
    ?view=company (named "view", not "scope" - ep_weekly_journey already
    uses "scope" for all-time-vs-this-week, a completely different thing -
    don't collide with it). Only for endpoints that aggregate across
    everyone without naming any individual (bookings, pipeline mix,
    seasonality) - seeing the company total isn't seeing "someone else's
    data" the way a named rep-comparison table would be, so this one's a
    view preference, not a privacy boundary."""
    session = current_session()
    if session and not session.get("is_admin"):
        if query.get("view", [None])[0] == "company":
            return None
        return session["owner_name"]
    return query.get("owner", [None])[0]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "dashboard")
DEFAULT_PORT = 8420

rows = db.rows
view_names = db.view_names


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
            "93 won deals have no real Closing_Date - bucketed by their last-modified "
            "date instead (closing_date_effective), flagged via closing_date_is_estimated, "
            "rather than being silently absent from bookings/target figures.",
            "Group funnels by pipeline - Consulting, Datasurfr, MSS and Renewal have "
            "different stage sets.",
            "Sort stages by stage_order, not alphabetically.",
            "lost_reason_misused holds billing frequency, not loss reasons.",
        ],
    }


def ep_bookings_fiscal(query):
    # v_bookings_fiscal is pre-aggregated and carries no owner_name, so a
    # scoped request re-aggregates straight from v_deals instead of using
    # the view - same grouping, with the owner filter the view can't do.
    owner = scoped_owner_optional(query)
    if not owner:
        return {"rows": rows("SELECT * FROM v_bookings_fiscal ORDER BY fiscal_year")}
    return {"rows": rows(
        """SELECT fiscal_year_effective AS fiscal_year, COUNT(*) AS deals_won,
                  ROUND(SUM(amount_inr), 2) AS revenue_inr, ROUND(AVG(amount_inr), 2) AS avg_deal_inr
           FROM v_deals WHERE is_won = 1 AND owner_name = ?
           GROUP BY fiscal_year_effective ORDER BY fiscal_year_effective""", (owner,))}


def ep_bookings_monthly(query):
    pipeline = query.get("pipeline", [None])[0]
    owner = scoped_owner_optional(query)
    if not owner:
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
    clause, params = "owner_name = ?", [owner]
    if pipeline:
        clause += " AND pipeline = ?"
        params.append(pipeline)
    return {"rows": rows(
        f"""SELECT closing_month_effective AS closing_month, fiscal_year_effective AS fiscal_year,
                   COUNT(*) AS deals_won, ROUND(SUM(amount_inr), 2) AS revenue_inr
            FROM v_deals WHERE is_won = 1 AND {clause}
            GROUP BY closing_month_effective, fiscal_year_effective ORDER BY closing_month_effective""", params)}


def ep_pipeline_open(query):
    owner = scoped_owner_optional(query)
    if not owner:
        return {"rows": rows("SELECT * FROM v_open_pipeline ORDER BY pipeline, stage_order")}
    return {"rows": rows(
        """SELECT pipeline, stage, stage_order, COUNT(*) AS deal_count,
                  ROUND(SUM(amount_inr), 2) AS value_inr,
                  ROUND(SUM(amount_inr * COALESCE(probability, 0) / 100.0), 2) AS weighted_inr
           FROM v_deals WHERE is_open = 1 AND owner_name = ?
           GROUP BY pipeline, stage, stage_order ORDER BY pipeline, stage_order""", (owner,))}


def ep_pipeline_history(query):
    owner = scoped_owner_optional(query)
    clause, params = ("", ()) if not owner else (" WHERE owner_name = ?", (owner,))
    return {"rows": rows(
        f"""SELECT snapshot_date,
                   COUNT(*) AS open_deals,
                   ROUND(SUM(amount_inr), 2) AS value_inr,
                   ROUND(SUM(amount_inr * COALESCE(probability, 0) / 100.0), 2) AS weighted_inr
            FROM pipeline_snapshots{clause}
            GROUP BY snapshot_date
            ORDER BY snapshot_date""", params)}


def ep_reps(query):
    fy = query.get("fiscal_year", [None])[0]
    owner = scoped_owner(query)
    clauses, params = [], []
    if fy:
        clauses.append("fiscal_year = ?")
        params.append(fy)
    if owner:
        clauses.append("owner_name = ?")
        params.append(owner)
    clause = ("AND " + " AND ".join(clauses)) if clauses else ""
    return {"fiscal_year": fy or "all", "owner": owner or "All", "rows": rows(
        f"""SELECT owner_name,
                   SUM(is_won) AS deals_won,
                   ROUND(SUM(CASE WHEN is_won = 1 THEN amount_inr END), 2) AS revenue_inr,
                   SUM(is_open) AS deals_open,
                   ROUND(SUM(CASE WHEN is_open = 1 THEN amount_inr END), 2) AS pipeline_inr
            FROM v_deals
            WHERE owner_name IS NOT NULL {clause}
            GROUP BY owner_name
            ORDER BY revenue_inr DESC NULLS LAST, owner_name""", params)}


def ep_rep_timeseries(query):
    owner = scoped_owner(query)
    granularity = query.get("granularity", ["monthly"])[0]
    start = query.get("start", [None])[0]
    end = query.get("end", [None])[0]
    try:
        return {"owner": owner or "All", "granularity": granularity, "start": start, "end": end,
                "rows": weekly.rep_timeseries(owner, granularity, start, end)}
    except ValueError as error:
        return {"error": str(error)}, 400


def ep_seasonality(query):
    owner = scoped_owner_optional(query)
    clause, params = ("", ()) if not owner else (" AND owner_name = ?", (owner,))
    return {"note": "Share of all-time won revenue by calendar month.", "rows": rows(
        f"""SELECT CAST(substr(closing_date_effective, 6, 2) AS INTEGER) AS month,
                   COUNT(*) AS deals_won,
                   ROUND(SUM(amount_inr), 2) AS revenue_inr
            FROM v_deals
            WHERE is_won = 1{clause}
            GROUP BY month ORDER BY month""", params)}


def ep_pipelines(query):
    owner = scoped_owner_optional(query)
    clause, params = ("", ()) if not owner else (" AND owner_name = ?", (owner,))
    return {"rows": rows(
        f"""SELECT pipeline,
                   COUNT(*) AS deals,
                   SUM(is_won) AS won,
                   SUM(is_open) AS open_deals,
                   ROUND(SUM(CASE WHEN is_won = 1 THEN amount_inr END), 2) AS won_inr,
                   ROUND(SUM(CASE WHEN is_open = 1 THEN amount_inr END), 2) AS open_inr
            FROM v_deals WHERE pipeline IS NOT NULL{clause}
            GROUP BY pipeline ORDER BY won_inr DESC NULLS LAST, pipeline""", params)}


# --- Weekly dashboard -------------------------------------------------------

def _wk(query):
    """(period, owner). The period is ?start=&end= (From/To picker) when
    both are given, else ?week= - see weekly.week_bounds for both forms."""
    start, end = query.get("start", [None])[0], query.get("end", [None])[0]
    if start and end:
        try:
            return weekly.make_period(start, end), scoped_owner(query)
        except ValueError:
            pass  # malformed dates: fall back to the week, never a 500
    return query.get("week", [None])[0], scoped_owner(query)


def ep_weekly_summary(query):
    week, owner = _wk(query)
    return {
        "owner": owner or "All",
        "kpis": weekly.kpis(week, owner),
        "movement": weekly.movement_summary(week, owner),
        "target": weekly.target(week, owner),
    }


def ep_weekly_journey(query):
    # Doesn't name any individual (just funnel counts), so - unlike the
    # other weekly/* endpoints - this one honors the company-wide toggle
    # too (the Overall page's Total Leads / conversion KPI tiles use it).
    week, _ = _wk(query)
    owner = scoped_owner_optional(query)
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
        "stuck": weekly.stuck_deals(owner, week=week),
        "slipped": weekly.slipped_deals(week, owner),
        "closing_soon": weekly.closing_soon(owner, week=week),
    }


def ep_weekly_options(_):
    return {"owners": weekly.owners(), "weeks": weekly.weeks_available(),
            "current_week": weekly.week_bounds()[0]}


# --- IST dashboard (replicates a native Zoho CRM Analytics dashboard) -------

def ep_ist_deals_by_stage(query):
    week, owner = _wk(query)
    return {"rows": ist.deals_by_stage(owner, week)}


def ep_ist_lead_status(query):
    week, owner = _wk(query)
    return {"rows": ist.lead_status(owner, week)}


def ep_ist_performance(query):
    week, owner = _wk(query)
    weeks = min(int(query.get("weeks", ["3"])[0]), 26)
    return {"rows": ist.performance_weeks(weeks, owner, week)}


def ep_leads_hot(query):
    week, owner = _wk(query)
    limit = min(int(query.get("limit", ["20"])[0]), 100)
    return {"rows": leads.hot_leads(owner, limit, weekly.as_of_date(week))}


def ep_segment_diagnostic(query):
    dimension = query.get("dimension", [None])[0]
    value = query.get("value", [None])[0]
    if not dimension or not value:
        return {"error": "pass ?dimension=pipeline|industry|country&value=..."}, 400
    return segments.segment_diagnostic(dimension, value)


def ep_account_contacts(query):
    account_name = query.get("account_name", [None])[0]
    if not account_name:
        return {"error": "pass ?account_name=..."}, 400
    session = current_session()
    owner = session["owner_name"] if session and not session.get("is_admin") else None
    return contacts.account_contacts(account_name, owner=owner)


def ep_data_quality(_query):
    return data_quality.current()


def ep_chat_status(_query):
    """Whether the Assistant has a model behind it (none until Gemini)."""
    return {"available": chatbot.MODEL_CONNECTED,
            "message": None if chatbot.MODEL_CONNECTED else chatbot.ASSISTANT_UNAVAILABLE}


def ep_me(_query):
    return current_session()


def ep_user_activity(_query):
    return {"rows": adoption.user_activity(), "active_within_days": adoption.ACTIVE_WITHIN_DAYS}


ROUTES = {
    "/api/me": ep_me,
    "/api/chat/status": ep_chat_status,
    "/api/users/activity": ep_user_activity,
    "/api/leads/hot": ep_leads_hot,
    "/api/segments/diagnostic": ep_segment_diagnostic,
    "/api/contacts/account": ep_account_contacts,
    "/api/data-quality": ep_data_quality,
    "/api/weekly/summary": ep_weekly_summary,
    "/api/weekly/journey": ep_weekly_journey,
    "/api/weekly/sources": ep_weekly_sources,
    "/api/weekly/movement": ep_weekly_movement,
    "/api/weekly/attention": ep_weekly_attention,
    "/api/weekly/options": ep_weekly_options,
    "/api/ist/deals_by_stage": ep_ist_deals_by_stage,
    "/api/ist/lead_status": ep_ist_lead_status,
    "/api/ist/performance": ep_ist_performance,
    "/api/meta": ep_meta,
    "/api/bookings/fiscal": ep_bookings_fiscal,
    "/api/bookings/monthly": ep_bookings_monthly,
    "/api/pipeline/open": ep_pipeline_open,
    "/api/pipeline/history": ep_pipeline_history,
    "/api/reps": ep_reps,
    "/api/reps/timeseries": ep_rep_timeseries,
    "/api/seasonality": ep_seasonality,
    "/api/pipelines": ep_pipelines,
}


def ep_chat(body):
    """The only endpoint that isn't a plain read of the views - it calls
    Gemini, which itself can only call the same read-only tool functions
    the rest of this API exposes. See chatbot.py.
    The session is passed through so chatbot.ask() can force every
    owner-scoped tool call to the asking user's own data (non-admins) -
    same enforcement principle as scoped_owner() above, applied to the
    chatbot's tool-calling loop instead of a query string.
    """
    message = (body or {}).get("message", "").strip()
    if not message:
        return {"error": "pass {\"message\": \"...\"}"}, 400
    history = (body or {}).get("history", [])
    session = current_session()

    upload = None
    upload_id = (body or {}).get("upload_id")
    if upload_id:
        upload = documents.get_upload(upload_id, session["username"])
        if not upload:
            return {"error": "that attached file isn't available anymore - try uploading it again"}, 400

    try:
        return chatbot.ask(message, history, session=session, upload=upload)
    except Exception as error:  # noqa: BLE001 - surface it to the chat UI, don't 500 silently
        return {"error": str(error)}, 500


def ep_upload(body):
    """Accepts a base64-encoded file (not multipart - keeps the existing
    JSON-only POST plumbing, no new request parsing needed) and stores its
    extracted text, scoped to the uploader. See documents.py."""
    session = current_session()
    filename = (body or {}).get("filename", "").strip()
    content_b64 = (body or {}).get("content_base64", "")
    if not filename or not content_b64:
        return {"error": "pass {\"filename\": \"...\", \"content_base64\": \"...\"}"}, 400
    try:
        content_bytes = base64.b64decode(content_b64)
    except Exception:  # noqa: BLE001 - malformed input, not a server problem
        return {"error": "invalid file data"}, 400
    if len(content_bytes) > documents.MAX_FILE_BYTES:
        return {"error": f"file too large - max {documents.MAX_FILE_BYTES // (1024 * 1024)}MB"}, 400
    try:
        return documents.store_upload(session["username"], filename, content_bytes)
    except ValueError as error:
        return {"error": str(error)}, 400


POST_ROUTES = {
    "/api/chat": ep_chat,
    "/api/chat/upload": ep_upload,
}

# Names every CRM user - admin only once viewers have their own identity.
ADMIN_ONLY_ROUTES = {"/api/users/activity"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def end_headers(self):
        # Without this, browsers cache ist.js/ist.css on their own heuristic
        # and keep running the old code after a dashboard change. no-cache
        # still allows caching, it just makes the browser re-check each
        # load (a cheap 304 when nothing changed).
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _write_json(self, result, status=200):
        if isinstance(result, tuple):
            result, status = result
        body = json.dumps(result, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        session = current_session()

        if not parsed.path.startswith("/api"):
            # export_json.py writes raw, unfiltered data dumps into
            # dashboard/data/ for a separate no-server mode. dashboard/ is
            # also this server's static dir, so never serve that folder -
            # data goes out through /api/* only.
            if parsed.path.startswith("/data/") or parsed.path == "/data":
                return self._write_json({"error": "not served here - use /api/*"}, 403)
            return super().do_GET()  # static file from dashboard/

        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ADMIN_ONLY_ROUTES and not session.get("is_admin"):
            return self._write_json({"error": "admin only"}, 403)

        try:
            if parsed.path in ROUTES:
                result = ROUTES[parsed.path](query)
            elif parsed.path in ("/api", "/api/"):
                result = {"endpoints": sorted(ROUTES) + sorted(POST_ROUTES)}
            else:
                result = ({"error": f"no such endpoint '{parsed.path}'",
                           "endpoints": sorted(ROUTES)}, 404)
        except Exception as error:  # noqa: BLE001 - a bad query param (e.g. an unparseable
            # date) must return a clean 400, not crash the handler thread and drop the
            # connection with no response at all (confirmed: ?week=not-a-date did exactly that).
            result = ({"error": f"bad request: {error}"}, 400)

        self._write_json(result)

    # Bigger than any real request needs - the base64-encoded document
    # upload is the largest legitimate body (5MB raw -> ~6.8MB encoded).
    # Previously unbounded: a client could claim any Content-Length at all
    # and this would read that many bytes straight into memory before any
    # other check ran, so one request with a huge claimed size was enough
    # to exhaust memory on its own (this machine's RAM is already a known
    # constraint).
    MAX_BODY_BYTES = 10 * 1024 * 1024

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if length > self.MAX_BODY_BYTES:
            return self._write_json({"error": "request body too large"}, 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._write_json({"error": "invalid JSON body"}, 400)

        if parsed.path not in POST_ROUTES:
            return self._write_json({"error": f"no such endpoint '{parsed.path}'"}, 404)

        try:
            result = POST_ROUTES[parsed.path](body)
        except Exception as error:  # noqa: BLE001 - same reasoning as the GET dispatch above -
            # malformed input must return a clean 400, not drop the connection.
            return self._write_json({"error": f"bad request: {error}"}, 400)
        self._write_json(result)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {fmt % args}\n")


def main():
    db.database_url()  # exits with a clear message if DATABASE_URL isn't set
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    os.makedirs(STATIC_DIR, exist_ok=True)

    # Defaults to localhost: there's no login, so only this machine (or the
    # platform's reverse proxy on the same server) should reach it. Don't
    # set CRM_BIND_HOST=0.0.0.0 unless something in front controls access.
    host = os.environ.get("CRM_BIND_HOST", "localhost")

    try:
        server = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        # errno 48 on macOS, 98 on Linux, winerror 10048 on Windows -
        # almost always a server left running
        if error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) == 10048:
            sys.exit(
                f"Port {port} is already in use - a server is probably still running.\n"
                f"Either use it (http://{host}:{port}) or stop it first:\n"
                f"    pkill -f scripts/serve.py\n"
                f"Or start this one on a different port:\n"
                f"    python3 scripts/serve.py {port + 1}"
            )
        raise

    print(f"CRM data API on http://{host}:{port}")
    print(f"  endpoints:  http://{host}:{port}/api")
    print(f"  static dir: {STATIC_DIR}/  (put index.html here)")
    print(f"  database: Postgres (DATABASE_URL), read-only")
    print("  press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")


if __name__ == "__main__":
    main()
