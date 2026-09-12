from __future__ import annotations

import contextlib
import hashlib
import io
import os
import posixpath
import shutil
import sqlite3
import tempfile
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast

from lxml import etree
from tqdm import tqdm

from lokit.data.structure import AdjacentContext, BaseStructure, Data, Meta, StreamingStructure, TranslationStatus
from lokit.data.targets import StreamingTargetSplit, select_target
from lokit.diagnostics import _record
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import raise_if_cancelled, run_cancellable_export
from lokit.io.filenames import (
    FILENAME_COLLISION,
    FILENAME_TOO_LONG,
    RESERVED_LOCALE,
    TOO_MANY_OUTPUTS,
    LocaleFilenameError,
    locale_output_names,
)
from lokit.office.errors import (
    OfficePackageError,
    OfficeReinsertionError,
    OfficeUnsupportedPackageError,
    OfficeValidationError,
)
from lokit.office.models import DocumentSink, DocumentSource, OfficeExportResult, OfficeWarning
from lokit.office.options import (
    ExtraTranslationPolicy,
    MissingTranslationPolicy,
    OfficeExportOptions,
    OfficeImportOptions,
)
from lokit.office.process import extract_with_worker_iter, reinsert_with_worker, worker_available
from lokit.office.runtime import load_runtime_info
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator, Sequence

    from lxml.etree import _Element

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]

_COPY_BUFFER_BYTES = 1024 * 1024

CONTENT_TYPES = "[Content_Types].xml"
_MACRO_PACKAGE_ERROR = "Macro-enabled Office packages are not supported"
DOCX_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
}
PPTX_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
WORD_PART_PREFIXES = (
    "word/document.xml",
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
)
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PPTX_METADATA_PROPERTIES = frozenset(
    {
        "category",
        "contentStatus",
        "coverage",
        "creator",
        "description",
        "identifier",
        "keywords",
        "language",
        "publisher",
        "relation",
        "rights",
        "source",
        "subject",
        "title",
        "type",
    }
)


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: Path
    fingerprint: str
    cleanup: Path | None = None


@dataclass(frozen=True, slots=True)
class _OfficeUnit:
    unit_id: str
    data: Data
    part: str
    paragraph_index: int


@dataclass(slots=True)
class _RewriteProgress:
    units_consumed: int = 0


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


class _BoundedXmlWriter:
    """File-like XML sink that never retains or writes beyond a part limit."""

    def __init__(self, limit: int, part: str, *, retain: bool) -> None:
        self._limit = limit
        self._part = part
        self._buffer = io.BytesIO() if retain else None
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        if len(data) > self._limit - self.bytes_written:
            raise OfficeReinsertionError(f"Office rewritten XML part exceeds max_unit_bytes: {self._part}")
        if self._buffer is not None:
            self._buffer.write(data)
        self.bytes_written += len(data)
        return len(data)

    def flush(self) -> None:
        return

    def value(self) -> bytes:
        if self._buffer is None:
            raise OfficeReinsertionError("Office XML writer was not configured to retain output")
        return self._buffer.getvalue()


class _TranslationSpool:
    """Bounded, disk-backed translations used by the Python reinserter."""

    def __init__(
        self,
        translations: Iterable[ExtractItem],
        target_locale: str | None,
        options: OfficeExportOptions,
        cancellation: threading.Event | None,
    ) -> None:
        items = iter(translations)
        try:
            _validate_translation_limits(options)
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="lokit-office-translations-")
            self._connection = sqlite3.connect(Path(self._temporary_directory.name) / "translations.sqlite3")
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-2048")
            self._connection.execute(
                "CREATE TABLE translations ("
                "unit_id TEXT PRIMARY KEY, target TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"
            )
            self._load(items, target_locale, options, cancellation)
        except BaseException:
            self.close()
            raise
        finally:
            _close_iterator(items)

    def consume(self, unit_id: str) -> str | None:
        target = self.lookup(unit_id)
        if target is None:
            return None
        self._connection.execute(
            "UPDATE translations SET consumed = 1 WHERE unit_id = ?",
            (unit_id,),
        )
        return target

    def lookup(self, unit_id: str) -> str | None:
        raw_row: object = self._connection.execute(
            "SELECT target FROM translations WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
        if raw_row is None:
            return None
        if not isinstance(raw_row, tuple) or not raw_row or not isinstance(raw_row[0], str):
            raise OfficeReinsertionError("Office translation spool returned an invalid row")
        return raw_row[0]

    def extra_count(self) -> int:
        raw_row: object = self._connection.execute("SELECT COUNT(*) FROM translations WHERE consumed = 0").fetchone()
        if not isinstance(raw_row, tuple) or not raw_row or not isinstance(raw_row[0], int):
            raise OfficeReinsertionError("Office translation spool returned an invalid count")
        return raw_row[0]

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if isinstance(connection, sqlite3.Connection):
            connection.close()
        temporary_directory = getattr(self, "_temporary_directory", None)
        if isinstance(temporary_directory, tempfile.TemporaryDirectory):
            temporary_directory.cleanup()

    def _load(
        self,
        items: Iterator[ExtractItem],
        target_locale: str | None,
        options: OfficeExportOptions,
        cancellation: threading.Event | None,
    ) -> None:
        units_seen = 0
        bytes_seen = 0
        try:
            self._connection.execute("BEGIN")
            for unit_id, data in items:
                raise_if_cancelled(cancellation)
                target = _translation_text(data, target_locale)
                if target is None:
                    continue
                if len(target) > options.max_text_unit_chars:
                    raise OfficeReinsertionError("Office translation exceeds max_text_unit_chars")
                units_seen += 1
                if units_seen > options.max_translation_units:
                    raise OfficeReinsertionError(
                        f"Office translations exceed max_translation_units ({options.max_translation_units})"
                    )
                bytes_seen += len(unit_id.encode("utf-8")) + len(target.encode("utf-8"))
                if bytes_seen > options.max_translation_bytes:
                    raise OfficeReinsertionError(
                        f"Office translations exceed max_translation_bytes ({options.max_translation_bytes})"
                    )
                self._connection.execute(
                    "INSERT INTO translations (unit_id, target, consumed) VALUES (?, ?, 0) "
                    "ON CONFLICT(unit_id) DO UPDATE SET target = excluded.target, consumed = 0",
                    (unit_id, target),
                )
            raise_if_cancelled(cancellation)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise


class OfficeBackend:
    def extract(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        opts = options or OfficeImportOptions()
        with _materialize_source(source, f".{file_format}", opts) as source_file:
            if _use_worker():
                items: Iterator[ExtractItem] = _with_adjacent_context_items(
                    extract_with_worker_iter(
                        source_file.path,
                        file_format,
                        source_locale,
                        target_locale,
                        opts,
                    ),
                    source_file.fingerprint,
                )
            else:
                units = _iter_extract_units(source_file.path, file_format, source_file.fingerprint, opts)
                items = _with_adjacent_context(units)
            yield from _project_office_items(
                items,
                file_format=file_format,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )

    def stream(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = False,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> StreamingStructure:
        opts = options or OfficeImportOptions()
        source_path = str(source) if isinstance(source, (str, Path)) else ""
        items: Iterable[ExtractItem] = self.extract(
            source,
            file_format,
            source_locale,
            target_locale,
            opts,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
        if progress:
            items = tqdm(items, desc=f"Parsing {file_format.upper()}", unit="units")
        document = StreamingStructure(
            source_locale=source_locale,
            target_locale=target_locale,
            items=items,
            source_language=_base_language(source_locale),
            target_language=_base_language(target_locale),
            extensions=_document_extensions(file_format, "", source_path),
        )
        if not progress:
            from lokit.office.native import attach_native_office_items

            attach_native_office_items(document)
        return document

    def extract_async(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncExtractionBridge[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                source,
                file_format,
                source_locale,
                target_locale,
                options,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
        )

    def import_document(
        self,
        source: DocumentSource,
        file_format: str,
        source_locale: str,
        target_locale: str | None,
        options: OfficeImportOptions | None = None,
        progress: bool = True,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> BaseStructure:
        opts = options or OfficeImportOptions()
        with _materialize_source(source, f".{file_format}", opts) as source_file:
            source_fingerprint = source_file.fingerprint

            def on_document_start(value: str) -> None:
                nonlocal source_fingerprint
                source_fingerprint = value

            if _use_worker():
                raw_items = extract_with_worker_iter(
                    source_file.path,
                    file_format,
                    source_locale,
                    target_locale,
                    opts,
                    on_document_start=on_document_start,
                )
            else:
                units = _iter_extract_units(source_file.path, file_format, source_file.fingerprint, opts)
                raw_items = _with_adjacent_context(units)
            projected_items = _project_office_items(
                raw_items,
                file_format=file_format,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
            data = {
                unit_id: unit_data
                for unit_id, unit_data in tqdm(
                    projected_items,
                    desc=f"Parsing {file_format.upper()}",
                    unit="units",
                    disable=not progress,
                )
            }
            return BaseStructure(
                source_locale=source_locale,
                target_locale=target_locale,
                data=data,
                source_language=_base_language(source_locale),
                target_language=_base_language(target_locale),
                extensions=_document_extensions(file_format, source_fingerprint, str(source_file.path)),
            )

    def reinsert(
        self,
        document: BaseStructure | StreamingStructure,
        output: DocumentSink,
        file_format: str,
        source_document: DocumentSource | None = None,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        *,
        resolve_placeholders: bool = True,
        _cancellation: threading.Event | None = None,
    ) -> OfficeExportResult:
        raise_if_cancelled(_cancellation)
        opts = options or OfficeExportOptions()
        allow_worker_reinsertion = isinstance(document, BaseStructure)
        source = source_document or _source_document_from_extensions(document)
        if source is None:
            raise OfficeReinsertionError(
                f"{file_format.upper()} export requires source_{file_format} or document.extensions['source_file']"
            )
        selected_source = _selected_document(document, target_locale)
        selected = prepare_export_document(
            selected_source,
            resolve_placeholders=resolve_placeholders,
        )
        with _materialize_source(source, f".{file_format}", opts, _cancellation) as source_file:
            return _write_output(
                selected,
                output,
                file_format,
                source_file,
                opts,
                target_locale,
                _cancellation,
                allow_worker_reinsertion,
            )


def _project_office_items(
    items: Iterator[ExtractItem],
    *,
    file_format: str,
    include_tags: bool,
    tag_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> Iterator[ExtractItem]:
    if file_format == "docx":
        native_syntax = TagSyntax.DOCX
    elif file_format == "pptx":
        native_syntax = TagSyntax.PPTX
    else:
        raise OfficeUnsupportedPackageError(f"Unsupported Office format: {file_format}")
    return project_items(
        items,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        native_syntax=native_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


_BACKEND = OfficeBackend()


def import_docx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    """Parses DOCX into a structured BaseStructure."""
    return _BACKEND.import_document(
        source,
        "docx",
        source_locale,
        target_locale,
        options,
        progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def stream_docx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Asynchronously streams docx translation units."""
    return _BACKEND.stream(
        source,
        "docx",
        source_locale,
        target_locale,
        options,
        progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_docx_async(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Async generator streaming docx translation units."""
    return _BACKEND.extract_async(
        source,
        "docx",
        source_locale,
        target_locale,
        options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def export_docx(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_docx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Reinserts translated units back into a DOCX file."""
    return _BACKEND.reinsert(
        document,
        output,
        "docx",
        source_docx,
        target_locale,
        options,
        resolve_placeholders=resolve_placeholders,
    )


async def export_docx_async(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_docx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Async version of export docx with quiescent cancellation."""
    return await run_cancellable_export(
        lambda cancellation: _BACKEND.reinsert(
            document,
            output,
            "docx",
            source_docx,
            target_locale,
            options,
            resolve_placeholders=resolve_placeholders,
            _cancellation=cancellation,
        )
    )


def import_pptx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    """Parses PPTX into a structured BaseStructure."""
    return _BACKEND.import_document(
        source,
        "pptx",
        source_locale,
        target_locale,
        options,
        progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def stream_pptx(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Asynchronously streams pptx translation units."""
    return _BACKEND.stream(
        source,
        "pptx",
        source_locale,
        target_locale,
        options,
        progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_pptx_async(
    source: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Async generator streaming pptx translation units."""
    return _BACKEND.extract_async(
        source,
        "pptx",
        source_locale,
        target_locale,
        options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def export_pptx(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_pptx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Reinserts translated units back to a PPTX file."""
    return _BACKEND.reinsert(
        document,
        output,
        "pptx",
        source_pptx,
        target_locale,
        options,
        resolve_placeholders=resolve_placeholders,
    )


async def export_pptx_async(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    *,
    source_pptx: DocumentSource | None = None,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Async version of export pptx with quiescent cancellation."""
    return await run_cancellable_export(
        lambda cancellation: _BACKEND.reinsert(
            document,
            output,
            "pptx",
            source_pptx,
            target_locale,
            options,
            resolve_placeholders=resolve_placeholders,
            _cancellation=cancellation,
        )
    )


@contextlib.contextmanager
def _materialize_source(
    source: DocumentSource,
    suffix: str,
    options: OfficeImportOptions,
    cancellation: threading.Event | None = None,
) -> Iterator[_SourceFile]:
    raise_if_cancelled(cancellation)
    if isinstance(source, (str, Path)):
        path = Path(source)
        fingerprint = _sha256_file(path, options.max_compressed_bytes, cancellation)
        yield _SourceFile(path=path, fingerprint=fingerprint)
        return

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            digest = hashlib.sha256()
            written = 0
            if isinstance(source, bytes):
                written = len(source)
                if written > options.max_compressed_bytes:
                    raise OfficePackageError("Office source exceeds max_compressed_bytes")
                digest.update(source)
                tmp.write(source)
            else:
                while True:
                    raise_if_cancelled(cancellation)
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > options.max_compressed_bytes:
                        raise OfficePackageError("Office source exceeds max_compressed_bytes")
                    digest.update(chunk)
                    tmp.write(chunk)
        raise_if_cancelled(cancellation)
        yield _SourceFile(path=tmp_path, fingerprint=f"sha256:{digest.hexdigest()}", cleanup=tmp_path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _iter_extract_units(
    path: Path,
    file_format: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> Iterator[_OfficeUnit]:
    with zipfile.ZipFile(path, "r") as zf:
        names = _preflight_zip(zf, options)
        actual = _detect_ooxml_format(zf, names, options)
        if actual != file_format:
            raise OfficeUnsupportedPackageError(f"Expected {file_format.upper()} package, detected {actual.upper()}")
        parts = _docx_parts(names, options) if file_format == "docx" else _pptx_parts(zf, names, options)
        for part in parts:
            if part not in names:
                continue
            with zf.open(part) as stream:
                xml = stream.read(options.max_unit_bytes + 1)
            if len(xml) > options.max_unit_bytes:
                raise OfficePackageError(f"Office XML part exceeds max_unit_bytes: {part}")
            root = _parse_xml(xml, part)
            if file_format == "docx":
                yield from _extract_docx_part(root, part, fingerprint, options)
            else:
                yield from _extract_pptx_part(root, part, fingerprint, options)


def _preflight_zip(zf: zipfile.ZipFile, options: OfficeImportOptions) -> set[str]:
    infos = zf.infolist()
    if len(infos) > options.max_zip_entries:
        raise OfficePackageError("Office package has too many ZIP entries")
    seen: set[str] = set()
    compressed = 0
    uncompressed = 0
    for info in infos:
        name = _normalize_part_name(info.filename)
        if not name or name != info.filename:
            raise OfficePackageError(f"Unsafe Office ZIP entry name: {info.filename!r}")
        if name in seen:
            raise OfficePackageError(f"Duplicate Office ZIP entry: {name}")
        seen.add(name)
        compressed += info.compress_size
        uncompressed += info.file_size
        if compressed > options.max_compressed_bytes:
            raise OfficePackageError("Office package exceeds max_compressed_bytes")
        if uncompressed > options.max_uncompressed_bytes:
            raise OfficePackageError("Office package exceeds max_uncompressed_bytes")
        if info.compress_size and info.file_size / info.compress_size > options.max_compression_ratio:
            raise OfficePackageError(f"Suspicious compression ratio in Office ZIP entry: {name}")
        if info.flag_bits & 0x1:
            raise OfficeUnsupportedPackageError("Encrypted Office packages are not supported")
        if _is_vba_project_part(name):
            raise OfficeUnsupportedPackageError(_MACRO_PACKAGE_ERROR)
    if CONTENT_TYPES not in seen:
        raise OfficePackageError("Office package is missing [Content_Types].xml")
    return seen


def _normalize_part_name(name: str) -> str:
    if name.startswith("/") or "\\" in name or "\x00" in name:
        return ""
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        return ""
    return normalized


def _detect_ooxml_format(
    zf: zipfile.ZipFile,
    names: set[str],
    options: OfficeImportOptions,
) -> str:
    with zf.open(CONTENT_TYPES) as stream:
        content_types_xml = stream.read(options.max_unit_bytes + 1)
    if len(content_types_xml) > options.max_unit_bytes:
        raise OfficePackageError(f"Office XML part exceeds max_unit_bytes: {CONTENT_TYPES}")
    root = _parse_xml(content_types_xml, CONTENT_TYPES)
    content_types: dict[str, str] = {}
    for child in root:
        if _local_name(child.tag) not in {"Default", "Override"}:
            continue
        content_type = child.get("ContentType") or ""
        if _is_macro_content_type(content_type):
            raise OfficeUnsupportedPackageError(_MACRO_PACKAGE_ERROR)
        if _local_name(child.tag) != "Override":
            continue
        part_name = (child.get("PartName") or "").lstrip("/")
        content_types[part_name] = content_type
    if any(content_type in DOCX_MAIN_TYPES for content_type in content_types.values()) or "word/document.xml" in names:
        return "docx"
    if (
        any(content_type in PPTX_MAIN_TYPES for content_type in content_types.values())
        or "ppt/presentation.xml" in names
    ):
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if any(name.startswith("Stories/") for name in names):
        return "idml"
    raise OfficeUnsupportedPackageError("Unsupported OOXML package type")


def _is_vba_project_part(name: str) -> bool:
    return name.rsplit("/", 1)[-1].casefold() == "vbaproject.bin"


def _is_macro_content_type(content_type: str) -> bool:
    normalized = content_type.casefold()
    return "macroenabled" in normalized or "vbaproject" in normalized


def _docx_parts(names: set[str], options: OfficeImportOptions) -> list[str]:
    parts = ["word/document.xml"]
    if options.include_headers_footers:
        parts.extend(
            sorted(name for name in names if name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))
        )
    if options.include_comments and "word/comments.xml" in names:
        parts.append("word/comments.xml")
    for name in ("word/footnotes.xml", "word/endnotes.xml"):
        if name in names:
            parts.append(name)
    return parts


def _pptx_parts(zf: zipfile.ZipFile, names: set[str], options: OfficeImportOptions) -> list[str]:
    slides = _presentation_slide_parts(zf, names, options)
    if not slides:
        slides = _matching_pptx_parts(names, "ppt/slides/slide", numeric=True)
    if not options.include_hidden_slides:
        slides = _visible_pptx_slides(zf, slides, options)
    notes = (
        _matching_pptx_parts(names, "ppt/notesSlides/notesSlide", numeric=True)
        if options.include_hidden_slides
        else _related_pptx_parts(zf, names, slides, "/notesSlide", options)
    )
    layouts = _related_pptx_parts(zf, names, slides, "/slideLayout", options)
    if not layouts and options.include_hidden_slides:
        layouts = _matching_pptx_parts(names, "ppt/slideLayouts/slideLayout")
    masters = _related_pptx_parts(zf, names, layouts, "/slideMaster", options)
    if not masters and options.include_hidden_slides:
        masters = _matching_pptx_parts(names, "ppt/slideMasters/slideMaster")
    notes_masters = _related_pptx_parts(zf, names, notes, "/notesMaster", options)
    if not notes_masters and options.include_hidden_slides:
        notes_masters = _matching_pptx_parts(names, "ppt/notesMasters/notesMaster")

    parts: list[str] = []
    if options.include_slides or options.include_alt_text:
        _extend_distinct(parts, slides)
    if (options.include_speaker_notes and options.include_notes) or options.include_alt_text:
        _extend_distinct(parts, notes)
    if (options.include_slide_layouts and options.include_master_layout_content) or options.include_alt_text:
        _extend_distinct(parts, layouts)
    if (options.include_slide_masters and options.include_master_layout_content) or options.include_alt_text:
        _extend_distinct(parts, masters)
    if options.include_notes_masters or options.include_alt_text:
        _extend_distinct(parts, notes_masters)
    if options.include_handout_masters or options.include_alt_text:
        _extend_distinct(parts, _matching_pptx_parts(names, "ppt/handoutMasters/handoutMaster"))
    if options.include_comments:
        comments = (
            _matching_pptx_parts(names, "ppt/comments/comment")
            if options.include_hidden_slides
            else _related_pptx_parts(zf, names, slides, "/comments", options)
        )
        _extend_distinct(parts, comments)
    if options.include_charts:
        charts = (
            _matching_pptx_parts(names, "ppt/charts/chart", numeric=True)
            if options.include_hidden_slides
            else _related_pptx_parts(zf, names, slides, "/chart", options)
        )
        _extend_distinct(parts, charts)
    if options.include_diagrams:
        diagrams = (
            _matching_pptx_parts(names, "ppt/diagrams/data", numeric=True)
            if options.include_hidden_slides
            else _related_pptx_parts(zf, names, slides, "/diagramData", options)
        )
        _extend_distinct(parts, diagrams)
    if options.include_document_metadata:
        _extend_distinct(parts, _matching_pptx_parts(names, "docProps/core"))
        _extend_distinct(parts, _matching_pptx_parts(names, "docProps/custom"))
    return parts


def _visible_pptx_slides(
    zf: zipfile.ZipFile,
    slides: list[str],
    options: OfficeImportOptions,
) -> list[str]:
    visible: list[str] = []
    for slide in slides:
        root = _read_office_xml(zf, slide, options)
        if not _is_hidden_slide(root, slide):
            visible.append(slide)
    return visible


def _presentation_slide_parts(
    zf: zipfile.ZipFile,
    names: set[str],
    options: OfficeImportOptions,
) -> list[str]:
    if "ppt/presentation.xml" not in names or "ppt/_rels/presentation.xml.rels" not in names:
        return []
    rels = _read_relationships(zf, "ppt/_rels/presentation.xml.rels", options)
    root = _read_office_xml(zf, "ppt/presentation.xml", options)
    parts: list[str] = []
    for slide_id in root.iter(f"{{{PRESENTATION_NS}}}sldId"):
        rel_id = slide_id.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if rel_id is None:
            continue
        target = rels.get(rel_id, "")
        part = _resolve_relationship_target("ppt/presentation.xml", target)
        if part in names:
            _extend_distinct(parts, [part])
    return parts


def _read_relationships(
    zf: zipfile.ZipFile,
    part: str,
    options: OfficeImportOptions,
) -> dict[str, str]:
    root = _read_office_xml(zf, part, options)
    relationships: dict[str, str] = {}
    for child in root.iter(f"{{{REL_NS}}}Relationship"):
        rel_id = child.get("Id")
        target = child.get("Target")
        mode = child.get("TargetMode")
        if rel_id and target and mode != "External":
            relationships[rel_id] = target
    return relationships


def _related_pptx_parts(
    zf: zipfile.ZipFile,
    names: set[str],
    source_parts: list[str],
    relationship_suffix: str,
    options: OfficeImportOptions,
) -> list[str]:
    parts: list[str] = []
    for source_part in source_parts:
        relationship_part = _relationship_part(source_part)
        if relationship_part not in names:
            continue
        root = _read_office_xml(zf, relationship_part, options)
        for child in root.iter(f"{{{REL_NS}}}Relationship"):
            relationship_type = child.get("Type") or ""
            target = child.get("Target")
            mode = child.get("TargetMode")
            if target and mode != "External" and relationship_type.endswith(relationship_suffix):
                part = _resolve_relationship_target(source_part, target)
                if part in names and part not in parts:
                    parts.append(part)
    return parts


def _read_office_xml(
    zf: zipfile.ZipFile,
    part: str,
    options: OfficeImportOptions,
) -> _Element:
    with zf.open(part) as stream:
        xml = stream.read(options.max_unit_bytes + 1)
    if len(xml) > options.max_unit_bytes:
        raise OfficePackageError(f"Office XML part exceeds max_unit_bytes: {part}")
    return _parse_xml(xml, part)


def _relationship_part(source_part: str) -> str:
    directory = posixpath.dirname(source_part)
    filename = posixpath.basename(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _matching_pptx_parts(names: set[str], prefix: str, *, numeric: bool = False) -> list[str]:
    parts = [name for name in names if name.startswith(prefix) and name.endswith(".xml") and "/_rels/" not in name]
    if numeric:
        return sorted(parts, key=_pptx_numeric_part_key)
    return sorted(parts)


def _pptx_numeric_part_key(part: str) -> tuple[int, str]:
    digits = _slide_number(part)
    return (int(digits) if digits else 0, part)


def _extend_distinct(parts: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate not in parts:
            parts.append(candidate)


def _resolve_relationship_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        resolved = target.lstrip("/")
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if resolved.startswith("../") or resolved == "..":
        raise OfficePackageError(f"Relationship target escapes package: {target}")
    return resolved


def _extract_docx_part(
    root: _Element,
    part: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    container = _docx_container(part)
    units: list[_OfficeUnit] = []
    paragraph_index = 0
    for paragraph in root.iter(f"{{{WORD_NS}}}p"):
        text = _docx_paragraph_text(paragraph)
        if not text.strip():
            paragraph_index += 1
            continue
        if len(text) > options.max_text_unit_chars:
            raise OfficePackageError("DOCX text unit exceeds max_text_unit_chars")
        unit_id = f"docx:{container}:p/{paragraph_index}"
        units.append(
            _OfficeUnit(
                unit_id,
                _office_data(text, "docx", part, container, fingerprint),
                part,
                paragraph_index,
            )
        )
        paragraph_index += 1
    if options.include_alt_text:
        units.extend(_extract_alt_text(root, "docx", part, container, fingerprint, len(units)))
    return units


def _docx_container(part: str) -> str:
    if part == "word/document.xml":
        return "body"
    if part.startswith("word/header"):
        return f"header/{Path(part).stem.removeprefix('header') or 'default'}"
    if part.startswith("word/footer"):
        return f"footer/{Path(part).stem.removeprefix('footer') or 'default'}"
    if part == "word/comments.xml":
        return "comment"
    return Path(part).stem


def _docx_paragraph_text(paragraph: _Element) -> str:
    parts: list[str] = []
    for element in paragraph.iter():
        local = _local_name(element.tag)
        if local == "t" and element.text:
            parts.append(element.text)
        elif local == "tab":
            parts.append("\t")
        elif local in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _extract_pptx_part(
    root: _Element,
    part: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    if not options.include_hidden_slides and _is_hidden_slide(root, part):
        return []
    container = _pptx_container(part)
    units: list[_OfficeUnit] = []
    if _pptx_area_enabled(part, options):
        if _is_pptx_metadata_part(part):
            units.extend(_extract_pptx_metadata(root, part, container, fingerprint, options))
        elif _is_pptx_comment_part(part):
            units.extend(_extract_pptx_comments(root, part, container, fingerprint, options))
        else:
            units.extend(_extract_pptx_paragraphs(root, part, container, fingerprint, options))
    if options.include_alt_text:
        units.extend(_extract_pptx_alt_text(root, part, container, fingerprint, options))
    return units


def _extract_pptx_paragraphs(
    root: _Element,
    part: str,
    container: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for paragraph_index, paragraph in enumerate(root.iter(f"{{{DRAWING_NS}}}p")):
        text = _pptx_paragraph_text(paragraph)
        if not text or (not text.strip() and _pptx_area(part) != "diagrams"):
            continue
        _validate_pptx_text(text, options)
        unit_id = f"pptx:{container}:p/{paragraph_index}"
        data = _office_data(text, "pptx", part, container, fingerprint)
        data.extensions["office.area"] = _pptx_area(part)
        if part.startswith(("ppt/slides/slide", "ppt/notesSlides/notesSlide")):
            slide_number = _slide_number(part)
            if slide_number:
                data.extensions["office.slide_number"] = slide_number
        units.append(_OfficeUnit(unit_id, data, part, paragraph_index))
    return units


def _extract_pptx_comments(
    root: _Element,
    part: str,
    container: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for element_index, element in enumerate(root.iter()):
        if _local_name(element.tag) != "text":
            continue
        text = element.text or ""
        if not text.strip():
            continue
        _validate_pptx_text(text, options)
        unit_id = f"pptx:{container}:comment/{element_index}"
        data = _office_data(text, "pptx", part, container, fingerprint)
        data.extensions["office.area"] = "comments"
        data.extensions["office.node_kind"] = "comment"
        units.append(_OfficeUnit(unit_id, data, part, element_index))
    return units


def _extract_pptx_metadata(
    root: _Element,
    part: str,
    container: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for element_index, element in enumerate(root.iter()):
        property_name = _pptx_metadata_property(element, part)
        if property_name is None:
            continue
        text = element.text or ""
        if not text.strip():
            continue
        _validate_pptx_text(text, options)
        unit_id = f"pptx:{container}:property/{property_name}/{element_index}"
        data = _office_data(text, "pptx", part, container, fingerprint)
        data.extensions["office.area"] = "document_metadata"
        data.extensions["office.node_kind"] = "metadata"
        data.extensions["office.property"] = property_name
        units.append(_OfficeUnit(unit_id, data, part, element_index))
    return units


def _extract_pptx_alt_text(
    root: _Element,
    part: str,
    container: str,
    fingerprint: str,
    options: OfficeImportOptions,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for element_index, element in enumerate(root.iter()):
        attributes = ["title", "descr"]
        for attribute in attributes:
            text = element.get(attribute) or ""
            if not text.strip():
                continue
            _validate_pptx_text(text, options)
            unit_id = f"pptx:{container}:alt/{element_index}/{attribute}"
            data = _office_data(text, "pptx", part, container, fingerprint)
            data.extensions["office.area"] = "alt_text"
            data.extensions["office.alt_text"] = "true"
            data.extensions["office.attribute"] = attribute
            units.append(_OfficeUnit(unit_id, data, part, element_index))
    return units


def _validate_pptx_text(text: str, options: OfficeImportOptions) -> None:
    if len(text) > options.max_text_unit_chars:
        raise OfficePackageError("PPTX text unit exceeds max_text_unit_chars")


def _pptx_area_enabled(part: str, options: OfficeImportOptions) -> bool:
    area = _pptx_area(part)
    if area == "slides":
        return options.include_slides
    if area == "speaker_notes":
        return options.include_speaker_notes and options.include_notes
    if area == "slide_masters":
        return options.include_slide_masters and options.include_master_layout_content
    if area == "slide_layouts":
        return options.include_slide_layouts and options.include_master_layout_content
    if area == "notes_masters":
        return options.include_notes_masters
    if area == "handout_masters":
        return options.include_handout_masters
    if area == "comments":
        return options.include_comments
    if area == "charts":
        return options.include_charts
    if area == "diagrams":
        return options.include_diagrams
    if area == "document_metadata":
        return options.include_document_metadata
    return False


def _pptx_area(part: str) -> str:
    if part.startswith("ppt/slides/slide"):
        return "slides"
    if part.startswith("ppt/notesSlides/notesSlide"):
        return "speaker_notes"
    if part.startswith("ppt/slideMasters/slideMaster"):
        return "slide_masters"
    if part.startswith("ppt/slideLayouts/slideLayout"):
        return "slide_layouts"
    if part.startswith("ppt/notesMasters/notesMaster"):
        return "notes_masters"
    if part.startswith("ppt/handoutMasters/handoutMaster"):
        return "handout_masters"
    if _is_pptx_comment_part(part):
        return "comments"
    if part.startswith("ppt/charts/chart"):
        return "charts"
    if part.startswith("ppt/diagrams/data"):
        return "diagrams"
    if _is_pptx_metadata_part(part):
        return "document_metadata"
    return ""


def _is_pptx_comment_part(part: str) -> bool:
    return part.startswith("ppt/comments/comment")


def _is_pptx_metadata_part(part: str) -> bool:
    return part.startswith(("docProps/core", "docProps/custom"))


def _pptx_metadata_property(element: _Element, part: str) -> str | None:
    local = _local_name(element.tag)
    if part.startswith("docProps/core"):
        return local if local in PPTX_METADATA_PROPERTIES else None
    parent = element.getparent()
    if parent is not None and _local_name(parent.tag) == "property" and len(element) == 0:
        return parent.get("name") or local
    return None


def _is_hidden_slide(root: _Element, part: str) -> bool:
    if not part.startswith("ppt/slides/slide"):
        return False
    show = root.get("show")
    return show is not None and show.strip().lower() in {"0", "false", "off", "no"}


def _pptx_container(part: str) -> str:
    if part.startswith("ppt/slides/slide"):
        return f"slide/{_slide_number(part)}"
    if part.startswith("ppt/notesSlides/notesSlide"):
        return f"slide/{_slide_number(part)}:notes"
    if part.startswith("ppt/slideLayouts/"):
        return f"layout/{Path(part).stem}"
    if part.startswith("ppt/slideMasters/"):
        return f"master/{Path(part).stem}"
    if part.startswith("ppt/notesMasters/"):
        return f"notes-master/{Path(part).stem}"
    if part.startswith("ppt/handoutMasters/"):
        return f"handout-master/{Path(part).stem}"
    if part.startswith("ppt/comments/"):
        return f"comment/{Path(part).stem}"
    if part.startswith("ppt/charts/"):
        return f"chart/{Path(part).stem}"
    if part.startswith("ppt/diagrams/"):
        return f"diagram/{Path(part).stem}"
    if part.startswith("docProps/"):
        return f"metadata/{Path(part).stem}"
    return Path(part).stem


def _slide_number(part: str) -> str:
    stem = Path(part).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return digits


def _pptx_paragraph_text(paragraph: _Element) -> str:
    parts: list[str] = []
    for element in paragraph.iter():
        local = _local_name(element.tag)
        in_generated_field = any(_local_name(ancestor.tag) == "fld" for ancestor in element.iterancestors())
        if local == "t" and element.text and not in_generated_field:
            parts.append(element.text)
        elif local == "br" and not in_generated_field:
            parts.append("\n")
    return "".join(parts)


def _extract_alt_text(
    root: _Element,
    file_format: str,
    part: str,
    container: str,
    fingerprint: str,
    offset: int,
) -> list[_OfficeUnit]:
    units: list[_OfficeUnit] = []
    for index, element in enumerate(root.iter()):
        descr = element.get("descr")
        title = element.get("title")
        text = descr or title
        if not text or not text.strip():
            continue
        unit_id = f"{file_format}:{container}:alt/{offset + index}"
        data = _office_data(text, file_format, part, container, fingerprint)
        data.extensions["office.alt_text"] = "true"
        units.append(_OfficeUnit(unit_id, data, part, -1))
    return units


def _office_data(
    text: str,
    file_format: str,
    part: str,
    container: str,
    fingerprint: str,
) -> Data:
    return Data(
        source=text,
        meta=Meta(),
        status=TranslationStatus.UNKNOWN,
        extensions={
            "input_format": file_format,
            "office.format": file_format,
            "office.part": part,
            "office.container": container,
            "office.source_fingerprint": fingerprint,
        },
    )


def _with_adjacent_context(units: Iterable[_OfficeUnit]) -> Iterator[ExtractItem]:
    items = iter(units)
    try:
        previous: _OfficeUnit | None = None
        current = next(items, None)
        while current is not None:
            next_unit = next(items, None)
            if previous is not None and previous.part == current.part:
                current.data.previous_context = AdjacentContext(previous.unit_id, previous.data.source)
            if next_unit is not None and next_unit.part == current.part:
                current.data.next_context = AdjacentContext(next_unit.unit_id, next_unit.data.source)
            yield current.unit_id, current.data
            previous = current
            current = next_unit
    finally:
        _close_iterator(items)


def _with_adjacent_context_items(
    items: Iterator[ExtractItem],
    fingerprint: str,
) -> Iterator[ExtractItem]:
    try:
        previous: ExtractItem | None = None
        current: ExtractItem | None = next(items, None)
        while current is not None:
            next_item = next(items, None)
            unit_id, data = current
            data.extensions.setdefault("office.source_fingerprint", fingerprint)
            if previous is not None and _office_part(previous[1]) == _office_part(data):
                data.previous_context = AdjacentContext(previous[0], previous[1].source)
            if next_item is not None and _office_part(next_item[1]) == _office_part(data):
                data.next_context = AdjacentContext(next_item[0], next_item[1].source)
            yield unit_id, data
            previous = current
            current = next_item
    finally:
        _close_iterator(items)


def _office_part(data: Data) -> str:
    return data.extensions.get("office.part", "")


def _target_output_names(
    target_locales: tuple[str, ...],
    file_format: str,
) -> tuple[tuple[str, str], ...]:
    try:
        return locale_output_names(target_locales, suffix=f".{file_format}")
    except LocaleFilenameError as exc:
        if exc.reason == TOO_MANY_OUTPUTS:
            raise OfficeReinsertionError("Office export supports at most 256 target locales") from exc
        if exc.reason == FILENAME_COLLISION:
            raise OfficeReinsertionError(
                "Office target locales produce colliding Unicode/case-insensitive filenames"
            ) from exc
        if exc.reason == RESERVED_LOCALE:
            raise OfficeReinsertionError(f"Reserved Office target locale filename: {exc.locale!r}") from exc
        if exc.reason == FILENAME_TOO_LONG:
            raise OfficeReinsertionError("Office target locale filename exceeds portable platform limits") from exc
        raise OfficeReinsertionError(f"Unsafe Office target locale filename: {exc.locale!r}") from exc


def _write_target_outputs(
    document: BaseStructure | StreamingStructure,
    output_path: Path,
    targets: tuple[tuple[str, str], ...],
    file_format: str,
    source_file: _SourceFile,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
    allow_worker_reinsertion: bool,
) -> OfficeExportResult:
    if output_path.exists() and not output_path.is_dir():
        raise OfficeReinsertionError("Multi-target Office output path must be a directory")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    units_written = 0
    output_bytes = 0
    warnings: list[OfficeWarning] = []
    with tempfile.TemporaryDirectory(
        dir=output_path.parent,
        prefix=f".{output_path.name}.office-targets-",
    ) as temporary_directory:
        staging_directory = Path(temporary_directory)
        if isinstance(document, BaseStructure):
            for locale, filename in targets:
                raise_if_cancelled(cancellation)
                result = _write_output(
                    select_target(document, locale),
                    staging_directory / filename,
                    file_format,
                    source_file,
                    options,
                    locale,
                    cancellation,
                    allow_worker_reinsertion,
                )
                units_written += result.units_written
                output_bytes += result.output_bytes
                warnings.extend(result.warnings)
        else:
            with StreamingTargetSplit(document, _cancellation=cancellation) as target_documents:
                for locale, filename in targets:
                    raise_if_cancelled(cancellation)
                    target_document = target_documents.get(locale)
                    if target_document is None:
                        raise OfficeReinsertionError(f"Office target locale was not found: {locale}")
                    result = _write_output(
                        target_document,
                        staging_directory / filename,
                        file_format,
                        source_file,
                        options,
                        locale,
                        cancellation,
                        allow_worker_reinsertion,
                    )
                    units_written += result.units_written
                    output_bytes += result.output_bytes
                    warnings.extend(result.warnings)
        raise_if_cancelled(cancellation)
        _commit_target_outputs(staging_directory, output_path, targets, cancellation)
    return OfficeExportResult(
        output_path,
        units_written,
        tuple(warnings),
        source_file.fingerprint,
        output_bytes,
    )


def _commit_target_outputs(
    staging_directory: Path,
    output_path: Path,
    targets: tuple[tuple[str, str], ...],
    cancellation: threading.Event | None,
) -> None:
    output_existed = output_path.exists()
    if not output_existed:
        raise_if_cancelled(cancellation)
        os.replace(staging_directory, output_path)
        try:
            raise_if_cancelled(cancellation)
        except BaseException:
            shutil.rmtree(output_path, ignore_errors=True)
            raise
        return

    for _, filename in targets:
        destination = output_path / filename
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise OfficeReinsertionError(f"Office target output is not a regular file: {filename}")
    backups = staging_directory / ".backups"
    backups.mkdir()
    committed: list[tuple[Path, Path | None]] = []
    try:
        for index, (_, filename) in enumerate(targets):
            raise_if_cancelled(cancellation)
            source = staging_directory / filename
            destination = output_path / filename
            backup: Path | None = None
            if destination.exists():
                backup = backups / str(index)
                os.replace(destination, backup)
            try:
                os.replace(source, destination)
            except BaseException:
                if backup is not None:
                    os.replace(backup, destination)
                raise
            committed.append((destination, backup))
        raise_if_cancelled(cancellation)
    except BaseException:
        for destination, backup in reversed(committed):
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
            if backup is not None:
                os.replace(backup, destination)
        raise


def _write_output(
    document: BaseStructure | StreamingStructure,
    output: DocumentSink,
    file_format: str,
    source_file: _SourceFile,
    options: OfficeExportOptions,
    target_locale: str | None,
    cancellation: threading.Event | None,
    allow_worker_reinsertion: bool,
) -> OfficeExportResult:
    raise_if_cancelled(cancellation)
    output_path = Path(output) if isinstance(output, (str, Path)) else None
    if output_path is not None and target_locale is None and document.target_locale is None and document.target_locales:
        if output_path.suffix:
            raise OfficeReinsertionError(
                f"{file_format.upper()} export needs a selected target locale for a single output path"
            )
        targets = _target_output_names(document.target_locales, file_format)
        return _write_target_outputs(
            document,
            output_path,
            targets,
            file_format,
            source_file,
            options,
            cancellation,
            allow_worker_reinsertion,
        )

    tmp_path = _temporary_output_path(output_path, f".{file_format}")
    try:
        translation_items = _translation_items_for(document, target_locale, cancellation)
        worker_result: OfficeExportResult | None = None
        units_written = 0
        if allow_worker_reinsertion and _use_worker():
            worker_result = reinsert_with_worker(
                source_file.path,
                tmp_path,
                file_format,
                translation_items,
                target_locale,
                options,
                cancellation,
            )
            warnings = list(worker_result.warnings)
            units_written = worker_result.units_written
        else:
            if not allow_worker_reinsertion:
                _record("python", "fallback", "office.reinsert", "streaming documents require Python reinsertion")
            translations = _TranslationSpool(translation_items, target_locale, options, cancellation)
            try:
                warnings, units_written = _rewrite_package(
                    source_file.path,
                    tmp_path,
                    file_format,
                    translations,
                    options,
                    cancellation,
                )
            finally:
                translations.close()
        raise_if_cancelled(cancellation)
        _validate_written_package(tmp_path, file_format, options, cancellation)
        raise_if_cancelled(cancellation)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            raise_if_cancelled(cancellation)
            os.replace(tmp_path, output_path)
            final_path = output_path
        else:
            assert hasattr(output, "write")
            with tmp_path.open("rb") as stream:
                while True:
                    raise_if_cancelled(cancellation)
                    chunk = stream.read(_COPY_BUFFER_BYTES)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        raise_if_cancelled(cancellation)
                        written = output.write(chunk[offset:])
                        if written is None or written <= 0:
                            raise OfficeReinsertionError("Office output sink stopped accepting data")
                        offset += written
            raise_if_cancelled(cancellation)
            final_path = None
        output_bytes = output_path.stat().st_size if output_path is not None else tmp_path.stat().st_size
        return OfficeExportResult(
            output_path=final_path,
            units_written=units_written,
            warnings=tuple(warnings),
            source_fingerprint=(
                worker_result.source_fingerprint or source_file.fingerprint
                if worker_result is not None
                else source_file.fingerprint
            ),
            output_bytes=output_bytes,
        )
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    finally:
        if output_path is None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _temporary_output_path(output_path: Path | None, suffix: str) -> Path:
    directory = output_path.parent if output_path is not None else None
    name = output_path.name if output_path is not None else "office-output"
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=f".{name}.",
        suffix=f"{suffix}.tmp",
        delete=False,
    ) as tmp:
        path = Path(tmp.name)
    return path


def _translation_items_for(
    document: BaseStructure | StreamingStructure,
    target_locale: str | None,
    cancellation: threading.Event | None,
) -> Iterator[ExtractItem]:
    source_items: Iterable[ExtractItem] = (
        document.data.items() if isinstance(document, BaseStructure) else document.items
    )
    items = iter(source_items)
    try:
        for unit_id, data in items:
            raise_if_cancelled(cancellation)
            if _translation_text(data, target_locale) is not None:
                yield unit_id, data
    finally:
        _close_iterator(items)


def _translation_text(data: Data, target_locale: str | None) -> str | None:
    if target_locale and target_locale in data.targets:
        return data.targets[target_locale].text
    return data.target


def _validate_translation_limits(options: OfficeExportOptions) -> None:
    if options.max_text_unit_chars < 1:
        raise OfficeReinsertionError("max_text_unit_chars must be at least 1")
    if options.max_translation_units < 1:
        raise OfficeReinsertionError("max_translation_units must be at least 1")
    if options.max_translation_bytes < 1:
        raise OfficeReinsertionError("max_translation_bytes must be at least 1")


def _close_iterator(items: object) -> None:
    if hasattr(items, "close"):
        cast("_ClosableIterator", items).close()


def _rewrite_package(
    source_path: Path,
    output_path: Path,
    file_format: str,
    translations: _TranslationSpool,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
) -> tuple[list[OfficeWarning], int]:
    warnings: list[OfficeWarning] = []
    progress = _RewriteProgress()
    with zipfile.ZipFile(source_path, "r") as zin:
        names = _preflight_zip(zin, options)
        raise_if_cancelled(cancellation)
        actual = _detect_ooxml_format(zin, names, options)
        if actual != file_format:
            raise OfficeUnsupportedPackageError(f"Expected {file_format.upper()} package, detected {actual.upper()}")
        parts = _docx_parts(names, options) if file_format == "docx" else _pptx_parts(zin, names, options)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                raise_if_cancelled(cancellation)
                if info.filename in parts:
                    with zin.open(info, "r") as source_member:
                        data = source_member.read(options.max_unit_bytes + 1)
                    if len(data) > options.max_unit_bytes:
                        raise OfficePackageError(f"Office XML part exceeds max_unit_bytes: {info.filename}")
                    data = _rewrite_xml_part(
                        data,
                        info.filename,
                        file_format,
                        translations,
                        progress,
                        options,
                        warnings,
                        cancellation,
                    )
                    zout.writestr(_copy_zip_info(info), data)
                else:
                    _copy_zip_member(zin, zout, info, cancellation)
    raise_if_cancelled(cancellation)
    extras = translations.extra_count()
    if extras:
        message = f"{extras} supplied translation unit(s) did not match source document"
        if options.extra_translation_policy == ExtraTranslationPolicy.ERROR:
            raise OfficeReinsertionError(message)
        warnings.append(OfficeWarning("office.extra_translation", message))
    return warnings, progress.units_consumed


def _copy_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    copied = zipfile.ZipInfo(info.filename, info.date_time)
    copied.comment = info.comment
    copied.extra = info.extra
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    copied.create_system = info.create_system
    copied.compress_type = info.compress_type
    return copied


def _copy_zip_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    cancellation: threading.Event | None,
) -> None:
    copied = _copy_zip_info(info)
    if info.is_dir():
        target.writestr(copied, b"")
        return
    copied_bytes = 0
    with (
        source.open(info, "r") as source_member,
        target.open(
            copied,
            "w",
            force_zip64=info.file_size >= 2_000_000_000,
        ) as target_member,
    ):
        while True:
            raise_if_cancelled(cancellation)
            chunk = source_member.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            copied_bytes += len(chunk)
            if copied_bytes > info.file_size:
                raise OfficePackageError(f"Office ZIP entry exceeds its declared size: {info.filename}")
            target_member.write(chunk)
    if copied_bytes != info.file_size:
        raise OfficePackageError(f"Office ZIP entry size does not match its directory record: {info.filename}")


def _rewrite_xml_part(
    xml: bytes,
    part: str,
    file_format: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> bytes:
    raise_if_cancelled(cancellation)
    root = _parse_xml(xml, part)
    _preflight_xml_rewrite(root, part, file_format, translations, options, cancellation)
    consumed_before = progress.units_consumed
    if file_format == "docx":
        _rewrite_docx_part(root, part, translations, progress, options, warnings, cancellation)
    else:
        _rewrite_pptx_part(root, part, translations, progress, options, warnings, cancellation)
    if progress.units_consumed == consumed_before:
        return xml
    raise_if_cancelled(cancellation)
    return _serialize_xml_bounded(root, options.max_unit_bytes, part)


def _preflight_xml_rewrite(
    root: _Element,
    part: str,
    file_format: str,
    translations: _TranslationSpool,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
) -> None:
    """Reject target-driven XML growth before allocating replacement nodes."""
    growth = iter(
        _docx_rewrite_growth(root, part, translations, cancellation)
        if file_format == "docx"
        else _pptx_rewrite_growth(root, part, translations, options, cancellation)
    )
    try:
        first_delta = next(growth)
    except StopIteration:
        # Preserve an untouched source part byte-for-byte. Re-serializing it can
        # be slightly larger than the original due to namespace declarations,
        # and should not make a valid, unmodified part fail the output limit.
        return
    baseline = _serialized_xml_size(root, options.max_unit_bytes, part)
    estimated = baseline + first_delta
    if estimated > options.max_unit_bytes:
        raise OfficeReinsertionError(f"Office rewritten XML part exceeds max_unit_bytes: {part}")
    for delta in growth:
        estimated += delta
        if estimated > options.max_unit_bytes:
            raise OfficeReinsertionError(f"Office rewritten XML part exceeds max_unit_bytes: {part}")


def _serialized_xml_size(root: _Element, limit: int, part: str) -> int:
    writer = _BoundedXmlWriter(limit, part, retain=False)
    _write_xml(root, writer, part)
    return writer.bytes_written


def _serialize_xml_bounded(root: _Element, limit: int, part: str) -> bytes:
    writer = _BoundedXmlWriter(limit, part, retain=True)
    _write_xml(root, writer, part)
    return writer.value()


def _write_xml(root: _Element, writer: _BoundedXmlWriter, part: str) -> None:
    try:
        etree.ElementTree(root).write(
            cast("BinaryIO", writer),
            xml_declaration=True,
            encoding="UTF-8",
        )
    except OfficeReinsertionError:
        raise
    except (etree.SerialisationError, TypeError, ValueError) as exc:
        raise OfficeReinsertionError(f"Unable to serialize Office XML part: {part}") from exc


def _docx_rewrite_growth(
    root: _Element,
    part: str,
    translations: _TranslationSpool,
    cancellation: threading.Event | None,
) -> Iterator[int]:
    container = _docx_container(part)
    text_tag = f"{{{WORD_NS}}}t"
    for paragraph_index, paragraph in enumerate(root.iter(f"{{{WORD_NS}}}p")):
        raise_if_cancelled(cancellation)
        replacement = translations.lookup(f"docx:{container}:p/{paragraph_index}")
        if replacement is None:
            continue
        text_nodes = list(paragraph.iter(text_tag))
        removed = sum(_utf8_text_bytes(node.text or "") for node in text_nodes)
        structure = 64 if text_nodes else 256
        yield max(0, _xml_escaped_upper_bytes(replacement) + structure - removed)


def _pptx_rewrite_growth(
    root: _Element,
    part: str,
    translations: _TranslationSpool,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
) -> Iterator[int]:
    raise_if_cancelled(cancellation)
    if not options.include_hidden_slides and _is_hidden_slide(root, part):
        return
    container = _pptx_container(part)
    if _pptx_area_enabled(part, options):
        if _is_pptx_metadata_part(part):
            for element_index, element in enumerate(root.iter()):
                raise_if_cancelled(cancellation)
                property_name = _pptx_metadata_property(element, part)
                if property_name is None:
                    continue
                replacement = translations.lookup(f"pptx:{container}:property/{property_name}/{element_index}")
                if replacement is not None:
                    removed = _utf8_text_bytes(element.text or "")
                    yield max(0, _xml_escaped_upper_bytes(replacement) - removed)
        elif _is_pptx_comment_part(part):
            for element_index, element in enumerate(root.iter()):
                raise_if_cancelled(cancellation)
                if _local_name(element.tag) != "text":
                    continue
                replacement = translations.lookup(f"pptx:{container}:comment/{element_index}")
                if replacement is not None:
                    removed = _utf8_text_bytes(element.text or "")
                    yield max(0, _xml_escaped_upper_bytes(replacement) - removed)
        else:
            run_tag = f"{{{DRAWING_NS}}}r"
            break_tag = f"{{{DRAWING_NS}}}br"
            run_properties_tag = f"{{{DRAWING_NS}}}rPr"
            text_tag = f"{{{DRAWING_NS}}}t"
            for paragraph_index, paragraph in enumerate(root.iter(f"{{{DRAWING_NS}}}p")):
                raise_if_cancelled(cancellation)
                replacement = translations.lookup(f"pptx:{container}:p/{paragraph_index}")
                if replacement is None:
                    continue
                content = [child for child in paragraph if child.tag in {run_tag, break_tag}]
                template = next((child for child in content if child.tag == run_tag), None)
                run_properties = template.find(run_properties_tag) if template is not None else None
                properties_bytes = (
                    _serialized_xml_size(run_properties, options.max_unit_bytes, part)
                    if run_properties is not None
                    else 0
                )
                lines = replacement.count("\n") + 1
                structure = lines * (192 + properties_bytes) + (lines - 1) * 64
                removed = sum(_utf8_text_bytes(node.text or "") for child in content for node in child.iter(text_tag))
                yield max(0, _xml_escaped_upper_bytes(replacement) + structure - removed)
    if options.include_alt_text:
        for element_index, element in enumerate(root.iter()):
            raise_if_cancelled(cancellation)
            for attribute in ("title", "descr"):
                source = element.get(attribute) or ""
                if not source.strip():
                    continue
                replacement = translations.lookup(f"pptx:{container}:alt/{element_index}/{attribute}")
                if replacement is not None:
                    yield max(
                        0,
                        _xml_escaped_upper_bytes(replacement, attribute=True) - _utf8_text_bytes(source),
                    )


def _xml_escaped_upper_bytes(text: str, *, attribute: bool = False) -> int:
    total = 0
    for character in text:
        if character == "&":
            total += 5
        elif character in {"<", ">"}:
            total += 4
        elif character == "\r":
            total += 5
        elif attribute and character == '"':
            total += 6
        elif (attribute and character == "\t") or (attribute and character == "\n"):
            total += 5
        else:
            codepoint = ord(character)
            total += 1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF else 3 if codepoint <= 0xFFFF else 4
    return total


def _utf8_text_bytes(text: str) -> int:
    total = 0
    for character in text:
        codepoint = ord(character)
        total += 1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF else 3 if codepoint <= 0xFFFF else 4
    return total


def _rewrite_docx_part(
    root: _Element,
    part: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> None:
    container = _docx_container(part)
    for paragraph_index, paragraph in enumerate(root.iter(f"{{{WORD_NS}}}p")):
        raise_if_cancelled(cancellation)
        unit_id = f"docx:{container}:p/{paragraph_index}"
        replacement = translations.consume(unit_id)
        if replacement is not None:
            _replace_docx_paragraph(paragraph, replacement)
            progress.units_consumed += 1
        else:
            _handle_missing_office_translation(
                unit_id,
                _docx_paragraph_text(paragraph),
                part,
                options,
                warnings,
            )


def _rewrite_pptx_part(
    root: _Element,
    part: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> None:
    raise_if_cancelled(cancellation)
    if not options.include_hidden_slides and _is_hidden_slide(root, part):
        return
    container = _pptx_container(part)
    if _pptx_area_enabled(part, options):
        if _is_pptx_metadata_part(part):
            _rewrite_pptx_metadata(
                root,
                part,
                container,
                translations,
                progress,
                options,
                warnings,
                cancellation,
            )
        elif _is_pptx_comment_part(part):
            _rewrite_pptx_comments(
                root,
                part,
                container,
                translations,
                progress,
                options,
                warnings,
                cancellation,
            )
        else:
            for paragraph_index, paragraph in enumerate(root.iter(f"{{{DRAWING_NS}}}p")):
                raise_if_cancelled(cancellation)
                unit_id = f"pptx:{container}:p/{paragraph_index}"
                replacement = translations.consume(unit_id)
                if replacement is not None:
                    _replace_pptx_paragraph(paragraph, replacement)
                    progress.units_consumed += 1
                else:
                    _handle_missing_office_translation(
                        unit_id,
                        _pptx_paragraph_text(paragraph),
                        part,
                        options,
                        warnings,
                        preserve_whitespace=_pptx_area(part) == "diagrams",
                    )
    if options.include_alt_text:
        _rewrite_pptx_alt_text(
            root,
            part,
            container,
            translations,
            progress,
            options,
            warnings,
            cancellation,
        )


def _rewrite_pptx_comments(
    root: _Element,
    part: str,
    container: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> None:
    for element_index, element in enumerate(root.iter()):
        raise_if_cancelled(cancellation)
        if _local_name(element.tag) != "text":
            continue
        unit_id = f"pptx:{container}:comment/{element_index}"
        replacement = translations.consume(unit_id)
        if replacement is not None:
            element.text = replacement
            progress.units_consumed += 1
        else:
            _handle_missing_office_translation(unit_id, element.text or "", part, options, warnings)


def _rewrite_pptx_metadata(
    root: _Element,
    part: str,
    container: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> None:
    for element_index, element in enumerate(root.iter()):
        raise_if_cancelled(cancellation)
        property_name = _pptx_metadata_property(element, part)
        if property_name is None:
            continue
        unit_id = f"pptx:{container}:property/{property_name}/{element_index}"
        replacement = translations.consume(unit_id)
        if replacement is not None:
            element.text = replacement
            progress.units_consumed += 1
        else:
            _handle_missing_office_translation(unit_id, element.text or "", part, options, warnings)


def _rewrite_pptx_alt_text(
    root: _Element,
    part: str,
    container: str,
    translations: _TranslationSpool,
    progress: _RewriteProgress,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    cancellation: threading.Event | None,
) -> None:
    for element_index, element in enumerate(root.iter()):
        raise_if_cancelled(cancellation)
        attributes = ["title", "descr"]
        for attribute in attributes:
            raise_if_cancelled(cancellation)
            source = element.get(attribute) or ""
            if not source.strip():
                continue
            unit_id = f"pptx:{container}:alt/{element_index}/{attribute}"
            replacement = translations.consume(unit_id)
            if replacement is not None:
                element.set(attribute, replacement)
                progress.units_consumed += 1
            else:
                _handle_missing_office_translation(unit_id, source, part, options, warnings)


def _handle_missing_office_translation(
    unit_id: str,
    source: str,
    part: str,
    options: OfficeExportOptions,
    warnings: list[OfficeWarning],
    *,
    preserve_whitespace: bool = False,
) -> None:
    if not source or (not preserve_whitespace and not source.strip()):
        return
    if options.missing_translation_policy == MissingTranslationPolicy.ERROR:
        raise OfficeReinsertionError(f"Missing translation for {unit_id}")
    if options.missing_translation_policy == MissingTranslationPolicy.WARN:
        warnings.append(
            OfficeWarning(
                "office.missing_translation",
                f"Missing translation for {unit_id}",
                unit_id,
                part,
            )
        )


def _replace_docx_paragraph(paragraph: _Element, text: str) -> None:
    text_nodes = [element for element in paragraph.iter(f"{{{WORD_NS}}}t")]
    if not text_nodes:
        run = etree.SubElement(paragraph, f"{{{WORD_NS}}}r")
        node = etree.SubElement(run, f"{{{WORD_NS}}}t")
        node.text = text
        node.set(XML_SPACE, "preserve")
        return
    text_nodes[0].text = text
    text_nodes[0].set(XML_SPACE, "preserve")
    for node in text_nodes[1:]:
        node.text = ""


def _replace_pptx_paragraph(paragraph: _Element, text: str) -> None:
    run_tag = f"{{{DRAWING_NS}}}r"
    break_tag = f"{{{DRAWING_NS}}}br"
    run_properties_tag = f"{{{DRAWING_NS}}}rPr"
    text_tag = f"{{{DRAWING_NS}}}t"
    content = [child for child in paragraph if child.tag in {run_tag, break_tag}]
    template = next((child for child in content if child.tag == run_tag), None)
    insert_at = paragraph.index(content[0]) if content else len(paragraph)
    end_properties = paragraph.find(f"{{{DRAWING_NS}}}endParaRPr")
    if not content and end_properties is not None:
        insert_at = paragraph.index(end_properties)
    for child in content:
        paragraph.remove(child)

    run_properties = template.find(run_properties_tag) if template is not None else None
    replacement: list[_Element] = []
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            replacement.append(etree.Element(break_tag))
        run = etree.Element(run_tag)
        if run_properties is not None:
            run.append(deepcopy(run_properties))
        node = etree.SubElement(run, text_tag)
        node.text = line
        node.set(XML_SPACE, "preserve")
        replacement.append(run)
    for element in replacement:
        paragraph.insert(insert_at, element)
        insert_at += 1


def _validate_written_package(
    path: Path,
    file_format: str,
    options: OfficeExportOptions,
    cancellation: threading.Event | None,
) -> None:
    raise_if_cancelled(cancellation)
    if options.validation_mode.value == "off":
        return
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = _preflight_zip(zf, options)
            raise_if_cancelled(cancellation)
            actual = _detect_ooxml_format(zf, names, options)
            if actual != file_format:
                raise OfficeValidationError(f"Output package is not {file_format.upper()}")
    except zipfile.BadZipFile as exc:
        raise OfficeValidationError("Output Office package is not a valid ZIP archive") from exc


def _selected_document(
    document: BaseStructure | StreamingStructure,
    target_locale: str | None,
) -> BaseStructure | StreamingStructure:
    if isinstance(document, BaseStructure) and target_locale and target_locale in document.target_locales:
        return select_target(document, target_locale)
    return document


def _source_document_from_extensions(document: BaseStructure | StreamingStructure) -> str | None:
    return document.extensions.get("source_file")


def _document_extensions(file_format: str, fingerprint: str, source_file: str) -> dict[str, str]:
    runtime = load_runtime_info()
    extensions = {
        "input_format": file_format,
        "office.worker_version": runtime.worker_version,
        "office.protocol_version": f"{runtime.protocol_major}.{runtime.protocol_minor}",
    }
    if fingerprint:
        extensions["office.source_fingerprint"] = fingerprint
    if source_file:
        extensions["source_file"] = source_file
    if runtime.openxml_sdk_version:
        extensions["office.openxml_sdk_version"] = runtime.openxml_sdk_version
    return extensions


def _use_worker() -> bool:
    if os.environ.get("LOKIT_OFFICE_BACKEND", "").lower() == "python":
        _record("python", "fallback", "office.backend", "LOKIT_OFFICE_BACKEND=python")
        return False
    if not worker_available():
        _record("python", "fallback", "office.backend", "Office worker is unavailable")
        return False
    _record("office-worker", "selection", "office.backend", "external Office worker selected (not the Rust extension)")
    return True


def _parse_xml(data: bytes, part: str) -> _Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise OfficePackageError(f"Malformed XML in Office part {part}") from exc


def _sha256_file(
    path: Path,
    limit: int,
    cancellation: threading.Event | None,
) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as stream:
        while True:
            raise_if_cancelled(cancellation)
            chunk = stream.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            read += len(chunk)
            if read > limit:
                raise OfficePackageError("Office source exceeds max_compressed_bytes")
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _base_language(locale: str | None) -> str | None:
    if not locale:
        return None
    return locale.replace("_", "-").split("-")[0].lower()


def _local_name(tag: str | bytes) -> str:
    name = tag if isinstance(tag, str) else tag.decode("utf-8")
    if "}" in name:
        return name.split("}", 1)[1]
    return name
