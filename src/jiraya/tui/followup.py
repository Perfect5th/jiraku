"""Modal for prompting the work agent to do on-demand follow-up work."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class FollowupScreen(ModalScreen[str | None]):
    """Collects a freeform instruction for the agent in a ticket's workspace.

    Dismisses with the instruction string, or ``None`` on cancel.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    FollowupScreen { align: center middle; }
    #dialog {
        width: 78;
        height: auto;
        padding: 1 2;
        border: thick $secondary;
        background: $surface;
    }
    #dialog .heading { text-style: bold; color: $secondary; }
    #ws { color: $text-muted; height: auto; margin-bottom: 1; }
    #buttons { height: auto; align: center middle; }
    #buttons Button { margin: 0 1; }
    """

    def __init__(self, ticket_key: str, workspace: str, *, dry_run: bool = False) -> None:
        super().__init__()
        self._ticket_key = ticket_key
        self._workspace = workspace
        self._dry_run = dry_run

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(f"Follow-up work · {self._ticket_key}", style="bold"),
                         classes="heading")
            yield Static(
                Text(f"workspace: {self._workspace or '(provisioned on demand)'}",
                     style="italic"),
                id="ws",
            )
            yield Label("Instruction for the agent (e.g. action this PR feedback):")
            yield Input(placeholder="e.g. Address review: rename the flag and add a test",
                        id="instruction")
            if self._dry_run:
                yield Static("Dry-run: the agent performs no writes.", id="dry-note")
            with Horizontal(id="buttons"):
                yield Button("Run agent", id="run", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#instruction", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self.dismiss(self.query_one("#instruction", Input).value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
