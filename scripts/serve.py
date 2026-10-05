#!/usr/bin/env python3
"""Read-only JSON API over the CRM views, for hand-built dashboards.

Serves two things:
  - /api/*        JSON endpoints backed by the SQL views
  - /             static files from dashboard/, so a frontend fetching
                  /api/... is same-origin and never hits CORS

The database is opened read-only, so nothing served here can modify data.

Login required for everything except /login.html and /api/auth/login (see
auth.py). A user tied to one owner_name always gets their own data back,
no matter what an "owner" query param or a chatbot tool-call argument asks
for - that's enforced here (scoped_owner()) and in chatbot.py, not left to
the frontend or the model to get right. /api/query and /api/view/<name>
return raw, unfiltered rows with no owner scoping at all, so they're
admin-only.

Usage:
    python3 scripts/auth.py add <you> --admin   # once, before first run
    python3 scripts/serve.py                     # http://localhost:8420
    python3 scripts/serve.py 9000                 # custom port
"""

import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly
import ist
import leads
import segments
import contacts
import data_quality
import chatbot
import auth

# Holds the current request's session per-thread (ThreadingHTTPServer gives
# each request its own thread) so endpoint functions can read "who's asking"
# without every one of them needing a session parameter threaded through.
_local = threading.local()


def current_session():
    return getattr(_local, "session", None)


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


def ep_bookings_fiscal(query):
    # v_bookings_fiscal is pre-aggregated and carries no owner_name, so a
    # scoped request re-aggregates straight from v_deals instead of using
    # the view - same grouping, with the owner filter the view can't do.
    owner = scoped_owner_optional(query)
    if not owner:
        return {"rows": rows("SELECT * FROM v_bookings_fiscal ORDER BY fiscal_year")}
    return {"rows": rows(
        """SELECT fiscal_year, COUNT(*) AS deals_won, ROUND(SUM(amount_inr), 2) AS revenue_inr,
                  ROUND(AVG(amount_inr), 2) AS avg_deal_inr
           FROM v_deals WHERE is_won = 1 AND closing_date IS NOT NULL AND owner_name = ?
           GROUP BY fiscal_year ORDER BY fiscal_year""", (owner,))}


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
        f"""SELECT closing_month, fiscal_year, COUNT(*) AS deals_won,
                   ROUND(SUM(amount_inr), 2) AS revenue_inr
            FROM v_deals WHERE is_won = 1 AND closing_date IS NOT NULL AND {clause}
            GROUP BY closing_month, fiscal_year ORDER BY closing_month""", params)}


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
                   ROUND(SUM(CASE WHEN is_won THEN amount_inr END), 2) AS revenue_inr,
                   SUM(is_open) AS deals_open,
                   ROUND(SUM(CASE WHEN is_open THEN amount_inr END), 2) AS pipeline_inr
            FROM v_deals
            WHERE owner_name IS NOT NULL {clause}
            GROUP BY owner_name
            ORDER BY revenue_inr DESC""", params)}


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
        f"""SELECT CAST(strftime('%m', closing_date) AS INTEGER) AS month,
                   COUNT(*) AS deals_won,
                   ROUND(SUM(amount_inr), 2) AS revenue_inr
            FROM v_deals
            WHERE is_won = 1 AND closing_date IS NOT NULL{clause}
            GROUP BY month ORDER BY month""", params)}


def ep_pipelines(query):
    owner = scoped_owner_optional(query)
    clause, params = ("", ()) if not owner else (" AND owner_name = ?", (owner,))
    return {"rows": rows(
        f"""SELECT pipeline,
                   COUNT(*) AS deals,
                   SUM(is_won) AS won,
                   SUM(is_open) AS open_deals,
                   ROUND(SUM(CASE WHEN is_won THEN amount_inr END), 2) AS won_inr,
                   ROUND(SUM(CASE WHEN is_open THEN amount_inr END), 2) AS open_inr
            FROM v_deals WHERE pipeline IS NOT NULL{clause}
            GROUP BY pipeline ORDER BY won_inr DESC""", params)}


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
    week = query.get("week", [None])[0]
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
        "stuck": weekly.stuck_deals(owner),
        "slipped": weekly.slipped_deals(week, owner),
        "closing_soon": weekly.closing_soon(owner),
    }


def ep_weekly_options(_):
    return {"owners": weekly.owners(), "weeks": weekly.weeks_available(),
            "current_week": weekly.week_bounds()[0]}


# --- IST dashboard (replicates a native Zoho CRM Analytics dashboard) -------

def ep_ist_deals_by_stage(query):
    return {"rows": ist.deals_by_stage(scoped_owner(query))}


def ep_ist_lead_status(query):
    return {"rows": ist.lead_status(scoped_owner(query))}


def ep_ist_performance(query):
    weeks = min(int(query.get("weeks", ["3"])[0]), 26)
    return {"rows": ist.performance_weeks(weeks, scoped_owner(query))}


def ep_leads_hot(query):
    owner = scoped_owner(query)
    limit = min(int(query.get("limit", ["20"])[0]), 100)
    return {"rows": leads.hot_leads(owner, limit)}


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
    return contacts.account_contacts(account_name)


def ep_data_quality(_query):
    return data_quality.current()


def ep_auth_me(_query):
    session = current_session()
    if not session:
        return {"error": "not authenticated"}, 401
    return {k: v for k, v in session.items() if k != "expires_at"}


ROUTES = {
    "/api/auth/me": ep_auth_me,
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
    "/api/views": ep_views,
    "/api/bookings/fiscal": ep_bookings_fiscal,
    "/api/bookings/monthly": ep_bookings_monthly,
    "/api/pipeline/open": ep_pipeline_open,
    "/api/pipeline/history": ep_pipeline_history,
    "/api/reps": ep_reps,
    "/api/reps/timeseries": ep_rep_timeseries,
    "/api/seasonality": ep_seasonality,
    "/api/pipelines": ep_pipelines,
    "/api/query": ep_query,
}


def ep_chat(body):
    """The only endpoint that isn't a plain read of the views - it calls a
    local (never external) LLM, which itself can only call the same
    read-only tool functions the rest of this API exposes. See chatbot.py.
    The session is passed through so chatbot.ask() can force every
    owner-scoped tool call to the asking user's own data (non-admins) -
    same enforcement principle as scoped_owner() above, applied to the
    chatbot's tool-calling loop instead of a query string.
    """
    message = (body or {}).get("message", "").strip()
    if not message:
        return {"error": "pass {\"message\": \"...\"}"}, 400
    history = (body or {}).get("history", [])
    try:
        return chatbot.ask(message, history, session=current_session())
    except Exception as error:  # noqa: BLE001 - surface it to the chat UI, don't 500 silently
        return {"error": str(error)}, 500


POST_ROUTES = {
    "/api/chat": ep_chat,
}

# These return raw, unfiltered rows from any view with no owner scoping at
# all - fine when only one trusted person ever used this tool, a real gap
# now that different reps have their own logins. Locked to admin only.
ADMIN_ONLY_PREFIXES = ("/api/view/",)
ADMIN_ONLY_ROUTES = {"/api/query"}

# Rendered without a session - everything else requires one.
PUBLIC_PAGES = {"/login.html"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def _session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "session":
                    return v
        return None

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

    def _set_session_cookie(self, token, max_age):
        self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")

    def _handle_login(self, body):
        username = (body or {}).get("username", "").strip()
        password = (body or {}).get("password", "")
        user = auth.authenticate(username, password)
        if not user:
            return self._write_json({"error": "invalid username or password"}, 401)
        token = auth.create_session(user)
        payload = json.dumps(user).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie(token, auth.SESSION_TTL_SECONDS)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_logout(self):
        auth.delete_session(self._session_token())
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie("", 0)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        session = auth.get_session(self._session_token())
        _local.session = session

        if not parsed.path.startswith("/api"):
            is_html_page = parsed.path in ("", "/") or parsed.path.endswith(".html")
            if is_html_page and parsed.path not in PUBLIC_PAGES and not session:
                self.send_response(302)
                self.send_header("Location", "/login.html")
                self.end_headers()
                return
            return super().do_GET()  # static file from dashboard/

        if not session:
            return self._write_json({"error": "not authenticated"}, 401)

        query = urllib.parse.parse_qs(parsed.query)
        is_admin_route = parsed.path in ADMIN_ONLY_ROUTES or parsed.path.startswith(ADMIN_ONLY_PREFIXES)
        if is_admin_route and not session.get("is_admin"):
            return self._write_json({"error": "admin only"}, 403)

        if parsed.path in ROUTES:
            result = ROUTES[parsed.path](query)
        elif parsed.path.startswith("/api/view/"):
            result = ep_view(query, parsed.path[len("/api/view/"):])
        elif parsed.path in ("/api", "/api/"):
            result = {"endpoints": sorted(ROUTES) + sorted(POST_ROUTES) + ["/api/view/<view_name>"]}
        else:
            result = ({"error": f"no such endpoint '{parsed.path}'",
                       "endpoints": sorted(ROUTES)}, 404)

        self._write_json(result)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._write_json({"error": "invalid JSON body"}, 400)

        if parsed.path == "/api/auth/login":
            return self._handle_login(body)
        if parsed.path == "/api/auth/logout":
            return self._handle_logout()

        session = auth.get_session(self._session_token())
        _local.session = session
        if not session:
            return self._write_json({"error": "not authenticated"}, 401)

        if parsed.path not in POST_ROUTES:
            return self._write_json({"error": f"no such endpoint '{parsed.path}'"}, 404)

        self._write_json(POST_ROUTES[parsed.path](body))

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {fmt % args}\n")


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    if not os.path.exists(auth.USERS_PATH):
        sys.exit(f"{auth.USERS_PATH} not found - create a login first: python3 scripts/auth.py add <username> --admin")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    os.makedirs(STATIC_DIR, exist_ok=True)

    # Defaults to localhost (nobody but this machine can reach it) exactly
    # as before - logins are the new access control, not network isolation,
    # but a laptop shouldn't start listening on the LAN without someone
    # deciding that on purpose. Set CRM_BIND_HOST=0.0.0.0 to let other
    # machines on the network reach it once that's actually wanted (may
    # also need a Windows Firewall rule for this port).
    host = os.environ.get("CRM_BIND_HOST", "localhost")

    print(f"CRM data API on http://{host}:{port}")
    print(f"  endpoints:  http://{host}:{port}/api")
    print(f"  static dir: {STATIC_DIR}/  (put index.html here)")
    print(f"  database is opened read-only")
    print(f"  logins: {auth.USERS_PATH} ({len(auth.load_users())} user(s))\n")

    http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
