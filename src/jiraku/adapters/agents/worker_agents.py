"""Specialized worker agents that validate and work each ticket category.

Each agent performs the "initial validation" described in the spec (is the bug
reproducible? is the feature a duplicate?) using deterministic heuristics, then
either declares the ticket actionable or asks for a human via ``needs_human``.
"""

from __future__ import annotations

from ...domain import (
    Classification,
    RepoResolution,
    Ticket,
    TicketCategory,
    ValidationResult,
)
from ...ports import WorkerAgent

_REPRO_SIGNALS = (
    "steps to reproduce", "reproduce", "repro", "expected", "actual",
    "stack trace", "traceback", "1.", "2.", "given", "when ", "then ",
)


class BugAgent(WorkerAgent):
    """Validates that a bug is reproducible before accepting it."""

    name = "bug-agent"

    def handles(self, category: TicketCategory) -> bool:
        return category is TicketCategory.BUG

    def validate(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None = None,
    ) -> ValidationResult:
        text = f"{ticket.summary}\n{ticket.description}".lower()
        has_repro = any(sig in text for sig in _REPRO_SIGNALS)
        detailed = len(ticket.description.strip()) >= 40
        if has_repro and detailed:
            return ValidationResult(
                actionable=True,
                summary="Reproduction steps present; bug looks actionable.",
            )
        missing: list[str] = []
        if not has_repro:
            missing.append("no reproduction steps")
        if not detailed:
            missing.append("description too sparse")
        return ValidationResult(
            actionable=False,
            needs_human=True,
            summary="Cannot confirm the bug is reproducible.",
            details=tuple(missing),
        )


class FeatureAgent(WorkerAgent):
    """Validates a feature request and screens for likely duplicates."""

    name = "feature-agent"

    def handles(self, category: TicketCategory) -> bool:
        return category is TicketCategory.FEATURE_REQUEST

    def validate(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None = None,
    ) -> ValidationResult:
        text = f"{ticket.summary}\n{ticket.description}".lower()
        labels = {label.lower() for label in ticket.labels}
        looks_duplicate = "duplicate" in labels or "duplicate" in text
        if looks_duplicate:
            return ValidationResult(
                actionable=False,
                needs_human=True,
                summary="Possible duplicate request; needs human confirmation.",
                details=("flagged as potential duplicate",),
            )
        return ValidationResult(
            actionable=True,
            summary="No duplicate detected; feature request accepted for scoping.",
        )


class DocumentationAgent(WorkerAgent):
    """Validates that a documentation request names a concrete target."""

    name = "docs-agent"

    _TARGETS = (
        "readme", "page", "section", "guide", "tutorial", "typo", "example",
        "http", ".md", "docstring", "changelog", "api reference",
    )

    def handles(self, category: TicketCategory) -> bool:
        return category is TicketCategory.DOCUMENTATION

    def validate(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None = None,
    ) -> ValidationResult:
        text = f"{ticket.summary}\n{ticket.description}".lower()
        has_target = any(t in text for t in self._TARGETS)
        if has_target and len(ticket.description.strip()) >= 25:
            return ValidationResult(
                actionable=True,
                summary="Documentation target identified; ready to draft an update.",
            )
        return ValidationResult(
            actionable=False,
            needs_human=True,
            summary="Documentation request is too vague to action.",
            details=("no concrete doc/page/section referenced",),
        )


def default_agents() -> list[WorkerAgent]:
    """The standard roster of worker agents."""
    return [BugAgent(), FeatureAgent(), DocumentationAgent()]
