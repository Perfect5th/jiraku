from __future__ import annotations

from pathlib import Path

from jiraku.adapters.agents import default_agents
from jiraku.adapters.classifier import KeywordClassifier
from jiraku.adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from jiraku.adapters.resolver import RegistryRepoResolver, default_catalog
from jiraku.adapters.work_runner import CopilotWorkAgentRunner, _extract_question
from jiraku.application import AgentRouter, TriageService
from jiraku.domain import (
    Classification,
    EscalationStage,
    Priority,
    Ticket,
    TicketCategory,
    TicketEscalated,
    TicketWorkStarted,
    TriageAction,
    WorkResult,
)


def _cls():
    return Classification(TicketCategory.BUG, "PROJ", 0.9, recommended_model="m")


def _ticket():
    return Ticket(key="PROJ-1", project="PROJ", summary="x", description="y",
                  reporter="r", priority=Priority.HIGH, issue_type="Bug")


# -- domain ------------------------------------------------------------------

def test_work_result_needs_input():
    r = WorkResult.blocked("Which DB?", branch="jiraku/proj-1", model="m")
    assert r.needs_input
    assert not r.started
    assert r.question == "Which DB?"
    assert not WorkResult(started=True, pr_url="x").needs_input


def test_extract_question():
    assert _extract_question("...\nNEEDS_INPUT: What is the API base URL?\n") == \
        "What is the API base URL?"
    assert _extract_question("no question here") == ""


# -- runner ------------------------------------------------------------------

def test_runner_parses_needs_input(tmp_path):
    ws = tmp_path / "PROJ-1"
    (ws / ".git").mkdir(parents=True)
    out = CopilotWorkAgentRunner(
        runner=lambda p, c, m: "thinking\nNEEDS_INPUT: Which queue backend?\n"
    ).run(_ticket(), _cls(), None, str(ws))
    assert out.needs_input
    assert out.question == "Which queue backend?"
    assert out.branch == "jiraku/proj-1"


def test_runner_resume_prompt_includes_answer(tmp_path):
    ws = tmp_path / "PROJ-1"
    (ws / ".git").mkdir(parents=True)
    seen = {}

    def runner(prompt, cwd, model):
        seen["prompt"] = prompt
        return "PR_URL: https://x/pull/9"

    out = CopilotWorkAgentRunner(runner=runner).run(
        _ticket(), _cls(), None, str(ws), answer="Use Redis"
    )
    assert out.opened_pr
    assert "Use Redis" in seen["prompt"]
    assert "jiraku/proj-1" in seen["prompt"]
    assert "NEEDS_INPUT" in seen["prompt"]  # sentinel still present on resume


# -- harness -----------------------------------------------------------------

class _Provisioner:
    def __init__(self, ws: Path):
        self._ws = ws

    def provision(self, repo, ticket_key):
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)
        return str(self._ws)


def _service(work_runner, ws: Path):
    bus = InMemoryEventBus()
    events: list = []
    bus.subscribe(events.append)
    svc = TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        events=bus,
        resolver=RegistryRepoResolver(default_catalog()),
        provisioner=_Provisioner(ws),
        work_runner=work_runner,
    )
    return svc, events


def test_blocked_work_escalates_at_work_stage(tmp_path):
    runner = CopilotWorkAgentRunner(
        runner=lambda p, c, m: "NEEDS_INPUT: Which database should I target?"
    )
    svc, events = _service(runner, tmp_path / "PROJ-101")
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")

    outcome = svc.triage_ticket(ticket)

    assert outcome.action is TriageAction.ESCALATED
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")
    assert entry.stage is EscalationStage.WORK
    assert "Which database" in entry.reason
    assert entry.branch == "jiraku/proj-101"
    assert entry.workspace.endswith("PROJ-101")
    # The work attempt and the escalation were both published.
    assert any(isinstance(e, TicketWorkStarted) and e.result.needs_input for e in events)
    assert any(isinstance(e, TicketEscalated) for e in events)
    # The ticket did move to In Progress (the agent is working, just blocked).
    assert svc._source.get("PROJ-101").status.value == "In Progress"


def test_answer_resumes_work_instead_of_retriaging(tmp_path):
    calls = {"n": 0}

    def runner(prompt, cwd, model):
        calls["n"] += 1
        if "human has now answered" in prompt:
            assert "Postgres" in prompt
            return "PR_URL: https://github.com/acme/proj-service/pull/8"
        return "NEEDS_INPUT: Which database should I target?"

    svc, _ = _service(CopilotWorkAgentRunner(runner=runner), tmp_path / "PROJ-101")
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    svc.triage_ticket(ticket)
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")

    resp = svc.respond_to_inbox(entry.id, "Use Postgres", rerun=True)

    assert resp.resumed is True
    assert resp.retriaged is False  # resumed work, did NOT re-triage
    assert resp.outcome.action is TriageAction.TRANSITIONED
    assert resp.outcome.work.opened_pr
    assert resp.outcome.work.pr_url.endswith("/pull/8")
    assert svc._inbox.get(entry.id).status.value == "Resolved"
    assert calls["n"] == 2  # initial + resume


def test_resume_can_block_again(tmp_path):
    def runner(prompt, cwd, model):
        if "human has now answered" in prompt:
            return "NEEDS_INPUT: And which migration tool?"
        return "NEEDS_INPUT: Which database should I target?"

    svc, _ = _service(CopilotWorkAgentRunner(runner=runner), tmp_path / "PROJ-101")
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    svc.triage_ticket(ticket)
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")

    resp = svc.respond_to_inbox(entry.id, "Use Postgres", rerun=True)

    assert resp.resumed is True
    assert resp.outcome.action is TriageAction.ESCALATED
    # A fresh WORK exception with the new question is open.
    new = [e for e in svc._inbox.open_entries()
           if e.ticket_key == "PROJ-101" and e.stage is EscalationStage.WORK]
    assert len(new) == 1
    assert "migration tool" in new[0].reason
    assert svc._inbox.get(entry.id).status.value == "Resolved"
