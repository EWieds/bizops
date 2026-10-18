# bizops

A holding company run by AI agents. Tell it to find a business (or describe one yourself) and a team of
expert agents researches the market, decides how to deliver, builds the product and a website, finds
real business customers, emails them, sends payment links, collects payment through Stripe, onboards
customers and delivers, around the clock, inside spending caps and legal rules enforced in code.

## Put it on the internet (recommended)

```bash
bash scripts/deploy-fly.sh
```

Creates an always-on server on Fly.io with HTTPS, gives Claude access through your subscription
(`claude setup-token`, no API key needed) and prints your dashboard URL and owner password.
Needs a Fly.io account (fly.io, card on file).

## Or run it on your computer

```bash
bash scripts/install.sh            # installs, runs in the background, opens http://127.0.0.1:8000
bizops serve --tunnel              # optional: a temporary public URL (needs cloudflared)
```

## Connect your accounts once (dashboard -> Connect accounts)

These must be in your name, by law, and are connected once for the whole umbrella:
1. **Stripe**: every business sells through it; each payment is labeled with the business that earned it.
2. **Email** (a mailbox on your domain): agents send B2B outreach with your identity, postal address and
   an unsubscribe link, and read replies.
3. **Vapi** (optional): only if a business sells phone agents.

Until they are connected, agents still research, build, publish and prepare, and the "connect" step
waits in your Needs-you queue.

## Try everything for free

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/bizops serve --dry-run      # scripted agents play the whole loop against a simulated market, $0
.venv/bin/python -m pytest -q
```

See ARCHITECTURE.md for how it works, config/policy.yaml for every guardrail.
