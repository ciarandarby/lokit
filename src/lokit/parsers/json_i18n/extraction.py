from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

from lokit.data.structure import Data, Meta, TargetData, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.tabular import normalize_language_header, parse_base_lang
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping

ExtractItem = tuple[str, Data]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def _unique_flat_key(
    flat: dict[str, tuple[str, tuple[str, ...]]],
    key: str,
) -> str:
    if key not in flat:
        return key
    index = 2
    while f"{key}#{index}" in flat:
        index += 1
    return f"{key}#{index}"


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
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        source_data = self._load_json(self.filepath)
        multilingual = self._multilingual_root(source_data)
        if multilingual:
            yield from self._extract_multilingual(source_data, multilingual)
            return

        source_flat = self._flatten(source_data)

        target_flats: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        if self.target_filepath is not None:
            target_data = self._load_json(self.target_filepath)
            locale = self.target_locale or self._locale_from_filename(self.target_filepath) or ""
            target_flats[locale] = self._flatten(target_data)
        for locale, filepath in self.target_filepaths.items():
            canonical = normalize_language_header(locale) or locale
            target_flats[canonical] = self._flatten(self._load_json(filepath))

        self._infer_locale()
        self._set_target_locales(tuple(locale for locale in target_flats if locale))

        seen_ids: dict[str, int] = {}
        for key, source_item in source_flat.items():
            source_value, path = source_item
            target_value = None
            targets: dict[str, TargetData] = {}
            for locale, target_flat in target_flats.items():
                target_item = target_flat.get(key)
                text = target_item[0] if target_item is not None else None
                if self.target_locale is not None and locale == self.target_locale:
                    target_value = text
                elif self.target_locale is None and locale:
                    targets[locale] = TargetData(
                        text=text,
                        status=TranslationStatus.TRANSLATED if text else TranslationStatus.NEW,
                    )
            status = TranslationStatus.TRANSLATED if target_value else TranslationStatus.NEW
            unit_id = self._unit_id(key, path, seen_ids)
            data = Data(
                source=source_value,
                target=target_value,
                targets=targets,
                meta=Meta(),
                status=status,
                extensions={
                    "input_format": "json_i18n",
                    "json_path": json.dumps(list(path), ensure_ascii=False),
                },
            )
            yield unit_id, data

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

    def _load_json(self, filepath: str) -> JsonObject:
        with Path(filepath).open("r", encoding="utf-8") as f:
            result = json.load(f)
        if not isinstance(result, dict):
            raise TypeError("Expected JSON object at translation root")
        return cast("JsonObject", result)

    def _flatten(
        self, obj: JsonObject, prefix: str = "", path: tuple[str, ...] = ()
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        flat: dict[str, tuple[str, tuple[str, ...]]] = {}
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            current_path = (*path, key)
            if isinstance(value, dict):
                for nested_key, nested_value in self._flatten(value, full_key, current_path).items():
                    flat[_unique_flat_key(flat, nested_key)] = nested_value
            elif isinstance(value, str):
                flat[_unique_flat_key(flat, full_key)] = (value, current_path)
        return flat

    def _extract_multilingual(
        self,
        source_data: JsonObject,
        locale_keys: tuple[str, ...],
    ) -> Iterator[ExtractItem]:
        source_locale = self._select_multilingual_source(locale_keys)
        source_root = source_data[source_locale]
        if not isinstance(source_root, dict):
            return
        self.source_locale = source_locale
        self.source_language = parse_base_lang(source_locale)
        target_locales = tuple(locale for locale in locale_keys if locale != source_locale)
        if self.target_locale is not None:
            target_locales = tuple(locale for locale in target_locales if locale == self.target_locale)
        self._set_target_locales(target_locales)

        source_flat = self._flatten(source_root)
        target_flats: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        for locale in target_locales:
            locale_root = source_data[locale]
            if isinstance(locale_root, dict):
                target_flats[locale] = self._flatten(locale_root)

        seen_ids: dict[str, int] = {}
        for key, source_item in source_flat.items():
            source_value, path = source_item
            selected_text = None
            targets: dict[str, TargetData] = {}
            for locale, target_flat in target_flats.items():
                target_item = target_flat.get(key)
                text = target_item[0] if target_item is not None else None
                if self.target_locale is not None:
                    selected_text = text
                else:
                    targets[locale] = TargetData(
                        text=text,
                        status=TranslationStatus.TRANSLATED if text else TranslationStatus.NEW,
                    )
            unit_id = self._unit_id(key, path, seen_ids)
            yield (
                unit_id,
                Data(
                    source=source_value,
                    target=selected_text,
                    targets=targets,
                    meta=Meta(),
                    status=TranslationStatus.TRANSLATED if selected_text else TranslationStatus.NEW,
                    extensions={
                        "input_format": "json_i18n",
                        "json_path": json.dumps(list(path), ensure_ascii=False),
                    },
                ),
            )

    def _multilingual_root(self, data: JsonObject) -> tuple[str, ...]:
        locales: list[str] = []
        for key, value in data.items():
            locale = normalize_language_header(key)
            if locale and isinstance(value, dict):
                locales.append(locale)
        if len(locales) >= 2:
            return tuple(locales)
        return ()

    def _select_multilingual_source(self, locales: tuple[str, ...]) -> str:
        if self.source_locale in locales:
            return self.source_locale
        inferred = self._locale_from_filename(self.filepath)
        if inferred in locales:
            return inferred
        return locales[0]

    def _unit_id(
        self,
        key: str,
        path: tuple[str, ...],
        seen: dict[str, int],
    ) -> str:
        count = seen.get(key, 0)
        seen[key] = count + 1
        if count == 0:
            return key
        return f"{key}#{count + 1}"

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
