"""In-process asyncio queues that pass coins from Scanner to Risk."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Awaitable

Listener = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class Bus:
    """Named queues plus a fan-out of event listeners (for SSE later)."""

    def __init__(self) -> None:
        self.auditor: asyncio.Queue = asyncio.Queue()
        self.analyst: asyncio.Queue = asyncio.Queue()
        self.executor: asyncio.Queue = asyncio.Queue()
        self.risk: asyncio.Queue = asyncio.Queue()
        self.events: asyncio.Queue = asyncio.Queue()
        self._listeners: list[Listener] = []

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    async def emit(self, kind: str, payload: dict[str, Any]) -> None:
        item = {"kind": kind, **payload}
        await self.events.put(item)
        for fn in self._listeners:
            try:
                res = fn(kind, payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                # Listeners must never break the agents.
                pass


# Default process-wide bus; tests can construct their own.
bus = Bus()
