#!/usr/bin/env python3
"""Verify the OAuth setup works and report what this CRM actually contains."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zoho

# /org sits behind ZohoCRM.org.READ, which the current grant may not include.
try:
    org = zoho.api_get("/crm/v8/org")["org"][0]
    print(f"Org:      {org.get('company_name')}")
    print(f"Edition:  {org.get('edition')}")
    print(f"Currency: {org.get('currency')}  |  Time zone: {org.get('time_zone')}")
except zoho.ZohoError as error:
    print(f"Org info: unavailable ({error.status}) - needs ZohoCRM.org.READ")

users = zoho.api_get("/crm/v8/users", {"type": "ActiveUsers", "per_page": 200})
print(f"Active users: {len(users.get('users', []))}")

modules = zoho.api_get("/crm/v8/settings/modules")["modules"]
readable = [m for m in modules if m.get("api_supported")]
print(f"API-accessible modules: {len(readable)}\n")

CORE = [
    "Leads", "Contacts", "Accounts", "Deals", "Tasks", "Calls", "Events",
    "Quotes", "Sales_Orders", "Invoices", "Campaigns", "Products",
    "DealHistory", "Marketing_Expense", "Notes",
]
present = {m["api_name"] for m in readable}

print("Record counts:")
for name in CORE:
    if name not in present:
        print(f"  {name:<20} not present")
        continue
    try:
        # Dedicated count endpoint - one credit, exact total.
        result = zoho.api_get(f"/crm/v8/{name}/actions/count")
        print(f"  {name:<20} {result.get('count')}")
    except zoho.ZohoError as error:
        print(f"  {name:<20} count failed ({error.status})")

custom = [m["api_name"] for m in readable if m.get("generated_type") == "custom"]
trackers = [m["api_name"] for m in readable if m.get("generated_type") == "field_tracker"]
print(f"\nCustom modules:  {', '.join(custom) or 'none'}")
print(f"Field trackers:  {', '.join(trackers) or 'none'}")
