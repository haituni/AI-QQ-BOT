"""Shared, cancellable HTTP queue; background work reserves capacity for chat."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager


class QueueFull(ValueError):
    pass


class ModelQueue:
    def __init__(self, capacity: int = 3, max_waiting: int = 100):
        self.capacity, self.max_waiting = capacity, max_waiting
        self.active = self.background_active = self._sequence = 0
        self._waiting: list[tuple[int, int, asyncio.Future]] = []

    @property
    def waiting(self) -> int:
        return sum(not future.done() for _, _, future in self._waiting)

    def _dispatch(self) -> None:
        self._waiting = [entry for entry in self._waiting if not entry[2].cancelled()]
        while self.active < self.capacity:
            eligible = [entry for entry in self._waiting
                        if not entry[2].done() and (entry[0] == 0 or self.background_active < 1)]
            if not eligible:
                return
            entry = min(eligible, key=lambda item: item[:2])
            self._waiting.remove(entry)
            self.active += 1
            self.background_active += entry[0]
            entry[2].set_result(None)

    @asynccontextmanager
    async def slot(self, *, background: bool = False):
        if self.waiting >= self.max_waiting:
            raise QueueFull('模型等待队列已满，请稍后重试')
        future = asyncio.get_running_loop().create_future()
        self._sequence += 1
        entry = (int(background), self._sequence, future)
        self._waiting.append(entry)
        self._dispatch()
        try:
            await future
            yield
        finally:
            # A grant can race cancellation before the waiter resumes.
            if future.done() and not future.cancelled():
                self.active -= 1
                self.background_active -= int(background)
            elif entry in self._waiting:
                self._waiting.remove(entry)
            self._dispatch()
