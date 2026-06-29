"""A simple, thread-safe in-process event bus."""

from __future__ import annotations

import threading
from typing import Callable

from ...domain import DomainEvent
from ...ports import EventHandler


class InMemoryEventBus:
    """Synchronous fan-out bus implementing the ``EventBus`` port.

    ``publish`` invokes every subscriber immediately on the calling thread.
    Subscribers are responsible for their own thread-safety (the TUI marshals
    onto its event loop). A misbehaving subscriber cannot break publishing.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._lock = threading.Lock()

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        with self._lock:
            self._handlers.append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return _unsubscribe

    def publish(self, event: DomainEvent) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not stop the bus
                pass
