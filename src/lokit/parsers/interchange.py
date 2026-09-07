from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias, cast

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator

    from lokit.data.structure import BaseStructure, Data, StreamingStructure

NativeRecord: TypeAlias = tuple[
    bool,
    str,
    str,
    str | None,
    list[tuple[str, str]],
    str,
    dict[str, str],
    bytes | None,
]


class NativeReader(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def source_locale(self) -> str | None: ...

    @property
    def target_locale(self) -> str | None: ...

    @property
    def source_language(self) -> str | None: ...

    @property
    def target_language(self) -> str | None: ...

    @property
    def target_locales(self) -> list[str]: ...

    @property
    def target_languages(self) -> list[str]: ...

    @property
    def export_origin(self) -> str: ...

    @property
    def export_timestamp(self) -> str: ...

    @property
    def extensions(self) -> dict[str, str]: ...

    @property
    def closed(self) -> bool: ...

    def read_batch(self, batch_size: int = 256) -> list[NativeRecord]: ...

    def read_data_batch(
        self,
        batch_size: int = 256,
        runtime_placeholders: bool = False,
        inline_placeholders: bool = False,
        syntaxes: list[str] | None = None,
        domain: str | None = None,
    ) -> list[tuple[str, Data]]: ...

    def close(self) -> None: ...


class NativePoReader(Protocol):
    @property
    def source_locale(self) -> str: ...

    @property
    def target_locale(self) -> str | None: ...

    @property
    def source_language(self) -> str | None: ...

    @property
    def target_language(self) -> str | None: ...

    @property
    def target_locales(self) -> list[str]: ...

    @property
    def target_languages(self) -> list[str]: ...

    @property
    def export_origin(self) -> str: ...

    @property
    def export_timestamp(self) -> str: ...

    @property
    def extensions(self) -> dict[str, str]: ...

    @property
    def closed(self) -> bool: ...

    def read_batch(self, batch_size: int = 256) -> list[tuple[str, Data]]: ...

    def close(self) -> None: ...


_DEFAULT_BATCH_SIZE = 256


class _NamedBinaryStream(Protocol):
    name: str


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


class _NativeStreamingExporter(Protocol):
    def export(
        self,
        document: StreamingStructure,
        target_path: str | Path,
        output_format: str,
        *,
        cancellation: threading.Event | None = None,
    ) -> int | None: ...


class _NativeConversionFallback(Exception):
    pass


@dataclass(frozen=True, slots=True)
class NativeDocumentSnapshot:
    source_locale: str
    target_locale: str | None
    target_locales: tuple[str, ...]
    format_version: str
    export_origin: str
    export_timestamp: str
    source_language: str | None
    target_language: str | None
    target_languages: tuple[str, ...]
    extensions: tuple[tuple[str, str], ...]

    @classmethod
    def from_document(cls, document: StreamingStructure) -> NativeDocumentSnapshot:
        return cls(
            source_locale=document.source_locale,
            target_locale=document.target_locale,
            target_locales=document.target_locales,
            format_version=document.format_version,
            export_origin=document.export_origin,
            export_timestamp=document.export_timestamp,
            source_language=document.source_language,
            target_language=document.target_language,
            target_languages=document.target_languages,
            extensions=tuple(sorted(document.extensions.items())),
        )

    def matches(self, document: StreamingStructure) -> bool:
        return self == self.from_document(document)


class NativeInterchangeItems:
    def __init__(
        self,
        items: Iterator[tuple[str, Data]],
        source_path: str,
        input_format: str,
        source_language: str | None,
        target_language: str | None,
        mode: str,
        copy_if_same: bool,
        snapshot: NativeDocumentSnapshot,
        close_source: Callable[[], None],
    ) -> None:
        self._items = items
        self._source_path = source_path
        self._input_format = input_format
        self._source_language = source_language
        self._target_language = target_language
        self._mode = mode
        self._copy_if_same = copy_if_same
        self._snapshot = snapshot
        self._close_source = close_source
        self._started = False
        self._closed = False
        self._exporting = False

    def __iter__(self) -> NativeInterchangeItems:
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
        self._close_source()

    def export(
        self,
        document: StreamingStructure,
        target_path: str | Path,
        output_format: str,
        *,
        cancellation: threading.Event | None = None,
    ) -> int | None:
        if self._started or self._closed or self._exporting or not self._snapshot.matches(document):
            return None
        self._exporting = True
        try:
            count = convert_native_path(
                self._source_path,
                target_path,
                self._input_format,
                output_format,
                source_language=self._source_language,
                target_language=self._target_language,
                mode=self._mode,
                copy_if_same=self._copy_if_same and self._input_format == output_format,
                cancellation=cancellation,
            )
            if count is None:
                self._refresh_document_metadata(document)
        except BaseException:
            self.close()
            raise
        finally:
            self._exporting = False
        if count is not None:
            self.close()
        return count

    def _refresh_document_metadata(self, document: StreamingStructure) -> None:
        if self._input_format == "po":
            po_reader = open_native_po_reader(
                self._source_path,
                self._source_language,
                self._target_language,
                self._mode,
            )
            try:
                while po_reader.read_batch(_DEFAULT_BATCH_SIZE):
                    pass
                _apply_po_reader_metadata(document, po_reader)
            finally:
                po_reader.close()
            return
        xml_reader = open_native_reader(
            self._source_path,
            self._input_format,
            self._source_language,
            self._target_language,
            self._mode,
        )
        try:
            while xml_reader.read_batch(_DEFAULT_BATCH_SIZE):
                pass
            document.source_locale = xml_reader.source_locale or ""
            document.target_locale = xml_reader.target_locale
            document.target_locales = tuple(xml_reader.target_locales)
            document.export_origin = xml_reader.export_origin
            document.export_timestamp = xml_reader.export_timestamp
            document.source_language = xml_reader.source_language
            document.target_language = xml_reader.target_language
            document.target_languages = tuple(xml_reader.target_languages)
            extensions = xml_reader.extensions
            if self._input_format == "xliff":
                extensions["xliff_version"] = xml_reader.version
            document.extensions.clear()
            document.extensions.update(extensions)
        finally:
            xml_reader.close()


def attach_native_items(
    document: StreamingStructure,
    *,
    source_path: str,
    input_format: str,
    source_language: str | None,
    target_language: str | None,
    mode: str,
    copy_if_same: bool,
    close_source: Callable[[], None],
) -> None:
    native_items = NativeInterchangeItems(
        iter(document.items),
        source_path,
        input_format,
        source_language,
        target_language,
        mode,
        copy_if_same,
        NativeDocumentSnapshot.from_document(document),
        close_source,
    )
    document.items = cast("Iterator[tuple[str, Data]]", native_items)


def try_native_interchange_export(
    document: StreamingStructure,
    target_path: str | Path,
    output_format: str,
    *,
    group_by_resource: bool = False,
    cancellation: threading.Event | None = None,
) -> int | None:
    items: object = document.items
    if group_by_resource:
        return None
    if isinstance(items, NativeInterchangeItems):
        return items.export(document, target_path, output_format, cancellation=cancellation)
    if getattr(items, "_lokit_native_office", False) is True:
        return cast("_NativeStreamingExporter", items).export(
            document,
            target_path,
            output_format,
            cancellation=cancellation,
        )
    return None


def try_native_base_export(
    document: BaseStructure,
    target_path: str | Path,
    output_format: str,
    *,
    group_by_resource: bool = False,
    cancellation: threading.Event | None = None,
) -> int | None:
    if group_by_resource:
        return None
    if document.extensions.get("input_format") == "po":
        po_count = try_native_base_po_interchange_export(
            document,
            target_path,
            output_format,
            cancellation=cancellation,
        )
        if po_count is not None:
            return po_count
    from lokit._interchange_rust import export_base_interchange
    from lokit.io.atomic import atomic_output_path

    try:
        with atomic_output_path(Path(target_path), "wb", cancellation=cancellation) as stream:
            temporary_path = cast("_NamedBinaryStream", stream).name
            count = export_base_interchange(document, temporary_path, output_format)
            if count is None:
                raise _NativeConversionFallback
    except _NativeConversionFallback:
        return None
    return count


def try_native_document_export(
    document: BaseStructure | StreamingStructure,
    target_path: str | Path,
    output_format: str,
    *,
    group_by_resource: bool = False,
    resolve_placeholders: bool = True,
    cancellation: threading.Event | None = None,
) -> int | None:
    from lokit.data.structure import BaseStructure

    if not resolve_placeholders:
        return None
    from lokit.io.atomic import raise_if_cancelled

    raise_if_cancelled(cancellation)
    if isinstance(document, BaseStructure):
        return try_native_base_export(
            document,
            target_path,
            output_format,
            group_by_resource=group_by_resource,
            cancellation=cancellation,
        )
    return try_native_interchange_export(
        document,
        target_path,
        output_format,
        group_by_resource=group_by_resource,
        cancellation=cancellation,
    )


def try_native_base_po_interchange_export(
    document: BaseStructure,
    target_path: str | Path,
    output_format: str,
    *,
    cancellation: threading.Event | None = None,
) -> int | None:
    from lokit._interchange_rust import export_base_po_interchange
    from lokit.io.atomic import atomic_output_path

    try:
        with atomic_output_path(Path(target_path), "wb", cancellation=cancellation) as stream:
            temporary_path = cast("_NamedBinaryStream", stream).name
            count = export_base_po_interchange(document, temporary_path, output_format)
            if count is None:
                raise _NativeConversionFallback
    except _NativeConversionFallback:
        return None
    return count


def try_native_base_po_export(
    document: BaseStructure,
    target_path: str | Path,
    *,
    mode: str = "auto",
) -> int | None:
    from lokit._interchange_rust import export_base_po
    from lokit.io.atomic import atomic_output_path

    try:
        with atomic_output_path(Path(target_path), "wb") as stream:
            temporary_path = cast("_NamedBinaryStream", stream).name
            count = export_base_po(document, temporary_path, mode)
            if count is None:
                raise _NativeConversionFallback
    except _NativeConversionFallback:
        return None
    return count


def convert_native_path(
    source_path: str | Path,
    target_path: str | Path,
    input_format: str,
    output_format: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: str = "full",
    copy_if_same: bool = False,
    cancellation: threading.Event | None = None,
) -> int | None:
    from lokit._interchange_rust import convert_interchange
    from lokit.io.atomic import atomic_output_path

    try:
        with atomic_output_path(Path(target_path), "wb", cancellation=cancellation) as stream:
            temporary_path = cast("_NamedBinaryStream", stream).name
            count = convert_interchange(
                str(source_path),
                temporary_path,
                input_format,
                output_format,
                source_language,
                target_language,
                mode,
                copy_if_same,
            )
            if count is None:
                raise _NativeConversionFallback
    except _NativeConversionFallback:
        return None
    return count


def try_native_materialize(
    source_path: str | Path,
    input_format: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: str = "full",
    runtime_placeholders: bool = False,
    inline_placeholders: bool = False,
    syntaxes: list[str] | None = None,
) -> BaseStructure | None:
    from lokit._interchange_rust import materialize_interchange

    return materialize_interchange(
        str(source_path),
        input_format,
        source_language,
        target_language,
        domain,
        mode,
        runtime_placeholders,
        inline_placeholders,
        syntaxes,
    )


def try_native_po_materialize(
    source_path: str | Path,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    mode: str = "auto",
) -> BaseStructure:
    from lokit._interchange_rust import materialize_po

    return materialize_po(
        str(source_path),
        source_locale or None,
        target_locale,
        mode,
    )


def open_native_reader(
    path: str,
    format_name: str,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: str = "full",
) -> NativeReader:
    from lokit._interchange_rust import Reader

    return Reader(str(path), format_name, source_language, target_language, mode)


def open_native_po_reader(
    path: str,
    source_locale: str | None = None,
    target_locale: str | None = None,
    mode: str = "auto",
) -> NativePoReader:
    from lokit._interchange_rust import PoReader

    return PoReader(path, source_locale, target_locale, mode)


def iter_native_records(
    reader: NativeReader,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    on_batch: Callable[[], None] | None = None,
) -> Iterator[NativeRecord]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    try:
        while True:
            batch = reader.read_batch(batch_size)
            if on_batch is not None:
                on_batch()
            if not batch:
                return
            yield from batch
    finally:
        reader.close()


def iter_native_data(
    reader: NativeReader,
    *,
    runtime_placeholders: bool = False,
    inline_placeholders: bool = False,
    syntaxes: list[str] | None = None,
    domain: str | None = None,
    on_batch: Callable[[], None] | None = None,
) -> Iterator[tuple[str, Data]]:
    try:
        for batch in iter_native_data_batches(
            reader,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            syntaxes=syntaxes,
            domain=domain,
            on_batch=on_batch,
        ):
            yield from batch
    finally:
        reader.close()


def iter_native_data_batches(
    reader: NativeReader,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    runtime_placeholders: bool = False,
    inline_placeholders: bool = False,
    syntaxes: list[str] | None = None,
    domain: str | None = None,
    on_batch: Callable[[], None] | None = None,
) -> Iterator[list[tuple[str, Data]]]:
    try:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        while True:
            batch = reader.read_data_batch(batch_size, runtime_placeholders, inline_placeholders, syntaxes, domain)
            if on_batch is not None:
                on_batch()
            if not batch:
                return
            yield batch
    finally:
        reader.close()


def iter_native_po_records(
    reader: NativePoReader,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> Iterator[tuple[str, Data]]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    try:
        while True:
            batch = reader.read_batch(batch_size)
            if not batch:
                return
            yield from batch
    finally:
        reader.close()


def _apply_po_reader_metadata(document: StreamingStructure, reader: NativePoReader) -> None:
    document.source_locale = reader.source_locale
    document.target_locale = reader.target_locale
    document.target_locales = tuple(reader.target_locales)
    document.export_origin = reader.export_origin
    document.export_timestamp = reader.export_timestamp
    document.source_language = reader.source_language
    document.target_language = reader.target_language
    document.target_languages = tuple(reader.target_languages)
    document.extensions.clear()
    document.extensions.update(reader.extensions)


def native_backend_version() -> str:
    from lokit._interchange_rust import backend_version

    return backend_version()
