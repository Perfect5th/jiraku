"""The expandable inbox detail + "respond" modal for the jiraya dashboard."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..domain import InboxEntry


class InboxDetailScreen(ModalScreen[dict | None]):
    """Shows the full detail of an inbox exception and collects a response.

    Dismisses with ``None`` on cancel, or a dict of the form
    ``{"action": "comment"|"rerun"|"both"|"resolve", "note": str}``.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    InboxDetailScreen {
        align: center middle;
    }
    #dialog {
        width: 78;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #dialog .heading { text-style: bold; color: $secondary; }
    #detail { height: auto; margin-bottom: 1; }
    #note { margin: 1 0; }
    #buttons { height: auto; align: center middle; }
    #buttons Button { margin: 0 1; }
    #dry-note { color: $warning; height: auto; }
    """

    def __init__(self, entry: InboxEntry, *, dry_run: bool = False) -> None:
        super().__init__()
        self._entry = entry
        self._dry_run = dry_run

    def compose(self) -> ComposeResult:
        e = self._entry
        with Vertical(id="dialog"):
            yield Static(Text(f"Inbox exception · {e.ticket_key}", style="bold"),
                         classes="heading")
            yield Static(self._detail_text(), id="detail")
            yield Label("Add a note (sent as a Jira comment and/or used as a triage hint):")
            yield Input(placeholder="e.g. This is actually a bug — repro: …", id="note")
            if self._dry_run:
                yield Static(
                    "Dry-run: comments are not posted to Jira; re-triage performs no writes.",
                    id="dry-note",
                )
            with Horizontal(id="buttons"):
                yield Button("Post comment", id="comment", variant="primary")
                yield Button("Re-run triage", id="rerun", variant="warning")
                yield Button("Comment + Re-run", id="both", variant="success")
                yield Button("Resolve", id="resolve")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#note", Input).focus()

    def _detail_text(self) -> Text:
        e = self._entry
        t = Text()
        t.append("Category   : ")
        t.append(f"{e.category}\n", style="bold")
        t.append(f"Confidence : {e.confidence:.0%}\n")
        t.append(f"Agent      : {e.agent or '—'}\n")
        t.append(f"Status     : {e.status}\n")
        t.append("Reason     : ")
        t.append(f"{e.reason}\n")
        if e.rationale:
            t.append("Rationale  : ")
            t.append(f"{e.rationale}\n", style="italic")
        if e.details:
            t.append("Details    :\n")
            for d in e.details:
                t.append(f"  • {d}\n", style="grey70")
        return t

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        note = self.query_one("#note", Input).value.strip()
        self.dismiss({"action": event.button.id, "note": note})

    def action_cancel(self) -> None:
        self.dismiss(None)
