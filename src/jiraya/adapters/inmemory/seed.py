"""Sample tickets so jiraya is runnable and demoable with zero configuration."""

from __future__ import annotations

import itertools
import random

from ...domain import Priority, Ticket, TicketStatus

_DEMO_SPECS = [
    ("PROJ", "Crash when saving profile", "bug", "Bug",
     "Steps to reproduce:\n1. Open profile\n2. Click save\nExpected: saved. "
     "Actual: the app throws an exception and the page reloads."),
    ("WEB", "Add keyboard shortcuts", "enhancement", "Story",
     "Please add the ability to navigate the report grid with arrow keys. "
     "Would be nice for power users."),
    ("DOC", "Clarify the authentication guide", "documentation", "Documentation",
     "The authentication guide section on token refresh is confusing. "
     "Please add an example to the docs page."),
    ("SUP", "Need assistance", "", "Task",
     "Something seems off, not sure what. Can someone take a look?"),
    ("WEB", "Export breaks for large reports", "bug", "Bug",
     "The CSV export fails with a 500 error for reports over 10k rows. "
     "Stack trace attached. Steps: open a large report, click export."),
]

_counter = itertools.count(500)


def random_ticket() -> Ticket:
    """Synthesize a fresh untriaged ticket for live-dashboard demos."""
    project, summary, label, issue_type, description = random.choice(_DEMO_SPECS)
    number = next(_counter)
    return Ticket(
        key=f"{project}-{number}",
        project=project,
        summary=summary,
        description=description,
        reporter=random.choice(("alice", "bob", "carol", "dave", "erin")),
        priority=random.choice(list(Priority)),
        labels=(label,) if label else (),
        issue_type=issue_type,
    )


def sample_tickets() -> list[Ticket]:
    """A curated batch that exercises every triage outcome.

    Four tickets are cleanly actionable (transitioned to In Progress) and four
    require human review (escalated to the inbox) for distinct reasons.
    """
    return [
        Ticket(
            key="PROJ-101",
            project="PROJ",
            summary="Login button throws 500 error",
            description=(
                "Steps to reproduce:\n"
                "1. Go to /login\n"
                "2. Enter valid credentials and click Submit\n"
                "Expected: redirected to the dashboard.\n"
                "Actual: 500 error with a stack trace in the server log."
            ),
            reporter="alice",
            priority=Priority.HIGH,
            labels=("bug",),
        ),
        Ticket(
            key="PROJ-102",
            project="PROJ",
            summary="App is broken",
            description="It crashes sometimes. Please fix.",
            reporter="bob",
            priority=Priority.MEDIUM,
            labels=("bug",),
        ),
        Ticket(
            key="PROJ-103",
            project="PROJ",
            summary="NullPointerException on checkout",
            description=(
                "Getting an NPE when clicking checkout.\n"
                "Steps: add an item to the cart, go to checkout, observe the crash.\n"
                "Stack trace:\n  at CheckoutService.process(CheckoutService.java:42)"
            ),
            reporter="carol",
            priority=Priority.HIGHEST,
            labels=("bug",),
        ),
        Ticket(
            key="WEB-200",
            project="WEB",
            summary="Add dark mode support",
            description=(
                "Please add the ability to switch to a dark theme from settings. "
                "Would be nice for night-time usage and accessibility."
            ),
            reporter="dave",
            priority=Priority.LOW,
            labels=("enhancement",),
        ),
        Ticket(
            key="WEB-201",
            project="WEB",
            summary="Add CSV export of reports",
            description=(
                "Requesting CSV export for the reports page. "
                "Note: this might be a duplicate of an earlier request."
            ),
            reporter="erin",
            priority=Priority.MEDIUM,
            labels=("feature", "duplicate"),
        ),
        Ticket(
            key="DOC-300",
            project="DOC",
            summary="Fix typo in README install section",
            description=(
                "The README install section has a typo: 'instal' should be 'install'. "
                "Please update the documentation page."
            ),
            reporter="frank",
            priority=Priority.LOW,
            labels=("documentation",),
        ),
        Ticket(
            key="DOC-301",
            project="DOC",
            summary="Docs",
            description="Improve docs.",
            reporter="grace",
            priority=Priority.LOW,
            labels=("documentation",),
        ),
        Ticket(
            key="SUP-400",
            project="SUP",
            summary="Please help",
            description="I have an issue, can someone take a look when you get a chance?",
            reporter="heidi",
            priority=Priority.MEDIUM,
        ),
        # Already worked — proves fetch_untriaged filters out non-triageable tickets.
        Ticket(
            key="WEB-150",
            project="WEB",
            summary="Upgrade to React 19",
            description="Migration already underway.",
            reporter="ivan",
            priority=Priority.MEDIUM,
            status=TicketStatus.DONE,
            labels=("chore",),
        ),
    ]
