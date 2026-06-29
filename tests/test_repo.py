from __future__ import annotations

import asyncio

import pytest

from jiraku.adapters.resolver import (
    CompositeRepoResolver,
    FileLearnedRulesStore,
    InMemoryLearnedRulesStore,
    KeywordRepoResolver,
    LearnedRulesRepoResolver,
    RegistryRepoResolver,
    RepoCatalogEntry,
    default_catalog,
    load_catalog,
)
from jiraku.adapters.workspace import GitWorkspaceProvisioner, NoopWorkspaceProvisioner
from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import (
    Classification,
    EscalationStage,
    Priority,
    RepoRef,
    RepoResolution,
    Ticket,
    TicketCategory,
    TriageAction,
)


def _ticket(project="PROJ", summary="Login crash", description="It crashes on /login.",
            issue_type="Bug"):
    return Ticket(key=f"{project}-1", project=project, summary=summary,
                  description=description, reporter="r", priority=Priority.HIGH,
                  issue_type=issue_type)


def _cls(project="PROJ"):
    return Classification(TicketCategory.BUG, project, 0.9)


# -- domain ------------------------------------------------------------------

def test_repo_resolution_confidence_gate():
    repo = RepoRef("acme/x", "https://github.com/acme/x.git")
    assert RepoResolution(repo, 0.6).is_confident
    assert not RepoResolution(repo, 0.59).is_confident
    assert not RepoResolution(None, 0.99).is_confident
    assert not RepoResolution.unresolved("nope").is_confident


# -- registry resolver -------------------------------------------------------

def test_registry_resolver_maps_project():
    r = RegistryRepoResolver(default_catalog())
    res = r.resolve(_ticket("WEB"), _cls("WEB"))
    assert res.is_confident
    assert res.repo.key == "acme/web"
    assert res.source == "registry"


def test_registry_resolver_unknown_project_unresolved():
    r = RegistryRepoResolver(default_catalog())
    res = r.resolve(_ticket("API"), _cls("API"))
    assert not res.is_confident
    assert res.repo is None


# -- keyword/code-token resolver --------------------------------------------

def test_keyword_resolver_matches_tokens():
    cat = [RepoCatalogEntry(key="acme/web", clone_url="u", keywords=("dark mode", "report"))]
    r = KeywordRepoResolver(cat)
    res = r.resolve(_ticket("ZZZ", "Add dark mode", "please add dark mode toggle"), _cls("ZZZ"))
    assert res.repo.key == "acme/web"
    assert res.source == "keyword"


def test_keyword_resolver_matches_repo_name_fragment():
    cat = [RepoCatalogEntry(key="acme/payments", clone_url="u")]
    r = KeywordRepoResolver(cat)
    res = r.resolve(_ticket("ZZZ", "payments fail", "the payments service errors"), _cls("ZZZ"))
    assert res.repo.key == "acme/payments"


def test_keyword_resolver_no_match():
    r = KeywordRepoResolver([RepoCatalogEntry(key="acme/web", clone_url="u", keywords=("theme",))])
    assert not r.resolve(_ticket("ZZZ", "totally unrelated", "nothing here"), _cls("ZZZ")).is_confident


# -- learned rules -----------------------------------------------------------

def test_learned_store_learns_project_rule():
    store = InMemoryLearnedRulesStore()
    repo = RepoRef("acme/support", "https://github.com/acme/support.git")
    store.learn(project="SUP", repo=repo)
    match = store.lookup(project="SUP", text="anything")
    assert match is not None
    found, conf, _ = match
    assert found.key == "acme/support"
    assert conf >= 0.6


def test_learned_store_token_rule():
    store = InMemoryLearnedRulesStore()
    store.learn(project="", repo=RepoRef("acme/cli", "u"), tokens=("reprepro",))
    match = store.lookup(project="OTHER", text="the reprepro upgrade broke")
    assert match is not None and match[0].key == "acme/cli"


def test_learned_store_latest_rule_wins():
    store = InMemoryLearnedRulesStore()
    store.learn(project="P", repo=RepoRef("acme/old", "u1"))
    store.learn(project="P", repo=RepoRef("acme/new", "u2"))
    assert store.lookup(project="P", text="x")[0].key == "acme/new"
    assert len(store.rules()) == 1


def test_file_learned_store_persists(tmp_path):
    path = tmp_path / "rules.yaml"
    store = FileLearnedRulesStore(path)
    store.learn(project="SUP", repo=RepoRef("acme/support", "https://x/support.git"))
    assert path.is_file()
    # A fresh store reloads the persisted rule.
    reloaded = FileLearnedRulesStore(path)
    assert reloaded.lookup(project="SUP", text="x")[0].key == "acme/support"


# -- composite ---------------------------------------------------------------

def test_composite_prefers_learned_then_registry():
    store = InMemoryLearnedRulesStore()
    cat = default_catalog()
    comp = CompositeRepoResolver([
        LearnedRulesRepoResolver(store),
        RegistryRepoResolver(cat),
        KeywordRepoResolver(cat),
    ])
    # registry handles WEB
    assert comp.resolve(_ticket("WEB"), _cls("WEB")).source == "registry"
    # teach a learned override for WEB and confirm it wins
    store.learn(project="WEB", repo=RepoRef("acme/web-next", "u"))
    res = comp.resolve(_ticket("WEB"), _cls("WEB"))
    assert res.source == "learned" and res.repo.key == "acme/web-next"


def test_composite_returns_best_partial_when_none_confident():
    cat = [RepoCatalogEntry(key="acme/web", clone_url="u", keywords=("report",))]
    comp = CompositeRepoResolver([KeywordRepoResolver(cat)])
    res = comp.resolve(_ticket("ZZZ", "no signal", "nothing"), _cls("ZZZ"))
    assert not res.is_confident


# -- catalog loading ---------------------------------------------------------

def test_load_catalog_from_yaml(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        "repos:\n"
        "  - key: acme/web\n"
        "    clone_url: https://github.com/acme/web.git\n"
        "    projects: [WEB]\n"
        "    keywords: [dark mode]\n"
    )
    cat = load_catalog(path)
    assert len(cat) == 1 and cat[0].key == "acme/web" and cat[0].projects == ("WEB",)


# -- provisioners ------------------------------------------------------------

def test_noop_provisioner_reports_path_without_cloning():
    p = NoopWorkspaceProvisioner(root="/tmp/ws")
    path = p.provision(RepoRef("acme/x", "u", path="sub"), "PROJ-1")
    assert path == "/tmp/ws/PROJ-1/sub"
    assert p.provisioned == [("PROJ-1", "/tmp/ws/PROJ-1/sub")]


def test_git_provisioner_runs_clone(tmp_path):
    calls = []
    p = GitWorkspaceProvisioner(root=str(tmp_path / "ws"), runner=calls.append)
    path = p.provision(RepoRef("acme/x", "https://github.com/acme/x.git"), "PROJ-2")
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["git", "clone"]
    assert "https://github.com/acme/x.git" in cmd
    assert path.endswith("PROJ-2")


# -- harness integration -----------------------------------------------------

def test_harness_escalates_at_repository_stage():
    system = build_system(JirakuConfig(source="memory"))
    # API is classifiable (Bug) but absent from the default registry.
    system.source.add(Ticket(
        key="API-9", project="API", summary="Timeout on the orders endpoint",
        description="Steps: POST /v2/orders; expected 200; actual 504. Stack trace attached.",
        reporter="r", priority=Priority.HIGH, issue_type="Bug",
    ))
    asyncio.run(system.poller.run_once())
    entry = next(e for e in system.inbox.open_entries() if e.ticket_key == "API-9")
    assert entry.stage is EscalationStage.REPOSITORY
    assert entry.needs_repo


def test_respond_with_repo_teaches_and_unblocks():
    system = build_system(JirakuConfig(source="memory"))
    system.source.add(Ticket(
        key="API-10", project="API", summary="Timeout on the orders endpoint",
        description="Steps: POST /v2/orders; expected 200; actual 504. Stack trace attached.",
        reporter="r", priority=Priority.HIGH, issue_type="Bug",
    ))
    asyncio.run(system.poller.run_once())
    entry = next(e for e in system.inbox.open_entries() if e.ticket_key == "API-10")
    assert entry.needs_repo

    repo = RepoRef("acme/api", "https://github.com/acme/api.git")
    resp = system.service.respond_to_inbox(entry.id, "this lives in the api repo", repo=repo)

    assert resp.taught is True
    assert resp.retriaged is True
    assert resp.outcome is not None
    # The taught repo unblocked the ticket: resolved + transitioned + provisioned.
    assert resp.outcome.action is TriageAction.TRANSITIONED
    assert resp.outcome.resolution.repo.key == "acme/api"
    assert resp.outcome.workspace  # Noop provisioner reported a path

    # The learned rule now resolves a *new* ticket in the same project.
    res2 = system.resolver.resolve(_ticket("API"), _cls("API"))
    assert res2.is_confident and res2.repo.key == "acme/api"


def test_require_repo_false_skips_gate():
    system = build_system(JirakuConfig(source="memory", require_repo=False))
    system.source.add(Ticket(
        key="API-11", project="API", summary="Timeout on the orders endpoint",
        description="Steps: POST /v2/orders; expected 200; actual 504. Stack trace attached.",
        reporter="r", priority=Priority.HIGH, issue_type="Bug",
    ))
    asyncio.run(system.poller.run_once())
    api_entries = [e for e in system.inbox.open_entries() if e.ticket_key == "API-11"]
    # With the gate off, the repo is unresolved but the ticket is not escalated
    # for that reason — it proceeds to the agent and transitions.
    assert api_entries == []
    assert system.source.get("API-11").status.value == "In Progress"


def test_transitioned_ticket_gets_workspace():
    system = build_system(JirakuConfig(source="memory"))
    outcomes = asyncio.run(system.poller.run_once())
    transitioned = [o for o in outcomes if o.action is TriageAction.TRANSITIONED]
    assert transitioned
    assert all(o.workspace for o in transitioned)
    assert all(o.resolution and o.resolution.repo for o in transitioned)
