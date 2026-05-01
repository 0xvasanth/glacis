"""Test-only Classifier that returns scripted typed events."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from app.core.events import NormalizedEvent


class ScriptedClassifier:
    """Returns the next scripted typed event on each `classify` call.

    Two modes:
      - List of NormalizedEvent instances (FIFO).
      - A callable that maps payload -> NormalizedEvent.
    """

    def __init__(
        self,
        responses: list[NormalizedEvent] | None = None,
        *,
        responder: Callable[[dict[str, Any]], NormalizedEvent] | None = None,
    ):
        if responses is None and responder is None:
            raise ValueError("provide responses or responder")
        self._queue: deque[NormalizedEvent] = deque(responses or [])
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def classify(self, payload: dict[str, Any]) -> NormalizedEvent:
        self.calls.append(payload)
        if self._responder is not None:
            return self._responder(payload)
        if not self._queue:
            raise RuntimeError("ScriptedClassifier ran out of scripted responses")
        return self._queue.popleft()
