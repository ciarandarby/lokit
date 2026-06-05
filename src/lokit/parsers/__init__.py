from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.parsers.csv.extraction import CsvExtractor
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.idml.extraction import IdmlExtractor
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.po.extraction import PoExtractor
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor

if TYPE_CHECKING:
    from lokit.io.stream_json import LokitJsonContext

ExtractItem = tuple[str, Data]


class read:
    @staticmethod
    def file(filepath: str) -> BaseStructure:
        from lokit.importers import import_file

        return import_file(filepath)

    @staticmethod
    def tmx(
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: TmxParseMode = TmxParseMode.FULL,
    ) -> BaseStructure:
        from lokit.importers import import_tmx

        return import_tmx(filepath, source_language, target_language, domain, mode)

    @staticmethod
    def tmx_parallel(
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: TmxParseMode = TmxParseMode.FULL,
        options: TmxParallelOptions | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_tmx_parallel

        return import_tmx_parallel(
            filepath,
            source_language,
            target_language,
            domain,
            mode,
            options,
        )

    @staticmethod
    def xliff(filepath: str) -> BaseStructure:
        from lokit.importers import import_xliff

        return import_xliff(filepath)

    @staticmethod
    def csv(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_csv

        return import_csv(filepath, source_locale, target_locale)

    @staticmethod
    def xlsx(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_xlsx

        return import_xlsx(filepath, source_locale, target_locale)

    @staticmethod
    def html(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_html

        return import_html(filepath, source_locale, target_locale)

    @staticmethod
    def po(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_po

        return import_po(filepath, source_locale, target_locale)

    @staticmethod
    def json(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_json_i18n

        return import_json_i18n(filepath, source_locale, target_locale, target_filepath)

    @staticmethod
    def json_i18n(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
    ) -> BaseStructure:
        return read.json(filepath, source_locale, target_locale, target_filepath)

    @staticmethod
    def idml(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> BaseStructure:
        from lokit.importers import import_idml

        return import_idml(filepath, source_locale, target_locale)


class stream:
    @staticmethod
    def tmx(
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        mode: TmxParseMode = TmxParseMode.FULL,
    ) -> StreamingStructure:
        from lokit.importers import stream_tmx

        return stream_tmx(filepath, source_language, target_language, mode)

    @staticmethod
    def tmx_parallel(
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: TmxParseMode = TmxParseMode.FULL,
        options: TmxParallelOptions | None = None,
    ) -> StreamingStructure:
        from lokit.importers import stream_tmx_parallel

        return stream_tmx_parallel(
            filepath,
            source_language,
            target_language,
            domain,
            mode,
            options,
        )

    @staticmethod
    async def json(
        filepath: str | Path,
        output: str | Path,
        context: Iterable[LokitJsonContext | str] | None = None,
    ) -> Path:
        from lokit import Lokit

        return await Lokit.to_json_async(filepath, output, context)


class async_:
    @staticmethod
    def file(filepath: str) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_file_async

        return import_file_async(filepath)

    @staticmethod
    def tmx(
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: TmxParseMode = TmxParseMode.FULL,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_tmx_async

        return import_tmx_async(filepath, source_language, target_language, domain, mode)

    @staticmethod
    def xliff(filepath: str) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_xliff_async

        return import_xliff_async(filepath)

    @staticmethod
    def csv(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_csv_async

        return import_csv_async(filepath, source_locale, target_locale)

    @staticmethod
    def xlsx(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_xlsx_async

        return import_xlsx_async(filepath, source_locale, target_locale)

    @staticmethod
    def html(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_html_async

        return import_html_async(filepath, source_locale, target_locale)

    @staticmethod
    def po(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_po_async

        return import_po_async(filepath, source_locale, target_locale)

    @staticmethod
    def json(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_json_i18n_async

        return import_json_i18n_async(filepath, source_locale, target_locale, target_filepath)

    @staticmethod
    def json_i18n(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        target_filepath: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        return async_.json(filepath, source_locale, target_locale, target_filepath)

    @staticmethod
    def idml(
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> AsyncIterator[ExtractItem]:
        from lokit.importers import import_idml_async

        return import_idml_async(filepath, source_locale, target_locale)


@dataclass(frozen=True)
class ExtractorNamespace:
    csv: type[CsvExtractor] = CsvExtractor
    html: type[HtmlExtractor] = HtmlExtractor
    idml: type[IdmlExtractor] = IdmlExtractor
    json: type[JsonI18nExtractor] = JsonI18nExtractor
    json_i18n: type[JsonI18nExtractor] = JsonI18nExtractor
    po: type[PoExtractor] = PoExtractor
    tmx: type[TmxExtractor] = TmxExtractor
    xliff: type[XliffExtractor] = XliffExtractor
    xlsx: type[XlsxExtractor] = XlsxExtractor


extractors = ExtractorNamespace()
parse = read

__all__ = [
    "CsvExtractor",
    "ExtractItem",
    "ExtractorNamespace",
    "HtmlExtractor",
    "IdmlExtractor",
    "JsonI18nExtractor",
    "PoExtractor",
    "TmxExtractor",
    "TmxParallelOptions",
    "TmxParseMode",
    "XliffExtractor",
    "XlsxExtractor",
    "async_",
    "extractors",
    "parse",
    "read",
    "stream",
]
