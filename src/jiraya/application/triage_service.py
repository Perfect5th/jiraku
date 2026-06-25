r"""The triage harness — orchestration of the end-to-end workflow.

This is the application core that ties the ports together:

    classify -> route -> validate -> transition  (happy path)
                                  \-> escalate    (low confidence / needs human)

It depends only on ports, never on concrete adapters, so the same logic runs
against the in-memory fakes, a real Jira instance, or the Copilot CLI.
"""

from __future__ import annotations

import uuid
from typing import Callable

from ..domain import (
    ActivityLevel,
    AgentActivity,
    ActivityLogged,
    Classification,
    InboxEntry,
    InboxResponse,
    MetricsUpdated,
    Ticket,
    TicketCategory,
    TicketClassified,
    TicketEscalated,
    TicketRouted,
    TicketStatus,
    TicketTransitioned,
    TicketTriaged,
    TriageAction,
    TriageMetrics,
    TriageOutcome,
)
from ..ports import Classifier, EventPublisher, InboxRepository, TicketSource
from .router import AgentRouter


class _NullPublisher:
    """No-op publisher used when no event bus is wired in."""

    def publish(self, event) -> None:  # noqa: D401, ANN001
        return None


class TriageService:
    """Implements the inbound :class:`~jiraya.ports.inbound.TriageService` port."""

    def __init__(
        self,
        *,
        ticket_source: TicketSource,
        classifier: Classifier,
        router: AgentRouter,
        inbox: InboxRepository,
        events: EventPublisher | None = None,
        confidence_threshold: float = 0.6,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._source = ticket_source
        self._classifier = classifier
        self._router = router
        self._inbox = inbox
        self._events = events or _NullPublisher()
        self._threshold = confidence_threshold
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._metrics = TriageMetrics()

    # -- public API -----------------------------------------------------------

    @property
    def metrics(self) -> TriageMetrics:
        return self._metrics

    def triage_batch(self, tickets: list[Ticket]) -> list[TriageOutcome]:
        return [self.triage_ticket(t) for t in tickets]

    def respond_to_inbox(
        self,
        entry_id: str,
        note: str,
        *,
        post_comment: bool = False,
        rerun: bool = False,
    ) -> InboxResponse:
        """Act on a human's response to a surfaced exception.

        Optionally posts ``note`` as a comment back to the Jira issue and/or
        re-runs triage for the ticket using ``note`` as an authoritative hint.
        Re-running resolves the original inbox entry (a fresh one is created if
        the ticket still cannot be actioned).
        """
        entry = self._inbox.get(entry_id)
        if entry is None:
            raise KeyError(f"Unknown inbox entry: {entry_id}")

        note = (note or "").strip()
        commented = False
        comment_id: str | None = None
        retriaged = False
        outcome: TriageOutcome | None = None

        if post_comment and note:
            comment_id = self._source.add_comment(entry.ticket_key, note)
            commented = True
            if comment_id:
                self._log("reviewer", entry.ticket_key,
                          f"Posted comment to Jira (id {comment_id}).",
                          level=ActivityLevel.SUCCESS)

        if rerun:
            self._inbox.resolve(
                entry_id,
                f"Re-triaged with reviewer note: {note}" if note else "Re-triaged",
            )
            ticket = self._source.get(entry.ticket_key)
            if ticket is not None:
                self._log("reviewer", entry.ticket_key,
                          "Re-running triage with reviewer note as a hint.",
                          level=ActivityLevel.INFO)
                outcome = self.triage_ticket(ticket, hint=note or None)
                retriaged = True
            else:
                self._log("reviewer", entry.ticket_key,
                          "Could not re-run triage: ticket not found.",
                          level=ActivityLevel.ERROR)
            # Refresh the open-inbox count in any dashboards.
            self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))

        final_entry = self._inbox.get(entry_id) or entry
        return InboxResponse(
            entry=final_entry,
            note=note,
            commented=commented,
            comment_id=comment_id,
            retriaged=retriaged,
            outcome=outcome,
        )

    def note_poll_cycle(self, at=None) -> None:
        """Record that a polling cycle ran (owned here so metrics stay internal)."""
        self._metrics.poll_cycles += 1
        self._metrics.last_poll_at = at
        self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))

    def triage_ticket(self, ticket: Ticket, hint: str | None = None) -> TriageOutcome:
        """Run a single ticket through the full triage workflow.

        ``hint`` is an optional authoritative note from a human reviewer, used
        when re-triaging from the dashboard "respond" action.
        """
        classification = self._classify(ticket, hint)

        # 1. Confidence gate — unclear intent goes straight to a human.
        if not classification.is_confident:
            outcome = self._escalate(
                ticket,
                classification,
                reason=self._low_confidence_reason(classification),
                agent=None,
            )
            return self._finish(outcome)

        # 2. Route to a specialized worker agent.
        agent = self._router.route(classification)
        if agent is None:
            outcome = self._escalate(
                ticket,
                classification,
                reason=f"No worker agent registered for '{classification.category}'.",
                agent=None,
            )
            return self._finish(outcome)

        self._events.publish(TicketRouted(ticket_key=ticket.key, agent=agent.name))
        self._log(agent.name, ticket.key, f"Picked up {classification.category} ticket.")

        # 3. Initial validation by the specialized agent.
        result = agent.validate(ticket, classification)
        if not result.actionable or result.needs_human:
            outcome = self._escalate(
                ticket,
                classification,
                reason=result.summary,
                agent=agent.name,
                validation_details=result.details,
            )
            return self._finish(outcome)

        # 4. Actionable: transition to In Progress and let the agent run.
        outcome = self._transition(ticket, classification, agent.name, result)
        return self._finish(outcome)

    # -- workflow steps -------------------------------------------------------

    def _classify(self, ticket: Ticket, hint: str | None = None) -> Classification:
        classification = self._classifier.classify(ticket, hint)
        self._events.publish(
            TicketClassified(ticket=ticket, classification=classification)
        )
        suffix = " using reviewer hint" if hint else ""
        self._log(
            "classifier",
            ticket.key,
            f"Classified as {classification.category} "
            f"({classification.confidence:.0%} confidence) -> "
            f"{classification.target_project}{suffix}.",
        )
        return classification

    def _transition(
        self,
        ticket: Ticket,
        classification: Classification,
        agent: str,
        result,
    ) -> TriageOutcome:
        try:
            updated = self._source.transition(ticket.key, TicketStatus.IN_PROGRESS)
        except Exception as exc:  # noqa: BLE001 - real workflows vary; degrade safely
            # The ticket is actionable but we could not move it (e.g. the
            # workflow has no "In Progress" transition). Surface it instead of
            # losing the result.
            return self._escalate(
                ticket,
                classification,
                reason=f"Validated but could not transition to In Progress: {exc}",
                agent=agent,
            )
        self._events.publish(
            TicketTransitioned(
                ticket_key=ticket.key,
                from_status=ticket.status,
                to_status=updated.status,
                agent=agent,
            )
        )
        self._log(
            agent,
            ticket.key,
            f"Validated and moved to In Progress: {result.summary}",
            level=ActivityLevel.SUCCESS,
        )
        return TriageOutcome(
            ticket_key=ticket.key,
            action=TriageAction.TRANSITIONED,
            classification=classification,
            agent=agent,
            validation=result,
            note=result.summary,
        )

    def _escalate(
        self,
        ticket: Ticket,
        classification: Classification,
        *,
        reason: str,
        agent: str | None,
        validation_details: tuple[str, ...] = (),
    ) -> TriageOutcome:
        entry = InboxEntry(
            id=self._id_factory(),
            ticket_key=ticket.key,
            reason=reason,
            category=classification.category,
            confidence=classification.confidence,
            agent=agent,
            rationale=classification.rationale,
            details=tuple(validation_details),
        )
        self._inbox.add(entry)
        # Per the spec, exceptions are surfaced to the jiraya dashboard for human
        # review; the ticket's Jira status is deliberately left untouched.
        self._events.publish(TicketEscalated(entry=entry))
        self._log(
            agent or "triage",
            ticket.key,
            f"Surfaced for human review: {reason}",
            level=ActivityLevel.WARNING,
        )
        return TriageOutcome(
            ticket_key=ticket.key,
            action=TriageAction.ESCALATED,
            classification=classification,
            agent=agent,
            note=reason,
        )

    def _finish(self, outcome: TriageOutcome) -> TriageOutcome:
        self._metrics.record(outcome)
        self._events.publish(TicketTriaged(outcome=outcome))
        self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))
        return outcome

    # -- helpers --------------------------------------------------------------

    def _log(
        self,
        agent: str,
        ticket_key: str,
        message: str,
        *,
        level: ActivityLevel = ActivityLevel.INFO,
    ) -> None:
        self._events.publish(
            ActivityLogged(
                activity=AgentActivity(
                    agent=agent, ticket_key=ticket_key, message=message, level=level
                )
            )
        )

    @staticmethod
    def _low_confidence_reason(classification: Classification) -> str:
        if classification.category is TicketCategory.UNKNOWN:
            return "Classifier could not determine the ticket category."
        return (
            f"Low confidence ({classification.confidence:.0%}) on "
            f"'{classification.category}'."
        )
