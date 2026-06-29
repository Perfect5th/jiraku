"""jiraku — agent-powered Jira triage agent with a TUI dashboard.

Implemented with a hexagonal architecture:

    domain/        pure business model (entities + events)
    ports/         abstract boundaries (inbound + outbound protocols)
    application/   the triage harness (service, router, poller)
    adapters/      concrete implementations (in-memory, jira, classifier, agents)
    tui/           Textual dashboard (a driving adapter)
    composition.py the composition root that wires it all together
"""

from __future__ import annotations

__version__ = "0.1.0"

from .composition import JiraConfig, JirakuConfig, JirakuSystem, build_system

__all__ = ["__version__", "JiraConfig", "JirakuConfig", "JirakuSystem", "build_system"]
