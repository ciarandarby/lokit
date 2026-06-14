from __future__ import annotations

from typing import TYPE_CHECKING

from python_calamine import CalamineWorkbook

from lokit.data.structure import Data
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.tabular import (
    ResolvedTabularLayout,
    TabularImportOptions,
    ensure_single_target,
    infer_locales_from_filename,
    make_tabular_data,
    parse_base_lang,
    resolve_tabular_layout,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

ExtractItem = tuple[str, Data]
CellValue = object


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

    def extract(self) -> Iterator[ExtractItem]:
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

        for index, row in enumerate(data_rows):
            yield make_tabular_data(row, index, layout, "xlsx", target_locale)

    def extract_targets(self) -> dict[str, dict[str, Data]]:
        targets: dict[str, dict[str, Data]] = {}
        rows = self._rows()
        first_row = next(rows, None)
        if first_row is None:
            return targets

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

        for target_locale in layout.target_columns:
            targets[target_locale] = {}
        for index, row in enumerate(data_rows):
            for target_locale in layout.target_columns:
                unit_id, data = make_tabular_data(row, index, layout, "xlsx", target_locale)
                targets[target_locale][unit_id] = data
        return targets

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)

    def _rows(self) -> Iterator[list[str]]:
        workbook = CalamineWorkbook.from_path(self.filepath)
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
