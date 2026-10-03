"""Segment diagnostics - "how are we doing in X" for a pipeline, industry,
or country. One flexible function rather than three separate ones, since
all three are plausible readings of "area" in a question like "how do we
increase sales in X" - the caller picks the dimension from phrasing
("in Mumbai" -> country, "in BFSI" -> industry, "in Datasurfr" -> pipeline).

Deliberately excludes city as a dimension - same messy free-text problem
as industry (720 distinct values in this CRM, "Bangalore"/"Bengaluru",
"mumbai"/"Mumbai" case variants), plus a higher null rate (36% vs
country's 24%). Country is the geography granularity actually clean
enough to trust here.

Important asymmetry, by data structure, not choice: `pipeline` is a field
on Deals, so that diagnostic covers win rate and open pipeline by stage.
`industry` and `country` are fields on Leads, not Deals - nothing links a
deal back to the lead's industry/country in this CRM - so those two
diagnostics are necessarily about the lead-to-converted funnel, not deal
win rate. Don't blur these two kinds of "performance" together.

Both `industry` and `country` are free text, so the same segment shows up
under several exact-string variants ("USA"/"United States", "Cyber
Security"/"CyberSecurity"). Querying one exact string alone would silently
undercount. `_variants_of()` normalizes (lowercase, strip punctuation) and
matches every raw value that collapses to the same form, then queries all
of them together - this catches "USA"+"United States" generically, not
just the handful of cases spelled out by hand.
"""

import re

import weekly
from weekly import rows, one

MIN_SAMPLE = 30
LEAD_DIMENSIONS = {"industry": "industry", "country": "country"}

# Normalization (below) only catches case/punctuation/spacing duplicates -
# "Cyber Security" vs "CyberSecurity" collapse to the same string, but
# "USA" vs "United States" never will, no matter how it's stripped. These
# are genuine abbreviations, confirmed by checking the actual data
# (39 leads under "USA", 148 under "United States" - same country, two
# unrelated strings). Kept as a short, explicit, checked list rather than
# a guessed-at general abbreviation expander.
COUNTRY_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u.s.a": "united states",
    "uk": "united kingdom",
    "in": "india",
    "uae": "united arab emirates",
}


def _normalize(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _canonical(value):
    plain = (value or "").strip().lower()
    return COUNTRY_ALIASES.get(plain, plain)


def _variants_of(column, value):
    """Every distinct raw value in `column` that normalizes to the same
    string as `value` (catches case/spacing duplicates like "Cyber
    Security" vs "CyberSecurity"), plus - for country - known
    abbreviations resolved through COUNTRY_ALIASES first. Falls back to
    [value] itself if nothing in the data matches, so an exact lookup
    still works even for a one-off value this can't cluster.
    """
    canonical_target = _normalize(_canonical(value))
    target = _normalize(value)
    all_values = rows(f"SELECT DISTINCT {column} AS v FROM v_leads WHERE {column} IS NOT NULL")
    matches = [
        r["v"] for r in all_values
        if _normalize(_canonical(r["v"])) == canonical_target or _normalize(r["v"]) == target
    ]
    return matches or [value]


def segment_diagnostic(dimension, value):
    if dimension == "pipeline":
        return _pipeline_segment(value)
    if dimension in LEAD_DIMENSIONS:
        return _lead_segment(LEAD_DIMENSIONS[dimension], value)
    return {"error": f"dimension must be one of: pipeline, industry, country (got '{dimension}')"}


def _lead_segment(column, value):
    variants = _variants_of(column, value)
    placeholders = ",".join("?" for _ in variants)
    seg = one(f"SELECT COUNT(*) leads, SUM(is_converted) converted FROM v_leads WHERE {column} IN ({placeholders})", variants)
    overall = one("SELECT COUNT(*) leads, SUM(is_converted) converted FROM v_leads")

    if not seg or not seg["leads"]:
        return {"error": f"no leads found with {column} = '{value}'"}

    seg_rate = seg["converted"] / seg["leads"]
    overall_rate = overall["converted"] / overall["leads"]

    result = {
        "dimension": column,
        "value": value,
        "matched_variants": variants if len(variants) > 1 else None,
        "metric": "lead-to-converted-contact rate (not deal win rate - see note)",
        "note": ("Leads don't carry which pipeline/stage they ended up in, so this is "
                 "conversion-to-contact, not revenue. Deals don't carry industry/country "
                 "at all in this CRM, so a deal-level view of this segment isn't possible."),
        "leads": seg["leads"],
        "converted": seg["converted"],
        "conversion_rate": round(seg_rate, 4),
        "company_conversion_rate": round(overall_rate, 4),
        "vs_company": "above average" if seg_rate > overall_rate else "below average",
    }
    if seg["leads"] < MIN_SAMPLE:
        result["sample_size_warning"] = (
            f"Only {seg['leads']} leads for this segment - too small a sample to draw a "
            f"reliable conclusion from. Treat as indicative only."
        )
    return result


def _pipeline_segment(value):
    seg = one("""
        SELECT COUNT(*) deals, SUM(is_won) won, SUM(is_open) open_deals,
               ROUND(SUM(CASE WHEN is_won THEN amount_inr END), 2) won_inr,
               ROUND(SUM(CASE WHEN is_open THEN amount_inr END), 2) open_inr
        FROM v_deals WHERE pipeline = ?
    """, (value,))
    if not seg or not seg["deals"]:
        return {"error": f"no deals found for pipeline = '{value}'"}

    stages = rows("""
        SELECT stage, stage_order, COUNT(*) deal_count, ROUND(SUM(amount_inr), 2) value_inr
        FROM v_deals WHERE pipeline = ? AND is_open = 1
        GROUP BY stage, stage_order ORDER BY stage_order
    """, (value,))

    overall = one("SELECT SUM(is_won) won, COUNT(*) deals FROM v_deals")
    win_rate = seg["won"] / seg["deals"]
    overall_win_rate = overall["won"] / overall["deals"]

    result = {
        "dimension": "pipeline",
        "value": value,
        "metric": "deal win rate",
        "deals": seg["deals"],
        "won": seg["won"],
        "open_deals": seg["open_deals"],
        "win_rate": round(win_rate, 4),
        "company_win_rate": round(overall_win_rate, 4),
        "vs_company": "above average" if win_rate > overall_win_rate else "below average",
        "won_inr": seg["won_inr"],
        "open_inr": seg["open_inr"],
        "open_deals_by_stage": stages,
    }
    if seg["deals"] < MIN_SAMPLE:
        result["sample_size_warning"] = (
            f"Only {seg['deals']} deals for this pipeline - small sample, treat as indicative only."
        )
    return result
