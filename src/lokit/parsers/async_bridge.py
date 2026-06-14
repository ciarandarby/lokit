from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

T = TypeVar("T")


class AsyncExtractionBatch(Generic[T]):
    __slots__ = ("done", "error", "items")

    def __init__(
        self,
        items: list[T] | None = None,
        error: BaseException | None = None,
        done: bool = False,
    ) -> None:
        self.items = items
        self.error = error
        self.done = done


class AsyncExtractionBridge(Generic[T]):
    def __init__(
        self,
        iterator_factory: Callable[[], Iterator[T]],
        maxsize: int = 4,
        batch_size: int = 1000,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._iterator_factory = iterator_factory
        self._queue: asyncio.Queue[AsyncExtractionBatch[T]] = asyncio.Queue(maxsize=maxsize)
        self._batch_size = batch_size
        self._current_batch: list[T] = []
        self._batch_index = 0
        self._stop = threading.Event()
        self._producer: asyncio.Task[None] | None = None

    def __aiter__(self) -> AsyncExtractionBridge[T]:
        return self

    async def __anext__(self) -> T:
        if self._producer is None:
            self._start()

        while self._batch_index >= len(self._current_batch):
            result = await self._queue.get()
            if result.done:
                await self.aclose()
                raise StopAsyncIteration
            if result.error is not None:
                await self.aclose()
                raise result.error
            if result.items is None:
                await self.aclose()
                raise StopAsyncIteration
            self._current_batch = result.items
            self._batch_index = 0

        item = self._current_batch[self._batch_index]
        self._batch_index += 1
        return item

    async def aclose(self) -> None:
        self._stop.set()
        if self._producer is not None:
            await self._producer
            self._producer = None

    def _start(self) -> None:
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                batch: list[T] = []
                for item in self._iterator_factory():
                    if self._stop.is_set():
                        break
                    batch.append(item)
                    if len(batch) >= self._batch_size:
                        self._put(loop, AsyncExtractionBatch(items=batch))
                        batch = []
                if batch:
                    self._put(loop, AsyncExtractionBatch(items=batch))
            except BaseException as exc:
                self._put(loop, AsyncExtractionBatch(error=exc))
            finally:
                self._put(loop, AsyncExtractionBatch(done=True))

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    def _put(
        self,
        loop: asyncio.AbstractEventLoop,
        result: AsyncExtractionBatch[T],
    ) -> None:
        if self._stop.is_set() and not result.done:
            return
        future = asyncio.run_coroutine_threadsafe(self._queue.put(result), loop)
        future.result()
