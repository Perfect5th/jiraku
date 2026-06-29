from __future__ import annotations

import asyncio
from pathlib import Path

from jiraku.adapters.work_runner import (
    CopilotWorkAgentRunner,
    NoopWorkAgentRunner,
    _extract_pr_url,
)
from jiraku.application import AgentRouter, TriageService
from jiraku.adapters.agents import default_agents
from jiraku.adapters.classifier import KeywordClassifier
from jiraku.adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from jiraku.adapters.resolver import RegistryRepoResolver, default_catalog
from jiraku.adapters.workspace import NoopWorkspaceProvisioner
from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import (
    Classification,
    Priority,
    RepoRef,
    RepoResolution,
    Ticket,
    TicketCategory,
    TicketWorkStarted,
    TriageAction,
    WorkResult,
)


def _ticket():
    return Ticket(key="PROJ-1", project="PROJ", summary="Login crash",
                  description="Steps: 1. open 2. crash. Expected ok, actual crash.",
                  reporter="r", priority=Priority.HIGH, issue_type="Bug")


def _cls():
    return Classification(TicketCategory.BUG, "PROJ", 0.9)


def _res():
    return RepoResolution(RepoRef("acme/proj", "https://github.com/acme/proj.git"), 0.95)


# -- domain ------------------------------------------------------------------

def test_work_result_opened_pr():
    assert WorkResult(started=True, pr_url="https://x/pull/1").opened_pr
    assert not WorkResult(started=True).opened_pr
    assert not WorkResult.skipped("noop").started


def test_extract_pr_url_variants():
    assert _extract_pr_url("PR_URL: https://github.com/a/b/pull/42") == \
        "https://github.com/a/b/pull/42"
    assert _extract_pr_url("see https://gitlab.com/a/b/merge_requests/7 ok") == \
        "https://gitlab.com/a/b/merge_requests/7"
    assert _extract_pr_url("nothing here") == ""


# -- noop runner -------------------------------------------------------------

def test_noop_runner_records_and_skips():
    r = NoopWorkAgentRunner()
    out = r.run(_ticket(), _cls(), _res(), "/tmp/ws/PROJ-1")
    assert out.started is False
    assert r.runs == [("PROJ-1", "/tmp/ws/PROJ-1")]


# -- copilot runner ----------------------------------------------------------

def test_copilot_runner_skips_without_workspace():
    r = CopilotWorkAgentRunner(runner=lambda p, c, m: "PR_URL: https://x/pull/1")
    out = r.run(_ticket(), _cls(), _res(), "")
    assert out.started is False
    assert "workspace" in out.summary.lower()


def test_copilot_runner_runs_in_workspace_and_parses_pr(tmp_path):
    seen = {}

    def fake(prompt: str, cwd: str, model: str | None) -> str:
        seen["cwd"] = cwd
        seen["prompt"] = prompt
        seen["model"] = model
        return "implemented\nPR_URL: https://github.com/acme/proj/pull/7\n"

    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    out = CopilotWorkAgentRunner(runner=fake).run(_ticket(), _cls(), _res(), str(ws))

    assert out.started is True
    assert out.opened_pr
    assert out.pr_url == "https://github.com/acme/proj/pull/7"
    assert out.branch == "jiraku/proj-1"
    assert seen["cwd"] == str(ws)
    assert "PROJ-1" in seen["prompt"]
    # No explicit model and no recommendation -> Copilot "auto".
    assert seen["model"] == "auto"
    assert out.model == "auto"


def test_copilot_runner_handles_failure_gracefully(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()

    def boom(prompt: str, cwd: str, model: str | None) -> str:
        raise RuntimeError("copilot exploded")

    out = CopilotWorkAgentRunner(runner=boom).run(_ticket(), _cls(), _res(), str(ws))
    assert out.started is False
    assert "exploded" in out.summary


def test_work_runner_uses_recommended_model_when_no_explicit(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    seen = {}

    def fake(prompt, cwd, model):
        seen["model"] = model
        return "PR_URL: https://x/pull/1"

    cls = Classification(TicketCategory.BUG, "PROJ", 0.9, recommended_model="claude-opus-4.5")
    out = CopilotWorkAgentRunner(runner=fake).run(_ticket(), cls, _res(), str(ws))
    assert seen["model"] == "claude-opus-4.5"
    assert out.model == "claude-opus-4.5"


def test_work_runner_explicit_model_overrides_recommendation(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    seen = {}

    def fake(prompt, cwd, model):
        seen["model"] = model
        return "PR_URL: https://x/pull/1"

    cls = Classification(TicketCategory.BUG, "PROJ", 0.9, recommended_model="claude-opus-4.5")
    out = CopilotWorkAgentRunner(runner=fake, model="gpt-5-mini").run(
        _ticket(), cls, _res(), str(ws))
    assert seen["model"] == "gpt-5-mini"  # explicit wins over recommendation
    assert out.model == "gpt-5-mini"


# -- harness wiring ----------------------------------------------------------

def _service(work_runner, events=None):
    return TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        events=events,
        resolver=RegistryRepoResolver(default_catalog()),
        provisioner=NoopWorkspaceProvisioner(),
        work_runner=work_runner,
    )


def test_harness_runs_work_agent_after_provisioning():
    runner = NoopWorkAgentRunner()
    svc = _service(runner)
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    outcome = svc.triage_ticket(ticket)
    assert outcome.action is TriageAction.TRANSITIONED
    assert outcome.work is not None
    # The runner was handed the provisioned workspace path.
    assert runner.runs == [("PROJ-101", outcome.workspace)]


def test_harness_records_pr_and_publishes_event(tmp_path):
    ws = tmp_path / "PROJ-101"
    ws.mkdir()
    runner = CopilotWorkAgentRunner(
        runner=lambda p, c, m: "PR_URL: https://github.com/acme/proj-service/pull/9"
    )

    class FixedProvisioner:
        def provision(self, repo, ticket_key):
            return str(ws)

    bus = InMemoryEventBus()
    events = []
    bus.subscribe(events.append)
    svc = TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        events=bus,
        resolver=RegistryRepoResolver(default_catalog()),
        provisioner=FixedProvisioner(),
        work_runner=runner,
    )
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    outcome = svc.triage_ticket(ticket)

    assert outcome.work.opened_pr
    assert outcome.work.pr_url.endswith("/pull/9")
    started = [e for e in events if isinstance(e, TicketWorkStarted)]
    assert started and started[0].result.pr_url.endswith("/pull/9")


def test_no_work_runner_leaves_outcome_work_none():
    svc = TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        resolver=RegistryRepoResolver(default_catalog()),
        provisioner=NoopWorkspaceProvisioner(),
        work_runner=None,
    )
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    assert svc.triage_ticket(ticket).work is None


# -- composition -------------------------------------------------------------

def test_composition_default_is_noop_runner():
    system = build_system(JirakuConfig(source="memory"))
    assert isinstance(system.work_runner, NoopWorkAgentRunner)


def test_composition_work_flag_uses_copilot_runner():
    system = build_system(JirakuConfig(source="memory", work=True))
    assert isinstance(system.work_runner, CopilotWorkAgentRunner)


def test_work_implies_real_provisioning():
    from jiraku.adapters.workspace import GitWorkspaceProvisioner
    system = build_system(JirakuConfig(source="memory", work=True))
    assert isinstance(system.provisioner, GitWorkspaceProvisioner)


def test_composition_work_model_is_passed_to_runner():
    system = build_system(JirakuConfig(source="memory", work=True, work_model="gpt-5-mini"))
    assert system.work_runner._model == "gpt-5-mini"


def test_composition_no_work_model_lets_recommendation_apply():
    system = build_system(JirakuConfig(source="memory", work=True))
    assert system.work_runner._model is None
