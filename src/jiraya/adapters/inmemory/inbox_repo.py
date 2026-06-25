"""In-memory implementation of the inbox (exception) repository."""

from __future__ import annotations

import threading

from ...domain import InboxEntry, InboxStatus
from ...ports import InboxRepository


class InMemoryInboxRepository(InboxRepository):
    """Stores surfaced exceptions in process memory, newest first."""

    def __init__(self) -> None:
        self._entries: dict[str, InboxEntry] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def add(self, entry: InboxEntry) -> None:
        with self._lock:
            self._entries[entry.id] = entry
            self._order.append(entry.id)

    def get(self, entry_id: str) -> InboxEntry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def all(self) -> list[InboxEntry]:
        with self._lock:
            return [self._entries[i] for i in reversed(self._order)]

    def open_entries(self) -> list[InboxEntry]:
        return [e for e in self.all() if e.status is InboxStatus.OPEN]

    def resolve(self, entry_id: str, resolution: str) -> InboxEntry | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            resolved = entry.resolved(resolution)
            self._entries[entry_id] = resolved
            return resolved
