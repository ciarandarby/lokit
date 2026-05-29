from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

from lokit.data.structure import Data, Meta, TranslationStatus

ExtractItem = tuple[str, Data]


@dataclass(slots=True)
class _AsyncExtractionResult:
    item: Optional[ExtractItem] = None
    error: Optional[BaseException] = None
    done: bool = False


class _AsyncJsonI18nExtraction:
    def __init__(self, extractor: JsonI18nExtractor) -> None:
        self._extractor = extractor
        self._queue: asyncio.Queue[_AsyncExtractionResult] = asyncio.Queue()
        self._producer: asyncio.Task[None] | None = None

    def __aiter__(self) -> _AsyncJsonI18nExtraction:
        return self

    async def __anext__(self) -> ExtractItem:
        if self._producer is None:
            self._start()
        result = await self._queue.get()
        if result.done:
            await self._finish()
            raise StopAsyncIteration
        if result.error is not None:
            await self._finish()
            raise result.error
        if result.item is None:
            await self._finish()
            raise StopAsyncIteration
        return result.item

    def _start(self) -> None:
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for item in self._extractor.extract():
                    loop.call_soon_threadsafe(
                        self._queue.put_nowait,
                        _AsyncExtractionResult(item=item),
                    )
            except BaseException as exc:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncExtractionResult(error=exc),
                )
            finally:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    _AsyncExtractionResult(done=True),
                )

        self._producer = asyncio.create_task(asyncio.to_thread(produce))

    async def _finish(self) -> None:
        if self._producer is not None:
            await self._producer


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
        return _AsyncJsonI18nExtraction(self)

    def _load_json(self, filepath: str) -> dict[str, Any]:
        with Path(filepath).open("r", encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
        return result

    def _flatten(
        self, obj: dict[str, Any], prefix: str = ""
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
