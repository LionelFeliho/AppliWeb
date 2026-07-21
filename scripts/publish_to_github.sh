#!/usr/bin/env bash
set -euo pipefail

repository="${1:-LionelFeliho/AppliWeb}"
visibility="${2:---public}"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Authenticate first with: gh auth login" >&2
  exit 1
fi

if gh repo view "$repository" >/dev/null 2>&1; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/${repository}.git"
  git push -u origin main
else
  gh repo create "$repository" "$visibility" --source=. --remote=origin --push
fi
