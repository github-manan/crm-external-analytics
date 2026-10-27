"""Load settings into os.environ - the one place they come from.

Server: CRM_SECRET_ID is set (by systemd) -> read AWS Secrets Manager.
Laptop: CRM_SECRET_ID not set             -> read the local .env file.

On the server a Secrets Manager failure crashes with the error - it never
falls back to .env. AWS credentials come from the EC2 instance role, never
from code or .env. A variable already in the environment always wins.

Not named secrets.py: that would hide Python's built-in secrets module.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")

_loaded = False


def _from_secrets_manager(secret_id):
    import boto3  # only needed on the server
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    return json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])


def _from_dotenv(path):
    values = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load():
    """Safe to call from every entry point and module - only the first call
    does any work, so Secrets Manager is read once per process."""
    global _loaded
    if _loaded:
        return
    secret_id = os.environ.get("CRM_SECRET_ID")
    values = _from_secrets_manager(secret_id) if secret_id else _from_dotenv(ENV_PATH)
    for key, value in values.items():
        os.environ.setdefault(key, str(value))
    _loaded = True
