#!/usr/bin/env bash
# Daily CRM refresh: pull deltas, rebuild views, freeze the open pipeline.
# Order matters - snapshot reads the views, which read the extracted records.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs

# Same one-generation rotation as daily.ps1 - this file only ever grew.
if [ -f logs/daily.log ] && [ "$(wc -c < logs/daily.log)" -gt 5242880 ]; then
    mv -f logs/daily.log logs/daily.log.1
fi

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
    python3 scripts/extract.py
    python3 scripts/transform.py
    python3 scripts/snapshot.py
    echo "=== done ==="
    echo
} >> logs/daily.log 2>&1
