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
    recommended_model: str = ""  # model recommended for working this ticket

    @property
    def is_confident(self) -> bool:
        return self.category is not TicketCategory.UNKNOWN and self.confidence >= 0.6


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A concrete pointer to where a ticket's work lives."""

    key: str                 # logical repo id, e.g. "canonical/landscape"
    clone_url: str           # git clone URL
    path: str = ""           # sub-path/module within the repo (optional)
    default_branch: str = "" # optional starting branch

    def __str__(self) -> str:
        return f"{self.key}{('/' + self.path) if self.path else ''}"


@dataclass(frozen=True, slots=True)
class RepoResolution:
    """Result of resolving which repository a ticket belongs to.

    Mirrors :class:`Classification`: a best guess with a confidence and the
    adapter that produced it, gated by the same confidence convention.
    """

    repo: RepoRef | None
    confidence: float
    rationale: str = ""
    source: str = "unknown"

    @property
    def is_confident(self) -> bool:
        return self.repo is not None and self.confidence >= 0.6

    @classmethod
    def unresolved(cls, rationale: str, source: str = "unknown") -> "RepoResolution":
        return cls(repo=None, confidence=0.0, rationale=rationale, source=source)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of a worker agent's initial validation of a ticket."""

    actionable: bool
    summary: str
    needs_human: bool = False
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkResult:
    """Outcome of a worker agent actually starting work in a provisioned repo.

    Produced by the :class:`~jiraya.ports.outbound.WorkAgentRunner` after a
    ticket transitions: e.g. the Copilot CLI ran in the cloned workspace and
    opened a pull request.
    """

    started: bool
    summary: str = ""
    branch: str = ""
    pr_url: str = ""
    model: str = ""  # the model the work agent ran with
    question: str = ""  # set when the agent is blocked and needs human input
    details: tuple[str, ...] = ()

    @property
    def opened_pr(self) -> bool:
        return bool(self.pr_url)

    @property
    def needs_input(self) -> bool:
        return bool(self.question)

    @classmethod
    def skipped(cls, summary: str) -> "WorkResult":
        return cls(started=False, summary=summary)

    @classmethod
    def blocked(cls, question: str, *, branch: str = "", model: str = "") -> "WorkResult":
        return cls(started=False, question=question, branch=branch, model=model,
                   summary=f"Work agent is blocked and needs input: {question}")



class TriageAction(str, Enum):
    """Terminal action taken by the harness for a ticket."""

    TRANSITIONED = "transitioned"  # moved to In Progress, worker agent engaged
    ESCALATED = "escalated"        # surfaced to the dashboard for a human

    def __str__(self) -> str:
        return self.value


class EscalationStage(str, Enum):
    """Which harness step surfaced a ticket for human review."""

    CLASSIFICATION = "classification"
    REPOSITORY = "repository"
    VALIDATION = "validation"
    PROVISIONING = "provisioning"
    WORK = "work"

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
    resolution: RepoResolution | None = None
    workspace: str = ""  # local clone path when a workspace was provisioned
    work: "WorkResult | None" = None  # result of the agent starting work
    stage: EscalationStage | None = None  # the stage at which it was escalated
    note: str = ""
    at: datetime = field(default_factory=utcnow)

    @property
    def escalated(self) -> bool:
        return self.action is TriageAction.ESCALATED


@dataclass(frozen=True, slots=True)
class TriageRecord:
    """A persisted projection of a :class:`TriageOutcome` (the actioned-ticket
    ledger). Holds just what the dashboard needs to remember a ticket across
    restarts, without reconstructing the full outcome graph."""

    ticket_key: str
    action: TriageAction
    category: TicketCategory
    agent: str = ""
    repo: str = ""          # repo key
    workspace: str = ""
    pr_url: str = ""
    question: str = ""      # the agent's blocking question, if any
    stage: EscalationStage | None = None
    note: str = ""
    at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_outcome(cls, outcome: "TriageOutcome") -> "TriageRecord":
        repo = ""
        if outcome.resolution is not None and outcome.resolution.repo is not None:
            repo = outcome.resolution.repo.key
        return cls(
            ticket_key=outcome.ticket_key,
            action=outcome.action,
            category=outcome.classification.category,
            agent=outcome.agent or "",
            repo=repo,
            workspace=outcome.workspace,
            pr_url=(outcome.work.pr_url if outcome.work else ""),
            question=(outcome.work.question if outcome.work else ""),
            stage=outcome.stage,
            note=outcome.note,
            at=outcome.at,
        )



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
    agent: str | None = None
    rationale: str = ""
    details: tuple[str, ...] = ()
    stage: EscalationStage = EscalationStage.CLASSIFICATION
    repo: RepoRef | None = None  # best-guess repo when known (esp. repository stage)
    workspace: str = ""          # provisioned checkout (for resuming WORK-stage items)
    branch: str = ""             # the work branch (for resuming WORK-stage items)
    status: InboxStatus = InboxStatus.OPEN
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolution: str = ""

    @property
    def needs_repo(self) -> bool:
        return self.stage in (EscalationStage.REPOSITORY, EscalationStage.PROVISIONING)

    def resolved(self, resolution: str, *, now: datetime | None = None) -> "InboxEntry":
        return replace(
            self,
            status=InboxStatus.RESOLVED,
            resolution=resolution,
            resolved_at=now or utcnow(),
        )


@dataclass(frozen=True, slots=True)
class InboxResponse:
    """Result of a human responding to an inbox exception.

    Captures what the "respond" action did: whether a comment was posted back
    to Jira and/or the ticket was re-triaged with the reviewer's note as a hint.
    """

    entry: InboxEntry
    note: str = ""
    repo: "RepoRef | None" = None
    commented: bool = False
    comment_id: str | None = None
    taught: bool = False  # whether a repo rule was learned from this response
    retriaged: bool = False
    resumed: bool = False  # whether blocked work was resumed (vs re-triaged)
    outcome: "TriageOutcome | None" = None


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

    def forget(self, record: "TriageRecord") -> None:
        """Reverse the tally contributed by a single ledger record."""
        self.processed = max(0, self.processed - 1)
        if self.by_category.get(record.category):
            self.by_category[record.category] -= 1
            if self.by_category[record.category] <= 0:
                del self.by_category[record.category]
        if record.action is TriageAction.TRANSITIONED:
            self.transitioned = max(0, self.transitioned - 1)
        elif record.action is TriageAction.ESCALATED:
            self.escalated = max(0, self.escalated - 1)
        if record.agent and self.by_agent.get(record.agent):
            self.by_agent[record.agent] -= 1
            if self.by_agent[record.agent] <= 0:
                del self.by_agent[record.agent]

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
