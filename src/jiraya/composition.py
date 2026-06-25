"""Composition root — the one place where concrete adapters are wired together.

Everything above this module depends only on ports; this module is allowed to
know about concrete classes. Driving adapters (CLI, TUI) ask :func:`build_system`
for a fully assembled :class:`JirayaSystem` and stay ignorant of the wiring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .adapters import ReadOnlyTicketSource
from .adapters.agents import default_agents
from .adapters.classifier import CopilotCliClassifier, KeywordClassifier
from .adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
)
from .adapters.jira import JiraRestTicketSource
from .application import AgentRouter, TriagePoller, TriageService
from .domain import ActivityLevel, ActivityLogged, AgentActivity, TicketStatus
from .ports import Classifier, EventBus, InboxRepository, TicketSource

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
    source_mode: str = "memory"
    dry_run: bool = False


def build_classifier(config: JirayaConfig) -> Classifier:
    if config.classifier == "copilot":
        fallback = KeywordClassifier() if config.copilot_fallback_to_keyword else None
        return CopilotCliClassifier(model=config.copilot_model, fallback=fallback)
    if config.classifier == "keyword":
        return KeywordClassifier()
    raise ValueError(f"Unknown classifier: {config.classifier!r}")


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

    source: TicketSource = build_source(config)
    # Dry-run only makes sense against a real, mutating backend.
    dry_run = config.dry_run and mode == "jira"
    if dry_run:
        source = ReadOnlyTicketSource(
            source, on_transition=_make_dry_run_observer(bus)
        )

    service = TriageService(
        ticket_source=source,
        classifier=classifier,
        router=router,
        inbox=inbox,
        events=bus,
        confidence_threshold=config.confidence_threshold,
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
        source_mode=mode,
        dry_run=dry_run,
    )


def _make_dry_run_observer(bus: EventBus):
    def observer(key: str, status: TicketStatus) -> None:
        bus.publish(
            ActivityLogged(
                activity=AgentActivity(
                    agent="dry-run",
                    ticket_key=key,
                    message=f"Would transition to {status} (dry-run; Jira not modified).",
                    level=ActivityLevel.INFO,
                )
            )
        )

    return observer
