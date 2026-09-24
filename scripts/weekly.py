"""Queries behind the weekly sales dashboard.

Every function takes an ISO week-start date (Monday) and an optional owner
name, and returns plain dicts ready to serialise.

Definitions settled with the sales team:
  Lead            a lead owned by the rep
  Lead Contacted  status Contacted / Attempted to Contact, or already converted
  Lead Converted  converted to a contact (Zoho's convert action)
  Lead Action     that contact has a deal, at any stage
  Deal Won        that deal reached Closed Won

The journey is cumulative on purpose: 1,397 converted leads never had their
status set to Contacted, so counting status strictly makes the funnel widen
at step two.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")
TARGETS_PATH = os.path.join(ROOT, "config", "targets.json")

CONTACTED_STATUSES = ("Contacted", "Attempted to Contact")
STUCK_AFTER_DAYS = 21
CLOSING_SOON_DAYS = 30


def db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql, params=()):
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def one(sql, params=()):
    result = rows(sql, params)
    return result[0] if result else {}


def week_start(value=None):
    """Monday of the week containing `value` (defaults to today)."""
    if isinstance(value, str) and value:
        d = datetime.strptime(value[:10], "%Y-%m-%d").date()
    else:
        d = date.today()
    return d - timedelta(days=d.weekday())


def week_bounds(value=None):
    start = week_start(value)
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def owner_clause(owner, column="owner_name"):
    """Returns (sql_fragment, params) so callers can splice a filter in."""
    if owner and owner != "All":
        return f" AND {column} = ?", [owner]
    return "", []


# --- KPI tiles --------------------------------------------------------------

def kpis(week=None, owner=None):
    start, end = week_bounds(week)
    prev_start = (datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=7)).isoformat()
    prev_end = (datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()

    def leads_in(a, b):
        w, p = owner_clause(owner)
        return one(f"""SELECT COUNT(*) n FROM v_leads
                       WHERE date(created_time) BETWEEN ? AND ?{w}""", [a, b] + p).get("n", 0)

    def revenue_in(a, b):
        w, p = owner_clause(owner)
        r = one(f"""SELECT COUNT(*) n, COALESCE(SUM(amount_inr),0) v FROM v_deals
                    WHERE is_won = 1 AND closing_date BETWEEN ? AND ?{w}""", [a, b] + p)
        return r.get("n", 0), r.get("v", 0.0)

    def accounts_in(a, b):
        # Accounts carry no Created_Time in this CRM, so a new account is dated
        # by the lead conversion that produced it.
        w, p = owner_clause(owner)
        return one(f"""SELECT COUNT(DISTINCT converted_account_id) n FROM v_leads
                       WHERE is_converted = 1 AND converted_account_id IS NOT NULL
                         AND date(converted_date) BETWEEN ? AND ?{w}""", [a, b] + p).get("n", 0)

    w, p = owner_clause(owner)
    pipeline = one(f"""SELECT COUNT(*) n, COALESCE(SUM(amount_inr),0) v,
                              COALESCE(SUM(amount_inr * COALESCE(probability,0)/100.0),0) wv
                       FROM v_deals WHERE is_open = 1{w}""", p)

    won_n, won_v = revenue_in(start, end)
    prev_won_n, prev_won_v = revenue_in(prev_start, prev_end)

    def delta(now, before):
        if before in (None, 0):
            return None
        return round((now - before) / before * 100, 1)

    leads_now, leads_prev = leads_in(start, end), leads_in(prev_start, prev_end)
    acc_now, acc_prev = accounts_in(start, end), accounts_in(prev_start, prev_end)

    return {
        "week_start": start, "week_end": end,
        "prev_week_start": prev_start, "prev_week_end": prev_end,
        "leads": {"value": leads_now, "prev": leads_prev, "delta_pct": delta(leads_now, leads_prev)},
        "revenue": {"value": won_v, "deals": won_n, "prev": prev_won_v,
                    "delta_pct": delta(won_v, prev_won_v)},
        "pipeline": {"value": pipeline.get("v", 0.0), "deals": pipeline.get("n", 0),
                     "weighted": pipeline.get("wv", 0.0)},
        "accounts": {"value": acc_now, "prev": acc_prev, "delta_pct": delta(acc_now, acc_prev)},
    }


# --- Lead analysis ----------------------------------------------------------

def lead_sources(week=None, owner=None, scope="week"):
    w, p = owner_clause(owner)
    if scope == "week":
        start, end = week_bounds(week)
        return rows(f"""SELECT COALESCE(lead_source,'(not set)') source, COUNT(*) n
                        FROM v_leads WHERE date(created_time) BETWEEN ? AND ?{w}
                        GROUP BY source ORDER BY n DESC""", [start, end] + p)
    return rows(f"""SELECT COALESCE(lead_source,'(not set)') source, COUNT(*) n
                    FROM v_leads WHERE 1=1{w}
                    GROUP BY source ORDER BY n DESC LIMIT 12""", p)


def journey(week=None, owner=None, scope="all"):
    """Lead -> Contacted -> Converted -> Action -> Won.

    scope 'week' restricts to leads created that week, which is a small
    cohort; 'all' is the standing funnel and is the more useful default.
    """
    w, p = owner_clause(owner, "l.owner_name")
    date_filter, date_params = "", []
    if scope == "week":
        start, end = week_bounds(week)
        date_filter = " AND date(l.created_time) BETWEEN ? AND ?"
        date_params = [start, end]

    statuses = ",".join("?" for _ in CONTACTED_STATUSES)
    r = one(f"""
        SELECT
            COUNT(DISTINCT l.id) lead,
            COUNT(DISTINCT CASE WHEN l.lead_status IN ({statuses}) OR l.is_converted = 1
                                THEN l.id END) contacted,
            COUNT(DISTINCT CASE WHEN l.is_converted = 1 THEN l.id END) converted,
            COUNT(DISTINCT CASE WHEN d.id IS NOT NULL THEN l.id END) action,
            COUNT(DISTINCT CASE WHEN d.is_won = 1 THEN l.id END) won
        FROM v_leads l
        LEFT JOIN v_deals d
               ON d.contact_id = l.converted_contact_id AND l.is_converted = 1
        WHERE 1=1{date_filter}{w}
    """, list(CONTACTED_STATUSES) + date_params + p)

    steps = [("Lead", r.get("lead", 0)), ("Lead Contacted", r.get("contacted", 0)),
             ("Lead Converted", r.get("converted", 0)), ("Lead Action", r.get("action", 0)),
             ("Deal Won", r.get("won", 0))]
    out, prev = [], None
    for label, n in steps:
        out.append({"stage": label, "count": n,
                    "rate_from_prev": round(n / prev * 100, 1) if prev else None})
        prev = n
    overall = round(r.get("won", 0) / r["lead"] * 100, 2) if r.get("lead") else 0
    return {"steps": out, "overall_pct": overall, "scope": scope}


# --- Deal movement ----------------------------------------------------------

def movement(week=None, owner=None):
    """Stage changes inside the week, as from -> to.

    LAG over each deal's history gives the previous stage; rows with no
    predecessor are deals entering the pipeline.
    """
    start, end = week_bounds(week)
    w, p = owner_clause(owner, "d.owner_name")
    return rows(f"""
        WITH ordered AS (
            SELECT h.deal_id, h.deal_name, h.stage_raw, h.changed_at, h.amount_inr,
                   LAG(h.stage_raw) OVER (PARTITION BY h.deal_id ORDER BY h.changed_at) prev_stage
            FROM v_deal_history h
        )
        SELECT o.deal_id, o.deal_name,
               o.prev_stage AS from_stage,
               o.stage_raw  AS to_stage,
               date(o.changed_at) changed_on,
               o.amount_inr,
               d.owner_name, d.pipeline, d.stage AS current_stage
        FROM ordered o
        LEFT JOIN v_deals d ON d.id = o.deal_id
        WHERE date(o.changed_at) BETWEEN ? AND ?{w}
        ORDER BY o.changed_at DESC
    """, [start, end] + p)


def movement_summary(week=None, owner=None):
    moves = movement(week, owner)
    entered = [m for m in moves if not m["from_stage"]]
    progressed, closed_won, closed_lost, other = [], [], [], []
    for m in moves:
        if not m["from_stage"]:
            continue
        if m["to_stage"] == "Closed Won":
            closed_won.append(m)
        elif m["to_stage"] in ("Closed Lost", "Closed Lost to Competition"):
            closed_lost.append(m)
        else:
            progressed.append(m)
    return {
        "total_changes": len(moves),
        "entered_pipeline": len(entered),
        "progressed": len(progressed),
        "closed_won": len(closed_won),
        "closed_lost": len(closed_lost),
        "won_value": sum(m["amount_inr"] or 0 for m in closed_won),
        "lost_value": sum(m["amount_inr"] or 0 for m in closed_lost),
    }


# --- Targets ----------------------------------------------------------------

def load_targets():
    if not os.path.exists(TARGETS_PATH):
        return {}
    with open(TARGETS_PATH) as handle:
        return json.load(handle)


def target(month=None, owner=None):
    """Revenue target vs actual for the month containing `month`."""
    month = (month or date.today().isoformat())[:7]
    cfg = load_targets()
    if owner and owner != "All":
        table = cfg.get("owner_monthly_inr", {})
        value = table.get(owner, table.get("_default", 0))
    else:
        table = cfg.get("company_monthly_inr", {})
        value = table.get(month, table.get("_default", 0))

    w, p = owner_clause(owner)
    actual = one(f"""SELECT COALESCE(SUM(amount_inr),0) v, COUNT(*) n FROM v_deals
                     WHERE is_won = 1 AND strftime('%Y-%m', closing_date) = ?{w}""",
                 [month] + p)
    achieved = actual.get("v", 0.0)
    days_total = 30
    return {
        "month": month, "owner": owner or "All",
        "target_inr": value, "actual_inr": achieved, "deals": actual.get("n", 0),
        "achieved_pct": round(achieved / value * 100, 1) if value else None,
        "gap_inr": value - achieved,
        "configured": bool(cfg),
    }


# --- Attention lists --------------------------------------------------------

def stuck_deals(owner=None, days=STUCK_AFTER_DAYS):
    """Open deals with no stage change in `days`. The weekly call's action list."""
    w, p = owner_clause(owner, "d.owner_name")
    return rows(f"""
        SELECT d.id, d.deal_name, d.stage, d.pipeline, d.owner_name,
               d.amount_inr, d.closing_date,
               MAX(h.changed_at) last_change,
               CAST(julianday('now') - julianday(MAX(h.changed_at)) AS INTEGER) days_stuck
        FROM v_deals d
        LEFT JOIN v_deal_history h ON h.deal_id = d.id
        WHERE d.is_open = 1{w}
        GROUP BY d.id
        HAVING last_change IS NULL OR days_stuck >= ?
        ORDER BY days_stuck DESC NULLS LAST
        LIMIT 50
    """, p + [days])


def slipped_deals(week=None, owner=None):
    """Open deals whose closing date moved later during the week."""
    start, end = week_bounds(week)
    w, p = owner_clause(owner, "d.owner_name")
    return rows(f"""
        WITH ordered AS (
            SELECT h.deal_id, h.closing_date, h.changed_at,
                   LAG(h.closing_date) OVER (PARTITION BY h.deal_id ORDER BY h.changed_at) prev_close
            FROM v_deal_history h
        )
        SELECT o.deal_id, d.deal_name, d.owner_name, d.stage, d.amount_inr,
               o.prev_close AS was, o.closing_date AS now,
               CAST(julianday(o.closing_date) - julianday(o.prev_close) AS INTEGER) slipped_days,
               date(o.changed_at) changed_on
        FROM ordered o
        JOIN v_deals d ON d.id = o.deal_id
        WHERE o.prev_close IS NOT NULL
          AND o.closing_date > o.prev_close
          AND date(o.changed_at) BETWEEN ? AND ?{w}
        ORDER BY slipped_days DESC
    """, [start, end] + p)


def closing_soon(owner=None, days=CLOSING_SOON_DAYS):
    w, p = owner_clause(owner)
    return rows(f"""
        SELECT id, deal_name, stage, pipeline, owner_name, amount_inr,
               probability, closing_date,
               CAST(julianday(closing_date) - julianday('now') AS INTEGER) days_to_close
        FROM v_deals
        WHERE is_open = 1 AND closing_date IS NOT NULL
          AND closing_date BETWEEN date('now') AND date('now', ?){w}
        ORDER BY closing_date
    """, [f"+{days} days"] + p)


def owners():
    return [r["owner_name"] for r in rows(
        """SELECT DISTINCT owner_name FROM v_deals WHERE owner_name IS NOT NULL
           UNION SELECT DISTINCT owner_name FROM v_leads WHERE owner_name IS NOT NULL
           ORDER BY owner_name""")]


def weeks_available(limit=26):
    """Recent Mondays, newest first, for the week picker."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [(monday - timedelta(weeks=i)).isoformat() for i in range(limit)]
