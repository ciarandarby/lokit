from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import Data
from lokit.parsers.tmx.models import TmxParseMode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from lokit.office.models import DocumentSource

ExtractItem = tuple[str, Data]

__all__ = ["csv", "docx", "file", "html", "idml", "json_i18n", "po", "pptx", "tmx", "xliff", "xlsx"]


def file(filepath: str) -> AsyncIterator[ExtractItem]:
    """Asynchronously reads and parses any supported file."""
    from lokit.importers import import_file_async

    return import_file_async(filepath)


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from a TMX file."""
    from lokit.importers import import_tmx_async

    return import_tmx_async(filepath, source_language, target_language, domain, mode)


def xliff(filepath: str) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from an XLIFF file."""
    from lokit.importers import import_xliff_async

    return import_xliff_async(filepath)


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
) -> AsyncIterator[ExtractItem]:
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
) -> AsyncIterator[ExtractItem]:
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
    )


def html(filepath: str, source_locale: str = "", target_locale: str | None = None) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from an HTML document."""
    from lokit.importers import import_html_async

    return import_html_async(filepath, source_locale, target_locale)


def po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    mode: str = "gettext",
) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from a Gettext PO file."""
    from lokit.importers import import_po_async

    return import_po_async(filepath, source_locale, target_locale, mode)


def json_i18n(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
    target_filepaths: Mapping[str, str] | None = None,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from a JSON localization file."""
    from lokit.importers import import_json_i18n_async

    return import_json_i18n_async(filepath, source_locale, target_locale, target_filepath, target_filepaths)


def idml(filepath: str, source_locale: str = "", target_locale: str | None = None) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from an IDML package."""
    from lokit.importers import import_idml_async

    return import_idml_async(filepath, source_locale, target_locale)


def docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from a Word document."""
    from lokit.importers import import_docx_async

    return import_docx_async(filepath, source_locale, target_locale)


def pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously parses and streams translation units from a PowerPoint document."""
    from lokit.importers import import_pptx_async

    return import_pptx_async(filepath, source_locale, target_locale)
