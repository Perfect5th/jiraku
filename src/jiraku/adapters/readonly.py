"""A read-only wrapper that makes any ``TicketSource`` non-mutating.

Used for ``--dry-run``: reads (``fetch_untriaged`` / ``get``) pass straight
through to the wrapped source, but writes (``transition`` and ``add_comment``)
are intercepted — they never reach the underlying API. Instead the wrapper
returns what it *would* have produced and notifies optional observers so the
intended change can be logged. This lets jiraku be pointed at a real, live Jira
to prove that real items flow through triage, without ever altering the board.
"""

from __future__ import annotations

from typing import Callable

from ..domain import Ticket, TicketStatus
from ..ports import TicketSource

TransitionObserver = Callable[[str, TicketStatus], None]
CommentObserver = Callable[[str, str], None]


class ReadOnlyTicketSource(TicketSource):
    """Decorator that suppresses writes to the wrapped ticket source."""

    def __init__(
        self,
        inner: TicketSource,
        *,
        on_transition: TransitionObserver | None = None,
        on_comment: CommentObserver | None = None,
    ) -> None:
        self._inner = inner
        self._on_transition = on_transition
        self._on_comment = on_comment

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

    def add_comment(self, key: str, body: str) -> str:
        if self._on_comment is not None:
            self._on_comment(key, body)
        # No comment is actually posted in dry-run mode.
        return ""
