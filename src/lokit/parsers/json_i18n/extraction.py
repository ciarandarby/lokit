from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lokit._interchange_rust import JsonReader, json_object_roots
from lokit.data.structure import Data
from lokit.diagnostics import _call_native
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.tabular import normalize_language_header, parse_base_lang
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]


class JsonI18nExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
        target_filepaths: Mapping[str, str] | None = None,
    ) -> None:
        self.filepath = str(filepath)
        self.source_locale = normalize_language_header(source_locale) or source_locale
        self.target_locale = (
            normalize_language_header(target_locale) or target_locale if target_locale is not None else None
        )
        self.target_filepath = target_filepath
        self.target_filepaths = dict(target_filepaths or {})
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.target_locales: tuple[str, ...] = ()
        self.target_languages: tuple[str, ...] = ()
        self.export_origin = ""
        self.extensions: dict[str, str] = {"input_format": "json_i18n"}

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
        )

    def _extract(
        self,
        runtime_placeholders: bool = False,
        inline_placeholders: bool = False,
        syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        roots: dict[str, str] = {}
        if self.target_filepath is None and not self.target_filepaths:
            for name in json_object_roots(self.filepath):
                locale = normalize_language_header(name)
                if locale:
                    roots[locale] = name
        source_root: str | None = None
        targets: list[tuple[str, str, str | None]] = []
        if len(roots) >= 2:
            inferred = self._locale_from_filename(self.filepath)
            if self.source_locale not in roots:
                self.source_locale = inferred if inferred in roots and inferred is not None else next(iter(roots))
            source_root = roots[self.source_locale]
            for locale, name in roots.items():
                if locale != self.source_locale and (self.target_locale is None or locale == self.target_locale):
                    targets.append((locale, self.filepath, name))
        else:
            self.source_locale = self.source_locale or self._locale_from_filename(self.filepath) or ""
            if self.target_filepath is not None:
                self.target_locale = self.target_locale or self._locale_from_filename(self.target_filepath)
                targets.append((self.target_locale or "", str(self.target_filepath), None))
            target_files = {locale: path for locale, path, _ in targets}
            for locale, path in self.target_filepaths.items():
                target_files[normalize_language_header(locale) or locale] = str(path)
            targets = [(locale, path, None) for locale, path in target_files.items()]
        self.source_language = parse_base_lang(self.source_locale) if self.source_locale else None
        self.target_locales = (
            (self.target_locale,)
            if self.target_locale is not None
            else tuple(locale for locale, _, _ in targets if locale)
        )
        self.target_language = parse_base_lang(self.target_locale) if self.target_locale else None
        self.target_languages = tuple(parse_base_lang(locale) for locale in self.target_locales)
        reader = _call_native(JsonReader, self.filepath, source_root, targets, self.target_locale)
        try:
            selected_syntaxes = [str(syntax) for syntax in syntaxes] if syntaxes is not None else None
            while batch := reader.read_batch(runtime_placeholders, inline_placeholders, selected_syntaxes):
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

    @staticmethod
    def _locale_from_filename(filepath: str) -> str | None:
        path = Path(filepath)
        stem = path.stem
        if path.suffix.lower() != ".json" or not stem or not all(c.isalnum() or c in "_-" for c in stem):
            return None
        if len(stem) < 2 or not stem[:2].isalpha():
            return None
        return normalize_language_header(stem) or stem
