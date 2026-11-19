#!/usr/bin/env bash
# Local launcher: creates .venv and installs dependencies on first run, then starts the web UI.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - add your API keys to it, then re-run ./run.sh"
  exit 1
fi

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "python3 not found - install Python 3.10+ first."; exit 1; }

# .venv/.installed marks a finished install, so an interrupted one is retried instead of skipped.
if [ ! -f .venv/.installed ]; then
  echo "First run: installing dependencies into .venv (a few hundred MB, takes a few minutes)..."
  [ -d .venv ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.installed
  echo "Dependencies installed."
fi

echo "Starting... open http://127.0.0.1:${PORT:-5000} (Ctrl+C to stop)"
exec .venv/bin/python app.py
