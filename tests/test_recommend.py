from __future__ import annotations

from jiraku.adapters.classifier import CopilotCliClassifier, KeywordClassifier, recommend_model
from jiraku.adapters.classifier.recommend import (
    AUTO_MODEL,
    DEEP_MODEL,
    FAST_MODEL,
    STANDARD_MODEL,
)
from jiraku.domain import Priority, Ticket, TicketCategory


def _ticket(summary="Add export", description="please add CSV export", issue_type="", labels=()):
    return Ticket(key="X-1", project="X", summary=summary, description=description,
                  reporter="r", priority=Priority.MEDIUM, issue_type=issue_type,
                  labels=tuple(labels))


# -- recommendation policy ---------------------------------------------------

def test_recommend_model_by_category():
    docs = _ticket("Fix typo", "typo in the readme docs page")
    assert recommend_model(TicketCategory.DOCUMENTATION, docs) == FAST_MODEL
    assert recommend_model(TicketCategory.UNKNOWN, docs) == AUTO_MODEL
    simple_bug = _ticket("button color wrong", "the button is the wrong colour")
    assert recommend_model(TicketCategory.BUG, simple_bug) == STANDARD_MODEL


def test_recommend_model_escalates_for_complex_tickets():
    complex_bug = _ticket("Crash", "NullPointerException with a long stack trace:\n" + "x" * 50)
    assert recommend_model(TicketCategory.BUG, complex_bug) == DEEP_MODEL
    big_feature = _ticket("Big feature", "y" * 700)
    assert recommend_model(TicketCategory.FEATURE_REQUEST, big_feature) == DEEP_MODEL


# -- keyword classifier ------------------------------------------------------

def test_keyword_classifier_sets_recommended_model():
    c = KeywordClassifier().classify(
        _ticket("Add dark mode", "please add the ability to switch theme", labels=("enhancement",))
    )
    assert c.category is TicketCategory.FEATURE_REQUEST
    assert c.recommended_model == STANDARD_MODEL


def test_keyword_classifier_unknown_recommends_auto():
    c = KeywordClassifier().classify(_ticket("help", "please look at this"))
    assert c.category is TicketCategory.UNKNOWN
    assert c.recommended_model == AUTO_MODEL


# -- copilot classifier ------------------------------------------------------

def test_copilot_classifier_parses_recommended_model():
    def runner(prompt: str) -> str:
        assert "recommended_model" in prompt
        return ('{"category": "Bug", "confidence": 0.9, '
                '"recommended_model": "claude-opus-4.5"}')

    c = CopilotCliClassifier(runner=runner).classify(_ticket("crash", "it crashes"))
    assert c.recommended_model == "claude-opus-4.5"


def test_copilot_classifier_falls_back_to_policy_recommendation():
    def runner(prompt: str) -> str:
        return '{"category": "Documentation", "confidence": 0.9}'  # no recommended_model

    c = CopilotCliClassifier(runner=runner).classify(_ticket("typo", "fix the docs typo"))
    assert c.recommended_model == FAST_MODEL  # policy default for docs
