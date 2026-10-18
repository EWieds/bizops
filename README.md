# bizops

An umbrella of AI-run businesses. One org engine; the platform and every business are the same thing
with different data. See ARCHITECTURE.md.

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/bizops serve --dry-run      # seeded fake company on a scripted model, $0
.venv/bin/python -m pytest -q
```

Live mode: copy `.env.example` to `.env`, set `ANTHROPIC_API_KEY`, review `config/policy.yaml`
(caps!), then `bizops serve`. The dashboard has no auth: keep it on localhost.
