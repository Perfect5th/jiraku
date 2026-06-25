"""Core domain entities and value objects for jiraya.

This module is pure business logic with **no external dependencies** (stdlib
only). It is the heart of the hexagonal architecture: everything else depends
on the domain, never the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (kept here so the domain owns its clock)."""
    return datetime.now(timezone.utc)


class TicketStatus(str, Enum):
    """Lifecycle states a ticket moves through during triage."""

    UNTRIAGED = "Untriaged"
    TODO = "To Do"
    IN_PROGRESS = "In Progress"
    NEEDS_REVIEW = "Needs Review"
    DONE = "Done"

    def __str__(self) -> str:  # nicer rendering in the TUI/CLI
        return self.value


#: Statuses the polling service considers "fresh" and in need of triage.
TRIAGEABLE_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.UNTRIAGED, TicketStatus.TODO}
)


class TicketCategory(str, Enum):
    """Intent classification buckets produced by the classifier agent."""

    BUG = "Bug"
    FEATURE_REQUEST = "Feature Request"
    DOCUMENTATION = "Documentation"
    UNKNOWN = "Unknown"

    def __str__(self) -> str:
        return self.value


class Priority(str, Enum):
    """Jira-style priority ladder."""

    LOWEST = "Lowest"
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    HIGHEST = "Highest"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Ticket:
    """An immutable snapshot of a Jira issue.

    State changes (e.g. a status transition) produce a *new* ``Ticket`` via
    :meth:`with_status` rather than mutating in place, keeping the domain free
    of hidden side effects.
    """

    key: str
    project: str
    summary: str
    description: str
    reporter: str
    priority: Priority = Priority.MEDIUM
    status: TicketStatus = TicketStatus.UNTRIAGED
    labels: tuple[str, ...] = ()
    issue_type: str = ""  # the native Jira issue type (Bug, Story, Epic, …)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def with_status(self, status: TicketStatus, *, now: datetime | None = None) -> "Ticket":
        return replace(self, status=status, updated_at=now or utcnow())

    @property
    def is_triageable(self) -> bool:
        return self.status in TRIAGEABLE_STATUSES


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of intent classification for a single ticket."""

    category: TicketCategory
    target_project: str
    confidence: float
    rationale: str = ""
    source: str = "unknown"  # which classifier produced this

    @property
    def is_confident(self) -> bool:
        return self.category is not TicketCategory.UNKNOWN and self.confidence >= 0.6


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of a worker agent's initial validation of a ticket."""

    actionable: bool
    summary: str
    needs_human: bool = False
    details: tuple[str, ...] = ()


class TriageAction(str, Enum):
    """Terminal action taken by the harness for a ticket."""

    TRANSITIONED = "transitioned"  # moved to In Progress, worker agent engaged
    ESCALATED = "escalated"        # surfaced to the dashboard for a human

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """A complete record of how one ticket was triaged."""

    ticket_key: str
    action: TriageAction
    classification: Classification
    agent: str | None = None
    validation: ValidationResult | None = None
    note: str = ""
    at: datetime = field(default_factory=utcnow)

    @property
    def escalated(self) -> bool:
        return self.action is TriageAction.ESCALATED


class InboxStatus(str, Enum):
    OPEN = "Open"
    RESOLVED = "Resolved"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class InboxEntry:
    """An exception surfaced to the jiraya dashboard for human review.

    Produced whenever the system cannot confidently classify a ticket or a
    worker agent decides the ticket needs clarification.
    """

    id: str
    ticket_key: str
    reason: str
    category: TicketCategory = TicketCategory.UNKNOWN
    confidence: float = 0.0
    status: InboxStatus = InboxStatus.OPEN
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolution: str = ""

    def resolved(self, resolution: str, *, now: datetime | None = None) -> "InboxEntry":
        return replace(
            self,
            status=InboxStatus.RESOLVED,
            resolution=resolution,
            resolved_at=now or utcnow(),
        )


class ActivityLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """A single line in the agent activity feed shown in the dashboard."""

    agent: str
    ticket_key: str
    message: str
    level: ActivityLevel = ActivityLevel.INFO
    at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class TriageMetrics:
    """Mutable running tally of triage activity, surfaced in the dashboard."""

    processed: int = 0
    transitioned: int = 0
    escalated: int = 0
    by_category: dict[TicketCategory, int] = field(default_factory=dict)
    by_agent: dict[str, int] = field(default_factory=dict)
    poll_cycles: int = 0
    last_poll_at: datetime | None = None

    def record(self, outcome: TriageOutcome) -> None:
        self.processed += 1
        self.by_category[outcome.classification.category] = (
            self.by_category.get(outcome.classification.category, 0) + 1
        )
        if outcome.action is TriageAction.TRANSITIONED:
            self.transitioned += 1
        elif outcome.action is TriageAction.ESCALATED:
            self.escalated += 1
        if outcome.agent:
            self.by_agent[outcome.agent] = self.by_agent.get(outcome.agent, 0) + 1

    @property
    def automation_rate(self) -> float:
        """Share of processed tickets handled without human escalation."""
        if self.processed == 0:
            return 0.0
        return self.transitioned / self.processed

    def snapshot(self) -> "TriageMetrics":
        """Return an independent copy safe to hand to another thread/UI."""
        return TriageMetrics(
            processed=self.processed,
            transitioned=self.transitioned,
            escalated=self.escalated,
            by_category=dict(self.by_category),
            by_agent=dict(self.by_agent),
            poll_cycles=self.poll_cycles,
            last_poll_at=self.last_poll_at,
        )
