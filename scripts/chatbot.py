"""Local AI assistant over the CRM data - Phase 2 of the original project plan.

Design principle (from the project proposal): the model never gets raw
database access. It only ever sees a fixed set of tools, each one a thin
wrapper around an already-tested query from weekly.py / ist.py / this
codebase's own views - the same numbers the dashboards show, nothing a
model could invent or get wrong by writing bad SQL.

Runs entirely local via Ollama (http://localhost:11434) - no CRM data is
ever sent to an external LLM provider, per the confirmed project
constraint. Read-only: nothing here can write to Zoho or to data/crm.db.

One deliberate exception to "nothing leaves this machine": search_company_background
sends a company name to DuckDuckGo's public search (no API key, no account,
via the `ddgs` package) so a rep can look up public background on a lead
before contacting them. That's a company name leaving this machine, not
CRM record data - approved explicitly as a separate decision from the
no-external-LLM rule, not bundled into it.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly
import ist
import leads
import segments
import contacts
import data_quality
from ddgs import DDGS

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = os.environ.get("CHATBOT_MODEL", "llama3.2:3b")

TODAY_NOTE = "Dates in this CRM's data run up to whatever the last daily sync captured - use get_weekly_kpis or ask the user for a specific period rather than assuming what 'today' is."

SYSTEM_PROMPT = f"""You are a read-only analytics assistant for MitKat Advisory's sales CRM data.

Hard rules:
1. Only state numbers that came from a tool call in this conversation. Never estimate, round to a "nice" number, or fill in a gap - if a tool didn't return something, say you don't have it.
2. You cannot write, change, or create anything in the CRM. You only ever read.
2b. search_company_background is the one exception to "nothing leaves this machine" - it sends a company name to a public web search. Only ever pass a company name to it, never a person's name or any other CRM field.
3. Deals span 4 pipelines (Consulting, Datasurfr, MSS, Renewal) with different stage sets - never add numbers across pipelines yourself unless a tool already did it for you.
3b. Never sum a list of numbers or pick the "highest"/"top" row from a list yourself - you have gotten this wrong before. Several tools already include a pre-computed answer field for exactly this (total_leads, top_by_revenue, top_by_deals_won, highest_open_value_pipeline, highest_won_value_pipeline) - use those. If a tool doesn't have one, say what you can and note you can't total/compare it reliably.
4. This data has known gaps - if asked something these tools can't answer (why a deal was lost, which rep's activity predicts a closed deal, an exact reason a lead didn't convert), say plainly that the data doesn't support that, rather than guessing.
4b. You have, once, invented two entire revenue figures and a % change for a "compare this month to last month" question, from a tool that had no such data at all. If no tool result actually contains the comparison/number being asked for, say you don't have it - do not produce a plausible-sounding figure from nothing.
5. {TODAY_NOTE} You have also stated a fabricated date range ("Dec 11-17") for "last week" that matched nothing in the actual data. Tool results that include week_start/week_end or similar date fields carry the real dates - quote those, never invent a date range yourself.
6. Money is in Indian Rupees. Every "*_inr" field comes with a "*_inr_fmt" sibling (e.g. revenue_inr_fmt: "₹80.78 Cr") - always quote that pre-formatted string exactly as given. Do not convert the raw rupee number yourself; you have gotten that arithmetic wrong before.
7. When useful, briefly name which figure/tool your answer is based on, so it's checkable.
8. get_data_quality_progress's each metric has a "trend" field - if it says "NO TREND YET", say plainly that tracking just started and there's nothing to compare yet. Never say a number is "up", "improving", or "increasing" unless that metric's own "trend" field says "improved" - you have invented "up from previous periods" out of nothing before.

Keep answers short and direct - this is a chat widget, not a report."""


NULLISH = {"null", "none", "n/a", "na", "undefined", ""}


def clean_args(args):
    """Small models frequently pass the literal string "null" (or similar)
    for an omitted optional argument instead of leaving it out. Left alone,
    that string is truthy in Python and gets used as a real filter value -
    e.g. a lead/deal query silently filtering for owner == "null", matching
    nothing, and the model then reporting "no leads" as if that were real.
    """
    return {k: v for k, v in args.items() if str(v).strip().lower() not in NULLISH}


def fmt_inr(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def add_formatted_money(obj):
    """Walks tool results and adds a '<field>_fmt' sibling next to every
    '<field>_inr' key, e.g. revenue_inr -> revenue_inr_fmt: "₹80.78 Cr".
    The model is instructed to relay these strings rather than convert
    raw rupee figures itself - it kept getting that arithmetic wrong
    (stating ₹80.78 Cr as "₹809.4 Cr", a 10x error).
    """
    if isinstance(obj, list):
        return [add_formatted_money(item) for item in obj]
    if isinstance(obj, dict):
        out = dict(obj)
        for key, value in obj.items():
            if key.endswith("_inr") and isinstance(value, (int, float)):
                out[f"{key}_fmt"] = fmt_inr(value)
            elif isinstance(value, (list, dict)):
                out[key] = add_formatted_money(value)
        return out
    return obj


def call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    result = result if isinstance(result, (list, dict)) else {"value": result}
    return add_formatted_money(result)


def strip_raw_money(obj):
    """Deletes every '<field>_inr' key, keeping only its '<field>_inr_fmt'
    sibling. Applied as the very last step before a tool result is shown to
    the model - after any Python-side aggregation (max/sort/sum) in the
    tool functions has already used the raw numbers. Telling the model to
    "prefer the _fmt field" wasn't reliable; it kept re-deriving Cr/L from
    the raw rupee figure anyway and getting it wrong (off by 10x, twice).
    With the raw number gone, there's nothing left to compute wrong.
    """
    if isinstance(obj, list):
        return [strip_raw_money(item) for item in obj]
    if isinstance(obj, dict):
        return {
            k: strip_raw_money(v) for k, v in obj.items()
            if not (k.endswith("_inr") and f"{k}_fmt" in obj)
        }
    return obj


def tool_bookings_fiscal(_args):
    return call(weekly.rows, "SELECT * FROM v_bookings_fiscal ORDER BY fiscal_year")


def tool_bookings_monthly(args):
    pipeline = args.get("pipeline")
    if pipeline:
        return call(weekly.rows,
                     "SELECT * FROM v_bookings_monthly WHERE pipeline = ? ORDER BY closing_month",
                     (pipeline,))
    return call(weekly.rows, """
        SELECT closing_month, fiscal_year, SUM(deals_won) deals_won, ROUND(SUM(revenue_inr),2) revenue_inr
        FROM v_bookings_monthly GROUP BY closing_month, fiscal_year ORDER BY closing_month
    """)


def tool_pipeline_overview(_args):
    rows = call(weekly.rows, """
        SELECT pipeline, COUNT(*) deals, SUM(is_won) won, SUM(is_open) open_deals,
               ROUND(SUM(CASE WHEN is_won THEN amount_inr END),2) won_inr,
               ROUND(SUM(CASE WHEN is_open THEN amount_inr END),2) open_inr
        FROM v_deals WHERE pipeline IS NOT NULL GROUP BY pipeline ORDER BY won_inr DESC
    """)
    # Picked here in Python, not left for the model to scan a list and
    # compare - it has gotten "which row is highest" wrong before.
    return {
        "pipelines": rows,
        "highest_open_value_pipeline": max(rows, key=lambda r: r["open_inr"] or 0),
        "highest_won_value_pipeline": max(rows, key=lambda r: r["won_inr"] or 0),
    }


def tool_deals_by_stage(args):
    return call(ist.deals_by_stage, args.get("owner"))


def tool_rep_performance(args):
    fy = args.get("fiscal_year")
    clause = "AND fiscal_year = ?" if fy else ""
    params = (fy,) if fy else ()
    rows = call(weekly.rows, f"""
        SELECT owner_name, SUM(is_won) deals_won,
               ROUND(SUM(CASE WHEN is_won THEN amount_inr END),2) revenue_inr,
               SUM(is_open) deals_open,
               ROUND(SUM(CASE WHEN is_open THEN amount_inr END),2) pipeline_inr
        FROM v_deals WHERE owner_name IS NOT NULL {clause}
        GROUP BY owner_name ORDER BY revenue_inr DESC
    """, params)
    by_revenue = [r for r in rows if r["revenue_inr"]]
    by_deals_won = sorted(rows, key=lambda r: r["deals_won"] or 0, reverse=True)
    # Picked here in Python, not left for the model to scan the list itself
    # (it has picked the wrong "top" row before, even pre-sorted).
    return {
        "reps": rows,
        "top_by_revenue": by_revenue[0] if by_revenue else None,
        "top_by_deals_won": by_deals_won[0] if by_deals_won else None,
    }


def tool_lead_status(args):
    rows = call(ist.lead_status, args.get("owner"))
    # Summed here in Python - the model has miscounted a status breakdown
    # itself before (said 10,757 when the real total was 10,402).
    return {"by_status": rows, "total_leads": sum(r["n"] for r in rows)}


def tool_total_leads(args):
    rows = ist.lead_status(args.get("owner"))
    return {"total_leads": sum(r["n"] for r in rows)}


def tool_leads_by_source(args):
    rows = call(weekly.lead_sources, None, args.get("owner"), args.get("scope", "all"))
    return {"by_source": rows, "top_source": rows[0] if rows else None}


def tool_lead_journey(args):
    return call(weekly.journey, None, args.get("owner"), args.get("scope", "all"))


def tool_revenue_target(args):
    result = call(weekly.target, args.get("month"), args.get("owner"))
    # Verdict computed here, not left for the model - it has stated "behind
    # target" for a month that was actually at 259% of target (correct
    # numbers, backwards conclusion).
    pct = result.get("achieved_pct")
    if pct is None:
        result["status"] = "no target configured for this period"
    elif pct >= 100:
        result["status"] = f"ahead of target ({pct}% achieved)"
    else:
        result["status"] = f"behind target ({pct}% achieved)"
    return result


def tool_revenue_comparison(args):
    """This month vs the previous calendar month, computed here - not left
    for the model to pick two rows out of get_bookings_monthly's ~90-row
    series itself. It once invented both figures from nowhere for this
    exact question instead.

    Looks up the two months explicitly by the real current date rather
    than taking the last row of the series - a stray future-dated Closed
    Won deal (data entry error, or a real forward-dated close) can put a
    row past the actual current month, which "just take the last row"
    would silently mistake for "this month".
    """
    pipeline = args.get("pipeline")
    clause = "AND pipeline = ?" if pipeline else ""
    params_extra = (pipeline,) if pipeline else ()

    today = weekly.date.today()
    this_key = today.strftime("%Y-%m")
    prev_key = (today.replace(day=1) - weekly.timedelta(days=1)).strftime("%Y-%m")

    def month_row(key):
        r = weekly.one(f"""
            SELECT ? closing_month, COALESCE(SUM(deals_won),0) deals_won,
                   ROUND(COALESCE(SUM(revenue_inr),0),2) revenue_inr
            FROM v_bookings_monthly WHERE closing_month = ? {clause}
        """, (key, key) + params_extra)
        return r or {"closing_month": key, "deals_won": 0, "revenue_inr": 0.0}

    this_month, last_month = month_row(this_key), month_row(prev_key)
    delta_pct = None
    if last_month["revenue_inr"]:
        delta_pct = round((this_month["revenue_inr"] - last_month["revenue_inr"]) / last_month["revenue_inr"] * 100, 1)
    return add_formatted_money({
        "this_month": this_month,
        "last_month": last_month,
        "change_pct": delta_pct,
        "direction": "up" if (delta_pct or 0) >= 0 else "down",
    })


def tool_weekly_kpis(args):
    return call(weekly.kpis, args.get("week"), args.get("owner"))


def tool_needs_attention(args):
    owner = args.get("owner")
    return add_formatted_money({
        "stuck_deals": weekly.stuck_deals(owner)[:10],
        "closing_within_30_days": weekly.closing_soon(owner)[:10],
    })


def tool_list_reps(_args):
    return {"reps": weekly.owners()}


def tool_hot_leads(args):
    owner = args.get("owner")
    limit = min(int(args.get("limit", 10) or 10), 50)
    rows = leads.hot_leads(owner, limit)
    return {
        "note": ("Heuristic score, not a prediction - combines how recently the lead "
                 "was last updated with how well its source/industry have historically "
                 "converted. This CRM doesn't log enough call/email activity to score "
                 "true engagement, so 'recently updated' is the best available stand-in."),
        "leads": rows,
    }


def tool_search_company_background(args):
    """The one tool that sends data off this machine - a company name goes
    to DuckDuckGo's public search (ddgs package, no key/account needed).
    Never pass lead/contact personal names here, only the company name.
    """
    query = (args.get("company") or "").strip()
    if not query:
        return {"error": "no company name given"}
    try:
        results = DDGS().text(f"{query} company", max_results=5)
    except Exception as error:  # noqa: BLE001 - network call, surface failure don't crash the loop
        return {"error": f"search failed: {error}"}
    return {
        "note": "Public web search results, external to this CRM - verify before relying on them.",
        "query": query,
        "results": [{"title": r.get("title"), "snippet": r.get("body"), "url": r.get("href")} for r in results],
    }


def tool_segment_diagnostic(args):
    dimension = (args.get("dimension") or "").strip().lower()
    value = (args.get("value") or "").strip()
    if not dimension or not value:
        return {"error": "need both dimension (pipeline/industry/country) and value"}
    return segments.segment_diagnostic(dimension, value)


def tool_account_contacts(args):
    account = (args.get("account_name") or "").strip()
    if not account:
        return {"error": "need an account_name"}
    return contacts.account_contacts(account)


def tool_data_quality(_args):
    return data_quality.current()


TOOLS = {
    "get_hot_leads": tool_hot_leads,
    "search_company_background": tool_search_company_background,
    "get_segment_diagnostic": tool_segment_diagnostic,
    "get_account_contacts": tool_account_contacts,
    "get_data_quality_progress": tool_data_quality,
    "get_bookings_by_fiscal_year": tool_bookings_fiscal,
    "get_bookings_monthly": tool_bookings_monthly,
    "get_pipeline_overview": tool_pipeline_overview,
    "get_deals_by_stage": tool_deals_by_stage,
    "get_rep_performance": tool_rep_performance,
    "get_lead_status_breakdown": tool_lead_status,
    "get_total_leads": tool_total_leads,
    "get_leads_by_source": tool_leads_by_source,
    "get_lead_journey": tool_lead_journey,
    "get_revenue_target": tool_revenue_target,
    "get_revenue_comparison": tool_revenue_comparison,
    "get_weekly_kpis": tool_weekly_kpis,
    "get_things_needing_attention": tool_needs_attention,
    "list_sales_reps": tool_list_reps,
}

TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "get_bookings_by_fiscal_year",
        "description": "Won deals and revenue, one row per fiscal year (Apr-Mar), all pipelines combined. Good for 'how are we doing this year / historically' questions.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_bookings_monthly",
        "description": "Won deals and revenue by calendar month. Optionally filter to one pipeline.",
        "parameters": {"type": "object", "properties": {
            "pipeline": {"type": "string", "description": "One of Consulting, Datasurfr, MSS, Renewal. Omit for all pipelines combined."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_pipeline_overview",
        "description": "Totals per pipeline (Consulting/Datasurfr/MSS/Renewal): deal count, won count+value, open count+value. Includes highest_open_value_pipeline and highest_won_value_pipeline, already picked out - use those directly for 'which pipeline has the most X' questions, don't compare the pipelines list yourself.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_deals_by_stage",
        "description": "Every deal's current stage, grouped by pipeline and stage (includes Closed Won/Lost, not just open). Each row is already tagged with its own pipeline - never sum stage counts across different pipelines yourself.",
        "parameters": {"type": "object", "properties": {
            "owner": {"type": "string", "description": "Filter to one sales rep's deals. Omit for everyone."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_rep_performance",
        "description": "Per-rep deals won/revenue and open deals/pipeline value. Includes top_by_revenue and top_by_deals_won, already picked out - use those directly for 'who is the top performer' questions, don't scan the reps list yourself.",
        "parameters": {"type": "object", "properties": {
            "fiscal_year": {"type": "string", "description": "e.g. 'FY2026-27'. Omit for all-time."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_total_leads",
        "description": "Just the single total lead count. Use this for 'how many leads do we have' - don't add up get_lead_status_breakdown's rows yourself.",
        "parameters": {"type": "object", "properties": {
            "owner": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_lead_status_breakdown",
        "description": "Lead counts grouped by Lead Status (e.g. Junk Lead, Contacted, None), plus a pre-summed total_leads field. Useful for lead-quality questions ('how many are junk'); for a plain total-leads question use get_total_leads instead.",
        "parameters": {"type": "object", "properties": {
            "owner": {"type": "string", "description": "Filter to one rep's leads. Omit for everyone."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_leads_by_source",
        "description": "Lead counts grouped by Lead Source (Website, Referral, D-0, etc), plus top_source already picked out - use it directly for 'which source brings the most leads' questions.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["all", "week"], "description": "'all' = all-time (default), 'week' = current week only."},
            "owner": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_lead_journey",
        "description": "The funnel: Lead -> Contacted -> Converted -> Action (has a deal) -> Won, with conversion % at each step. This is THE way to answer any lead-conversion-rate question.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["all", "week"], "description": "'all' = the standing funnel (default, more meaningful), 'week' = only leads created this week."},
            "owner": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_revenue_target",
        "description": "Revenue target vs. actual for a given month (company-wide, or one rep), with a pre-computed 'status' field ('ahead of target' / 'behind target') - use that directly, don't work out ahead-vs-behind yourself from the percentage. Targets are manually configured, not from Zoho. This does NOT compare to a previous month - use get_revenue_comparison for that.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "YYYY-MM, e.g. '2026-09'. Omit for the current month."},
            "owner": {"type": "string", "description": "One rep's name for their individual target. Omit for the company target."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_revenue_comparison",
        "description": "This month's revenue vs. last month's, with change_pct already computed. The ONLY correct tool for 'how does this month compare to last month' style questions - never answer that from get_revenue_target or by inventing numbers.",
        "parameters": {"type": "object", "properties": {
            "pipeline": {"type": "string", "description": "One of Consulting, Datasurfr, MSS, Renewal. Omit for all pipelines combined."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_weekly_kpis",
        "description": "This week's leads/revenue/pipeline/new-accounts vs. the previous week, with % change. Good for 'how's this week going' questions.",
        "parameters": {"type": "object", "properties": {
            "week": {"type": "string", "description": "Any date (YYYY-MM-DD) inside the target week. Omit for the current week."},
            "owner": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_things_needing_attention",
        "description": "Open deals stuck with no stage change in 21+ days, and deals closing within 30 days. Good for 'what should I focus on' questions.",
        "parameters": {"type": "object", "properties": {
            "owner": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "list_sales_reps",
        "description": "The exact list of sales rep names as they appear in the CRM. Call this before filtering by a rep name you're not 100% sure is spelled/cased correctly.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_hot_leads",
        "description": "Ranks currently-open leads by a heuristic 'hotness' score (recency of update + historical conversion rate of their source/industry). Use for 'which lead should I pick up next' or 'what are our hottest leads' questions. This is a heuristic, not a prediction - always mention that when answering.",
        "parameters": {"type": "object", "properties": {
            "owner": {"type": "string", "description": "Filter to one rep's leads. Omit for everyone."},
            "limit": {"type": "integer", "description": "How many leads to return, default 10."},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_company_background",
        "description": "Public web search for background on a company (what they do, size, news) - use before suggesting a rep contact a lead, to give them context. This is the ONLY tool that sends anything (the company name) outside this machine - never pass a person's name, only the company.",
        "parameters": {"type": "object", "properties": {
            "company": {"type": "string", "description": "Company name to search for."},
        }, "required": ["company"]},
    }},
    {"type": "function", "function": {
        "name": "get_segment_diagnostic",
        "description": "How a specific segment is performing vs. the company average - use for ANY 'how are we doing in X' / 'how do we increase sales in X' / 'leads in X' / 'conversion in X' question, for ANY X, including a country or region name. Pick dimension by what kind of thing X is: a COUNTRY OR PLACE NAME (India, US, USA, UK, Singapore, Qatar, Mumbai, Middle East, APAC, any nation/region/city) -> dimension='country'; an INDUSTRY/VERTICAL (BFSI, Technology, Manufacturing, Healthcare) -> dimension='industry'; one of our 4 named pipelines (Consulting, Datasurfr, MSS, Renewal) -> dimension='pipeline'. If X is a place name, ALWAYS use 'country', never 'industry' - this has been picked wrong before. Pipeline gives deal win rate; industry/country give lead conversion rate, NOT deal revenue (deals here don't carry industry/country). Always check for a sample_size_warning before treating the result as reliable.",
        "parameters": {"type": "object", "properties": {
            "dimension": {"type": "string", "enum": ["pipeline", "industry", "country"]},
            "value": {"type": "string", "description": "The specific segment name, e.g. 'Datasurfr', 'BFSI', 'India', 'United States'."},
        }, "required": ["dimension", "value"]},
    }},
    {"type": "function", "function": {
        "name": "get_account_contacts",
        "description": "Who to actually contact at a specific account/company - returns name and email (this CRM doesn't track role/title for contacts). Use for 'who should we contact at X' questions. Tries an exact match first, falls back to a partial match - if that's ambiguous you'll get a did_you_mean list back instead of contacts.",
        "parameters": {"type": "object", "properties": {
            "account_name": {"type": "string"},
        }, "required": ["account_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_data_quality_progress",
        "description": "How complete the CRM's own data entry is right now, and whether that's improved since this started being tracked - activity logging on open deals, real loss reasons on Closed Lost deals, closing dates on open deals, and subscription end dates on Renewal-pipeline deals. Use for 'is the data getting better / is the team filling things in' questions - NOT a sales metric, this is about data entry completeness.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def _post(payload):
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


# --- Headlines: the guaranteed-correct answer, built here in Python -----
#
# The model reliably picks the right tool and the right row/verdict now,
# but has repeatedly mis-transcribed the actual digits of a number even
# when quoting a pre-formatted string verbatim (₹24.69 Cr stated as
# ₹924.69 Cr, ₹92.00 L as ₹9.20 L or ₹992.00 L - a different slip each
# time, so it's not one fixable typo). A bigger local model didn't remove
# this, only changed which digits got mangled, and made it much slower.
#
# So the number the user actually sees as *the* answer is built here,
# directly from verified tool data - never generated by the model. The
# model's own sentence still renders too, as supporting commentary, but
# it is not the source of truth for any figure anymore.

def _headline_rep_performance(data):
    top = data.get("top_by_revenue")
    return f"Top by revenue: {top['owner_name']} ({top.get('revenue_inr_fmt', 'n/a')})" if top else None


def _headline_pipeline_overview(data):
    top = data.get("highest_open_value_pipeline")
    return f"Highest open value: {top['pipeline']} ({top.get('open_inr_fmt', 'n/a')})" if top else None


def _headline_total_leads(data):
    n = data.get("total_leads")
    return f"Total leads: {n:,}" if n is not None else None


def _headline_lead_status(data):
    n = data.get("total_leads")
    return f"Total leads: {n:,}" if n is not None else None


def _headline_leads_by_source(data):
    top = data.get("top_source")
    return f"Top lead source: {top['source']} ({top['n']:,} leads)" if top else None


def _headline_revenue_target(data):
    status = data.get("status")
    if not status:
        return None
    return (f"{status[0].upper()}{status[1:]} - "
            f"{data.get('actual_inr_fmt', 'n/a')} actual vs {data.get('target_inr_fmt', 'n/a')} target")


def _headline_revenue_comparison(data):
    if "change_pct" not in data:
        return None
    this_m, last_m = data.get("this_month", {}), data.get("last_month", {})
    arrow = "up" if data.get("direction") == "up" else "down"
    return (f"{this_m.get('revenue_inr_fmt', 'n/a')} this month vs "
            f"{last_m.get('revenue_inr_fmt', 'n/a')} last month ({arrow} {abs(data['change_pct'])}%)")


def _headline_lead_journey(data):
    pct = data.get("overall_pct")
    return f"Lead-to-won conversion: {pct}%" if pct is not None else None


def _headline_weekly_kpis(data):
    # This is the exact tool that once got restated with a fabricated date
    # range ("Dec 11-17") that matched nothing real - so the real week
    # dates go in the headline explicitly, not left to the model's prose.
    ws, we = data.get("week_start"), data.get("week_end")
    leads = data.get("leads", {}).get("value")
    if ws is None or leads is None:
        return None
    return f"Week of {ws} to {we}: {leads:,} leads"


def _headline_segment_diagnostic(data):
    if data.get("error"):
        return None
    metric = "win rate" if data["dimension"] == "pipeline" else "conversion rate"
    rate = data.get("win_rate", data.get("conversion_rate"))
    company_rate = data.get("company_win_rate", data.get("company_conversion_rate"))
    warning = " (small sample - treat as indicative)" if data.get("sample_size_warning") else ""
    return (f"{data['value']}: {rate:.1%} {metric} vs {company_rate:.1%} company average "
            f"({data['vs_company']}){warning}")


def _headline_account_contacts(data):
    if data.get("error") or not data.get("contacts"):
        return None
    c = data["contacts"][0]
    more = f" (+{len(data['contacts']) - 1} more)" if len(data["contacts"]) > 1 else ""
    return f"Contact at {data['account_name']}: {c['full_name']} ({c['email']}){more}"


def _headline_hot_leads(data):
    top = (data.get("leads") or [None])[0]
    if not top:
        return None
    return f"Hottest lead: {top['name']} at {top['company']} (score {top['score']}/100)"


def _headline_data_quality(data):
    metrics = data.get("metrics")
    if not metrics:
        return None
    worst = min((m for m in metrics if m.get("pct") is not None), key=lambda m: m["pct"], default=None)
    if not worst:
        return None
    base = f"Lowest-filled: {worst['label']} - {worst['numerator']}/{worst['denominator']} ({worst['pct']}%)"
    return base + " (tracking just started - no trend yet)" if data.get("note") else base


HEADLINE_BUILDERS = {
    "get_rep_performance": _headline_rep_performance,
    "get_pipeline_overview": _headline_pipeline_overview,
    "get_total_leads": _headline_total_leads,
    "get_lead_status_breakdown": _headline_lead_status,
    "get_leads_by_source": _headline_leads_by_source,
    "get_revenue_target": _headline_revenue_target,
    "get_revenue_comparison": _headline_revenue_comparison,
    "get_lead_journey": _headline_lead_journey,
    "get_weekly_kpis": _headline_weekly_kpis,
    "get_hot_leads": _headline_hot_leads,
    "get_segment_diagnostic": _headline_segment_diagnostic,
    "get_account_contacts": _headline_account_contacts,
    "get_data_quality_progress": _headline_data_quality,
}


def build_headline(tool_log):
    """Looks at the LAST tool call for a known aggregate/verdict shape and
    returns a guaranteed-correct one-line answer, or None if this question
    didn't hit one (e.g. a raw list with no single "the answer" concept -
    those are fine relayed as the model's own prose plus the Verified Data
    table, there's no single figure to protect there).
    """
    if not tool_log:
        return None
    last = tool_log[-1]
    builder = HEADLINE_BUILDERS.get(last["tool"])
    if not builder:
        return None
    try:
        return builder(last["data"])
    except (KeyError, TypeError):
        return None


def ask(user_message, history=None, max_tool_rounds=4):
    """Runs the tool-calling loop. Returns {reply, tool_calls: [...]} for transparency."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    tool_log = []
    for _ in range(max_tool_rounds):
        try:
            data = _post({
                "model": MODEL, "messages": messages, "tools": TOOL_SCHEMA, "stream": False,
                # Low temperature on purpose: this restates business figures,
                # it doesn't write creatively. Same question has produced two
                # different (one fabricated) answers run to run at defaults -
                # more determinism won't fix transcription errors, but it
                # stops adding random variance on top of them.
                "options": {"temperature": 0.1},
            })
        except urllib.error.URLError as error:
            return {"reply": f"Can't reach the local model (Ollama) - is it running? ({error})", "tool_calls": tool_log}

        msg = data.get("message", {})
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": msg.get("content", "").strip(), "tool_calls": tool_log,
                    "headline": build_headline(tool_log)}

        messages.append(msg)
        for call_req in calls:
            name = call_req["function"]["name"]
            args = call_req["function"].get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            args = clean_args(args)
            fn = TOOLS.get(name)
            result = fn(args) if fn else {"error": f"unknown tool '{name}'"}
            clean_result = strip_raw_money(result)
            # Full verified data goes back with the response too, not just to
            # the model - the model's prose can mis-transcribe a number even
            # when the underlying figure is correct, so the UI can show the
            # real thing alongside whatever the model says.
            tool_log.append({"tool": name, "args": args, "data": clean_result})
            messages.append({"role": "tool", "content": json.dumps(clean_result, default=str)})

    return {"reply": "That took more tool calls than expected - try asking a more specific question.",
            "tool_calls": tool_log}


if __name__ == "__main__":
    import readline  # noqa: F401  (nicer input() editing, if available)
    print(f"Chatting with {MODEL} via Ollama. Ctrl+C to quit.\n")
    history = []
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        result = ask(q, history)
        print(f"\nbot> {result['reply']}\n")
        if result["tool_calls"]:
            print(f"  (used: {', '.join(t['tool'] for t in result['tool_calls'])})\n")
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": result["reply"]})
