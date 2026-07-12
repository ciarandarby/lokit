from __future__ import annotations

import csv
from typing import TYPE_CHECKING

from lokit.data.structure import Data
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
    from collections.abc import AsyncIterator, Iterator

ExtractItem = tuple[str, Data]


class CsvExtractor:
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
        self.extensions: dict[str, str] = {"input_format": "csv"}
        self.layout: ResolvedTabularLayout | None = None

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        with open(self.filepath, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            first_row = next(reader, None)
            if first_row is None:
                return

            layout = resolve_tabular_layout(
                first_row,
                len(first_row),
                self.options,
                self.source_locale,
                self.target_locale,
                "csv",
            )
            target_locale = ensure_single_target(layout, self._requested_target_locale)
            self._update_layout(layout, target_locale)

            rows: Iterator[list[str]]
            rows = reader if layout.has_header and not layout.include_header_as_data else _prepend(first_row, reader)

            for index, row in enumerate(rows):
                yield make_tabular_data(row, index, layout, "csv", target_locale)

    def extract_targets(self) -> dict[str, dict[str, Data]]:
        targets: dict[str, dict[str, Data]] = {}
        with open(self.filepath, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            first_row = next(reader, None)
            if first_row is None:
                return targets

            layout = resolve_tabular_layout(
                first_row,
                len(first_row),
                self.options,
                self.source_locale,
                self.target_locale,
                "csv",
            )
            self._update_layout(layout, layout.target_locale)

            rows: Iterator[list[str]]
            rows = reader if layout.has_header and not layout.include_header_as_data else _prepend(first_row, reader)

            for target_locale in layout.target_columns:
                targets[target_locale] = {}
            for index, row in enumerate(rows):
                for target_locale in layout.target_columns:
                    unit_id, data = make_tabular_data(row, index, layout, "csv", target_locale)
                    targets[target_locale][unit_id] = data
        return targets

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
            )
        )

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


def _prepend(first: list[str], rows: Iterator[list[str]]) -> Iterator[list[str]]:
    yield first
    yield from rows
