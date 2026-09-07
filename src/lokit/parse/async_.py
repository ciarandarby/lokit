from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, Data
from lokit.parsers.po.extraction import PoImportMode
from lokit.parsers.tmx.models import TmxParseMode
from lokit.types import DEFAULT_DICT_FIELDS, DictField, StringMode, TagSyntax, TranslationRow, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from os import PathLike
    from pathlib import Path

    from lokit.office.models import DocumentSource
    from lokit.office.options import OfficeImportOptions
    from lokit.parsers.async_bridge import AsyncExtractionBridge
    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]

__all__ = [
    "csv",
    "document",
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
    "to_dict",
    "xliff",
    "xlsx",
]


async def document(
    filepath: str | PathLike[str],
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> BaseStructure:
    from lokit.importers import import_file
    from lokit.io.atomic import run_cancellable_export

    return await run_cancellable_export(
        lambda _cancellation: import_file(
            filepath,
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    )


async def to_dict(
    filepath: str | Path,
    source_language: str = "",
    target_language: str = "",
    domain: str = "",
    *,
    fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
    strings: StringMode | str = StringMode.SANITIZED,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> list[TranslationRow]:
    """Materialize rows from the bounded asynchronous projection stream."""
    from lokit.stream.async_ import to_dict as stream_to_dict

    return [
        row
        async for row in stream_to_dict(
            filepath,
            source_language,
            target_language,
            domain,
            fields=fields,
            strings=strings,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )
    ]


def file(
    filepath: str | PathLike[str],
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously reads and parses any supported file."""
    from lokit.importers import import_file_async

    return import_file_async(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def lokit(
    filepath: str | PathLike[str],
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams units from a Lokit interchange file."""
    from lokit.importers import import_lokit_async

    return import_lokit_async(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def lokit_json(
    filepath: str | PathLike[str],
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parse units from the legacy Lokit JSON representation."""
    from lokit.importers import import_lokit_json_async

    return import_lokit_json_async(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def tmx(
    filepath: str | PathLike[str],
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a TMX file."""
    from lokit.importers import import_tmx_async

    return import_tmx_async(
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


def xliff(
    filepath: str | PathLike[str],
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from an XLIFF file."""
    from lokit.importers import import_xliff_async

    return import_xliff_async(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


def csv(
    filepath: str | PathLike[str],
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a CSV file."""
    from lokit.importers import import_csv_async

    return import_csv_async(
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
    filepath: str | PathLike[str],
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from an Excel sheet."""
    from lokit.importers import import_xlsx_async

    return import_xlsx_async(
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
    filepath: str | PathLike[str],
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from an HTML document."""
    from lokit.importers import import_html_async

    return import_html_async(
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
    filepath: str | PathLike[str],
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a Gettext PO file."""
    from lokit.importers import import_po_async

    return import_po_async(
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


def json_i18n(
    filepath: str | PathLike[str],
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a JSON localization file."""
    from lokit.importers import import_json_i18n_async

    return import_json_i18n_async(
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
    filepath: str | PathLike[str],
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from an IDML package."""
    from lokit.importers import import_idml_async

    return import_idml_async(
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


def docx(
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
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a Word document."""
    from lokit.importers import import_docx_async

    return import_docx_async(
        filepath,
        source_locale,
        target_locale,
        options=options,
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
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously parses and streams translation units from a PowerPoint document."""
    from lokit.importers import import_pptx_async

    return import_pptx_async(
        filepath,
        source_locale,
        target_locale,
        options=options,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )
