"""Domain events.

The application layer publishes these through the ``EventPublisher`` port; any
inbound adapter (the TUI, a logger, a metrics exporter) can subscribe without
the core knowing those adapters exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    AgentActivity,
    Classification,
    InboxEntry,
    RepoResolution,
    Ticket,
    TicketStatus,
    TriageMetrics,
    TriageOutcome,
    WorkResult,
    utcnow,
)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base type for everything published on the event bus."""

    at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class PollCycleStarted(DomainEvent):
    cycle: int = 0


@dataclass(frozen=True, slots=True)
class TicketsFetched(DomainEvent):
    tickets: tuple[Ticket, ...] = ()

    @property
    def count(self) -> int:
        return len(self.tickets)


@dataclass(frozen=True, slots=True)
class TicketClassified(DomainEvent):
    ticket: Ticket | None = None
    classification: Classification | None = None


@dataclass(frozen=True, slots=True)
class TicketRepoResolved(DomainEvent):
    ticket_key: str = ""
    resolution: "RepoResolution | None" = None


@dataclass(frozen=True, slots=True)
class TicketRouted(DomainEvent):
    ticket_key: str = ""
    agent: str = ""


@dataclass(frozen=True, slots=True)
class TicketTransitioned(DomainEvent):
    ticket_key: str = ""
    from_status: TicketStatus | None = None
    to_status: TicketStatus | None = None
    agent: str = ""


@dataclass(frozen=True, slots=True)
class TicketWorkStarted(DomainEvent):
    ticket_key: str = ""
    agent: str = ""
    result: "WorkResult | None" = None


@dataclass(frozen=True, slots=True)
class TicketEscalated(DomainEvent):
    entry: InboxEntry | None = None


@dataclass(frozen=True, slots=True)
class ActivityLogged(DomainEvent):
    activity: AgentActivity | None = None


@dataclass(frozen=True, slots=True)
class TicketTriaged(DomainEvent):
    outcome: TriageOutcome | None = None


@dataclass(frozen=True, slots=True)
class PollCycleCompleted(DomainEvent):
    cycle: int = 0
    processed: int = 0


@dataclass(frozen=True, slots=True)
class MetricsUpdated(DomainEvent):
    metrics: TriageMetrics | None = None
