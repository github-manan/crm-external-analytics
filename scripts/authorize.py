#!/usr/bin/env python3
"""One-time Zoho CRM OAuth setup.

Opens the consent screen, catches the redirect on localhost:8000, exchanges the
grant code for a refresh token, and writes .env. Run once; after this the
pipeline mints access tokens from the refresh token on its own.

Stdlib only - no pip install needed.
"""

import http.server
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://localhost:8000/callback"
PORT = 8000
# Read-only by construction. The ".READ" suffix is the operation. For the
# modules scope specifically, Zoho's all-modules wildcard is just
# "ZohoCRM.modules.READ" - "ZohoCRM.modules.ALL.READ" is not valid syntax
# ("ALL" is only used when naming one specific module, e.g.
# "ZohoCRM.modules.leads.ALL"); Zoho rejects the whole scope string with
# "Invalid OAuth Scope" if it's included. Nothing here can write to the CRM
# even if a caller tried.
SCOPES = ",".join([
    "ZohoCRM.modules.READ",           # records in every module
    "ZohoCRM.settings.modules.READ",  # module list
    "ZohoCRM.settings.fields.READ",   # field metadata
    "ZohoCRM.users.READ",             # owner id -> name
    "ZohoCRM.coql.READ",              # ad-hoc queries
])

# Zoho hands back an `accounts-server` param on the redirect, so the data
# center never has to be guessed. This maps it to the matching API domain.
API_DOMAIN = {
    "accounts.zoho.in": "www.zohoapis.in",
    "accounts.zoho.com": "www.zohoapis.com",
    "accounts.zoho.eu": "www.zohoapis.eu",
    "accounts.zoho.com.au": "www.zohoapis.com.au",
    "accounts.zoho.jp": "www.zohoapis.jp",
    "accounts.zohocloud.ca": "www.zohoapis.ca",
    "accounts.zoho.sa": "www.zohoapis.sa",
}

captured = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        captured.update(urllib.parse.parse_qs(parsed.query))
        ok = "code" in captured
        body = (
            "<h2>Authorized.</h2><p>Close this tab and return to the terminal.</p>"
            if ok else
            "<h2>No code returned.</h2><p>Check the terminal for details.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # keep the console clean


ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)


def load_env():
    """Pull KEY=VALUE lines from .env into the environment.

    Claude Code's shell gives scripts no stdin, so credentials come from the
    file rather than an interactive prompt.
    """
    if not os.path.exists(ENV_PATH):
        return
    for line in open(ENV_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require(key, label):
    value = os.environ.get(key, "").strip()
    if not value:
        sys.exit(
            f"{label} not found.\n"
            f"Add it to {ENV_PATH} as a line like:\n"
            f"  {key}=your_value_here"
        )
    return value


def main():
    load_env()
    client_id = require("ZOHO_CLIENT_ID", "Client ID")
    client_secret = require("ZOHO_CLIENT_SECRET", "Client Secret")

    # access_type=offline is what makes Zoho return a refresh token at all;
    # prompt=consent forces a fresh one even if this client was authorized before.
    auth_url = "https://accounts.zoho.in/oauth/v2/auth?" + urllib.parse.urlencode({
        "scope": SCOPES,
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": REDIRECT_URI,
    })

    print(f"\nOpening the consent screen. If it doesn't open, paste this:\n\n{auth_url}\n")
    print(f"Waiting for the redirect on port {PORT} ...")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    server.serve_forever()

    if "code" not in captured:
        sys.exit(f"No grant code in the redirect. Got: {captured}")

    code = captured["code"][0]
    accounts_server = captured.get("accounts-server", ["https://accounts.zoho.com"])[0]
    accounts_host = urllib.parse.urlparse(accounts_server).netloc
    api_domain = API_DOMAIN.get(accounts_host)
    if not api_domain:
        sys.exit(f"Unrecognized accounts server '{accounts_host}' - add it to API_DOMAIN.")

    print(f"Got the code. Data center: {accounts_host} -> {api_domain}")
    print("Exchanging for a refresh token ...")

    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }).encode()
    request = urllib.request.Request(f"https://{accounts_host}/oauth/v2/token", data=payload)
    with urllib.request.urlopen(request) as response:
        token = json.load(response)

    if "refresh_token" not in token:
        sys.exit(
            "No refresh token returned. Zoho said: "
            f"{json.dumps(token, indent=2)}\n"
            "Usually means the grant code was already used or expired - rerun this script."
        )

    with open(ENV_PATH, "w") as handle:
        handle.write(
            f"ZOHO_CLIENT_ID={client_id}\n"
            f"ZOHO_CLIENT_SECRET={client_secret}\n"
            f"ZOHO_REFRESH_TOKEN={token['refresh_token']}\n"
            f"ZOHO_ACCOUNTS_HOST={accounts_host}\n"
            f"ZOHO_API_DOMAIN={api_domain}\n"
        )
    os.chmod(ENV_PATH, 0o600)
    print(f"\nDone. Credentials written to {ENV_PATH} (mode 600).")
    print("This file holds a non-expiring refresh token - never commit it.")


if __name__ == "__main__":
    main()
