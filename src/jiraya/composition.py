"""Composition root — the one place where concrete adapters are wired together.

Everything above this module depends only on ports; this module is allowed to
know about concrete classes. Driving adapters (CLI, TUI) ask :func:`build_system`
for a fully assembled :class:`JirayaSystem` and stay ignorant of the wiring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .adapters import (
    CopilotWorkAgentRunner,
    NoopWorkAgentRunner,
    ReadOnlyTicketSource,
)
from .adapters.agents import default_agents
from .adapters.classifier import CopilotCliClassifier, KeywordClassifier
from .adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from .adapters.jira import JiraRestTicketSource
from .adapters.resolver import (
    CompositeRepoResolver,
    InMemoryLearnedRulesStore,
    FileLearnedRulesStore,
    KeywordRepoResolver,
    LearnedRulesRepoResolver,
    RegistryRepoResolver,
    default_catalog,
    load_catalog,
)
from .adapters.workspace import GitWorkspaceProvisioner, NoopWorkspaceProvisioner
from .application import AgentRouter, TriagePoller, TriageService
from .domain import ActivityLevel, ActivityLogged, AgentActivity
from .ports import (
    Classifier,
    EventBus,
    InboxRepository,
    LearnedRulesStore,
    RepoResolver,
    TicketSource,
    WorkAgentRunner,
    WorkspaceProvisioner,
)

_DEFAULT_JQL = 'status in ("To Do", "Untriaged") ORDER BY created ASC'


@dataclass(slots=True)
class JiraConfig:
    """Connection settings for the real Jira adapter (read from env by default)."""

    base_url: str = ""
    email: str = ""
    api_token: str = ""
    jql: str = _DEFAULT_JQL

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "JiraConfig":
        env = env if env is not None else os.environ
        # Accept both JIRA_BASE_URL and the shorter JIRA_BASE.
        base = env.get("JIRA_BASE_URL") or env.get("JIRA_BASE") or ""
        return cls(
            base_url=base,
            email=env.get("JIRA_EMAIL", ""),
            api_token=env.get("JIRA_API_TOKEN", ""),
            jql=env.get("JIRA_JQL") or _DEFAULT_JQL,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)


@dataclass(slots=True)
class JirayaConfig:
    """User-facing configuration for assembling the system."""

    classifier: str = "keyword"      # "keyword" | "copilot"
    source: str = "auto"             # "auto" | "memory" | "jira"
    interval_seconds: float = 1800.0
    confidence_threshold: float = 0.6
    copilot_model: str | None = None
    copilot_fallback_to_keyword: bool = False
    dry_run: bool = False
    repo_registry_path: str | None = None   # YAML repo catalog
    learned_rules_path: str | None = None    # where learned repo rules persist
    require_repo: bool = True                 # gate on confident repo resolution
    provision: bool = False                   # actually `git clone` workspaces
    work: bool = False                        # run the work agent (Copilot) + open PRs
    jira: JiraConfig = field(default_factory=JiraConfig)

    def resolve_source(self) -> str:
        """Resolve the effective source, honouring ``auto`` detection."""
        if self.source == "auto":
            return "jira" if self.jira.is_configured else "memory"
        return self.source


@dataclass(slots=True)
class JirayaSystem:
    """A fully assembled, ready-to-run jiraya instance."""

    bus: EventBus
    source: TicketSource
    inbox: InboxRepository
    router: AgentRouter
    service: TriageService
    poller: TriagePoller
    resolver: RepoResolver
    learned_store: LearnedRulesStore
    provisioner: WorkspaceProvisioner
    work_runner: WorkAgentRunner
    source_mode: str = "memory"
    dry_run: bool = False


def build_classifier(config: JirayaConfig) -> Classifier:
    if config.classifier == "copilot":
        fallback = KeywordClassifier() if config.copilot_fallback_to_keyword else None
        return CopilotCliClassifier(model=config.copilot_model, fallback=fallback)
    if config.classifier == "keyword":
        return KeywordClassifier()
    raise ValueError(f"Unknown classifier: {config.classifier!r}")


def build_learned_store(config: JirayaConfig) -> LearnedRulesStore:
    if config.learned_rules_path:
        return FileLearnedRulesStore(config.learned_rules_path)
    return InMemoryLearnedRulesStore()


def build_resolver(
    config: JirayaConfig, learned_store: LearnedRulesStore
) -> RepoResolver:
    catalog = (
        load_catalog(config.repo_registry_path)
        if config.repo_registry_path
        else default_catalog()
    )
    # Layered strategy: learned rules and the project registry give the
    # high-confidence hits; the keyword/code-token matcher handles the residual.
    return CompositeRepoResolver([
        LearnedRulesRepoResolver(learned_store),
        RegistryRepoResolver(catalog),
        KeywordRepoResolver(catalog),
    ])


def build_provisioner(config: JirayaConfig, *, dry_run: bool) -> WorkspaceProvisioner:
    # Running the work agent needs a real checkout, so --work implies cloning.
    wants_clone = config.provision or config.work
    if wants_clone and not dry_run:
        return GitWorkspaceProvisioner()
    return NoopWorkspaceProvisioner()


def build_work_runner(config: JirayaConfig, *, dry_run: bool) -> WorkAgentRunner:
    if config.work and not dry_run:
        return CopilotWorkAgentRunner(model=config.copilot_model)
    return NoopWorkAgentRunner()


def build_source(config: JirayaConfig) -> TicketSource:
    mode = config.resolve_source()
    if mode == "memory":
        return InMemoryTicketSource()
    if mode == "jira":
        jira = config.jira
        if not jira.base_url:
            raise ValueError(
                "Jira source selected but no base URL is configured "
                "(set JIRA_BASE_URL/JIRA_BASE, JIRA_EMAIL and JIRA_API_TOKEN)."
            )
        return JiraRestTicketSource(
            base_url=jira.base_url,
            email=jira.email or None,
            api_token=jira.api_token or None,
            jql=jira.jql,
        )
    raise ValueError(f"Unknown source: {config.source!r}")


def build_system(config: JirayaConfig | None = None) -> JirayaSystem:
    """Assemble every component for the given configuration."""
    config = config or JirayaConfig()
    mode = config.resolve_source()

    bus = InMemoryEventBus()
    inbox = InMemoryInboxRepository()
    classifier = build_classifier(config)
    router = AgentRouter(default_agents())
    learned_store = build_learned_store(config)
    resolver = build_resolver(config, learned_store)

    source: TicketSource = build_source(config)
    # Dry-run only makes sense against a real, mutating backend.
    dry_run = config.dry_run and mode == "jira"
    if dry_run:
        source = ReadOnlyTicketSource(
            source,
            on_transition=_make_dry_run_logger(bus, "Would transition to {0}"),
            on_comment=_make_dry_run_logger(bus, "Would post comment"),
        )
    provisioner = build_provisioner(config, dry_run=dry_run)
    work_runner = build_work_runner(config, dry_run=dry_run)

    service = TriageService(
        ticket_source=source,
        classifier=classifier,
        router=router,
        inbox=inbox,
        events=bus,
        resolver=resolver,
        provisioner=provisioner,
        work_runner=work_runner,
        learned_store=learned_store,
        confidence_threshold=config.confidence_threshold,
        require_repo=config.require_repo,
    )
    poller = TriagePoller(
        ticket_source=source,
        service=service,
        events=bus,
        interval_seconds=config.interval_seconds,
        inbox=inbox,
    )
    return JirayaSystem(
        bus=bus,
        source=source,
        inbox=inbox,
        router=router,
        service=service,
        poller=poller,
        resolver=resolver,
        learned_store=learned_store,
        provisioner=provisioner,
        work_runner=work_runner,
        source_mode=mode,
        dry_run=dry_run,
    )


def _make_dry_run_logger(bus: EventBus, template: str):
    def observer(key: str, arg) -> None:
        bus.publish(
            ActivityLogged(
                activity=AgentActivity(
                    agent="dry-run",
                    ticket_key=key,
                    message=f"{template.format(arg)} (dry-run; Jira not modified).",
                    level=ActivityLevel.INFO,
                )
            )
        )

    return observer
