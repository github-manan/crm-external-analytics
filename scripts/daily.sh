#!/usr/bin/env bash
# Daily CRM refresh: pull deltas, rebuild views, freeze the open pipeline.
# Order matters - snapshot reads the views, which read the extracted records.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
    python3 scripts/extract.py
    python3 scripts/transform.py
    python3 scripts/snapshot.py
    echo "=== done ==="
    echo
} >> logs/daily.log 2>&1
