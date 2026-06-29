from __future__ import annotations

from jiraku.domain import (
    Classification,
    InboxEntry,
    InboxStatus,
    Priority,
    Ticket,
    TicketCategory,
    TicketStatus,
    TriageAction,
    TriageMetrics,
    TriageOutcome,
)


def test_ticket_is_immutable_and_transitions_via_copy():
    t = Ticket(key="A-1", project="A", summary="s", description="d", reporter="r")
    assert t.status is TicketStatus.UNTRIAGED
    assert t.is_triageable
    moved = t.with_status(TicketStatus.IN_PROGRESS)
    assert moved is not t
    assert moved.status is TicketStatus.IN_PROGRESS
    assert t.status is TicketStatus.UNTRIAGED  # original unchanged
    assert not moved.is_triageable


def test_classification_confidence_gate():
    assert Classification(TicketCategory.BUG, "A", 0.6).is_confident
    assert not Classification(TicketCategory.BUG, "A", 0.59).is_confident
    assert not Classification(TicketCategory.UNKNOWN, "A", 0.99).is_confident


def test_metrics_record_and_automation_rate():
    m = TriageMetrics()
    assert m.automation_rate == 0.0
    cls = Classification(TicketCategory.BUG, "A", 0.9)
    m.record(TriageOutcome("A-1", TriageAction.TRANSITIONED, cls, agent="bug-agent"))
    m.record(TriageOutcome("A-2", TriageAction.ESCALATED, cls, agent="bug-agent"))
    assert m.processed == 2
    assert m.transitioned == 1
    assert m.escalated == 1
    assert m.by_category[TicketCategory.BUG] == 2
    assert m.by_agent["bug-agent"] == 2
    assert m.automation_rate == 0.5


def test_metrics_snapshot_is_independent():
    m = TriageMetrics(processed=3)
    snap = m.snapshot()
    m.processed = 99
    assert snap.processed == 3


def test_inbox_entry_resolution():
    e = InboxEntry(id="1", ticket_key="A-1", reason="why")
    assert e.status is InboxStatus.OPEN
    resolved = e.resolved("done")
    assert resolved.status is InboxStatus.RESOLVED
    assert resolved.resolution == "done"
    assert resolved.resolved_at is not None
    assert e.status is InboxStatus.OPEN  # original untouched


def test_priority_and_enum_str():
    assert str(Priority.HIGH) == "High"
    assert str(TicketStatus.IN_PROGRESS) == "In Progress"
    assert str(TicketCategory.FEATURE_REQUEST) == "Feature Request"
