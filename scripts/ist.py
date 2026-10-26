"""Queries behind the replicated "IST Dashboard" (a native Zoho CRM Analytics
dashboard, screenshotted 2026-09-29 - see README for the component mapping).

Reuses weekly.py wherever the same metric already exists there (KPI tiles,
Lead Journey, Revenue Target, Leads by Source) so there is exactly one
definition of each per this codebase. This module only adds what weekly.py
doesn't already cover:

  deals_by_stage      current deal distribution by stage, split by pipeline.
                      The source dashboard blends all 4 pipelines into one
                      axis ("Deal Movement") - deliberately not replicated
                      that way here; see README point 4 on why.
  lead_status         lead counts by lead_status.
  performance_weeks   a rolling N-week table: leads created, deals entering
                      the pipeline (the closest honest proxy for "deals
                      created" - Deals.Created_Time is a migration artifact,
                      see README point 2), deals won, revenue won, and the
                      value of deals that entered the pipeline that week.

None of these numbers are guaranteed to match the source Zoho widgets
exactly - that dashboard's underlying report definitions aren't inspectable
from outside Zoho Analytics, and a couple of its own labels don't match its
own data (a "This Week" tile scoped to thousands of leads; a "Last 3 Months"
table whose columns are weekly). What's built here uses the same documented,
tested definitions as the rest of this codebase instead of guessing at
Zoho's internal logic.
"""

import weekly
from weekly import rows, one, week_bounds, owner_clause, movement, movement_summary  # noqa: F401
from weekly import as_of_date, DEALS_AS_OF


def deals_by_stage(owner=None, week=None):
    """For a past week, each deal's stage as of that Sunday (from history)."""
    w, p = owner_clause(owner)
    as_of = as_of_date(week)
    cte, src, params = (f"WITH {DEALS_AS_OF}", "asof", [as_of] + p) if as_of else ("", "v_deals", p)
    return rows(f"""
        {cte}
        SELECT pipeline, stage, stage_order, COUNT(*) deal_count,
               COALESCE(SUM(amount_inr), 0) value_inr
        FROM {src}
        WHERE pipeline IS NOT NULL{w}
        GROUP BY pipeline, stage, stage_order
        ORDER BY pipeline, stage_order
    """, params)


def lead_status(owner=None, week=None):
    """For a past week: leads created by that Sunday, but with today's
    status - Zoho keeps no history of lead status changes."""
    w, p = owner_clause(owner)
    as_of = as_of_date(week)
    created = " AND substr(created_time, 1, 10) <= ?" if as_of else ""
    return rows(f"""
        SELECT COALESCE(lead_status, 'None') status, COUNT(*) n
        FROM v_leads
        WHERE 1=1{created}{w}
        GROUP BY status
        ORDER BY n DESC, status
    """, ([as_of] if as_of else []) + p)


CLOSED_STAGES = ("Closed Won", "Closed Lost", "Closed Lost to Competition")


def performance_weeks(n=3, owner=None, week=None):
    """Oldest -> newest, left to right, matching how the source table reads.
    The last column is `week` (default: this week)."""
    anchor = week_bounds(week)[1] if week else None  # the week the period ends in
    week_starts = list(reversed(weekly.weeks_available(limit=n, anchor=anchor)))
    out = []
    for wk in week_starts:
        start, end = week_bounds(wk)
        w, p = owner_clause(owner)

        leads = one(f"""SELECT COUNT(*) n FROM v_leads
                        WHERE substr(created_time, 1, 10) BETWEEN ? AND ?{w}""",
                    [start, end] + p).get("n", 0)

        won = one(f"""SELECT COUNT(*) n, COALESCE(SUM(amount_inr), 0) v FROM v_deals
                      WHERE is_won = 1 AND closing_date BETWEEN ? AND ?{w}""",
                  [start, end] + p)

        summary = movement_summary(wk, owner)
        entered_open_value = sum(
            m["amount_inr"] or 0 for m in movement(wk, owner)
            if not m["from_stage"] and m["current_stage"] not in CLOSED_STAGES
        )

        out.append({
            "week_start": start, "week_end": end,
            "leads_created": leads,
            "deals_entered": summary["entered_pipeline"],
            "deals_won": won.get("n", 0),
            "revenue_won_inr": won.get("v", 0.0),
            "open_value_inr": entered_open_value,
        })
    return out
