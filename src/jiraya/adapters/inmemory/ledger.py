"""In-memory triage ledger (in-session history; not persisted)."""

from __future__ import annotations

import threading

from ...domain import TriageOutcome, TriageRecord
from ...ports import TriageLedger


class InMemoryTriageLedger(TriageLedger):
    """Keeps actioned-ticket records in process memory (latest per ticket)."""

    def __init__(self) -> None:
        self._records: dict[str, TriageRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def record(self, outcome: TriageOutcome) -> None:
        rec = TriageRecord.from_outcome(outcome)
        with self._lock:
            if rec.ticket_key not in self._records:
                self._order.append(rec.ticket_key)
            self._records[rec.ticket_key] = rec

    def actioned_keys(self) -> set[str]:
        with self._lock:
            return set(self._records)

    def records(self) -> list[TriageRecord]:
        with self._lock:
            return [self._records[k] for k in self._order]

    def get_record(self, ticket_key: str) -> TriageRecord | None:
        with self._lock:
            return self._records.get(ticket_key)

    def forget(self, ticket_key: str) -> bool:
        with self._lock:
            if ticket_key not in self._records:
                return False
            del self._records[ticket_key]
            self._order.remove(ticket_key)
            return True
