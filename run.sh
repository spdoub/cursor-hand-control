#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

source .venv/bin/activate

if [ ! -f ".venv/.installed" ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing dependencies..."
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  touch .venv/.installed
fi

exec python -m server.main
