"""Event Bus: in-process asynchronous publish/subscribe.

A slow subscriber never blocks the publisher or other subscribers: each subscriber has
its own bounded queue, and when it overflows the oldest events are dropped and counted.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ts": self.ts, "data": self.data}


class Subscription:
    def __init__(self, bus: "EventBus", maxsize: int, prefixes: tuple[str, ...]) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.prefixes = prefixes
        self.dropped = 0

    def matches(self, event: Event) -> bool:
        return not self.prefixes or event.type.startswith(self.prefixes)

    def put(self, event: Event) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def close(self) -> None:
        self._bus._subs.discard(self)


class EventBus:
    """Publishing is synchronous and non-blocking; must be called from the event loop thread."""

    # Event types that are not kept in the "recent events" feed shown on the dashboard.
    _NOISY = ("metrics.", "server.log")

    def __init__(self, recent: int = 200) -> None:
        self._subs: set[Subscription] = set()
        self.recent: deque[Event] = deque(maxlen=recent)

    def subscribe(self, *prefixes: str, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, maxsize, prefixes)
        self._subs.add(sub)
        return sub

    def publish(self, type_: str, **data: Any) -> Event:
        event = Event(type_, data)
        if not type_.startswith(self._NOISY):
            self.recent.append(event)
        for sub in list(self._subs):
            if sub.matches(event):
                sub.put(event)
        return event
