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

Views are recreated on every run, so this is safe to re-run any time.
Usage: python3 scripts/transform.py
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "crm.db")

# Apr-Mar fiscal year label, e.g. 2026-04-15 -> FY2026-27
FISCAL_YEAR = """
    CASE WHEN CAST(strftime('%m', {d}) AS INTEGER) >= 4
        THEN 'FY' || strftime('%Y', {d}) || '-' ||
             substr(CAST(CAST(strftime('%Y', {d}) AS INTEGER) + 1 AS TEXT), 3, 2)
        ELSE 'FY' || CAST(CAST(strftime('%Y', {d}) AS INTEGER) - 1 AS TEXT) || '-' ||
             substr(strftime('%Y', {d}), 3, 2)
    END
"""

# Fixes the CRM's own spelling and strips the parenthetical qualifiers.
STAGE_CLEAN = """
    CASE json_extract(payload, '$.Stage')
        WHEN 'Demo (Pipleline)'                            THEN 'Demo'
        WHEN 'Negotation / Contract Finalization (Commit)'  THEN 'Negotiation / Contract Finalization'
        WHEN 'Proposal / Pricing Quote (Qualified)'         THEN 'Proposal / Pricing Quote'
        WHEN 'Need Analysis'                                THEN 'Needs Analysis'
        ELSE json_extract(payload, '$.Stage')
    END
"""

# Funnel ordering. Closed and parked states sort last so charts read correctly.
STAGE_ORDER = """
    CASE json_extract(payload, '$.Stage')
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
    json_extract(payload, '$.Deal_Name')                        AS deal_name,
    {STAGE_CLEAN}                                               AS stage,
    json_extract(payload, '$.Stage')                            AS stage_raw,
    {STAGE_ORDER}                                               AS stage_order,
    json_extract(payload, '$.Stage') = 'Closed Won'             AS is_won,
    json_extract(payload, '$.Stage') = 'Closed Lost'            AS is_lost,
    json_extract(payload, '$.Stage') NOT IN ('Closed Won', 'Closed Lost') AS is_open,
    json_extract(payload, '$.Pipeline')                         AS pipeline,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    json_extract(payload, '$.Owner.id')                         AS owner_id,
    json_extract(payload, '$.Account_Name.name')                AS account_name,
    json_extract(payload, '$.Account_Name.id')                  AS account_id,
    json_extract(payload, '$.Contact_Name.name')                AS contact_name,
    json_extract(payload, '$.Currency')                         AS currency,
    json_extract(payload, '$.Amount')                           AS amount_original,
    json_extract(payload, '$.Exchange_Rate')                    AS exchange_rate,
    -- Divide, not multiply: see module docstring, trap 1.
    CASE
        WHEN COALESCE(json_extract(payload, '$.Exchange_Rate'), 0) = 0 THEN NULL
        ELSE json_extract(payload, '$.Amount') / json_extract(payload, '$.Exchange_Rate')
    END                                                         AS amount_inr,
    json_extract(payload, '$.Probability')                      AS probability,
    date(json_extract(payload, '$.Closing_Date'))               AS closing_date,
    strftime('%Y-%m', json_extract(payload, '$.Closing_Date'))  AS closing_month,
    {FISCAL_YEAR.format(d="json_extract(payload, '$.Closing_Date')")} AS fiscal_year,
    json_extract(payload, '$.Financial_Year')                   AS fiscal_year_field,
    json_extract(payload, '$.Lead_Source')                      AS lead_source,
    json_extract(payload, '$.Opportunity_Type')                 AS opportunity_type,
    json_extract(payload, '$.Trial_Status')                      AS trial_status,
    date(json_extract(payload, '$.Trial_End_Date'))             AS trial_end_date,
    date(json_extract(payload, '$.Subscription_Start_Date'))    AS subscription_start_date,
    date(json_extract(payload, '$.Subscription_End_Date'))      AS subscription_end_date,
    date(json_extract(payload, '$.Proposal_Sent_Date'))         AS proposal_sent_date,
    date(json_extract(payload, '$.Expected_Closure_Date'))      AS expected_closure_date,
    json_extract(payload, '$.Sales_Cycle_Duration')             AS sales_cycle_days,
    json_extract(payload, '$.Overall_Sales_Duration')           AS overall_sales_days,
    json_extract(payload, '$.Hold_Reason')                      AS hold_reason,
    -- Lost_Reason holds billing frequency in this CRM, not loss causes.
    json_extract(payload, '$.Lost_Reason')                      AS lost_reason_misused,
    json_extract(payload, '$.Next_Step')                        AS next_step,
    datetime(json_extract(payload, '$.Last_Activity_Time'))     AS last_activity_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Deals' AND is_deleted = 0
"""

VIEWS["v_deal_history"] = f"""
SELECT
    id,
    json_extract(payload, '$.Potential_Name.id')                AS deal_id,
    json_extract(payload, '$.Potential_Name.name')              AS deal_name,
    json_extract(payload, '$.Stage')                            AS stage_raw,
    json_extract(payload, '$.Stage_Duration_Calendar_Days')     AS stage_duration_days,
    json_extract(payload, '$.Amount')                           AS amount_original,
    json_extract(payload, '$.Exchange_Rate')                    AS exchange_rate,
    CASE
        WHEN COALESCE(json_extract(payload, '$.Exchange_Rate'), 0) = 0 THEN NULL
        ELSE json_extract(payload, '$.Amount') / json_extract(payload, '$.Exchange_Rate')
    END                                                         AS amount_inr,
    json_extract(payload, '$.Probability')                      AS probability,
    date(json_extract(payload, '$.Closing_Date'))               AS closing_date,
    json_extract(payload, '$.Moved_To__s')                      AS moved_to,
    json_extract(payload, '$.Modified_By.name')                 AS changed_by,
    datetime(json_extract(payload, '$.Modified_Time'))          AS changed_at,
    {FISCAL_YEAR.format(d="json_extract(payload, '$.Modified_Time')")} AS fiscal_year
FROM records
WHERE module = 'DealHistory' AND is_deleted = 0
"""

VIEWS["v_leads"] = """
SELECT
    id,
    json_extract(payload, '$.Full_Name')                        AS full_name,
    json_extract(payload, '$.Company')                          AS company,
    json_extract(payload, '$.Email')                            AS email,
    json_extract(payload, '$.Lead_Status')                      AS lead_status,
    json_extract(payload, '$.Lead_Source')                      AS lead_source,
    json_extract(payload, '$.Industry')                         AS industry,
    json_extract(payload, '$.City')                             AS city,
    json_extract(payload, '$.Country')                          AS country,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    json_extract(payload, '$.Owner.id')                         AS owner_id,
    COALESCE(json_extract(payload, '$.Converted__s'), 0)        AS is_converted,
    date(json_extract(payload, '$.Converted_Date_Time'))        AS converted_date,
    json_extract(payload, '$.Converted_Deal.id')                AS converted_deal_id,
    json_extract(payload, '$.Converted_Account.id')             AS converted_account_id,
    json_extract(payload, '$.Converted_Contact.id')             AS converted_contact_id,
    -- Unlike Deals, Leads do carry a real Created_Time.
    datetime(json_extract(payload, '$.Created_Time'))           AS created_time,
    strftime('%Y-%m', json_extract(payload, '$.Created_Time'))  AS created_month,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Leads' AND is_deleted = 0
"""

VIEWS["v_accounts"] = """
SELECT
    id,
    json_extract(payload, '$.Account_Name')                     AS account_name,
    json_extract(payload, '$.Industry')                         AS industry,
    json_extract(payload, '$.Account_Type')                     AS account_type,
    json_extract(payload, '$.Billing_City')                     AS billing_city,
    json_extract(payload, '$.Billing_Country')                  AS billing_country,
    json_extract(payload, '$.Website')                          AS website,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    json_extract(payload, '$.Owner.id')                         AS owner_id,
    datetime(json_extract(payload, '$.Created_Time'))           AS created_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Accounts' AND is_deleted = 0
"""

VIEWS["v_contacts"] = """
SELECT
    id,
    json_extract(payload, '$.Full_Name')                        AS full_name,
    json_extract(payload, '$.Email')                            AS email,
    json_extract(payload, '$.Title')                            AS title,
    json_extract(payload, '$.Department')                       AS department,
    json_extract(payload, '$.Account_Name.name')                AS account_name,
    json_extract(payload, '$.Account_Name.id')                  AS account_id,
    json_extract(payload, '$.Lead_Source')                      AS lead_source,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    datetime(json_extract(payload, '$.Created_Time'))           AS created_time,
    modified_time,
    extracted_at
FROM records
WHERE module = 'Contacts' AND is_deleted = 0
"""

VIEWS["v_activities"] = """
SELECT
    module                                                      AS activity_type,
    id,
    COALESCE(
        json_extract(payload, '$.Subject'),
        json_extract(payload, '$.Event_Title')
    )                                                           AS subject,
    json_extract(payload, '$.Status')                           AS status,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    json_extract(payload, '$.Who_Id.name')                      AS related_contact,
    json_extract(payload, '$.What_Id.name')                      AS related_record,
    COALESCE(
        date(json_extract(payload, '$.Due_Date')),
        date(json_extract(payload, '$.Call_Start_Time')),
        date(json_extract(payload, '$.Start_DateTime'))
    )                                                           AS activity_date,
    json_extract(payload, '$.Call_Duration')                    AS call_duration,
    datetime(json_extract(payload, '$.Created_Time'))           AS created_time,
    modified_time
FROM records
WHERE module IN ('Tasks', 'Calls', 'Events') AND is_deleted = 0
"""

VIEWS["v_campaigns"] = """
SELECT
    id,
    json_extract(payload, '$.Campaign_Name')                    AS campaign_name,
    json_extract(payload, '$.Type')                             AS campaign_type,
    json_extract(payload, '$.Status')                           AS status,
    json_extract(payload, '$.Expected_Revenue')                 AS expected_revenue,
    json_extract(payload, '$.Budgeted_Cost')                    AS budgeted_cost,
    json_extract(payload, '$.Actual_Cost')                      AS actual_cost,
    date(json_extract(payload, '$.Start_Date'))                 AS start_date,
    date(json_extract(payload, '$.End_Date'))                   AS end_date,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    modified_time
FROM records
WHERE module = 'Campaigns' AND is_deleted = 0
"""

VIEWS["v_marketing_expense"] = """
SELECT
    id,
    json_extract(payload, '$.Name')                             AS name,
    json_extract(payload, '$.Owner.name')                       AS owner_name,
    payload                                                     AS raw_payload,
    modified_time
FROM records
WHERE module = 'Marketing_Expense' AND is_deleted = 0
"""

VIEWS["v_users"] = """
SELECT
    id,
    json_extract(payload, '$.full_name')                        AS full_name,
    json_extract(payload, '$.email')                            AS email,
    json_extract(payload, '$.role.name')                        AS role,
    json_extract(payload, '$.profile.name')                     AS profile,
    json_extract(payload, '$.status')                           AS status
FROM users
"""

# --- Convenience aggregates -------------------------------------------------

VIEWS["v_bookings_monthly"] = """
SELECT
    closing_month,
    fiscal_year,
    pipeline,
    COUNT(*)                                                    AS deals_won,
    ROUND(SUM(amount_inr), 2)                                   AS revenue_inr
FROM v_deals
WHERE is_won = 1 AND closing_date IS NOT NULL
GROUP BY closing_month, fiscal_year, pipeline
"""

VIEWS["v_bookings_fiscal"] = """
SELECT
    fiscal_year,
    COUNT(*)                                                    AS deals_won,
    ROUND(SUM(amount_inr), 2)                                   AS revenue_inr,
    ROUND(AVG(amount_inr), 2)                                   AS avg_deal_inr
FROM v_deals
WHERE is_won = 1 AND closing_date IS NOT NULL
GROUP BY fiscal_year
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


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found - run scripts/extract.py first.")
    conn = sqlite3.connect(DB_PATH)

    # Snapshot table must exist before the view that reads it.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_snapshots (
            snapshot_date TEXT NOT NULL,
            deal_id       TEXT NOT NULL,
            deal_name     TEXT,
            stage         TEXT,
            pipeline      TEXT,
            amount_inr    REAL,
            probability   INTEGER,
            closing_date  TEXT,
            owner_name    TEXT,
            PRIMARY KEY (snapshot_date, deal_id)
        );
    """)

    for name, body in VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.execute(f"CREATE VIEW {name} AS {body}")
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name:<24} {count:>7} rows")

    conn.commit()
    conn.close()
    print(f"\n{len(VIEWS)} views built in {DB_PATH}")


if __name__ == "__main__":
    main()
