from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, StreamingStructure

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions

Structure = BaseStructure | StreamingStructure

__all__ = [
    "csv",
    "csv_async",
    "docx",
    "docx_async",
    "html",
    "html_async",
    "idml",
    "idml_async",
    "json",
    "json_async",
    "json_i18n",
    "json_i18n_async",
    "po",
    "po_async",
    "pptx",
    "pptx_async",
    "tmx",
    "tmx_async",
    "xliff",
    "xliff_async",
    "xlsx",
    "xlsx_async",
]


def csv(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_csv

    regen_csv(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
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
        resolve_placeholders=resolve_placeholders,
    )


async def csv_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_csv_async

    await regen_csv_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
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
        resolve_placeholders=resolve_placeholders,
    )


def xlsx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_xlsx

    regen_xlsx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
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
        resolve_placeholders=resolve_placeholders,
    )


async def xlsx_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    source_locale: str = "",
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_xlsx_async

    await regen_xlsx_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        source_locale=source_locale,
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
        resolve_placeholders=resolve_placeholders,
    )


def xliff(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_xliff

    regen_xliff(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


async def xliff_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_xliff_async

    await regen_xliff_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


def tmx(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_tmx

    regen_tmx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


async def tmx_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_tmx_async

    await regen_tmx_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


def po(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_po

    regen_po(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


async def po_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_po_async

    await regen_po_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


def json_i18n(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_json_i18n

    regen_json_i18n(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
        resolve_placeholders=resolve_placeholders,
    )


def json(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    json_i18n(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
        resolve_placeholders=resolve_placeholders,
    )


async def json_i18n_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_json_i18n_async

    await regen_json_i18n_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
        resolve_placeholders=resolve_placeholders,
    )


async def json_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
    resolve_placeholders: bool = True,
) -> None:
    await json_i18n_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
        resolve_placeholders=resolve_placeholders,
    )


def html(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_html

    regen_html(
        document,
        original_filepath,
        output_path,
        resolve_placeholders=resolve_placeholders,
    )


async def html_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_html_async

    await regen_html_async(
        document,
        original_filepath,
        output_path,
        resolve_placeholders=resolve_placeholders,
    )


def idml(
    document: BaseStructure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_idml

    regen_idml(
        document,
        original_filepath,
        output_path,
        resolve_placeholders=resolve_placeholders,
    )


async def idml_async(
    document: BaseStructure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    from lokit.exporters.regen import regen_idml_async

    await regen_idml_async(
        document,
        original_filepath,
        output_path,
        resolve_placeholders=resolve_placeholders,
    )


def docx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    from lokit.exporters.regen import regen_docx

    return regen_docx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


async def docx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    from lokit.exporters.regen import regen_docx_async

    return await regen_docx_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        resolve_placeholders=resolve_placeholders,
    )


def pptx(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    from lokit.exporters.regen import regen_pptx

    return regen_pptx(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )


async def pptx_async(
    document: Structure,
    original_filepath: DocumentSource,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    from lokit.exporters.regen import regen_pptx_async

    return await regen_pptx_async(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )
