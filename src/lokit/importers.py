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
from lokit.parsers.interchange import (
    attach_native_items,
    convert_native_path,
    iter_native_po_records,
    open_native_po_reader,
    try_native_materialize,
    try_native_po_materialize,
)
from lokit.parsers.json_i18n.extraction import JsonI18nExtractor
from lokit.parsers.lokit.extraction import LokitExtractor
from lokit.parsers.po.extraction import PoExtractor, PoImportMode, normalize_po_import_mode
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.parallel import TmxParallelOptions, extract_tmx_parallel
from lokit.parsers.tmx.xml_utils import local_name
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit.parsers.xlsx.extraction import XlsxExtractor
from lokit.tabular import build_import_options
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence

    from lokit.office.models import DocumentSource
    from lokit.office.options import OfficeImportOptions
    from lokit.parsers.interchange import NativePoReader
    from lokit.placeholders import PlaceholderSyntax

TmxBatch = list[tuple[str, Data]]
_NATIVE_PO_BATCH_SIZE = 2048


def import_lokit(
    filepath: str,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    extractor = LokitExtractor(filepath)
    parsed_data = _collect_items(
        project_items(
            extractor.extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=_native_syntax_for_extensions(extractor.extensions),
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing Lokit",
        progress,
    )
    return _build_lokit_structure(extractor, parsed_data)


def import_lokit_async(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[tuple[str, Data]]:
    # Reader construction parses the document header. Keep that work in the
    # bridge's worker instead of running it on the caller's event-loop thread.
    return AsyncExtractionBridge(
        lambda: _project_lokit_path(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )


def stream_lokit(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    extractor = LokitExtractor(filepath)
    return StreamingStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        items=project_items(
            extractor.extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=_native_syntax_for_extensions(extractor.extensions),
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        target_locales=extractor.target_locales,
        format_version=extractor.format_version,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        extensions=extractor.extensions.copy(),
    )


def stream_lokit_json(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Stream the legacy BaseStructure JSON representation."""
    from lokit.io.legacy_json_stream import stream_lokit_json as _stream_lokit_json

    document = _stream_lokit_json(filepath)
    document.items = project_items(
        iter(document.items),
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        native_syntax=_native_syntax_for_extensions(document.extensions),
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
    return document


def import_lokit_json(
    filepath: str,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    """Materialize legacy Lokit JSON without first loading a duplicate JSON tree."""
    document = stream_lokit_json(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
    parsed_data = _collect_items(iter(document.items), "Parsing Lokit JSON", progress)
    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=document.target_locale,
        data=parsed_data,
        target_locales=document.target_locales,
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=document.target_language,
        target_languages=document.target_languages,
        extensions=document.extensions.copy(),
    )


def import_lokit_json_async(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[tuple[str, Data]]:
    """Stream legacy Lokit JSON without blocking the caller's event loop."""
    return AsyncExtractionBridge(
        lambda: iter(
            stream_lokit_json(
                filepath,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            ).items
        )
    )


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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    _validate_xml_root(filepath, "tmx")
    if not progress:
        native_document = try_native_materialize(
            filepath,
            "tmx",
            source_language=source_language,
            target_language=target_language,
            domain=domain,
            mode=mode.value,
        )
        if native_document is not None:
            return _project_materialized(
                native_document,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                native_syntax=TagSyntax.TMX_14,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
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
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
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
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
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
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_tmx_batches_async(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    *,
    batch_size: int = 1000,
    mode: TmxParseMode = TmxParseMode.FULL,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        lambda: _iter_batches(
            extractor.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            ),
            batch_size,
        ),
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
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> None:
    async for batch in import_tmx_batches_async(
        filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        batch_size=batch_size,
        mode=mode,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    ):
        await callback(batch)


def import_xliff(
    filepath: str,
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    _validate_xml_root(filepath, "xliff")
    if not progress:
        native_document = try_native_materialize(filepath, "xliff")
        if native_document is not None:
            return _project_materialized(
                native_document,
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                native_syntax=_xliff_native_syntax(native_document.extensions),
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
    extractor = XliffExtractor(filepath)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing XLIFF",
        progress,
        merge_xliff=False,
    )
    if len(extractor.target_locales) > 1:
        parsed_data = _merge_xliff_identities(parsed_data)
    return _build_xliff_structure(extractor, parsed_data)


def import_xliff_async(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    _validate_xml_root(filepath, "xliff")
    extractor = XliffExtractor(filepath)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_file(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    detected = detect_format(filepath)
    if detected == LokitInputFormat.LOKIT:
        return import_lokit(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.LOKIT_JSON:
        return import_lokit_json(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.TMX:
        return import_tmx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLIFF:
        return import_xliff(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.CSV:
        return import_csv(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLSX:
        return import_xlsx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.DOCX:
        return import_docx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PPTX:
        return import_pptx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.HTML:
        return import_html(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PO:
        return import_po(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.JSON_I18N:
        return import_json_i18n(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.IDML:
        return import_idml(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    raise ValueError(f"Unsupported input format: {filepath}")


def import_file_async(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    detected = detect_format(filepath)
    if detected == LokitInputFormat.LOKIT:
        return import_lokit_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.LOKIT_JSON:
        return import_lokit_json_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.TMX:
        return import_tmx_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLIFF:
        return import_xliff_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.CSV:
        return import_csv_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLSX:
        return import_xlsx_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.DOCX:
        return import_docx_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PPTX:
        return import_pptx_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.HTML:
        return import_html_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PO:
        return import_po_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.JSON_I18N:
        return import_json_i18n_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.IDML:
        return import_idml_async(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    raise ValueError(f"Unsupported input format: {filepath}")


def stream_file(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Open any detected input format through its bounded streaming path."""
    detected = detect_format(filepath)
    if detected == LokitInputFormat.LOKIT:
        return stream_lokit(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.LOKIT_JSON:
        return stream_lokit_json(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.TMX:
        return stream_tmx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLIFF:
        return stream_xliff(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.CSV:
        return stream_csv(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.XLSX:
        return stream_xlsx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.DOCX:
        return stream_docx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PPTX:
        return stream_pptx(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.HTML:
        return stream_html(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.PO:
        return stream_po(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.JSON_I18N:
        return stream_json_i18n(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    if detected == LokitInputFormat.IDML:
        return stream_idml(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    raise ValueError(f"Unsupported input format: {filepath}")


def stream_tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
    document = StreamingStructure(
        source_locale=extractor.source_locale or extractor.native_source,
        target_locale=_resolved_target_locale(
            extractor.target_locale,
            extractor.target_locales,
            extractor.native_target,
        ),
        items=extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        extensions=extractor.extensions,
    )
    attach_native_items(
        document,
        source_path=filepath,
        input_format="tmx",
        source_language=source_language,
        target_language=target_language,
        mode=mode.value,
        copy_if_same=(include_tags and tag_syntax is TagSyntax.NATIVE and mode is TmxParseMode.FULL),
        close_source=extractor.close,
    )
    return document


def stream_xliff(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    _validate_xml_root(filepath, "xliff")
    extractor = XliffExtractor(filepath)
    extractor._initialize_from_file()
    document = StreamingStructure(
        source_locale=extractor.source_locale or "",
        target_locale=extractor.target_locale,
        items=extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        target_locales=extractor.target_locales,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        extensions=extractor.extensions,
    )
    attach_native_items(
        document,
        source_path=filepath,
        input_format="xliff",
        source_language=None,
        target_language=None,
        mode="full",
        copy_if_same=include_tags and tag_syntax is TagSyntax.NATIVE,
        close_source=extractor.close,
    )
    return document


def convert_tmx_to_tmx(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, "tmx", export_tmx, source_language, target_language)


def convert_tmx_to_xliff(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, "xliff", export_xliff, source_language, target_language)


def convert_tmx_to_csv(
    source_path: str,
    target_path: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    return _convert_tmx(source_path, target_path, None, export_csv, source_language, target_language)


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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing CSV",
        progress,
    )
    return _build_csv_structure(extractor, parsed_data)


def stream_csv(
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
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
    items = _prime_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )
    return _streaming_structure(_build_csv_structure(extractor, {}), items)


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
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        targets[locale] = _project_data_dict(
            targets[locale],
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing XLSX",
        progress,
    )
    return _build_xlsx_structure(extractor, parsed_data)


def stream_xlsx(
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
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
    items = _prime_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )
    return _streaming_structure(_build_xlsx_structure(extractor, {}), items)


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
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        targets[locale] = _project_data_dict(
            targets[locale],
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing HTML",
        progress,
    )
    return _build_html_structure(extractor, parsed_data)


def stream_html(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    items = _prime_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )
    return _streaming_structure(_build_html_structure(extractor, {}), items)


def import_html_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = HtmlExtractor(filepath, source_locale, target_locale)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    mode: PoImportMode | str = PoImportMode.AUTO,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    normalized_mode = normalize_po_import_mode(mode)
    if not progress:
        return _project_materialized(
            try_native_po_materialize(
                filepath,
                source_locale=source_locale,
                target_locale=target_locale,
                mode=normalized_mode.value,
            ),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    return _materialize_native_po_with_progress(
        open_native_po_reader(
            filepath,
            source_locale or None,
            target_locale,
            normalized_mode.value,
        ),
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_po_targets(
    source_filepath: str,
    target_filepaths: Mapping[str, str],
    source_locale: str = "",
    *,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    document = import_po(
        source_filepath,
        source_locale=source_locale,
        mode="source",
        progress=progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
    target_locales: list[str] = []
    for locale, filepath in target_filepaths.items():
        target = import_po(
            filepath,
            source_locale=document.source_locale,
            target_locale=locale,
            mode="gettext",
            progress=progress,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
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


def _materialize_native_po_with_progress(
    reader: NativePoReader,
    *,
    include_tags: bool,
    tag_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> BaseStructure:
    parsed_data: dict[str, Data] = {}
    with tqdm(desc="Parsing PO", unit="units") as progress_bar:
        try:
            while True:
                batch = reader.read_batch(_NATIVE_PO_BATCH_SIZE)
                if not batch:
                    break
                parsed_data.update(
                    project_items(
                        iter(batch),
                        include_tags=include_tags,
                        tag_syntax=tag_syntax,
                        native_syntax=TagSyntax.HTML,
                        unsupported_tags=unsupported_tags,
                        runtime_placeholders=runtime_placeholders,
                        inline_placeholders=inline_placeholders,
                        placeholder_syntaxes=placeholder_syntaxes,
                    )
                )
                progress_bar.update(len(batch))
        finally:
            reader.close()
    return BaseStructure(
        source_locale=reader.source_locale,
        target_locale=reader.target_locale,
        data=parsed_data,
        target_locales=tuple(reader.target_locales),
        export_origin=reader.export_origin,
        export_timestamp=reader.export_timestamp,
        source_language=reader.source_language,
        target_language=reader.target_language,
        target_languages=tuple(reader.target_languages),
        extensions=reader.extensions,
    )


def import_po_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    mode: PoImportMode | str = PoImportMode.AUTO,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    normalized_mode = normalize_po_import_mode(mode)
    return AsyncExtractionBridge(
        lambda: project_items(
            iter_native_po_records(
                open_native_po_reader(
                    filepath,
                    source_locale or None,
                    target_locale,
                    normalized_mode.value,
                )
            ),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )


def stream_po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    mode: PoImportMode | str = PoImportMode.AUTO,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    normalized_mode = normalize_po_import_mode(mode)
    reader = open_native_po_reader(
        filepath,
        source_locale or None,
        target_locale,
        normalized_mode.value,
    )
    native_items = iter_native_po_records(reader)
    items = project_items(
        native_items,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        native_syntax=TagSyntax.HTML,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
    document = StreamingStructure(
        source_locale=reader.source_locale,
        target_locale=reader.target_locale,
        items=items,
        target_locales=tuple(reader.target_locales),
        export_origin=reader.export_origin,
        export_timestamp=reader.export_timestamp,
        source_language=reader.source_language,
        target_language=reader.target_language,
        target_languages=tuple(reader.target_languages),
        extensions=reader.extensions,
    )
    attach_native_items(
        document,
        source_path=filepath,
        input_format="po",
        source_language=source_locale or None,
        target_language=target_locale,
        mode=normalized_mode.value,
        copy_if_same=False,
        close_source=reader.close,
    )
    return document


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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing JSON",
        progress,
    )
    return _build_json_i18n_structure(extractor, parsed_data)


def stream_json_i18n(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
    target_filepaths: Mapping[str, str] | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    extractor = JsonI18nExtractor(
        filepath,
        source_locale,
        target_locale,
        target_filepath,
        target_filepaths,
    )
    items = _prime_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )
    return _streaming_structure(_build_json_i18n_structure(extractor, {}), items)


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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
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
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
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
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    parsed_data = _collect_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        ),
        "Parsing IDML",
        progress,
    )
    return _build_idml_structure(extractor, parsed_data)


def stream_idml(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    items = _prime_items(
        extractor.extract(
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )
    return _streaming_structure(_build_idml_structure(extractor, {}), items)


def import_idml_async(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    extractor = IdmlExtractor(filepath, source_locale, target_locale)
    return extractor.extract_async(
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    from lokit.office import import_docx as _import_docx

    return _import_docx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        progress=progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def stream_docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    from lokit.office import stream_docx as _stream_docx

    return _stream_docx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        progress=progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_docx_async(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    from lokit.office import import_docx_async as _import_docx_async

    return _import_docx_async(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = True,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    from lokit.office import import_pptx as _import_pptx

    return _import_pptx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        progress=progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def stream_pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    progress: bool = False,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    from lokit.office import stream_pptx as _stream_pptx

    return _stream_pptx(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        progress=progress,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def import_pptx_async(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    options: OfficeImportOptions | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncIterator[tuple[str, Data]]:
    from lokit.office import import_pptx_async as _import_pptx_async

    return _import_pptx_async(
        filepath,
        source_locale=source_locale,
        target_locale=target_locale,
        options=options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def _project_materialized(
    document: BaseStructure,
    *,
    include_tags: bool,
    tag_syntax: TagSyntax,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> BaseStructure:
    document.data = _project_data_dict(
        document.data,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        native_syntax=native_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
    return document


def _project_lokit_path(
    filepath: str,
    *,
    include_tags: bool,
    tag_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> Iterator[tuple[str, Data]]:
    extractor = LokitExtractor(filepath)
    return project_items(
        extractor.extract(),
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        native_syntax=_native_syntax_for_extensions(extractor.extensions),
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def _project_data_dict(
    data: dict[str, Data],
    *,
    include_tags: bool,
    tag_syntax: TagSyntax,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> dict[str, Data]:
    return dict(
        project_items(
            iter(data.items()),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=native_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )


def _xliff_native_syntax(extensions: Mapping[str, str]) -> TagSyntax:
    version = extensions.get("xliff_version", "1.2")
    if version.startswith("2.1"):
        return TagSyntax.XLIFF_21
    if version.startswith("2"):
        return TagSyntax.XLIFF_20
    return TagSyntax.XLIFF_12


def _native_syntax_for_extensions(extensions: Mapping[str, str]) -> TagSyntax:
    input_format = extensions.get("input_format", "")
    if input_format == "tmx":
        return TagSyntax.TMX_14
    if input_format == "xliff":
        return _xliff_native_syntax(extensions)
    if input_format == "idml":
        return TagSyntax.IDML
    if input_format == "docx":
        return TagSyntax.DOCX
    if input_format == "pptx":
        return TagSyntax.PPTX
    return TagSyntax.HTML


def _prime_items(items: Iterator[tuple[str, Data]]) -> Iterator[tuple[str, Data]]:
    try:
        first = next(items)
    except StopIteration:
        return iter(())
    return _prepend_item(first, items)


def _prepend_item(
    first: tuple[str, Data],
    items: Iterator[tuple[str, Data]],
) -> Iterator[tuple[str, Data]]:
    yield first
    yield from items


def _streaming_structure(
    metadata: BaseStructure,
    items: Iterator[tuple[str, Data]],
) -> StreamingStructure:
    return StreamingStructure(
        source_locale=metadata.source_locale,
        target_locale=metadata.target_locale,
        items=items,
        target_locales=metadata.target_locales,
        format_version=metadata.format_version,
        export_origin=metadata.export_origin,
        export_timestamp=metadata.export_timestamp,
        source_language=metadata.source_language,
        target_language=metadata.target_language,
        target_languages=metadata.target_languages,
        extensions=metadata.extensions,
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


def _build_lokit_structure(
    extractor: LokitExtractor,
    parsed_data: dict[str, Data],
) -> BaseStructure:
    return BaseStructure(
        source_locale=extractor.source_locale,
        target_locale=extractor.target_locale,
        data=parsed_data,
        target_locales=extractor.target_locales,
        format_version=extractor.format_version,
        export_origin=extractor.export_origin,
        export_timestamp=extractor.export_timestamp,
        source_language=extractor.source_language,
        target_language=extractor.target_language,
        target_languages=extractor.target_languages,
        extensions=extractor.extensions.copy(),
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
    *,
    merge_xliff: bool = True,
) -> dict[str, Data]:
    parsed_data: dict[str, Data] = {}
    xliff_identities: dict[tuple[str, str, str], list[str]] = {}
    iterable: Iterable[tuple[str, Data]] = tqdm(items, desc=desc, unit="units") if progress else items
    for unit_id, data in iterable:
        identity = _xliff_merge_identity(data) if merge_xliff else None
        if identity is not None:
            merged = False
            for existing_id in xliff_identities.get(identity, ()):
                existing = parsed_data[existing_id]
                if _can_merge_xliff_targets(existing, data):
                    _merge_data(existing, data)
                    merged = True
                    break
            if merged:
                continue
            xliff_identities.setdefault(identity, []).append(unit_id)
        existing = parsed_data.setdefault(unit_id, data)
        if existing is not data:
            _merge_data(existing, data)
    return parsed_data


def _merge_xliff_identities(data: dict[str, Data]) -> dict[str, Data]:
    merged_data: dict[str, Data] = {}
    identities: dict[tuple[str, str, str], list[str]] = {}
    for unit_id, unit in data.items():
        identity = _xliff_merge_identity(unit)
        if identity is not None:
            merged = False
            for existing_id in identities.get(identity, ()):
                existing = merged_data[existing_id]
                if _can_merge_xliff_targets(existing, unit):
                    _merge_data(existing, unit)
                    merged = True
                    break
            if merged:
                continue
            identities.setdefault(identity, []).append(unit_id)
        merged_data[unit_id] = unit
    return merged_data


def _xliff_merge_identity(data: Data) -> tuple[str, str, str] | None:
    extensions = data.extensions
    raw_unit_id = extensions.get("unit_id", "")
    if not raw_unit_id or "resource_index" not in extensions:
        return None
    segment_id = extensions.get("segment_id", "")
    logical_id = f"{raw_unit_id}:{segment_id}" if segment_id else raw_unit_id
    return extensions.get("resource", ""), logical_id, data.source


def _can_merge_xliff_targets(existing: Data, incoming: Data) -> bool:
    if existing.target is not None or incoming.target is not None:
        return False
    if not existing.targets and not incoming.targets:
        return False
    return existing.targets.keys().isdisjoint(incoming.targets)


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
    output_format: str | None,
    exporter: Callable[[StreamingStructure, str], None],
    source_language: str | None,
    target_language: str | None,
) -> ConversionStats:
    started = perf_counter()
    native_count = (
        convert_native_path(
            source_path,
            target_path,
            "tmx",
            output_format,
            source_language=source_language,
            target_language=target_language,
        )
        if output_format is not None
        else None
    )
    if native_count is not None:
        output_path = Path(target_path)
        return ConversionStats(
            units_read=native_count,
            units_written=native_count,
            input_bytes=Path(source_path).stat().st_size,
            output_bytes=output_path.stat().st_size,
            seconds=perf_counter() - started,
        )
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
