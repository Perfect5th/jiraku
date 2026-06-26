from __future__ import annotations

from textual.widgets import Button, DataTable, Input, RichLog, Static

from jiraya.composition import JirayaConfig
from jiraya.domain import (
    EscalationStage,
    InboxEntry,
    InboxStatus,
    Priority,
    RepoRef,
    Ticket,
    TicketEscalated,
)
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


async def test_detail_modal_shows_clone_command_for_provisioning_entry(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        entry = InboxEntry(
            id="p1", ticket_key="API-99",
            reason="git clone failed — examine the command and supply a corrected URL.",
            stage=EscalationStage.PROVISIONING,
            repo=RepoRef("acme/api", "https://example.invalid/api.git"),
            details=("$ git clone --depth 1 https://example.invalid/api.git /tmp/ws/API-99",
                     "exit code: 128", "fatal: repository not found"),
        )
        app._inbox_entries[entry.id] = entry
        app.push_screen(
            InboxDetailScreen(entry, dry_run=False),
            lambda r: app._on_detail_result(entry.id, r),
        )
        await pilot.pause()
        detail = str(app.screen.query_one("#detail", Static).render())
        assert "provisioning" in detail.lower()
        assert "git clone" in detail
        assert "repository not found" in detail
        # A repo-needing exception focuses the clone-URL input.
        assert app.screen.focused is app.screen.query_one("#repo-url", Input)


def _make_app_with_repo_gap() -> JirayaApp:
    app = JirayaApp(config=JirayaConfig(source="memory"), poll_interval=10_000)
    # Inject an uncovered-project ticket so a repository-stage exception appears.
    app._system.source.add(Ticket(
        key="API-77", project="API", summary="Timeout on the orders endpoint",
        description="Steps: POST /v2/orders; expected 200; actual 504. Stack trace attached.",
        reporter="r", priority=Priority.HIGH, issue_type="Bug",
    ))
    return app


async def test_tickets_table_shows_resolved_repo(wait_for):
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        tickets = app.query_one("#tickets", DataTable)
        # The Repo column is populated for transitioned tickets.
        repo_cell = str(tickets.get_cell("PROJ-101", app._cols["repo"]))
        assert "acme/proj-service" in repo_cell


async def test_tickets_table_shows_pr_when_work_opens_one(wait_for):
    from jiraya.domain import TicketWorkStarted, WorkResult
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        app._system.bus.publish(TicketWorkStarted(
            ticket_key="PROJ-101", agent="bug-agent",
            result=WorkResult(started=True, summary="opened",
                              pr_url="https://github.com/acme/proj-service/pull/12"),
        ))
        tickets = app.query_one("#tickets", DataTable)
        ok = await wait_for(
            pilot,
            lambda: "PR #12" in str(tickets.get_cell("PROJ-101", app._cols["outcome"])),
        )
        assert ok


async def test_tickets_table_shows_needs_input(wait_for):
    from jiraya.domain import TicketWorkStarted, WorkResult
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        app._system.bus.publish(TicketWorkStarted(
            ticket_key="PROJ-101", agent="bug-agent",
            result=WorkResult.blocked("Which DB?", branch="jiraya/proj-101"),
        ))
        tickets = app.query_one("#tickets", DataTable)
        ok = await wait_for(
            pilot,
            lambda: "Needs input" in str(tickets.get_cell("PROJ-101", app._cols["outcome"])),
        )
        assert ok


async def test_activity_header_shows_active_worker_count(wait_for):
    from jiraya.domain import TicketWorkStarted, WorkResult
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        log = app.query_one("#activity", RichLog)
        # Four tickets transitioned to In Progress -> four engaged workers.
        ok = await wait_for(pilot, lambda: "4 active workers" in str(log.border_title))
        assert ok
        # A worker that opens a PR is no longer active.
        app._system.bus.publish(TicketWorkStarted(
            ticket_key="PROJ-101", agent="bug-agent",
            result=WorkResult(started=True, pr_url="https://x/pull/1"),
        ))
        ok = await wait_for(pilot, lambda: "3 active workers" in str(log.border_title))
        assert ok
        # A worker blocked & surfaced to the inbox is no longer active either.
        app._system.bus.publish(TicketEscalated(entry=InboxEntry(
            id="z", ticket_key="WEB-200", reason="blocked", stage=EscalationStage.WORK)))
        ok = await wait_for(pilot, lambda: "2 active workers" in str(log.border_title))
        assert ok


async def test_dashboard_rehydrates_actioned_tickets_from_state(wait_for, tmp_path):
    from jiraya.composition import build_system
    db = str(tmp_path / "state.db")
    # Populate the store with a full triage cycle, then close it.
    seed = build_system(JirayaConfig(source="memory", state_db_path=db))
    await seed.poller.run_once()
    seed.inbox.close()

    # A fresh dashboard on the same DB restores the actioned tickets + inbox
    # on mount, before any polling.
    app = JirayaApp(
        system=build_system(JirayaConfig(source="memory", state_db_path=db)),
        poll_interval=10_000)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#tickets", DataTable).row_count == 8
        assert app.query_one("#inbox", DataTable).row_count == 4
        assert app._system.service.metrics.processed == 8
        # Workspaces are restored so on-demand follow-up still works.
        assert "PROJ-101" in app._workspaces
    app._system.inbox.close()


async def test_work_question_modal_answer_resumes(wait_for):
    # A WORK-stage entry's modal answers via the note and resumes work.
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        entry = InboxEntry(
            id="w1", ticket_key="PROJ-101",
            reason="Work agent is blocked and needs input: Which DB?",
            stage=EscalationStage.WORK, agent="bug-agent",
            workspace="/tmp/ws/PROJ-101", branch="jiraya/proj-101",
            details=("Which DB?",),
        )
        app._inbox_entries[entry.id] = entry
        app.push_screen(
            InboxDetailScreen(entry, dry_run=False),
            lambda result: app._on_detail_result(entry.id, result),
        )
        await pilot.pause()
        # The note label/button reflect the answer-and-resume affordance.
        assert "Answer & resume work" in str(
            app.screen.query_one("#rerun", Button).label)
        assert app.screen.focused is app.screen.query_one("#note", Input)


async def test_followup_modal_runs_agent_in_workspace(wait_for):
    from jiraya.tui.followup import FollowupScreen
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        # PROJ-101 transitioned, so the dashboard recorded its workspace.
        await wait_for(pilot, lambda: "PROJ-101" in app._workspaces)
        app.query_one("#tickets", DataTable).move_cursor(row=0)

        app.action_followup()
        await pilot.pause()
        assert isinstance(app.screen, FollowupScreen)
        app.screen.query_one("#instruction", Input).value = "Address review feedback"
        app.screen.query_one("#run", Button).press()

        runner = app._system.work_runner  # NoopWorkAgentRunner in the demo
        ok = await wait_for(pilot, lambda: any(k == "PROJ-101" for k, _ in runner.runs))
        assert ok


async def test_followup_requires_provisioned_workspace(wait_for):
    from jiraya.tui.followup import FollowupScreen
    app = _make_app()
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._system.service.metrics.processed == 8)
        # Row 1 is PROJ-102, which escalated (no provisioned workspace).
        app.query_one("#tickets", DataTable).move_cursor(row=1)
        assert "PROJ-102" not in app._workspaces

        app.action_followup()
        await pilot.pause()
        # No modal is opened for a ticket without a workspace.
        assert not isinstance(app.screen, FollowupScreen)


async def test_respond_with_repo_unblocks_repository_stage(wait_for):
    app = _make_app_with_repo_gap()
    async with app.run_test() as pilot:
        await wait_for(
            pilot,
            lambda: any(e.ticket_key == "API-77" and e.needs_repo
                        for e in app._system.inbox.open_entries()),
        )
        entry = next(e for e in app._system.inbox.open_entries()
                     if e.ticket_key == "API-77")

        # Open the detail modal for that specific entry and supply a repo.
        app.push_screen(
            InboxDetailScreen(entry, dry_run=False),
            lambda result: app._on_detail_result(entry.id, result),
        )
        await pilot.pause()
        app.screen.query_one("#repo-url", Input).value = "https://github.com/acme/api.git"
        app.screen.query_one("#both", Button).press()

        ok = await wait_for(
            pilot,
            lambda: app._system.inbox.get(entry.id).status is InboxStatus.RESOLVED,
        )
        assert ok
        # The repo mapping was learned: a new API ticket now resolves.
        from jiraya.domain import Classification, TicketCategory
        res = app._system.resolver.resolve(
            app._system.source.get("API-77"),
            Classification(TicketCategory.BUG, "API", 0.9),
        )
        assert res.is_confident and res.repo.key == "acme/api"
