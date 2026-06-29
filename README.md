# jiraku — agent-powered Jira triage

An automated triage system that polls Jira for new / untriaged tickets,
classifies their intent with an LLM agent, hands them off to specialized worker
agents, and either transitions them to **In Progress** or surfaces them to a
**TUI dashboard** for human review.

This repository contains the **triage agent harness** and the **TUI dashboard**.

![The jiraku TUI dashboard: a Tickets table on the left, a live Agent activity
log and an exceptions Inbox on the right.](examples/screenshot.png)

The dashboard shows tickets being classified, routed to worker agents and either
transitioned to **In Progress** or surfaced to the **Inbox** for human review,
with a live activity log and running metrics across the top.

## Architecture

jiraku uses a **hexagonal (ports & adapters)** architecture so the business
logic is fully decoupled from Jira, the LLM, and the front-end:

```
            ┌──────────────────────── driving adapters ────────────────────────┐
            │   TUI dashboard (Textual)            CLI (jiraku run / tui)       │
            └───────────────┬───────────────────────────────┬──────────────────┘
                            │ subscribe (events)             │ use cases
                    ┌───────▼───────────────────────────────▼───────┐
                    │                 application                    │
                    │   TriageService · AgentRouter · TriagePoller   │   ← the harness
                    └───────┬───────────────────────────────┬───────┘
                            │ ports (Protocols)              │
   ┌────────────────────────▼──────┐               ┌─────────▼───────────────────┐
   │  TicketSource                 │               │  Classifier                 │
   │   • InMemory (seed/offline)   │               │   • Keyword (deterministic) │
   │   • JiraRest (httpx)          │               │   • CopilotCli · GeminiCli  │
   │                               │               │     (LLM)                   │
   ├───────────────────────────────┤               ├─────────────────────────────┤
   │  RepoResolver                 │               │  WorkspaceProvisioner       │
   │   • Registry (YAML)           │               │   • Noop (dry-run)          │
   │   • LearnedRules · Keyword    │               │   • Git (clone)             │
   ├───────────────────────────────┤               ├─────────────────────────────┤
   │  WorkAgentRunner              │               │  InboxRepository · EventBus │
   │   • Noop (default)            │               │                             │
   │   • Copilot · Gemini          │               │                             │
   │     (implement + PR)          │               │                             │
   ├───────────────────────────────┴───────────────┴─────────────────────────────┤
   │  WorkerAgent: Bug / Feature / Documentation                                  │
   └──────────────────────────────────────────────────────────────────────────────┘
                              driven adapters
```

- **`domain/`** — pure entities (`Ticket`, `Classification`, `RepoResolution`,
  `WorkResult`, `InboxEntry`, `TriageMetrics`, …) and domain events. No external
  dependencies.
- **`ports/`** — inbound (`TriageService`) and outbound (`TicketSource`,
  `Classifier`, `RepoResolver`, `LearnedRulesStore`, `WorkspaceProvisioner`,
  `WorkAgentRunner`, `WorkerAgent`, `InboxRepository`, `EventBus`) protocols.
- **`application/`** — the harness: `TriageService` (classify → resolve repo →
  route → validate → transition → provision → run work), `AgentRouter`,
  `TriagePoller`.
- **`adapters/`** — `inmemory` (default, offline), `jira` (real REST API),
  `classifier` (keyword + Copilot CLI + Gemini CLI), `resolver` (registry +
  learned + keyword), `workspace` (noop + git), `work_runner` (noop + Copilot +
  Gemini), `sqlite` (durable inbox + ledger), `agents`.
- **`tui/`** — the Textual dashboard (a driving adapter).
- **`composition.py`** — the composition root that wires everything together.

## Workflow

1. **Poll** — `TriagePoller` fetches `Untriaged` / `To Do` tickets on an interval.
2. **Classify** — the `Classifier` agent labels each ticket (Bug / Feature
   Request / Documentation / Unknown) with a confidence score.
3. **Resolve repo** — the `RepoResolver` maps the ticket to a repository
   (`clone_url` + path) with a confidence score. Low confidence is escalated
   through the inbox so a human can supply the repo (which also *teaches* the
   resolver).
4. **Route & validate** — `AgentRouter` hands the ticket (and its resolved repo)
   to the matching worker agent, which performs initial validation (is the bug
   reproducible? is the feature a duplicate?).
5. **Transition & start work** — actionable tickets are moved to **In Progress**,
   a workspace is provisioned (`git clone`), and a **work agent** runs in it
   (e.g. the Copilot CLI implements the change and opens a pull request);
   low-confidence or ambiguous tickets are surfaced to the dashboard inbox.

## Repository resolution

After classification the harness resolves **which repo** a ticket belongs to,
mirroring how the classifier is structured: a `RepoResolver` port with layered
adapters and a confidence gate.

- **Registry** (`RegistryRepoResolver`) — an authoritative project→repo mapping
  loaded from a YAML catalog (`--repo-registry`, see
  [`examples/repo_registry.yaml`](examples/repo_registry.yaml)). **Seed it from
  Jira dev-status / commit-mining**: the issue→commit→repo links Jira already
  records give you an empirical mapping (and an eval set) on day one.
- **Learned rules** (`LearnedRulesRepoResolver`) — mappings taught by inbox
  corrections; persisted with `--learned-rules <path>`. Each correction improves
  precision over time.
- **Keyword / code-tokens** (`KeywordRepoResolver`) — a deterministic matcher for
  the residual (module names, path-like tokens, repo-name fragments). This is the
  seam where code-search + an LLM would layer in next.

These are combined by a `CompositeRepoResolver` (learned → registry → tokens);
the first *confident* hit wins. If none is confident the ticket is escalated at
the **repository** stage. In the dashboard, press `d` and paste a **clone URL**:
that unblocks the ticket *and* teaches the resolver, so future tickets in the
same project resolve automatically.

When a ticket transitions, a `WorkspaceProvisioner` hands the worker agent a
local checkout. The default is a no-op that only reports the intended path;
`--provision` performs a real `git clone` (never in dry-run). Provisioning
happens **before** the status change, so a **failed `git clone` is surfaced to
the inbox** (stage `provisioning`) with the exact command and error captured —
respond with a corrected repo **clone URL** to teach the resolver and re-run.

## Work agent (implement + open a PR)

Right after provisioning, the harness calls a `WorkAgentRunner` to actually do
the work in the cloned workspace. Two LLM-CLI runners ship: the
`CopilotWorkAgentRunner` (GitHub Copilot CLI, the default) and the
`GeminiWorkAgentRunner` (Gemini CLI). Each invokes its agent in the checkout to
implement the ticket, push a branch, and open a pull request; the resulting PR
URL is recorded on the outcome and shown in the dashboard. Select the provider
with `--work-agent {copilot,gemini}`.

```bash
# Resolve repo, clone it, run the work agent, and open a PR (real writes — use --apply)
uv run jiraku run --once --apply \
  --repo-registry examples/repo_registry.yaml \
  --work

# Same, but drive the Gemini CLI as the work agent
uv run jiraku run --once --apply \
  --repo-registry examples/repo_registry.yaml \
  --work --work-agent gemini
```

`--work` implies `--provision` (the agent needs a checkout) and, like all
writes, is **disabled in dry-run**. The default runner is a no-op, so the work
agent never runs unless you opt in. The Gemini runner auto-approves tool calls
(`--yolo`) and trusts the freshly-cloned workspace (`--skip-trust`) so it runs
unattended. The port is the seam for other runners (a different CLI agent, a
queue worker, etc.).

### When the agent gets stuck (NEEDS_INPUT)

The work prompt tells the agent: *if you're blocked, print `NEEDS_INPUT:
<question>` and stop.* When that happens the runner returns a blocked
`WorkResult` and the harness **escalates the question to the inbox** at the
`work` stage (the ticket is already In Progress). The inbox entry remembers the
**branch and workspace**. Press `d`, type your **answer** in the note field, and
choose *Answer & resume work* — the harness re-invokes the agent on the **same
branch/workspace** with your answer (it does **not** re-triage). If the agent
gets stuck again it raises a fresh question; otherwise it finishes and opens the
PR.

### On-demand follow-up work

Already-worked tickets keep their provisioned workspace, so you can re-engage the
agent at any time — e.g. to action outside feedback on a PR. In the dashboard,
select the ticket and press `w`, then type an instruction; the agent runs in that
ticket's **existing workspace/branch** (no re-triage, no status change) and
opens/updates the PR. From the shell:

```bash
uv run jiraku work PROJ-123 "Address review feedback: rename the flag and add a test" \
  --work --apply --repo-registry examples/repo_registry.yaml
```

A follow-up reuses the existing checkout (no re-clone), so the agent continues on
the same branch. If it gets blocked it raises a `NEEDS_INPUT` question just like
initial work; a clone failure escalates at the `provisioning` stage.

### Model selection

The classifier model and the work model are configured **separately**:

- `--classifier-model` — the model the LLM CLI *classifier* (copilot/gemini) uses.
- `--work-model` — the model the *work agent* uses. If unset, each ticket uses
  the model **recommended by its classification** (`Classification.recommended_model`).

The classifier recommends a model per ticket — a deeper model for complex/risky
work (e.g. a bug with a stack trace or race condition), a cheaper one for
trivial changes (e.g. a docs typo). The keyword classifier uses a tiered
heuristic; the LLM classifiers ask the model and fall back to that heuristic.
The recommendation tiers are **provider-specific** (Copilot names like
`claude-opus-4.5`/`gpt-5-mini`; Gemini names like `gemini-2.5-pro`/
`gemini-2.5-flash`), so the recommended model is always one the chosen work
agent accepts. An explicit `--work-model` always overrides the recommendation.

```bash
# Cheap classifier, per-ticket recommended work model
uv run jiraku run --once --apply --classifier copilot \
  --classifier-model gpt-5-mini --work

# Pin both explicitly
uv run jiraku run --once --apply --classifier copilot \
  --classifier-model gpt-5-mini --work --work-model claude-sonnet-4.5

# Gemini end to end (classifier + work agent)
uv run jiraku run --once --apply --classifier gemini \
  --work --work-agent gemini
```

```bash
# Resolve against your registry, persist what you teach, and clone workspaces
uv run jiraku run --once --apply \
  --repo-registry examples/repo_registry.yaml \
  --learned-rules ~/.config/jiraku/learned-rules.yaml \
  --provision

# Don't escalate on unresolved repos (skip the repo confidence gate)
uv run jiraku run --once --no-require-repo
```

## Install

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Run

Launch the real-time dashboard (default command):

```bash
uv run jiraku            # or: uv run jiraku tui
```

Dashboard keys: `p` poll now · `g` inject a demo ticket · `d` open the
detail/respond view for the selected inbox item · `r` resolve it · `w` prompt the
agent for follow-up work on the selected ticket · `x` forget the selected ticket
(drop it from the ledger/inbox so it can be re-triaged) · `q` quit.

The **Agent activity** panel header shows a live count of **active workers** —
tickets currently In Progress with a worker agent engaged (not yet PR'd or
surfaced to the inbox).

### Inbox detail & responding

Select an inbox exception and press `d` to open an expandable detail view that
shows the full picture the harness captured: the worker **agent**, the
classifier's **rationale**, the specific **validation details** (e.g. "no
reproduction steps"), category and confidence. From there you can **respond**
with a note that either:

- **posts a comment** back to the Jira issue (e.g. asking the reporter for
  reproduction steps), and/or
- **re-runs triage** using your note as an authoritative hint — so telling it
  "this is actually a bug" re-classifies and routes the ticket accordingly.

Re-running resolves the original inbox item (a fresh one is raised only if the
ticket still can't be actioned). In dry-run mode comments are **not** posted and
re-triage performs no writes.

> The interactive TUI needs a real terminal. In CI / headless contexts use
> `jiraku run` (below) or drive the app via Textual's `run_test()` pilot.

Run the harness headlessly:

```bash
uv run jiraku run --once          # one poll cycle, print a summary
uv run jiraku run --cycles 3      # three cycles then exit
uv run jiraku run                 # poll forever (Ctrl-C to stop)
```

### Classifier selection

```bash
# Use the GitHub Copilot CLI as the classification agent
uv run jiraku run --once --classifier copilot

# Use the Gemini CLI as the classification agent
uv run jiraku run --once --classifier gemini

# Fall back to the deterministic keyword classifier if the LLM CLI is unavailable
uv run jiraku run --once --classifier copilot --copilot-fallback
```

The Copilot and Gemini classifiers are interchangeable LLM-CLI adapters over a
shared base (`LlmCliClassifier`): they prompt their CLI for a single JSON object
and parse the category, confidence and recommended model. The Gemini classifier
runs read-only (`--approval-mode plan`) since classification never writes.
`--copilot-fallback` applies to whichever LLM classifier is selected.

By default jiraku runs fully offline against an in-memory Jira seeded with a
representative batch of tickets, so it is runnable with zero configuration.

## Connecting to real Jira

jiraku authenticates to **Jira Cloud** with your email + an API token
([create one here](https://id.atlassian.com/manage-profile/security/api-tokens))
using HTTP Basic auth, and reads issues with the current
`/rest/api/3/search/jql` endpoint (token pagination).

Provide credentials via environment variables or a `.jira.env` file in the
working directory (auto-loaded; **git-ignored** — never commit it):

```bash
# .jira.env
JIRA_BASE=https://your-org.atlassian.net   # JIRA_BASE_URL also accepted
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-api-token
JIRA_JQL=assignee = currentUser() AND status in ("To Do", "Untriaged") ORDER BY created ASC
```

When credentials are present, `--source auto` (the default) selects real Jira;
otherwise it falls back to the in-memory demo. The chosen mode is always printed
at startup — jiraku never silently degrades.

### Dry-run vs. apply (write safety)

Triage **mutates the board** (it transitions actionable tickets to *In
Progress*). To avoid surprises, a real Jira source is **read-only by default**:
every intended transition is logged but not written. Pass `--apply` to actually
perform transitions.

```bash
# Preview triage of your real tickets — no writes (default for real Jira)
uv run jiraku run --once

# Actually transition actionable tickets to In Progress
uv run jiraku run --once --apply

# Live dashboard over real Jira, read-only
uv run jiraku tui --classifier copilot
```

Escalations are surfaced to the dashboard inbox **without** changing the
ticket's Jira status (the harness only ever writes the *In Progress*
transition, and only with `--apply`). The native Jira **issue type** (Bug,
Story, Epic, …) is used as a strong classification signal.

## State persistence

Actioned tickets and the exception inbox are persisted to a **SQLite file** so
the dashboard survives restarts: on launch it restores the previously-actioned
tickets, the open inbox items (which you can still answer), the cumulative
metrics, and each ticket's provisioned workspace. The poller also skips tickets
it has already actioned, so nothing is re-triaged after a restart.

The `tui` command persists by default to `$XDG_STATE_HOME/jiraku/state.db`
(`~/.local/state/jiraku/state.db`). Override or disable it:

```bash
uv run jiraku tui --state-db /path/to/state.db   # custom location
uv run jiraku tui --no-state                      # in-memory only
uv run jiraku run --once --state-db state.db      # opt-in for headless runs
```

(`run` and `work` don't persist unless you pass `--state-db`.)

### Forgetting a ticket

Persistence is durable, so an actioned ticket normally never comes back. To
deliberately drop one — clearing it from the ledger **and** any open inbox items
so it disappears from the dashboard and becomes eligible for re-triage on the
next poll — use the `x` key in the dashboard (a confirm prompt guards the
action) or the CLI:

```bash
uv run jiraku forget PROJ-123                    # default dashboard store
uv run jiraku forget PROJ-123 --state-db state.db
```

Forgetting reverses that ticket's contribution to the metrics and persists the
removal across restarts; the next poll re-triages it if it is still untriaged in
Jira.

## Test

```bash
uv run pytest
```

The suite covers the domain, the harness, every adapter (including the Jira
REST adapter via `httpx.MockTransport`, token pagination, the read-only
dry-run wrapper, the Copilot and Gemini classifiers and work runners via
injected runners, and the SQLite state store round-tripping across restarts),
and the TUI via Textual's headless pilot.
