# bizops

An umbrella of AI-run businesses. You press "new business", brain-dump to a CEO agent, and a team of
expert agents (CEO, COO, CFO/investment strategist, researchers, marketers, writers, engineers...) builds
and runs it around the clock, inside spending caps and hard rules enforced in code. A holding-company
layer (platform CEO, chief investment officer, outside auditor, venture analyst) oversees everything.

## Install (one command)

```bash
bash scripts/install.sh
```

That installs bizops, makes sure it can reach Claude, starts it in the background (it restarts on
login and after crashes) and opens the dashboard at http://127.0.0.1:8000.

**No API key needed.** bizops finds model access by itself, in this order:
1. `ANTHROPIC_API_KEY` in the environment or `.env` (optional)
2. your **Claude login** through Claude Code (`claude`): the installer installs it and asks you to log in once
3. otherwise it runs the free scripted demo model so you can look around

## Try it without spending anything

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/bizops serve --dry-run      # seeded demo company on a scripted model, $0
.venv/bin/python -m pytest -q
```

See ARCHITECTURE.md for how it works and config/policy.yaml for the guardrails (caps, payment limits,
hard stops). The dashboard has no login: keep it on localhost.
