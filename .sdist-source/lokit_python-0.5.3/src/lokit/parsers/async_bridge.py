from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import TracebackType

T = TypeVar("T")


class _Closable(Protocol):
    def close(self) -> None: ...


class AsyncExtractionBatch(Generic[T]):
    __slots__ = ("done", "error", "items", "window_done")

    def __init__(
        self,
        items: list[T] | None = None,
        error: BaseException | None = None,
        done: bool = False,
        window_done: bool = False,
    ) -> None:
        self.items = items
        self.error = error
        self.done = done
        self.window_done = window_done


class AsyncExtractionBridge(Generic[T]):
    """Bounded, resumable async adapter for a synchronous iterator factory.

    A worker prefetches only a finite queue window before exiting. An early
    consumer break can therefore leave a resumable iterator and open resource,
    but never a producer thread blocked indefinitely on a full queue. Use the
    async context-manager form when an early break should close immediately.
    """

    def __init__(
        self,
        iterator_factory: Callable[[], Iterator[T]],
        maxsize: int = 4,
        batch_size: int = 128,
    ) -> None:
        self._stop = threading.Event()
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._iterator_factory = iterator_factory
        self._iterator: Iterator[T] | None = None
        self._batch_iterator_factory: Callable[[], Iterator[list[T]]] | None = None
        self._batch_iterator: Iterator[list[T]] | None = None
        self._queue: asyncio.Queue[AsyncExtractionBatch[T]] = asyncio.Queue(maxsize=maxsize)
        self._window_batches = max(1, maxsize - 1)
        self._batch_size = batch_size
        self._current_batch: list[T] = []
        self._batch_index = 0
        self._pending_error: BaseException | None = None
        self._done_after_batch = False
        self._producer: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    def from_batches(
        cls,
        batch_iterator_factory: Callable[[], Iterator[list[T]]],
        maxsize: int = 4,
    ) -> AsyncExtractionBridge[T]:
        bridge = cls(lambda: iter(()), maxsize=maxsize)
        bridge._batch_iterator_factory = batch_iterator_factory
        return bridge

    def __aiter__(self) -> AsyncExtractionBridge[T]:
        return self

    async def __aenter__(self) -> AsyncExtractionBridge[T]:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __del__(self) -> None:
        # A producer window is finite, but this also tells an in-flight window
        # to stop promptly when an unbound bridge is discarded after `break`.
        self._stop.set()

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration

        while self._batch_index >= len(self._current_batch):
            if self._pending_error is not None:
                error = self._pending_error
                self._pending_error = None
                await self.aclose()
                raise error
            if self._done_after_batch:
                await self.aclose()
                raise StopAsyncIteration

            if self._queue.empty():
                self._ensure_prefetch()
            result = await self._queue.get()
            if result.window_done:
                producer = self._producer
                if producer is not None:
                    await asyncio.shield(producer)
                    if self._producer is producer:
                        self._producer = None
                continue
            if result.items:
                self._current_batch = result.items
                self._batch_index = 0
                self._pending_error = result.error
                self._done_after_batch = result.done
                continue
            if result.error is not None:
                error = result.error
                await self.aclose()
                raise error
            if result.done:
                await self.aclose()
                raise StopAsyncIteration

        item = self._current_batch[self._batch_index]
        self._batch_index += 1
        return item

    async def aclose(self) -> None:
        close_task = self._close_task
        if close_task is None:
            self._closed = True
            self._stop.set()
            close_task = asyncio.create_task(self._close())
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close(self) -> None:
        producer = self._producer
        try:
            if producer is not None:
                await producer
        finally:
            self._producer = None
            await asyncio.to_thread(self._close_iterator)
            self._current_batch = []
            self._batch_index = 0
            self._pending_error = None
            self._done_after_batch = True
            while not self._queue.empty():
                self._queue.get_nowait()

    def _ensure_prefetch(self) -> None:
        if self._closed or self._stop.is_set() or self._pending_error is not None or self._done_after_batch:
            return
        producer = self._producer
        if producer is not None:
            if not producer.done():
                return
            self._producer = None
        if not self._queue.empty():
            return
        self._start(self._window_batches)

    def _start(self, max_batches: int) -> None:
        loop = asyncio.get_running_loop()

        def produce() -> None:
            batch_iterator_factory = self._batch_iterator_factory
            if batch_iterator_factory is not None:
                batch_iterator = self._batch_iterator
                if batch_iterator is None:
                    try:
                        batch_iterator = batch_iterator_factory()
                    except BaseException as exc:
                        self._put(loop, AsyncExtractionBatch(error=exc))
                        return
                    self._batch_iterator = batch_iterator
                for _ in range(max_batches):
                    if self._stop.is_set():
                        return
                    try:
                        native_batch = next(batch_iterator)
                    except StopIteration:
                        self._put(loop, AsyncExtractionBatch(done=True))
                        return
                    except BaseException as exc:
                        self._put(loop, AsyncExtractionBatch(error=exc))
                        return
                    if native_batch and not self._put(loop, AsyncExtractionBatch(items=native_batch)):
                        return
                self._put(loop, AsyncExtractionBatch(window_done=True))
                return

            iterator = self._iterator
            if iterator is None:
                try:
                    iterator = self._iterator_factory()
                except BaseException as exc:
                    self._put(loop, AsyncExtractionBatch(error=exc))
                    return
                self._iterator = iterator

            for _ in range(max_batches):
                batch: list[T] = []
                while len(batch) < self._batch_size:
                    if self._stop.is_set():
                        return
                    try:
                        item = next(iterator)
                    except StopIteration:
                        self._put(loop, AsyncExtractionBatch(items=batch or None, done=True))
                        return
                    except BaseException as exc:
                        self._put(loop, AsyncExtractionBatch(items=batch or None, error=exc))
                        return
                    batch.append(item)
                if not self._put(loop, AsyncExtractionBatch(items=batch)):
                    return
            self._put(loop, AsyncExtractionBatch(window_done=True))

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    def _put(
        self,
        loop: asyncio.AbstractEventLoop,
        result: AsyncExtractionBatch[T],
    ) -> bool:
        if self._stop.is_set():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._queue.put(result), loop)
        except RuntimeError:
            return False
        while True:
            try:
                future.result(timeout=0.05)
                return True
            except FutureTimeoutError:
                if self._stop.is_set():
                    future.cancel()
                    return False
            except FutureCancelledError:
                return False

    def _close_iterator(self) -> None:
        iterator = self._iterator
        self._iterator = None
        batch_iterator = self._batch_iterator
        self._batch_iterator = None
        self._close_candidate(iterator)
        self._close_candidate(batch_iterator)

    def _close_candidate(self, candidate: object) -> None:
        if hasattr(candidate, "close"):
            cast("_Closable", candidate).close()
