"""The jiraya TUI dashboard.

A Textual app that drives the triage poller in the background and renders, in
real time, the ticket pipeline, the agent activity feed, live metrics and the
exception inbox surfaced for human review.

The app is a *driving adapter*: it talks to the core only through the event bus
(subscribe) and the assembled :class:`~jiraya.composition.JirayaSystem`. Domain
events arrive from poller worker threads and are marshalled onto the UI event
loop with ``call_soon_threadsafe`` before any widget is touched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from ..composition import JirayaConfig, JirayaSystem, build_system
from ..domain import (
    ActivityLevel,
    ActivityLogged,
    DomainEvent,
    EscalationStage,
    InboxEntry,
    InboxStatus,
    MetricsUpdated,
    PollCycleStarted,
    RepoRef,
    TicketCategory,
    TicketClassified,
    TicketEscalated,
    TicketForgotten,
    TicketRepoResolved,
    TicketRouted,
    TicketStatus,
    TicketTransitioned,
    TicketTriaged,
    TicketWorkStarted,
    TicketsFetched,
    TriageAction,
    TriageMetrics,
)
from ..adapters.inmemory import InMemoryTicketSource, random_ticket
from .detail import InboxDetailScreen
from .followup import FollowupScreen
from .confirm import ConfirmScreen

# Explicit truecolor palette chosen for ≥4.5:1 contrast on both the normal dark
# rows and the muted selected-row highlight (so colours never wash out when a
# row is highlighted), and independent of the terminal's ANSI theme.
_C_BUG = "#ff8080"
_C_FEATURE = "#5cc8ff"
_C_DOC = "#4fe06d"
_C_UNKNOWN = "#ffd24a"
_C_BLUE = "#6cb3ff"
_C_DIM = "#aab0b8"
_C_TEXT = "#e6e6e6"

_CATEGORY_STYLE = {
    TicketCategory.BUG: f"bold {_C_BUG}",
    TicketCategory.FEATURE_REQUEST: f"bold {_C_FEATURE}",
    TicketCategory.DOCUMENTATION: f"bold {_C_DOC}",
    TicketCategory.UNKNOWN: f"bold {_C_UNKNOWN}",
}
_STATUS_STYLE = {
    TicketStatus.UNTRIAGED: "#c7ccd1",
    TicketStatus.TODO: "#ffffff",
    TicketStatus.IN_PROGRESS: f"bold {_C_DOC}",
    TicketStatus.NEEDS_REVIEW: f"bold {_C_UNKNOWN}",
    TicketStatus.DONE: _C_BLUE,
}
_LEVEL_STYLE = {
    ActivityLevel.INFO: _C_DIM,
    ActivityLevel.SUCCESS: _C_DOC,
    ActivityLevel.WARNING: _C_UNKNOWN,
    ActivityLevel.ERROR: f"bold {_C_BUG}",
}
_LEVEL_GLYPH = {
    ActivityLevel.INFO: "•",
    ActivityLevel.SUCCESS: "✓",
    ActivityLevel.WARNING: "⚠",
    ActivityLevel.ERROR: "✗",
}


def _repo_key_from_url(url: str) -> str:
    """Derive an "org/repo" key from a clone URL (best effort)."""
    cleaned = url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    # Strip scheme / host, keep the last two path segments.
    tail = cleaned.replace(":", "/").split("/")
    parts = [p for p in tail if p][-2:]
    return "/".join(parts) if parts else cleaned


def _pr_label(pr_url: str) -> str:
    """Short label for a PR URL, e.g. '#42' from '.../pull/42'."""
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{tail}" if tail.isdigit() else "↗"


def _record_outcome_cell(r) -> Text:
    """Outcome-column text for a restored ledger record."""
    if r.action is TriageAction.TRANSITIONED:
        if r.pr_url:
            return Text(f"PR {_pr_label(r.pr_url)} ↗", style=f"bold {_C_FEATURE}")
        return Text("In Progress ✓", style=f"bold {_C_DOC}")
    if r.stage is EscalationStage.WORK or r.question:
        return Text("Needs input ⌨", style=f"bold {_C_UNKNOWN}")
    return Text("Review ⚠", style=f"bold {_C_UNKNOWN}")


class _TicketsTable(DataTable):
    """Tickets table that re-stretches its columns whenever it is resized.

    The table's own ``Resize`` is the only signal that fires *after* its region
    has been re-laid out, so ``content_size`` is already current here (unlike the
    app-level resize, which runs before children reflow).
    """

    def on_resize(self, event) -> None:  # noqa: ANN001 - textual events.Resize
        fit = getattr(self.app, "_fit_ticket_columns", None)
        if fit is not None:
            fit()


class JirayaApp(App):
    """Real-time triage dashboard."""

    TITLE = "jiraya"
    SUB_TITLE = "agent-powered Jira triage"

    CSS = """
    Screen { layers: base; }
    #metrics {
        height: 3;
        padding: 0 1;
        content-align: left middle;
        background: $panel;
        border: round $primary;
    }
    #body { height: 1fr; }
    #tickets {
        width: 3fr;
        border: round $primary;
    }
    #side { width: 2fr; }
    #activity {
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
    }
    #inbox {
        height: 1fr;
        border: round $warning;
    }
    .panel-title { text-style: bold; }

    /* Selected-row highlight: a muted dark band (instead of the default bright
       accent) so the coloured cell text keeps the same contrast it has on the
       normal dark rows. */
    DataTable > .datatable--cursor {
        background: #282c34;
        color: #ffffff;
        text-style: bold;
    }
    DataTable:focus > .datatable--cursor {
        background: #343a47;
        color: #ffffff;
        text-style: bold;
    }
    DataTable > .datatable--hover {
        background: #23262e;
    }
    """

    BINDINGS = [
        ("p", "poll", "Poll now"),
        ("g", "generate", "New ticket"),
        ("d", "detail", "Detail / respond"),
        ("w", "followup", "Prompt agent"),
        ("r", "resolve", "Resolve inbox"),
        ("x", "forget", "Forget ticket"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        system: JirayaSystem | None = None,
        *,
        config: JirayaConfig | None = None,
        poll_interval: float = 20.0,
    ) -> None:
        super().__init__()
        self._system = system or build_system(config or JirayaConfig())
        self.interval = poll_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poke = asyncio.Event()
        self._unsubscribe = None
        self._ticket_rows: set[str] = set()
        self._active_workers: set[str] = set()
        self._workspaces: dict[str, str] = {}  # ticket_key -> provisioned workspace
        self._inbox_entries: dict[str, InboxEntry] = {}
        self._cols: dict[str, object] = {}
        self._inbox_cols: dict[str, object] = {}
        self._fit_pending = False

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="metrics")
        with Horizontal(id="body"):
            yield _TicketsTable(id="tickets", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield RichLog(id="activity", markup=True, wrap=True, highlight=False)
                yield DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()

        tickets = self.query_one("#tickets", DataTable)
        cols = tickets.add_columns("Key", "Type", "Category", "Status", "Agent", "Repo", "Outcome")
        self._cols = dict(zip(["key", "type", "category", "status", "agent", "repo", "outcome"], cols))

        inbox = self.query_one("#inbox", DataTable)
        icols = inbox.add_columns("Ticket", "Category", "Agent", "Reason")
        self._inbox_cols = dict(zip(["ticket", "category", "agent", "reason"], icols))
        inbox.border_title = "Inbox — exceptions (d: detail/respond · r: resolve)"

        self.query_one("#activity", RichLog).border_title = "Agent activity"
        self._update_activity_header()
        tickets.border_title = "Tickets"

        self._render_metrics(self._system.service.metrics.snapshot())
        mode = "real Jira" if self._system.source_mode == "jira" else "in-memory demo"
        if self._system.dry_run:
            mode += " (dry-run: no writes)"
        self.sub_title = f"triage · {mode}"
        self._log_line(f"jiraya dashboard started — source: {mode}.", ActivityLevel.INFO)

        self._rehydrate()
        self._unsubscribe = self._system.bus.subscribe(self._on_event)
        self.run_worker(self._poll_loop(), name="poller", exclusive=False)
        self._schedule_fit()

    def _rehydrate(self) -> None:
        """Restore previously-actioned tickets + open inbox items from the store."""
        records = self._system.ledger.records()
        for r in records:
            project = r.ticket_key.split("-", 1)[0]
            status = (TicketStatus.IN_PROGRESS
                      if r.action is TriageAction.TRANSITIONED
                      else TicketStatus.UNTRIAGED)
            self._ensure_row(r.ticket_key, project, status, "")
            self._update(r.ticket_key, "category",
                         Text(str(r.category), style=_CATEGORY_STYLE[r.category]))
            if r.agent:
                self._update(r.ticket_key, "agent", r.agent)
            if r.repo:
                self._update(r.ticket_key, "repo", Text(r.repo, style=_C_FEATURE))
            if r.workspace:
                self._workspaces[r.ticket_key] = r.workspace
            self._update(r.ticket_key, "outcome", _record_outcome_cell(r))
        for entry in self._system.inbox.open_entries():
            self._add_inbox_row(entry)
        if records:
            self._log_line(
                f"Restored {len(records)} actioned ticket(s) and "
                f"{len(self._inbox_entries)} open inbox item(s) from state.",
                ActivityLevel.INFO)
        self._update_activity_header()

    def on_unmount(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()

    # -- background polling ---------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._system.poller.run_once()
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the UI
                self._log_line(f"poll cycle failed: {exc}", ActivityLevel.ERROR)
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # -- event marshalling ----------------------------------------------------

    def _on_event(self, event: DomainEvent) -> None:
        """Called from any thread; hop onto the UI loop before touching widgets."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._apply_event, event)

    def _apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, TicketsFetched):
            for ticket in event.tickets:
                self._ensure_row(ticket.key, ticket.project, ticket.status,
                                 ticket.issue_type)
            if event.count:
                self._log_line(f"Fetched {event.count} untriaged ticket(s).",
                               ActivityLevel.INFO)
        elif isinstance(event, TicketClassified):
            t, c = event.ticket, event.classification
            if t is not None and c is not None:
                self._ensure_row(t.key, t.project, t.status, t.issue_type)
                self._update(t.key, "category",
                             Text(str(c.category), style=_CATEGORY_STYLE[c.category]))
        elif isinstance(event, TicketRepoResolved):
            res = event.resolution
            if res is not None and res.repo is not None:
                style = _C_FEATURE if res.is_confident else _C_UNKNOWN
                self._update(event.ticket_key, "repo", Text(res.repo.key, style=style))
        elif isinstance(event, TicketRouted):
            self._update(event.ticket_key, "agent", event.agent)
        elif isinstance(event, TicketTransitioned):
            self._set_status(event.ticket_key, event.to_status or TicketStatus.IN_PROGRESS)
            self._update(event.ticket_key, "outcome", Text("In Progress ✓", style=f"bold {_C_DOC}"))
            # A worker agent is now engaged on this In-Progress ticket.
            self._active_workers.add(event.ticket_key)
            self._update_activity_header()
        elif isinstance(event, TicketWorkStarted):
            r = event.result
            if r is not None and r.opened_pr:
                self._update(event.ticket_key, "outcome",
                             Text(f"PR {_pr_label(r.pr_url)} ↗", style=f"bold {_C_FEATURE}"))
                # The worker delivered a PR — its task is done.
                self._discard_worker(event.ticket_key)
            elif r is not None and r.needs_input:
                self._update(event.ticket_key, "outcome",
                             Text("Needs input ⌨", style=f"bold {_C_UNKNOWN}"))
        elif isinstance(event, TicketEscalated):
            entry = event.entry
            if entry is not None:
                # Escalation surfaces to the inbox without changing Jira status.
                label = ("Needs input ⌨" if entry.stage is EscalationStage.WORK
                         else "Review ⚠")
                self._update(entry.ticket_key, "outcome",
                             Text(label, style=f"bold {_C_UNKNOWN}"))
                self._add_inbox_row(entry)
                # A surfaced ticket is awaiting a human, not actively worked.
                self._discard_worker(entry.ticket_key)
        elif isinstance(event, ActivityLogged) and event.activity is not None:
            a = event.activity
            self._log_line(f"[b]{a.agent}[/b] · {a.ticket_key}: {a.message}", a.level)
        elif isinstance(event, MetricsUpdated) and event.metrics is not None:
            self._render_metrics(event.metrics)
        elif isinstance(event, PollCycleStarted):
            self._log_line(f"— poll cycle #{event.cycle} —", ActivityLevel.INFO)
        elif isinstance(event, TicketTriaged) and event.outcome is not None:
            o = event.outcome
            if o.workspace:
                self._workspaces[o.ticket_key] = o.workspace
            if o.action is TriageAction.ESCALATED:
                self._update(o.ticket_key, "outcome", Text("Review ⚠", style=f"bold {_C_UNKNOWN}"))
        elif isinstance(event, TicketForgotten):
            self._forget_ticket_rows(event.ticket_key)

    # -- widget helpers -------------------------------------------------------

    def _ensure_row(
        self, key: str, project: str, status: TicketStatus, issue_type: str = ""
    ) -> None:
        if key in self._ticket_rows:
            return
        table = self.query_one("#tickets", DataTable)
        table.add_row(
            Text(key, style="bold"),
            issue_type or "—",
            Text("…", style=_C_DIM),
            Text(str(status), style=_STATUS_STYLE.get(status, "white")),
            "—",
            Text("…", style=_C_DIM),
            Text("queued", style=_C_DIM),
            key=key,
        )
        self._ticket_rows.add(key)
        self._schedule_fit()

    def _set_status(self, key: str, status: TicketStatus) -> None:
        self._update(key, "status",
                     Text(str(status), style=_STATUS_STYLE.get(status, "white")))

    def _update(self, row_key: str, column: str, value) -> None:
        if row_key not in self._ticket_rows:
            self._ensure_row(row_key, row_key.split("-", 1)[0], TicketStatus.TODO)
        table = self.query_one("#tickets", DataTable)
        try:
            table.update_cell(row_key, self._cols[column], value)
        except Exception:  # noqa: BLE001 - row may have been removed
            pass
        self._schedule_fit()

    def _schedule_fit(self) -> None:
        """Coalesce column re-fitting to once per refresh cycle."""
        if self._fit_pending:
            return
        self._fit_pending = True
        self.call_after_refresh(self._fit_ticket_columns)

    def _fit_ticket_columns(self) -> None:
        """Grow the Tickets columns so they fill all available horizontal space.

        Textual's DataTable sizes each column to its content, leaving the panel
        looking cramped with empty space on the right. There is no built-in
        "flex" for columns, so we distribute the leftover width across them by
        pinning each column's width (``content_width`` plus an even share of the
        slack). When the natural content is already wider than the panel we fall
        back to content sizing so the table can scroll instead of truncating.
        """
        self._fit_pending = False
        try:
            table = self.query_one("#tickets", DataTable)
        except Exception:  # noqa: BLE001 - not mounted yet / torn down
            return
        columns = table.ordered_columns
        if not columns:
            return
        available = table.content_size.width - table._row_label_column_width
        if table.show_vertical_scrollbar:
            available -= table.scrollbar_size_vertical
        if available <= 0:  # not laid out yet; a later pass will fit it
            return
        padding = 2 * table.cell_padding
        natural = [c.content_width for c in columns]
        natural_total = sum(width + padding for width in natural)
        if available <= natural_total:
            # No room to stretch — let the table size to content (and scroll).
            if any(not c.auto_width for c in columns):
                for column in columns:
                    column.auto_width = True
                table._update_dimensions([])
            return
        base, extra = divmod(available - natural_total, len(columns))
        for index, column in enumerate(columns):
            column.width = natural[index] + base + (1 if index < extra else 0)
            column.auto_width = False
        table._update_dimensions([])

    def _add_inbox_row(self, entry) -> None:
        self._inbox_entries[entry.id] = entry
        table = self.query_one("#inbox", DataTable)
        agent = entry.agent or "—"
        table.add_row(
            Text(entry.ticket_key, style="bold"),
            Text(str(entry.category), style=_CATEGORY_STYLE.get(entry.category, "white")),
            agent,
            entry.reason,
            key=entry.id,
        )

    def _log_line(self, markup: str, level: ActivityLevel) -> None:
        log = self.query_one("#activity", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        glyph = _LEVEL_GLYPH[level]
        style = _LEVEL_STYLE[level]
        log.write(f"[grey62]{ts}[/] [{style}]{glyph}[/] {markup}")

    def _discard_worker(self, ticket_key: str) -> None:
        if ticket_key in self._active_workers:
            self._active_workers.discard(ticket_key)
            self._update_activity_header()

    def _update_activity_header(self) -> None:
        """Show the live count of active worker agents on the activity panel."""
        n = len(self._active_workers)
        plural = "" if n == 1 else "s"
        try:
            log = self.query_one("#activity", RichLog)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        log.border_title = f"Agent activity — {n} active worker{plural}"

    def _render_metrics(self, m: TriageMetrics) -> None:
        last = m.last_poll_at.astimezone().strftime("%H:%M:%S") if m.last_poll_at else "—"
        auto = f"{m.automation_rate * 100:.0f}%"
        open_inbox = len(self._system.inbox.open_entries())
        text = (
            f"[b]Processed[/] {m.processed}   "
            f"[green]✓ Transitioned[/] {m.transitioned}   "
            f"[yellow]⚠ Escalated[/] {m.escalated}   "
            f"[b]Automation[/] {auto}   "
            f"[b]Open inbox[/] {open_inbox}   "
            f"[b]Cycles[/] {m.poll_cycles}   "
            f"[grey62]Last poll[/] {last}"
        )
        self.query_one("#metrics", Static).update(text)

    # -- actions --------------------------------------------------------------

    def action_poll(self) -> None:
        self._poke.set()

    def _selected_ticket_key(self) -> str | None:
        table = self.query_one("#tickets", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:  # noqa: BLE001 - no valid cursor
            return None
        value = cell_key.row_key.value
        return str(value) if value is not None else None

    def action_followup(self) -> None:
        """Prompt the work agent to do further work in a ticket's workspace."""
        key = self._selected_ticket_key()
        if key is None:
            self._log_line("Select a ticket first (Tab to the Tickets table).",
                           ActivityLevel.INFO)
            return
        workspace = self._workspaces.get(key)
        if not workspace:
            self._log_line(
                f"[b]{key}[/b] has no provisioned workspace yet — it must be worked first.",
                ActivityLevel.WARNING)
            return
        self.push_screen(
            FollowupScreen(key, workspace, dry_run=self._system.dry_run),
            lambda instruction: self._on_followup(key, instruction),
        )

    def _on_followup(self, ticket_key: str, instruction: str | None) -> None:
        if not instruction:
            return
        self._active_workers.add(ticket_key)
        self._update_activity_header()
        self.run_worker(
            self._followup(ticket_key, instruction),
            name=f"followup-{ticket_key}", exclusive=False,
        )

    async def _followup(self, ticket_key: str, instruction: str) -> None:
        try:
            await asyncio.to_thread(
                self._system.service.run_followup, ticket_key, instruction
            )
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the UI
            self._log_line(f"Follow-up work failed: {exc}", ActivityLevel.ERROR)
        finally:
            self._discard_worker(ticket_key)
        self._render_metrics(self._system.service.metrics.snapshot())

    def action_generate(self) -> None:
        source = self._system.source
        if isinstance(source, InMemoryTicketSource):
            ticket = random_ticket()
            source.add(ticket)
            self._log_line(f"Injected demo ticket [b]{ticket.key}[/].", ActivityLevel.INFO)
            self._poke.set()
        else:
            self._log_line("Ticket injection only available with the in-memory source.",
                           ActivityLevel.WARNING)

    def _selected_inbox_id(self) -> str | None:
        table = self.query_one("#inbox", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:  # noqa: BLE001 - no valid cursor
            return None
        value = cell_key.row_key.value
        return str(value) if value is not None else None

    def _remove_inbox_row(self, entry_id: str) -> None:
        table = self.query_one("#inbox", DataTable)
        try:
            table.remove_row(entry_id)
        except Exception:  # noqa: BLE001 - already gone
            pass
        self._inbox_entries.pop(entry_id, None)

    def _forget_ticket_rows(self, ticket_key: str) -> None:
        """Remove a forgotten ticket and its inbox items from the dashboard."""
        if ticket_key in self._ticket_rows:
            table = self.query_one("#tickets", DataTable)
            try:
                table.remove_row(ticket_key)
            except Exception:  # noqa: BLE001 - already gone
                pass
            self._ticket_rows.discard(ticket_key)
        for entry_id in [
            eid for eid, e in self._inbox_entries.items()
            if e.ticket_key == ticket_key
        ]:
            self._remove_inbox_row(entry_id)
        self._workspaces.pop(ticket_key, None)
        self._discard_worker(ticket_key)
        self._render_metrics(self._system.service.metrics.snapshot())
        self._schedule_fit()

    def action_resolve(self) -> None:
        entry_id = self._selected_inbox_id()
        if entry_id is None:
            return
        resolved = self._system.inbox.resolve(entry_id, "Resolved via dashboard")
        if resolved is not None and resolved.status is InboxStatus.RESOLVED:
            self._remove_inbox_row(entry_id)
            self._log_line(f"Resolved inbox item for [b]{resolved.ticket_key}[/].",
                           ActivityLevel.SUCCESS)
            self._render_metrics(self._system.service.metrics.snapshot())

    def action_forget(self) -> None:
        """Forget the selected ticket: remove it from the ledger + inbox so it
        drops off the dashboard and can be re-triaged on the next poll."""
        key = self._selected_ticket_key()
        if key is None:
            self._log_line("Select a ticket first (Tab to the Tickets table).",
                           ActivityLevel.INFO)
            return
        self.push_screen(
            ConfirmScreen(
                f"Forget {key}?",
                f"This removes [b]{key}[/b] from the durable ledger and clears any "
                "inbox items for it. The ticket will be re-triaged on the next poll "
                "if it is still untriaged in Jira.",
                confirm_label="Forget",
                confirm_variant="error",
            ),
            lambda ok: self._on_forget(key, ok),
        )

    def _on_forget(self, ticket_key: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        removed = self._system.service.forget_ticket(ticket_key)
        if removed:
            self._log_line(f"Forgot [b]{ticket_key}[/]; eligible for re-triage.",
                           ActivityLevel.SUCCESS)
        else:
            self._log_line(f"Nothing to forget for [b]{ticket_key}[/].",
                           ActivityLevel.INFO)

    def action_detail(self) -> None:
        """Open the expandable detail + respond modal for the selected entry."""
        entry_id = self._selected_inbox_id()
        if entry_id is None:
            self._log_line("No inbox item selected.", ActivityLevel.INFO)
            return
        entry = self._inbox_entries.get(entry_id) or self._system.inbox.get(entry_id)
        if entry is None:
            return
        self.push_screen(
            InboxDetailScreen(entry, dry_run=self._system.dry_run),
            lambda result: self._on_detail_result(entry_id, result),
        )

    def _on_detail_result(self, entry_id: str, result: dict | None) -> None:
        if not result:
            return
        action = result.get("action")
        note = result.get("note", "")
        if action == "resolve":
            resolved = self._system.inbox.resolve(entry_id, note or "Resolved via dashboard")
            if resolved is not None:
                self._remove_inbox_row(entry_id)
                self._log_line(f"Resolved inbox item for [b]{resolved.ticket_key}[/].",
                               ActivityLevel.SUCCESS)
                self._render_metrics(self._system.service.metrics.snapshot())
            return
        flags = {
            "comment": (True, False),
            "rerun": (False, True),
            "both": (True, True),
        }.get(action)
        if flags is None:
            return
        post_comment, rerun = flags

        repo: RepoRef | None = None
        repo_url = (result.get("repo_url") or "").strip()
        if repo_url:
            repo = RepoRef(
                key=_repo_key_from_url(repo_url),
                clone_url=repo_url,
                path=(result.get("repo_path") or "").strip(),
            )

        if post_comment and not note and repo is None:
            self._log_line("Enter a note to post as a comment.", ActivityLevel.WARNING)
            return
        if not (note or repo or rerun):
            return
        self.run_worker(
            self._respond(entry_id, note, repo, post_comment, rerun),
            name=f"respond-{entry_id}",
            exclusive=False,
        )

    async def _respond(
        self,
        entry_id: str,
        note: str,
        repo: RepoRef | None,
        post_comment: bool,
        rerun: bool,
    ) -> None:
        try:
            response = await asyncio.to_thread(
                lambda: self._system.service.respond_to_inbox(
                    entry_id, note, repo=repo, post_comment=post_comment, rerun=rerun,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the UI
            self._log_line(f"Respond failed: {exc}", ActivityLevel.ERROR)
            return
        # Re-running resolves the original entry; drop its (now stale) row. Any
        # fresh escalation has already been added via the event stream.
        if response.retriaged:
            self._remove_inbox_row(entry_id)
        self._render_metrics(self._system.service.metrics.snapshot())


def run(config: JirayaConfig | None = None, *, poll_interval: float = 20.0) -> None:
    """Launch the dashboard (blocking)."""
    JirayaApp(config=config, poll_interval=poll_interval).run()
