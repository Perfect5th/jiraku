"""The polling service — jiraya's scheduled background heartbeat.

Periodically asks the ticket source for fresh work and runs every ticket
through the triage harness. Blocking work (Jira HTTP calls, Copilot CLI
subprocesses) is pushed onto worker threads so an event loop driving a TUI is
never starved.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..domain import (
    PollCycleCompleted,
    PollCycleStarted,
    TicketsFetched,
    TriageOutcome,
)
from ..ports import EventPublisher, InboxRepository, TicketSource
from .triage_service import TriageService, _NullPublisher


class TriagePoller:
    """Drives :class:`TriageService` on a fixed interval."""

    def __init__(
        self,
        *,
        ticket_source: TicketSource,
        service: TriageService,
        events: EventPublisher | None = None,
        interval_seconds: float = 1800.0,
        inbox: InboxRepository | None = None,
    ) -> None:
        self._source = ticket_source
        self._service = service
        self._events = events or _NullPublisher()
        self.interval_seconds = interval_seconds
        self._inbox = inbox
        self._stop = asyncio.Event()

    async def run_once(self) -> list[TriageOutcome]:
        """Execute a single poll → triage cycle and return the outcomes."""
        cycle = self._service.metrics.poll_cycles + 1
        self._events.publish(PollCycleStarted(cycle=cycle))

        tickets = await asyncio.to_thread(self._source.fetch_untriaged)
        self._events.publish(TicketsFetched(tickets=tuple(tickets)))

        # Skip tickets we've already actioned (persisted ledger, so this holds
        # across restarts) or that are awaiting human review, so repeated polls
        # never re-process or create duplicate inbox entries.
        skip = self._open_inbox_keys() | self._service.actioned_keys()
        outcomes: list[TriageOutcome] = []
        for ticket in tickets:
            if ticket.key in skip:
                continue
            outcome = await asyncio.to_thread(self._service.triage_ticket, ticket)
            outcomes.append(outcome)

        self._service.note_poll_cycle(datetime.now(timezone.utc))
        self._events.publish(
            PollCycleCompleted(cycle=cycle, processed=len(outcomes))
        )
        return outcomes

    def _open_inbox_keys(self) -> set[str]:
        if self._inbox is None:
            return set()
        return {entry.ticket_key for entry in self._inbox.open_entries()}

    async def run_forever(self, *, max_cycles: int | None = None) -> None:
        """Poll until :meth:`stop` is called (or ``max_cycles`` is reached)."""
        self._stop.clear()
        cycles = 0
        while not self._stop.is_set():
            await self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            await self._sleep_or_stop(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for ``seconds`` but wake immediately if stop is requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
