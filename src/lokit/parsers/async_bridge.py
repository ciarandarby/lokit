from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

T = TypeVar("T")


@dataclass(slots=True)
class AsyncExtractionResult:
    item: object | None = None
    error: BaseException | None = None
    done: bool = False


class AsyncExtractionBridge(Generic[T]):
    def __init__(
        self,
        iterator_factory: Callable[[], Iterator[T]],
        maxsize: int = 1000,
    ) -> None:
        self._iterator_factory = iterator_factory
        self._queue: asyncio.Queue[AsyncExtractionResult] = asyncio.Queue(
            maxsize=maxsize
        )
        self._stop = threading.Event()
        self._producer: asyncio.Task[None] | None = None

    def __aiter__(self) -> AsyncExtractionBridge[T]:
        return self

    async def __anext__(self) -> T:
        if self._producer is None:
            self._start()

        result = await self._queue.get()
        if result.done:
            await self.aclose()
            raise StopAsyncIteration
        if result.error is not None:
            await self.aclose()
            raise result.error
        if result.item is None:
            await self.aclose()
            raise StopAsyncIteration
        return cast(T, result.item)

    async def aclose(self) -> None:
        self._stop.set()
        if self._producer is not None:
            await self._producer
            self._producer = None

    def _start(self) -> None:
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for item in self._iterator_factory():
                    if self._stop.is_set():
                        break
                    self._put(loop, AsyncExtractionResult(item=item))
            except BaseException as exc:
                self._put(loop, AsyncExtractionResult(error=exc))
            finally:
                self._put(loop, AsyncExtractionResult(done=True))

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    def _put(
        self,
        loop: asyncio.AbstractEventLoop,
        result: AsyncExtractionResult,
    ) -> None:
        if self._stop.is_set() and not result.done:
            return
        future = asyncio.run_coroutine_threadsafe(self._queue.put(result), loop)
        future.result()
