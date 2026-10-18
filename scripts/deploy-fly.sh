#!/usr/bin/env bash
# Put bizops on the internet (Fly.io), always on, with HTTPS and an owner login.
#   bash scripts/deploy-fly.sh [app-name]
# Needs: a Fly.io account (fly.io, card on file) and an Anthropic API key (asked for if not set).
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

# Model access for the server. An Anthropic API key is the supported route for an always-on server.
# A subscription login token (CLAUDE_CODE_OAUTH_TOKEN) is used only if you set it yourself: check
# Anthropic's current terms before running a subscription login on a 24/7 server.
SECRETS=()
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  SECRETS+=("ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "Using CLAUDE_CODE_OAUTH_TOKEN (subscription login). Check Anthropic's terms for 24/7 server use."
  SECRETS+=("CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN")
elif fly secrets list -a "$APP" 2>/dev/null | grep -Eq '^\s*(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)\b'; then
  echo "Model access already configured on $APP."
else
  echo "Create an API key at https://console.anthropic.com/settings/keys (set a monthly spend limit there)."
  read -r -s -p "Paste your Anthropic API key (input hidden): " KEY; echo
  [ -n "$KEY" ] || { echo "No key given; add one later on the Connect page."; }
  [ -n "$KEY" ] && SECRETS+=("ANTHROPIC_API_KEY=$KEY")
fi
SECRETS+=("PUBLIC_BASE_URL=https://$APP.fly.dev")
PASSWORD=""
# The first owner password is set once; later deploys keep whatever you set on the Connect page.
if ! fly secrets list -a "$APP" 2>/dev/null | grep -Eq '^\s*BIZOPS_PASSWORD\b'; then
  PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
  SECRETS+=("BIZOPS_PASSWORD=$PASSWORD")
fi
# The signing key is created once: rotating it would break unsubscribe links already sent.
if ! fly secrets list -a "$APP" 2>/dev/null | grep -Eq '^\s*BIZOPS_SECRET\b'; then
  SECRETS+=("BIZOPS_SECRET=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 64)")
fi
fly secrets set -a "$APP" --stage "${SECRETS[@]}"
fly deploy -a "$APP" --ha=false --remote-only

echo
echo "bizops is live: https://$APP.fly.dev"
if [ -n "$PASSWORD" ]; then echo "Owner password: $PASSWORD   (save it; change it on the Connect page)"
else echo "Owner password: unchanged"; fi
echo "Next: open the Connect page and connect Stripe and email once."
