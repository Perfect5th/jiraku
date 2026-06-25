"""Production classifier that delegates intent classification to Copilot CLI.

Implements the same ``Classifier`` port as :class:`KeywordClassifier`, so it is
a drop-in replacement selected at the composition root. The subprocess call is
injected (``runner``) to keep the adapter unit-testable without the binary.

In keeping with "deterministic failure over silent degradation", a failure to
reach the CLI raises :class:`CopilotUnavailableError` unless the composition
root *explicitly* supplies a ``fallback`` classifier.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Callable

from ...domain import Classification, Ticket, TicketCategory
from ...ports import Classifier

PromptRunner = Callable[[str], str]

_PROMPT_TEMPLATE = """\
You are a Jira triage classifier. Classify the ticket below into exactly one
category: "Bug", "Feature Request", "Documentation", or "Unknown".

Respond with ONLY a single JSON object, no prose, in this exact shape:
{{"category": "<one of the categories>", "project": "<target project key>", \
"confidence": <float 0..1>, "rationale": "<one short sentence>"}}

Ticket key: {key}
Project: {project}
Issue type: {issue_type}
Reporter: {reporter}
Priority: {priority}
Labels: {labels}
Summary: {summary}
Description:
{description}
"""

_CATEGORY_BY_NAME = {c.value.lower(): c for c in TicketCategory}


class CopilotUnavailableError(RuntimeError):
    """Raised when the Copilot CLI cannot be reached and no fallback is set."""


def _default_runner(command: list[str], timeout: float) -> PromptRunner:
    def run(prompt: str) -> str:
        if shutil.which(command[0]) is None:
            raise CopilotUnavailableError(f"'{command[0]}' not found on PATH")
        try:
            completed = subprocess.run(
                [*command, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise CopilotUnavailableError(str(exc)) from exc
        return completed.stdout

    return run


class CopilotCliClassifier(Classifier):
    """Classifies tickets by prompting the GitHub Copilot CLI for JSON."""

    source_name = "copilot-cli"

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
        self._command = command or ["copilot", "--allow-all-tools", "--no-color"]
        if model:
            self._command = [*self._command, "--model", model]
        self._runner = runner or _default_runner(self._command, timeout)
        self._fallback = fallback

    def classify(self, ticket: Ticket) -> Classification:
        prompt = _PROMPT_TEMPLATE.format(
            key=ticket.key,
            project=ticket.project,
            issue_type=ticket.issue_type or "unspecified",
            reporter=ticket.reporter,
            priority=ticket.priority,
            labels=", ".join(ticket.labels) or "none",
            summary=ticket.summary,
            description=ticket.description,
        )
        try:
            raw = self._runner(prompt)
            return self._parse(raw, ticket)
        except (CopilotUnavailableError, ValueError) as exc:
            if self._fallback is not None:
                return self._fallback.classify(ticket)
            raise CopilotUnavailableError(
                f"Copilot classification failed for {ticket.key}: {exc}"
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
        return Classification(
            category=category,
            target_project=str(payload.get("project") or ticket.project),
            confidence=round(confidence, 2),
            rationale=str(payload.get("rationale", "")).strip(),
            source=self.source_name,
        )


def _extract_json(raw: str) -> dict:
    """Pull the first balanced JSON object out of free-form CLI output."""
    start = raw.find("{")
    if start == -1:
        raise ValueError("no JSON object found in Copilot output")
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
                    raise ValueError(f"invalid JSON in Copilot output: {exc}") from exc
    raise ValueError("unterminated JSON object in Copilot output")
