from __future__ import annotations

import pytest

from jiraya.adapters import ReadOnlyTicketSource
from jiraya.adapters.inmemory import InMemoryTicketSource
from jiraya.domain import TicketStatus


def test_readonly_passes_reads_through():
    inner = InMemoryTicketSource()
    ro = ReadOnlyTicketSource(inner)
    assert ro.dry_run is True
    assert len(ro.fetch_untriaged()) == len(inner.fetch_untriaged())
    key = inner.fetch_untriaged()[0].key
    assert ro.get(key).key == key


def test_readonly_suppresses_transition_writes():
    inner = InMemoryTicketSource()
    ro = ReadOnlyTicketSource(inner)
    ticket = inner.fetch_untriaged()[0]
    before = ticket.status

    returned = ro.transition(ticket.key, TicketStatus.IN_PROGRESS)

    # The returned ticket reflects the intended change ...
    assert returned.status is TicketStatus.IN_PROGRESS
    # ... but nothing was actually persisted to the wrapped source.
    assert inner.get(ticket.key).status is before


def test_readonly_notifies_observer():
    inner = InMemoryTicketSource()
    seen = []
    ro = ReadOnlyTicketSource(inner, on_transition=lambda k, s: seen.append((k, s)))
    ticket = inner.fetch_untriaged()[0]
    ro.transition(ticket.key, TicketStatus.IN_PROGRESS)
    assert seen == [(ticket.key, TicketStatus.IN_PROGRESS)]


def test_readonly_unknown_key_raises():
    ro = ReadOnlyTicketSource(InMemoryTicketSource())
    with pytest.raises(KeyError):
        ro.transition("NOPE-1", TicketStatus.IN_PROGRESS)


def test_readonly_suppresses_comments():
    inner = InMemoryTicketSource()
    seen = []
    ro = ReadOnlyTicketSource(inner, on_comment=lambda k, b: seen.append((k, b)))
    key = inner.fetch_untriaged()[0].key

    cid = ro.add_comment(key, "hello")

    assert cid == ""  # nothing posted
    assert inner.comments(key) == []  # wrapped source untouched
    assert seen == [(key, "hello")]  # observer notified
