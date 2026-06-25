"""A read-only wrapper that makes any ``TicketSource`` non-mutating.

Used for ``--dry-run``: reads (``fetch_untriaged`` / ``get``) pass straight
through to the wrapped source, but ``transition`` is intercepted — it never
calls the underlying API. Instead it returns the ticket it *would* have
produced and notifies an optional observer so the intended change can be
logged. This lets jiraya be pointed at a real, live Jira to prove that real
items flow through triage, without ever altering the production board.
"""

from __future__ import annotations

from typing import Callable

from ..domain import Ticket, TicketStatus
from ..ports import TicketSource

TransitionObserver = Callable[[str, TicketStatus], None]


class ReadOnlyTicketSource(TicketSource):
    """Decorator that suppresses writes to the wrapped ticket source."""

    def __init__(
        self, inner: TicketSource, *, on_transition: TransitionObserver | None = None
    ) -> None:
        self._inner = inner
        self._on_transition = on_transition

    @property
    def dry_run(self) -> bool:
        return True

    def fetch_untriaged(self) -> list[Ticket]:
        return self._inner.fetch_untriaged()

    def get(self, key: str) -> Ticket | None:
        return self._inner.get(key)

    def transition(self, key: str, status: TicketStatus) -> Ticket:
        if self._on_transition is not None:
            self._on_transition(key, status)
        current = self._inner.get(key)
        if current is None:
            raise KeyError(f"Unknown ticket: {key}")
        # Return the would-be result without persisting it anywhere.
        return current.with_status(status)
