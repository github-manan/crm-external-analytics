#!/usr/bin/env python3
"""Automatic "what's notable right now" scan - flags segments (pipelines,
industries, countries) whose performance diverges meaningfully from the
company average, so a question doesn't have to already know which segment
to ask about. Built entirely on segments.py's existing, tested
segment_diagnostic() - same MIN_SAMPLE threshold, same company-average
baseline, same variant-merging and country-alias normalization - this
only automates WHICH segments get checked, not how any one of them gets
scored.

DIVERGENCE_POINTS (10 percentage points) is a deliberately simple,
disclosed cutoff, not a statistical significance test - sample sizes here
are too small and uneven for one. A finding here means "worth a look",
same spirit as every sample_size_warning elsewhere in this project, not
a verdict.
"""

import weekly
import segments
from weekly import rows

DIVERGENCE_POINTS = 10
PIPELINES = ("Consulting", "Datasurfr", "MSS", "Renewal")
TOP_N_INDUSTRIES = 6
TOP_N_COUNTRIES = 6

# Floor before a rep's stuck-deal rate is trusted enough to compare against
# the company average - same spirit as segments.py's MIN_SAMPLE, just a
# much lower bar since open-deal counts per rep are small to begin with.
MIN_OPEN_DEALS = 3


def _divergence(dimension, value):
    result = segments.segment_diagnostic(dimension, value)
    if result.get("error") or result.get("sample_size_warning"):
        return None
    rate = result.get("win_rate", result.get("conversion_rate"))
    company_rate = result.get("company_win_rate", result.get("company_conversion_rate"))
    diff_points = round((rate - company_rate) * 100, 1)
    if abs(diff_points) < DIVERGENCE_POINTS:
        return None
    metric = "win rate" if dimension == "pipeline" else "conversion rate"
    direction = "above" if diff_points > 0 else "below"
    rate_pct = f"{rate * 100:.1f}%"
    return {
        "category": "segment_divergence",
        "dimension": dimension,
        "value": value,
        "metric": metric,
        "rate": rate,
        "rate_pct": rate_pct,
        "company_rate": company_rate,
        "company_rate_pct": f"{company_rate * 100:.1f}%",
        "diff_points": diff_points,
        "direction": direction,
        # Computed here, not inferred from the sign of diff_points by a
        # caller (chart, chatbot, anything) - "above average" is good for a
        # win/conversion rate, but for stuck_deal_rate below it's the good
        # side (see stuck_rate_divergences) - the two categories invert.
        "favorable": diff_points > 0,
        "label": f"{value} ({dimension})",
        "headline": f"{value}: {rate_pct} {metric} ({direction} company average by {abs(diff_points)}pts)",
    }


def stuck_rate_divergences():
    """Each rep's share of open deals with no stage change in
    weekly.STUCK_AFTER_DAYS (the same stuck-deal definition the Weekly
    Review dashboard already uses, not a new one) vs. the company-wide
    weighted average - flags reps whose open pipeline is notably fresher
    or notably staler than peers. Requires >= MIN_OPEN_DEALS open deals.

    Checked real data before building this: nearly every rep sits at
    80-100% stuck by this definition (a known, already-tracked company-
    wide gap - see data_quality.py's recent_activity metric), so a flat
    "who's above X%" threshold would flag almost everyone and tell nobody
    anything. Comparing against the company average instead only
    surfaces genuine outliers - in practice, right now, that means reps
    whose pipeline is notably FRESHER than peers, not staler (there isn't
    enough spread on the staler side to mean anything).
    """
    rows_ = rows(f"""
        SELECT d.owner_name,
               COUNT(*) AS open_deals,
               SUM(CASE WHEN last_change IS NULL
                        OR julianday('now') - julianday(last_change) >= {weekly.STUCK_AFTER_DAYS}
                   THEN 1 ELSE 0 END) AS stuck
        FROM (
            SELECT d.id, d.owner_name, MAX(h.changed_at) AS last_change
            FROM v_deals d LEFT JOIN v_deal_history h ON h.deal_id = d.id
            WHERE d.is_open = 1 AND d.owner_name IS NOT NULL
            GROUP BY d.id
        ) d
        GROUP BY owner_name HAVING open_deals >= ?
    """, (MIN_OPEN_DEALS,))
    if not rows_:
        return []

    total_open = sum(r["open_deals"] for r in rows_)
    total_stuck = sum(r["stuck"] for r in rows_)
    company_rate = total_stuck / total_open if total_open else 0

    findings = []
    for r in rows_:
        rate = r["stuck"] / r["open_deals"]
        diff_points = round((rate - company_rate) * 100, 1)
        if abs(diff_points) < DIVERGENCE_POINTS:
            continue
        direction = "staler than" if diff_points > 0 else "fresher than"
        rate_pct = f"{rate * 100:.0f}%"
        findings.append({
            "category": "stuck_deal_rate",
            "owner_name": r["owner_name"],
            "open_deals": r["open_deals"],
            "stuck_deals": r["stuck"],
            "rate_pct": rate_pct,
            "company_rate_pct": f"{company_rate * 100:.0f}%",
            "diff_points": diff_points,
            "direction": direction,
            # Inverted vs segment_divergence: here a HIGHER stuck-rate is
            # worse, so "favorable" is the opposite sign test.
            "favorable": diff_points < 0,
            "label": f"{r['owner_name']} (stuck rate)",
            "headline": (f"{r['owner_name']}: {r['stuck']}/{r['open_deals']} open deals stuck 21+ days "
                         f"({rate_pct}, {direction} the {company_rate * 100:.0f}% company average)"),
        })
    return findings


def _top_values(column, limit):
    """Top values by volume, excluding anything below segments.MIN_SAMPLE -
    scanning a segment too small to trust would just reproduce the
    sample_size_warning path for nothing."""
    return [r["v"] for r in rows(
        f"""SELECT {column} AS v, COUNT(*) AS n FROM v_leads
            WHERE {column} IS NOT NULL GROUP BY {column}
            HAVING n >= ? ORDER BY n DESC LIMIT ?""",
        (segments.MIN_SAMPLE, limit),
    )]


def scan(owner=None):
    """owner, if given, restricts the stuck_deal_rate category (which
    names individual reps) to just that one person - segment_divergence
    findings are company-wide and never restricted, same policy as
    segment_diagnostic itself (pipeline/industry/country rates don't name
    anyone)."""
    findings = []
    for pipeline in PIPELINES:
        hit = _divergence("pipeline", pipeline)
        if hit:
            findings.append(hit)
    for industry in _top_values("industry", TOP_N_INDUSTRIES):
        hit = _divergence("industry", industry)
        if hit:
            findings.append(hit)
    for country in _top_values("country", TOP_N_COUNTRIES):
        hit = _divergence("country", country)
        if hit:
            findings.append(hit)

    stuck_findings = stuck_rate_divergences()
    if owner:
        stuck_findings = [f for f in stuck_findings if f["owner_name"] == owner]
    findings.extend(stuck_findings)

    findings.sort(key=lambda f: abs(f["diff_points"]), reverse=True)
    return findings
