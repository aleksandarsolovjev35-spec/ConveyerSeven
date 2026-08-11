"""Thread-safe in-process publish/subscribe event bus."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeAlias

EventHandler: TypeAlias = Callable[..., Any]


class EventBus:
    """Synchronous event bus that keeps UI adapters independent of equipment."""

    def __init__(self) -> None:
        """Create an empty, thread-safe subscription registry."""
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_name: str, handler: EventHandler) -> Callable[[], None]:
        """Subscribe a handler and return an idempotent unsubscribe function."""
        with self._lock:
            self._handlers[event_name].append(handler)

        def unsubscribe() -> None:
            """Remove this handler from its event channel."""
            with self._lock:
                handlers = self._handlers.get(event_name, [])
                if handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    def emit(self, event_name: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Synchronously publish an event and return subscriber results."""
        with self._lock:
            handlers = tuple(self._handlers.get(event_name, ()))
        return [handler(*args, **kwargs) for handler in handlers]
