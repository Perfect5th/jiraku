"""Routing: pick the worker agent responsible for a classified ticket."""

from __future__ import annotations

from ..domain import Classification, TicketCategory
from ..ports import WorkerAgent


class AgentRouter:
    """Maps a :class:`Classification` to the first agent that can handle it.

    Agents are consulted in registration order, so more specific agents should
    be registered before general fallbacks.
    """

    def __init__(self, agents: list[WorkerAgent] | None = None) -> None:
        self._agents: list[WorkerAgent] = list(agents or [])

    def register(self, agent: WorkerAgent) -> None:
        self._agents.append(agent)

    @property
    def agents(self) -> list[WorkerAgent]:
        return list(self._agents)

    def route(self, classification: Classification) -> WorkerAgent | None:
        for agent in self._agents:
            if agent.handles(classification.category):
                return agent
        return None

    def handles(self, category: TicketCategory) -> bool:
        return any(agent.handles(category) for agent in self._agents)
