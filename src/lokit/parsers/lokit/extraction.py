from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from lokit.data.structure import Data
from lokit.parsers.async_bridge import AsyncExtractionBridge

if TYPE_CHECKING:
    from collections.abc import Iterator

ExtractItem = tuple[str, Data]
_NATIVE_BATCH_SIZE = 256
_ASYNC_BATCH_SIZE = 256


class NativeLokitReader(Protocol):
    @property
    def source_locale(self) -> str: ...

    @property
    def target_locale(self) -> str | None: ...

    @property
    def target_locales(self) -> list[str]: ...

    @property
    def format_version(self) -> str: ...

    @property
    def export_origin(self) -> str: ...

    @property
    def export_timestamp(self) -> str: ...

    @property
    def source_language(self) -> str | None: ...

    @property
    def target_language(self) -> str | None: ...

    @property
    def target_languages(self) -> list[str]: ...

    @property
    def extensions(self) -> dict[str, str]: ...

    @property
    def closed(self) -> bool: ...

    def read_batch(self, batch_size: int = _NATIVE_BATCH_SIZE) -> list[ExtractItem]: ...

    def close(self) -> None: ...


class LokitExtractor:
    def __init__(self, filepath: str, *, eager: bool = True) -> None:
        self.filepath = filepath
        self.source_locale = ""
        self.target_locale: str | None = None
        self.target_locales: tuple[str, ...] = ()
        self.format_version = "0.1"
        self.export_origin = ""
        self.export_timestamp = ""
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.target_languages: tuple[str, ...] = ()
        self.extensions: dict[str, str] = {}
        self._reader: NativeLokitReader | None = None
        if eager:
            self._ensure_reader()

    def extract(self) -> Iterator[ExtractItem]:
        reader = self._ensure_reader()
        try:
            while True:
                batch = reader.read_batch(_NATIVE_BATCH_SIZE)
                if not batch:
                    return
                yield from batch
        finally:
            reader.close()

    def extract_async(self) -> AsyncExtractionBridge[ExtractItem]:
        return AsyncExtractionBridge(self.extract, batch_size=_ASYNC_BATCH_SIZE)

    def _ensure_reader(self) -> NativeLokitReader:
        reader = self._reader
        if reader is None or reader.closed:
            reader = self._open_reader()
            self._reader = reader
            self._sync_metadata(reader)
        return reader

    def _open_reader(self) -> NativeLokitReader:
        from lokit._interchange_rust import LokitReader

        return LokitReader(self.filepath)

    def _sync_metadata(self, reader: NativeLokitReader) -> None:
        self.source_locale = reader.source_locale
        self.target_locale = reader.target_locale
        self.target_locales = tuple(reader.target_locales)
        self.format_version = reader.format_version
        self.export_origin = reader.export_origin
        self.export_timestamp = reader.export_timestamp
        self.source_language = reader.source_language
        self.target_language = reader.target_language
        self.target_languages = tuple(reader.target_languages)
        self.extensions = reader.extensions.copy()
