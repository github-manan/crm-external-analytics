"""Shared Zoho CRM API client: token handling and paged reads.

Stdlib only. Access tokens live 1 hour and are minted on demand from the
refresh token in .env, so nothing needs to persist them.
"""

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60
MAX_ATTEMPTS = 5

# Blips seen in practice against Zoho: truncated chunked responses, reset
# connections, DNS hiccups. All worth retrying; a 4xx is not.
TRANSIENT = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    urllib.error.URLError,
    socket.timeout,
    ConnectionError,
)

ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

_token = {"value": None, "expires_at": 0}


class ZohoError(RuntimeError):
    """An API call returned a non-2xx status."""

    def __init__(self, status, path, body):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status} on {path}\n{body}")


def load_env():
    if not os.path.exists(ENV_PATH):
        raise SystemExit(f"{ENV_PATH} not found - run scripts/authorize.py first.")
    for line in open(ENV_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _post_form(url, fields):
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def access_token():
    """Return a valid access token, refreshing with 60s of headroom."""
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]

    load_env()
    payload = _post_form(
        f"https://{os.environ['ZOHO_ACCOUNTS_HOST']}/oauth/v2/token",
        {
            "grant_type": "refresh_token",
            "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        },
    )
    if "access_token" not in payload:
        raise SystemExit(f"Token refresh failed: {json.dumps(payload, indent=2)}")

    _token["value"] = payload["access_token"]
    _token["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return _token["value"]


def api_get(path, params=None, headers=None):
    """GET a CRM endpoint.

    Returns None for 204 (empty result) and 304 (nothing modified since the
    If-Modified-Since header), both of which are normal outcomes here.
    """
    load_env()
    url = f"https://{os.environ['ZOHO_API_DOMAIN']}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Zoho-oauthtoken {access_token()}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status == 204:
                    return None
                return json.load(response)

        except urllib.error.HTTPError as error:
            # 304 answers If-Modified-Since: nothing changed. Not a failure.
            if error.code == 304:
                return None
            # Rate limit and server faults are worth waiting out.
            if error.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                delay = 60 if error.code == 429 else 2 ** attempt
                print(f"    HTTP {error.code}, retry {attempt}/{MAX_ATTEMPTS} in {delay}s")
                time.sleep(delay)
                continue
            raise ZohoError(error.code, path, error.read().decode()) from None

        except TRANSIENT as error:
            if attempt == MAX_ATTEMPTS:
                raise ZohoError(0, path, f"{type(error).__name__}: {error}") from None
            delay = 2 ** attempt
            print(f"    {type(error).__name__}, retry {attempt}/{MAX_ATTEMPTS} in {delay}s")
            time.sleep(delay)
