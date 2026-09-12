from __future__ import annotations

from typing import TYPE_CHECKING

from lokit._interchange_rust import CsvReader
from lokit.data.structure import Data
from lokit.diagnostics import _call_native
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.tabular import (
    ResolvedTabularLayout,
    TabularImportOptions,
    ensure_single_target,
    infer_locales_from_filename,
    parse_base_lang,
    resolve_tabular_layout,
)
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
TargetExtractRow = dict[str, ExtractItem]


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
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(runtime_placeholders, inline_placeholders, placeholder_syntaxes),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=False,
            inline_placeholders=False,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(
        self,
        runtime_placeholders: bool = False,
        inline_placeholders: bool = False,
        syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        reader = _call_native(CsvReader, str(self.filepath))
        try:
            first_row = reader.first_row
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

            reader.configure(layout)
            selected_syntaxes = [str(syntax) for syntax in syntaxes] if syntaxes is not None else None
            while batch := reader.read_batch(
                target_locale, runtime_placeholders, inline_placeholders, selected_syntaxes
            ):
                yield from batch
        finally:
            reader.close()

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
        reader = _call_native(CsvReader, str(self.filepath))
        try:
            first_row = reader.first_row
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
            self._update_layout(layout, layout.target_locale)

            reader.configure(layout)
            while batch := reader.read_target_batch():
                yield from batch
        finally:
            reader.close()

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
