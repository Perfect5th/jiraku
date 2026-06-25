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

# (prompt, cwd) -> stdout
PromptRunner = Callable[[str, str], str]

_PR_LABEL_RE = re.compile(r"PR_URL:\s*(\S+)", re.IGNORECASE)
_PR_URL_RE = re.compile(
    r"https?://[^\s)\"']+/(?:pull|pulls|merge_requests|pull-requests)/\d+"
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
    ) -> WorkResult:
        self.runs.append((ticket.key, workspace))
        return WorkResult.skipped("Work agent not configured (no-op).")


class CopilotWorkAgentRunner:
    """Runs the Copilot CLI in the cloned workspace and opens a PR."""

    def __init__(
        self,
        *,
        runner: PromptRunner | None = None,
        command: list[str] | None = None,
        model: str | None = None,
        timeout: float = 1800.0,
    ) -> None:
        self._command = command or ["copilot", "--allow-all-tools", "--no-color"]
        if model:
            self._command = [*self._command, "--model", model]
        self._runner = runner or _default_runner(self._command, timeout)

    def run(
        self,
        ticket: Ticket,
        classification: Classification,
        resolution: RepoResolution | None,
        workspace: str,
    ) -> WorkResult:
        if not workspace or not Path(workspace).is_dir():
            return WorkResult.skipped(
                f"No provisioned workspace for {ticket.key}; skipping work agent."
            )
        branch = f"jiraya/{ticket.key.lower()}"
        prompt = _PROMPT_TEMPLATE.format(
            key=ticket.key,
            category=classification.category,
            repo=(resolution.repo.key if resolution and resolution.repo else "unknown"),
            summary=ticket.summary,
            description=ticket.description,
            branch=branch,
        )
        try:
            output = self._runner(prompt, workspace)
        except Exception as exc:  # noqa: BLE001 - work is best-effort, never crash triage
            return WorkResult(
                started=False,
                summary=f"Work agent failed for {ticket.key}: {exc}",
            )
        pr_url = _extract_pr_url(output)
        summary = (
            f"Opened pull request for {ticket.key}."
            if pr_url
            else f"Work agent ran for {ticket.key} but reported no PR URL."
        )
        return WorkResult(started=True, summary=summary, branch=branch, pr_url=pr_url)


def _default_runner(command: list[str], timeout: float) -> PromptRunner:
    def run(prompt: str, cwd: str) -> str:
        if shutil.which(command[0]) is None:
            raise RuntimeError(f"'{command[0]}' not found on PATH")
        completed = subprocess.run(
            [*command, "-p", prompt],
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
