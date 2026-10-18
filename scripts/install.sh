#!/usr/bin/env bash
# One command: install bizops, run it in the background forever, open the dashboard.
#   bash scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
HOME_DIR="$(pwd)"

if command -v uv >/dev/null 2>&1; then
  uv venv -q .venv && uv pip install -q --python .venv/bin/python -e .
else
  python3 -m venv .venv && .venv/bin/pip install -q -e .
fi

# Model access: uses an API key if you have one, otherwise your Claude login via Claude Code.
if ! command -v claude >/dev/null 2>&1 && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Installing Claude Code so bizops can use your Claude login (no API key needed)..."
  if command -v npm >/dev/null 2>&1; then npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 || true; fi
fi
if command -v claude >/dev/null 2>&1 && ! claude auth status --json 2>/dev/null | grep -q '"loggedIn": true'; then
  echo "Log in to Claude once (a browser window opens), then come back here."
  claude auth login || claude || true
fi

.venv/bin/bizops init-secret   # the signing key for logins and links, kept in .env
.venv/bin/bizops install-service --home "$HOME_DIR"
sleep 3
URL="http://127.0.0.1:8000"
echo "bizops is running in the background: $URL"
(command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true
