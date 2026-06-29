"""Classifier adapters."""

from __future__ import annotations

from .copilot_classifier import CopilotCliClassifier, CopilotUnavailableError
from .gemini_classifier import GeminiCliClassifier, GeminiUnavailableError
from ._llm_cli import LlmCliClassifier, LlmUnavailableError
from .keyword_classifier import KeywordClassifier
from .recommend import (
    COPILOT_TIERS,
    GEMINI_TIERS,
    ModelTiers,
    recommend_model,
)

__all__ = [
    "CopilotCliClassifier",
    "CopilotUnavailableError",
    "GeminiCliClassifier",
    "GeminiUnavailableError",
    "LlmCliClassifier",
    "LlmUnavailableError",
    "KeywordClassifier",
    "ModelTiers",
    "COPILOT_TIERS",
    "GEMINI_TIERS",
    "recommend_model",
]
