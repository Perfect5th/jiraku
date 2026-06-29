"""Shared base for classifiers that delegate to an LLM command-line tool.

Both the Copilot and Gemini classifiers prompt an external CLI for a single
JSON object describing the ticket's category, target project, confidence and a
recommended work model. They differ only in their default command, model-tier
policy and ``source`` label, so the prompt/parse/fallback machinery lives here.

In keeping with "deterministic failure over silent degradation", a failure to
reach the CLI raises :class:`LlmUnavailableError` (or a provider subclass)
unless the composition root *explicitly* supplies a ``fallback`` classifier.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Callable

from ...domain import Classification, Ticket, TicketCategory
from ...ports import Classifier
from .recommend import COPILOT_TIERS, ModelTiers, recommend_model

PromptRunner = Callable[[str], str]

_PROMPT_TEMPLATE = """\
You are a Jira triage classifier. Classify the ticket below into exactly one
category: "Bug", "Feature Request", "Documentation", or "Unknown". Also
recommend which model should do the implementation work for this ticket
(a stronger model for complex/risky work, a cheaper one for trivial changes).

Respond with ONLY a single JSON object, no prose, in this exact shape:
{{"category": "<one of the categories>", "project": "<target project key>", \
"confidence": <float 0..1>, "rationale": "<one short sentence>", \
"recommended_model": "<model name or empty>"}}

Ticket key: {key}
Project: {project}
Issue type: {issue_type}
Reporter: {reporter}
Priority: {priority}
Labels: {labels}
Summary: {summary}
Description:
{description}
{hint_block}"""

_CATEGORY_BY_NAME = {c.value.lower(): c for c in TicketCategory}


class LlmUnavailableError(RuntimeError):
    """Raised when an LLM CLI cannot be reached and no fallback is set."""


def _default_runner(
    command: list[str], timeout: float, error_cls: type[LlmUnavailableError]
) -> PromptRunner:
    def run(prompt: str) -> str:
        if shutil.which(command[0]) is None:
            raise error_cls(f"'{command[0]}' not found on PATH")
        try:
            completed = subprocess.run(
                [*command, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise error_cls(str(exc)) from exc
        return completed.stdout

    return run


class LlmCliClassifier(Classifier):
    """Classifies tickets by prompting an LLM CLI for a single JSON object.

    Subclasses set :attr:`source_name`, :attr:`default_command`,
    :attr:`model_tiers` and :attr:`error_cls`.
    """

    source_name: str = "llm-cli"
    default_command: list[str] = []
    model_tiers: ModelTiers = COPILOT_TIERS
    error_cls: type[LlmUnavailableError] = LlmUnavailableError

    def __init__(
        self,
        *,
        runner: PromptRunner | None = None,
        command: list[str] | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        fallback: Classifier | None = None,
    ) -> None:
        # ``-p <prompt>`` is appended by the runner, so the prompt always
        # immediately follows the flag regardless of the rest of the command.
        self._command = command or list(self.default_command)
        if model:
            self._command = [*self._command, "--model", model]
        self._runner = runner or _default_runner(
            self._command, timeout, self.error_cls
        )
        self._fallback = fallback

    def classify(self, ticket: Ticket, hint: str | None = None) -> Classification:
        hint_block = (
            f"\nHuman reviewer hint (authoritative, weigh heavily): {hint}\n"
            if hint
            else ""
        )
        prompt = _PROMPT_TEMPLATE.format(
            key=ticket.key,
            project=ticket.project,
            issue_type=ticket.issue_type or "unspecified",
            reporter=ticket.reporter,
            priority=ticket.priority,
            labels=", ".join(ticket.labels) or "none",
            summary=ticket.summary,
            description=ticket.description,
            hint_block=hint_block,
        )
        try:
            raw = self._runner(prompt)
            return self._parse(raw, ticket)
        except (LlmUnavailableError, ValueError) as exc:
            if self._fallback is not None:
                return self._fallback.classify(ticket, hint)
            raise self.error_cls(
                f"{self.source_name} classification failed for {ticket.key}: {exc}"
            ) from exc

    def _parse(self, raw: str, ticket: Ticket) -> Classification:
        payload = _extract_json(raw)
        category = _CATEGORY_BY_NAME.get(
            str(payload.get("category", "")).strip().lower(),
            TicketCategory.UNKNOWN,
        )
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        # Use the model the LLM recommended; fall back to the policy default
        # for this provider's model tiers.
        recommended = str(payload.get("recommended_model", "")).strip()
        if not recommended:
            recommended = recommend_model(category, ticket, self.model_tiers)
        return Classification(
            category=category,
            target_project=str(payload.get("project") or ticket.project),
            confidence=round(confidence, 2),
            rationale=str(payload.get("rationale", "")).strip(),
            source=self.source_name,
            recommended_model=recommended,
        )


def _extract_json(raw: str) -> dict:
    """Pull the first balanced JSON object out of free-form CLI output."""
    start = raw.find("{")
    if start == -1:
        raise ValueError("no JSON object found in CLI output")
    depth = 0
    for end in range(start, len(raw)):
        char = raw[end]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON in CLI output: {exc}") from exc
    raise ValueError("unterminated JSON object in CLI output")
