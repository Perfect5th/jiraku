"""Outbound ports — interfaces the application needs the outside world to fill.

Adapters (in-memory fakes, real Jira, the Copilot CLI, etc.) implement these
``Protocol`` classes. The application depends only on these abstractions.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ..domain import (
    Classification,
    DomainEvent,
    InboxEntry,
    RepoRef,
    RepoResolution,
    Ticket,
    TicketCategory,
    TicketStatus,
    ValidationResult,
    WorkResult,
)


@runtime_checkable
class TicketSource(Protocol):
    """A source of tickets to triage (the Jira side of the world)."""

    def fetch_untriaged(self) -> list[Ticket]:
        """Return tickets currently awaiting triage (Untriaged / To Do)."""

    def transition(self, key: str, status: TicketStatus) -> Ticket:
        """Move a ticket to ``status`` and return the updated ticket."""

    def get(self, key: str) -> Ticket | None:
        """Look up a single ticket by key."""

    def add_comment(self, key: str, body: str) -> str:
        """Post a comment to the issue; return the new comment's id."""


@runtime_checkable
class Classifier(Protocol):
    """Intent classification — turns a ticket into a :class:`Classification`.

    ``hint`` carries an optional authoritative note from a human reviewer (used
    when re-running triage from the dashboard "respond" action).
    """

    def classify(self, ticket: Ticket, hint: str | None = None) -> Classification: ...


@runtime_checkable
class RepoResolver(Protocol):
    """Resolve which repository a ticket belongs to (clone_url + path).

    Mirrors :class:`Classifier`: the harness calls this right after
    classification, and the result is confidence-gated. ``hint`` carries a
    human note from the dashboard "respond" action.
    """

    def resolve(
        self,
        ticket: Ticket,
        classification: Classification,
        hint: str | None = None,
    ) -> RepoResolution: ...


@runtime_checkable
class LearnedRulesStore(Protocol):
    """Persistence for repo-resolution rules learned from inbox corrections."""

    def learn(self, *, project: str, repo: RepoRef, tokens: tuple[str, ...] = ()) -> None:
        """Record that tickets in ``project`` (or matching ``tokens``) map to ``repo``."""

    def lookup(self, *, project: str, text: str) -> tuple[RepoRef, float, str] | None:
        """Return (repo, confidence, rationale) for the best learned match, if any."""

    def rules(self) -> list[dict]:
        """Return all learned rules (for inspection / persistence)."""


class WorkspaceProvisionError(Exception):
    """Raised by a :class:`WorkspaceProvisioner` when provisioning fails.

    Carries the failing command and its output so the failure can be surfaced
    to a human, who can examine it and supply a corrected upstream URL.
    """

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...] = (),
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stderr = stderr

    @property
    def command_line(self) -> str:
        return " ".join(self.command)

    def details(self) -> tuple[str, ...]:
        """Human-readable detail lines for the inbox entry."""
        lines: list[str] = []
        if self.command:
            lines.append(f"$ {self.command_line}")
        if self.returncode is not None:
            lines.append(f"exit code: {self.returncode}")
        for line in (self.stderr or "").strip().splitlines():
            if line.strip():
                lines.append(line.strip())
        return tuple(lines[:8])


@runtime_checkable
class WorkspaceProvisioner(Protocol):
    """Provision a local working copy for a resolved repo (``git clone`` + path).

    Returns the local workspace path. Implementations may be no-ops (dry-run)
    that only report the intended path without cloning. A failed clone must
    raise :class:`WorkspaceProvisionError`.
    """

    def provision(self, repo: RepoRef, ticket_key: str) -> str: ...


@runtime_checkable
class WorkAgentRunner(Protocol):
    """Engage a worker agent to actually do the work in a provisioned workspace.

    Called right after provisioning: e.g. invoke the Copilot CLI in the cloned
    repo to implement the ticket and open a pull request.
    """

    def run(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None,
        workspace: str,
    ) -> WorkResult: ...


@runtime_checkable
class WorkerAgent(Protocol):
    """A specialized agent that validates and works a category of ticket."""

    name: str

    def handles(self, category: TicketCategory) -> bool: ...

    def validate(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None = None,
    ) -> ValidationResult: ...



@runtime_checkable
class InboxRepository(Protocol):
    """Persistence for exceptions surfaced to the dashboard for human review."""

    def add(self, entry: InboxEntry) -> None: ...

    def get(self, entry_id: str) -> InboxEntry | None: ...

    def all(self) -> list[InboxEntry]: ...

    def open_entries(self) -> list[InboxEntry]: ...

    def resolve(self, entry_id: str, resolution: str) -> InboxEntry | None: ...


# An event handler receives a single domain event. Handlers must be cheap or
# marshal heavy work elsewhere; adapters own their own thread-safety.
EventHandler = Callable[[DomainEvent], None]


@runtime_checkable
class EventPublisher(Protocol):
    """Publish side of the event bus (what the application is allowed to do)."""

    def publish(self, event: DomainEvent) -> None: ...


@runtime_checkable
class EventBus(EventPublisher, Protocol):
    """Full event bus: publish plus subscribe (used by inbound adapters)."""

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register ``handler``; returns a callable that unsubscribes it."""


@runtime_checkable
class Clock(Protocol):
    """Injectable time source so the harness is deterministically testable."""

    def now(self): ...
