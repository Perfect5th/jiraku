r"""The triage harness — orchestration of the end-to-end workflow.

This is the application core that ties the ports together:

    classify -> resolve repo -> route -> validate -> transition  (happy path)
                                                  \-> escalate    (low confidence)

It depends only on ports, never on concrete adapters, so the same logic runs
against the in-memory fakes, a real Jira instance, or the Copilot CLI.
"""

from __future__ import annotations

import uuid
from typing import Callable

from ..domain import (
    ActivityLevel,
    AgentActivity,
    ActivityLogged,
    Classification,
    EscalationStage,
    InboxEntry,
    InboxResponse,
    MetricsUpdated,
    RepoRef,
    RepoResolution,
    Ticket,
    TicketCategory,
    TicketClassified,
    TicketEscalated,
    TicketRepoResolved,
    TicketRouted,
    TicketStatus,
    TicketTransitioned,
    TicketTriaged,
    TicketWorkStarted,
    TriageAction,
    TriageMetrics,
    TriageOutcome,
)
from ..ports import (
    Classifier,
    EventPublisher,
    InboxRepository,
    LearnedRulesStore,
    RepoResolver,
    TicketSource,
    WorkAgentRunner,
    WorkspaceProvisioner,
)
from .router import AgentRouter


class _NullResolver:
    """Resolver used when none is configured: never resolves a repo."""

    def resolve(self, ticket, classification, hint=None):  # noqa: ANN001
        return RepoResolution.unresolved("No repo resolver configured.", source="none")


class _NullPublisher:
    """No-op publisher used when no event bus is wired in."""

    def publish(self, event) -> None:  # noqa: D401, ANN001
        return None


class TriageService:
    """Implements the inbound :class:`~jiraya.ports.inbound.TriageService` port."""

    def __init__(
        self,
        *,
        ticket_source: TicketSource,
        classifier: Classifier,
        router: AgentRouter,
        inbox: InboxRepository,
        events: EventPublisher | None = None,
        resolver: RepoResolver | None = None,
        provisioner: WorkspaceProvisioner | None = None,
        work_runner: WorkAgentRunner | None = None,
        learned_store: LearnedRulesStore | None = None,
        confidence_threshold: float = 0.6,
        resolution_threshold: float = 0.6,
        require_repo: bool = True,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._source = ticket_source
        self._classifier = classifier
        self._router = router
        self._inbox = inbox
        self._events = events or _NullPublisher()
        self._resolver = resolver or _NullResolver()
        self._provisioner = provisioner
        self._work_runner = work_runner
        self._learned = learned_store
        self._threshold = confidence_threshold
        self._resolution_threshold = resolution_threshold
        # When no real resolver is configured, don't block tickets on repo
        # resolution (keeps the resolution step opt-in/back-compatible).
        self._require_repo = require_repo and not isinstance(self._resolver, _NullResolver)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:8])
        self._metrics = TriageMetrics()


    # -- public API -----------------------------------------------------------

    @property
    def metrics(self) -> TriageMetrics:
        return self._metrics

    def triage_batch(self, tickets: list[Ticket]) -> list[TriageOutcome]:
        return [self.triage_ticket(t) for t in tickets]

    def respond_to_inbox(
        self,
        entry_id: str,
        note: str,
        *,
        repo: RepoRef | None = None,
        post_comment: bool = False,
        rerun: bool = False,
    ) -> InboxResponse:
        """Act on a human's response to a surfaced exception.

        Optionally posts ``note`` as a comment back to the Jira issue, teaches a
        ``repo`` mapping (which both unblocks this ticket and improves future
        resolution), and/or re-runs triage using ``note`` as an authoritative
        hint. Re-running resolves the original inbox entry (a fresh one is
        created if the ticket still cannot be actioned).
        """
        entry = self._inbox.get(entry_id)
        if entry is None:
            raise KeyError(f"Unknown inbox entry: {entry_id}")

        note = (note or "").strip()
        commented = False
        comment_id: str | None = None
        taught = False
        retriaged = False
        outcome: TriageOutcome | None = None

        if post_comment and note:
            comment_id = self._source.add_comment(entry.ticket_key, note)
            commented = True
            if comment_id:
                self._log("reviewer", entry.ticket_key,
                          f"Posted comment to Jira (id {comment_id}).",
                          level=ActivityLevel.SUCCESS)

        # Teach the repo mapping: unblocks this ticket and, via the learned-rules
        # store, resolves future tickets in the same project automatically.
        if repo is not None and self._learned is not None:
            ticket_for_project = self._source.get(entry.ticket_key)
            project = ticket_for_project.project if ticket_for_project else ""
            self._learned.learn(project=project, repo=repo)
            taught = True
            self._log("reviewer", entry.ticket_key,
                      f"Learned repo mapping: {project or entry.ticket_key} -> {repo}.",
                      level=ActivityLevel.SUCCESS)
            # Supplying a repo implies we want to proceed.
            rerun = True

        if rerun:
            self._inbox.resolve(
                entry_id,
                f"Re-triaged with reviewer note: {note}" if note else "Re-triaged",
            )
            ticket = self._source.get(entry.ticket_key)
            if ticket is not None:
                self._log("reviewer", entry.ticket_key,
                          "Re-running triage with reviewer note as a hint.",
                          level=ActivityLevel.INFO)
                outcome = self.triage_ticket(ticket, hint=note or None)
                retriaged = True
            else:
                self._log("reviewer", entry.ticket_key,
                          "Could not re-run triage: ticket not found.",
                          level=ActivityLevel.ERROR)
            # Refresh the open-inbox count in any dashboards.
            self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))

        final_entry = self._inbox.get(entry_id) or entry
        return InboxResponse(
            entry=final_entry,
            note=note,
            repo=repo,
            commented=commented,
            comment_id=comment_id,
            taught=taught,
            retriaged=retriaged,
            outcome=outcome,
        )

    def note_poll_cycle(self, at=None) -> None:
        """Record that a polling cycle ran (owned here so metrics stay internal)."""
        self._metrics.poll_cycles += 1
        self._metrics.last_poll_at = at
        self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))

    def triage_ticket(self, ticket: Ticket, hint: str | None = None) -> TriageOutcome:
        """Run a single ticket through the full triage workflow.

        ``hint`` is an optional authoritative note from a human reviewer, used
        when re-triaging from the dashboard "respond" action.
        """
        classification = self._classify(ticket, hint)

        # 1. Confidence gate — unclear intent goes straight to a human.
        if not classification.is_confident:
            outcome = self._escalate(
                ticket,
                classification,
                reason=self._low_confidence_reason(classification),
                agent=None,
                stage=EscalationStage.CLASSIFICATION,
            )
            return self._finish(outcome)

        # 2. Resolve which repository the ticket belongs to.
        resolution = self._resolve(ticket, classification, hint)
        if self._require_repo and not resolution.is_confident:
            outcome = self._escalate(
                ticket,
                classification,
                reason=self._repo_reason(resolution),
                agent=None,
                stage=EscalationStage.REPOSITORY,
                resolution=resolution,
            )
            return self._finish(outcome)

        # 3. Route to a specialized worker agent.
        agent = self._router.route(classification)
        if agent is None:
            outcome = self._escalate(
                ticket,
                classification,
                reason=f"No worker agent registered for '{classification.category}'.",
                agent=None,
                resolution=resolution,
            )
            return self._finish(outcome)

        self._events.publish(TicketRouted(ticket_key=ticket.key, agent=agent.name))
        repo_note = f" (repo {resolution.repo})" if resolution.repo else ""
        self._log(agent.name, ticket.key,
                  f"Picked up {classification.category} ticket{repo_note}.")

        # 4. Initial validation by the specialized agent.
        result = agent.validate(ticket, classification, resolution)
        if not result.actionable or result.needs_human:
            outcome = self._escalate(
                ticket,
                classification,
                reason=result.summary,
                agent=agent.name,
                validation_details=result.details,
                stage=EscalationStage.VALIDATION,
                resolution=resolution,
            )
            return self._finish(outcome)

        # 5. Actionable: transition to In Progress and provision the workspace.
        outcome = self._transition(ticket, classification, agent.name, result, resolution)
        return self._finish(outcome)

    # -- workflow steps -------------------------------------------------------

    def _resolve(
        self, ticket: Ticket, classification: Classification, hint: str | None = None
    ) -> RepoResolution:
        resolution = self._resolver.resolve(ticket, classification, hint)
        self._events.publish(
            TicketRepoResolved(ticket_key=ticket.key, resolution=resolution)
        )
        if resolution.repo is not None:
            self._log(
                "resolver",
                ticket.key,
                f"Resolved repo {resolution.repo} "
                f"({resolution.confidence:.0%}, {resolution.source}).",
            )
        return resolution

    def _classify(self, ticket: Ticket, hint: str | None = None) -> Classification:
        classification = self._classifier.classify(ticket, hint)
        self._events.publish(
            TicketClassified(ticket=ticket, classification=classification)
        )
        suffix = " using reviewer hint" if hint else ""
        self._log(
            "classifier",
            ticket.key,
            f"Classified as {classification.category} "
            f"({classification.confidence:.0%} confidence) -> "
            f"{classification.target_project}{suffix}.",
        )
        return classification

    def _transition(
        self,
        ticket: Ticket,
        classification: Classification,
        agent: str,
        result,
        resolution: RepoResolution | None = None,
    ) -> TriageOutcome:
        try:
            updated = self._source.transition(ticket.key, TicketStatus.IN_PROGRESS)
        except Exception as exc:  # noqa: BLE001 - real workflows vary; degrade safely
            # The ticket is actionable but we could not move it (e.g. the
            # workflow has no "In Progress" transition). Surface it instead of
            # losing the result.
            return self._escalate(
                ticket,
                classification,
                reason=f"Validated but could not transition to In Progress: {exc}",
                agent=agent,
                resolution=resolution,
            )
        self._events.publish(
            TicketTransitioned(
                ticket_key=ticket.key,
                from_status=ticket.status,
                to_status=updated.status,
                agent=agent,
            )
        )
        self._log(
            agent,
            ticket.key,
            f"Validated and moved to In Progress: {result.summary}",
            level=ActivityLevel.SUCCESS,
        )
        # The worker agent "starts" by being handed a local workspace, then runs.
        workspace = self._provision(ticket, agent, resolution)
        work = self._run_work(ticket, classification, agent, resolution, workspace)
        return TriageOutcome(
            ticket_key=ticket.key,
            action=TriageAction.TRANSITIONED,
            classification=classification,
            agent=agent,
            validation=result,
            resolution=resolution,
            workspace=workspace,
            work=work,
            note=result.summary,
        )

    def _run_work(
        self,
        ticket: Ticket,
        classification: Classification,
        agent: str,
        resolution: RepoResolution | None,
        workspace: str,
    ):
        if self._work_runner is None:
            return None
        try:
            result = self._work_runner.run(ticket, classification, resolution, workspace)
        except Exception as exc:  # noqa: BLE001 - running the agent is best-effort
            self._log(agent, ticket.key, f"Work agent error: {exc}",
                      level=ActivityLevel.ERROR)
            return None
        self._events.publish(
            TicketWorkStarted(ticket_key=ticket.key, agent=agent, result=result)
        )
        if result.opened_pr:
            self._log(agent, ticket.key,
                      f"Opened pull request: {result.pr_url}",
                      level=ActivityLevel.SUCCESS)
        elif result.started:
            self._log(agent, ticket.key, result.summary, level=ActivityLevel.INFO)
        return result

    def _provision(
        self, ticket: Ticket, agent: str, resolution: RepoResolution | None
    ) -> str:
        if self._provisioner is None or resolution is None or resolution.repo is None:
            return ""
        try:
            workspace = self._provisioner.provision(resolution.repo, ticket.key)
        except Exception as exc:  # noqa: BLE001 - cloning is best-effort
            self._log(agent, ticket.key, f"Workspace provisioning failed: {exc}",
                      level=ActivityLevel.ERROR)
            return ""
        self._log(
            agent,
            ticket.key,
            f"Workspace ready for {resolution.repo} at {workspace}; agent starting.",
            level=ActivityLevel.SUCCESS,
        )
        return workspace

    def _escalate(
        self,
        ticket: Ticket,
        classification: Classification,
        *,
        reason: str,
        agent: str | None,
        validation_details: tuple[str, ...] = (),
        stage: EscalationStage = EscalationStage.CLASSIFICATION,
        resolution: RepoResolution | None = None,
    ) -> TriageOutcome:
        entry = InboxEntry(
            id=self._id_factory(),
            ticket_key=ticket.key,
            reason=reason,
            category=classification.category,
            confidence=classification.confidence,
            agent=agent,
            rationale=classification.rationale,
            details=tuple(validation_details),
            stage=stage,
            repo=resolution.repo if resolution else None,
        )
        self._inbox.add(entry)
        # Per the spec, exceptions are surfaced to the jiraya dashboard for human
        # review; the ticket's Jira status is deliberately left untouched.
        self._events.publish(TicketEscalated(entry=entry))
        self._log(
            agent or "triage",
            ticket.key,
            f"Surfaced for human review ({stage}): {reason}",
            level=ActivityLevel.WARNING,
        )
        return TriageOutcome(
            ticket_key=ticket.key,
            action=TriageAction.ESCALATED,
            classification=classification,
            agent=agent,
            resolution=resolution,
            note=reason,
        )


    def _finish(self, outcome: TriageOutcome) -> TriageOutcome:
        self._metrics.record(outcome)
        self._events.publish(TicketTriaged(outcome=outcome))
        self._events.publish(MetricsUpdated(metrics=self._metrics.snapshot()))
        return outcome

    # -- helpers --------------------------------------------------------------

    def _log(
        self,
        agent: str,
        ticket_key: str,
        message: str,
        *,
        level: ActivityLevel = ActivityLevel.INFO,
    ) -> None:
        self._events.publish(
            ActivityLogged(
                activity=AgentActivity(
                    agent=agent, ticket_key=ticket_key, message=message, level=level
                )
            )
        )

    @staticmethod
    def _low_confidence_reason(classification: Classification) -> str:
        if classification.category is TicketCategory.UNKNOWN:
            return "Classifier could not determine the ticket category."
        return (
            f"Low confidence ({classification.confidence:.0%}) on "
            f"'{classification.category}'."
        )

    @staticmethod
    def _repo_reason(resolution: RepoResolution) -> str:
        if resolution.repo is None:
            return ("Could not resolve which repository this ticket belongs to "
                    "— respond with a repo (clone URL) to unblock and teach jiraya.")
        return (
            f"Low confidence ({resolution.confidence:.0%}) resolving repository "
            f"'{resolution.repo}' — confirm or correct the repo to proceed."
        )
