"""Work-agent runner adapters.

The default runner is a no-op (records intent, does nothing) so the harness is
safe in dry-run and tests. The Copilot runner invokes the GitHub Copilot CLI
inside the cloned workspace to implement the ticket and open a pull request —
mirroring the injectable structure of the Copilot *classifier*.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from ..domain import Classification, RepoResolution, Ticket, WorkResult

# (prompt, cwd, model) -> stdout
PromptRunner = Callable[[str, str, "str | None"], str]

_PR_LABEL_RE = re.compile(r"PR_URL:\s*(\S+)", re.IGNORECASE)
_PR_URL_RE = re.compile(
    r"https?://[^\s)\"']+/(?:pull|pulls|merge_requests|pull-requests)/\d+"
)
_NEEDS_INPUT_RE = re.compile(r"NEEDS_INPUT:\s*(.+)", re.IGNORECASE)

_SENTINEL = (
    "If you are blocked and need a decision or information from a human to "
    "proceed, print exactly one line: NEEDS_INPUT: <your question> and stop "
    "(do not open a pull request)."
)

_PROMPT_TEMPLATE = """\
You are an autonomous software engineer working in the repository checked out in
the current working directory. Implement this Jira ticket end to end.

Key: {key}
Type: {category}
Repo: {repo}
Summary: {summary}
Description:
{description}

Do the following:
1. Create and switch to a new branch named "{branch}".
2. Make a minimal, focused change that addresses the ticket.
3. Commit with a clear message that references {key}.
4. Push the branch and open a pull request (use `gh pr create` if available).
5. On the final line, print exactly: PR_URL: <the pull request URL>

{sentinel}
"""

_RESUME_TEMPLATE = """\
You are resuming work on Jira ticket {key} in the repository checked out in the
current working directory. You previously stopped to ask a human a question.

A human has now answered: {answer}

Continue your work on the existing branch "{branch}" (it is already checked out
in this workspace), implement the change, commit, push, and open a pull request.
On the final line, print exactly: PR_URL: <the pull request URL>

{sentinel}
"""


class NoopWorkAgentRunner:
    """Records the request and does nothing (safe default / dry-run / tests)."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []  # (ticket_key, workspace)

    def run(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None,
        workspace: str,
        answer: str | None = None,
    ) -> WorkResult:
        self.runs.append((ticket.key, workspace))
        return WorkResult.skipped("Work agent not configured (no-op).")


class CopilotWorkAgentRunner:
    """Runs the Copilot CLI in the cloned workspace and opens a PR.

    The model is chosen per ticket: the explicitly-configured work ``model`` if
    set, otherwise the model the classifier *recommended* for this ticket,
    otherwise Copilot's ``auto``.
    """

    def __init__(
        self,
        *,
        runner: PromptRunner | None = None,
        command: list[str] | None = None,
        model: str | None = None,
        timeout: float = 1800.0,
    ) -> None:
        self._command = command or ["copilot", "--allow-all-tools", "--no-color"]
        self._model = model  # explicit work model (overrides any recommendation)
        self._runner = runner or _default_runner(self._command, timeout)

    def run(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None,
        workspace: str,
        answer: str | None = None,
    ) -> WorkResult:
        if not workspace or not Path(workspace).is_dir():
            return WorkResult.skipped(
                f"No provisioned workspace for {ticket.key}; skipping work agent."
            )
        model = self._model or classification.recommended_model or "auto"
        branch = f"jiraya/{ticket.key.lower()}"
        if answer:
            prompt = _RESUME_TEMPLATE.format(
                key=ticket.key, answer=answer, branch=branch, sentinel=_SENTINEL,
            )
        else:
            prompt = _PROMPT_TEMPLATE.format(
                key=ticket.key,
                category=classification.category,
                repo=(resolution.repo.key if resolution and resolution.repo else "unknown"),
                summary=ticket.summary,
                description=ticket.description,
                branch=branch,
                sentinel=_SENTINEL,
            )
        try:
            output = self._runner(prompt, workspace, model)
        except Exception as exc:  # noqa: BLE001 - work is best-effort, never crash triage
            return WorkResult(
                started=False, model=model,
                summary=f"Work agent failed for {ticket.key}: {exc}",
            )
        # A blocked agent asks a question instead of opening a PR.
        question = _extract_question(output)
        if question:
            return WorkResult.blocked(question, branch=branch, model=model)
        pr_url = _extract_pr_url(output)
        summary = (
            f"Opened pull request for {ticket.key} (model {model})."
            if pr_url
            else f"Work agent ran for {ticket.key} (model {model}) but reported no PR URL."
        )
        return WorkResult(
            started=True, summary=summary, branch=branch, pr_url=pr_url, model=model
        )


def _default_runner(command: list[str], timeout: float) -> PromptRunner:
    def run(prompt: str, cwd: str, model: str | None) -> str:
        if shutil.which(command[0]) is None:
            raise RuntimeError(f"'{command[0]}' not found on PATH")
        cmd = [*command]
        if model:
            cmd += ["--model", model]
        completed = subprocess.run(
            [*cmd, "-p", prompt],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return completed.stdout

    return run


def _extract_pr_url(output: str) -> str:
    label = _PR_LABEL_RE.search(output)
    if label:
        candidate = label.group(1).strip().rstrip(".,)")
        if candidate.startswith("http"):
            return candidate
    url = _PR_URL_RE.search(output)
    return url.group(0) if url else ""


def _extract_question(output: str) -> str:
    match = _NEEDS_INPUT_RE.search(output)
    return match.group(1).strip() if match else ""
