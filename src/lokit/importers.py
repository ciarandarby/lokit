from __future__ import annotations

from collections.abc import AsyncIterator

from lokit.data.structure import BaseStructure, Data
from lokit.parsers.csv.extraction import CsvExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.po.extraction import PoExtractor
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.idml.extraction import IdmlExtractor
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.xliff.extraction import XliffExtractor


def import_tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
) -> BaseStructure:
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
    )
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_tmx_structure(extractor, parsed_data)


async def import_tmx_async(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
    )
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_xliff(filepath: str) -> BaseStructure:
    extractor = XliffExtractor(filepath)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_xliff_structure(extractor, parsed_data)


async def import_xliff_async(filepath: str) -> AsyncIterator[tuple[str, Data]]:
    extractor = XliffExtractor(filepath)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_csv(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> BaseStructure:
    extractor = CsvExtractor(filepath, source_locale, target_locale)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_csv_structure(extractor, parsed_data)


async def import_csv_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = CsvExtractor(filepath, source_locale, target_locale)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_xlsx(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> BaseStructure:
    extractor = XlsxExtractor(filepath, source_locale, target_locale)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_xlsx_structure(extractor, parsed_data)


async def import_xlsx_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = XlsxExtractor(filepath, source_locale, target_locale)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_html(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> BaseStructure:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_html_structure(extractor, parsed_data)


async def import_html_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> BaseStructure:
    extractor = PoExtractor(filepath, source_locale, target_locale)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_po_structure(extractor, parsed_data)


async def import_po_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = PoExtractor(filepath, source_locale, target_locale)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_json_i18n(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
) -> BaseStructure:
    extractor = JsonI18nExtractor(filepath, source_locale, target_locale, target_filepath)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_json_i18n_structure(extractor, parsed_data)


async def import_json_i18n_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = JsonI18nExtractor(filepath, source_locale, target_locale, target_filepath)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def import_idml(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> BaseStructure:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    parsed_data: dict[str, Data] = {
        unit_id: data for unit_id, data in extractor.extract()
    }
    return _build_idml_structure(extractor, parsed_data)


async def import_idml_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    async for unit_id, data in extractor.extract_async():
        yield unit_id, data


def _build_tmx_structure(
    extractor: TmxExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale or extractor.native_source,
        target_locale=extractor.target_locale or extractor.native_target or None,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_xliff_structure(
    extractor: XliffExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale or "",
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_csv_structure(
    extractor: CsvExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_xlsx_structure(
    extractor: XlsxExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_html_structure(
    extractor: HtmlExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_po_structure(
    extractor: PoExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        extensions=extractor.extensions,
    )


def _build_json_i18n_structure(
    extractor: JsonI18nExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        extensions=extractor.extensions,
    )


def _build_idml_structure(
    extractor: IdmlExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )
