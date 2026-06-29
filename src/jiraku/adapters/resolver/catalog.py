"""The repo catalog: the empirical ticket→repo mapping the resolvers share.

The catalog is the data the registry resolver matches against. In production it
is best seeded from Jira **dev-status / commit-mining** (the issue→commit→repo
links Jira already records), then hand-curated; here it is loaded from a YAML
file (see ``jiraku/data/repo_registry.yaml``) or supplied programmatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ...domain import RepoRef


@dataclass(frozen=True, slots=True)
class RepoCatalogEntry:
    """One repository plus the signals that map tickets onto it."""

    key: str
    clone_url: str
    path: str = ""
    default_branch: str = ""
    projects: tuple[str, ...] = ()      # Jira project keys that live here
    keywords: tuple[str, ...] = ()      # prose signals (module/feature names)

    def ref(self) -> RepoRef:
        return RepoRef(
            key=self.key,
            clone_url=self.clone_url,
            path=self.path,
            default_branch=self.default_branch,
        )


def entry_from_dict(data: dict[str, Any]) -> RepoCatalogEntry:
    return RepoCatalogEntry(
        key=str(data["key"]),
        clone_url=str(data.get("clone_url", "")),
        path=str(data.get("path", "") or ""),
        default_branch=str(data.get("default_branch", "") or ""),
        projects=tuple(str(p).upper() for p in data.get("projects", []) or ()),
        keywords=tuple(str(k).lower() for k in data.get("keywords", []) or ()),
    )


def load_catalog(source: str | Path | list[dict] | None) -> list[RepoCatalogEntry]:
    """Load a catalog from a YAML file path, a list of dicts, or nothing."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            return []
        raw = yaml.safe_load(path.read_text()) or {}
        items = raw.get("repos", []) if isinstance(raw, dict) else raw
    else:
        items = source
    return [entry_from_dict(item) for item in items]


def default_catalog() -> list[RepoCatalogEntry]:
    """A small catalog so the in-memory demo resolves its seeded tickets."""
    return [
        RepoCatalogEntry(
            key="acme/proj-service", clone_url="https://github.com/acme/proj-service.git",
            projects=("PROJ",), keywords=("login", "checkout", "profile"),
        ),
        RepoCatalogEntry(
            key="acme/web", clone_url="https://github.com/acme/web.git",
            projects=("WEB",), keywords=("dark mode", "csv export", "report", "theme"),
        ),
        RepoCatalogEntry(
            key="acme/docs", clone_url="https://github.com/acme/docs.git",
            projects=("DOC",), keywords=("readme", "documentation", "guide", "tutorial"),
        ),
    ]
