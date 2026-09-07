from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import (
    Data,
)
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.interchange import iter_native_data, iter_native_data_batches, open_native_reader
from lokit.parsers.projection import project_items
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from lokit.parsers.interchange import NativeReader
    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
_ASYNC_BATCH_SIZE = 64


class XliffExtractor:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.version = "1.2"
        self.source_locale: str | None = None
        self.target_locale: str | None = None
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.target_locales: tuple[str, ...] = ()
        self.target_languages: tuple[str, ...] = ()
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {"input_format": "xliff"}
        self._initialized = False
        self._native_reader: NativeReader | None = None
        self._on_metadata: Callable[[], None] | None = None

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
        self._initialize_from_file()
        return project_items(
            self._extract(runtime_placeholders, inline_placeholders, placeholder_syntaxes),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=self._native_syntax(),
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
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)
        try:
            yield from iter_native_data(
                native_reader,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                syntaxes=[str(syntax) for syntax in syntaxes] if syntaxes is not None else None,
                on_batch=lambda: self._sync_native_metadata(native_reader),
            )
        finally:
            self._sync_native_metadata(native_reader)

    def _initialize_from_file(self) -> None:
        if self._initialized:
            return
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)
        self._initialized = True

    def _extract_batches(
        self,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        syntaxes: Sequence[PlaceholderSyntax | str] | None,
    ) -> Iterator[list[ExtractItem]]:
        native_reader = self._ensure_native_reader()
        try:
            yield from iter_native_data_batches(
                native_reader,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                syntaxes=[str(syntax) for syntax in syntaxes] if syntaxes is not None else None,
                on_batch=lambda: self._sync_native_metadata(native_reader),
            )
        finally:
            self._sync_native_metadata(native_reader)

    def _ensure_native_reader(self) -> NativeReader:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            return reader
        reader = open_native_reader(self.filepath, "xliff")
        self._native_reader = reader
        return reader

    def close(self) -> None:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            reader.close()
        self._on_metadata = None

    def _sync_native_metadata(self, reader: NativeReader) -> None:
        self.version = reader.version
        self.extensions["xliff_version"] = reader.version
        self.source_locale = reader.source_locale
        self.target_locale = reader.target_locale
        self.source_language = reader.source_language
        self.target_language = reader.target_language
        self.target_locales = tuple(reader.target_locales)
        self.target_languages = tuple(reader.target_languages)
        self.export_origin = reader.export_origin
        self.export_timestamp = reader.export_timestamp
        self.extensions.update(reader.extensions)

        if self._on_metadata is not None:
            self._on_metadata()

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
        if not include_tags:
            return AsyncExtractionBridge.from_batches(
                lambda: self._extract_batches(runtime_placeholders, inline_placeholders, placeholder_syntaxes)
            )
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            ),
            batch_size=_ASYNC_BATCH_SIZE,
        )

    def _native_syntax(self) -> TagSyntax:
        if self.version.startswith("2.1"):
            return TagSyntax.XLIFF_21
        if self.version.startswith("2"):
            return TagSyntax.XLIFF_20
        return TagSyntax.XLIFF_12
