from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.parse import async_ as async_
from lokit.parse import write as write
from lokit.parsers.tmx.models import TmxParseMode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lokit.data.structure import BaseStructure
    from lokit.office.models import DocumentSource
    from lokit.parsers.tmx.parallel import TmxParallelOptions

__all__ = [
    "async_",
    "csv",
    "csv_targets",
    "docx",
    "file",
    "html",
    "idml",
    "json_i18n",
    "po",
    "po_targets",
    "pptx",
    "tmx",
    "tmx_parallel",
    "write",
    "xliff",
    "xlsx",
    "xlsx_targets",
]


def file(filepath: str) -> BaseStructure:
    """Imports and parses any supported file with string filepath intake"""
    from lokit.importers import import_file

    return import_file(filepath)


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    progress: bool = True,
) -> BaseStructure:
    """
    Parses a TMX (translation memory eXchange) file type.\n
    Source and target languages can be auto-detected but reccomended to add in the specific language codes
    """
    from lokit.importers import import_tmx

    return import_tmx(filepath, source_language, target_language, domain, mode, progress=progress)


def tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """
    Parses a TMX (translation memory eXchange) file type using concurrent workers in parallel.
    Reccomended for large files.
    """
    from lokit.importers import import_tmx_parallel

    return import_tmx_parallel(filepath, source_language, target_language, domain, mode, options, progress=progress)


def xliff(filepath: str, *, progress: bool = True) -> BaseStructure:
    """Parses an XLIFF filetype by filepath (string)"""
    from lokit.importers import import_xliff

    return import_xliff(filepath, progress=progress)


def csv(
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
) -> BaseStructure:
    """
    Parses a CSV filetype.\n
    Supports pre-translated files without translated content to prepare for translation and repopualtion in CSV.
    """
    from lokit.importers import import_csv

    return import_csv(
        filepath,
        source_locale,
        target_locale,
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


def csv_targets(
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
    """
    Parses a CSV filetype. Multiple target languages supported.\n
    Auto-detection for target language with language codes in the header.\n
    Supports pre-translated files without translated content to prepare for translation and repopualtion in CSV.\n
    """
    from lokit.importers import import_csv_targets

    return import_csv_targets(
        filepath,
        source_locale=source_locale,
        progress=progress,
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


def xlsx(
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
) -> BaseStructure:
    """
    Parses an XLSX (Excel sheet) filetype.\n
    Supports pre-translated files without translated content to prepare for translation and repopualtion in CSV.
    """
    from lokit.importers import import_xlsx

    return import_xlsx(
        filepath,
        source_locale,
        target_locale,
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


def xlsx_targets(
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
    """
    Parses an XLSX (Excel sheet) filetype. Multiple target languages supported.\n
    Auto-detection for target language with language codes in the header.\n
    Supports pre-translated files without translated content to prepare for translation and repopualtion in CSV.\n
    """
    from lokit.importers import import_xlsx_targets

    return import_xlsx_targets(
        filepath,
        source_locale=source_locale,
        progress=progress,
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


def html(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses an HTML filetype from string filepath"""
    from lokit.importers import import_html

    return import_html(filepath, source_locale, target_locale, progress=progress)


def po(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    mode: str = "gettext",
    progress: bool = True,
) -> BaseStructure:
    """Parses a portable object (.po) file, usually used with gettext() for localizing codebases"""
    from lokit.importers import import_po

    return import_po(filepath, source_locale, target_locale, mode=mode, progress=progress)


def po_targets(
    source_filepath: str,
    target_filepaths: Mapping[str, str],
    source_locale: str = "",
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses translation meapping across multiple portable object (.po) files."""
    from lokit.importers import import_po_targets

    return import_po_targets(source_filepath, target_filepaths, source_locale, progress=progress)


def json_i18n(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    target_filepath: str | None = None,
    target_filepaths: Mapping[str, str] | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses an i18n (internationalization) formatted JSON file"""
    from lokit.importers import import_json_i18n

    return import_json_i18n(
        filepath,
        source_locale,
        target_locale,
        target_filepath,
        target_filepaths,
        progress=progress,
    )


def idml(
    filepath: str,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses an Adobe In-Design (IDML) filetype from string path"""
    from lokit.importers import import_idml

    return import_idml(filepath, source_locale, target_locale, progress=progress)


def docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses a DOCX (Microsoft Word) filetype"""
    from lokit.importers import import_docx

    return import_docx(filepath, source_locale, target_locale, progress=progress)


def pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = True,
) -> BaseStructure:
    """Parses a PPTX (Microsoft Powerpoint [presentation] filetype)"""
    from lokit.importers import import_pptx

    return import_pptx(filepath, source_locale, target_locale, progress=progress)
