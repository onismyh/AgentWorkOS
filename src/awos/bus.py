"""Tiny in-process pub/sub used to push live updates to dashboard SSE clients."""
from __future__ import annotations

import queue
import threading
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        qu: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(qu)
        return qu

    def unsubscribe(self, qu: queue.Queue) -> None:
        with self._lock:
            if qu in self._subs:
                self._subs.remove(qu)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for qu in subs:
            try:
                qu.put_nowait(event)
            except queue.Full:
                pass


bus = Bus()
