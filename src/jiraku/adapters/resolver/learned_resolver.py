"""Learned-rules resolver — repo mappings taught by inbox corrections.

When a human responds to a "which repo?" exception and supplies a repo, the
harness teaches it here. Future tickets in the same project (or matching the
learned code-tokens) then resolve automatically — so inbox corrections steadily
improve precision, exactly as the spec's "new rules learned over time" intends.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from ...domain import Classification, RepoRef, RepoResolution, Ticket
from ...ports import LearnedRulesStore, RepoResolver


def _repo_to_dict(repo: RepoRef) -> dict:
    return {
        "key": repo.key,
        "clone_url": repo.clone_url,
        "path": repo.path,
        "default_branch": repo.default_branch,
    }


def _repo_from_dict(data: dict) -> RepoRef:
    return RepoRef(
        key=str(data.get("key", "")),
        clone_url=str(data.get("clone_url", "")),
        path=str(data.get("path", "") or ""),
        default_branch=str(data.get("default_branch", "") or ""),
    )


class InMemoryLearnedRulesStore(LearnedRulesStore):
    """Holds learned repo rules in process memory."""

    def __init__(self, rules: list[dict] | None = None) -> None:
        self._rules: list[dict] = list(rules or [])
        self._lock = threading.Lock()

    def learn(self, *, project: str, repo: RepoRef, tokens: tuple[str, ...] = ()) -> None:
        rule = {
            "project": (project or "").upper(),
            "tokens": [t.lower() for t in tokens],
            "repo": _repo_to_dict(repo),
        }
        with self._lock:
            # Replace any existing rule for the same project to keep the latest
            # human correction authoritative.
            self._rules = [
                r for r in self._rules
                if not (rule["project"] and r.get("project") == rule["project"])
            ]
            self._rules.append(rule)
            self._persist()

    def lookup(self, *, project: str, text: str) -> tuple[RepoRef, float, str] | None:
        project = (project or "").upper()
        low = text.lower()
        with self._lock:
            rules = list(self._rules)
        for rule in rules:
            if rule.get("project") and rule["project"] == project:
                return (
                    _repo_from_dict(rule["repo"]),
                    0.9,
                    f"Learned rule: project '{project}' -> {rule['repo'].get('key')}.",
                )
        for rule in rules:
            hits = [t for t in rule.get("tokens", []) if t and t in low]
            if hits:
                return (
                    _repo_from_dict(rule["repo"]),
                    0.78,
                    f"Learned rule matched token(s) {', '.join(hits[:3])}.",
                )
        return None

    def rules(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rules]

    def _persist(self) -> None:  # overridden by the file-backed store
        return None


class FileLearnedRulesStore(InMemoryLearnedRulesStore):
    """In-memory store that persists learned rules to a YAML file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        loaded: list[dict] = []
        if self._path.is_file():
            raw = yaml.safe_load(self._path.read_text()) or {}
            loaded = raw.get("rules", []) if isinstance(raw, dict) else (raw or [])
        super().__init__(rules=loaded)

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump({"rules": self._rules}, sort_keys=False))


class LearnedRulesRepoResolver(RepoResolver):
    """Resolves repos from rules taught via inbox corrections."""

    source_name = "learned"

    def __init__(self, store: LearnedRulesStore) -> None:
        self._store = store

    @property
    def store(self) -> LearnedRulesStore:
        return self._store

    def resolve(
        self,
        ticket: Ticket,
        classification: Classification,
        hint: str | None = None,
    ) -> RepoResolution:
        text = f"{ticket.summary}\n{ticket.description}\n{hint or ''}"
        match = self._store.lookup(project=ticket.project, text=text)
        if match is None:
            return RepoResolution.unresolved(
                "No learned rule matched the ticket.", source=self.source_name
            )
        repo, confidence, rationale = match
        return RepoResolution(
            repo=repo, confidence=confidence, rationale=rationale, source=self.source_name
        )
