"""Keyword / code-token resolver — deterministic residual matcher.

Scores the ticket's text against each catalog repo's keywords and code-tokens
(repo-name fragments, module/path-like tokens). Deterministic and dependency
free, analogous to the keyword classifier; used for tickets the project
registry can't place.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain import Classification, RepoResolution, Ticket
from ...ports import RepoResolver
from .catalog import RepoCatalogEntry, load_catalog

# Code-ish tokens: dotted/slashed paths, snake/kebab identifiers, repo names.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{2,}")


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        out.add(tok)
        # also index the last path/repo segment, e.g. "acme/web" -> "web"
        for part in re.split(r"[./-]", tok):
            if len(part) >= 3:
                out.add(part)
    return out


class KeywordRepoResolver(RepoResolver):
    """Matches ticket code-tokens/keywords against the repo catalog."""

    source_name = "keyword"

    def __init__(
        self,
        catalog: list[RepoCatalogEntry] | None = None,
        *,
        path: str | Path | None = None,
    ) -> None:
        self._catalog = catalog if catalog is not None else load_catalog(path)

    def resolve(
        self,
        ticket: Ticket,
        classification: Classification,
        hint: str | None = None,
    ) -> RepoResolution:
        text = f"{ticket.summary}\n{ticket.description}\n{hint or ''}"
        tokens = _tokens(text)

        best: RepoCatalogEntry | None = None
        best_score = 0
        best_hits: list[str] = []
        for entry in self._catalog:
            hits = self._match(entry, text, tokens)
            if len(hits) > best_score:
                best, best_score, best_hits = entry, len(hits), hits

        if best is None or best_score == 0:
            return RepoResolution.unresolved(
                "No repo keyword/code-token matched the ticket.",
                source=self.source_name,
            )
        confidence = round(min(0.85, 0.55 + 0.12 * best_score), 2)
        return RepoResolution(
            repo=best.ref(),
            confidence=confidence,
            rationale=f"Matched {', '.join(best_hits[:4])} -> {best.key}.",
            source=self.source_name,
        )

    @staticmethod
    def _match(entry: RepoCatalogEntry, text: str, tokens: set[str]) -> list[str]:
        hits: list[str] = []
        low = text.lower()
        for kw in entry.keywords:
            if kw in low:  # multi-word keywords match as substrings
                hits.append(kw)
        # repo-name fragments (e.g. "web" from "acme/web")
        for part in re.split(r"[./-]", entry.key.lower()):
            if len(part) >= 3 and part in tokens:
                hits.append(part)
        return hits
