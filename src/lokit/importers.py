from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from tqdm import tqdm

from lokit.data.structure import BaseStructure, ConversionStats, Data, StreamingStructure, TargetData
from lokit.data.targets import split_targets
from lokit.exporters import export_csv, export_tmx, export_xliff, export_xliff_targets
from lokit.format_detection import LokitInputFormat, detect_format
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.csv.extraction import CsvExtractor
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.idml.extraction import IdmlExtractor
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.po.extraction import PoExtractor, PoImportMode
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions, extract_tmx_parallel
from lokit.parsers.tmx.xml_utils import local_name
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor
from lokit.tabular import build_import_options
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping

    from lokit.office.models import DocumentSource

TmxBatch = list[tuple[str, Data]]


def import_tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing TMX",
        progress,
    )
    return _build_tmx_structure(extractor, parsed_data)


def import_tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    extractor._initialize_from_file()
    parsed_data = _collect_items(
        extract_tmx_parallel(
            filepath=filepath,
            source_language=extractor.native_source,
            target_language=extractor.native_target,
            domain=domain,
            mode=mode,
            options=options,
            selected_target=target_language is not None,
        ),
        "Parsing TMX",
        progress,
    )
    return _build_tmx_structure(extractor, parsed_data)


def stream_tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
) -> StreamingStructure:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    extractor._initialize_from_file()
    return StreamingStructure(
        source_locale=extractor.source_locale or extractor.native_source,
        target_locale=_resolved_target_locale(
            extractor.target_locale,
            extractor.target_locales,
            extractor.native_target,
        ),
        items=extract_tmx_parallel(
            filepath=filepath,
            source_language=extractor.native_source,
            target_language=extractor.native_target,
            domain=domain,
            mode=mode,
            options=options,
            selected_target=target_language is not None,
        ),
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        extensions=extractor.extensions,
    )


def import_tmx_async(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_tmx_batches_async(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    *,
    batch_size: int = 1000,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> AsyncIterator[TmxBatch]:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    return AsyncExtractionBridge(
        lambda: _iter_batches(extractor.extract(), batch_size),
        batch_size=1,
    )


def _iter_batches(
    items: Iterator[tuple[str, Data]],
    batch_size: int,
) -> Iterator[TmxBatch]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    batch: TmxBatch = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


async def process_tmx_async(
    filepath: str,
    callback: Callable[[TmxBatch], Awaitable[None]],
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    *,
    batch_size: int = 1000,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> None:
    async for batch in import_tmx_batches_async(
        filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        batch_size=batch_size,
        mode=mode,
    ):
        await callback(batch)


def import_xliff(
    filepath: str,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    _validate_xml_root(filepath, "xliff")
    extractor = XliffExtractor(filepath)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing XLIFF",
        progress,
    )
    return _build_xliff_structure(extractor, parsed_data)


def import_xliff_async(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    _validate_xml_root(filepath, "xliff")
    extractor = XliffExtractor(filepath)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_file(filepath: str) -> BaseStructure:
    detected = detect_format(filepath)
    if detected == LokitInputFormat.TMX:
        return import_tmx(filepath)
    if detected == LokitInputFormat.XLIFF:
        return import_xliff(filepath)
    if detected == LokitInputFormat.CSV:
        return import_csv(filepath)
    if detected == LokitInputFormat.XLSX:
        return import_xlsx(filepath)
    if detected == LokitInputFormat.DOCX:
        return import_docx(filepath)
    if detected == LokitInputFormat.PPTX:
        return import_pptx(filepath)
    if detected == LokitInputFormat.HTML:
        return import_html(filepath)
    if detected == LokitInputFormat.PO:
        return import_po(filepath)
    if detected == LokitInputFormat.JSON_I18N:
        return import_json_i18n(filepath)
    if detected == LokitInputFormat.IDML:
        return import_idml(filepath)
    from lokit.io import load_lokit_json

    return load_lokit_json(Path(filepath))


def import_file_async(filepath: str) -> AsyncIterator[tuple[str, Data]]:
    detected = detect_format(filepath)
    if detected == LokitInputFormat.TMX:
        return import_tmx_async(filepath)
    if detected == LokitInputFormat.XLIFF:
        return import_xliff_async(filepath)
    if detected == LokitInputFormat.CSV:
        return import_csv_async(filepath)
    if detected == LokitInputFormat.XLSX:
        return import_xlsx_async(filepath)
    if detected == LokitInputFormat.DOCX:
        return import_docx_async(filepath)
    if detected == LokitInputFormat.PPTX:
        return import_pptx_async(filepath)
    if detected == LokitInputFormat.HTML:
        return import_html_async(filepath)
    if detected == LokitInputFormat.PO:
        return import_po_async(filepath)
    if detected == LokitInputFormat.JSON_I18N:
        return import_json_i18n_async(filepath)
    if detected == LokitInputFormat.IDML:
        return import_idml_async(filepath)
    return AsyncExtractionBridge(lambda: iter(import_file(filepath).data.items()))


def stream_tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> StreamingStructure:
    _validate_xml_root(filepath, "tmx")
    extractor = TmxExtractor(
        filepath=filepath,
        source_language=source_language,
        target_language=target_language,
        parse_header=not (source_language and target_language),
        mode=mode,
    )
    extractor._initialize_from_file()
    return StreamingStructure(
        source_locale=extractor.source_locale or extractor.native_source,
        target_locale=_resolved_target_locale(
            extractor.target_locale,
            extractor.target_locales,
            extractor.native_target,
        ),
        items=extractor.extract(),
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        extensions=extractor.extensions,
    )


def stream_xliff(filepath: str) -> StreamingStructure:
    _validate_xml_root(filepath, "xliff")
    extractor = XliffExtractor(filepath)
    extractor._initialize_from_file()
    return StreamingStructure(
        source_locale=extractor.source_locale or "",
        target_locale=extractor.target_locale,
        items=extractor.extract(),
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def convert_tmx_to_tmx(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, export_tmx, source_language, target_language)


def convert_tmx_to_xliff(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, export_xliff, source_language, target_language)


def convert_tmx_to_csv(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, export_csv, source_language, target_language)


def convert_csv_to_xliff(
    source_path: str,
    target_path: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    progress: bool = True,
) -> None:
    document = import_csv(
        source_path,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    if document.target_locale is None and document.target_locales:
        export_xliff_targets(split_targets(document), target_path)
        return
    export_xliff(document, target_path)


def convert_xlsx_to_xliff(
    source_path: str,
    target_path: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    progress: bool = True,
) -> None:
    document = import_xlsx(
        source_path,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    if document.target_locale is None and document.target_locales:
        export_xliff_targets(split_targets(document), target_path)
        return
    export_xliff(document, target_path)


def import_csv(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = CsvExtractor(filepath, source_locale, target_locale, options)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing CSV",
        progress,
    )
    return _build_csv_structure(extractor, parsed_data)


def import_csv_targets(
    filepath: str,
    source_locale: str = "",
    *,
    progress: bool = True,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
) -> dict[str, BaseStructure]:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = CsvExtractor(filepath, source_locale, options=options)
    targets = _collect_target_rows(extractor.extract_target_rows(), "Parsing CSV", progress)
    for locale in extractor.target_locales:
        targets.setdefault(locale, {})
    return {
        locale: _build_csv_structure_for_target(extractor, locale, targets[locale])
        for locale in extractor.target_locales
    }


def import_csv_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = CsvExtractor(filepath, source_locale, target_locale, options)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_xlsx(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = XlsxExtractor(filepath, source_locale, target_locale, options)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing XLSX",
        progress,
    )
    return _build_xlsx_structure(extractor, parsed_data)


def import_xlsx_targets(
    filepath: str,
    source_locale: str = "",
    *,
    progress: bool = True,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
) -> dict[str, BaseStructure]:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = XlsxExtractor(filepath, source_locale, options=options)
    targets = _collect_target_rows(extractor.extract_target_rows(), "Parsing XLSX", progress)
    for locale in extractor.target_locales:
        targets.setdefault(locale, {})
    return {
        locale: _build_xlsx_structure_for_target(extractor, locale, targets[locale])
        for locale in extractor.target_locales
    }


def import_xlsx_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: dict[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    options = build_import_options(
        header_mode=header_mode,
        include_header_as_data=include_header_as_data,
        source_column=source_column,
        target_column=target_column,
        target_columns=target_columns,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )
    extractor = XlsxExtractor(filepath, source_locale, target_locale, options)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_html(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing HTML",
        progress,
    )
    return _build_html_structure(extractor, parsed_data)


def import_html_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    mode: str = "gettext",
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    extractor = PoExtractor(filepath, source_locale, target_locale, PoImportMode(mode))
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing PO",
        progress,
    )
    return _build_po_structure(extractor, parsed_data)


def import_po_targets(
    source_filepath: str,
    target_filepaths: Mapping[str, str],
    source_locale: str = "",
    *,
    progress: bool = True,
) -> BaseStructure:
    document = import_po(
        source_filepath,
        source_locale=source_locale,
        mode="source",
        progress=progress,
    )
    target_locales: list[str] = []
    for locale, filepath in target_filepaths.items():
        target = import_po(
            filepath,
            source_locale=document.source_locale,
            target_locale=locale,
            mode="gettext",
            progress=progress,
        )
        target_locales.append(locale)
        for unit_id, target_unit in target.data.items():
            if unit_id not in document.data:
                document.data[unit_id] = Data(source=target_unit.source)
            document.data[unit_id].targets[locale] = target_unit_as_target(target_unit)
    document.target_locale = None
    document.target_locales = tuple(target_locales)
    document.target_language = None
    document.target_languages = tuple(locale.replace("_", "-").split("-")[0].lower() for locale in target_locales)
    return document


def import_po_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    mode: str = "gettext",
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = PoExtractor(filepath, source_locale, target_locale, PoImportMode(mode))
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_json_i18n(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
    target_filepaths: Mapping[str, str] | None = None,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    extractor = JsonI18nExtractor(
        filepath,
        source_locale,
        target_locale,
        target_filepath,
        target_filepaths,
    )
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing JSON",
        progress,
    )
    return _build_json_i18n_structure(extractor, parsed_data)


def import_json_i18n_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
    target_filepaths: Mapping[str, str] | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = JsonI18nExtractor(
        filepath,
        source_locale,
        target_locale,
        target_filepath,
        target_filepaths,
    )
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_idml(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> BaseStructure:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
        ),
        "Parsing IDML",
        progress,
    )
    return _build_idml_structure(extractor, parsed_data)


def import_idml_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def import_docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    from lokit.office import import_docx as _import_docx

    return _import_docx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
    )


def stream_docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = False,
) -> StreamingStructure:
    from lokit.office import stream_docx as _stream_docx

    return _stream_docx(filepath, source_locale=source_locale, target_locale=target_locale, progress=progress)


def import_docx_async(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    from lokit.office import import_docx_async as _import_docx_async

    return _import_docx_async(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
    )


def import_pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    from lokit.office import import_pptx as _import_pptx

    return _import_pptx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
    )


def stream_pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = False,
) -> StreamingStructure:
    from lokit.office import stream_pptx as _stream_pptx

    return _stream_pptx(filepath, source_locale=source_locale, target_locale=target_locale, progress=progress)


def import_pptx_async(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    from lokit.office import import_pptx_async as _import_pptx_async

    return _import_pptx_async(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
    )


def _build_tmx_structure(
    extractor: TmxExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale or extractor.native_source,
        target_locale=_resolved_target_locale(
            extractor.target_locale,
            extractor.target_locales,
            extractor.native_target,
        ),
        data=parsed_data,
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _resolved_target_locale(
    target_locale: str | None,
    target_locales: tuple[str, ...],
    native_target: str,
) -> str | None:
    if len(target_locales) > 1:
        return None
    if target_locale is not None:
        return target_locale
    if len(target_locales) == 1:
        return target_locales[0]
    return native_target or None


def _build_xliff_structure(
    extractor: XliffExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    document = BaseStructure(
        source_locale=extractor.source_locale or "",
        target_locale=extractor.target_locale,
        data=parsed_data,
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )
    if len(document.target_locales) == 1 and document.target_locale is not None:
        _collapse_single_target(document.data, document.target_locale)
    return document


def _collapse_single_target(data: dict[str, Data], locale: str) -> None:
    for unit in data.values():
        selected = unit.targets.get(locale)
        unit.targets = {}
        if selected is None:
            continue
        unit.target = selected.text
        unit.status = selected.status
        if selected.plural is not None:
            unit.plural = selected.plural
        unit.meta = selected.meta
        if selected.comments:
            unit.comments = list(selected.comments)
        if selected.extensions:
            unit.extensions.update(selected.extensions)
        tags = unit.tags
        if tags is None:
            continue
        selected_tags = selected.tags
        if selected_tags is None:
            tags.target_tag_map = {}
            tags.target_parts = []
        else:
            tags.target_tag_map = selected_tags.tag_map
            tags.target_parts = selected_tags.parts


def _build_csv_structure(
    extractor: CsvExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
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
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )


def _build_csv_structure_for_target(
    extractor: CsvExtractor,
    target_locale: str,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    target_language = target_locale.replace("_", "-").split("-")[0].lower()
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=target_locale,
        data=parsed_data,
        target_locales=(target_locale,),
        source_language=extractor.source_language,
        target_language=target_language,
        target_languages=(target_language,),
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions.copy(),
    )


def _build_xlsx_structure_for_target(
    extractor: XlsxExtractor,
    target_locale: str,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    target_language = target_locale.replace("_", "-").split("-")[0].lower()
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=target_locale,
        data=parsed_data,
        target_locales=(target_locale,),
        source_language=extractor.source_language,
        target_language=target_language,
        target_languages=(target_language,),
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions.copy(),
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


def target_unit_as_target(unit: Data) -> TargetData:
    return TargetData(
        text=unit.target,
        status=unit.status,
        plural=unit.plural,
        meta=unit.meta,
        comments=list(unit.comments),
        extensions=unit.extensions.copy(),
    )


def _build_json_i18n_structure(
    extractor: JsonI18nExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
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


def _validate_xml_root(filepath: str, expected: str) -> None:
    with open(filepath, "rb") as f:
        data = f.read(4096)
    root = _peek_xml_root(data)
    if root != expected:
        found = root or "unknown"
        raise ValueError(f"Expected {expected.upper()} XML root in {filepath!r}, found {found!r}")


def _peek_xml_root(data: bytes) -> str:
    index = 0
    data_len = len(data)
    while index < data_len:
        start = data.find(b"<", index)
        if start < 0 or start + 1 >= data_len:
            return ""
        marker = data[start + 1 : start + 2]
        if marker in (b"?", b"!"):
            end = data.find(b">", start + 1)
            if end < 0:
                return ""
            index = end + 1
            continue
        end = start + 1
        while end < data_len and data[end] not in b" />\t\r\n":
            end += 1
        raw = data[start + 1 : end].decode("utf-8", errors="ignore")
        if ":" in raw:
            raw = raw.rsplit(":", 1)[-1]
        return local_name(raw).lower()
    return ""


def _collect_items(
    items: Iterable[tuple[str, Data]],
    desc: str,
    progress: bool,
) -> dict[str, Data]:
    parsed_data: dict[str, Data] = {}
    if not progress:
        for unit_id, data in items:
            existing = parsed_data.setdefault(unit_id, data)
            if existing is not data:
                _merge_data(existing, data)
        return parsed_data

    for unit_id, data in tqdm(items, desc=desc, unit="units"):
        existing = parsed_data.setdefault(unit_id, data)
        if existing is not data:
            _merge_data(existing, data)
    return parsed_data


def _collect_target_rows(
    rows: Iterable[dict[str, tuple[str, Data]]],
    desc: str,
    progress: bool,
) -> dict[str, dict[str, Data]]:
    targets: dict[str, dict[str, Data]] = {}
    iterable = tqdm(rows, desc=desc, unit="units") if progress else rows
    for row in iterable:
        for locale, item in row.items():
            unit_id, data = item
            locale_data = targets.setdefault(locale, {})
            existing = locale_data.get(unit_id)
            if existing is None:
                locale_data[unit_id] = data
            else:
                _merge_data(existing, data)
    return targets


def _merge_data(existing: Data, incoming: Data) -> None:
    if not existing.source and incoming.source:
        existing.source = incoming.source
    existing.targets.update(incoming.targets)
    if existing.target is None and incoming.target is not None:
        existing.target = incoming.target
    if existing.tags is None and incoming.tags is not None:
        existing.tags = incoming.tags
    if not existing.comments and incoming.comments:
        existing.comments = incoming.comments
    existing.extensions.update(incoming.extensions)


def _convert_tmx(
    source_path: str,
    target_path: str,
    exporter: Callable[[StreamingStructure, str], None],
    source_language: str | None,
    target_language: str | None,
) -> ConversionStats:
    started = perf_counter()
    document = stream_tmx(source_path, source_language, target_language)
    counter = _CountingItems(document.items)
    document.items = counter
    exporter(document, target_path)
    output_path = Path(target_path)
    return ConversionStats(
        units_read=counter.count,
        units_written=counter.count,
        input_bytes=Path(source_path).stat().st_size,
        output_bytes=output_path.stat().st_size if output_path.exists() else 0,
        seconds=perf_counter() - started,
    )


class _CountingItems:
    def __init__(self, items: Iterable[tuple[str, Data]]) -> None:
        self._items = items
        self.count = 0

    def __iter__(self) -> Iterator[tuple[str, Data]]:
        for item in self._items:
            self.count += 1
            yield item
