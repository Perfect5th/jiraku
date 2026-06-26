from __future__ import annotations

from pathlib import Path

from jiraya.adapters.agents import default_agents
from jiraya.adapters.classifier import KeywordClassifier
from jiraya.adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from jiraya.adapters.resolver import RegistryRepoResolver, default_catalog
from jiraya.adapters.work_runner import CopilotWorkAgentRunner, NoopWorkAgentRunner
from jiraya.application import AgentRouter, TriageService
from jiraya.domain import Classification, EscalationStage, Priority, Ticket, TicketCategory, TriageAction
from jiraya.ports import WorkspaceProvisionError


class _Provisioner:
    def __init__(self, ws: Path):
        self._ws = ws

    def provision(self, repo, ticket_key):
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)
        return str(self._ws)


def _service(work_runner, provisioner):
    return TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        events=InMemoryEventBus(),
        resolver=RegistryRepoResolver(default_catalog()),
        provisioner=provisioner,
        work_runner=work_runner,
    )


# -- runner ------------------------------------------------------------------

def test_runner_followup_prompt_includes_instruction(tmp_path):
    ws = tmp_path / "PROJ-1"
    (ws / ".git").mkdir(parents=True)
    seen = {}

    def runner(prompt, cwd, model):
        seen["prompt"] = prompt
        return "PR_URL: https://x/pull/3"

    cls = Classification(TicketCategory.BUG, "PROJ", 0.9)
    out = CopilotWorkAgentRunner(runner=runner).run(
        Ticket(key="PROJ-1", project="PROJ", summary="s", description="d", reporter="r",
               priority=Priority.HIGH),
        cls, None, str(ws), instruction="Rename the flag and add a test",
    )
    assert out.opened_pr
    assert "Rename the flag and add a test" in seen["prompt"]
    assert "jiraya/proj-1" in seen["prompt"]
    assert "further on-demand work" in seen["prompt"]


# -- service.run_followup ----------------------------------------------------

def test_run_followup_engages_agent_in_workspace(tmp_path):
    ws = tmp_path / "PROJ-101"
    seen = {}

    def runner(prompt, cwd, model):
        seen["cwd"] = cwd
        seen["prompt"] = prompt
        return "PR_URL: https://github.com/acme/proj-service/pull/21"

    svc = _service(CopilotWorkAgentRunner(runner=runner), _Provisioner(ws))
    outcome = svc.run_followup("PROJ-101", "Action the review feedback")

    assert outcome.action is TriageAction.TRANSITIONED
    assert outcome.work.opened_pr
    assert outcome.work.pr_url.endswith("/pull/21")
    assert seen["cwd"] == str(ws)
    assert "Action the review feedback" in seen["prompt"]


def test_run_followup_unknown_ticket_returns_none(tmp_path):
    svc = _service(NoopWorkAgentRunner(), _Provisioner(tmp_path / "x"))
    assert svc.run_followup("NOPE-1", "do x") is None


def test_run_followup_empty_instruction_returns_none(tmp_path):
    svc = _service(NoopWorkAgentRunner(), _Provisioner(tmp_path / "PROJ-101"))
    assert svc.run_followup("PROJ-101", "   ") is None


def test_run_followup_blocked_escalates_at_work_stage(tmp_path):
    runner = CopilotWorkAgentRunner(
        runner=lambda p, c, m: "NEEDS_INPUT: Which feature flag should I gate this on?")
    svc = _service(runner, _Provisioner(tmp_path / "PROJ-101"))

    outcome = svc.run_followup("PROJ-101", "Make it configurable")

    assert outcome.action is TriageAction.ESCALATED
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")
    assert entry.stage is EscalationStage.WORK
    assert "feature flag" in entry.reason


def test_run_followup_clone_failure_escalates_provisioning(tmp_path):
    class _BadProvisioner:
        def provision(self, repo, ticket_key):
            raise WorkspaceProvisionError(
                "git clone failed.", command=("git", "clone", "x"),
                returncode=128, stderr="fatal: no")

    svc = _service(NoopWorkAgentRunner(), _BadProvisioner())
    outcome = svc.run_followup("PROJ-101", "do x")
    assert outcome.action is TriageAction.ESCALATED
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")
    assert entry.stage is EscalationStage.PROVISIONING


# -- CLI ---------------------------------------------------------------------

def test_cli_work_command_runs(monkeypatch):
    from jiraya import cli
    for key in ("JIRA_BASE", "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    rc = cli.main(["work", "PROJ-101", "add a unit test", "--source", "memory"])
    assert rc == 0
