"""Registry resolver — authoritative project→repo mapping from the catalog."""

from __future__ import annotations

from pathlib import Path

from ...domain import Classification, RepoResolution, Ticket
from ...ports import RepoResolver
from .catalog import RepoCatalogEntry, load_catalog


class RegistryRepoResolver(RepoResolver):
    """Resolves a ticket to a repo by its Jira project key (high confidence).

    This is the empirical mapping (one row per project) that
    dev-status/commit-mining produces on day one.
    """

    source_name = "registry"

    def __init__(
        self,
        catalog: list[RepoCatalogEntry] | None = None,
        *,
        path: str | Path | None = None,
    ) -> None:
        self._catalog = catalog if catalog is not None else load_catalog(path)
        self._by_project: dict[str, RepoCatalogEntry] = {}
        for entry in self._catalog:
            for project in entry.projects:
                self._by_project.setdefault(project.upper(), entry)

    def resolve(
        self,
        ticket: Ticket,
        classification: Classification,
        hint: str | None = None,
    ) -> RepoResolution:
        for project in (ticket.project, classification.target_project):
            entry = self._by_project.get((project or "").upper())
            if entry is not None:
                return RepoResolution(
                    repo=entry.ref(),
                    confidence=0.95,
                    rationale=f"Project '{project}' maps to {entry.key} in the registry.",
                    source=self.source_name,
                )
        return RepoResolution.unresolved(
            f"No registry entry for project '{ticket.project}'.",
            source=self.source_name,
        )
