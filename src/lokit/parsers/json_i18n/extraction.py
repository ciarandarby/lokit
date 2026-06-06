from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator, Iterator, TypeAlias, cast

from lokit.data.structure import Data, Meta, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge

ExtractItem = tuple[str, Data]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class JsonI18nExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
    ) -> None:
        self.filepath = filepath
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.target_filepath = target_filepath
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.export_origin = ""
        self.extensions: dict[str, str] = {"input_format": "json_i18n"}

    def extract(self) -> Iterator[ExtractItem]:
        source_data = self._load_json(self.filepath)
        source_flat = self._flatten(source_data)

        target_flat: dict[str, str] = {}
        if self.target_filepath is not None:
            target_data = self._load_json(self.target_filepath)
            target_flat = self._flatten(target_data)

        self._infer_locale()

        for key, source_value in source_flat.items():
            target_value = target_flat.get(key)
            status = (
                TranslationStatus.TRANSLATED
                if target_value
                else TranslationStatus.NEW
            )
            data = Data(
                source=source_value,
                target=target_value,
                meta=Meta(),
                status=status,
                extensions={"input_format": "json_i18n"},
            )
            yield key, data

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)

    def _load_json(self, filepath: str) -> JsonObject:
        with Path(filepath).open("r", encoding="utf-8") as f:
            result = json.load(f)
        if not isinstance(result, dict):
            raise TypeError("Expected JSON object at translation root")
        return cast(JsonObject, result)

    def _flatten(
        self, obj: JsonObject, prefix: str = ""
    ) -> dict[str, str]:
        flat: dict[str, str] = {}
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(self._flatten(value, full_key))
            elif isinstance(value, str):
                flat[full_key] = value
        return flat

    def _infer_locale(self) -> None:
        if not self.source_locale:
            inferred = self._locale_from_filename(self.filepath)
            if inferred:
                self.source_locale = inferred
        if self.source_locale:
            self.source_language = self._base_language(self.source_locale)
        if not self.target_locale and self.target_filepath:
            inferred = self._locale_from_filename(self.target_filepath)
            if inferred:
                self.target_locale = inferred
        if self.target_locale:
            self.target_language = self._base_language(self.target_locale)

    def _locale_from_filename(self, filepath: str) -> str | None:
        path = Path(filepath)
        if path.suffix.lower() != ".json":
            return None
        stem = path.stem
        if not stem or not all(c.isalnum() or c in "_-" for c in stem):
            return None
        if len(stem) < 2 or not stem[:2].isalpha():
            return None
        return stem

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
