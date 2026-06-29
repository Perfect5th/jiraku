"""A deterministic, dependency-free intent classifier.

Used as jiraku's default classifier so the harness runs fully offline and so
tests are reproducible. The Copilot CLI classifier (``copilot_classifier``) is
the production drop-in that implements the same port.
"""

from __future__ import annotations

from ...domain import Classification, Ticket, TicketCategory
from ...ports import Classifier
from .recommend import recommend_model

_SIGNALS: dict[TicketCategory, tuple[str, ...]] = {
    TicketCategory.BUG: (
        "bug", "error", "crash", "exception", "fails", "failing", "broken",
        "not working", "doesn't work", "stack trace", "traceback", "500",
        "npe", "nullpointer", "regression", "throws",
    ),
    TicketCategory.FEATURE_REQUEST: (
        "feature", "add ", "support for", "would be nice", "enhancement",
        "implement", "ability to", "please add", "request", "nice to have",
        "new ", "allow",
    ),
    TicketCategory.DOCUMENTATION: (
        "docs", "documentation", "readme", "typo", "clarify", "example",
        "tutorial", "guide", "comment", "wording", "explain",
    ),
}

_LABEL_CATEGORY: dict[str, TicketCategory] = {
    "bug": TicketCategory.BUG,
    "defect": TicketCategory.BUG,
    "feature": TicketCategory.FEATURE_REQUEST,
    "enhancement": TicketCategory.FEATURE_REQUEST,
    "documentation": TicketCategory.DOCUMENTATION,
    "docs": TicketCategory.DOCUMENTATION,
}

# The native Jira issue type is the most authoritative signal Jira gives us, so
# it carries the most weight in scoring.
_TYPE_CATEGORY: dict[str, TicketCategory] = {
    "bug": TicketCategory.BUG,
    "defect": TicketCategory.BUG,
    "incident": TicketCategory.BUG,
    "fault": TicketCategory.BUG,
    "story": TicketCategory.FEATURE_REQUEST,
    "epic": TicketCategory.FEATURE_REQUEST,
    "objective": TicketCategory.FEATURE_REQUEST,
    "initiative": TicketCategory.FEATURE_REQUEST,
    "task": TicketCategory.FEATURE_REQUEST,
    "improvement": TicketCategory.FEATURE_REQUEST,
    "new feature": TicketCategory.FEATURE_REQUEST,
    "feature": TicketCategory.FEATURE_REQUEST,
    "suggestion": TicketCategory.FEATURE_REQUEST,
    "documentation": TicketCategory.DOCUMENTATION,
    "docs": TicketCategory.DOCUMENTATION,
}

_TYPE_WEIGHT = 3  # an explicit issue type outweighs prose and labels
_HINT_WEIGHT = 4  # an explicit human reviewer note is authoritative


class KeywordClassifier(Classifier):
    """Scores tickets against per-category keyword, label and issue-type signals."""

    source_name = "keyword"

    def classify(self, ticket: Ticket, hint: str | None = None) -> Classification:
        haystack = f"{ticket.summary}\n{ticket.description}".lower()
        scores: dict[TicketCategory, int] = {c: 0 for c in _SIGNALS}

        for category, signals in _SIGNALS.items():
            scores[category] += sum(1 for s in signals if s in haystack)

        for label in ticket.labels:
            mapped = _LABEL_CATEGORY.get(label.lower())
            if mapped is not None:
                scores[mapped] += 2  # explicit labels weigh more than prose

        type_category = _TYPE_CATEGORY.get(ticket.issue_type.strip().lower())
        if type_category is not None:
            scores[type_category] += _TYPE_WEIGHT

        # A human reviewer's note is authoritative: any category signal in the
        # hint outweighs everything else.
        hint_category: TicketCategory | None = None
        if hint:
            hint_text = hint.lower()
            for category, signals in _SIGNALS.items():
                if any(s in hint_text for s in signals):
                    scores[category] += _HINT_WEIGHT
                    hint_category = category

        best = max(scores, key=lambda c: scores[c])
        best_score = scores[best]
        if best_score == 0:
            return Classification(
                category=TicketCategory.UNKNOWN,
                target_project=ticket.project,
                confidence=0.2,
                rationale="No category signals found in issue type, labels or text.",
                source=self.source_name,
                recommended_model=recommend_model(TicketCategory.UNKNOWN, ticket),
            )

        runner_up = max((scores[c] for c in scores if c != best), default=0)
        confidence = min(0.93, 0.55 + 0.13 * best_score)
        if best_score - runner_up <= 0:  # ambiguous tie between categories
            confidence *= 0.7

        rationale = f"Matched {best_score} '{best}' signal(s)"
        if hint_category is best:
            rationale += " (reviewer hint)"
        elif type_category is best:
            rationale += f" (Jira issue type '{ticket.issue_type}')"
        return Classification(
            category=best,
            target_project=ticket.project,
            confidence=round(confidence, 2),
            rationale=rationale + ".",
            source=self.source_name,
            recommended_model=recommend_model(best, ticket),
        )
