from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, StreamingStructure

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from lokit.data.structure import Data

Structure = BaseStructure | StreamingStructure


class _AsyncExportCancelled(Exception):
    pass


def export_lokit(document: Structure, filepath: str | Path) -> None:
    _export_lokit(document, filepath, None)


def _export_lokit(
    document: Structure,
    filepath: str | Path,
    cancellation: threading.Event | None,
) -> None:
    path = Path(filepath)
    with _atomic_native_path(path, cancellation) as temporary_path:
        from lokit._interchange_rust import LokitWriter

        writer = LokitWriter(
            str(temporary_path),
            document.source_locale,
            document.target_locale,
            document.target_locales,
            document.format_version,
            document.export_origin,
            document.export_timestamp,
            document.source_language,
            document.target_language,
            document.target_languages,
            document.extensions,
        )
        try:
            for unit_id, data in _iter_items(document):
                _raise_if_cancelled(cancellation)
                writer.write(unit_id, data)
            _raise_if_cancelled(cancellation)
            writer.close()
            _raise_if_cancelled(cancellation)
        except _AsyncExportCancelled:
            with contextlib.suppress(BaseException):
                writer.abort()
            return
        except BaseException:
            with contextlib.suppress(BaseException):
                writer.abort()
            raise


async def export_lokit_async(document: Structure, filepath: str | Path) -> None:
    cancellation = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(_export_lokit, document, filepath, cancellation))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation_error:
        cancellation.set()
        await _quiesce_cancelled_worker(worker)
        # An explicit exception is required here: mypyc can lose the active
        # exception across the awaited quiescence call when using a bare raise.
        raise cancellation_error


async def _quiesce_cancelled_worker(worker: asyncio.Task[None]) -> None:
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


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


@contextmanager
def _atomic_native_path(path: Path, cancellation: threading.Event | None) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        temporary_path = _create_temporary_path(path)
        yield temporary_path
        if _cancel_requested(cancellation):
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
            temporary_path = None
            return
        # Windows implements fsync via _commit, which requires a writable handle.
        with temporary_path.open("r+b") as temporary:
            os.fsync(temporary.fileno())
        if _cancel_requested(cancellation):
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
            temporary_path = None
            return
        os.replace(temporary_path, path)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            directory_descriptor = os.open(path.parent, directory_flag)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        if temporary_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
        raise


def _create_temporary_path(path: Path) -> Path:
    existing_mode: int | None = None
    if os.name != "nt":
        with contextlib.suppress(FileNotFoundError):
            existing_mode = stat.S_IMODE(path.stat().st_mode)

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    for _ in range(128):
        candidate = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(candidate, flags, 0o666)
        except FileExistsError:
            continue
        try:
            if existing_mode is not None:
                os.fchmod(descriptor, existing_mode)
        except BaseException:
            os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()
            raise
        os.close(descriptor)
        return candidate
    raise FileExistsError(f"could not allocate a temporary output beside {path}")


def _raise_if_cancelled(cancellation: threading.Event | None) -> None:
    if _cancel_requested(cancellation):
        raise _AsyncExportCancelled


def _cancel_requested(cancellation: threading.Event | None) -> bool:
    return cancellation is not None and cancellation.is_set()
