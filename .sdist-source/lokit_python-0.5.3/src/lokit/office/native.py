from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lokit.io.atomic import atomic_output_path
from lokit.parsers.interchange import NativeDocumentSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lokit.data.structure import Data, StreamingStructure


class _NamedBinaryStream(Protocol):
    name: str


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


class _NativeOfficeFallback(Exception):
    pass


class NativeOfficeItems:
    _lokit_native_office = True

    def __init__(
        self,
        items: Iterator[tuple[str, Data]],
        snapshot: NativeDocumentSnapshot,
    ) -> None:
        self._items = items
        self._snapshot = snapshot
        self._started = False
        self._closed = False
        self._exporting = False

    def __iter__(self) -> NativeOfficeItems:
        return self

    def __next__(self) -> object:
        if self._closed:
            raise StopIteration
        self._started = True
        try:
            return next(self._items)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        candidate: object = self._items
        if hasattr(candidate, "close"):
            cast("_ClosableIterator", candidate).close()

    def export(
        self,
        document: StreamingStructure,
        target_path: str | Path,
        output_format: str,
    ) -> int | None:
        if (
            output_format != "xliff"
            or self._started
            or self._closed
            or self._exporting
            or not self._snapshot.matches(document)
        ):
            return None
        self._exporting = True
        try:
            count = _export_native_office_stream(document, target_path)
        except BaseException:
            self.close()
            raise
        finally:
            self._exporting = False
        if count is not None:
            self.close()
        return count


def attach_native_office_items(document: StreamingStructure) -> None:
    document.items = cast(
        "Iterator[tuple[str, Data]]",
        NativeOfficeItems(
            iter(document.items),
            NativeDocumentSnapshot.from_document(document),
        ),
    )


def _export_native_office_stream(
    document: StreamingStructure,
    target_path: str | Path,
) -> int | None:
    from lokit._interchange_rust import export_stream_interchange

    try:
        with atomic_output_path(Path(target_path), "wb") as stream:
            temporary_path = cast("_NamedBinaryStream", stream).name
            count = export_stream_interchange(document, temporary_path, "xliff")
            if count is None:
                raise _NativeOfficeFallback
    except _NativeOfficeFallback:
        return None
    return count
