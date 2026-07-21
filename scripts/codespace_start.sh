#!/usr/bin/env bash
set -euo pipefail

docker compose up -d db

echo "PostgreSQL is starting on port 5432. The persisted volume is xva_pgdata."
