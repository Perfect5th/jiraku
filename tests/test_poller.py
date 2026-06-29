from __future__ import annotations

import asyncio

from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import PollCycleCompleted, PollCycleStarted, TicketsFetched


def test_run_once_processes_seed_batch():
    system = build_system(JirakuConfig())
    events = []
    system.bus.subscribe(events.append)

    outcomes = asyncio.run(system.poller.run_once())

    assert len(outcomes) == 8
    assert system.service.metrics.processed == 8
    assert system.service.metrics.transitioned == 4
    assert system.service.metrics.escalated == 4
    assert system.service.metrics.poll_cycles == 1
    assert system.service.metrics.last_poll_at is not None
    assert len(system.inbox.open_entries()) == 4
    assert any(isinstance(e, PollCycleStarted) for e in events)
    assert any(isinstance(e, PollCycleCompleted) for e in events)
    assert any(isinstance(e, TicketsFetched) for e in events)


def test_second_poll_is_idempotent():
    system = build_system(JirakuConfig())
    asyncio.run(system.poller.run_once())
    second = asyncio.run(system.poller.run_once())
    assert second == []
    assert system.service.metrics.processed == 8  # unchanged
    assert system.service.metrics.poll_cycles == 2


def test_escalated_tickets_are_not_reprocessed():
    # Escalation no longer changes Jira status, so dedupe must come from the
    # poller skipping tickets that already have an open inbox entry.
    system = build_system(JirakuConfig())
    asyncio.run(system.poller.run_once())
    open_before = len(system.inbox.open_entries())
    assert open_before == 4

    asyncio.run(system.poller.run_once())
    # No duplicate inbox entries created for the already-surfaced tickets.
    assert len(system.inbox.open_entries()) == open_before


def test_run_forever_respects_max_cycles():
    system = build_system(JirakuConfig(interval_seconds=0.0))
    asyncio.run(system.poller.run_forever(max_cycles=3))
    assert system.service.metrics.poll_cycles == 3


def test_run_forever_stops_on_request():
    system = build_system(JirakuConfig(interval_seconds=100.0))

    async def drive():
        task = asyncio.create_task(system.poller.run_forever())
        # Let the first cycle complete, then ask it to stop.
        while system.service.metrics.poll_cycles < 1:
            await asyncio.sleep(0.01)
        system.poller.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())
    assert system.service.metrics.poll_cycles >= 1
