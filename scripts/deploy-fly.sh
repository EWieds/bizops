#!/usr/bin/env bash
# Put bizops on the internet (Fly.io), always on, with HTTPS and an owner login.
#   bash scripts/deploy-fly.sh [app-name]
# Needs: a Fly.io account (fly.io, card on file) and a Claude subscription (or an Anthropic API key).
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${1:-bizops-$(whoami | tr -cd 'a-z0-9' | head -c 12)-$RANDOM}"
ORG="${FLY_ORG:-personal}"      # e.g. FLY_ORG=evan-wieds
# Unattended mode: with FLY_API_TOKEN set (an org deploy token), no browser login is needed.

if ! command -v fly >/dev/null 2>&1; then
  echo "Installing flyctl..."; curl -fsSL https://fly.io/install.sh | sh
  export PATH="$HOME/.fly/bin:$PATH"
fi
if [ -z "${FLY_API_TOKEN:-}" ]; then fly auth whoami >/dev/null 2>&1 || fly auth login; fi

sed -i.bak "s/^app = .*/app = \"$APP\"/" fly.toml && rm -f fly.toml.bak
fly apps create "$APP" --org "$ORG" >/dev/null 2>&1 || true
fly volumes list -a "$APP" 2>/dev/null | grep -q bizops_data || fly volumes create bizops_data --size 3 --region iad -a "$APP" --yes

# Model access for the server: a long-lived Claude token from your subscription (no API key needed),
# unless ANTHROPIC_API_KEY is already set in your shell.
SECRETS=()
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  SECRETS+=("ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  SECRETS+=("CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN")
else
  command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code
  echo "Creating a long-lived Claude token for the server (a browser window may open)..."
  TOKEN="$(claude setup-token | grep -Eo 'sk-ant-[A-Za-z0-9_-]+' | tail -1 || true)"
  if [ -z "$TOKEN" ]; then read -r -p "Paste the token printed above: " TOKEN; fi
  SECRETS+=("CLAUDE_CODE_OAUTH_TOKEN=$TOKEN")
fi
PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
SECRETS+=("BIZOPS_PASSWORD=$PASSWORD" "PUBLIC_BASE_URL=https://$APP.fly.dev" "BIZOPS_SECRET=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 48)")
fly secrets set -a "$APP" --stage "${SECRETS[@]}"
fly deploy -a "$APP" --ha=false --remote-only

echo
echo "bizops is live: https://$APP.fly.dev"
echo "Owner password: $PASSWORD   (save it; change it on the Connect page)"
echo "Next: open the Connect page and connect Stripe and email once."
