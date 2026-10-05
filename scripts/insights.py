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

import segments
from weekly import rows

DIVERGENCE_POINTS = 10
PIPELINES = ("Consulting", "Datasurfr", "MSS", "Renewal")
TOP_N_INDUSTRIES = 6
TOP_N_COUNTRIES = 6


def _divergence(dimension, value):
    result = segments.segment_diagnostic(dimension, value)
    if result.get("error") or result.get("sample_size_warning"):
        return None
    rate = result.get("win_rate", result.get("conversion_rate"))
    company_rate = result.get("company_win_rate", result.get("company_conversion_rate"))
    diff_points = round((rate - company_rate) * 100, 1)
    if abs(diff_points) < DIVERGENCE_POINTS:
        return None
    return {
        "dimension": dimension,
        "value": value,
        "metric": "win rate" if dimension == "pipeline" else "conversion rate",
        "rate": rate,
        "rate_pct": f"{rate * 100:.1f}%",
        "company_rate": company_rate,
        "company_rate_pct": f"{company_rate * 100:.1f}%",
        "diff_points": diff_points,
        "direction": "above" if diff_points > 0 else "below",
    }


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


def scan():
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
    findings.sort(key=lambda f: abs(f["diff_points"]), reverse=True)
    return findings
