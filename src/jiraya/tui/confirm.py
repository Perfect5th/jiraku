"""A small yes/no confirmation modal used for destructive actions."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmScreen(ModalScreen[bool]):
    """Ask the user to confirm a destructive action.

    Dismisses with ``True`` if confirmed, ``False`` otherwise.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        border: thick $warning;
        background: $surface;
    }
    #dialog .heading { text-style: bold; color: $warning; }
    #body { height: auto; margin: 1 0; }
    #buttons { height: auto; align: center middle; }
    #buttons Button { margin: 0 1; }
    """

    def __init__(
        self,
        heading: str,
        body: str,
        *,
        confirm_label: str = "Confirm",
        confirm_variant: str = "error",
    ) -> None:
        super().__init__()
        self._heading = heading
        self._body = body
        self._confirm_label = confirm_label
        self._confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self._heading, style="bold"), classes="heading")
            yield Static(self._body, id="body")
            with Horizontal(id="buttons"):
                yield Button(self._confirm_label, id="confirm",
                             variant=self._confirm_variant)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
