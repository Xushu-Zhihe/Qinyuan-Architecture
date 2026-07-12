import asyncio
from contextlib import asynccontextmanager


class ProjectEventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()

    async def publish(self, event: dict[str, object]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
