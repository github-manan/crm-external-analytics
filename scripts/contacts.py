"""Account contacts - "who do we actually talk to at X" lookup.

Honest limitation: `title`/role is never populated for any of the 1,850
contacts in this CRM (0%) - so this can say who to email, not what their
role is. Email itself is solid (93% populated).
"""

from weekly import rows


def account_contacts(account_name, limit=10):
    exact = rows("""
        SELECT full_name, email, account_name
        FROM v_contacts WHERE account_name = ?
        ORDER BY full_name LIMIT ?
    """, (account_name, limit))
    if exact:
        return {
            "account_name": account_name,
            "note": "Role/title isn't tracked for contacts in this CRM - name and email only.",
            "contacts": exact,
        }

    # No exact match - try a partial match before giving up, since a
    # slightly-off account name (abbreviation, different capitalization,
    # a parenthetical dropped) shouldn't return "not found" when the
    # account genuinely exists under a near-identical name.
    partial = rows("""
        SELECT DISTINCT account_name
        FROM v_contacts WHERE account_name LIKE '%' || ? || '%'
        LIMIT 5
    """, (account_name,))

    if len(partial) == 1:
        matched = partial[0]["account_name"]
        contacts_found = rows("""
            SELECT full_name, email, account_name
            FROM v_contacts WHERE account_name = ?
            ORDER BY full_name LIMIT ?
        """, (matched, limit))
        return {
            "account_name": matched,
            "note": (f"No contact found under the exact name '{account_name}' - matched the "
                     f"closest account name instead ('{matched}'). "
                     "Role/title isn't tracked for contacts in this CRM - name and email only."),
            "contacts": contacts_found,
        }

    if len(partial) > 1:
        return {
            "error": f"'{account_name}' isn't an exact match, and matches more than one account",
            "did_you_mean": [r["account_name"] for r in partial],
        }

    return {
        "error": f"no account found matching '{account_name}'",
        "note": "Checked for both an exact and a partial name match - neither found anything.",
    }
