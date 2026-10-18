# bizops

Read ARCHITECTURE.md first. PLAN.md has the milestone plan and schema.

## Commands
- Install: `uv venv && uv pip install -e '.[dev]'` (or `pip install -e '.[dev]'`)
- Tests: `.venv/bin/python -m pytest -q`
- Server, no tokens spent: `.venv/bin/bizops serve --dry-run` -> http://127.0.0.1:8000
- Server, live: put ANTHROPIC_API_KEY in .env, then `.venv/bin/bizops serve`
- Run the scheduler until idle (no server): `.venv/bin/bizops tick --dry-run`
- Kill switches: `bizops pause all|org <id>|agent <id>`, `bizops resume ...`
- Owner money: `bizops fund <org_id> <usd>`, `bizops payee "<name>" --org <id> --max 25`

## Rules
- Every tool and model call goes through `gateway.invoke` / `gateway.model_call`. Never call a tool handler directly.
- Policy lives in config/*.yaml and is enforced in code. Never rely on a prompt for safety.
- `events` and `ledger_entries` are append-only (a test enforces it).
- Keep SQL portable (TEXT ids, ISO timestamps, integer micro-dollars).
- A milestone is done when tests pass and `bizops serve --dry-run` shows the seeded company working.
