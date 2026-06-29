"""Composite resolver — layer learned rules, then registry, then code-tokens.

Mirrors the layered strategy in the brief: the empirical registry and learned
rules give high-confidence hits first; the deterministic keyword/code-token
matcher (and, later, code-search + LLM) handle the residual. The first
*confident* resolution wins; otherwise the highest-confidence guess is returned
so it can still be surfaced to the inbox for a human to correct.
"""

from __future__ import annotations

from ...domain import Classification, RepoResolution, Ticket
from ...ports import RepoResolver


class CompositeRepoResolver(RepoResolver):
    """Tries each resolver in priority order and returns the best result."""

    source_name = "composite"

    def __init__(self, resolvers: list[RepoResolver]) -> None:
        self._resolvers = list(resolvers)

    def resolve(
        self,
        ticket: Ticket,
        classification: Classification,
        hint: str | None = None,
    ) -> RepoResolution:
        best = RepoResolution.unresolved("No resolver produced a match.")
        for resolver in self._resolvers:
            result = resolver.resolve(ticket, classification, hint)
            if result.is_confident:
                return result
            if result.confidence > best.confidence:
                best = result
        return best
