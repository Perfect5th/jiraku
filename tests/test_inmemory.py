from __future__ import annotations

import pytest

from jiraya.adapters.inmemory import (
    InMemoryEventBus,
    InMemoryInboxRepository,
    InMemoryTicketSource,
    random_ticket,
    sample_tickets,
)
from jiraya.domain import InboxEntry, Ticket, TicketStatus


def test_ticket_source_filters_triageable():
    src = InMemoryTicketSource()
    untriaged = src.fetch_untriaged()
    keys = {t.key for t in untriaged}
    assert "WEB-150" not in keys  # status DONE is filtered out
    assert all(t.is_triageable for t in untriaged)


def test_ticket_source_transition_and_idempotent_fetch():
    src = InMemoryTicketSource()
    first = {t.key for t in src.fetch_untriaged()}
    for key in first:
        src.transition(key, TicketStatus.IN_PROGRESS)
    assert src.fetch_untriaged() == []
    assert src.get(next(iter(first))).status is TicketStatus.IN_PROGRESS


def test_ticket_source_add_and_get_unknown():
    src = InMemoryTicketSource(tickets=[])
    assert src.fetch_untriaged() == []
    src.add(random_ticket())
    assert len(src.fetch_untriaged()) == 1
    assert src.get("nope") is None


def test_ticket_source_add_comment():
    src = InMemoryTicketSource()
    key = src.fetch_untriaged()[0].key
    cid = src.add_comment(key, "first note")
    assert cid
    src.add_comment(key, "second note")
    assert src.comments(key) == ["first note", "second note"]


def test_ticket_source_add_comment_unknown_key_raises():
    src = InMemoryTicketSource(tickets=[])
    with pytest.raises(KeyError):
        src.add_comment("NOPE-1", "x")


def test_inbox_repository_lifecycle():
    repo = InMemoryInboxRepository()
    repo.add(InboxEntry(id="1", ticket_key="A-1", reason="r1"))
    repo.add(InboxEntry(id="2", ticket_key="A-2", reason="r2"))
    assert len(repo.open_entries()) == 2
    # newest first
    assert repo.all()[0].id == "2"
    resolved = repo.resolve("1", "fixed")
    assert resolved is not None
    assert len(repo.open_entries()) == 1
    assert repo.resolve("missing", "x") is None


def test_event_bus_subscribe_unsubscribe_and_isolation():
    bus = InMemoryEventBus()
    received = []
    unsub = bus.subscribe(received.append)

    def bad(_):
        raise RuntimeError("boom")

    bus.subscribe(bad)  # must not break publishing
    bus.publish("hello")
    assert received == ["hello"]
    unsub()
    bus.publish("world")
    assert received == ["hello"]


def test_sample_tickets_have_unique_keys():
    keys = [t.key for t in sample_tickets()]
    assert len(keys) == len(set(keys))
