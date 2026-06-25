from __future__ import annotations

import asyncio

import pytest

from jiraya.composition import JirayaConfig, build_system
from jiraya.domain import InboxStatus, TicketCategory


def _system_with_inbox():
    system = build_system(JirayaConfig(source="memory"))
    asyncio.run(system.poller.run_once())
    return system


def _entry_for(system, ticket_key):
    return next(e for e in system.inbox.open_entries() if e.ticket_key == ticket_key)


def test_persisted_fields_on_escalated_entry():
    system = _system_with_inbox()
    # PROJ-102 is a bug with no repro steps → escalated by the bug agent.
    entry = _entry_for(system, "PROJ-102")
    assert entry.agent == "bug-agent"
    assert entry.category is TicketCategory.BUG
    assert entry.details  # validation details captured
    assert entry.rationale  # classifier rationale captured


def test_respond_comment_only_keeps_entry_open():
    system = _system_with_inbox()
    entry = _entry_for(system, "PROJ-102")

    resp = system.service.respond_to_inbox(
        entry.id, "Could you add reproduction steps?", post_comment=True, rerun=False
    )

    assert resp.commented is True
    assert resp.comment_id  # in-memory returns a generated id
    assert resp.retriaged is False
    assert system.source.comments("PROJ-102") == ["Could you add reproduction steps?"]
    # Entry stays open while awaiting the reporter's clarification.
    assert system.inbox.get(entry.id).status is InboxStatus.OPEN


def test_respond_rerun_resolves_and_retriages_with_hint():
    system = _system_with_inbox()
    # SUP-400 was Unknown (could not classify). A hint says it's a bug.
    entry = _entry_for(system, "SUP-400")
    assert entry.category is TicketCategory.UNKNOWN

    resp = system.service.respond_to_inbox(
        entry.id, "This is a bug — it crashes on startup.",
        post_comment=False, rerun=True,
    )

    assert resp.retriaged is True
    assert resp.outcome is not None
    # The hint drove re-classification to Bug.
    assert resp.outcome.classification.category is TicketCategory.BUG
    # The original entry is now resolved.
    assert system.inbox.get(entry.id).status is InboxStatus.RESOLVED


def test_respond_both_comments_and_retriages():
    system = _system_with_inbox()
    entry = _entry_for(system, "DOC-301")

    resp = system.service.respond_to_inbox(
        entry.id, "Please clarify which doc page; treat as documentation.",
        post_comment=True, rerun=True,
    )

    assert resp.commented is True
    assert resp.retriaged is True
    assert system.source.comments("DOC-301")
    assert system.inbox.get(entry.id).status is InboxStatus.RESOLVED


def test_respond_unknown_entry_raises():
    system = _system_with_inbox()
    with pytest.raises(KeyError):
        system.service.respond_to_inbox("does-not-exist", "x", rerun=True)


def test_respond_dry_run_suppresses_comment(monkeypatch):
    # A dry-run Jira system must not post comments even when asked to.
    from jiraya.composition import JiraConfig

    cfg = JirayaConfig(
        source="jira", dry_run=True,
        jira=JiraConfig(base_url="https://x.atlassian.net", email="e", api_token="t"),
    )
    system = build_system(cfg)
    # Inject an inbox entry directly (no network needed).
    from jiraya.domain import InboxEntry
    system.inbox.add(InboxEntry(id="z1", ticket_key="PROJ-1", reason="why",
                                category=TicketCategory.BUG))

    resp = system.service.respond_to_inbox("z1", "note", post_comment=True, rerun=False)
    # The read-only wrapper returns an empty comment id (nothing posted).
    assert resp.comment_id == ""
