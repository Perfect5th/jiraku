"""Model-recommendation policy.

The classifier recommends which model the *work agent* should use for a ticket
when no work model is explicitly configured. The recommendation is a tiered
heuristic by category and complexity. These are sensible defaults — override the
tier constants (or pass an explicit ``--work-model``) for your environment.

The tiers are provider-specific: each LLM-CLI classifier passes the
:class:`ModelTiers` for its own provider so the recommended model is a name that
provider's work agent will actually accept.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain import Ticket, TicketCategory


@dataclass(frozen=True, slots=True)
class ModelTiers:
    """A provider's model names by capability tier.

    ``auto`` is the "let the CLI decide" value: it may be a sentinel the CLI
    understands (Copilot's ``"auto"``) or an empty string meaning "omit the
    model flag and use the CLI's configured default" (Gemini).
    """

    fast: str       # cheap/fast — small, well-scoped changes
    standard: str   # general coding work
    deep: str       # gnarly bugs / large features
    auto: str       # let the CLI choose


# Copilot CLI model names (the historical default tiers; "auto" lets Copilot choose).
COPILOT_TIERS = ModelTiers(
    fast="gpt-5-mini",
    standard="claude-sonnet-4.5",
    deep="claude-opus-4.5",
    auto="auto",
)

# Gemini CLI model names. ``auto`` is empty: omit ``--model`` and let the
# Gemini CLI use its configured default.
GEMINI_TIERS = ModelTiers(
    fast="gemini-2.5-flash",
    standard="gemini-2.5-pro",
    deep="gemini-2.5-pro",
    auto="",
)

# Backwards-compatible module constants (the Copilot tiers).
FAST_MODEL = COPILOT_TIERS.fast
STANDARD_MODEL = COPILOT_TIERS.standard
DEEP_MODEL = COPILOT_TIERS.deep
AUTO_MODEL = COPILOT_TIERS.auto

_COMPLEXITY_SIGNALS = (
    "stack trace", "traceback", "exception", "race condition", "deadlock",
    "regression", "segfault", "memory leak", "performance", "scalab",
)


def _is_complex(ticket: Ticket) -> bool:
    text = f"{ticket.summary}\n{ticket.description}".lower()
    if any(s in text for s in _COMPLEXITY_SIGNALS):
        return True
    return len(ticket.description.strip()) >= 600


def recommend_model(
    category: TicketCategory,
    ticket: Ticket,
    tiers: ModelTiers = COPILOT_TIERS,
) -> str:
    """Recommend a work model for a classified ticket using ``tiers``."""
    if category is TicketCategory.UNKNOWN:
        return tiers.auto
    if category is TicketCategory.DOCUMENTATION:
        return tiers.fast
    # Bugs and feature requests: go deep when the ticket looks complex.
    return tiers.deep if _is_complex(ticket) else tiers.standard
