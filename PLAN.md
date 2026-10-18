# bizops v1 — M1 plan

Scope: **M1 only** — org engine, agent tree in the DB, wake scheduler, inbox, CEO chat,
read-only dashboard with the activity log. Budget enforcement, the policy engine proper,
spawning, auditor, connectors etc. are later milestones; M1 lays the tables and the single
tool gateway they will plug into, so nothing has to be rewired later.

Done when: `pytest` passes and `bizops serve --dry-run` shows a seeded fake company running
on the scripted dry-run model (no tokens spent).

---

## 1. Layout

```
bizops/
  pyproject.toml            # entry point: bizops = bizops.cli:main
  CLAUDE.md                 # "read ARCHITECTURE.md first" + test/serve commands
  ARCHITECTURE.md           # the engine in one page
  .env.example              # ANTHROPIC_API_KEY, BIZOPS_DB, limits (real .env gitignored)
  config/
    policy.yaml             # standing authorizations + money rules (M1: money rules, message limits)
    models.yaml             # tier -> model id (opus/sonnet/haiku), per-run limits
    templates/generic.yaml  # org template = org chart + prompts + KPIs + default budgets (data)
  src/bizops/
    cli.py                  # bizops serve [--dry-run] | seed | migrate | pause/resume
    config.py               # .env + yaml loading
    db.py                   # sqlite (WAL), connection helper, migration runner
    migrations/0001_m1.sql  # portable SQL (schema below)
    ids.py                  # sortable ULID-style ids (TEXT, works on Postgres unchanged)
    repo.py                 # data access; events/ledger are insert-only (no update/delete fns)
    engine.py               # org engine: create_org_from_template, tree queries, depth calc
    policy.py               # v0 money rules (cap-raise rule) + M1 action checks (grants, hops, TTL, peer scope)
    gateway.py              # THE one path for every tool + model call
    tools/                  # tool registry: name -> (grant, schema, handler)
      comms.py              #   message, reply, create_task, update_task, yield
      memory.py             #   note_write, note_read, kb_write, kb_read
      files.py              #   write_artifact (sandboxed to data/artifacts/<org>/<agent>)
    llm/
      base.py               # LLMClient protocol: complete(system, messages, tools) -> response+usage
      anthropic_client.py   # real client (prices from models.yaml -> model_cost)
      dry_run.py            # scripted, deterministic, role-aware fake model
    runtime.py              # one agent run: wake -> context -> tool loop -> yield
    scheduler.py            # asyncio wake scheduler (event-driven + timers)
    seed.py                 # platform root org + one fake business for --dry-run
    web/
      app.py                # FastAPI app; scheduler started in lifespan
      routes.py
      templates/*.html      # Jinja + htmx + Tailwind CDN + Chart.js CDN, no build step
  tests/
```

## 2. Schema (M1 migration, portable SQL)

Conventions: ids are TEXT (sortable, generated in Python) so Postgres is a config change;
timestamps are ISO-8601 UTC TEXT; money is INTEGER **micro-dollars** (token costs are
fractions of a cent); JSON is TEXT. No SQLite-only types or AUTOINCREMENT. WAL mode is set
at connect time, not in the schema.

```sql
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);

-- An org is a company. Root org = the platform; each business is a child org.
CREATE TABLE orgs (
  id               TEXT PRIMARY KEY,
  parent_org_id    TEXT REFERENCES orgs(id),          -- NULL only for the platform root
  name             TEXT NOT NULL,
  slug             TEXT NOT NULL UNIQUE,
  template         TEXT NOT NULL,                     -- 'generic', 'rank-and-rent', ...
  brief            TEXT NOT NULL DEFAULT '',          -- the owner's brain-dump
  status           TEXT NOT NULL DEFAULT 'active',    -- active | trial | paused | retired
  monthly_cap_micros INTEGER NOT NULL DEFAULT 0,      -- enforced from M2
  settings_json    TEXT NOT NULL DEFAULT '{}',        -- max_agents, max_depth, ... (limits enforced M3)
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

-- A node in an org's tree. Roles are data, not code.
CREATE TABLE agents (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  parent_agent_id  TEXT REFERENCES agents(id),        -- org CEO's parent = platform CEO
  role             TEXT NOT NULL,                     -- 'ceo', 'coo', 'marketer', ...
  name             TEXT NOT NULL,
  system_prompt    TEXT NOT NULL,
  model_tier       TEXT NOT NULL DEFAULT 'worker',    -- exec | worker | bulk -> models.yaml
  model_override   TEXT,
  grants_json      TEXT NOT NULL DEFAULT '[]',        -- tool names / grant groups
  kpi_json         TEXT NOT NULL DEFAULT '{}',
  budget_micros    INTEGER NOT NULL DEFAULT 0,        -- monthly allocation (enforced M2)
  depth            INTEGER NOT NULL DEFAULT 0,        -- 0 = org CEO
  status           TEXT NOT NULL DEFAULT 'active',    -- active | paused | retired
  wake_interval_s  INTEGER,                           -- timer wake; NULL = only on events
  min_interval_s   INTEGER NOT NULL DEFAULT 30,       -- never run more often than this
  next_wake_at     TEXT,                              -- next timer fire
  wake_requested_at TEXT,                             -- set on new message/task; cleared on run start
  last_run_at      TEXT,
  created_by       TEXT,                              -- agent id, 'human', or 'template'
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_agents_org ON agents(org_id);
CREATE INDEX ix_agents_parent ON agents(parent_agent_id);

CREATE TABLE tasks (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  assignee_id      TEXT NOT NULL REFERENCES agents(id),
  created_by       TEXT NOT NULL,                     -- agent id or 'human'
  parent_task_id   TEXT REFERENCES tasks(id),
  title            TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  deliverable      TEXT NOT NULL,                     -- the checkable "done means..."
  status           TEXT NOT NULL DEFAULT 'open',      -- open | in_progress | done | blocked | failed | cancelled
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  result           TEXT,
  blocked_reason   TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_tasks_assignee ON tasks(assignee_id, status);

-- Inbox. Human is a sender/recipient kind, not a fake agent row.
CREATE TABLE messages (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  sender_kind      TEXT NOT NULL,                     -- agent | human | system
  sender_id        TEXT,                              -- agent id when sender_kind = agent
  recipient_kind   TEXT NOT NULL,                     -- agent | human
  recipient_id     TEXT,
  type             TEXT NOT NULL,                     -- instruction|question|answer|recommendation|escalation|status|chat
  body             TEXT NOT NULL,
  thread_id        TEXT NOT NULL,                     -- root message id
  in_reply_to      TEXT REFERENCES messages(id),
  hop              INTEGER NOT NULL DEFAULT 0,        -- reply chain depth; refused past max_hops
  expires_at       TEXT NOT NULL,                     -- TTL; expired messages never wake anyone
  status           TEXT NOT NULL DEFAULT 'unread',    -- unread | read | expired
  created_at       TEXT NOT NULL,
  read_at          TEXT
);
CREATE INDEX ix_messages_inbox ON messages(recipient_kind, recipient_id, status);
CREATE INDEX ix_messages_thread ON messages(thread_id);

CREATE TABLE runs (
  id               TEXT PRIMARY KEY,
  agent_id         TEXT NOT NULL REFERENCES agents(id),
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  wake_reason      TEXT NOT NULL,                     -- message | task | timer | manual
  model            TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'running',   -- running | yielded | turn_limit | token_limit | error | stopped
  turns            INTEGER NOT NULL DEFAULT 0,
  input_tokens     INTEGER NOT NULL DEFAULT 0,
  output_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_micros      INTEGER NOT NULL DEFAULT 0,
  summary          TEXT,                              -- agent's yield note
  error            TEXT,
  started_at       TEXT NOT NULL,
  ended_at         TEXT
);
CREATE INDEX ix_runs_agent ON runs(agent_id, started_at);

-- Append-only audit log. Gateway writes 'before' prior to acting and 'after' with the result
-- (two rows linked by call_id, never an UPDATE).
CREATE TABLE events (
  id               TEXT PRIMARY KEY,                  -- sortable => timeline order
  ts               TEXT NOT NULL,
  org_id           TEXT REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  run_id           TEXT REFERENCES runs(id),
  actor_kind       TEXT NOT NULL,                     -- agent | human | system | scheduler
  kind             TEXT NOT NULL,                     -- tool | model | message | wake | run | spawn | spend | decision | human
  action           TEXT NOT NULL,                     -- tool name / 'llm.call' / 'run.start' ...
  phase            TEXT NOT NULL,                     -- before | after | point
  call_id          TEXT,                              -- links before/after
  reason           TEXT,                              -- the agent's stated "why"
  args_json        TEXT,
  result_json      TEXT,
  outcome          TEXT,                              -- ok | refused | error | (NULL on before)
  cost_micros      INTEGER NOT NULL DEFAULT 0,
  ref_type         TEXT,                              -- task | message | artifact | agent ...
  ref_id           TEXT
);
CREATE INDEX ix_events_org_ts ON events(org_id, ts);
CREATE INDEX ix_events_agent_ts ON events(agent_id, ts);
CREATE INDEX ix_events_action ON events(action);

-- Append-only money ledger. M1 writes model_cost only; spend/revenue arrive in M2/M4.
CREATE TABLE ledger_entries (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  run_id           TEXT REFERENCES runs(id),
  kind             TEXT NOT NULL,                     -- model_cost | spend | revenue | allocation | reserve
  amount_micros    INTEGER NOT NULL,                  -- positive; kind gives direction
  source           TEXT NOT NULL,                     -- model | connector | human | system (agents never write revenue)
  memo             TEXT,
  event_id         TEXT REFERENCES events(id)
);
CREATE INDEX ix_ledger_org_ts ON ledger_entries(org_id, ts);

-- Per-agent notes + org knowledge base. 'summary' notes are rewritten by compaction.
CREATE TABLE memory_notes (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),        -- NULL = org knowledge base
  key              TEXT NOT NULL,
  body             TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_memory_key ON memory_notes(org_id, agent_id, key);

CREATE TABLE artifacts (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  task_id          TEXT REFERENCES tasks(id),
  name             TEXT NOT NULL,
  path             TEXT NOT NULL,                     -- relative to data/artifacts
  mime             TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  sha256           TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
```

Later migrations (not in M1): `policy_decisions` (M2), `metrics`, `experiments`,
`tournaments` (M4), `auditor_findings`, `decisions` (M3), `proposals` (M5).
Note: `UNIQUE(org_id, agent_id, key)` with NULL agent_id doesn't dedupe in SQL; the repo
layer upserts by lookup, so behaviour is identical on SQLite and Postgres.

## 3. The engine

**Org engine (`engine.py`).** `create_org_from_template(template, name, brief, parent_org,
supervisor_agent)` reads `config/templates/<t>.yaml` and inserts the org and its starting
chart. The org CEO's `parent_agent_id` is the platform CEO, which is the only cross-org edge.
No role is special in code; "CEO" is just `depth = 0` in a non-root org. (M5 exposes this as
the `create_org` tool; M3 adds `spawn_agent` on the same function.)

**Gateway (`gateway.py`).** `invoke(ctx, tool, args, reason) -> ToolResult` is the only way
any tool or model call executes:
1. policy check: tool exists, agent holds the grant, agent/org not paused, plus tool-specific
   checks (message hop ≤ max_hops, TTL, recipient in same org or direct supervisor/report).
   M2 replaces this step with the full policy engine + standing authorizations.
2. write `before` event (with the agent's stated reason).
3. budget/limits check: per-run turn and token limits (M1); budget tree from M2.
4. run the handler.
5. write `after` event with result/outcome/cost; refusals and errors are logged the same way.
Model calls go through the same path as action `llm.call`, so cost is recorded on every call
(and in M2 enforced mid-run). Tool handlers receive a narrow context object with no
secrets; credentials live in clients the handlers close over.

**Agent run (`runtime.py`).** On wake: mark unread inbox read, load open tasks, memory
summary + org KB headlines, role/KPI → system prompt + one user turn → tool loop through the
gateway until the model calls `yield(summary)` or a limit hits (max_turns, max_tokens from
models.yaml). Run row records turns, tokens, cost, status. Memory compaction: when an agent's
notes exceed N chars, the next run is asked to rewrite its `summary` note (dry-run does it
deterministically).

**Tools in M1:** `message(to, type, text)`, `reply(message_id, text)` (hop+1),
`recommend_up(text)`, `escalate(text)` (both = message to supervisor with type set),
`create_task(assignee, title, deliverable, ...)` (assignee must be self or a descendant),
`update_task(task_id, status, result)`, `note_write/note_read`, `kb_write/kb_read`,
`write_artifact(name, content)`, `reply_human(text)` (CEOs only — the chat path), `yield`.

**Scheduler (`scheduler.py`).** One asyncio task in the serve process. An agent is due when
it's `active` (and its org isn't paused) AND (`wake_requested_at` set OR `next_wake_at <= now`)
AND `last_run_at + min_interval_s <= now`. Writing a message/task sets
`wake_requested_at` and pokes an `asyncio.Event`; a 1s poll is the fallback. Global
concurrency via a semaphore (`BIZOPS_MAX_CONCURRENT_RUNS`, default 4). Expired messages
are swept to `expired` and never wake anyone. Sync DB work runs via `asyncio.to_thread`
with one connection per thread (WAL allows concurrent readers + one writer).

**Dry-run model (`llm/dry_run.py`).** Same interface as the real client, deterministic,
role-aware: a CEO receiving a human chat message replies via `reply_human` and delegates
with `create_task`; a worker with an open task writes an artifact + `update_task(done)` +
`status` message to its supervisor; supervisors acknowledge status with notes; timer wakes
produce a short status. It emits fake token counts so cost/ledger/UI show real-looking
numbers (priced at $0 by default, flag to simulate prices).

**Seed (`seed.py`).** Platform root org ("bizops platform": CEO, COO, CFO) and one fake
business from the `generic` template ("Acme Lawn Leads": CEO, COO, CFO, marketer, writer,
researcher), plus one human brief message to the business CEO so the log fills up on start.

## 4. policy.py (money rules)

The v0 cap rule is kept as pure functions with tests, exactly as the spec states it: cap may
rise only after **two complete profitable months**, by at most **25% per month**, never above
the owner's **hard ceiling**. M1 doesn't call it from anywhere yet (budgets are M2) but it
stays tested. M1 also holds the message rules (max_hops default 6, TTL default 24h) and the
per-tool grant check.

## 5. Dashboard (read-only + chat)

FastAPI + Jinja + htmx (5s polling on live panels) + Tailwind CDN + Chart.js CDN. Binds
127.0.0.1 by default; no auth (README notes: add auth before exposing).

- `/` Platform overview — orgs table (status, agents, open/blocked tasks, model spend this
  month vs cap, health dot from error rate + blocked tasks), totals, spend-per-day chart.
- `/orgs/{id}` — org chart tree, tasks, recent activity, spend by agent.
- `/agents/{id}` — inbox, tasks, runs (turns/tokens/cost), memory notes, prompt + grants.
- `/activity` — the "what did it do" timeline: filter by org, agent, kind, action, outcome,
  min cost, time range, free text; each row = who / what / why / cost / produced (links to
  task, message, artifact). Before/after rows collapse into one line with a status. "Explain"
  button sends the row (or a day) to the platform CEO and opens the chat thread for the answer.
- `/chat` — pick the platform CEO or any org CEO; send drops a `chat` message in its inbox
  and wakes it; replies appear in the thread (polling).
- `/artifacts/{id}` — view a produced file.
- Pause/resume buttons are **not** in M1 (kill switches are M2); `bizops pause` CLI exists
  for testing the scheduler's status checks.

## 6. Tests

- migrations apply on a fresh DB; WAL enabled; schema has no SQLite-only types.
- money rules (two profitable months, 25% step, ceiling, rounding).
- gateway: every call writes before+after events; ungranted tool refused + logged; paused
  agent refused; handler exception → `error` event, run continues.
- messages: hop limit refusal, TTL expiry sweep, cross-org peer message refused, supervisor
  edge allowed.
- scheduler: wakes on message, respects min_interval, timer wake, concurrency cap, skips
  paused agents/orgs, does nothing when nothing is due.
- runtime: turn limit and token limit stop a run; yield ends it; ledger gets model_cost.
- end-to-end dry run: seed → run scheduler for N ticks → tasks created and completed,
  artifacts written, human chat answered.
- web: every page renders 200 on the seeded DB; chat post creates message + wake.

## 7. Not in M1 (by design)

Budget tree enforcement, policy_decisions + hard-stop queue, kill-switch UI, spawn/retire,
auditor, connectors, experiments, tournaments, ideas pipeline, "new business" button.

---

## Update: scope pulled into M1 at the owner's request

After plan review the owner asked for safeguards, real action execution, fast internet access, a Jev
hook and a safe way to give agents money. So M1 also ships these pieces of M2/M4 (the schema in
`migrations/0001_init.sql` adds `settings`, `policy_decisions`, `payees`):

- Policy engine with standing authorizations, hard stops, rate limits (`policy.py`, `config/policy.yaml`).
- Budget tree enforced on every model call and spend, platform daily/monthly caps (`budget.py`).
- Approver chain for risky actions: rules -> Jev (HTTP, `JEV_API_URL`) -> LLM. Blocks or allows; never waits.
- Needs-a-human queue + blocked-actions list with resolve/override; kill switches (UI + CLI).
- Web tools: `web_search` (Brave or Anthropic server-side search) and `fetch_url` (async, SSRF-guarded,
  cached, untrusted-content wrapping).
- Wallets: owner deposits, owner-allowlisted payees, `pay` with per-payment/payee/day/wallet/budget limits,
  idempotency and reversal. Provider is `sandbox`; a real provider plugs in behind `PaymentProvider`.

Still not built: spawn/retire agents (M3), auditor (M3), email/Stripe connectors (M4), experiments and
tournaments (M4), ideas pipeline (M5), real payments provider.
