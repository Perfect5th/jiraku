from __future__ import annotations

from jiraku.adapters.inmemory import InMemoryEventBus, InMemoryInboxRepository
from jiraku.application import AgentRouter, TriageService
from jiraku.domain import (
    Classification,
    Priority,
    Ticket,
    TicketCategory,
    TicketEscalated,
    TicketStatus,
    TicketTransitioned,
    TriageAction,
    ValidationResult,
)


class StubSource:
    def __init__(self, ticket: Ticket):
        self._tickets = {ticket.key: ticket}
        self.transitions: list[tuple[str, TicketStatus]] = []

    def fetch_untriaged(self):
        return [t for t in self._tickets.values() if t.is_triageable]

    def transition(self, key, status):
        self.transitions.append((key, status))
        self._tickets[key] = self._tickets[key].with_status(status)
        return self._tickets[key]

    def get(self, key):
        return self._tickets.get(key)


class StubClassifier:
    def __init__(self, classification: Classification):
        self._c = classification

    def classify(self, ticket, hint=None):
        return self._c


class StubAgent:
    def __init__(self, category, result, name="stub-agent"):
        self.name = name
        self._category = category
        self._result = result

    def handles(self, category):
        return category is self._category

    def validate(self, ticket, classification, resolution=None):
        return self._result


def _ticket():
    return Ticket(key="A-1", project="A", summary="s", description="d", reporter="r",
                  priority=Priority.MEDIUM)


def _build(classification, agents, ticket=None):
    ticket = ticket or _ticket()
    source = StubSource(ticket)
    bus = InMemoryEventBus()
    inbox = InMemoryInboxRepository()
    events = []
    bus.subscribe(events.append)
    service = TriageService(
        ticket_source=source,
        classifier=StubClassifier(classification),
        router=AgentRouter(agents),
        inbox=inbox,
        events=bus,
    )
    return service, source, inbox, events, ticket


def test_actionable_ticket_is_transitioned():
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    agent = StubAgent(TicketCategory.BUG, ValidationResult(actionable=True, summary="ok"))
    service, source, inbox, events, ticket = _build(cls, [agent])

    outcome = service.triage_ticket(ticket)

    assert outcome.action is TriageAction.TRANSITIONED
    assert outcome.agent == "stub-agent"
    assert (ticket.key, TicketStatus.IN_PROGRESS) in source.transitions
    assert inbox.open_entries() == []
    assert any(isinstance(e, TicketTransitioned) for e in events)
    assert service.metrics.transitioned == 1


def test_low_confidence_is_escalated():
    cls = Classification(TicketCategory.BUG, "A", 0.3)
    agent = StubAgent(TicketCategory.BUG, ValidationResult(actionable=True, summary="ok"))
    service, source, inbox, events, ticket = _build(cls, [agent])

    outcome = service.triage_ticket(ticket)

    assert outcome.action is TriageAction.ESCALATED
    assert len(inbox.open_entries()) == 1
    # Escalation surfaces to the inbox WITHOUT mutating the ticket's Jira status.
    assert source.transitions == []
    assert any(isinstance(e, TicketEscalated) for e in events)
    assert service.metrics.escalated == 1


def test_actionable_but_failed_transition_is_escalated():
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    agent = StubAgent(TicketCategory.BUG, ValidationResult(actionable=True, summary="ok"))
    service, source, inbox, events, ticket = _build(cls, [agent])

    def boom(key, status):
        raise ValueError("no In Progress transition in this workflow")

    source.transition = boom  # simulate a workflow without the transition

    outcome = service.triage_ticket(ticket)
    assert outcome.action is TriageAction.ESCALATED
    assert "could not transition" in outcome.note
    assert len(inbox.open_entries()) == 1


def test_unknown_category_is_escalated():
    cls = Classification(TicketCategory.UNKNOWN, "A", 0.95)
    service, source, inbox, events, ticket = _build(cls, [])
    outcome = service.triage_ticket(ticket)
    assert outcome.action is TriageAction.ESCALATED
    assert len(inbox.open_entries()) == 1


def test_no_agent_for_category_is_escalated():
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    # Only a docs agent registered, but ticket is a Bug.
    agent = StubAgent(TicketCategory.DOCUMENTATION, ValidationResult(True, "ok"))
    service, source, inbox, events, ticket = _build(cls, [agent])
    outcome = service.triage_ticket(ticket)
    assert outcome.action is TriageAction.ESCALATED
    assert "No worker agent" in outcome.note


def test_agent_needs_human_is_escalated():
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    agent = StubAgent(
        TicketCategory.BUG,
        ValidationResult(actionable=False, needs_human=True, summary="needs repro"),
    )
    service, source, inbox, events, ticket = _build(cls, [agent])
    outcome = service.triage_ticket(ticket)
    assert outcome.action is TriageAction.ESCALATED
    assert inbox.open_entries()[0].reason == "needs repro"


def test_escalation_persists_agent_details_and_rationale():
    cls = Classification(TicketCategory.BUG, "A", 0.9, rationale="looks buggy")
    agent = StubAgent(
        TicketCategory.BUG,
        ValidationResult(actionable=False, needs_human=True, summary="no repro",
                         details=("missing steps", "too short")),
    )
    service, source, inbox, events, ticket = _build(cls, [agent])
    service.triage_ticket(ticket)
    entry = inbox.open_entries()[0]
    assert entry.agent == "stub-agent"
    assert entry.details == ("missing steps", "too short")
    assert entry.rationale == "looks buggy"


def test_triage_batch_records_metrics():
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    agent = StubAgent(TicketCategory.BUG, ValidationResult(True, "ok"))
    service, source, inbox, events, ticket = _build(cls, [agent])
    outcomes = service.triage_batch([ticket])
    assert len(outcomes) == 1
    assert service.metrics.processed == 1
