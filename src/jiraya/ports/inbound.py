"""Inbound ports — the use cases the application offers to driving adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import InboxResponse, RepoRef, Ticket, TriageMetrics, TriageOutcome


@runtime_checkable
class TriageService(Protocol):
    """The core use case: triage tickets and report what happened."""

    def triage_ticket(self, ticket: Ticket, hint: str | None = None) -> TriageOutcome: ...

    def triage_batch(self, tickets: list[Ticket]) -> list[TriageOutcome]: ...

    def respond_to_inbox(
        self,
        entry_id: str,
        note: str,
        *,
        repo: RepoRef | None = None,
        post_comment: bool = False,
        rerun: bool = False,
    ) -> InboxResponse: ...

    def run_followup(self, ticket_key: str, instruction: str) -> TriageOutcome | None: ...

    def forget_ticket(self, ticket_key: str) -> bool: ...

    @property
    def metrics(self) -> TriageMetrics: ...
