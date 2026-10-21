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
The same poll applies renewals (invoice.paid, matched to the first checkout through the subscription),
refunds (charge.refunded, cumulative, so replays are harmless) and chargebacks (charge.dispute.*:
reversed when opened, restored only if won, plus a Needs-you item to respond in Stripe). Reversals are new
`revenue_reversal` ledger rows; `ledger.revenue` and `business.cleared_revenue_cents` are always net.

## Capital allocation (capital.py, tools/capital.py)
Monthly, once a month's holdback has passed, `capital_cycle` closes it per org: verified profit (cleared
revenue net of refunds/disputes, minus all spend) -> 25% tax reserve -> operating reserve (2 months of
costs) -> surplus. Two keys: the org CEO proposes (reinvest in self and/or send to the platform treasury,
with reasoning, numbers and expected result); the CFO (or the platform CFO if the org has none) scores and
approves (executes) or rejects (escalates to the platform CEO). The platform CEO proposes treasury -> org
moves; the platform CFO is the second key. Reinvesting raises the recipient's monthly cap. Guardrails in
`check_proposal`, re-checked at execution: verified profit only; >= 50% reinvested in self unless both keys
write a case; no org gets > 40% of a month's treasury; trial orgs <= venture cap; losing orgs need a case;
caps rise <= 25%/month and stay inside the platform cap; the treasury floor is never spent; there is no
way to move money out (owner distributions are a hard stop). Executed allocations go to the auditor and
are later scored against what happened. Money page: treasury, reserves, each org's waterfall, decision log.

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

Security (security.py, web/auth.py): BIZOPS_SECRET is required (>= 32 chars; the app refuses to start
without it, dry runs get a throwaway one) and each use derives its own key (file links, sessions...).
Owner sessions are server-side (`sessions` table stores only a hash of the cookie) and revocable; changing
the password logs everyone out. 5 failed logins per address in 15 min lock it for 15 min. Every dashboard
POST needs the CSRF token (header from htmx, hidden field added to forms by base.html; double-submit cookie
when there is no password). Client IPs come from fly-client-ip / cf-connecting-ip only when actually behind
that proxy. The public voice demo goes through `gateway.public_model_call`: kill switch, org budget and a
per-business daily ceiling are checked BEFORE the model call.

## Ideas pipeline (ideas.py, tools/ideas.py)
Launch many, keep what sticks (`ideas.launch_bar`, policy `venture`): a reviewed proposal launches in code
right after review (`auto_launch`, `launch_sweep`) when it needs $500 or less to start (startup + first month),
or is a safe bet (red team: low legal risk, few owner steps, cheap to test), or its red-team score clears a bar
set from its breakdown. Legal risk under 2.5 never launches. The red team's verdict is a to-do list, not a veto:
its required changes become the new CEO's first tasks; review rounds never end in "parked". Each launch gets
its starting capital (up to $500) moved from the platform's wallet (the venture fund the owner loads) into its
own wallet (ledger `allocation_out`/`allocation_in`); businesses hire freelancers for the human parts through
card purchases. Trials whose real outreach gets no reply or payment within 14 days are paused; a first real
payment graduates a trial to active (`maintenance.trial_reviews`). Venture analysts and red teamers work as
pools (`ideas.ensure_pools`, least busy first).
YouTube channels (`idea_sources.py`): the owner adds channels on the Ideas page and picks the order (most viewed
first, or newest first) and how many (all, or the top N). The whole channel is listed (YouTube Data API with
YOUTUBE_API_KEY; else yt-dlp, which covers roughly the newest 150-200 videos), then a few videos per check are
sent to the venture analyst (transcript saved as an artifact, plus a task to turn it into proposals that go
through the red team). New uploads jump the queue; shorts are skipped.
maintenance runs research_cycle every venture.research_every_days: it tasks the venture analyst, who
submits proposals (submit_proposal: weighted 1-5 scores, sourced evidence, plan, kill criteria; duplicates
refused) with a full business case: price + model + sourced competitor prices, exactly who the first 10
customers are and how we reach them, channels with cost and conversion, positioning, startup and monthly
costs, CAC / margin / LTV, days to first revenue, monthly projections, assumptions, pre-mortem.
`ideas.validate_case` recomputes the arithmetic (profit, cumulative profit, break-even) and flags optimism
(revenue > customers x price, LTV/CAC < 3, CAC $0, margin < 50%, slow first sale, hockey sticks, burn vs
budget). Every proposal then goes to the independent red team (role red_team: no parent, like the auditor;
can't write proposals or review its own). Its review must be thorough (enforced: risks with likelihood and
impact, sourced counter-evidence, 3+ challenged assumptions, its own scores on every criterion, its own
conservative projection); owner ideas need more (5 risks, 2 counter-evidence) and the brief says no
deference. Verdict: go -> 'new' (decidable), fix -> back to the author (revise_proposal, max
venture.red_team_max_rounds), no_go -> parked. Only then is the platform CEO told. Agents launch only a 'go'
with a red-team score >= auto_launch_min_score and positive red-team month-6 profit, inside the venture
budget; the owner can overrule from /ideas (recorded as an override). Owner ideas (/new, chat "launch")
go through the analyst and red team like everything else; there is no direct create-business path. On
launch the brief carries the business case and red-team review, and two measured goals are seeded (first
paying customer by the red team's timeline; revenue on plan for 60 days).
Owner research parameters (Ideas page, stored in settings `venture:params`): guidance (focus, avoid,
geography, customer type, models, risk tolerance, owner involvement, notes) is shown to the analyst, red team
and platform CEO on every wake and in research/red-team tasks; hard limits (max startup / monthly cost, max
days to first revenue, min price, min month-6 profit) refuse research proposals in code and only flag the
owner's own ideas.

## Chat and media (web/app.py chat_post, media.py)
Owner messages can carry files (PDF/DOCX/text/CSV: original + extracted text saved as artifacts) and
YouTube links (title/channel via oEmbed, transcript via youtube-transcript-api, saved as an artifact). The
agent sees previews wrapped as untrusted content and reads the full text with read_artifact. Quick actions
append a precise brief (business plan / website / launch). Replies render as markdown with artifact links.

## Jev and quality (decide.py)
Decider answers typed questions with Jev (TypeSafe System One) when TYPESAFE_API_KEY is set, else the
cheapest Claude tier: reply triage, heartbeat triage, model routing, approver step, and the quality gates
that score outbound emails, published pages and "done" claims before they ship.

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
- Caps/budgets are code, set in `config/policy.yaml`. Per-agent monthly budgets are off by default
  (`budgets.agent_budgets`): agents are limited by their org's monthly cap and the platform caps. Agents cannot raise them; v0 cap-raise rule in
  `policy.max_allowed_cap` (2 profitable months, +25%/month max, hard ceiling).
- Agents never see secrets. Tools read credentials via `Settings.secret()`.
- Internet (`webclient.py`): async httpx, shared pool, concurrency limit, timeouts, byte caps, cache,
  SSRF guard (no private/loopback/link-local hosts, every redirect re-checked), results wrapped as
  `<untrusted_web_content>` so injected instructions stay data.
- Money (`payments.py`, `tools/money.py`): only the owner deposits and allowlists payees. `pay` checks
  payee, per-payment max, payee max, daily org max, wallet balance and budget atomically, needs a memo
  and a task, passes the approver, is idempotent, and reverses on provider failure. Provider is
  `sandbox` until a real one is plugged in.
- Purchases (`purchases.py`, `cards.py`, `tools/purchases.py`): an agent with the money grant asks to buy
  a tool or subscription (vendor, item, price, once/monthly, why). Refused in code above the per-purchase
  limit, the per-business subscription limit, or what the wallet has left after other pending requests.
  The platform's Chief Investment Officer decides (`decide_purchase`), or the owner on the Money page;
  nobody decides their own request. Approval debits the wallet (ledger kind `purchase`, not counted as
  model/ops spend) and issues one virtual card (Privacy.com, or `sandbox`) locked to the vendor with a
  limit of exactly the approved amount, so the issuer enforces the limit too. Monthly purchases are
  debited every 30 days; when the wallet can't cover it the card is paused until the owner adds money.
- Browser (`browser.py`, `tools/browser.py`): headless Chromium (Playwright) on the server, one
  persistent profile per business (logins survive), one tab per agent. Pages come back as text with
  numbered element refs (iframes too, for checkout card fields). Navigations pass the same SSRF guard as
  fetch_url. Secrets are typed through placeholders, `{{card:<purchase>:number}}` and
  `{{login:<name>:password}}`, resolved at the last moment and redacted from everything returned and
  logged. `save_login` generates passwords the model never sees. Mail to `mailbox+<business-slug>@domain`
  is stored as account mail (`read_account_mail`) for verification codes. Steps only a person can do
  (CAPTCHA, a code to the owner's phone, ID checks) go to the owner's queue as `browser_assist`; the
  owner's note (e.g. the code) comes back to the agent, whose tab is still open.
- Owner setup is one item (`owner_setup.py`): any hard stop about a platform connection (Stripe, the
  mailbox) folds into a single queue item for all businesses and never blocks a task; identical asks are
  never filed twice; when the connection works the item closes itself and every waiting agent is told.
  Agents see what's connected (and their business's email address) in every wake-up context.
- Bottleneck guard (`maintenance.bottleneck_checks`): any refusal rule that blocks agents 5+ times in 24h
  becomes an audit finding with examples, so the cause is fixed in code (tool, grant, route), not prompts.
- Messages: agents may name recipients by role or name ('ceo', 'cio'); the gateway resolves it. Business
  CEOs and the platform's executives may message each other directly. Otherwise peers only within an org (plus direct supervisor/report edges), hop limit, TTL.
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
