from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from lokit.io.stream_json import LokitJsonContext
from lokit.parsers.po.extraction import PoImportMode
from lokit.parsers.tmx.models import TmxParseMode
from lokit.stream import async_ as async_
from lokit.types import DEFAULT_DICT_FIELDS, DictField, StringMode, TagSyntax, TranslationRow, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from lokit.data.structure import StreamingStructure
    from lokit.office.models import DocumentSource
    from lokit.office.options import OfficeImportOptions
    from lokit.parsers.tmx.parallel import TmxParallelOptions
    from lokit.placeholders import PlaceholderSyntax

__all__ = [
    "LokitJsonContext",
    "async_",
    "csv",
    "docx",
    "file",
    "html",
    "idml",
    "json_i18n",
    "lokit",
    "lokit_json",
    "po",
    "pptx",
    "tmx",
    "tmx_parallel",
    "to_dict",
    "write_jsonl",
    "xliff",
    "xlsx",
]


def lokit(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Streams a Lokit interchange document as a StreamingStructure."""
    from lokit.importers import stream_lokit

    return stream_lokit(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def lokit_json(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Stream the legacy Lokit JSON document representation."""
    from lokit.importers import stream_lokit_json

    return stream_lokit_json(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def file(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Stream any supported file selected by bounded format detection."""
    from lokit.importers import stream_file

    return stream_file(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def tmx(
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
    """Streams a TMX document as a StreamingStructure."""
    from lokit.importers import stream_tmx

    return stream_tmx(
        filepath,
        source_language,
        target_language,
        domain,
        mode,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def tmx_parallel(
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
    """Streams a TMX document in parallel as a StreamingStructure."""
    from lokit.importers import stream_tmx_parallel

    return stream_tmx_parallel(
        filepath,
        source_language,
        target_language,
        domain,
        mode,
        options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def xliff(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> StreamingStructure:
    """Streams an XLIFF document as a StreamingStructure."""
    from lokit.importers import stream_xliff

    return stream_xliff(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def csv(
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
    """Streams translation units from a CSV file."""
    from lokit.importers import stream_csv

    return stream_csv(
        filepath,
        source_locale,
        target_locale,
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
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def xlsx(
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
    """Streams translation units from an XLSX workbook."""
    from lokit.importers import stream_xlsx

    return stream_xlsx(
        filepath,
        source_locale,
        target_locale,
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
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def html(
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
    """Streams translation units from an HTML document."""
    from lokit.importers import stream_html

    return stream_html(
        filepath,
        source_locale,
        target_locale,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def json_i18n(
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
    """Streams translation units from a JSON localization file."""
    from lokit.importers import stream_json_i18n

    return stream_json_i18n(
        filepath,
        source_locale,
        target_locale,
        target_filepath,
        target_filepaths,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def idml(
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
    """Streams translation units from an IDML package."""
    from lokit.importers import stream_idml

    return stream_idml(
        filepath,
        source_locale,
        target_locale,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def po(
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
    """Streams a Gettext PO document as a StreamingStructure."""
    from lokit.importers import stream_po

    return stream_po(
        filepath,
        source_locale,
        target_locale,
        mode=mode,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def docx(
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
    """Streams a Word DOCX file as a StreamingStructure."""
    from lokit.importers import stream_docx

    return stream_docx(
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


def pptx(
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
    """Streams a PowerPoint PPTX file as a StreamingStructure."""
    from lokit.importers import stream_pptx

    return stream_pptx(
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


def write_jsonl(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> Path:
    """Streams a document directly to a newline-delimited JSON file."""
    return asyncio.run(
        async_.write_jsonl(
            filepath,
            output,
            context,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )


def to_dict(
    filepath: str | Path,
    source_language: str = "",
    target_language: str = "",
    domain: str = "",
    *,
    fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
    strings: StringMode | str = StringMode.SANITIZED,
) -> Iterator[TranslationRow]:
    """Lazily project an interchange file into flat, string-only rows."""
    from lokit.data.dict_projection import iter_file_rows, normalize_fields, normalize_string_mode

    return iter_file_rows(
        filepath,
        source_language,
        target_language,
        domain,
        normalize_fields(fields),
        normalize_string_mode(strings),
    )
