from __future__ import annotations

import argparse
import asyncio

from jiraku.adapters.inmemory import InMemoryTriageLedger
from jiraku.adapters.sqlite import SqliteStateStore
from jiraku.cli import _resolve_state_db
from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import (
    Classification,
    EscalationStage,
    InboxEntry,
    InboxStatus,
    RepoRef,
    RepoResolution,
    TicketCategory,
    TriageAction,
    TriageOutcome,
    WorkResult,
)


def _outcome(key, action=TriageAction.TRANSITIONED, pr="", question="",
             stage=None):
    cls = Classification(TicketCategory.BUG, "PROJ", 0.9)
    work = None
    if pr:
        work = WorkResult(started=True, pr_url=pr)
    elif question:
        work = WorkResult.blocked(question)
    return TriageOutcome(
        key, action, cls, agent="bug-agent",
        resolution=RepoResolution(RepoRef("acme/proj", "u"), 0.95),
        workspace=f"/tmp/ws/{key}", work=work, stage=stage, note="n")


# -- in-memory ledger --------------------------------------------------------

def test_in_memory_ledger_latest_per_ticket():
    led = InMemoryTriageLedger()
    led.record(_outcome("PROJ-1", pr="https://x/pull/1"))
    led.record(_outcome("PROJ-1", pr="https://x/pull/2"))  # same ticket again
    led.record(_outcome("PROJ-2"))
    assert led.actioned_keys() == {"PROJ-1", "PROJ-2"}
    recs = {r.ticket_key: r for r in led.records()}
    assert recs["PROJ-1"].pr_url.endswith("/pull/2")  # latest wins


# -- sqlite store ------------------------------------------------------------

def test_sqlite_inbox_round_trip(tmp_path):
    db = tmp_path / "state.db"
    store = SqliteStateStore(db)
    entry = InboxEntry(
        id="e1", ticket_key="PROJ-9", reason="needs repo",
        category=TicketCategory.BUG, confidence=0.3, agent="bug-agent",
        details=("$ git clone x", "fatal"), stage=EscalationStage.PROVISIONING,
        repo=RepoRef("acme/x", "https://x/x.git"), workspace="/tmp/ws/PROJ-9",
        branch="jiraku/proj-9")
    store.add(entry)
    store.close()

    reopened = SqliteStateStore(db)  # simulate restart
    got = reopened.get("e1")
    assert got is not None
    assert got.stage is EscalationStage.PROVISIONING
    assert got.repo.key == "acme/x"
    assert got.branch == "jiraku/proj-9"
    assert got.details == ("$ git clone x", "fatal")
    assert len(reopened.open_entries()) == 1
    reopened.resolve("e1", "done")
    reopened.close()

    again = SqliteStateStore(db)
    assert again.open_entries() == []
    assert again.get("e1").status is InboxStatus.RESOLVED
    again.close()


def test_sqlite_ledger_round_trip(tmp_path):
    db = tmp_path / "state.db"
    store = SqliteStateStore(db)
    store.record(_outcome("PROJ-1", pr="https://x/pull/3"))
    store.record(_outcome("PROJ-2", action=TriageAction.ESCALATED,
                          question="Which DB?", stage=EscalationStage.WORK))
    store.close()

    reopened = SqliteStateStore(db)
    assert reopened.actioned_keys() == {"PROJ-1", "PROJ-2"}
    recs = {r.ticket_key: r for r in reopened.records()}
    assert recs["PROJ-1"].pr_url.endswith("/pull/3")
    assert recs["PROJ-2"].stage is EscalationStage.WORK
    assert recs["PROJ-2"].question == "Which DB?"
    reopened.close()


# -- composition wiring ------------------------------------------------------

def test_state_db_uses_sqlite_for_inbox_and_ledger(tmp_path):
    system = build_system(JirakuConfig(source="memory",
                                       state_db_path=str(tmp_path / "s.db")))
    assert isinstance(system.inbox, SqliteStateStore)
    assert system.ledger is system.inbox  # one store backs both
    system.inbox.close()


def test_no_state_db_uses_in_memory():
    from jiraku.adapters.inmemory import InMemoryInboxRepository
    system = build_system(JirakuConfig(source="memory"))
    assert isinstance(system.inbox, InMemoryInboxRepository)
    assert isinstance(system.ledger, InMemoryTriageLedger)


# -- end to end: persistence + dedup + metrics restore -----------------------

def test_actioned_tickets_persist_and_dedup_across_restart(tmp_path):
    db = str(tmp_path / "state.db")
    s1 = build_system(JirakuConfig(source="memory", state_db_path=db))
    asyncio.run(s1.poller.run_once())
    assert s1.service.metrics.processed == 8
    assert len(s1.inbox.open_entries()) == 4
    assert len(s1.service.actioned_keys()) == 8
    s1.inbox.close()

    # Restart: a fresh system on the same DB restores everything.
    s2 = build_system(JirakuConfig(source="memory", state_db_path=db))
    assert s2.service.metrics.processed == 8          # metrics restored
    assert s2.service.metrics.escalated == 4
    assert len(s2.inbox.open_entries()) == 4          # inbox restored
    assert len(s2.service.actioned_keys()) == 8

    # The poller does not re-action anything.
    out = asyncio.run(s2.poller.run_once())
    assert out == []
    assert s2.service.metrics.processed == 8
    s2.inbox.close()


# -- CLI state-db resolution -------------------------------------------------

def test_resolve_state_db():
    base = argparse.Namespace(no_state=False, state_db=None, default_state=True)
    assert _resolve_state_db(base).endswith("state.db")
    assert _resolve_state_db(
        argparse.Namespace(no_state=False, state_db=None, default_state=False)) is None
    assert _resolve_state_db(
        argparse.Namespace(no_state=True, state_db="/x", default_state=True)) is None
    assert _resolve_state_db(
        argparse.Namespace(no_state=False, state_db="/c.db", default_state=True)) == "/c.db"
