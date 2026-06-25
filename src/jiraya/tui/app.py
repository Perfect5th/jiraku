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
    InboxStatus,
    MetricsUpdated,
    PollCycleStarted,
    TicketCategory,
    TicketClassified,
    TicketEscalated,
    TicketRouted,
    TicketStatus,
    TicketTransitioned,
    TicketTriaged,
    TicketsFetched,
    TriageAction,
    TriageMetrics,
)
from ..adapters.inmemory import InMemoryTicketSource, random_ticket

_CATEGORY_STYLE = {
    TicketCategory.BUG: "bold red",
    TicketCategory.FEATURE_REQUEST: "bold cyan",
    TicketCategory.DOCUMENTATION: "bold green",
    TicketCategory.UNKNOWN: "bold yellow",
}
_STATUS_STYLE = {
    TicketStatus.UNTRIAGED: "grey62",
    TicketStatus.TODO: "white",
    TicketStatus.IN_PROGRESS: "bold green",
    TicketStatus.NEEDS_REVIEW: "bold yellow",
    TicketStatus.DONE: "blue",
}
_LEVEL_STYLE = {
    ActivityLevel.INFO: "grey70",
    ActivityLevel.SUCCESS: "green",
    ActivityLevel.WARNING: "yellow",
    ActivityLevel.ERROR: "bold red",
}
_LEVEL_GLYPH = {
    ActivityLevel.INFO: "•",
    ActivityLevel.SUCCESS: "✓",
    ActivityLevel.WARNING: "⚠",
    ActivityLevel.ERROR: "✗",
}


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
    """

    BINDINGS = [
        ("p", "poll", "Poll now"),
        ("g", "generate", "New ticket"),
        ("r", "resolve", "Resolve inbox"),
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
        self._cols: dict[str, object] = {}
        self._inbox_cols: dict[str, object] = {}

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="metrics")
        with Horizontal(id="body"):
            yield DataTable(id="tickets", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield RichLog(id="activity", markup=True, wrap=True, highlight=False)
                yield DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()

        tickets = self.query_one("#tickets", DataTable)
        cols = tickets.add_columns("Key", "Project", "Type", "Category", "Status", "Agent", "Outcome")
        self._cols = dict(zip(["key", "project", "type", "category", "status", "agent", "outcome"], cols))

        inbox = self.query_one("#inbox", DataTable)
        icols = inbox.add_columns("Ticket", "Category", "Reason")
        self._inbox_cols = dict(zip(["ticket", "category", "reason"], icols))
        inbox.border_title = "Inbox — exceptions for human review"

        self.query_one("#activity", RichLog).border_title = "Agent activity"
        tickets.border_title = "Tickets"

        self._render_metrics(self._system.service.metrics.snapshot())
        mode = "real Jira" if self._system.source_mode == "jira" else "in-memory demo"
        if self._system.dry_run:
            mode += " (dry-run: no writes)"
        self.sub_title = f"triage · {mode}"
        self._log_line(f"jiraya dashboard started — source: {mode}.", ActivityLevel.INFO)

        self._unsubscribe = self._system.bus.subscribe(self._on_event)
        self.run_worker(self._poll_loop(), name="poller", exclusive=False)

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
        elif isinstance(event, TicketRouted):
            self._update(event.ticket_key, "agent", event.agent)
        elif isinstance(event, TicketTransitioned):
            self._set_status(event.ticket_key, event.to_status or TicketStatus.IN_PROGRESS)
            self._update(event.ticket_key, "outcome", Text("In Progress ✓", style="green"))
        elif isinstance(event, TicketEscalated):
            entry = event.entry
            if entry is not None:
                # Escalation surfaces to the inbox without changing Jira status.
                self._update(entry.ticket_key, "outcome", Text("Review ⚠", style="yellow"))
                self._add_inbox_row(entry)
        elif isinstance(event, ActivityLogged) and event.activity is not None:
            a = event.activity
            self._log_line(f"[b]{a.agent}[/b] · {a.ticket_key}: {a.message}", a.level)
        elif isinstance(event, MetricsUpdated) and event.metrics is not None:
            self._render_metrics(event.metrics)
        elif isinstance(event, PollCycleStarted):
            self._log_line(f"— poll cycle #{event.cycle} —", ActivityLevel.INFO)
        elif isinstance(event, TicketTriaged) and event.outcome is not None:
            o = event.outcome
            if o.action is TriageAction.ESCALATED:
                self._update(o.ticket_key, "outcome", Text("Review ⚠", style="yellow"))

    # -- widget helpers -------------------------------------------------------

    def _ensure_row(
        self, key: str, project: str, status: TicketStatus, issue_type: str = ""
    ) -> None:
        if key in self._ticket_rows:
            return
        table = self.query_one("#tickets", DataTable)
        table.add_row(
            Text(key, style="bold"),
            project,
            issue_type or "—",
            Text("…", style="grey50"),
            Text(str(status), style=_STATUS_STYLE.get(status, "white")),
            "—",
            Text("queued", style="grey50"),
            key=key,
        )
        self._ticket_rows.add(key)

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

    def _add_inbox_row(self, entry) -> None:
        table = self.query_one("#inbox", DataTable)
        table.add_row(
            Text(entry.ticket_key, style="bold"),
            Text(str(entry.category), style=_CATEGORY_STYLE.get(entry.category, "white")),
            entry.reason,
            key=entry.id,
        )

    def _log_line(self, markup: str, level: ActivityLevel) -> None:
        log = self.query_one("#activity", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        glyph = _LEVEL_GLYPH[level]
        style = _LEVEL_STYLE[level]
        log.write(f"[grey50]{ts}[/] [{style}]{glyph}[/] {markup}")

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

    def action_resolve(self) -> None:
        table = self.query_one("#inbox", DataTable)
        if table.row_count == 0:
            return
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        entry_id = cell_key.row_key.value
        if entry_id is None:
            return
        resolved = self._system.inbox.resolve(str(entry_id), "Resolved via dashboard")
        if resolved is not None and resolved.status is InboxStatus.RESOLVED:
            table.remove_row(cell_key.row_key)
            self._log_line(f"Resolved inbox item for [b]{resolved.ticket_key}[/].",
                           ActivityLevel.SUCCESS)
            self._render_metrics(self._system.service.metrics.snapshot())


def run(config: JirayaConfig | None = None, *, poll_interval: float = 20.0) -> None:
    """Launch the dashboard (blocking)."""
    JirayaApp(config=config, poll_interval=poll_interval).run()
