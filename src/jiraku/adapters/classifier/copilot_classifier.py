"""Production classifier that delegates intent classification to Copilot CLI.

Implements the same ``Classifier`` port as :class:`KeywordClassifier`, so it is
a drop-in replacement selected at the composition root. The subprocess call is
injected (``runner``) to keep the adapter unit-testable without the binary.

The prompt/parse/fallback machinery is shared with the Gemini classifier in
:mod:`._llm_cli`; this module only fixes the Copilot-specific command, model
tiers and error type.
"""

from __future__ import annotations

from .recommend import COPILOT_TIERS
from ._llm_cli import (  # noqa: F401 - re-exported for backwards compatibility
    LlmCliClassifier,
    LlmUnavailableError,
    PromptRunner,
    _extract_json,
)


class CopilotUnavailableError(LlmUnavailableError):
    """Raised when the Copilot CLI cannot be reached and no fallback is set."""


class CopilotCliClassifier(LlmCliClassifier):
    """Classifies tickets by prompting the GitHub Copilot CLI for JSON."""

    source_name = "copilot-cli"
    default_command = ["copilot", "--allow-all-tools", "--no-color"]
    model_tiers = COPILOT_TIERS
    error_cls = CopilotUnavailableError
