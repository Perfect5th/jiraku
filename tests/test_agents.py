from __future__ import annotations

from jiraku.adapters.agents import (
    BugAgent,
    DocumentationAgent,
    FeatureAgent,
    default_agents,
)
from jiraku.domain import Classification, Priority, Ticket, TicketCategory


def _ticket(summary: str, description: str = "", labels=()):
    return Ticket(
        key="X-1", project="X", summary=summary, description=description,
        reporter="r", priority=Priority.MEDIUM, labels=tuple(labels),
    )


def _cls(category: TicketCategory):
    return Classification(category=category, target_project="X", confidence=0.9)


def test_bug_agent_handles_only_bugs():
    agent = BugAgent()
    assert agent.handles(TicketCategory.BUG)
    assert not agent.handles(TicketCategory.FEATURE_REQUEST)


def test_bug_agent_actionable_with_repro():
    agent = BugAgent()
    t = _ticket(
        "Crash on save",
        "Steps to reproduce:\n1. open\n2. save\nExpected ok. Actual: stack trace.",
    )
    result = agent.validate(t, _cls(TicketCategory.BUG))
    assert result.actionable
    assert not result.needs_human


def test_bug_agent_escalates_without_repro():
    agent = BugAgent()
    result = agent.validate(_ticket("Broken", "fix it"), _cls(TicketCategory.BUG))
    assert not result.actionable
    assert result.needs_human
    assert result.details


def test_feature_agent_accepts_and_flags_duplicate():
    agent = FeatureAgent()
    ok = agent.validate(
        _ticket("Add export", "please add CSV export ability"),
        _cls(TicketCategory.FEATURE_REQUEST),
    )
    dup = agent.validate(
        _ticket("Add export", "csv export", ("feature", "duplicate")),
        _cls(TicketCategory.FEATURE_REQUEST),
    )
    assert ok.actionable
    assert dup.needs_human
    assert not dup.actionable


def test_docs_agent_requires_concrete_target():
    agent = DocumentationAgent()
    ok = agent.validate(
        _ticket("Typo in README", "Fix the typo on the install page of the docs"),
        _cls(TicketCategory.DOCUMENTATION),
    )
    vague = agent.validate(_ticket("Docs", "improve"), _cls(TicketCategory.DOCUMENTATION))
    assert ok.actionable
    assert vague.needs_human


def test_default_agents_roster():
    names = {a.name for a in default_agents()}
    assert names == {"bug-agent", "feature-agent", "docs-agent"}
