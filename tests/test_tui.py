from __future__ import annotations

from textual.widgets import Button, DataTable, Input, RichLog, Static

from jiraya.composition import JirayaConfig
from jiraya.domain import InboxStatus
from jiraya.tui import JirayaApp
from jiraya.tui.detail import InboxDetailScreen


def _make_app() -> JirayaApp:
    # Large interval so the dashboard runs exactly one automatic cycle; further
    # cycles are triggered explicitly via key bindings in the tests.
    return JirayaApp(config=JirayaConfig(), poll_interval=10_000)


async def test_dashboard_populates_after_first_cycle(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        ok = await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        assert ok

        tickets = app.query_one("#tickets", DataTable)
        inbox = app.query_one("#inbox", DataTable)
        assert tickets.row_count == 8
        assert inbox.row_count == 4

        metrics_text = app.query_one("#metrics", Static).render()
        assert "Processed" in str(metrics_text)

        activity = app.query_one("#activity", RichLog)
        assert len(activity.lines) > 0


async def test_generate_action_injects_and_triages_new_ticket(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        before = app._system.service.metrics.processed

        await pilot.press("g")
        ok = await wait_for(
            pilot, lambda: app._system.service.metrics.processed > before
        )
        assert ok
        assert app.query_one("#tickets", DataTable).row_count >= 9


async def test_resolve_action_clears_inbox_row(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        inbox = app.query_one("#inbox", DataTable)
        assert inbox.row_count == 4
        open_before = len(app._system.inbox.open_entries())

        await pilot.press("r")
        ok = await wait_for(pilot, lambda: inbox.row_count == 3)
        assert ok
        assert len(app._system.inbox.open_entries()) == open_before - 1


async def test_inbox_detail_modal_shows_details_and_posts_comment(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        entry_id = app._selected_inbox_id()
        assert entry_id is not None
        ticket_key = app._inbox_entries[entry_id].ticket_key

        app.action_detail()
        await pilot.pause()
        assert isinstance(app.screen, InboxDetailScreen)
        # The expandable detail view surfaces the persisted agent + rationale.
        detail = str(app.screen.query_one("#detail", Static).render())
        assert "Agent" in detail
        assert "Rationale" in detail

        app.screen.query_one("#note", Input).value = "Please add reproduction steps."
        app.screen.query_one("#comment", Button).press()

        ok = await wait_for(pilot, lambda: bool(app._system.source.comments(ticket_key)))
        assert ok
        assert app._system.source.comments(ticket_key) == ["Please add reproduction steps."]
        # Comment-only keeps the entry open for the reporter to respond.
        assert app._system.inbox.get(entry_id).status is InboxStatus.OPEN


async def test_inbox_detail_modal_rerun_resolves_entry(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        entry_id = app._selected_inbox_id()
        assert entry_id is not None

        app.action_detail()
        await pilot.pause()
        app.screen.query_one("#note", Input).value = "Reclassify as a bug, please."
        app.screen.query_one("#rerun", Button).press()

        ok = await wait_for(
            pilot,
            lambda: app._system.inbox.get(entry_id).status is InboxStatus.RESOLVED,
        )
        assert ok
