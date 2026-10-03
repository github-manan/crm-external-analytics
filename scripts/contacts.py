"""Account contacts - "who do we actually talk to at X" lookup.

Honest limitation: `title`/role is never populated for any of the 1,850
contacts in this CRM (0%) - so this can say who to email, not what their
role is. Email itself is solid (93% populated).
"""

from weekly import rows


def account_contacts(account_name, limit=10):
    found = rows("""
        SELECT full_name, email, account_name
        FROM v_contacts
        WHERE account_name = ?
        ORDER BY full_name
        LIMIT ?
    """, (account_name, limit))
    if not found:
        return {"error": f"no contacts found for account '{account_name}'",
                "note": "Try the exact account name as it appears in Zoho - this is an exact match, not a fuzzy search."}
    return {
        "account_name": account_name,
        "note": "Role/title isn't tracked for contacts in this CRM - name and email only.",
        "contacts": found,
    }
