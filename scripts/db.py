"""The one Postgres connection point for every script and the API server.

The database comes from the DATABASE_URL environment variable (or a
DATABASE_URL line in .env for local runs), e.g.
    postgres://crm_user:<password>@<host>:5432/crm?sslmode=require
Nothing about the database - host, user, password - is ever written in code.

Queries in this codebase use `?` placeholders; rows()/one()/execute()
turn them into psycopg's `%s` (escaping any literal `%`, e.g. in
LIKE 'Closed%'), so a query reads the same whichever helper runs it.

Values come back the way the SQLite version returned them, so callers
didn't have to change: dates are 'YYYY-MM-DD' text (the views build them
that way), and NUMERIC comes back as int when it's whole (COUNT/SUM of
flags) or float otherwise (money) - never Decimal, which json.dumps
would turn into a string.

Reads (rows/one) go through a small connection pool, read-only and in
India time (Asia/Kolkata), so "today" means today in IST. Writers
(extract, transform, snapshot) open their own connection via connect().
"""

import os
import threading

import psycopg
from psycopg.adapt import Loader
from psycopg.rows import dict_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
TIMEZONE = "Asia/Kolkata"


def load_env():
    """KEY=VALUE lines from .env into the environment, never overriding a
    variable that's already set (a real environment always wins)."""
    if not os.path.exists(ENV_PATH):
        return
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL isn't set - add it to the environment (or .env for local runs), "
                         "e.g. DATABASE_URL=postgresql://localhost/crm")
    return url


class _NumericLoader(Loader):
    def load(self, data):
        text = bytes(data).decode()
        if "." in text or "e" in text.lower() or text in ("NaN", "Infinity", "-Infinity"):
            return float(text)
        return int(text)


psycopg.adapters.register_loader("numeric", _NumericLoader)


def q(sql):
    """`?` placeholders -> psycopg's `%s`, escaping literal `%` first."""
    return sql.replace("%", "%%").replace("?", "%s")


def _options(read_only):
    opts = f"-c TimeZone={TIMEZONE} -c statement_timeout=60000"
    return opts + (" -c default_transaction_read_only=on" if read_only else "")


def connect(read_only=False):
    """A plain connection for scripts that write (extract, transform,
    snapshot). Rows come back as dicts. Caller commits and closes."""
    return psycopg.connect(database_url(), row_factory=dict_row, options=_options(read_only))


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    database_url(), min_size=1, max_size=8, open=True,
                    kwargs={"autocommit": True, "row_factory": dict_row,
                            "options": _options(read_only=True)},
                )
    return _pool


def rows(sql, params=None):
    """Read query -> list of dicts."""
    params = list(params) if params else None
    with _get_pool().connection() as conn:
        return conn.execute(q(sql) if params else sql, params).fetchall()


def one(sql, params=None):
    result = rows(sql, params)
    return result[0] if result else {}


def execute(conn, sql, params=None):
    """Run one statement on a writer connection with `?` placeholders."""
    params = list(params) if params else None
    return conn.execute(q(sql) if params else sql, params)


def executemany(conn, sql, seq):
    with conn.cursor() as cur:
        cur.executemany(q(sql), seq)
        return cur.rowcount


def view_names(conn=None):
    sql = """SELECT table_name AS name FROM information_schema.views
             WHERE table_schema = current_schema() ORDER BY table_name"""
    found = conn.execute(sql).fetchall() if conn else rows(sql)
    return [r["name"] for r in found]


# Tables, created by ensure_schema() (scripts/migrate.py). The records and
# users payloads are Zoho's raw JSON as JSONB; transform.py's views read it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    module        TEXT NOT NULL,
    id            TEXT NOT NULL,
    payload       JSONB NOT NULL,
    modified_time TEXT,
    extracted_at  TEXT NOT NULL,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (module, id)
);
CREATE INDEX IF NOT EXISTS idx_records_module_modified ON records (module, modified_time);

CREATE TABLE IF NOT EXISTS sync_state (
    module       TEXT PRIMARY KEY,
    last_run_at  TEXT,
    watermark    TEXT,
    records_seen INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    payload      JSONB NOT NULL,
    extracted_at TEXT NOT NULL
);

-- The one table that can't be rebuilt from Zoho - see snapshot.py.
CREATE TABLE IF NOT EXISTS pipeline_snapshots (
    snapshot_date TEXT NOT NULL,
    deal_id       TEXT NOT NULL,
    deal_name     TEXT,
    stage         TEXT,
    pipeline      TEXT,
    amount_inr    NUMERIC,
    probability   NUMERIC,
    closing_date  TEXT,
    owner_name    TEXT,
    PRIMARY KEY (snapshot_date, deal_id)
);

-- Daily fill-rate history for the data-quality asks (data_quality.py).
CREATE TABLE IF NOT EXISTS data_quality_snapshots (
    snapshot_date TEXT NOT NULL,
    metric_key    TEXT NOT NULL,
    numerator     INTEGER,
    denominator   INTEGER,
    pct           NUMERIC,
    PRIMARY KEY (snapshot_date, metric_key)
);

-- Monthly revenue targets (Zoho holds no quota data). scope 'company':
-- key is a month 'YYYY-MM'; scope 'owner': key is a CRM owner_name. Key
-- '_default' covers any month/owner without its own row. Seeded once
-- from config/targets.json; edit the rows directly after that.
CREATE TABLE IF NOT EXISTS revenue_targets (
    scope      TEXT NOT NULL CHECK (scope IN ('company', 'owner')),
    key        TEXT NOT NULL,
    target_inr NUMERIC NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


def ensure_schema(conn):
    conn.execute(SCHEMA)
