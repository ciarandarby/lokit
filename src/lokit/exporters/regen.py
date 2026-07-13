from __future__ import annotations

import ast
import asyncio
import contextlib
import csv
import json
import os
import posixpath
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias, cast

from lxml import etree

from lokit.data.structure import BaseStructure, CodePart, Data, SegmentPart, StreamingStructure, TargetTags, TextPart
from lokit.data.targets import target_text
from lokit.exporters.docx import export_docx, export_docx_async
from lokit.exporters.html import export_html, export_html_async
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.exporters.pptx import export_pptx, export_pptx_async
from lokit.io.atomic import atomic_output_path
from lokit.parsers.tmx.xml_utils import find_child, local_name
from lokit.tabular import (
    ResolvedTabularLayout,
    build_import_options,
    column_reference_to_index,
    make_tabular_data,
    normalize_language_header,
    resolve_tabular_layout,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData
    from lokit.office.models import DocumentSource, OfficeExportResult

Structure = BaseStructure | StreamingStructure
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class TextWriter(Protocol):
    def write(self, value: str) -> int: ...


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
    def __init__(self, document: Structure) -> None:
        self._document = document
        self._items: Iterator[tuple[str, Data]] | None = None
        self._last_unit_id = ""
        self._last_unit: Data | None = None
        if isinstance(document, StreamingStructure):
            self._items = iter(document.items)

    def get(self, unit_id: str, locale: str | None = None) -> Data | None:
        if isinstance(self._document, BaseStructure):
            return self._document.data.get(unit_id)

        cached = self._last_unit
        if cached is not None and self._last_unit_id == unit_id and _has_replacement(cached, locale):
            return cached

        if self._items is None:
            return None

        for next_id, next_unit in self._items:
            self._last_unit_id = next_id
            self._last_unit = next_unit
            if next_id == unit_id and _has_replacement(next_unit, locale):
                return next_unit
        return None


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
    provider = _UnitProvider(document)

    with source.open("r", newline="", encoding="utf-8-sig") as input_stream:
        reader = csv.reader(input_stream)
        first_row = next(reader, None)
        if first_row is None:
            with atomic_output_path(output, "w"):
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

        with atomic_output_path(output, "w") as output_stream:
            writer = csv.writer(output_stream)
            data_rows: Iterable[list[str]] = reader
            if layout.has_header and not layout.include_header_as_data:
                writer.writerow(first_row)
            else:
                data_rows = _prepend_row(first_row, reader)

            for row_index, row in enumerate(data_rows):
                unit_id, _ = make_tabular_data(row, row_index, layout, "csv", _layout_target_locale(layout))
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
) -> None:
    await asyncio.to_thread(
        regen_csv,
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
    provider = _UnitProvider(document)

    with zipfile.ZipFile(source, "r") as archive:
        worksheet_path = _worksheet_path(archive, sheet_name, sheet_index)
        shared_strings = _shared_strings(archive)
        root = etree.fromstring(archive.read(worksheet_path))
        rows = _worksheet_rows(root)
        if not rows:
            _copy_zip_with_replacements(archive, output, {})
            return

        first = _row_values(rows[0], shared_strings)
        layout = resolve_tabular_layout(
            first,
            len(first),
            options,
            source_locale or document.source_locale,
            target_locale or document.target_locale,
            "xlsx",
        )
        columns = _target_columns_for_layout(document, layout, target_locale)
        data_rows = rows[1:] if layout.has_header and not layout.include_header_as_data else rows
        for row_index, row in enumerate(data_rows):
            values = _row_values(row, shared_strings)
            unit_id, _ = make_tabular_data(values, row_index, layout, "xlsx", _layout_target_locale(layout))
            _replace_xlsx_targets(row, provider, document, unit_id, columns)

        replacement = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        _copy_zip_with_replacements(archive, output, {worksheet_path: replacement})


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
) -> None:
    await asyncio.to_thread(
        regen_xlsx,
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
    )


def regen_xliff(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    provider = _UnitProvider(document)
    file_index = 0
    file_stack: list[tuple[int, str | None]] = []

    def start_element(element: _Element) -> None:
        nonlocal file_index
        if local_name(element.tag) != "file":
            return
        file_stack.append((file_index, element.attrib.get("target-language")))
        file_index += 1

    def end_element(element: _Element) -> None:
        if local_name(element.tag) == "file":
            file_stack.pop()

    def rewrite_unit(trans_unit: _Element) -> None:
        if not file_stack:
            return
        current_file_index, file_locale = file_stack[-1]
        raw_unit_id = trans_unit.attrib.get("id", "")
        unit_id = raw_unit_id or str(current_file_index)
        locale = target_locale or file_locale or document.target_locale
        unit = provider.get(unit_id, locale)
        replacement = _replacement_for_unit(unit, locale)
        if replacement is not None:
            _replace_xliff_target(trans_unit, unit, replacement, locale)

    _stream_xml_rewrite(
        Path(original_filepath),
        Path(output_path),
        record_name="trans-unit",
        rewrite_record=rewrite_unit,
        on_start=start_element,
        on_end=end_element,
    )


async def regen_xliff_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    await asyncio.to_thread(regen_xliff, document, original_filepath, output_path, target_locale=target_locale)


def regen_tmx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    provider = _UnitProvider(document)
    generated_index = 0

    def rewrite_unit(tu: _Element) -> None:
        nonlocal generated_index
        unit_id = tu.attrib.get("tuid")
        if unit_id is None:
            unit_id = f"auto_{generated_index}"
            generated_index += 1
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
    )


async def regen_tmx_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    await asyncio.to_thread(regen_tmx, document, original_filepath, output_path, target_locale=target_locale)


def regen_po(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    provider = _UnitProvider(document)
    locale = target_locale or document.target_locale
    with (
        Path(original_filepath).open("r", encoding="utf-8") as source,
        atomic_output_path(Path(output_path), "w") as out,
    ):
        block: list[str] = []
        for line in source:
            if line.strip():
                block.append(line)
                continue
            _write_po_block(out, provider, block, locale)
            block = []
            out.write(line)
        _write_po_block(out, provider, block, locale)


async def regen_po_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> None:
    await asyncio.to_thread(regen_po, document, original_filepath, output_path, target_locale=target_locale)


def regen_json_i18n(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
) -> None:
    source = _load_json_object(Path(original_filepath))
    path_map = _json_path_map(document)
    selected_locale = target_locale or document.target_locale
    result = _replace_json_document(source, path_map, document, selected_locale)
    with atomic_output_path(Path(output_path), "w") as out:
        json.dump(result, out, ensure_ascii=False, indent=indent)
        out.write("\n")


async def regen_json_i18n_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
) -> None:
    await asyncio.to_thread(
        regen_json_i18n,
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
    )


def regen_html(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
) -> None:
    export_html(document, output_path, original_filepath)


async def regen_html_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
) -> None:
    await export_html_async(document, output_path, original_filepath)


def regen_idml(
    document: BaseStructure,
    original_filepath: str | Path,
    output_path: str | Path,
) -> None:
    export_idml(document, output_path, original_filepath)


async def regen_idml_async(
    document: BaseStructure,
    original_filepath: str | Path,
    output_path: str | Path,
) -> None:
    await export_idml_async(document, output_path, original_filepath)


def regen_docx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> OfficeExportResult:
    return export_docx(document, output_path, original_filepath, target_locale=target_locale)


async def regen_docx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> OfficeExportResult:
    return await export_docx_async(document, output_path, original_filepath, target_locale=target_locale)


def regen_pptx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> OfficeExportResult:
    return export_pptx(document, output_path, original_filepath, target_locale=target_locale)


async def regen_pptx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
) -> OfficeExportResult:
    return await export_pptx_async(document, output_path, original_filepath, target_locale=target_locale)


def _has_replacement(unit: Data, locale: str | None) -> bool:
    return target_text(unit, locale) is not None


def _replacement_for_unit(unit: Data | None, locale: str | None) -> str | None:
    if unit is None:
        return None
    return target_text(unit, locale)


def _prepend_row(first: list[str], rows: Iterable[list[str]]) -> Iterator[list[str]]:
    yield first
    yield from rows


def _layout_target_locale(layout: ResolvedTabularLayout) -> str | None:
    return layout.target_locale


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


def _worksheet_path(archive: zipfile.ZipFile, sheet_name: str, sheet_index: int) -> str:
    workbook = etree.fromstring(archive.read("xl/workbook.xml"))
    sheets = [sheet for sheet in workbook.findall(f".//{{{SHEET_NS}}}sheet")]
    if not sheets:
        raise ValueError("XLSX workbook does not contain worksheets")
    selected = _select_sheet(sheets, sheet_name, sheet_index)
    relationship_id = selected.attrib.get(f"{{{OFFICE_REL_NS}}}id")
    if not relationship_id:
        raise ValueError("XLSX worksheet relationship is missing")

    rels = etree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") != relationship_id:
            continue
        target = rel.attrib.get("Target", "")
        if not target:
            break
        if target.startswith("/"):
            return target.lstrip("/")
        return posixpath.normpath(posixpath.join("xl", target))
    raise ValueError(f"XLSX worksheet relationship {relationship_id!r} does not resolve")


def _select_sheet(sheets: Sequence[_Element], sheet_name: str, sheet_index: int) -> _Element:
    if sheet_name:
        for sheet in sheets:
            if sheet.attrib.get("name") == sheet_name:
                return sheet
        raise ValueError(f"XLSX sheet {sheet_name!r} does not resolve")
    if sheet_index < 0 or sheet_index >= len(sheets):
        raise ValueError(f"XLSX sheet index {sheet_index} does not resolve")
    return sheets[sheet_index]


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    with contextlib.suppress(KeyError):
        root = etree.fromstring(archive.read("xl/sharedStrings.xml"))
        values: list[str] = []
        for si in root.findall(f"{{{SHEET_NS}}}si"):
            values.append("".join(t.text or "" for t in si.findall(f".//{{{SHEET_NS}}}t")))
        return values
    return []


def _worksheet_rows(root: _Element) -> list[_Element]:
    return root.findall(f".//{{{SHEET_NS}}}sheetData/{{{SHEET_NS}}}row")


def _row_values(row: _Element, shared_strings: Sequence[str]) -> list[str]:
    cells = row.findall(f"{{{SHEET_NS}}}c")
    values: list[str] = []
    for fallback_index, cell in enumerate(cells):
        column_index = _cell_column_index(cell, fallback_index)
        while len(values) <= column_index:
            values.append("")
        values[column_index] = _cell_text(cell, shared_strings)
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


def _cell_text(cell: _Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "s":
        value = find_child(cell, "v")
        if value is None or value.text is None:
            return ""
        with contextlib.suppress(ValueError, IndexError):
            return shared_strings[int(value.text)]
        return ""
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(f".//{{{SHEET_NS}}}t"))
    value = find_child(cell, "v")
    return value.text if value is not None and value.text is not None else ""


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


def _copy_zip_with_replacements(
    source: zipfile.ZipFile,
    output_path: Path,
    replacements: Mapping[str, bytes],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w") as target:
            for info in source.infolist():
                data = replacements.get(info.filename)
                if data is not None:
                    target.writestr(info, data)
                    continue
                with source.open(info, "r") as source_member, target.open(info, "w") as target_member:
                    shutil.copyfileobj(source_member, target_member, length=1024 * 1024)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def _stream_xml_rewrite(
    source: Path,
    output: Path,
    *,
    record_name: str,
    rewrite_record: Callable[[_Element], None],
    on_start: Callable[[_Element], None] | None = None,
    on_end: Callable[[_Element], None] | None = None,
) -> None:
    frames: list[_XmlOutputFrame] = []
    epilog: list[bytes] = []
    record_depth = 0
    root_seen = False

    with source.open("rb") as input_stream, atomic_output_path(output, "wb") as output_stream:
        events = ("start", "end", "comment", "pi")
        context = etree.iterparse(
            input_stream,
            events=events,
            no_network=True,
            resolve_entities=False,
            remove_blank_text=False,
        )
        with etree.xmlfile(output_stream, encoding="UTF-8", buffered=False) as raw_writer:
            writer = cast("_XmlWriter", raw_writer)
            writer.write_declaration()
            for event, raw_element in context:
                element = cast("_Element", raw_element)
                if event == "start":
                    if record_depth:
                        record_depth += 1
                        continue

                    if local_name(element.tag) == record_name:
                        if frames:
                            _write_frame_text(writer, frames[-1])
                        record_depth = 1
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
                    continue
                if frames:
                    _write_frame_text(writer, frames[-1])
                elif root_seen:
                    epilog.append(etree.tostring(element, encoding="UTF-8", with_tail=True))
                    _clear_emitted_element(element)
                    continue
                _write_completed_element(writer, element)
                _clear_emitted_element(element)

        for chunk in epilog:
            output_stream.write(chunk)

    if record_depth or frames:
        raise ValueError("XML document ended before all elements were closed")
    if not root_seen:
        raise ValueError("XML document does not contain a root element")


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
    local = {
        prefix: uri
        for prefix, uri in element.nsmap.items()
        if uri != XML_NS and inherited.get(prefix) != uri
    }
    return local or None


def _output_attributes(element: _Element) -> dict[str, str]:
    attributes: dict[str, str] = {}
    xml_prefix = f"{{{XML_NS}}}"
    for raw_name, raw_value in element.attrib.items():
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else raw_name
        value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
        output_name = f"xml:{name[len(xml_prefix):]}" if name.startswith(xml_prefix) else name
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
        target = etree.Element(f"{{{XLIFF_NS}}}target")
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


def _load_json_object(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as source:
        parsed = json.load(source)
    if not isinstance(parsed, dict):
        raise TypeError("Expected JSON object at translation root")
    return cast("JsonObject", parsed)


def _json_path_map(document: Structure) -> dict[tuple[str, ...], Data]:
    paths: dict[tuple[str, ...], Data] = {}
    for unit_id, unit in _iter_items(document):
        paths[_json_unit_path(unit_id, unit)] = unit
    return paths


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _json_unit_path(unit_id: str, unit: Data) -> tuple[str, ...]:
    raw = unit.extensions.get("json_path")
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return tuple(parsed)
    return tuple(unit_id.split("."))


def _replace_json_document(
    source: JsonObject,
    path_map: Mapping[tuple[str, ...], Data],
    document: Structure,
    selected_locale: str | None,
) -> JsonObject:
    locales = _json_target_locales(document, selected_locale)
    if _is_multilingual_json(source, document, locales):
        return _replace_multilingual_json(source, path_map, document, locales)
    return cast("JsonObject", _replace_json_value(source, (), path_map, selected_locale))


def _replace_multilingual_json(
    source: JsonObject,
    path_map: Mapping[tuple[str, ...], Data],
    document: Structure,
    locales: Sequence[str],
) -> JsonObject:
    result = dict(source)
    source_root = source.get(document.source_locale)
    for locale in locales:
        existing = source.get(locale)
        base = existing if isinstance(existing, dict) else source_root
        if isinstance(base, dict):
            result[locale] = _replace_json_value(base, (), path_map, locale)
    return result


def _replace_json_value(
    value: JsonValue,
    path: tuple[str, ...],
    path_map: Mapping[tuple[str, ...], Data],
    locale: str | None,
) -> JsonValue:
    if isinstance(value, dict):
        replaced: JsonObject = {}
        for key, child in value.items():
            replaced[key] = _replace_json_value(child, (*path, key), path_map, locale)
        return replaced
    if isinstance(value, list):
        return [_replace_json_value(child, path, path_map, locale) for child in value]
    if isinstance(value, str):
        unit = path_map.get(path)
        replacement = _replacement_for_unit(unit, locale)
        return replacement if replacement is not None else value
    return value


def _json_target_locales(document: Structure, selected_locale: str | None) -> tuple[str, ...]:
    if selected_locale is not None:
        return (selected_locale,)
    if document.target_locales:
        return document.target_locales
    if document.target_locale is not None:
        return (document.target_locale,)
    return ()


def _is_multilingual_json(
    source: JsonObject,
    document: Structure,
    locales: Sequence[str],
) -> bool:
    if document.source_locale not in source or not isinstance(source[document.source_locale], dict):
        return False
    return any(locale in source or locale in document.target_locales for locale in locales)
