#!/usr/bin/env bash
# Logging observer only; hooks.on_session_end timeout must be set explicitly.
exec python3 "${BASH_SOURCE[0]%/*}/session_end_log.py"
