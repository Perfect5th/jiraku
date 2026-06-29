from __future__ import annotations

import asyncio

from jiraku.adapters.inmemory import (
    InMemoryInboxRepository,
    InMemoryTriageLedger,
)
from jiraku.adapters.sqlite import SqliteStateStore
from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import (
    Classification,
    EscalationStage,
    InboxEntry,
    RepoRef,
    RepoResolution,
    TicketCategory,
    TicketForgotten,
    TriageAction,
    TriageOutcome,
    WorkResult,
)


def _outcome(key, action=TriageAction.TRANSITIONED, pr=""):
    cls = Classification(TicketCategory.BUG, "PROJ", 0.9)
    work = WorkResult(started=True, pr_url=pr) if pr else None
    return TriageOutcome(
        key, action, cls, agent="bug-agent",
        resolution=RepoResolution(RepoRef("acme/proj", "u"), 0.95),
        workspace=f"/tmp/ws/{key}", work=work, note="n")


# -- adapter-level removal ---------------------------------------------------

def test_in_memory_ledger_get_record_and_forget():
    led = InMemoryTriageLedger()
    led.record(_outcome("PROJ-1", pr="https://x/pull/1"))
    assert led.get_record("PROJ-1").pr_url.endswith("/pull/1")
    assert led.get_record("PROJ-404") is None
    assert led.forget("PROJ-1") is True
    assert led.get_record("PROJ-1") is None
    assert led.actioned_keys() == set()
    assert led.forget("PROJ-1") is False  # idempotent


def test_in_memory_inbox_delete_for_ticket():
    inbox = InMemoryInboxRepository()
    inbox.add(InboxEntry(id="a", ticket_key="PROJ-1", reason="r1"))
    inbox.add(InboxEntry(id="b", ticket_key="PROJ-1", reason="r2"))
    inbox.add(InboxEntry(id="c", ticket_key="PROJ-2", reason="r3"))
    assert inbox.delete_for_ticket("PROJ-1") == 2
    assert {e.ticket_key for e in inbox.open_entries()} == {"PROJ-2"}
    assert inbox.delete_for_ticket("PROJ-1") == 0


def test_sqlite_delete_for_ticket_and_forget_persist(tmp_path):
    db = tmp_path / "state.db"
    store = SqliteStateStore(db)
    store.record(_outcome("PROJ-1", pr="https://x/pull/1"))
    store.add(InboxEntry(id="e1", ticket_key="PROJ-1", reason="needs repo",
                         stage=EscalationStage.PROVISIONING))
    store.add(InboxEntry(id="e2", ticket_key="PROJ-2", reason="other"))

    assert store.get_record("PROJ-1") is not None
    assert store.delete_for_ticket("PROJ-1") == 1
    assert store.forget("PROJ-1") is True
    store.close()

    reopened = SqliteStateStore(db)  # simulate restart
    assert reopened.get_record("PROJ-1") is None        # ledger deletion persisted
    assert "PROJ-1" not in reopened.actioned_keys()
    assert {e.ticket_key for e in reopened.open_entries()} == {"PROJ-2"}
    assert reopened.forget("PROJ-1") is False
    reopened.close()


# -- service use case --------------------------------------------------------

def test_forget_ticket_removes_and_allows_retriage(tmp_path):
    db = str(tmp_path / "state.db")
    system = build_system(JirakuConfig(source="memory", state_db_path=db))
    events: list = []
    system.bus.subscribe(events.append)

    asyncio.run(system.poller.run_once())
    assert system.service.metrics.processed == 8
    assert system.service.metrics.escalated == 4
    key = "WEB-201"  # an escalated ticket: still untriaged in the source
    assert key in system.service.actioned_keys()
    assert any(e.ticket_key == key for e in system.inbox.open_entries())

    removed = system.service.forget_ticket(key)
    assert removed is True
    assert key not in system.service.actioned_keys()
    assert not any(e.ticket_key == key for e in system.inbox.open_entries())
    assert system.service.metrics.processed == 7      # decremented
    assert system.service.metrics.escalated == 3
    assert any(isinstance(e, TicketForgotten) and e.ticket_key == key
               for e in events)

    # The next poll re-actions exactly the forgotten ticket.
    out = asyncio.run(system.poller.run_once())
    assert [o.ticket_key for o in out] == [key]
    assert system.service.metrics.processed == 8
    assert system.service.metrics.escalated == 4
    assert any(e.ticket_key == key for e in system.inbox.open_entries())
    system.inbox.close()


def test_forget_unknown_ticket_is_noop():
    system = build_system(JirakuConfig(source="memory"))
    events: list = []
    system.bus.subscribe(events.append)
    assert system.service.forget_ticket("NOPE-1") is False
    assert not any(isinstance(e, TicketForgotten) for e in events)


def test_forget_persists_across_restart(tmp_path):
    db = str(tmp_path / "state.db")
    s1 = build_system(JirakuConfig(source="memory", state_db_path=db))
    asyncio.run(s1.poller.run_once())
    assert s1.service.forget_ticket("WEB-201") is True
    s1.inbox.close()

    s2 = build_system(JirakuConfig(source="memory", state_db_path=db))
    assert "WEB-201" not in s2.service.actioned_keys()
    assert not any(e.ticket_key == "WEB-201" for e in s2.inbox.open_entries())
    assert s2.service.metrics.processed == 7  # restored without the forgotten one
    s2.inbox.close()


# -- CLI ---------------------------------------------------------------------

def test_cli_forget_removes_actioned_ticket(tmp_path, capsys):
    from jiraku.cli import main

    db = str(tmp_path / "state.db")
    seed = build_system(JirakuConfig(source="memory", state_db_path=db))
    asyncio.run(seed.poller.run_once())
    seed.inbox.close()

    rc = main(["forget", "--source", "memory", "--state-db", db, "WEB-201"])
    assert rc == 0
    assert "Forgot WEB-201" in capsys.readouterr().out

    # Re-opening the store proves the deletion was durable.
    after = build_system(JirakuConfig(source="memory", state_db_path=db))
    assert "WEB-201" not in after.service.actioned_keys()
    after.inbox.close()


def test_cli_forget_unknown_ticket_exits_nonzero(tmp_path, capsys):
    from jiraku.cli import main

    db = str(tmp_path / "state.db")
    rc = main(["forget", "--source", "memory", "--state-db", db, "NOPE-1"])
    assert rc == 1
    assert "Nothing to forget" in capsys.readouterr().err

