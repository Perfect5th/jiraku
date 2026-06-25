# jiraya — agent-powered Jira triage

An automated triage system that polls Jira for new / untriaged tickets,
classifies their intent with an LLM agent, hands them off to specialized worker
agents, and either transitions them to **In Progress** or surfaces them to a
**TUI dashboard** for human review.

This repository contains the **triage agent harness** and the **TUI dashboard**.

## Architecture

jiraya uses a **hexagonal (ports & adapters)** architecture so the business
logic is fully decoupled from Jira, the LLM, and the front-end:

```
            ┌──────────────────────── driving adapters ────────────────────────┐
            │   TUI dashboard (Textual)            CLI (jiraya run / tui)       │
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
   │   • JiraRest (httpx)          │               │   • CopilotCli (LLM)        │
   ├───────────────────────────────┤               ├─────────────────────────────┤
   │  WorkerAgent: Bug / Feature / Documentation    │  InboxRepository · EventBus │
   └────────────────────────────────────────────────┴─────────────────────────────┘
                              driven adapters
```

- **`domain/`** — pure entities (`Ticket`, `Classification`, `InboxEntry`,
  `TriageMetrics`, …) and domain events. No external dependencies.
- **`ports/`** — inbound (`TriageService`) and outbound (`TicketSource`,
  `Classifier`, `WorkerAgent`, `InboxRepository`, `EventBus`) protocols.
- **`application/`** — the harness: `TriageService` (classify → route → validate
  → transition / escalate), `AgentRouter`, and the scheduled `TriagePoller`.
- **`adapters/`** — `inmemory` (default, offline), `jira` (real REST API),
  `classifier` (keyword + Copilot CLI), `agents` (worker agents).
- **`tui/`** — the Textual dashboard (a driving adapter).
- **`composition.py`** — the composition root that wires everything together.

## Workflow

1. **Poll** — `TriagePoller` fetches `Untriaged` / `To Do` tickets on an interval.
2. **Classify** — the `Classifier` agent labels each ticket (Bug / Feature
   Request / Documentation / Unknown) with a confidence score.
3. **Route & validate** — `AgentRouter` hands the ticket to the matching worker
   agent, which performs initial validation (is the bug reproducible? is the
   feature a duplicate?).
4. **Transition or escalate** — actionable tickets are moved to **In Progress**;
   low-confidence or ambiguous tickets are surfaced to the dashboard inbox.

## Install

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Run

Launch the real-time dashboard (default command):

```bash
uv run jiraya            # or: uv run jiraya tui
```

Dashboard keys: `p` poll now · `g` inject a demo ticket · `d` open the
detail/respond view for the selected inbox item · `r` resolve it · `q` quit.

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
> `jiraya run` (below) or drive the app via Textual's `run_test()` pilot.

Run the harness headlessly:

```bash
uv run jiraya run --once          # one poll cycle, print a summary
uv run jiraya run --cycles 3      # three cycles then exit
uv run jiraya run                 # poll forever (Ctrl-C to stop)
```

### Classifier selection

```bash
# Use the GitHub Copilot CLI as the classification agent
uv run jiraya run --once --classifier copilot

# Fall back to the deterministic keyword classifier if Copilot is unavailable
uv run jiraya run --once --classifier copilot --copilot-fallback
```

By default jiraya runs fully offline against an in-memory Jira seeded with a
representative batch of tickets, so it is runnable with zero configuration.

## Connecting to real Jira

jiraya authenticates to **Jira Cloud** with your email + an API token
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
at startup — jiraya never silently degrades.

### Dry-run vs. apply (write safety)

Triage **mutates the board** (it transitions actionable tickets to *In
Progress*). To avoid surprises, a real Jira source is **read-only by default**:
every intended transition is logged but not written. Pass `--apply` to actually
perform transitions.

```bash
# Preview triage of your real tickets — no writes (default for real Jira)
uv run jiraya run --once

# Actually transition actionable tickets to In Progress
uv run jiraya run --once --apply

# Live dashboard over real Jira, read-only
uv run jiraya tui --classifier copilot
```

Escalations are surfaced to the dashboard inbox **without** changing the
ticket's Jira status (the harness only ever writes the *In Progress*
transition, and only with `--apply`). The native Jira **issue type** (Bug,
Story, Epic, …) is used as a strong classification signal.

## Test

```bash
uv run pytest
```

The suite covers the domain, the harness, every adapter (including the Jira
REST adapter via `httpx.MockTransport`, token pagination, the read-only
dry-run wrapper, and the Copilot classifier via an injected runner), and the
TUI via Textual's headless pilot.
