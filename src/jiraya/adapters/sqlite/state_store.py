"""SQLite-backed state store: durable inbox + actioned-ticket ledger.

One file holds both the exception inbox and the ledger of every actioned ticket,
so the dashboard survives restarts (open exceptions can still be answered, and
previously-actioned tickets are remembered and not re-actioned).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ...domain import (
    EscalationStage,
    InboxEntry,
    InboxStatus,
    RepoRef,
    TicketCategory,
    TriageAction,
    TriageOutcome,
    TriageRecord,
)
from ...ports import InboxRepository, TriageLedger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    ticket_key TEXT NOT NULL,
    reason TEXT,
    category TEXT,
    confidence REAL,
    agent TEXT,
    rationale TEXT,
    details TEXT,
    stage TEXT,
    repo TEXT,
    workspace TEXT,
    branch TEXT,
    status TEXT,
    created_at TEXT,
    resolved_at TEXT,
    resolution TEXT,
    seq INTEGER
);
CREATE TABLE IF NOT EXISTS ledger (
    ticket_key TEXT PRIMARY KEY,
    action TEXT,
    category TEXT,
    agent TEXT,
    repo TEXT,
    workspace TEXT,
    pr_url TEXT,
    question TEXT,
    stage TEXT,
    note TEXT,
    at TEXT,
    seq INTEGER
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(value) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _repo_to_json(repo: RepoRef | None) -> str | None:
    if repo is None:
        return None
    return json.dumps({
        "key": repo.key, "clone_url": repo.clone_url,
        "path": repo.path, "default_branch": repo.default_branch,
    })


def _repo_from_json(value) -> RepoRef | None:
    if not value:
        return None
    d = json.loads(value)
    return RepoRef(key=d.get("key", ""), clone_url=d.get("clone_url", ""),
                   path=d.get("path", ""), default_branch=d.get("default_branch", ""))


class SqliteStateStore(InboxRepository, TriageLedger):
    """Persists the inbox and the actioned-ticket ledger to a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.parent and str(self._path.parent) not in ("", "."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._seq = 0
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT MAX(s) AS m FROM ("
                "  SELECT MAX(seq) AS s FROM inbox UNION ALL SELECT MAX(seq) FROM ledger)"
            ).fetchone()
            self._seq = (row["m"] or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- InboxRepository ------------------------------------------------------

    def add(self, entry: InboxEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO inbox VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id, entry.ticket_key, entry.reason,
                    entry.category.value, entry.confidence, entry.agent,
                    entry.rationale, json.dumps(list(entry.details)),
                    entry.stage.value, _repo_to_json(entry.repo),
                    entry.workspace, entry.branch, entry.status.value,
                    _iso(entry.created_at), _iso(entry.resolved_at),
                    entry.resolution, self._next_seq(),
                ),
            )
            self._conn.commit()

    def get(self, entry_id: str) -> InboxEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM inbox WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def all(self) -> list[InboxEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM inbox ORDER BY seq DESC").fetchall()
        return [_row_to_entry(r) for r in rows]

    def open_entries(self) -> list[InboxEntry]:
        return [e for e in self.all() if e.status is InboxStatus.OPEN]

    def resolve(self, entry_id: str, resolution: str) -> InboxEntry | None:
        entry = self.get(entry_id)
        if entry is None:
            return None
        resolved = entry.resolved(resolution)
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET status = ?, resolution = ?, resolved_at = ? "
                "WHERE id = ?",
                (resolved.status.value, resolved.resolution,
                 _iso(resolved.resolved_at), entry_id),
            )
            self._conn.commit()
        return resolved

    # -- TriageLedger ---------------------------------------------------------

    def record(self, outcome: TriageOutcome) -> None:
        rec = TriageRecord.from_outcome(outcome)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.ticket_key, rec.action.value, rec.category.value,
                    rec.agent, rec.repo, rec.workspace, rec.pr_url, rec.question,
                    (rec.stage.value if rec.stage else None), rec.note,
                    _iso(rec.at), self._next_seq(),
                ),
            )
            self._conn.commit()

    def actioned_keys(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT ticket_key FROM ledger").fetchall()
        return {r["ticket_key"] for r in rows}

    def records(self) -> list[TriageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ledger ORDER BY seq ASC").fetchall()
        return [_row_to_record(r) for r in rows]


def _row_to_entry(row: sqlite3.Row) -> InboxEntry:
    return InboxEntry(
        id=row["id"], ticket_key=row["ticket_key"], reason=row["reason"] or "",
        category=TicketCategory(row["category"]), confidence=row["confidence"] or 0.0,
        agent=row["agent"], rationale=row["rationale"] or "",
        details=tuple(json.loads(row["details"] or "[]")),
        stage=EscalationStage(row["stage"]), repo=_repo_from_json(row["repo"]),
        workspace=row["workspace"] or "", branch=row["branch"] or "",
        status=InboxStatus(row["status"]),
        created_at=_dt(row["created_at"]), resolved_at=_dt(row["resolved_at"]),
        resolution=row["resolution"] or "",
    )


def _row_to_record(row: sqlite3.Row) -> TriageRecord:
    return TriageRecord(
        ticket_key=row["ticket_key"], action=TriageAction(row["action"]),
        category=TicketCategory(row["category"]), agent=row["agent"] or "",
        repo=row["repo"] or "", workspace=row["workspace"] or "",
        pr_url=row["pr_url"] or "", question=row["question"] or "",
        stage=EscalationStage(row["stage"]) if row["stage"] else None,
        note=row["note"] or "", at=_dt(row["at"]),
    )
