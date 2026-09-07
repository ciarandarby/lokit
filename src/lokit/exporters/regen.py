from __future__ import annotations

import ast
import contextlib
import copy
import csv
import json
import posixpath
import sqlite3
import stat
import zipfile
from collections import OrderedDict
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit._interchange_rust import IdentityRegistry
from lokit.data.structure import BaseStructure, CodePart, Data, SegmentPart, StreamingStructure, TargetTags, TextPart
from lokit.data.targets import target_text
from lokit.export_projection import prepare_export_data, prepare_export_document
from lokit.exporters.docx import export_docx, export_docx_async
from lokit.exporters.html import export_html, export_html_async
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.exporters.pptx import export_pptx, export_pptx_async
from lokit.io.atomic import atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.parsers.tmx.xml_utils import find_child, local_name
from lokit.tabular import (
    ResolvedTabularLayout,
    build_import_options,
    column_reference_to_index,
    normalize_language_header,
    resolve_tabular_layout,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from types import TracebackType
    from typing import BinaryIO

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData
    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions

Structure = BaseStructure | StreamingStructure

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"

_COPY_BUFFER_BYTES = 1024 * 1024
_MAX_ZIP_ENTRIES = 100_000
_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_WORKBOOK_XML_BYTES = 16 * 1024 * 1024
_MAX_RELATIONSHIPS_XML_BYTES = 16 * 1024 * 1024
_MAX_SHARED_STRINGS_XML_BYTES = 512 * 1024 * 1024
_MAX_WORKSHEET_XML_BYTES = 512 * 1024 * 1024
_MAX_OUTPUT_WORKSHEET_BYTES = 4 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000.0
_MAX_MEMBER_NAME_BYTES = 4096
_MAX_SHARED_STRINGS = 10_000_000
_MAX_CELL_CHARACTERS = 32_767
_MAX_ROW_COLUMNS = 16_384
_SHARED_STRING_CACHE_ENTRIES = 256
_MAX_PO_BLOCK_CHARACTERS = 64 * 1024 * 1024
_MAX_XML_EPILOG_BYTES = 16 * 1024 * 1024
_MAX_XML_RECORD_ELEMENTS = 250_000
_MAX_XML_RECORD_CHARACTERS = 64 * 1024 * 1024


class TextWriter(Protocol):
    def write(self, value: str) -> int: ...


class _Closable(Protocol):
    def close(self) -> None: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _BinaryWriter(Protocol):
    def write(self, value: bytes, /) -> int: ...


class _XmlElementContext(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object: ...


class _XmlWriter(Protocol):
    def element(
        self,
        tag: str,
        attrib: Mapping[str, str],
        nsmap: Mapping[str | None, str] | None = None,
    ) -> _XmlElementContext: ...

    def write(self, content: object, *, with_tail: bool = True, pretty_print: bool = False) -> None: ...

    def write_declaration(self) -> None: ...

    def write_doctype(self, doctype: str) -> None: ...


class _XmlOutputFrame:
    __slots__ = ("context", "element", "text_written")

    def __init__(self, element: _Element, context: _XmlElementContext) -> None:
        self.context = context
        self.element = element
        self.text_written = False


class _UnitProvider:
    def __init__(self, document: Structure, *, resolve_placeholders: bool) -> None:
        self._document = document
        self._resolve_placeholders = resolve_placeholders
        self._items: Iterator[tuple[str, Data]] | None = None
        self._last_unit_id = ""
        self._last_unit: Data | None = None
        if isinstance(document, StreamingStructure):
            self._items = iter(document.items)

    def __enter__(self) -> _UnitProvider:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        items = self._items
        self._items = None
        if items is not None:
            _close_iterator(items)

    def get(self, unit_id: str, locale: str | None = None) -> Data | None:
        cached = self._last_unit
        if cached is not None and self._last_unit_id == unit_id:
            return cached

        if isinstance(self._document, BaseStructure):
            raw = self._document.data.get(unit_id)
            if raw is None:
                return None
            prepared = prepare_export_data(
                raw,
                resolve_placeholders=self._resolve_placeholders,
            )
            self._last_unit_id = unit_id
            self._last_unit = prepared
            return prepared

        if self._items is None:
            return None

        try:
            for next_id, next_unit in self._items:
                self._last_unit_id = next_id
                self._last_unit = prepare_export_data(
                    next_unit,
                    resolve_placeholders=self._resolve_placeholders,
                )
                if next_id == unit_id:
                    return self._last_unit
        except BaseException:
            self.close()
            raise
        self.close()
        return None


def _close_iterator(items: object) -> None:
    if hasattr(items, "close"):
        cast("_Closable", items).close()


class _BoundedReader:
    def __init__(
        self,
        stream: _BinaryReader,
        limit: int,
        label: str,
        cancellation: threading.Event | None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._label = label
        self._cancellation = cancellation
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        raise_if_cancelled(self._cancellation)
        remaining_with_probe = self._limit - self.bytes_read + 1
        requested = remaining_with_probe if size < 0 else min(size, remaining_with_probe)
        data = self._stream.read(max(0, requested))
        self.bytes_read += len(data)
        if self.bytes_read > self._limit:
            raise ValueError(f"{self._label} exceeds its decompression limit")
        return data


class _BoundedWriter:
    def __init__(
        self,
        stream: _BinaryWriter,
        limit: int,
        label: str,
        cancellation: threading.Event | None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._label = label
        self._cancellation = cancellation
        self.bytes_written = 0

    def write(self, value: bytes) -> int:
        raise_if_cancelled(self._cancellation)
        next_size = self.bytes_written + len(value)
        if next_size > self._limit:
            raise ValueError(f"{self._label} exceeds its output size limit")
        written = self._stream.write(value)
        self.bytes_written += written
        return written


class _SharedStringStore(AbstractContextManager["_SharedStringStore"]):
    """Lazy, disk-backed shared-string lookup with a small bounded hot cache."""

    def __init__(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo | None,
        cancellation: threading.Event | None,
    ) -> None:
        self._archive = archive
        self._info = info
        self._cancellation = cancellation
        self._directory: TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None
        self._cache: OrderedDict[int, str] = OrderedDict()
        self._loaded = False

    def __enter__(self) -> _SharedStringStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        directory = self._directory
        self._connection = None
        self._directory = None
        self._cache.clear()
        try:
            if connection is not None:
                connection.close()
        finally:
            if directory is not None:
                directory.cleanup()

    def get(self, index: int) -> str:
        if index < 0:
            return ""
        self._load()
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        connection = self._connection
        if connection is None:
            return ""
        row = connection.execute("SELECT value FROM strings WHERE position = ?", (index,)).fetchone()
        if row is None:
            return ""
        value = cast("str", row[0])
        self._cache[index] = value
        if len(self._cache) > _SHARED_STRING_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return value

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        info = self._info
        if info is None:
            return

        directory = TemporaryDirectory(prefix="lokit-xlsx-regen-")
        connection = sqlite3.connect(Path(directory.name) / "shared-strings.sqlite3")
        self._directory = directory
        self._connection = connection
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("CREATE TABLE strings (position INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            batch: list[tuple[int, str]] = []
            count = 0
            with self._archive.open(info, "r") as raw_member:
                member = _BoundedReader(
                    raw_member,
                    min(info.file_size, _MAX_SHARED_STRINGS_XML_BYTES),
                    "XLSX sharedStrings.xml",
                    self._cancellation,
                )
                context = etree.iterparse(
                    cast("BinaryIO", member),
                    events=("end",),
                    tag=f"{{{SHEET_NS}}}si",
                    resolve_entities=False,
                    load_dtd=False,
                    no_network=True,
                    huge_tree=False,
                )
                for _, element in context:
                    raise_if_cancelled(self._cancellation)
                    if count >= _MAX_SHARED_STRINGS:
                        raise ValueError(f"XLSX sharedStrings.xml has more than {_MAX_SHARED_STRINGS} entries")
                    value = "".join(text.text or "" for text in element.iterfind(f".//{{{SHEET_NS}}}t"))
                    _validate_cell_text(value, "XLSX shared string")
                    batch.append((count, value))
                    count += 1
                    if len(batch) >= 1000:
                        connection.executemany("INSERT INTO strings(position, value) VALUES (?, ?)", batch)
                        batch.clear()
                    _clear_emitted_element(element)
                if member.bytes_read != info.file_size:
                    raise ValueError("XLSX sharedStrings.xml size does not match its directory record")
            if batch:
                connection.executemany("INSERT INTO strings(position, value) VALUES (?, ?)", batch)
            connection.commit()
        except BaseException:
            self.close()
            raise


def regen_csv(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    _regen_csv(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _regen_csv(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    source_locale: str,
    header_mode: str,
    include_header_as_data: bool,
    source_column: str,
    target_column: str,
    target_columns: Mapping[str, str] | None,
    id_column: str,
    status_column: str,
    comment_column: str,
    preserve_extra_columns: bool,
    strict_language_headers: bool,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    source = Path(original_filepath)
    output = Path(output_path)
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    with (
        atomic_output_path(
            output,
            "w",
            cancellation=cancellation,
            encoding="utf-8",
            newline="",
        ) as output_stream,
        _UnitProvider(document, resolve_placeholders=resolve_placeholders) as provider,
        source.open("r", newline="", encoding="utf-8-sig") as input_stream,
    ):
        reader = csv.reader(input_stream)
        raise_if_cancelled(cancellation)
        first_row = next(reader, None)
        if first_row is None:
            return

        layout = resolve_tabular_layout(
            first_row,
            len(first_row),
            options,
            source_locale or document.source_locale,
            target_locale or document.target_locale,
            "csv",
        )
        columns = _target_columns_for_layout(document, layout, target_locale)
        writer = csv.writer(output_stream)
        data_rows: Iterable[list[str]] = reader
        if layout.has_header and not layout.include_header_as_data:
            writer.writerow(first_row)
        else:
            data_rows = _prepend_row(first_row, reader)

        ids = IdentityRegistry()
        for row_index, row in enumerate(data_rows):
            raise_if_cancelled(cancellation)
            unit_id = ids.resolve_tabular(row, row_index, layout.id_column, "csv")
            _replace_row_targets(row, provider, document, unit_id, columns)
            writer.writerow(row)


async def regen_csv_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regen_csv(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            source_locale=source_locale,
            header_mode=header_mode,
            include_header_as_data=include_header_as_data,
            source_column=source_column,
            target_column=target_column,
            target_columns=target_columns,
            id_column=id_column,
            status_column=status_column,
            comment_column=comment_column,
            preserve_extra_columns=preserve_extra_columns,
            strict_language_headers=strict_language_headers,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def regen_xlsx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    _regen_xlsx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _regen_xlsx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    source_locale: str,
    header_mode: str,
    include_header_as_data: bool,
    source_column: str,
    target_column: str,
    target_columns: Mapping[str, str] | None,
    id_column: str,
    status_column: str,
    comment_column: str,
    sheet_name: str,
    sheet_index: int,
    preserve_extra_columns: bool,
    strict_language_headers: bool,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    source = Path(original_filepath)
    output = Path(output_path)
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    with (
        atomic_output_path(output, "w+b", cancellation=cancellation) as output_stream,
        zipfile.ZipFile(source, "r") as archive,
    ):
        infos, entries = _preflight_xlsx_package(source, archive)
        worksheet = _worksheet_info(archive, entries, sheet_name, sheet_index, cancellation)
        with (
            _SharedStringStore(archive, entries.get("xl/sharedStrings.xml"), cancellation) as shared_strings,
            _UnitProvider(document, resolve_placeholders=resolve_placeholders) as provider,
        ):
            first = _first_worksheet_row(archive, worksheet, shared_strings, cancellation)
            layout = (
                resolve_tabular_layout(
                    first,
                    len(first),
                    options,
                    source_locale or document.source_locale,
                    target_locale or document.target_locale,
                    "xlsx",
                )
                if first is not None
                else None
            )
            columns = _target_columns_for_layout(document, layout, target_locale) if layout is not None else ()
            _write_xlsx_package(
                archive,
                infos,
                output_stream,
                worksheet,
                shared_strings,
                provider,
                document,
                layout,
                columns,
                cancellation,
            )


async def regen_xlsx_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regen_xlsx(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            source_locale=source_locale,
            header_mode=header_mode,
            include_header_as_data=include_header_as_data,
            source_column=source_column,
            target_column=target_column,
            target_columns=target_columns,
            id_column=id_column,
            status_column=status_column,
            comment_column=comment_column,
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            preserve_extra_columns=preserve_extra_columns,
            strict_language_headers=strict_language_headers,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def regen_xliff(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    _regen_xliff(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _regen_xliff(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    file_index = 0
    file_stack: list[tuple[int, str | None]] = []
    parent_ids: list[str] = []
    ids = IdentityRegistry()
    version_two = False
    root_locale: str | None = None

    def start_element(element: _Element) -> None:
        nonlocal file_index, version_two, root_locale
        name = local_name(element.tag)
        if name == "xliff":
            version_two = element.attrib.get("version", "").startswith("2")
            root_locale = element.attrib.get("trgLang")
        elif name == "unit" and version_two:
            parent_ids.append(element.attrib.get("id", ""))
        elif name == "file":
            file_stack.append((file_index, element.attrib.get("target-language") or root_locale))
            file_index += 1

    def end_element(element: _Element) -> None:
        name = local_name(element.tag)
        if name == "file":
            file_stack.pop()
        elif name == "unit" and version_two:
            parent_ids.pop()

    with _UnitProvider(document, resolve_placeholders=resolve_placeholders) as provider:

        def rewrite_unit(trans_unit: _Element) -> None:
            if not file_stack:
                return
            current_file_index, file_locale = file_stack[-1]
            raw_unit_id = trans_unit.attrib.get("id", "")
            unit_id = ids.xliff(raw_unit_id, parent_ids[-1] if parent_ids else "", current_file_index, version_two)
            locale = target_locale or file_locale or document.target_locale
            unit = provider.get(unit_id, locale)
            replacement = _replacement_for_unit(unit, locale)
            if replacement is not None:
                _replace_xliff_target(trans_unit, unit, replacement, locale)

        _stream_xml_rewrite(
            Path(original_filepath),
            Path(output_path),
            record_name=("trans-unit", "segment"),
            rewrite_record=rewrite_unit,
            on_start=start_element,
            on_end=end_element,
            cancellation=cancellation,
        )


async def regen_xliff_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regen_xliff(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def regen_tmx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    _regen_tmx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _regen_tmx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    ids = IdentityRegistry()

    with _UnitProvider(document, resolve_placeholders=resolve_placeholders) as provider:

        def rewrite_unit(tu: _Element) -> None:
            unit_id = ids.tmx(tu.attrib.get("tuid", ""))
            source_locale = document.source_locale
            for tuv in _iter_direct_children(tu, "tuv"):
                locale = _xml_lang(tuv)
                if _same_locale(locale, source_locale):
                    continue
                if target_locale is not None and not _same_locale(locale, target_locale):
                    continue
                effective_locale = target_locale or locale or document.target_locale
                unit = provider.get(unit_id, effective_locale)
                replacement = _replacement_for_unit(unit, effective_locale)
                if replacement is None:
                    continue
                seg = find_child(tuv, "seg")
                if seg is not None:
                    _replace_plain_xml_payload(seg, replacement)

        _stream_xml_rewrite(
            Path(original_filepath),
            Path(output_path),
            record_name="tu",
            rewrite_record=rewrite_unit,
            cancellation=cancellation,
        )


async def regen_tmx_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regen_tmx(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def regen_po(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    _regen_po(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _regen_po(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    locale = target_locale or document.target_locale
    with (
        atomic_output_path(
            Path(output_path),
            "w",
            cancellation=cancellation,
            encoding="utf-8",
            newline="",
        ) as out,
        _UnitProvider(document, resolve_placeholders=resolve_placeholders) as provider,
        Path(original_filepath).open("r", encoding="utf-8", newline="") as source,
    ):
        block: list[str] = []
        block_characters = 0
        for line in source:
            raise_if_cancelled(cancellation)
            if line.strip():
                block_characters += len(line)
                if block_characters > _MAX_PO_BLOCK_CHARACTERS:
                    raise ValueError("PO entry exceeds its regeneration size limit")
                block.append(line)
                continue
            _write_po_block(out, provider, block, locale)
            block = []
            block_characters = 0
            out.write(line)
        _write_po_block(out, provider, block, locale)


async def regen_po_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regen_po(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def regen_json_i18n(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.json_i18n_regen import regenerate_json_i18n

    regenerate_json_i18n(
        prepare_export_document(
            document,
            resolve_placeholders=resolve_placeholders,
        ),
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
    )


async def regen_json_i18n_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.json_i18n_regen import regenerate_json_i18n_async

    await regenerate_json_i18n_async(
        prepare_export_document(
            document,
            resolve_placeholders=resolve_placeholders,
        ),
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
    )


def regen_html(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    export_html(
        document,
        output_path,
        original_filepath,
        resolve_placeholders=resolve_placeholders,
    )


async def regen_html_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await export_html_async(
        document,
        output_path,
        original_filepath,
        resolve_placeholders=resolve_placeholders,
    )


def regen_idml(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    export_idml(
        document,
        output_path,
        original_filepath,
        resolve_placeholders=resolve_placeholders,
    )


async def regen_idml_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await export_idml_async(
        document,
        output_path,
        original_filepath,
        resolve_placeholders=resolve_placeholders,
    )


def regen_docx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    return export_docx(
        document,
        output_path,
        original_filepath,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


async def regen_docx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    return await export_docx_async(
        document,
        output_path,
        original_filepath,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


def regen_pptx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    return export_pptx(
        document,
        output_path,
        original_filepath,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )


async def regen_pptx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    return await export_pptx_async(
        document,
        output_path,
        original_filepath,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )


def _replacement_for_unit(unit: Data | None, locale: str | None) -> str | None:
    if unit is None:
        return None
    return target_text(unit, locale)


def _prepend_row(first: list[str], rows: Iterable[list[str]]) -> Iterator[list[str]]:
    yield first
    yield from rows


def _target_columns_for_layout(
    document: Structure,
    layout: ResolvedTabularLayout,
    requested_locale: str | None,
) -> tuple[tuple[str | None, int], ...]:
    if requested_locale is not None:
        resolved = _find_target_column(layout, requested_locale)
        return ((requested_locale, resolved),) if resolved >= 0 else ()

    if document.target_locale is not None:
        resolved = _find_target_column(layout, document.target_locale)
        if resolved >= 0:
            return ((document.target_locale, resolved),)

    if layout.target_columns:
        return tuple((locale or None, index) for locale, index in layout.target_columns.items())
    return ()


def _find_target_column(layout: ResolvedTabularLayout, locale: str) -> int:
    canonical = normalize_language_header(locale) or locale
    if canonical in layout.target_columns:
        return layout.target_columns[canonical]
    base = _base_language(canonical)
    matches = [index for key, index in layout.target_columns.items() if key and _base_language(key) == base]
    if len(matches) == 1:
        return matches[0]
    return layout.target_columns.get("", -1)


def _replace_row_targets(
    row: list[str],
    provider: _UnitProvider,
    document: Structure,
    unit_id: str,
    columns: Sequence[tuple[str | None, int]],
) -> None:
    for locale, index in columns:
        effective_locale = locale or document.target_locale
        unit = provider.get(unit_id, effective_locale)
        replacement = _replacement_for_unit(unit, effective_locale)
        if replacement is None:
            continue
        while len(row) <= index:
            row.append("")
        row[index] = replacement


def _base_language(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()


def _preflight_xlsx_package(
    source_path: Path,
    archive: zipfile.ZipFile,
) -> tuple[tuple[zipfile.ZipInfo, ...], dict[str, zipfile.ZipInfo]]:
    if source_path.stat().st_size > _MAX_COMPRESSED_BYTES:
        raise ValueError("XLSX package exceeds its compressed size limit")
    infos = tuple(archive.infolist())
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise ValueError(f"XLSX package has more than {_MAX_ZIP_ENTRIES} ZIP entries")

    entries: dict[str, zipfile.ZipInfo] = {}
    compressed_bytes = 0
    uncompressed_bytes = 0
    for info in infos:
        _validate_xlsx_member_name(info.filename)
        if info.filename in entries:
            raise ValueError(f"duplicate XLSX ZIP entry: {info.filename}")
        entries[info.filename] = info
        if info.flag_bits & 0x1:
            raise ValueError("encrypted XLSX ZIP entries are not supported")
        if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
            raise ValueError(f"symbolic-link XLSX ZIP entry is not supported: {info.filename}")
        if info.is_dir() and (info.file_size or info.compress_size):
            raise ValueError(f"XLSX directory entry contains data: {info.filename}")
        if info.file_size > _MAX_MEMBER_BYTES:
            raise ValueError(f"XLSX ZIP entry exceeds its size limit: {info.filename}")
        if info.filename == "xl/sharedStrings.xml" and info.file_size > _MAX_SHARED_STRINGS_XML_BYTES:
            raise ValueError("XLSX sharedStrings.xml exceeds its decompression limit")
        compressed_bytes += info.compress_size
        uncompressed_bytes += info.file_size
        if compressed_bytes > _MAX_COMPRESSED_BYTES:
            raise ValueError("XLSX package exceeds its compressed size limit")
        if uncompressed_bytes > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("XLSX package exceeds its decompression limit")
        if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO):
            raise ValueError(f"suspicious compression ratio in XLSX ZIP entry: {info.filename}")
    return infos, entries


def _validate_xlsx_member_name(name: str) -> None:
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed
        or len(name.encode("utf-8")) > _MAX_MEMBER_NAME_BYTES
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or (len(name) >= 2 and name[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe XLSX ZIP entry: {name!r}")


def _worksheet_info(
    archive: zipfile.ZipFile,
    entries: Mapping[str, zipfile.ZipInfo],
    sheet_name: str,
    sheet_index: int,
    cancellation: threading.Event | None,
) -> zipfile.ZipInfo:
    workbook_info = _required_xlsx_member(entries, "xl/workbook.xml")
    workbook = _parse_bounded_xml_member(
        archive,
        workbook_info,
        _MAX_WORKBOOK_XML_BYTES,
        "XLSX workbook.xml",
        cancellation,
    )
    sheets = [sheet for sheet in workbook.findall(f".//{{{SHEET_NS}}}sheet")]
    if not sheets:
        raise ValueError("XLSX workbook does not contain worksheets")
    selected = _select_sheet(sheets, sheet_name, sheet_index)
    relationship_id = selected.attrib.get(f"{{{OFFICE_REL_NS}}}id")
    if not relationship_id:
        raise ValueError("XLSX worksheet relationship is missing")

    relationships_info = _required_xlsx_member(entries, "xl/_rels/workbook.xml.rels")
    rels = _parse_bounded_xml_member(
        archive,
        relationships_info,
        _MAX_RELATIONSHIPS_XML_BYTES,
        "XLSX workbook relationships",
        cancellation,
    )
    for rel in rels.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") != relationship_id:
            continue
        if rel.attrib.get("TargetMode", "").lower() == "external":
            raise ValueError("external XLSX worksheet relationships are not supported")
        target = rel.attrib.get("Target", "")
        if not target:
            break
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        _validate_xlsx_member_name(resolved)
        if not resolved.startswith("xl/"):
            raise ValueError(f"XLSX worksheet relationship escapes the workbook directory: {target!r}")
        worksheet = entries.get(resolved)
        if worksheet is None or worksheet.is_dir():
            raise ValueError(f"XLSX worksheet relationship resolves to a missing member: {resolved!r}")
        if worksheet.file_size > _MAX_WORKSHEET_XML_BYTES:
            raise ValueError(f"XLSX worksheet exceeds its decompression limit: {resolved}")
        return worksheet
    raise ValueError(f"XLSX worksheet relationship {relationship_id!r} does not resolve")


def _required_xlsx_member(entries: Mapping[str, zipfile.ZipInfo], name: str) -> zipfile.ZipInfo:
    info = entries.get(name)
    if info is None or info.is_dir():
        raise ValueError(f"XLSX package is missing required member: {name}")
    return info


def _parse_bounded_xml_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
    label: str,
    cancellation: threading.Event | None,
) -> _Element:
    payload = _read_xlsx_member(archive, info, limit, label, cancellation)
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_blank_text=False,
    )
    return cast("_Element", etree.fromstring(payload, parser=parser))


def _read_xlsx_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
    label: str,
    cancellation: threading.Event | None,
) -> bytes:
    if info.file_size > limit:
        raise ValueError(f"{label} exceeds its decompression limit")
    payload = bytearray()
    with archive.open(info, "r") as raw_member:
        member = _BoundedReader(raw_member, min(info.file_size, limit), label, cancellation)
        while True:
            chunk = member.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            payload.extend(chunk)
        if member.bytes_read != info.file_size:
            raise ValueError(f"{label} size does not match its directory record")
    return bytes(payload)


def _select_sheet(sheets: Sequence[_Element], sheet_name: str, sheet_index: int) -> _Element:
    if sheet_name:
        for sheet in sheets:
            if sheet.attrib.get("name") == sheet_name:
                return sheet
        raise ValueError(f"XLSX sheet {sheet_name!r} does not resolve")
    if sheet_index < 0 or sheet_index >= len(sheets):
        raise ValueError(f"XLSX sheet index {sheet_index} does not resolve")
    return sheets[sheet_index]


def _first_worksheet_row(
    archive: zipfile.ZipFile,
    worksheet: zipfile.ZipInfo,
    shared_strings: _SharedStringStore,
    cancellation: threading.Event | None,
) -> list[str] | None:
    with archive.open(worksheet, "r") as raw_member:
        member = _BoundedReader(
            raw_member,
            min(worksheet.file_size, _MAX_WORKSHEET_XML_BYTES),
            f"XLSX worksheet {worksheet.filename}",
            cancellation,
        )
        context = etree.iterparse(
            cast("BinaryIO", member),
            events=("end",),
            tag=f"{{{SHEET_NS}}}row",
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
        )
        for _, row in context:
            raise_if_cancelled(cancellation)
            parent = row.getparent()
            if parent is not None and local_name(parent.tag) == "sheetData":
                return _row_values(row, shared_strings)
            _clear_emitted_element(row)
        if member.bytes_read != worksheet.file_size:
            raise ValueError(f"XLSX worksheet size does not match its directory record: {worksheet.filename}")
    return None


def _row_values(row: _Element, shared_strings: _SharedStringStore) -> list[str]:
    cells = row.findall(f"{{{SHEET_NS}}}c")
    if len(cells) > _MAX_ROW_COLUMNS:
        raise ValueError(f"XLSX row has more than {_MAX_ROW_COLUMNS} cells")
    values: list[str] = []
    seen_columns: set[int] = set()
    for fallback_index, cell in enumerate(cells):
        column_index = _cell_column_index(cell, fallback_index)
        if column_index < 0 or column_index >= _MAX_ROW_COLUMNS:
            raise ValueError(f"XLSX cell column exceeds the {_MAX_ROW_COLUMNS}-column worksheet limit")
        if column_index in seen_columns:
            raise ValueError(f"XLSX row contains duplicate cell column {_column_reference(column_index)}")
        seen_columns.add(column_index)
        while len(values) <= column_index:
            values.append("")
        value = _cell_text(cell, shared_strings)
        _validate_cell_text(value, "XLSX cell")
        values[column_index] = value
    return values


def _cell_column_index(cell: _Element, fallback_index: int) -> int:
    reference = cell.attrib.get("r", "")
    letters = []
    for char in reference:
        if char.isalpha():
            letters.append(char)
            continue
        break
    if letters:
        return column_reference_to_index("".join(letters))
    return fallback_index


def _cell_text(cell: _Element, shared_strings: _SharedStringStore) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "s":
        value = find_child(cell, "v")
        if value is None or value.text is None:
            return ""
        with contextlib.suppress(ValueError):
            return shared_strings.get(int(value.text))
        return ""
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(f".//{{{SHEET_NS}}}t"))
    value = find_child(cell, "v")
    return value.text if value is not None and value.text is not None else ""


def _validate_cell_text(value: str, label: str) -> None:
    if len(value) > _MAX_CELL_CHARACTERS:
        raise ValueError(f"{label} exceeds Excel's {_MAX_CELL_CHARACTERS}-character limit")


def _replace_xlsx_targets(
    row: _Element,
    provider: _UnitProvider,
    document: Structure,
    unit_id: str,
    columns: Sequence[tuple[str | None, int]],
) -> None:
    for locale, index in columns:
        effective_locale = locale or document.target_locale
        unit = provider.get(unit_id, effective_locale)
        replacement = _replacement_for_unit(unit, effective_locale)
        if replacement is not None:
            _set_inline_string(_ensure_cell(row, index), replacement)


def _ensure_cell(row: _Element, column_index: int) -> _Element:
    if column_index < 0 or column_index >= _MAX_ROW_COLUMNS:
        raise ValueError(f"XLSX target column exceeds the {_MAX_ROW_COLUMNS}-column worksheet limit")
    cells = row.findall(f"{{{SHEET_NS}}}c")
    for fallback_index, cell in enumerate(cells):
        if _cell_column_index(cell, fallback_index) == column_index:
            return cell

    row_number = row.attrib.get("r", "1")
    new_cell = etree.Element(f"{{{SHEET_NS}}}c", r=f"{_column_reference(column_index)}{row_number}")
    for insert_index, cell in enumerate(cells):
        if _cell_column_index(cell, insert_index) > column_index:
            row.insert(insert_index, new_cell)
            return new_cell
    row.append(new_cell)
    return new_cell


def _set_inline_string(cell: _Element, value: str) -> None:
    _validate_cell_text(value, "XLSX replacement")
    tail = cell.tail
    reference = cell.attrib.get("r")
    style = cell.attrib.get("s")
    cell.clear()
    if reference is not None:
        cell.attrib["r"] = reference
    if style is not None:
        cell.attrib["s"] = style
    cell.attrib["t"] = "inlineStr"
    inline = etree.SubElement(cell, f"{{{SHEET_NS}}}is")
    text = etree.SubElement(inline, f"{{{SHEET_NS}}}t")
    if value[:1].isspace() or value[-1:].isspace():
        text.attrib[f"{{{XML_NS}}}space"] = "preserve"
    text.text = value
    cell.tail = tail


def _column_reference(index: int) -> str:
    value = index + 1
    parts: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        parts.append(chr(ord("A") + remainder))
    return "".join(reversed(parts))


def _write_xlsx_package(
    source: zipfile.ZipFile,
    infos: Sequence[zipfile.ZipInfo],
    output_stream: BinaryIO,
    worksheet: zipfile.ZipInfo,
    shared_strings: _SharedStringStore,
    provider: _UnitProvider,
    document: Structure,
    layout: ResolvedTabularLayout | None,
    columns: Sequence[tuple[str | None, int]],
    cancellation: threading.Event | None,
) -> None:
    with zipfile.ZipFile(output_stream, "w", allowZip64=True) as target:
        target.comment = source.comment
        for info in infos:
            raise_if_cancelled(cancellation)
            if info.filename == worksheet.filename and layout is not None:
                _rewrite_worksheet_member(
                    source,
                    target,
                    info,
                    shared_strings,
                    provider,
                    document,
                    layout,
                    columns,
                    cancellation,
                )
            else:
                _copy_xlsx_member(source, target, info, cancellation)


def _copy_xlsx_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    cancellation: threading.Event | None,
) -> None:
    copied_info = copy.copy(info)
    if info.is_dir():
        target.writestr(copied_info, b"")
        return
    with (
        source.open(info, "r") as source_member,
        target.open(copied_info, "w", force_zip64=info.file_size >= 2_000_000_000) as target_member,
    ):
        copied = 0
        while True:
            raise_if_cancelled(cancellation)
            chunk = source_member.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > info.file_size or copied > _MAX_MEMBER_BYTES:
                raise ValueError(f"XLSX ZIP entry exceeds its declared size: {info.filename}")
            target_member.write(chunk)
        if copied != info.file_size:
            raise ValueError(f"XLSX ZIP entry size does not match its directory record: {info.filename}")


def _rewrite_worksheet_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    shared_strings: _SharedStringStore,
    provider: _UnitProvider,
    document: Structure,
    layout: ResolvedTabularLayout,
    columns: Sequence[tuple[str | None, int]],
    cancellation: threading.Event | None,
) -> None:
    copied_info = copy.copy(info)
    data_row_index = 0
    worksheet_row_index = 0
    ids = IdentityRegistry()

    def rewrite_row(row: _Element) -> None:
        nonlocal data_row_index, worksheet_row_index
        parent = row.getparent()
        if parent is None or local_name(parent.tag) != "sheetData":
            return
        values = _row_values(row, shared_strings)
        is_header = worksheet_row_index == 0 and layout.has_header and not layout.include_header_as_data
        worksheet_row_index += 1
        if is_header:
            return
        unit_id = ids.resolve_tabular(values, data_row_index, layout.id_column, "xlsx")
        data_row_index += 1
        _replace_xlsx_targets(row, provider, document, unit_id, columns)

    with (
        source.open(info, "r") as source_member,
        target.open(copied_info, "w", force_zip64=True) as target_member,
    ):
        bounded_source = _BoundedReader(
            source_member,
            min(info.file_size, _MAX_WORKSHEET_XML_BYTES),
            f"XLSX worksheet {info.filename}",
            cancellation,
        )
        bounded_target = _BoundedWriter(
            target_member,
            _MAX_OUTPUT_WORKSHEET_BYTES,
            f"XLSX worksheet {info.filename}",
            cancellation,
        )
        _stream_xml_rewrite_stream(
            cast("BinaryIO", bounded_source),
            cast("BinaryIO", bounded_target),
            record_name="row",
            rewrite_record=rewrite_row,
            cancellation=cancellation,
        )
        if bounded_source.bytes_read != info.file_size:
            raise ValueError(f"XLSX worksheet size does not match its directory record: {info.filename}")


def _stream_xml_rewrite(
    source: Path,
    output: Path,
    *,
    record_name: str | tuple[str, ...],
    rewrite_record: Callable[[_Element], None],
    on_start: Callable[[_Element], None] | None = None,
    on_end: Callable[[_Element], None] | None = None,
    cancellation: threading.Event | None = None,
) -> None:
    with (
        atomic_output_path(output, "wb", cancellation=cancellation) as output_stream,
        source.open("rb") as input_stream,
    ):
        _stream_xml_rewrite_stream(
            input_stream,
            output_stream,
            record_name=record_name,
            rewrite_record=rewrite_record,
            on_start=on_start,
            on_end=on_end,
            cancellation=cancellation,
        )


def _stream_xml_rewrite_stream(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    record_name: str | tuple[str, ...],
    rewrite_record: Callable[[_Element], None],
    on_start: Callable[[_Element], None] | None = None,
    on_end: Callable[[_Element], None] | None = None,
    cancellation: threading.Event | None,
) -> None:
    record_names = (record_name,) if isinstance(record_name, str) else record_name
    frames: list[_XmlOutputFrame] = []
    epilog: list[bytes] = []
    epilog_bytes = 0
    record_depth = 0
    record_elements = 0
    record_characters = 0
    root_seen = False

    events = ("start", "end", "comment", "pi")
    context = etree.iterparse(
        input_stream,
        events=events,
        no_network=True,
        resolve_entities=False,
        load_dtd=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    with etree.xmlfile(output_stream, encoding="UTF-8", buffered=False) as raw_writer:
        writer = cast("_XmlWriter", raw_writer)
        writer.write_declaration()
        for event, raw_element in context:
            raise_if_cancelled(cancellation)
            element = cast("_Element", raw_element)
            if event == "start":
                if record_depth:
                    record_depth += 1
                    record_elements += 1
                    record_characters += sum(len(name) + len(value) for name, value in element.attrib.items())
                    _validate_xml_record_size(record_elements, record_characters)
                    continue

                if local_name(element.tag) in record_names:
                    if frames:
                        _write_frame_text(writer, frames[-1])
                    record_depth = 1
                    record_elements = 1
                    record_characters = sum(len(name) + len(value) for name, value in element.attrib.items())
                    _validate_xml_record_size(record_elements, record_characters)
                    continue

                if frames:
                    _write_frame_text(writer, frames[-1])
                else:
                    if root_seen:
                        raise ValueError("XML document contains multiple roots")
                    root_seen = True
                    doctype = cast("str", getattr(element.getroottree().docinfo, "doctype", ""))
                    if doctype:
                        writer.write_doctype(doctype)

                if on_start is not None:
                    on_start(element)
                tag = element.tag
                if not isinstance(tag, str):
                    raise TypeError("XML element tag must be a string")
                element_context = writer.element(tag, _output_attributes(element), _local_nsmap(element))
                element_context.__enter__()
                frames.append(_XmlOutputFrame(element, element_context))
                continue

            if event == "end":
                if record_depth:
                    record_characters += len(element.text or "") + len(element.tail or "")
                    _validate_xml_record_size(record_elements, record_characters)
                    record_depth -= 1
                    if record_depth == 0:
                        rewrite_record(element)
                        _write_completed_element(writer, element)
                        _clear_emitted_element(element)
                    continue

                if not frames or frames[-1].element is not element:
                    raise ValueError("XML event stream is not properly nested")
                frame = frames.pop()
                _write_frame_text(writer, frame)
                frame.context.__exit__(None, None, None)
                _write_tail(writer, element.tail)
                if on_end is not None:
                    on_end(element)
                _clear_emitted_element(element)
                continue

            if record_depth:
                record_elements += 1
                record_characters += len(element.text or "") + len(element.tail or "")
                _validate_xml_record_size(record_elements, record_characters)
                continue
            if frames:
                _write_frame_text(writer, frames[-1])
            elif root_seen:
                chunk = etree.tostring(element, encoding="UTF-8", with_tail=True)
                epilog_bytes += len(chunk)
                if epilog_bytes > _MAX_XML_EPILOG_BYTES:
                    raise ValueError("XML epilog exceeds its regeneration size limit")
                epilog.append(chunk)
                _clear_emitted_element(element)
                continue
            _write_completed_element(writer, element)
            _clear_emitted_element(element)

    for chunk in epilog:
        raise_if_cancelled(cancellation)
        output_stream.write(chunk)

    if record_depth or frames:
        raise ValueError("XML document ended before all elements were closed")
    if not root_seen:
        raise ValueError("XML document does not contain a root element")


def _validate_xml_record_size(elements: int, characters: int) -> None:
    if elements > _MAX_XML_RECORD_ELEMENTS:
        raise ValueError(f"XML record has more than {_MAX_XML_RECORD_ELEMENTS} elements")
    if characters > _MAX_XML_RECORD_CHARACTERS:
        raise ValueError("XML record exceeds its regeneration text limit")


def _write_frame_text(writer: _XmlWriter, frame: _XmlOutputFrame) -> None:
    if frame.text_written:
        return
    if frame.element.text is not None:
        writer.write(frame.element.text)
    frame.text_written = True


def _write_completed_element(writer: _XmlWriter, element: _Element) -> None:
    _write_element_without_tail(writer, element)
    _write_tail(writer, element.tail)


def _write_element_without_tail(writer: _XmlWriter, element: _Element) -> None:
    tag = cast("object", element.tag)
    if not isinstance(tag, str):
        writer.write(element, with_tail=False)
        return
    element_context = writer.element(tag, _output_attributes(element), _local_nsmap(element))
    element_context.__enter__()
    if element.text is not None:
        writer.write(element.text)
    for child in element:
        _write_element_without_tail(writer, child)
        _write_tail(writer, child.tail)
    element_context.__exit__(None, None, None)


def _write_tail(writer: _XmlWriter, tail: str | None) -> None:
    if tail is not None:
        writer.write(tail)


def _local_nsmap(element: _Element) -> dict[str | None, str] | None:
    parent = element.getparent()
    inherited = parent.nsmap if parent is not None else {}
    local = {prefix: uri for prefix, uri in element.nsmap.items() if uri != XML_NS and inherited.get(prefix) != uri}
    return local or None


def _output_attributes(element: _Element) -> dict[str, str]:
    attributes: dict[str, str] = {}
    xml_prefix = f"{{{XML_NS}}}"
    for raw_name, raw_value in element.attrib.items():
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else raw_name
        value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
        output_name = f"xml:{name[len(xml_prefix) :]}" if name.startswith(xml_prefix) else name
        attributes[output_name] = value
    return attributes


def _clear_emitted_element(element: _Element) -> None:
    element.clear()
    parent = element.getparent()
    if parent is None:
        return
    while element.getprevious() is not None:
        del parent[0]


def _iter_direct_children(parent: _Element, name: str) -> Iterator[_Element]:
    for child in parent:
        if local_name(child.tag) == name:
            yield child


def _replace_xliff_target(
    trans_unit: _Element,
    unit: Data | None,
    replacement: str,
    locale: str | None,
) -> None:
    source = find_child(trans_unit, "source")
    target = find_child(trans_unit, "target")
    if target is None:
        namespace = etree.QName(trans_unit).namespace
        target = etree.Element(f"{{{namespace}}}target" if namespace else "target")
        if source is None:
            trans_unit.insert(0, target)
        else:
            source.addnext(target)
    _replace_xml_payload(target, replacement, _target_parts(unit, locale), _target_tag_map(unit, locale))


def _replace_plain_xml_payload(element: _Element, value: str) -> None:
    _replace_xml_payload(element, value, (), {})


def _replace_xml_payload(
    element: _Element,
    value: str,
    parts: Sequence[SegmentPart],
    tag_map: Mapping[str, TieData],
) -> None:
    tail = element.tail
    for child in list(element):
        element.remove(child)
    element.text = None
    last_child: _Element | None = None
    effective_parts: Iterable[SegmentPart] = parts if parts else ()
    wrote_parts = False
    for part in effective_parts:
        wrote_parts = True
        if isinstance(part, TextPart):
            last_child = _append_text(element, last_child, part.value)
        elif isinstance(part, CodePart):
            tie = tag_map.get(part.ref)
            if tie is not None:
                child = _inline_placeholder(tie)
                element.append(child)
                last_child = child
    if not wrote_parts:
        element.text = value
    element.tail = tail


def _append_text(parent: _Element, last_child: _Element | None, value: str) -> _Element | None:
    if last_child is None:
        parent.text = (parent.text or "") + value
    else:
        last_child.tail = (last_child.tail or "") + value
    return last_child


def _inline_placeholder(tie: TieData) -> _Element:
    if tie.original_name:
        element = etree.Element(tie.original_name, dict(tie.attributes))
        element.text = tie.original_text
        return element
    element = etree.Element("ph", id=tie.id)
    return element


def _target_parts(unit: Data | None, locale: str | None) -> Sequence[SegmentPart]:
    tags = _selected_target_tags(unit, locale)
    if tags is not None:
        return tags.parts
    if unit is not None and unit.tags is not None:
        return unit.tags.target_parts
    return ()


def _target_tag_map(unit: Data | None, locale: str | None) -> Mapping[str, TieData]:
    tags = _selected_target_tags(unit, locale)
    if tags is not None:
        return tags.tag_map
    if unit is not None and unit.tags is not None:
        return unit.tags.target_tag_map
    return {}


def _selected_target_tags(unit: Data | None, locale: str | None) -> TargetTags | None:
    if unit is None or locale is None:
        return None
    target = unit.targets.get(locale)
    if target is None:
        return None
    return target.tags


def _xml_lang(element: _Element) -> str:
    value = element.attrib.get(f"{{{XML_NS}}}lang") or element.attrib.get("lang")
    return value if value is not None else ""


def _same_locale(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return (normalize_language_header(left) or left) == (normalize_language_header(right) or right)


def _write_po_block(
    out: TextWriter,
    provider: _UnitProvider,
    block: Sequence[str],
    locale: str | None,
) -> None:
    if not block:
        return
    updated = _updated_po_block(provider, block, locale)
    for line in updated:
        out.write(line)


def _updated_po_block(
    provider: _UnitProvider,
    block: Sequence[str],
    locale: str | None,
) -> Sequence[str]:
    if _is_obsolete_po_block(block):
        return block
    msgid = _po_field_value(block, "msgid")
    if msgid == "":
        return block
    msgctxt = _po_field_value(block, "msgctxt")
    unit_id = f"{msgctxt}\x04{msgid}" if msgctxt else msgid
    if _po_field_value(block, "msgid_plural"):
        return _updated_po_plural_block(provider, block, unit_id, locale)
    unit = provider.get(unit_id, locale)
    replacement = _replacement_for_unit(unit, locale)
    if replacement is None:
        return block
    return _replace_po_directive(block, "msgstr", replacement)


def _updated_po_plural_block(
    provider: _UnitProvider,
    block: Sequence[str],
    unit_id: str,
    locale: str | None,
) -> Sequence[str]:
    updated = list(block)
    base = provider.get(unit_id, locale)
    base_text = _replacement_for_unit(base, locale)
    if base_text is not None:
        updated = _replace_po_directive(updated, "msgstr[0]", base_text)
    for index in _po_plural_indexes(block):
        if index == 0:
            continue
        unit = provider.get(f"{unit_id}[{index}]", locale)
        replacement = _replacement_for_unit(unit, locale)
        if replacement is not None:
            updated = _replace_po_directive(updated, f"msgstr[{index}]", replacement)
    return updated


def _is_obsolete_po_block(block: Sequence[str]) -> bool:
    return any(line.startswith("#~") for line in block)


def _po_plural_indexes(block: Sequence[str]) -> tuple[int, ...]:
    indexes: list[int] = []
    for line in block:
        stripped = line.lstrip()
        if not stripped.startswith("msgstr["):
            continue
        close = stripped.find("]")
        if close < 0:
            continue
        with contextlib.suppress(ValueError):
            indexes.append(int(stripped[len("msgstr[") : close]))
    return tuple(indexes)


def _po_field_value(block: Sequence[str], directive: str) -> str:
    start = _po_directive_index(block, directive)
    if start < 0:
        return ""
    values: list[str] = []
    first = block[start].strip()[len(directive) :].strip()
    if first:
        values.append(_po_string_value(first))
    index = start + 1
    while index < len(block):
        candidate = block[index].strip()
        if not candidate.startswith('"'):
            break
        values.append(_po_string_value(candidate))
        index += 1
    return "".join(values)


def _po_directive_index(block: Sequence[str], directive: str) -> int:
    prefix = f"{directive} "
    for index, line in enumerate(block):
        if line.startswith(prefix):
            return index
    return -1


def _po_string_value(token: str) -> str:
    parsed = ast.literal_eval(token)
    return parsed if isinstance(parsed, str) else ""


def _replace_po_directive(block: Sequence[str], directive: str, value: str) -> list[str]:
    start = _po_directive_index(block, directive)
    replacement = f"{directive} {_po_quote(value)}\n"
    if start < 0:
        return [*block, replacement]
    end = start + 1
    while end < len(block) and block[end].strip().startswith('"'):
        end += 1
    return [*block[:start], replacement, *block[end:]]


def _po_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
