from __future__ import annotations

import pytest

from jiraya.adapters.classifier import (
    CopilotCliClassifier,
    CopilotUnavailableError,
    KeywordClassifier,
)
from jiraya.adapters.classifier.copilot_classifier import _extract_json
from jiraya.domain import Priority, Ticket, TicketCategory


def _ticket(summary: str, description: str = "", labels=()):
    return Ticket(
        key="X-1", project="X", summary=summary, description=description,
        reporter="r", priority=Priority.MEDIUM, labels=tuple(labels),
    )


def test_keyword_classifier_detects_bug():
    c = KeywordClassifier().classify(
        _ticket("App crash", "It throws an exception with a stack trace", ("bug",))
    )
    assert c.category is TicketCategory.BUG
    assert c.is_confident
    assert c.source == "keyword"


def test_keyword_classifier_detects_feature_and_docs():
    feat = KeywordClassifier().classify(
        _ticket("Add dark mode", "Please add the ability to switch theme", ("enhancement",))
    )
    docs = KeywordClassifier().classify(
        _ticket("Fix typo in README", "Update the documentation page", ("documentation",))
    )
    assert feat.category is TicketCategory.FEATURE_REQUEST
    assert docs.category is TicketCategory.DOCUMENTATION


def test_keyword_classifier_unknown_when_no_signal():
    c = KeywordClassifier().classify(_ticket("Please help", "Can someone look?"))
    assert c.category is TicketCategory.UNKNOWN
    assert not c.is_confident


def test_keyword_classifier_uses_jira_issue_type():
    # No keyword/label signals at all — only the native Jira issue type.
    ticket = Ticket(key="X-1", project="X", summary="Quarterly objective",
                    description="Deliver the thing.", reporter="r",
                    issue_type="Bug")
    c = KeywordClassifier().classify(ticket)
    assert c.category is TicketCategory.BUG
    assert c.is_confident
    assert "issue type" in c.rationale.lower()


def test_keyword_classifier_issue_type_story_is_feature():
    ticket = Ticket(key="X-2", project="X", summary="Do work",
                    description="Some work item.", reporter="r", issue_type="Story")
    assert KeywordClassifier().classify(ticket).category is TicketCategory.FEATURE_REQUEST


def test_keyword_classifier_respects_reviewer_hint():
    t = _ticket("Investigate the thing", "Please look into this when you can.")
    assert KeywordClassifier().classify(t).category is TicketCategory.UNKNOWN
    hinted = KeywordClassifier().classify(t, hint="this is a bug, it crashes on save")
    assert hinted.category is TicketCategory.BUG
    assert "hint" in hinted.rationale.lower()


def test_copilot_classifier_includes_hint_in_prompt():
    seen = {}

    def runner(prompt: str) -> str:
        seen["prompt"] = prompt
        return '{"category": "Bug", "confidence": 0.9}'

    CopilotCliClassifier(runner=runner).classify(_ticket("x", "y"), hint="treat as a bug")
    assert "treat as a bug" in seen["prompt"]
    assert "reviewer hint" in seen["prompt"].lower()


def test_extract_json_from_noisy_output():
    raw = "thinking...\nHere is the result:\n{\"category\": \"Bug\", \"confidence\": 0.8}\nbye"
    assert _extract_json(raw) == {"category": "Bug", "confidence": 0.8}


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def test_copilot_classifier_parses_runner_output():
    def fake_runner(prompt: str) -> str:
        assert "Jira triage classifier" in prompt
        return '{"category": "Feature Request", "project": "WEB", "confidence": 0.91, "rationale": "asks to add"}'

    c = CopilotCliClassifier(runner=fake_runner).classify(_ticket("Add export"))
    assert c.category is TicketCategory.FEATURE_REQUEST
    assert c.target_project == "WEB"
    assert c.confidence == 0.91
    assert c.source == "copilot-cli"


def test_copilot_classifier_clamps_confidence_and_handles_unknown_category():
    def fake_runner(prompt: str) -> str:
        return '{"category": "Nonsense", "confidence": 5}'

    c = CopilotCliClassifier(runner=fake_runner).classify(_ticket("???"))
    assert c.category is TicketCategory.UNKNOWN
    assert c.confidence == 1.0


def test_copilot_classifier_raises_without_fallback():
    def boom(prompt: str) -> str:
        raise CopilotUnavailableError("cli missing")

    with pytest.raises(CopilotUnavailableError):
        CopilotCliClassifier(runner=boom).classify(_ticket("x"))


def test_copilot_classifier_uses_explicit_fallback():
    def boom(prompt: str) -> str:
        raise CopilotUnavailableError("cli missing")

    c = CopilotCliClassifier(runner=boom, fallback=KeywordClassifier())
    result = c.classify(_ticket("App crash", "exception stack trace", ("bug",)))
    assert result.category is TicketCategory.BUG
    assert result.source == "keyword"  # fell back deterministically


def test_copilot_classifier_bad_json_raises():
    def fake_runner(prompt: str) -> str:
        return "{not valid json"

    with pytest.raises(CopilotUnavailableError):
        CopilotCliClassifier(runner=fake_runner).classify(_ticket("x"))
