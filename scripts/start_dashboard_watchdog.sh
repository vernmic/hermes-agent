#!/usr/bin/env bash
# Idempotent compatibility launcher; scheduled task supplies continuous recovery.
set -euo pipefail
dashboard_home="${HERMES_HOME:-$HOME/.hermes}"
exec /usr/bin/python3 "$dashboard_home/scripts/dashboard_watchdog.py" \
    --home "$dashboard_home" --once
