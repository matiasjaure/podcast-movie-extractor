#!/usr/bin/env bash
# Local launcher: creates .venv on first run, then starts the web UI.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "Created .env - add your API keys to it, then re-run."; exit 1; }
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
echo "Open http://127.0.0.1:${PORT:-5000}"
exec .venv/bin/python app.py
