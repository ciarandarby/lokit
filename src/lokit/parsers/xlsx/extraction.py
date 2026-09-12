from __future__ import annotations

import stat
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from python_calamine import CalamineWorkbook

from lokit._interchange_rust import IdentityRegistry
from lokit.data.structure import Data
from lokit.diagnostics import _call_native
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.tabular import (
    ResolvedTabularLayout,
    TabularImportOptions,
    ensure_single_target,
    infer_locales_from_filename,
    make_tabular_data,
    parse_base_lang,
    resolve_tabular_layout,
)
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
TargetExtractRow = dict[str, ExtractItem]
CellValue = object

_MAX_ZIP_ENTRIES = 100_000
_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_WORKSHEET_BYTES = 512 * 1024 * 1024
_MAX_SHARED_STRINGS_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000.0
_MAX_MEMBER_NAME_BYTES = 4096


class XlsxExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        options: TabularImportOptions | None = None,
    ) -> None:
        self.filepath: str = filepath
        self.options: TabularImportOptions = options or TabularImportOptions()
        self._requested_target_locale: str | None = target_locale

        if source_locale:
            self.source_locale: str = source_locale
            self.target_locale: str | None = target_locale
        else:
            inferred_source, inferred_target = infer_locales_from_filename(filepath)
            self.source_locale = inferred_source
            self.target_locale = target_locale or inferred_target

        self.source_language: str | None = parse_base_lang(self.source_locale) if self.source_locale else None
        self.target_language: str | None = parse_base_lang(self.target_locale) if self.target_locale else None
        self.target_locales: tuple[str, ...] = ()
        self.target_languages: tuple[str, ...] = ()

        self.export_origin: str = ""
        self.export_timestamp: str = ""
        self.extensions: dict[str, str] = {"input_format": "xlsx"}
        self.layout: ResolvedTabularLayout | None = None

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        rows = self._rows()
        first_row = next(rows, None)
        if first_row is None:
            return

        layout = resolve_tabular_layout(
            first_row,
            len(first_row),
            self.options,
            self.source_locale,
            self.target_locale,
            "xlsx",
        )
        target_locale = ensure_single_target(layout, self._requested_target_locale)
        self._update_layout(layout, target_locale)

        data_rows: Iterator[list[str]]
        data_rows = rows if layout.has_header and not layout.include_header_as_data else _prepend(first_row, rows)

        ids = _call_native(IdentityRegistry)
        for index, row in enumerate(data_rows):
            unit_id = ids.resolve_tabular(row, index, layout.id_column, "xlsx")
            yield make_tabular_data(row, layout, unit_id, target_locale)

    def extract_targets(self) -> dict[str, dict[str, Data]]:
        targets: dict[str, dict[str, Data]] = {}
        for row in self.extract_target_rows():
            for target_locale, item in row.items():
                unit_id, data = item
                targets.setdefault(target_locale, {})[unit_id] = data
        if self.layout is not None:
            for target_locale in self.layout.target_columns:
                targets.setdefault(target_locale, {})
        return targets

    def extract_target_rows(self) -> Iterator[TargetExtractRow]:
        rows = self._rows()
        first_row = next(rows, None)
        if first_row is None:
            return

        layout = resolve_tabular_layout(
            first_row,
            len(first_row),
            self.options,
            self.source_locale,
            self.target_locale,
            "xlsx",
        )
        self._update_layout(layout, layout.target_locale)

        data_rows: Iterator[list[str]]
        data_rows = rows if layout.has_header and not layout.include_header_as_data else _prepend(first_row, rows)

        ids = _call_native(IdentityRegistry)
        for index, row in enumerate(data_rows):
            unit_id = ids.resolve_tabular(row, index, layout.id_column, "xlsx")
            target_row: TargetExtractRow = {}
            for target_locale in layout.target_columns:
                target_row[target_locale] = make_tabular_data(row, layout, unit_id, target_locale)
            yield target_row

    def extract_async(
        self,
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
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
        )

    def _rows(self) -> Iterator[list[str]]:
        _preflight_workbook(Path(self.filepath))
        workbook = CalamineWorkbook.from_path(self.filepath)
        try:
            sheet_names: Sequence[str] = workbook.sheet_names
            if not sheet_names:
                return

            if self.options.sheet_name:
                sheet = workbook.get_sheet_by_name(self.options.sheet_name)
            else:
                if self.options.sheet_index < 0 or self.options.sheet_index >= len(sheet_names):
                    raise ValueError(f"XLSX sheet index {self.options.sheet_index} does not resolve")
                sheet = workbook.get_sheet_by_name(sheet_names[self.options.sheet_index])

            for row in sheet.iter_rows():
                yield [_cell_str(value) for value in row]
        finally:
            workbook.close()

    def _update_layout(
        self,
        layout: ResolvedTabularLayout,
        target_locale: str | None,
    ) -> None:
        self.layout = layout
        self.source_locale = layout.source_locale
        self.target_locale = target_locale
        self.target_locales = (target_locale,) if target_locale else layout.target_locales
        self.source_language = layout.source_language
        self.target_language = parse_base_lang(target_locale) if target_locale else None
        self.target_languages = (parse_base_lang(target_locale),) if target_locale else layout.target_languages


def _cell_str(value: CellValue) -> str:
    if value is None:
        return ""
    return str(value)


def _prepend(first: list[str], rows: Iterator[list[str]]) -> Iterator[list[str]]:
    yield first
    yield from rows


def _preflight_workbook(path: Path) -> None:
    if path.stat().st_size > _MAX_COMPRESSED_BYTES:
        raise ValueError("XLSX package exceeds its compressed size limit")
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise ValueError(f"XLSX package has more than {_MAX_ZIP_ENTRIES} ZIP entries")

        names: set[str] = set()
        compressed_bytes = 0
        uncompressed_bytes = 0
        for info in infos:
            _validate_member_name(info.filename)
            if info.filename in names:
                raise ValueError(f"duplicate XLSX ZIP entry: {info.filename}")
            names.add(info.filename)
            if info.flag_bits & 0x1:
                raise ValueError("encrypted XLSX ZIP entries are not supported")
            if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                raise ValueError(f"symbolic-link XLSX ZIP entry is not supported: {info.filename}")
            if info.file_size > _MAX_MEMBER_BYTES:
                raise ValueError(f"XLSX ZIP entry exceeds its size limit: {info.filename}")
            if info.filename.startswith("xl/worksheets/") and info.file_size > _MAX_WORKSHEET_BYTES:
                raise ValueError(f"XLSX worksheet exceeds its decompression limit: {info.filename}")
            if info.filename == "xl/sharedStrings.xml" and info.file_size > _MAX_SHARED_STRINGS_BYTES:
                raise ValueError("XLSX shared strings exceed their decompression limit")
            compressed_bytes += info.compress_size
            uncompressed_bytes += info.file_size
            if compressed_bytes > _MAX_COMPRESSED_BYTES:
                raise ValueError("XLSX package exceeds its compressed size limit")
            if uncompressed_bytes > _MAX_UNCOMPRESSED_BYTES:
                raise ValueError("XLSX package exceeds its decompression limit")
            if info.file_size and (
                info.compress_size == 0 or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise ValueError(f"suspicious compression ratio in XLSX ZIP entry: {info.filename}")


def _validate_member_name(name: str) -> None:
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
