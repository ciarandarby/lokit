from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, TextIO, TypeVar, cast, overload

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType


_T = TypeVar("_T")


class AsyncExportCancelled(Exception):
    """Internal signal used to unwind a cancelled worker before commit."""


def raise_if_cancelled(cancellation: threading.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise AsyncExportCancelled


async def run_cancellable_export(worker_fn: Callable[[threading.Event], _T]) -> _T:
    """Run a synchronous exporter off-loop and quiesce it on cancellation."""
    cancellation = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(worker_fn, cancellation))
    was_cancelled = False
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Leave the exception handler before awaiting. mypyc otherwise keeps
        # the active exception state across the suspension point, which can
        # leak cancellation into later coroutines on the same event loop.
        was_cancelled = True
    if was_cancelled:
        cancellation.set()
        await _quiesce_cancelled_worker(worker)
        raise asyncio.CancelledError() from None
    raise RuntimeError("cancellable export exited without a result")


async def _quiesce_cancelled_worker(worker: asyncio.Task[_T]) -> None:
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except BaseException:
            return
    if not worker.cancelled():
        with contextlib.suppress(BaseException):
            worker.result()


@overload
def atomic_output_path(
    path: Path,
    mode: Literal[
        "w",
        "wt",
        "w+",
        "wt+",
        "a",
        "at",
        "a+",
        "at+",
        "x",
        "xt",
        "x+",
        "xt+",
    ],
    *,
    cancellation: threading.Event | None = None,
    encoding: str | None = None,
    newline: str | None = None,
) -> AbstractContextManager[TextIO]: ...


@overload
def atomic_output_path(
    path: Path,
    mode: Literal[
        "wb",
        "w+b",
        "wb+",
        "ab",
        "a+b",
        "ab+",
        "xb",
        "x+b",
        "xb+",
    ] = "wb",
    *,
    cancellation: threading.Event | None = None,
    encoding: None = None,
    newline: None = None,
) -> AbstractContextManager[BinaryIO]: ...


@overload
def atomic_output_path(
    path: Path,
    mode: str,
    *,
    cancellation: threading.Event | None = None,
    encoding: str | None = None,
    newline: str | None = None,
) -> AbstractContextManager[BinaryIO | TextIO]: ...


def atomic_output_path(
    path: Path,
    mode: str = "wb",
    *,
    cancellation: threading.Event | None = None,
    encoding: str | None = None,
    newline: str | None = None,
) -> AbstractContextManager[BinaryIO | TextIO]:
    return _AtomicOutput(path, mode, cancellation, encoding, newline)


class _AtomicOutput(AbstractContextManager[BinaryIO | TextIO]):
    def __init__(
        self,
        path: Path,
        mode: str,
        cancellation: threading.Event | None,
        encoding: str | None,
        newline: str | None,
    ) -> None:
        self._path = path
        self._mode = mode
        self._cancellation = cancellation
        self._encoding = encoding
        self._newline = newline
        self._stream: BinaryIO | TextIO | None = None
        self._temporary_path = ""

    def __enter__(self) -> BinaryIO | TextIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            mode=self._mode,
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
            encoding=self._encoding,
            newline=self._newline,
        )
        stream = cast("BinaryIO | TextIO", temporary)
        self._stream = stream
        self._temporary_path = temporary.name
        return stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream = self._stream
        temporary_path = self._temporary_path
        try:
            if stream is None or not temporary_path or exc_type is not None:
                return
            raise_if_cancelled(self._cancellation)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            raise_if_cancelled(self._cancellation)
            os.replace(temporary_path, self._path)
            self._temporary_path = ""
            _fsync_directory(self._path.parent)
        finally:
            if stream is not None and not stream.closed:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
            if self._temporary_path:
                with contextlib.suppress(FileNotFoundError):
                    Path(self._temporary_path).unlink()
            self._stream = None
            self._temporary_path = ""


def _fsync_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is not None:
        directory = os.open(path, directory_flag)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
