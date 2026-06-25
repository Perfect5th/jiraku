from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jiraya.adapters.agents import default_agents
from jiraya.adapters.classifier import KeywordClassifier
from jiraya.adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from jiraya.adapters.resolver import (
    CompositeRepoResolver,
    InMemoryLearnedRulesStore,
    KeywordRepoResolver,
    LearnedRulesRepoResolver,
    RegistryRepoResolver,
    RepoCatalogEntry,
)
from jiraya.adapters.work_runner import NoopWorkAgentRunner
from jiraya.adapters.workspace import GitWorkspaceProvisioner
from jiraya.application import AgentRouter, TriageService
from jiraya.domain import EscalationStage, RepoRef, TriageAction
from jiraya.ports import WorkspaceProvisionError


# -- provisioner error behaviour --------------------------------------------

def test_git_provisioner_raises_rich_error_on_clone_failure():
    def boom(cmd):
        raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not found\n")

    prov = GitWorkspaceProvisioner(root="/tmp/jiraya-test-ws", runner=boom)
    with pytest.raises(WorkspaceProvisionError) as ei:
        prov.provision(RepoRef("acme/x", "https://example.invalid/x.git"), "PROJ-1")

    err = ei.value
    assert err.returncode == 128
    assert err.command[:2] == ("git", "clone")
    details = err.details()
    assert any(d.startswith("$ git clone") for d in details)
    assert any("fatal: not found" in d for d in details)


def test_git_provisioner_missing_url_raises():
    prov = GitWorkspaceProvisioner(runner=lambda cmd: None)
    with pytest.raises(WorkspaceProvisionError):
        prov.provision(RepoRef("acme/x", ""), "PROJ-1")


def test_git_provisioner_cleans_up_partial_checkout(tmp_path):
    root = tmp_path / "ws"

    def fail_after_creating_dir(cmd):
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)  # partial clone
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    prov = GitWorkspaceProvisioner(root=str(root), runner=fail_after_creating_dir)
    with pytest.raises(WorkspaceProvisionError):
        prov.provision(RepoRef("acme/x", "u"), "PROJ-1")
    # The partial checkout was removed so a retry can re-clone cleanly.
    assert not (root / "PROJ-1").exists()


# -- harness escalation + recovery ------------------------------------------

def _service(provisioner, store):
    cat = [RepoCatalogEntry(key="acme/proj",
                            clone_url="https://example.invalid/bad.git",
                            projects=("PROJ",))]
    resolver = CompositeRepoResolver([
        LearnedRulesRepoResolver(store),
        RegistryRepoResolver(cat),
        KeywordRepoResolver(cat),
    ])
    return TriageService(
        ticket_source=InMemoryTicketSource(),
        classifier=KeywordClassifier(),
        router=AgentRouter(default_agents()),
        inbox=InMemoryInboxRepository(),
        events=InMemoryEventBus(),
        resolver=resolver,
        learned_store=store,
        provisioner=provisioner,
        work_runner=NoopWorkAgentRunner(),
    )


class _ScriptedProvisioner:
    """Fails for 'bad' clone URLs; 'succeeds' (creates a fake checkout) for others."""

    def __init__(self, root: Path):
        self._root = root

    def provision(self, repo, ticket_key):
        if "bad" in repo.clone_url:
            raise WorkspaceProvisionError(
                f"git clone of {repo.clone_url} failed.",
                command=("git", "clone", "--depth", "1", repo.clone_url),
                returncode=128,
                stderr="fatal: repository not found",
            )
        dest = self._root / ticket_key
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        return str(dest)


def test_clone_failure_escalates_at_provisioning_stage(tmp_path):
    store = InMemoryLearnedRulesStore()
    svc = _service(_ScriptedProvisioner(tmp_path), store)
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")

    outcome = svc.triage_ticket(ticket)

    assert outcome.action is TriageAction.ESCALATED
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")
    assert entry.stage is EscalationStage.PROVISIONING
    assert entry.needs_repo
    assert entry.repo is not None  # the bad repo that was attempted
    # The failing command + error are captured for the human to examine.
    assert any(d.startswith("$ git clone") for d in entry.details)
    assert any("repository not found" in d for d in entry.details)
    # The ticket was not half-started — it never transitioned.
    assert svc._source.get("PROJ-101").status.value != "In Progress"


def test_respond_with_corrected_url_recovers(tmp_path):
    store = InMemoryLearnedRulesStore()
    svc = _service(_ScriptedProvisioner(tmp_path), store)
    ticket = next(t for t in svc._source.fetch_untriaged() if t.key == "PROJ-101")
    svc.triage_ticket(ticket)
    entry = next(e for e in svc._inbox.open_entries() if e.ticket_key == "PROJ-101")

    good = RepoRef("acme/proj", "https://example.test/good.git")
    resp = svc.respond_to_inbox(entry.id, "use the right repo", repo=good)

    assert resp.taught and resp.retriaged
    assert resp.outcome.action is TriageAction.TRANSITIONED
    assert (Path(resp.outcome.workspace) / ".git").is_dir()
    assert svc._source.get("PROJ-101").status.value == "In Progress"
    # No provisioning exception remains open.
    assert not [e for e in svc._inbox.open_entries() if e.needs_repo]
