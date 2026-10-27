#!/usr/bin/env python3
"""Build typed SQL views over the raw JSON landed by extract.py.

Everything downstream should read these views, never the `records` table.
They handle the four traps this CRM contains:

  1. Currency. Zoho's Exchange_Rate is quoted home->deal (USD deals carry
     0.011), so INR = Amount / Exchange_Rate. Multiplying shrinks USD deals
     by ~91x. `amount_inr` is the only safe money column.
  2. Created_Time is the Nov-2025 migration date for all 2,110 deals and is
     therefore useless. Anything time-based uses Closing_Date.
  3. Fiscal years run April-March.
  4. Two stage names carry typos in the CRM config ("Pipleline",
     "Negotation"); `stage` is cleaned, `stage_raw` preserves the original.
  5. Zoho's datetime fields (Created_Time, Modified_Time, Last_Activity_Time,
     Converted_Date_Time, Call_Start_Time, Start_DateTime - anything that
     isn't a plain date like Closing_Date/Due_Date) come back as ISO-8601
     with a +05:30 offset, e.g. "2026-10-19T02:00:00+05:30". Converting
     those to a timestamp would shift anything from 00:00-05:30 IST onto
     the previous day (or week) in UTC. The offset here is always +05:30,
     so IST_DATE/IST_DATETIME/IST_MONTH below read the wall-clock digits
     directly instead.

Postgres. Zoho's raw JSON is JSONB in records.payload; J()/N() below pull
a field out as text/number. Dates stay 'YYYY-MM-DD' text and flags
(is_won, is_open, ...) stay 0/1 integers, as the queries expect.

Views are recreated on every run, so this is safe to re-run any time.
Also creates any missing tables and seeds revenue targets (see main()).
Usage: python3 scripts/transform.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS_SEED_PATH = os.path.join(ROOT, "config", "targets.json")


def J(path, src="payload"):
    """A JSON field as text: J('Owner.name') -> payload->'Owner'->>'name'."""
    parts = path.split(".")
    return "(" + src + "".join(f"->'{x}'" for x in parts[:-1]) + f"->>'{parts[-1]}')"


def N(path):
    """A JSON field as a number (NULL when missing or empty)."""
    return f"NULLIF({J(path)}, '')::numeric"


def flag(condition):
    """A 0/1 integer column - CASE so a NULL input reads as 0."""
    return f"CASE WHEN {condition} THEN 1 ELSE 0 END"


# See trap 5 above - use these on any Zoho *datetime* field (not a plain
# date like Closing_Date/Due_Date, which carry no offset and are fine as-is).
IST_DATE = "substr({d}, 1, 10)"
IST_DATETIME = "substr({d}, 1, 16)"
IST_MONTH = "substr({d}, 1, 7)"

# Apr-Mar fiscal year label from a 'YYYY-MM-DD' text date, e.g.
# 2026-04-15 -> FY2026-27
FISCAL_YEAR = """
    CASE WHEN CAST(substr({d}, 6, 2) AS INTEGER) >= 4
        THEN 'FY' || substr({d}, 1, 4) || '-' ||
             substr(CAST(CAST(substr({d}, 1, 4) AS INTEGER) + 1 AS TEXT), 3, 2)
        ELSE 'FY' || CAST(CAST(substr({d}, 1, 4) AS INTEGER) - 1 AS TEXT) || '-' ||
             substr({d}, 3, 2)
    END
"""

STAGE = "(payload->>'Stage')"

# Fixes the CRM's own spelling and strips the parenthetical qualifiers.
STAGE_CLEAN = """
    CASE (payload->>'Stage')
        WHEN 'Demo (Pipleline)'                            THEN 'Demo'
        WHEN 'Negotation / Contract Finalization (Commit)'  THEN 'Negotiation / Contract Finalization'
        WHEN 'Proposal / Pricing Quote (Qualified)'         THEN 'Proposal / Pricing Quote'
        WHEN 'Need Analysis'                                THEN 'Needs Analysis'
        ELSE (payload->>'Stage')
    END
"""

# Funnel ordering. Closed and parked states sort last so charts read correctly.
STAGE_ORDER = """
    CASE (payload->>'Stage')
        WHEN 'Prospects'                                    THEN 10
        WHEN 'Need Analysis'                                THEN 20
        WHEN 'Demo (Pipleline)'                             THEN 30
        WHEN 'Trial'                                        THEN 40
        WHEN 'Proposal / Pricing Quote (Qualified)'         THEN 50
        WHEN 'Negotation / Contract Finalization (Commit)'  THEN 60
        WHEN 'Renewal'                                      THEN 70
        WHEN 'OnHold'                                       THEN 90
        WHEN 'Closed Won'                                   THEN 100
        WHEN 'Closed Lost'                                  THEN 110
        ELSE 999
    END
"""

VIEWS = {}

VIEWS["v_deals"] = f"""
SELECT
    id,
    {J('Deal_Name')}                        AS deal_name,
    {STAGE_CLEAN}                                               AS stage,
    {J('Stage')}                            AS stage_raw,
    {STAGE_ORDER}                                               AS stage_order,
    {flag(STAGE + " = 'Closed Won'")}  AS is_won,
    {flag(STAGE + " = 'Closed Lost'")} AS is_lost,
    -- Was "Stage NOT IN ('Closed Won', 'Closed Lost')" - matched only those
    -- two exact strings, so any future closed stage with a different exact
    -- name (e.g. "Closed Lost to Competition") would silently count as open.
    -- LIKE 'Closed%' catches any closed-sounding stage, known or not; the
    -- business question of whether OnHold should count as open is separate
    -- and deliberately untouched here - see README/project notes.
    {flag(STAGE + " NOT LIKE 'Closed%'")} AS is_open,
    {J('Pipeline')}                         AS pipeline,
    {J('Owner.name')}                       AS owner_name,
    {J('Owner.id')}                         AS owner_id,
    {J('Account_Name.name')}                AS account_name,
    {J('Account_Name.id')}                  AS account_id,
    {J('Contact_Name.name')}                AS contact_name,
    {J('Contact_Name.id')}                  AS contact_id,
    {J('Currency')}                         AS currency,
    {N('Amount')}                           AS amount_original,
    {N('Exchange_Rate')}                    AS exchange_rate,
    -- Divide, not multiply: see module docstring, trap 1.
    CASE
        WHEN COALESCE({N('Exchange_Rate')}, 0) = 0 THEN NULL
        ELSE {N('Amount')} / {N('Exchange_Rate')}
    END                                                         AS amount_inr,
    {N('Probability')}                      AS probability,
    substr({J('Closing_Date')}, 1, 10)               AS closing_date,
    substr({J('Closing_Date')}, 1, 7)  AS closing_month,
    {FISCAL_YEAR.format(d=J('Closing_Date'))} AS fiscal_year,
    -- 93 of 1,679 won deals have no Closing_Date, which silently dropped
    -- them from every bookings figure (v_bookings_monthly/_fiscal filtered
    -- on "closing_date IS NOT NULL"). These _effective columns fall back to
    -- the record's own last-modified date - the best available proxy for
    -- "when this was actually won" - and flag which rows are estimated so
    -- a consumer can tell real from inferred.
    COALESCE(substr({J('Closing_Date')}, 1, 10), {IST_DATE.format(d="modified_time")}) AS closing_date_effective,
    COALESCE(substr({J('Closing_Date')}, 1, 7), {IST_MONTH.format(d="modified_time")}) AS closing_month_effective,
    {FISCAL_YEAR.format(d="COALESCE(" + J('Closing_Date') + ", " + IST_DATE.format(d="modified_time") + ")")} AS fiscal_year_effective,
    {flag(J('Closing_Date') + " IS NULL")} AS closing_date_is_estimated,
    {J('Financial_Year')}                   AS fiscal_year_field,
    {J('Lead_Source')}                      AS lead_source,
    {J('Opportunity_Type')}                 AS opportunity_type,
    {J('Trial_Status')}                      AS trial_status,
    substr({J('Trial_End_Date')}, 1, 10)             AS trial_end_date,
    substr({J('Subscription_Start_Date')}, 1, 10)    AS subscription_start_date,
    substr({J('Subscription_End_Date')}, 1, 10)      AS subscription_end_date,
    substr({J('Proposal_Sent_Date')}, 1, 10)         AS proposal_sent_date,
    substr({J('Expected_Closure_Date')}, 1, 10)      AS expected_closure_date,
    {N('Sales_Cycle_Duration')}             AS sales_cycle_days,
    {N('Overall_Sales_Duration')}           AS overall_sales_days,
    {J('Hold_Reason')}                      AS hold_reason,
    -- Lost_Reason holds billing frequency in this CRM, not loss causes.
    {J('Lost_Reason')}                      AS lost_reason_misused,
    {J('Next_Step')}                        AS next_step,
    {IST_DATETIME.format(d=J('Last_Activity_Time'))} AS last_activity_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Deals' AND is_deleted = 0
"""

VIEWS["v_deal_history"] = f"""
SELECT
    id,
    {J('Potential_Name.id')}                AS deal_id,
    {J('Potential_Name.name')}              AS deal_name,
    {J('Stage')}                            AS stage_raw,
    {N('Stage_Duration_Calendar_Days')}     AS stage_duration_days,
    {N('Amount')}                           AS amount_original,
    {N('Exchange_Rate')}                    AS exchange_rate,
    CASE
        WHEN COALESCE({N('Exchange_Rate')}, 0) = 0 THEN NULL
        ELSE {N('Amount')} / {N('Exchange_Rate')}
    END                                                         AS amount_inr,
    {N('Probability')}                      AS probability,
    substr({J('Closing_Date')}, 1, 10)               AS closing_date,
    {J('Moved_To__s')}                      AS moved_to,
    {J('Modified_By.name')}                 AS changed_by,
    {IST_DATETIME.format(d=J('Modified_Time'))} AS changed_at,
    -- To the second (changed_at is to the minute, and a deal can change
    -- twice in one minute). Always +05:30, so text order is time order.
    {J('Modified_Time')}                    AS changed_at_exact,
    {FISCAL_YEAR.format(d=IST_DATE.format(d=J('Modified_Time')))} AS fiscal_year
FROM records
WHERE module = 'DealHistory' AND is_deleted = 0
"""

VIEWS["v_leads"] = f"""
SELECT
    id,
    {J('Full_Name')}                        AS full_name,
    {J('Company')}                          AS company,
    {J('Email')}                            AS email,
    {J('Lead_Status')}                      AS lead_status,
    {J('Lead_Source')}                      AS lead_source,
    {J('Industry')}                         AS industry,
    {J('City')}                             AS city,
    {J('Country')}                          AS country,
    {J('Owner.name')}                       AS owner_name,
    {J('Owner.id')}                         AS owner_id,
    {flag(J('Converted__s') + " = 'true'")} AS is_converted,
    {IST_DATE.format(d=J('Converted_Date_Time'))} AS converted_date,
    {J('Converted_Deal.id')}                AS converted_deal_id,
    {J('Converted_Account.id')}             AS converted_account_id,
    {J('Converted_Contact.id')}             AS converted_contact_id,
    -- Unlike Deals, Leads do carry a real Created_Time.
    {IST_DATETIME.format(d=J('Created_Time'))} AS created_time,
    {IST_MONTH.format(d=J('Created_Time'))} AS created_month,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Leads' AND is_deleted = 0
"""

VIEWS["v_accounts"] = f"""
SELECT
    id,
    {J('Account_Name')}                     AS account_name,
    {J('Industry')}                         AS industry,
    {J('Account_Type')}                     AS account_type,
    {J('Billing_City')}                     AS billing_city,
    {J('Billing_Country')}                  AS billing_country,
    {J('Website')}                          AS website,
    {J('Owner.name')}                       AS owner_name,
    {J('Owner.id')}                         AS owner_id,
    {IST_DATETIME.format(d=J('Created_Time'))} AS created_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Accounts' AND is_deleted = 0
"""

VIEWS["v_contacts"] = f"""
SELECT
    id,
    {J('Full_Name')}                        AS full_name,
    {J('Email')}                            AS email,
    {J('Title')}                            AS title,
    {J('Department')}                       AS department,
    {J('Account_Name.name')}                AS account_name,
    {J('Account_Name.id')}                  AS account_id,
    {J('Lead_Source')}                      AS lead_source,
    {J('Owner.name')}                       AS owner_name,
    {IST_DATETIME.format(d=J('Created_Time'))} AS created_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Contacts' AND is_deleted = 0
"""

VIEWS["v_activities"] = f"""
SELECT
    module                                                      AS activity_type,
    id,
    COALESCE(
        {J('Subject')},
        {J('Event_Title')}
    )                                                           AS subject,
    {J('Status')}                           AS status,
    {J('Owner.name')}                       AS owner_name,
    {J('Who_Id.name')}                      AS related_contact,
    {J('What_Id.name')}                      AS related_record,
    COALESCE(
        substr({J('Due_Date')}, 1, 10),
        {IST_DATE.format(d=J('Call_Start_Time'))},
        {IST_DATE.format(d=J('Start_DateTime'))}
    )                                                           AS activity_date,
    {J('Call_Duration')}                    AS call_duration,
    {IST_DATETIME.format(d=J('Created_Time'))} AS created_time,
    modified_time
FROM records
WHERE module IN ('Tasks', 'Calls', 'Events') AND is_deleted = 0
"""

VIEWS["v_campaigns"] = f"""
SELECT
    id,
    {J('Campaign_Name')}                    AS campaign_name,
    {J('Type')}                             AS campaign_type,
    {J('Status')}                           AS status,
    {N('Expected_Revenue')}                 AS expected_revenue,
    {N('Budgeted_Cost')}                    AS budgeted_cost,
    {N('Actual_Cost')}                      AS actual_cost,
    substr({J('Start_Date')}, 1, 10)                 AS start_date,
    substr({J('End_Date')}, 1, 10)                   AS end_date,
    {J('Owner.name')}                       AS owner_name,
    modified_time
FROM records
WHERE module = 'Campaigns' AND is_deleted = 0
"""

VIEWS["v_marketing_expense"] = f"""
SELECT
    id,
    {J('Name')}                             AS name,
    {J('Owner.name')}                       AS owner_name,
    payload                                                     AS raw_payload,
    modified_time
FROM records
WHERE module = 'Marketing_Expense' AND is_deleted = 0
"""

VIEWS["v_users"] = f"""
SELECT
    id,
    {J('full_name')}                        AS full_name,
    {J('email')}                            AS email,
    {J('role.name')}                        AS role,
    {J('profile.name')}                     AS profile,
    {J('status')}                           AS status
FROM users
"""

# --- Convenience aggregates -------------------------------------------------

VIEWS["v_bookings_monthly"] = """
SELECT
    closing_month_effective                                     AS closing_month,
    fiscal_year_effective                                        AS fiscal_year,
    pipeline,
    COUNT(*)                                                    AS deals_won,
    ROUND(SUM(amount_inr), 2)                                   AS revenue_inr,
    SUM(closing_date_is_estimated)                              AS deals_with_estimated_date
FROM v_deals
WHERE is_won = 1
GROUP BY closing_month_effective, fiscal_year_effective, pipeline
"""

VIEWS["v_bookings_fiscal"] = """
SELECT
    fiscal_year_effective                                       AS fiscal_year,
    COUNT(*)                                                    AS deals_won,
    ROUND(SUM(amount_inr), 2)                                   AS revenue_inr,
    ROUND(AVG(amount_inr), 2)                                   AS avg_deal_inr,
    SUM(closing_date_is_estimated)                              AS deals_with_estimated_date
FROM v_deals
WHERE is_won = 1
GROUP BY fiscal_year_effective
"""

VIEWS["v_open_pipeline"] = """
SELECT
    pipeline,
    stage,
    stage_order,
    COUNT(*)                                                    AS deal_count,
    ROUND(SUM(amount_inr), 2)                                   AS value_inr,
    ROUND(SUM(amount_inr * COALESCE(probability, 0) / 100.0), 2) AS weighted_inr
FROM v_deals
WHERE is_open = 1
GROUP BY pipeline, stage, stage_order
"""

VIEWS["v_pipeline_snapshot"] = """
SELECT
    s.snapshot_date,
    s.deal_id,
    s.stage,
    s.pipeline,
    s.amount_inr,
    s.probability,
    s.closing_date,
    s.owner_name
FROM pipeline_snapshots s
"""


def seed_targets(conn):
    """Copies config/targets.json into revenue_targets, but only while the
    table is empty - after that the table is the source of truth and is
    edited directly, so a redeploy never overwrites a changed target."""
    if conn.execute("SELECT 1 FROM revenue_targets LIMIT 1").fetchone():
        return 0
    if not os.path.exists(TARGETS_SEED_PATH):
        return 0
    with open(TARGETS_SEED_PATH, encoding="utf-8") as handle:
        cfg = json.load(handle)
    seed = [("company", k, v) for k, v in cfg.get("company_monthly_inr", {}).items()]
    seed += [("owner", k, v) for k, v in cfg.get("owner_monthly_inr", {}).items()]
    db.executemany(conn, "INSERT INTO revenue_targets (scope, key, target_inr) VALUES (?, ?, ?)", seed)
    return len(seed)


def main():
    conn = db.connect()
    db.ensure_schema(conn)
    seeded = seed_targets(conn)
    if seeded:
        print(f"  revenue_targets seeded with {seeded} rows from config/targets.json")

    # Dropped together and rebuilt in order: Postgres won't replace a view
    # whose columns changed, and the aggregates depend on v_deals.
    for name in reversed(list(VIEWS)):
        conn.execute(f"DROP VIEW IF EXISTS {name} CASCADE")
    for name, body in VIEWS.items():
        conn.execute(f"CREATE VIEW {name} AS {body}")
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        print(f"  {name:<24} {count:>7} rows")

    # Expression indexes the planner matches against the views' join keys -
    # weekly.journey joins leads to deals on the converted contact.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deals_contact_id
            ON records ((payload->'Contact_Name'->>'id'))
            WHERE module = 'Deals' AND is_deleted = 0;
        CREATE INDEX IF NOT EXISTS idx_leads_converted_contact_id
            ON records ((payload->'Converted_Contact'->>'id'))
            WHERE module = 'Leads' AND is_deleted = 0;
        CREATE INDEX IF NOT EXISTS idx_history_deal_id
            ON records ((payload->'Potential_Name'->>'id'))
            WHERE module = 'DealHistory' AND is_deleted = 0;
    """)
    conn.execute("ANALYZE records")

    conn.commit()
    conn.close()
    print(f"\n{len(VIEWS)} views built")


if __name__ == "__main__":
    main()
