#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -r apps/api/requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo "XVA Codespace setup complete. Use F5 for FastAPI or run 'docker compose up --build'."
