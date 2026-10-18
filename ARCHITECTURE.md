# bizops architecture

One process (`bizops serve`): FastAPI dashboard + asyncio scheduler + SQLite (WAL).

## One engine, everything is data
- **Org**: a company. The platform is the root org; every business is a child org. Created from a
  YAML template (`config/templates/*.yaml`) = starting org chart + playbook + KPIs + budgets.
- **Agent**: a node in an org's tree (role, prompt, model tier, grants, budget, KPIs, wake schedule).
  No role is special in code. An org's CEO is its depth-0 agent, whose supervisor is the platform CEO.
- **Task, Message, Run, Event, LedgerEntry, PolicyDecision, Payee, MemoryNote, Artifact**: see
  `src/bizops/migrations/0001_init.sql`.

## Flow of one agent run (`runtime.py`)
1. Scheduler (`scheduler.py`) finds a due agent: active, org active, platform not paused, has a pending
   wake (new message/task, or a delegated task finished) or its timer fired, and its min interval passed.
   A global semaphore caps concurrent runs.
2. Runtime builds the wake-up context: inbox (marked read), open tasks, delegated task updates, team,
   memory, budget, wallet. Plus a `<context_json>` block.
3. Tool loop: `gateway.model_call` -> tool calls run in parallel through `gateway.invoke` ->
   results back -> until `yield_run`, a turn/token limit, a kill switch or a budget stop.

## The gateway (`gateway.py`): the only path to act
For every tool call:
1. **Policy** (`policy.check_action`): kill switches, grant, hard stops, standing authorization, rate limit.
   Hard stops go to the needs-a-human queue (task marked blocked; the agent keeps working).
2. **Event 'before'** written.
3. **Budget** (`budget.check_spend`): platform month/day caps, org cap, agent budget.
   Then the **approver chain** for risky actions (`approver.py`: rules -> Jev -> LLM). It allows or
   blocks on its own; low confidence blocks. It never waits for a human.
4. **Action** (handler; exceptions become logged errors, timeouts enforced).
5. **Event 'after'** + ledger entry for any cost.

Model calls get the same treatment (`llm.call`): kill switch + budget checked before every call,
cost written to the ledger after.

## Safety model
- Caps/budgets are code, set in `config/policy.yaml`. Agents cannot raise them; v0 cap-raise rule in
  `policy.max_allowed_cap` (2 profitable months, +25%/month max, hard ceiling).
- Agents never see secrets. Tools read credentials via `Settings.secret()`.
- Internet (`webclient.py`): async httpx, shared pool, concurrency limit, timeouts, byte caps, cache,
  SSRF guard (no private/loopback/link-local hosts, every redirect re-checked), results wrapped as
  `<untrusted_web_content>` so injected instructions stay data.
- Money (`payments.py`, `tools/money.py`): only the owner deposits and allowlists payees. `pay` checks
  payee, per-payment max, payee max, daily org max, wallet balance and budget atomically, needs a memo
  and a task, passes the approver, is idempotent, and reverses on provider failure. Provider is
  `sandbox` until a real one is plugged in.
- Messages: peers only within an org (plus direct supervisor/report edges), hop limit, TTL.
- Revenue/metrics are written only by connectors or the owner (ledger source rules).
- Kill switches: platform, org, agent. Dashboard buttons + `bizops pause`.

## Model clients (`llm/`)
- `anthropic_client.py`: official SDK, adaptive thinking, effort per tier, prompt caching, server-side
  refusal fallbacks, Anthropic web search for the `web_search` tool.
- `dry_run.py`: scripted, deterministic, role-aware. Plays out delegation, research, payments, hard
  stops and chat replies through the real gateway. Simulated prices.

## Dashboard (`web/`)
Overview, org page (chart, tasks, artifacts, KB), agent page (inbox, runs, cost, memory), activity log
(filter/search, details, "explain"), chat with any CEO, needs-a-human + blocked actions with
override, money (wallets, payees, ledger), new business.
