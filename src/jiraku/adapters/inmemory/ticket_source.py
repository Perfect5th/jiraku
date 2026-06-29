"""In-memory ticket source — a fake Jira for offline runs, demos and tests."""

from __future__ import annotations

import threading

from ...domain import TRIAGEABLE_STATUSES, Ticket, TicketStatus
from ...ports import TicketSource
from .seed import sample_tickets


class InMemoryTicketSource(TicketSource):
    """Holds tickets in memory and applies status transitions in place.

    Because a transition moves a ticket out of the triageable set, the same
    ticket is never handed back on a later poll — mirroring how a real Jira
    JQL filter stops returning issues once their status changes.
    """

    def __init__(self, tickets: list[Ticket] | None = None) -> None:
        seed = tickets if tickets is not None else sample_tickets()
        self._tickets: dict[str, Ticket] = {t.key: t for t in seed}
        self._comments: dict[str, list[str]] = {}
        self._comment_seq = 0
        self._lock = threading.Lock()

    def fetch_untriaged(self) -> list[Ticket]:
        with self._lock:
            return [
                t for t in self._tickets.values()
                if t.status in TRIAGEABLE_STATUSES
            ]

    def transition(self, key: str, status: TicketStatus) -> Ticket:
        with self._lock:
            current = self._tickets.get(key)
            if current is None:
                raise KeyError(f"Unknown ticket: {key}")
            updated = current.with_status(status)
            self._tickets[key] = updated
            return updated

    def get(self, key: str) -> Ticket | None:
        with self._lock:
            return self._tickets.get(key)

    def add_comment(self, key: str, body: str) -> str:
        with self._lock:
            if key not in self._tickets:
                raise KeyError(f"Unknown ticket: {key}")
            self._comment_seq += 1
            comment_id = f"c{self._comment_seq}"
            self._comments.setdefault(key, []).append(body)
            return comment_id

    def comments(self, key: str) -> list[str]:
        with self._lock:
            return list(self._comments.get(key, ()))

    def add(self, ticket: Ticket) -> None:
        """Insert a new ticket (used to simulate fresh Jira activity live)."""
        with self._lock:
            self._tickets[ticket.key] = ticket

    def all(self) -> list[Ticket]:
        with self._lock:
            return list(self._tickets.values())
