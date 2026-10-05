#!/usr/bin/env python3
"""Regression suite for chatbot.py - run this after touching the system
prompt, TOOL_SCHEMA, or any tool function, before shipping the change.

Every case here encodes a real bug this project already hit once (wrong
tool, wrong argument, a fabricated number, a privacy boundary that wasn't
actually enforced) - see each case's `notes`. The point isn't abstract
coverage, it's making sure none of those come back silently next time a
tool description gets reworded or a new tool gets added.

Needs Ollama running locally (same requirement as the chatbot itself -
this calls chatbot.ask() for real, it does not mock the model). Honest
limitation: the model is not perfectly deterministic even at temperature
0.1 (documented in chatbot.py) - a single failure here might be run-to-run
noise, not a real regression. Re-run before concluding a case is broken,
and treat a case that fails consistently, not once, as the real signal.

Usage: python3 scripts/chatbot_regression.py
Exit code 0 if every case passes, 1 otherwise.
"""

import io
import os
import sys
from dataclasses import dataclass, field

# Headlines can contain a rupee sign - the default Windows console encoding
# (cp1252) can't print it and crashes the whole run on a passing case.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chatbot

ADMIN = None  # chatbot.ask()'s own CLI mode also calls it with no session
REP = {"username": "namrata.dhuri", "display_name": "Namrata Dhuri",
       "owner_name": "Namrata Dhuri", "is_admin": False}


@dataclass
class Case:
    name: str
    question: str
    notes: str
    session: dict = None
    expect_tool: object = None          # str, or a list/tuple of acceptable tools
    expect_args: dict = field(default_factory=dict)   # partial match against the call's args
    expect_blocked: bool = False        # True => expect the admin-only refusal, no real tool call


CASES = [
    # --- segment_diagnostic dimension disambiguation ---
    # Real bug: "leads in the US" first picked get_leads_by_source with a
    # bogus owner="US", then on retry picked segment_diagnostic but with
    # dimension="industry" for a place name. Fixed via a much more
    # explicit tool description - these pin that fix in place.
    Case("country_phrasing_us", "how are we doing in the US?",
         "Place name must map to dimension=country, not industry/owner.",
         expect_tool="get_segment_diagnostic", expect_args={"dimension": "country"}),
    Case("country_phrasing_region", "what's our conversion rate like in the Middle East?",
         "A region name, not a single country - still dimension=country.",
         expect_tool="get_segment_diagnostic", expect_args={"dimension": "country"}),
    Case("industry_phrasing", "how do we increase sales in BFSI?",
         "An industry/vertical name must map to dimension=industry.",
         expect_tool="get_segment_diagnostic", expect_args={"dimension": "industry"}),
    Case("pipeline_phrasing", "how is the Datasurfr pipeline performing?",
         "One of the 4 named pipelines must map to dimension=pipeline.",
         expect_tool="get_segment_diagnostic", expect_args={"dimension": "pipeline"}),

    # --- revenue_comparison vs revenue_target disambiguation ---
    # Real bug: the model once invented two entire revenue figures and a
    # % change for this question from a tool that had no such data.
    Case("revenue_comparison_not_target", "how does this month's revenue compare to last month?",
         "Must use get_revenue_comparison, never get_revenue_target or invented numbers.",
         expect_tool="get_revenue_comparison"),
    Case("revenue_target_not_comparison", "are we ahead of target this month?",
         "A target question must use get_revenue_target, not get_revenue_comparison.",
         expect_tool="get_revenue_target"),

    # --- basic tool-selection sanity (broad coverage, not just edge cases) ---
    Case("total_leads", "how many leads do we have in total?",
         "Plain count question - get_total_leads, not a breakdown tool.",
         expect_tool="get_total_leads"),
    Case("lead_journey", "what's our lead to deal conversion rate?",
         "The funnel question - get_lead_journey.",
         expect_tool="get_lead_journey"),
    Case("pipeline_overview", "how's the open pipeline looking across all pipelines?",
         "Company-wide pipeline question - get_pipeline_overview.",
         expect_tool="get_pipeline_overview"),
    Case("weekly_kpis", "how's this week going so far?",
         "This-week question - get_weekly_kpis.",
         expect_tool="get_weekly_kpis"),
    Case("account_contacts", "who should I contact at Prodiscover?",
         "Contact lookup - get_account_contacts with the account name.",
         expect_tool="get_account_contacts", expect_args={"account_name": "Prodiscover"}),
    Case("data_quality", "is our data quality improving?",
         "Data-entry-completeness question - get_data_quality_progress.",
         expect_tool="get_data_quality_progress"),

    # --- non-admin owner scoping: the actual security boundary ---
    # Enforced in ask() itself (args["owner"] forced), not by trusting the
    # model - these confirm the override actually fires end to end.
    Case("rep_hot_leads_forced_to_self", "what lead should I pick up next?",
         "Non-admin: owner must be forced to the session's own name.",
         session=REP, expect_tool="get_hot_leads", expect_args={"owner": "Namrata Dhuri"}),
    Case("rep_weekly_kpis_forced_to_self", "how's my week going?",
         "Non-admin: owner must be forced to the session's own name.",
         session=REP, expect_tool="get_weekly_kpis", expect_args={"owner": "Namrata Dhuri"}),

    # --- admin-only tools: blocked for non-admin, allowed for admin ---
    Case("rep_blocked_from_rep_performance", "which rep has the highest revenue this year?",
         "Names/ranks every rep - must be refused for a non-admin session.",
         session=REP, expect_tool="get_rep_performance", expect_blocked=True),
    Case("admin_allowed_rep_performance", "which rep has the highest revenue this year?",
         "Same question, admin session - must NOT be blocked.",
         session=ADMIN, expect_tool="get_rep_performance", expect_blocked=False),
    Case("rep_blocked_from_user_activity", "which CRM users aren't active?",
         "Names every user - must be refused for a non-admin session.",
         session=REP, expect_tool="get_user_activity", expect_blocked=True),
    Case("admin_allowed_user_activity", "which CRM users aren't active?",
         "Same question, admin session - must NOT be blocked.",
         session=ADMIN, expect_tool="get_user_activity", expect_blocked=False),

    # --- insights: open-ended "what's notable" routing ---
    Case("insights_open_ended", "what stands out right now? any notable trends?",
         "Open-ended question with no named segment - must route to get_insights, "
         "not a specific segment_diagnostic lookup.",
         expect_tool="get_insights"),
]


def run_case(case):
    result = chatbot.ask(case.question, session=case.session)
    tool_log = result.get("tool_calls") or []
    if not tool_log:
        return False, "no tool was called at all"

    last = tool_log[-1]
    expected = case.expect_tool
    if expected is not None:
        ok = last["tool"] == expected if isinstance(expected, str) else last["tool"] in expected
        if not ok:
            return False, f"expected tool {expected!r}, got {last['tool']!r}"

    for key, value in case.expect_args.items():
        actual = last["args"].get(key)
        if actual != value:
            return False, f"expected arg {key}={value!r}, got {actual!r} (all args: {last['args']})"

    was_blocked = bool(last["data"].get("error")) and "admin" in str(last["data"].get("error", "")).lower()
    if case.expect_blocked and not was_blocked:
        return False, f"expected an admin-only refusal, got: {last['data']}"
    if not case.expect_blocked and was_blocked:
        return False, f"unexpectedly blocked: {last['data']}"

    return True, result.get("headline") or "ok"


def main():
    passed, failed = [], []
    for case in CASES:
        try:
            ok, detail = run_case(case)
        except Exception as error:  # noqa: BLE001 - a crash is also a failure, report it don't abort the run
            ok, detail = False, f"raised {type(error).__name__}: {error}"
        (passed if ok else failed).append((case, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case.name:<32} {detail}")

    print(f"\n{len(passed)}/{len(CASES)} passed.")
    if failed:
        print("\nFailed cases:")
        for case, detail in failed:
            print(f"  - {case.name}: {case.notes}\n    {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
