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

## Agents are experts (config/roles.yaml)
Every agent is built from a role in the role library: a senior professional persona with its working
method (the CEO is an expert in business management and oversight; the CFO and the platform's Chief
Investment Officer are experts in smart investing and capital allocation; the auditor is a forensic
auditor; and so on), plus a default model tier, tool grants and heartbeat. Templates pick roles and add
an org-specific `focus`.

## The harness prompt (runtime.py)
System prompt, in stable layers (cache-friendly): persona -> where you work (org, owner's brief,
playbook) -> your seat (supervisor, reports, tools, KPIs) -> operating rhythm (inbox, goals, tasks,
team, memory, yield) -> rules enforced in code -> quality bar. The per-wake user message carries the
volatile state: why you woke, inbox, your goals, team goals, open tasks, delegated results, memory,
budget, wallet. The dashboard shows both for any agent ("show the full harness prompt").

## Goals and loops
- Kickoff: a new org's CEO gets a kickoff instruction to set 2-4 measurable goals, cascade them to
  reports (set_goal with parent_goal_id), and create the first tasks.
- Goals have a metric, target, due date and review cadence. Agents set status and progress notes;
  the measured value is written only by the owner or a connector.
- Heartbeats (maintenance.py, code only, no model cost): goal reviews when due, stalled-task nudges
  then escalation to the delegator, budget alerts at 80% of cap, the platform CEO's daily digest to the
  owner, message expiry, and code-level audit checks (message loops, cost spikes, achieved-without-
  evidence goals, agents that run but finish nothing).
- Agent heartbeats: each role wakes on a schedule, but a heartbeat with nothing to act on (no inbox,
  tasks, goals to set, or daily review due) is skipped for free.

## Hiring and the auditor
- spawn_agent hires from the role library under the hirer; the budget moves out of the hirer's own
  budget; headcount, depth and reports-per-manager limits apply; hires never get money or platform
  access their manager lacks. retire_agent returns budget and moves tasks, goals and reports up.
- The outside auditor sits in the platform org with no parent (reports to the owner), reads raw
  evidence (audit_evidence) and records findings with evidence (record_finding).

## Model backends (llm/)
Auto-detected (core.detect_backend): API key or `ant` profile -> official SDK; else the Claude login on
the machine via the `claude` CLI (llm/claude_cli.py: one headless call per turn, built-in tools off,
JSON schema for proposed tool calls, exact cost from the CLI); else the scripted dry-run model.

## The money loop (business.py, tools/sales.py, connectors/)
research -> decide (memo) -> build (publish_page, create_voice_agent, ...) -> create_offer (Stripe product +
price + payment link, labeled with the business) -> find_businesses / extract_contacts / add_lead ->
send_email (compliance in code) -> replies arrive (IMAP poll) -> send_payment_link (tracked per lead) ->
Stripe reports the payment (event poll, no public webhook needed) -> revenue in the ledger (connector
source, per business, 30-day holdback before it counts as cleared) -> customer created -> customer
success onboards and delivers -> goals with a metric_key are measured from this data automatically.

Compliance enforced in code: B2B only with a public source, sender identity + postal address +
one-click unsubscribe on every email, suppression list, unsubscribe replies honored automatically,
per-business daily cap, follow-up gap, max cold touches, spammy wording blocked, inbound calls only.
Outside dry runs, a real lead can't be emailed until a real mailbox is connected, and offers need
Stripe: each is one queue item for the owner (deduplicated), and agents keep working meanwhile.

Connectors: connectors/billing.py (Stripe or test checkout), connectors/email.py (SMTP/IMAP or test
outbox), connectors/voice.py (Vapi or web-demo only). simworld.py is the simulated market used only by
dry runs and tests; its money is marked sandbox and never counts as revenue.

## Public web (web/public.py, web/auth.py)
One server: the dashboard (owner login required; without a password it only answers this machine) and
public customer-facing routes: /s/<business>/<page> websites, /demo/<agent> live voice-agent demos,
/pay/<offer> checkout, /u/<token> unsubscribe, /f/<file>/<sig> shared deliverables. Deploy:
Dockerfile + fly.toml + scripts/deploy-fly.sh, or `bizops serve --tunnel` from a computer.

## Ventures
The platform's venture analyst proposes businesses; the platform CEO launches them with create_business
inside the venture budget (max new per month, max on trial, starting cap). New businesses start on trial.

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
