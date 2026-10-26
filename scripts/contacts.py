"""Account contacts - "who do we actually talk to at X" lookup.

Honest limitation: `title`/role is never populated for any of the 1,850
contacts in this CRM (0%) - so this can say who to email, not what their
role is. Email itself is solid (93% populated).
"""

from weekly import rows, one


def _account_owner(account_name):
    row = one("SELECT owner_name FROM v_accounts WHERE account_name = ?", (account_name,))
    return row["owner_name"] if row else None


def _forbidden(account_name):
    return {
        "error": (f"'{account_name}' exists, but isn't one of your accounts - "
                  "ask an admin, or whoever owns it."),
    }


def account_contacts(account_name, limit=10, owner=None):
    """`owner`: when given (always set for a non-admin chatbot session - see
    chatbot.py's ACCOUNT_SCOPED_TOOLS), only that rep's own accounts are
    returned. Previously any logged-in rep could pull any account's contact
    names/emails, not just their own - the same scoping every other
    per-rep tool already enforces."""
    exact = rows("""
        SELECT full_name, email, account_name
        FROM v_contacts WHERE account_name = ?
        ORDER BY full_name LIMIT ?
    """, (account_name, limit))
    if exact:
        if owner and _account_owner(account_name) != owner:
            return _forbidden(account_name)
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
        FROM v_contacts WHERE account_name ILIKE '%' || ? || '%'
        LIMIT 5
    """, (account_name,))

    if len(partial) == 1:
        matched = partial[0]["account_name"]
        if owner and _account_owner(matched) != owner:
            return _forbidden(matched)
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
        if owner:
            partial = [r for r in partial if _account_owner(r["account_name"]) == owner]
            if not partial:
                return _forbidden(account_name)
        return {
            "error": f"'{account_name}' isn't an exact match, and matches more than one account",
            "did_you_mean": [r["account_name"] for r in partial],
        }

    return {
        "error": f"no account found matching '{account_name}'",
        "note": "Checked for both an exact and a partial name match - neither found anything.",
    }
