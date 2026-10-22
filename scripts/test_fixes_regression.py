#!/usr/bin/env python3
"""Regression tests for the bugs found by the 2026-10-20 three-review audit
(see the "IMAGE CHANGES" commit). Unlike chatbot_regression.py, these don't
need Ollama running - they test the underlying calculations and access
rules directly, the same way chatbot_regression.py pins chatbot behavior.

Stdlib only, same hand-rolled pattern as chatbot_regression.py (no pytest
dependency) - a plain assertion per case, first failure prints and exits 1.

Usage: python3 scripts/test_fixes_regression.py
"""

import io
import os
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly
import segments
import contacts
import auth


def check(name, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" - {detail}" if detail and not condition else ""))
    return condition


def main():
    results = []

    # --- timezone: a pre-05:30 IST record keeps its IST wall-clock date ---
    row = weekly.one(
        "SELECT created_time, created_month FROM v_leads "
        "WHERE substr(created_time, 12, 2) < '05' AND substr(created_time, 12, 2) >= '00' LIMIT 1"
    )
    results.append(check(
        "timezone: pre-05:30-IST lead keeps its IST date (not shifted to previous day)",
        bool(row) and row["created_time"][:10] == row["created_month"][:7] + row["created_time"][7:10],
        detail=str(row),
    ))

    # --- bookings: previously-missing won deals (no Closing_Date) now counted ---
    total_won = weekly.one("SELECT COUNT(*) n FROM v_deals WHERE is_won = 1")["n"]
    bookings_total = weekly.one("SELECT SUM(deals_won) n FROM v_bookings_fiscal")["n"]
    results.append(check(
        "bookings: every won deal counted, including the 93 with no Closing_Date",
        bookings_total == total_won,
        detail=f"won={total_won} bookings_total={bookings_total}",
    ))

    # --- win rate: denominator is decided deals (won+lost), not every deal ---
    renewal = segments.segment_diagnostic("pipeline", "Renewal")
    results.append(check(
        "win rate: Renewal reads ~100% of decided deals, not ~10% of all deals",
        renewal.get("win_rate") is not None and renewal["win_rate"] > 0.9,
        detail=str(renewal.get("win_rate")),
    ))

    # --- revenue target: a real rep still gets a target, the placeholder doesn't ---
    real_rep = weekly.target(owner="Namrata Dhuri")
    placeholder = weekly.target(owner="Admin MitKat")
    results.append(check(
        "revenue target: a real rep still gets a quota",
        real_rep.get("target_inr") not in (None, 0),
        detail=str(real_rep),
    ))
    results.append(check(
        "revenue target: the non-individual placeholder owner does NOT get a quota",
        placeholder.get("target_inr") is None and placeholder.get("not_applicable") is True,
        detail=str(placeholder),
    ))

    # --- account contacts: scoped to the owning rep, admin unrestricted ---
    an_account = weekly.one("SELECT account_name, owner_name FROM v_accounts WHERE owner_name IS NOT NULL LIMIT 1")
    if an_account:
        own = contacts.account_contacts(an_account["account_name"], owner=an_account["owner_name"])
        other = contacts.account_contacts(an_account["account_name"], owner="Someone Else Entirely")
        admin_view = contacts.account_contacts(an_account["account_name"], owner=None)
        results.append(check(
            "account contacts: the owning rep can see their own account",
            "error" not in own,
            detail=str(own),
        ))
        results.append(check(
            "account contacts: a different rep is refused, not silently shown the data",
            other.get("error", "").startswith(f"'{an_account['account_name']}'"),
            detail=str(other),
        ))
        results.append(check(
            "account contacts: admin (owner=None) sees it regardless",
            "error" not in admin_view,
            detail=str(admin_view),
        ))

    # --- login rate limiting ---
    test_user = "__ratelimit_test_user_does_not_exist__"
    for _ in range(auth.LOGIN_MAX_ATTEMPTS):
        auth.authenticate(test_user, "wrong")
    locked = auth._login_locked_out(test_user)
    results.append(check(
        f"login: locks out after {auth.LOGIN_MAX_ATTEMPTS} failed attempts",
        locked,
    ))
    auth._FAILED_LOGINS.pop(test_user, None)  # don't leave test state behind

    # --- session revalidation: promoting/demoting a real user invalidates ---
    # --- their already-issued session, instead of waiting out the 12h TTL ---
    import copy
    users = auth.load_users()
    test_username = next(iter(users), None)
    if test_username:
        backup = copy.deepcopy(users)
        original_record = users[test_username]
        token = auth.create_session({
            "username": test_username,
            "display_name": original_record.get("display_name", test_username),
            "owner_name": original_record.get("owner_name"),
            "is_admin": bool(original_record.get("is_admin")),
        })
        valid_right_after_login = auth.get_session(token) is not None
        users[test_username]["is_admin"] = not bool(original_record.get("is_admin"))
        auth.save_users(users)
        invalid_after_role_change = auth.get_session(token) is None
        auth.save_users(backup)  # restore immediately, regardless of outcome
        results.append(check(
            "sessions: valid immediately after login",
            valid_right_after_login,
        ))
        results.append(check(
            "sessions: a role change invalidates an already-issued session "
            "(used to only take effect after the 12h TTL expired)",
            invalid_after_role_change,
        ))

    print(f"\n{sum(results)}/{len(results)} passed.")
    if not all(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
