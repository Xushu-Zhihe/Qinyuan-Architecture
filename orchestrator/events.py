import asyncio
from collections import deque
from contextlib import asynccontextmanager
from time import monotonic


class ProjectEventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._published = 0
        self._dropped = 0
        self._backfilled = 0
        self._log_times: deque[float] = deque()

    async def publish(self, event: dict[str, object]) -> None:
        self._published += 1
        now = monotonic()
        if str(event.get("type", "")).endswith("log_chunk"):
            self._log_times.append(now)
            while self._log_times and now - self._log_times[0] > 60:
                self._log_times.popleft()
        for queue in tuple(self._subscribers):
            if queue.full():
                self._dropped += 1
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def record_backfill(self, count: int) -> None:
        self._backfilled += max(0, count)

    def metrics(self) -> dict[str, int | float]:
        now = monotonic()
        while self._log_times and now - self._log_times[0] > 60:
            self._log_times.popleft()
        depths = [queue.qsize() for queue in self._subscribers]
        return {
            "subscribers": len(depths),
            "queue_depth": max(depths, default=0),
            "queue_capacity": 500,
            "published_events": self._published,
            "dropped_events": self._dropped,
            "backfilled_events": self._backfilled,
            "log_events_last_minute": len(self._log_times),
        }

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
