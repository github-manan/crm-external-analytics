#!/usr/bin/env python3
"""Deploy-time database setup - the "migrate" step. Safe to run on every
deploy: creates any missing tables, seeds revenue_targets the first time
(from config/targets.json), and rebuilds the views. Never deletes data.

Needs DATABASE_URL. Usage: python3 scripts/migrate.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_secrets
app_secrets.load()  # settings first: Secrets Manager on the server, .env on a laptop
import transform

if __name__ == "__main__":
    transform.main()
