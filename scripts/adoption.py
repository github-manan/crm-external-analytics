#!/usr/bin/env python3
"""Per-user CRM activity - who's actually touching records, not who's
logging in. Zoho's Users API carries no login/session history at all
(checked the full raw payload for a user: created_time, a Modified_Time
that's when their own profile was last edited, and Isonline - an instant
"are they online right this second" flag, not a history) - so "opening
the CRM but not doing anything" genuinely can't be built from what we
have access to. This is the honest substitute: real record-touching
activity, not app-opening activity.

Two signals, both genuinely attributed to a specific person (not just
"whoever owns this record" - an owned record can be edited by someone
else entirely, e.g. an admin bulk import):
  - DealHistory's changed_by - a real actor on a real deal stage change.
    Well populated (2,515 rows, 13 people with real volume).
  - Tasks/Calls/Events owner - logged activities. Sparse (245 rows total
    across 23 users - this CRM barely logs activity at all, see leads.py)
    but still a real per-person signal where it exists.

Record modified_time/owner on Leads/Deals/Contacts is deliberately NOT
used here for the same reason leads.py's hot-lead heuristic flags it:
proves the record changed, not that its owner personally changed it.

Three statuses, not Zoho-login-based:
  active       - a genuine activity (either signal) within ACTIVE_WITHIN_DAYS
  inactive     - has activity on record, just not recently
  no_activity  - never has a DealHistory or Tasks/Calls/Events entry
                 attributed to them at all (could be new, could mean they
                 only ever work leads and never touch deals/activities)
"""

from datetime import date, datetime

from weekly import rows

ACTIVE_WITHIN_DAYS = 30


def _days_since(timestamp):
    if not timestamp:
        return None
    return (date.today() - datetime.fromisoformat(timestamp[:19]).date()).days


def user_activity():
    users = rows("SELECT id, full_name, email, role, profile, status FROM v_users ORDER BY full_name")

    deal_activity = {
        r["name"]: r for r in rows(
            """SELECT changed_by AS name, COUNT(*) AS n, MAX(changed_at) AS last
               FROM v_deal_history WHERE changed_by IS NOT NULL GROUP BY changed_by"""
        )
    }
    logged_activity = {
        r["name"]: r for r in rows(
            """SELECT owner_name AS name, COUNT(*) AS n, MAX(created_time) AS last
               FROM v_activities WHERE owner_name IS NOT NULL GROUP BY owner_name"""
        )
    }

    out = []
    for u in users:
        name = u["full_name"]
        d = deal_activity.get(name)
        t = logged_activity.get(name)
        last_dates = [x["last"] for x in (d, t) if x and x["last"]]
        last_activity = max(last_dates) if last_dates else None
        days_since = _days_since(last_activity)

        if last_activity is None:
            status = "no_activity"
        elif days_since <= ACTIVE_WITHIN_DAYS:
            status = "active"
        else:
            status = "inactive"

        out.append({
            "id": u["id"],
            "full_name": name,
            "email": u["email"],
            "role": u["role"],
            "profile": u["profile"],
            "account_status": u["status"],
            "deal_stage_changes": d["n"] if d else 0,
            "logged_activities": t["n"] if t else 0,
            "last_activity": last_activity,
            "days_since_last_activity": days_since,
            "status": status,
        })

    status_rank = {"active": 0, "inactive": 1, "no_activity": 2}
    out.sort(key=lambda r: (status_rank[r["status"]],
                             r["days_since_last_activity"] if r["days_since_last_activity"] is not None else 1 << 30))
    return out
