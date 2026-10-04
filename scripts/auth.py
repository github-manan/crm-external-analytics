#!/usr/bin/env python3
"""Login + per-user data scoping. Stdlib only (hashlib's pbkdf2_hmac, no
new dependency - bcrypt/argon2 would be nicer but this is an internal tool
for ~20 people, not a public-facing login).

Accounts live in config/users.json (same pattern as config/targets.json -
plain JSON, hand-edited or managed via this file's CLI), never in
data/crm.db - that database is opened read-only everywhere it's queried
(see serve.py), and login accounts have nothing to do with CRM records,
so they deliberately don't share a store with them.

Sessions are an in-memory dict, not a table - they reset on server
restart (logs everyone out), which is fine for an internal tool and
avoids needing a write-capable store at all for anything touching
process state.

Role model: a user is either tied to one owner_name (sees only their own
deals/leads, everywhere - dashboards and chat) or is_admin (sees
everyone's, and can still filter down to one individual same as before).
There is no third tier - see CLI below to create accounts.

CLI usage:
    python3 scripts/auth.py add <username> --owner "Namrata Dhuri"
    python3 scripts/auth.py add <username> --admin
    python3 scripts/auth.py passwd <username>
    python3 scripts/auth.py list
    python3 scripts/auth.py remove <username>
"""

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS_PATH = os.path.join(ROOT, "config", "users.json")

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 12 * 3600  # 12 hours - re-login once a day, not every page load

# token -> {"username", "display_name", "owner_name", "is_admin", "expires_at"}
SESSIONS = {}


# --- password hashing --------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return salt, digest


def verify_password(password, salt, expected_hash):
    _, digest = hash_password(password, salt)
    return secrets.compare_digest(digest, expected_hash)


# --- user store ----------------------------------------------------------------

def load_users():
    if not os.path.exists(USERS_PATH):
        return {}
    with open(USERS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
        f.write("\n")


def authenticate(username, password):
    """Returns the user record (without salt/hash) on success, else None."""
    users = load_users()
    record = users.get(username)
    if not record:
        return None
    if not verify_password(password, record["salt"], record["hash"]):
        return None
    return {
        "username": username,
        "display_name": record.get("display_name", username),
        "owner_name": record.get("owner_name"),
        "is_admin": bool(record.get("is_admin")),
    }


# --- sessions --------------------------------------------------------------------

def create_session(user):
    token = secrets.token_hex(32)
    SESSIONS[token] = {**user, "expires_at": time.time() + SESSION_TTL_SECONDS}
    return token


def get_session(token):
    if not token:
        return None
    session = SESSIONS.get(token)
    if not session:
        return None
    if session["expires_at"] < time.time():
        del SESSIONS[token]
        return None
    return session


def delete_session(token):
    SESSIONS.pop(token, None)


# --- CLI -------------------------------------------------------------------------

def cli_add(args):
    users = load_users()
    if args.username in users:
        sys.exit(f"'{args.username}' already exists - use 'passwd' to change their password.")
    if not args.admin and not args.owner:
        sys.exit("pass either --owner \"Rep Name\" or --admin")
    password = getpass.getpass(f"Password for {args.username}: ")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("passwords didn't match")
    salt, digest = hash_password(password)
    users[args.username] = {
        "salt": salt, "hash": digest,
        "display_name": args.display_name or args.username,
        "owner_name": None if args.admin else args.owner,
        "is_admin": bool(args.admin),
    }
    save_users(users)
    role = "admin (sees everyone)" if args.admin else f"owner={args.owner}"
    print(f"Added {args.username} ({role}).")


def cli_passwd(args):
    users = load_users()
    if args.username not in users:
        sys.exit(f"no such user '{args.username}'")
    password = getpass.getpass(f"New password for {args.username}: ")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("passwords didn't match")
    salt, digest = hash_password(password)
    users[args.username]["salt"] = salt
    users[args.username]["hash"] = digest
    save_users(users)
    print(f"Password updated for {args.username}.")


def cli_list(_args):
    users = load_users()
    if not users:
        print("No users yet - add one with: python3 scripts/auth.py add <username> --owner \"...\" | --admin")
        return
    for username, record in users.items():
        role = "admin" if record.get("is_admin") else f"owner={record.get('owner_name')}"
        print(f"{username:<20} {record.get('display_name', username):<24} {role}")


def cli_remove(args):
    users = load_users()
    if args.username not in users:
        sys.exit(f"no such user '{args.username}'")
    del users[args.username]
    save_users(users)
    print(f"Removed {args.username}.")


def main():
    parser = argparse.ArgumentParser(description="Manage dashboard login accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="create a new login")
    p_add.add_argument("username")
    p_add.add_argument("--owner", help="exact owner_name as it appears in the CRM (e.g. 'Namrata Dhuri')")
    p_add.add_argument("--admin", action="store_true", help="sees everyone's data, not just one owner's")
    p_add.add_argument("--display-name", help="defaults to the username")
    p_add.set_defaults(func=cli_add)

    p_passwd = sub.add_parser("passwd", help="change a user's password")
    p_passwd.add_argument("username")
    p_passwd.set_defaults(func=cli_passwd)

    p_list = sub.add_parser("list", help="list all logins")
    p_list.set_defaults(func=cli_list)

    p_remove = sub.add_parser("remove", help="delete a login")
    p_remove.add_argument("username")
    p_remove.set_defaults(func=cli_remove)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
