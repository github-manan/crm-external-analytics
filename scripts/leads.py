"""Hot-lead scoring - "which lead should I pick up first" helper for the
chatbot. Came out of a recorded discussion about prioritizing leads that
are both recently active and likely to convert.

This is a heuristic, not a trained model, and every answer should be
labeled as such. It combines:

  - recency       how recently the lead record was last touched
  - source_rate   historical conversion rate of that lead's source
  - industry_rate historical conversion rate of that lead's industry

Why not real engagement data (last call, last email, last site visit)?
This CRM barely logs activity at all - 6 calls and 3 events, total, across
21 reps (see README). There is no genuine "the prospect just engaged with
us" signal to score against. `modified_time` (last time the record itself
was edited) is the best available substitute, not a true engagement
signal - a modified record doesn't prove the lead did anything.

Why a minimum sample size before trusting a source/industry rate? The
`industry` field is free text with a very long, messy tail - hundreds of
near-duplicate one-off values ("Cyber Security" / "CyberSecurity" /
"CYber Security" each with a single lead). A rate computed from 1-2 leads
is noise, not signal, so anything under MIN_SAMPLE falls back to the
overall average instead.
"""

import math

import weekly
from weekly import rows, one, owner_clause

MIN_SAMPLE = 30
DEAD_STATUSES = ("Junk Lead", "Lost Lead", "Already Client")
RECENCY_HALFLIFE_DAYS = 30  # score halves every ~30 days since last update


def _rate_table(column):
    """{value: historical conversion rate}, falling back to the overall
    rate for any value seen fewer than MIN_SAMPLE times."""
    counted = rows(f"""
        SELECT {column} AS key, COUNT(*) n, SUM(is_converted) conv
        FROM v_leads WHERE {column} IS NOT NULL
        GROUP BY {column}
    """)
    overall = one("SELECT CAST(SUM(is_converted) AS FLOAT) / COUNT(*) AS rate FROM v_leads")["rate"]
    table = {r["key"]: (r["conv"] / r["n"] if r["n"] >= MIN_SAMPLE else overall) for r in counted}
    return table, overall


def hot_leads(owner=None, limit=20):
    source_rates, overall_source = _rate_table("lead_source")
    industry_rates, overall_industry = _rate_table("industry")

    status_list = ",".join("?" for _ in DEAD_STATUSES)
    w, p = owner_clause(owner)
    live = rows(f"""
        SELECT id, full_name, company, lead_source, industry, lead_status,
               owner_name, modified_time,
               CAST(julianday('now') - julianday(modified_time) AS REAL) days_since_update
        FROM v_leads
        WHERE is_converted = 0
          AND (lead_status IS NULL OR lead_status NOT IN ({status_list})){w}
    """, list(DEAD_STATUSES) + p)

    scored = []
    for lead in live:
        days = lead["days_since_update"] if lead["days_since_update"] is not None else 9999
        recency = math.exp(-days / RECENCY_HALFLIFE_DAYS)
        source_rate = source_rates.get(lead["lead_source"], overall_source)
        industry_rate = industry_rates.get(lead["industry"], overall_industry)
        score = 100 * (0.5 * recency + 0.25 * source_rate + 0.25 * industry_rate)
        scored.append({
            "lead_id": lead["id"],
            "name": lead["full_name"],
            "company": lead["company"],
            "owner_name": lead["owner_name"],
            "lead_source": lead["lead_source"],
            "industry": lead["industry"],
            "lead_status": lead["lead_status"],
            "days_since_update": round(days, 1),
            "score": round(score, 1),
            "source_conversion_rate": round(source_rate, 3),
            "industry_conversion_rate": round(industry_rate, 3),
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
