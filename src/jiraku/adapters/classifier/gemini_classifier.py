"""Production classifier that delegates intent classification to the Gemini CLI.

A drop-in :class:`Classifier` selected at the composition root, mirroring the
Copilot classifier but invoking ``gemini`` instead. The prompt/parse/fallback
machinery is shared in :mod:`._llm_cli`; this module only fixes the
Gemini-specific command, model tiers and error type.

The classifier needs no write access, so it runs the CLI in read-only
``--approval-mode plan`` — tool calls never block on a prompt and the model can
never modify the workspace while merely classifying.
"""

from __future__ import annotations

from .recommend import GEMINI_TIERS
from ._llm_cli import LlmCliClassifier, LlmUnavailableError


class GeminiUnavailableError(LlmUnavailableError):
    """Raised when the Gemini CLI cannot be reached and no fallback is set."""


class GeminiCliClassifier(LlmCliClassifier):
    """Classifies tickets by prompting the Gemini CLI for JSON."""

    source_name = "gemini-cli"
    default_command = ["gemini", "--approval-mode", "plan"]
    model_tiers = GEMINI_TIERS
    error_cls = GeminiUnavailableError
