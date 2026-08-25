from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from lokit.data.structure import Data, Meta, TargetData, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.json_i18n.streaming import (
    LocaleRoot,
    inspect_locale_roots,
    iter_selected_root_leaves,
    iter_string_leaves,
)
from lokit.parsers.projection import project_items
from lokit.tabular import normalize_language_header, parse_base_lang
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
    from types import TracebackType

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]


class _JsonI18nIndex(AbstractContextManager["_JsonI18nIndex"]):
    """Disk-backed collision and target index for bounded-memory extraction."""

    def __init__(self) -> None:
        self._directory = TemporaryDirectory(prefix="lokit-json-i18n-")
        # AsyncExtractionBridge resumes bounded producer windows on worker-pool
        # threads. Access is serialized, but a window is not guaranteed to use
        # the same OS thread as its predecessor.
        self._connection = sqlite3.connect(
            Path(self._directory.name) / "index.sqlite3",
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-2048")
        self._connection.execute(
            "CREATE TABLE seen_keys (scope TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY (scope, key)) WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE key_suffixes ("
            "scope TEXT NOT NULL, base_key TEXT NOT NULL, next_suffix INTEGER NOT NULL, "
            "PRIMARY KEY (scope, base_key)"
            ") WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE target_values ("
            "flat_key TEXT NOT NULL, locale TEXT NOT NULL, text TEXT NOT NULL, "
            "PRIMARY KEY (flat_key, locale)"
            ") WITHOUT ROWID"
        )

    def __enter__(self) -> _JsonI18nIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._connection.close()
        finally:
            self._directory.cleanup()

    def add_target_file(self, locale: str, filepath: str) -> None:
        scope = f"target:{locale}"
        for path, text in iter_string_leaves(filepath):
            flat_key = self._unique_key(scope, ".".join(path))
            self._connection.execute(
                "INSERT OR REPLACE INTO target_values (flat_key, locale, text) VALUES (?, ?, ?)",
                (flat_key, locale, text),
            )
        self._connection.execute("DELETE FROM seen_keys WHERE scope = ?", (scope,))
        self._connection.execute("DELETE FROM key_suffixes WHERE scope = ?", (scope,))

    def add_multilingual_targets(self, filepath: str, roots: tuple[LocaleRoot, ...]) -> None:
        selections = {(root.member_name, root.occurrence): root.locale for root in roots}
        for locale, path, text in iter_selected_root_leaves(filepath, selections):
            scope = f"target:{locale}"
            flat_key = self._unique_key(scope, ".".join(path))
            self._connection.execute(
                "INSERT OR REPLACE INTO target_values (flat_key, locale, text) VALUES (?, ?, ?)",
                (flat_key, locale, text),
            )
        for root in roots:
            self._connection.execute("DELETE FROM seen_keys WHERE scope = ?", (f"target:{root.locale}",))
            self._connection.execute("DELETE FROM key_suffixes WHERE scope = ?", (f"target:{root.locale}",))

    def iter_source_file(self, filepath: str) -> Iterator[tuple[str, tuple[str, ...], str]]:
        for path, text in iter_string_leaves(filepath):
            yield self._unique_key("source", ".".join(path)), path, text

    def iter_multilingual_source(
        self,
        filepath: str,
        root: LocaleRoot,
    ) -> Iterator[tuple[str, tuple[str, ...], str]]:
        selection = {(root.member_name, root.occurrence): root.locale}
        for _locale, path, text in iter_selected_root_leaves(filepath, selection):
            yield self._unique_key("source", ".".join(path)), path, text

    def targets_for(self, flat_key: str) -> dict[str, str]:
        result: dict[str, str] = {}
        cursor = self._connection.execute(
            "SELECT locale, text FROM target_values WHERE flat_key = ?",
            (flat_key,),
        )
        for raw_locale, raw_text in cursor:
            result[cast("str", raw_locale)] = cast("str", raw_text)
        return result

    def _unique_key(self, scope: str, flat_key: str) -> str:
        try:
            self._connection.execute(
                "INSERT INTO seen_keys (scope, key) VALUES (?, ?)",
                (scope, flat_key),
            )
            return flat_key
        except sqlite3.IntegrityError:
            pass

        while True:
            suffix = self._next_suffix(scope, flat_key)
            candidate = f"{flat_key}#{suffix}"
            try:
                self._connection.execute(
                    "INSERT INTO seen_keys (scope, key) VALUES (?, ?)",
                    (scope, candidate),
                )
                return candidate
            except sqlite3.IntegrityError:
                continue

    def _next_suffix(self, scope: str, flat_key: str) -> int:
        row = self._connection.execute(
            "SELECT next_suffix FROM key_suffixes WHERE scope = ? AND base_key = ?",
            (scope, flat_key),
        ).fetchone()
        if row is None:
            suffix = 2
            self._connection.execute(
                "INSERT INTO key_suffixes (scope, base_key, next_suffix) VALUES (?, ?, ?)",
                (scope, flat_key, suffix + 1),
            )
            return suffix
        suffix = cast("int", row[0])
        self._connection.execute(
            "UPDATE key_suffixes SET next_suffix = ? WHERE scope = ? AND base_key = ?",
            (suffix + 1, scope, flat_key),
        )
        return suffix


class JsonI18nExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
        target_filepaths: Mapping[str, str] | None = None,
    ) -> None:
        self.filepath = filepath
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
        locale_roots = inspect_locale_roots(self.filepath)
        if len(locale_roots) >= 2:
            yield from self._extract_multilingual(locale_roots)
            return
        yield from self._extract_separate_files()

    def _extract_separate_files(self) -> Iterator[ExtractItem]:
        self._infer_locale()
        target_files: dict[str, str] = {}
        if self.target_filepath is not None:
            locale = self.target_locale or self._locale_from_filename(self.target_filepath) or ""
            target_files[locale] = self.target_filepath
        for locale, filepath in self.target_filepaths.items():
            canonical = normalize_language_header(locale) or locale
            target_files[canonical] = filepath

        self._set_target_locales(tuple(locale for locale in target_files if locale))
        with _JsonI18nIndex() as index:
            for locale, filepath in target_files.items():
                index.add_target_file(locale, filepath)
            for key, path, source_value in index.iter_source_file(self.filepath):
                yield key, self._make_data(source_value, path, index.targets_for(key), target_files)

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncIterator[ExtractItem]:
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

    def _extract_multilingual(
        self,
        locale_roots: tuple[LocaleRoot, ...],
    ) -> Iterator[ExtractItem]:
        source_root = self._select_multilingual_source(locale_roots)
        self.source_locale = source_root.locale
        self.source_language = parse_base_lang(source_root.locale)
        target_roots = tuple(root for root in locale_roots if root.locale != source_root.locale)
        if self.target_locale is not None:
            target_roots = tuple(root for root in target_roots if root.locale == self.target_locale)
        self._set_target_locales(tuple(root.locale for root in target_roots))

        with _JsonI18nIndex() as index:
            index.add_multilingual_targets(self.filepath, target_roots)
            target_files = {root.locale: self.filepath for root in target_roots}
            for key, path, source_value in index.iter_multilingual_source(self.filepath, source_root):
                yield key, self._make_data(source_value, path, index.targets_for(key), target_files)

    def _select_multilingual_source(self, roots: tuple[LocaleRoot, ...]) -> LocaleRoot:
        for root in roots:
            if root.locale == self.source_locale:
                return root
        inferred = self._locale_from_filename(self.filepath)
        for root in roots:
            if root.locale == inferred:
                return root
        return roots[0]

    def _make_data(
        self,
        source_value: str,
        path: tuple[str, ...],
        found_targets: Mapping[str, str],
        target_files: Mapping[str, str],
    ) -> Data:
        target_value = None
        targets: dict[str, TargetData] = {}
        for locale in target_files:
            text = found_targets.get(locale)
            if self.target_locale is not None and locale == self.target_locale:
                target_value = text
            elif self.target_locale is None and locale:
                targets[locale] = TargetData(
                    text=text,
                    status=TranslationStatus.TRANSLATED if text else TranslationStatus.NEW,
                )
        return Data(
            source=source_value,
            target=target_value,
            targets=targets,
            meta=Meta(),
            status=TranslationStatus.TRANSLATED if target_value else TranslationStatus.NEW,
            extensions={
                "input_format": "json_i18n",
                "json_path": json.dumps(list(path), ensure_ascii=False),
            },
        )

    def _infer_locale(self) -> None:
        if not self.source_locale:
            inferred = self._locale_from_filename(self.filepath)
            if inferred:
                self.source_locale = inferred
        if self.source_locale:
            self.source_language = parse_base_lang(self.source_locale)
        if not self.target_locale and self.target_filepath:
            inferred = self._locale_from_filename(self.target_filepath)
            if inferred:
                self.target_locale = inferred
        if self.target_locale:
            self.target_language = parse_base_lang(self.target_locale)

    def _locale_from_filename(self, filepath: str) -> str | None:
        path = Path(filepath)
        if path.suffix.lower() != ".json":
            return None
        stem = path.stem
        if not stem or not all(c.isalnum() or c in "_-" for c in stem):
            return None
        if len(stem) < 2 or not stem[:2].isalpha():
            return None
        return normalize_language_header(stem) or stem

    def _set_target_locales(self, locales: tuple[str, ...]) -> None:
        if self.target_locale is not None:
            self.target_locales = (self.target_locale,)
            self.target_language = parse_base_lang(self.target_locale)
            self.target_languages = (self.target_language,)
            return
        self.target_locales = locales
        self.target_languages = tuple(parse_base_lang(locale) for locale in locales)
