from __future__ import annotations

import pytest

from jiraku.adapters import (
    CopilotWorkAgentRunner,
    GeminiWorkAgentRunner,
    NoopWorkAgentRunner,
)
from jiraku.adapters.classifier import (
    GeminiCliClassifier,
    GeminiUnavailableError,
    KeywordClassifier,
    recommend_model,
)
from jiraku.adapters.classifier.recommend import GEMINI_TIERS
from jiraku.composition import JirakuConfig, build_system
from jiraku.domain import (
    Classification,
    Priority,
    RepoRef,
    RepoResolution,
    Ticket,
    TicketCategory,
)


def _ticket(summary="Add export", description="please add CSV export", labels=()):
    return Ticket(key="X-1", project="X", summary=summary, description=description,
                  reporter="r", priority=Priority.MEDIUM, labels=tuple(labels))


def _cls(**kw):
    return Classification(TicketCategory.BUG, "PROJ", 0.9, **kw)


def _res():
    return RepoResolution(RepoRef("acme/proj", "https://github.com/acme/proj.git"), 0.95)


# -- recommendation tiers ----------------------------------------------------

def test_gemini_tiers_recommendation():
    docs = _ticket("Fix typo", "typo in the readme docs page")
    assert recommend_model(TicketCategory.DOCUMENTATION, docs, GEMINI_TIERS) == "gemini-2.5-flash"
    # Gemini's "auto" is empty: omit the model flag and use the CLI default.
    assert recommend_model(TicketCategory.UNKNOWN, docs, GEMINI_TIERS) == ""
    simple = _ticket("button color", "the button is the wrong colour")
    assert recommend_model(TicketCategory.BUG, simple, GEMINI_TIERS) == "gemini-2.5-pro"
    complex_bug = _ticket("Crash", "NullPointerException with a stack trace " + "x" * 50)
    assert recommend_model(TicketCategory.BUG, complex_bug, GEMINI_TIERS) == "gemini-2.5-pro"


# -- gemini classifier -------------------------------------------------------

def test_gemini_classifier_default_command_is_read_only():
    # The classifier needs no writes, so it runs in read-only plan mode.
    assert GeminiCliClassifier()._command == ["gemini", "--approval-mode", "plan"]


def test_gemini_classifier_appends_model_flag():
    assert GeminiCliClassifier(model="gemini-2.5-pro")._command[-2:] == \
        ["--model", "gemini-2.5-pro"]


def test_gemini_classifier_parses_runner_output():
    def fake_runner(prompt: str) -> str:
        assert "Jira triage classifier" in prompt
        return '{"category": "Feature Request", "project": "WEB", "confidence": 0.91}'

    c = GeminiCliClassifier(runner=fake_runner).classify(_ticket("Add export"))
    assert c.category is TicketCategory.FEATURE_REQUEST
    assert c.target_project == "WEB"
    assert c.confidence == 0.91
    assert c.source == "gemini-cli"


def test_gemini_classifier_includes_hint_in_prompt():
    seen = {}

    def runner(prompt: str) -> str:
        seen["prompt"] = prompt
        return '{"category": "Bug", "confidence": 0.9}'

    GeminiCliClassifier(runner=runner).classify(_ticket("x", "y"), hint="treat as a bug")
    assert "treat as a bug" in seen["prompt"]
    assert "reviewer hint" in seen["prompt"].lower()


def test_gemini_classifier_falls_back_to_gemini_tiers():
    # No recommended_model in the output -> the Gemini policy default applies.
    def runner(prompt: str) -> str:
        return '{"category": "Documentation", "confidence": 0.9}'

    c = GeminiCliClassifier(runner=runner).classify(_ticket("typo", "fix the docs typo"))
    assert c.recommended_model == "gemini-2.5-flash"  # Gemini docs tier


def test_gemini_classifier_raises_without_fallback():
    def boom(prompt: str) -> str:
        raise GeminiUnavailableError("cli missing")

    with pytest.raises(GeminiUnavailableError):
        GeminiCliClassifier(runner=boom).classify(_ticket("x"))


def test_gemini_classifier_uses_explicit_fallback():
    def boom(prompt: str) -> str:
        raise GeminiUnavailableError("cli missing")

    c = GeminiCliClassifier(runner=boom, fallback=KeywordClassifier())
    result = c.classify(_ticket("App crash", "exception stack trace", ("bug",)))
    assert result.category is TicketCategory.BUG
    assert result.source == "keyword"  # fell back deterministically


# -- gemini work runner ------------------------------------------------------

def test_gemini_runner_default_command_yolo_and_trust():
    assert GeminiWorkAgentRunner()._command == ["gemini", "--yolo", "--skip-trust"]


def test_gemini_runner_skips_without_workspace():
    r = GeminiWorkAgentRunner(runner=lambda p, c, m: "PR_URL: https://x/pull/1")
    out = r.run(_ticket(), _cls(), _res(), "")
    assert out.started is False
    assert "workspace" in out.summary.lower()


def test_gemini_runner_omits_model_by_default(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    seen = {}

    def fake(prompt, cwd, model):
        seen["model"] = model
        seen["cwd"] = cwd
        return "done\nPR_URL: https://github.com/acme/proj/pull/7\n"

    out = GeminiWorkAgentRunner(runner=fake).run(_ticket(), _cls(), _res(), str(ws))
    assert out.started is True and out.opened_pr
    assert out.pr_url == "https://github.com/acme/proj/pull/7"
    assert out.branch == "jiraku/x-1"
    assert seen["cwd"] == str(ws)
    # No explicit model and no recommendation -> empty (CLI default), shown as "default".
    assert seen["model"] == ""
    assert out.model == "default"


def test_gemini_runner_uses_recommended_model(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    seen = {}

    def fake(prompt, cwd, model):
        seen["model"] = model
        return "PR_URL: https://x/pull/1"

    cls = _cls(recommended_model="gemini-2.5-pro")
    out = GeminiWorkAgentRunner(runner=fake).run(_ticket(), cls, _res(), str(ws))
    assert seen["model"] == "gemini-2.5-pro"
    assert out.model == "gemini-2.5-pro"


def test_gemini_runner_explicit_model_overrides_recommendation(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()
    seen = {}

    def fake(prompt, cwd, model):
        seen["model"] = model
        return "PR_URL: https://x/pull/1"

    cls = _cls(recommended_model="gemini-2.5-pro")
    out = GeminiWorkAgentRunner(runner=fake, model="gemini-2.5-flash").run(
        _ticket(), cls, _res(), str(ws))
    assert seen["model"] == "gemini-2.5-flash"  # explicit wins
    assert out.model == "gemini-2.5-flash"


def test_gemini_runner_surfaces_needs_input(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()

    def fake(prompt, cwd, model):
        return "NEEDS_INPUT: which database should I target?"

    out = GeminiWorkAgentRunner(runner=fake).run(_ticket(), _cls(), _res(), str(ws))
    assert out.started is False
    assert out.needs_input
    assert out.question == "which database should I target?"


def test_gemini_runner_handles_failure_gracefully(tmp_path):
    ws = tmp_path / "PROJ-1"
    ws.mkdir()

    def boom(prompt, cwd, model):
        raise RuntimeError("gemini exploded")

    out = GeminiWorkAgentRunner(runner=boom).run(_ticket(), _cls(), _res(), str(ws))
    assert out.started is False
    assert "exploded" in out.summary


# -- composition selection ---------------------------------------------------

def test_composition_gemini_classifier_selected():
    system = build_system(JirakuConfig(source="memory", classifier="gemini"))
    assert type(system.service._classifier).__name__ == "GeminiCliClassifier"


def test_composition_gemini_classifier_fallback_to_keyword():
    system = build_system(JirakuConfig(
        source="memory", classifier="gemini", copilot_fallback_to_keyword=True))
    assert isinstance(system.service._classifier._fallback, KeywordClassifier)


def test_composition_gemini_work_runner_selected():
    system = build_system(JirakuConfig(source="memory", work=True, work_agent="gemini"))
    assert isinstance(system.work_runner, GeminiWorkAgentRunner)


def test_composition_default_work_agent_is_copilot():
    system = build_system(JirakuConfig(source="memory", work=True))
    assert isinstance(system.work_runner, CopilotWorkAgentRunner)


def test_composition_gemini_work_model_passed_to_runner():
    system = build_system(JirakuConfig(
        source="memory", work=True, work_agent="gemini", work_model="gemini-2.5-pro"))
    assert system.work_runner._model == "gemini-2.5-pro"


def test_composition_no_work_runner_when_work_off():
    system = build_system(JirakuConfig(source="memory", work_agent="gemini"))
    assert isinstance(system.work_runner, NoopWorkAgentRunner)
