from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.parse.write import async_ as async_
from lokit.parse.write import regen as regen

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions

Structure = BaseStructure | StreamingStructure

__all__ = [
    "async_",
    "csv",
    "docx",
    "html",
    "idml",
    "json",
    "json_i18n",
    "lokit",
    "po",
    "pptx",
    "regen",
    "tmx",
    "xliff",
    "xlsx",
]


def csv(
    document: Structure,
    filepath: str | Path,
    *,
    header_style: str = "generic",
    write_header: bool = True,
    source_column_name: str = "",
    target_column_name: str = "",
    include_id: bool = True,
    include_status: bool = True,
    include_comment: bool = True,
    include_target: bool = True,
    column_order: tuple[str, ...] = (),
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to a CSV file."""
    from lokit.exporters import export_csv

    export_csv(
        document,
        filepath,
        header_style=header_style,
        write_header=write_header,
        source_column_name=source_column_name,
        target_column_name=target_column_name,
        include_id=include_id,
        include_status=include_status,
        include_comment=include_comment,
        include_target=include_target,
        column_order=column_order,
        resolve_placeholders=resolve_placeholders,
    )


def xlsx(
    document: Structure,
    filepath: str | Path,
    *,
    header_style: str = "generic",
    write_header: bool = True,
    source_column_name: str = "",
    target_column_name: str = "",
    include_id: bool = True,
    include_status: bool = True,
    include_comment: bool = True,
    include_target: bool = True,
    column_order: tuple[str, ...] = (),
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to an Excel XLSX spreadsheet."""
    from lokit.exporters import export_xlsx

    export_xlsx(
        document,
        filepath,
        header_style=header_style,
        write_header=write_header,
        source_column_name=source_column_name,
        target_column_name=target_column_name,
        include_id=include_id,
        include_status=include_status,
        include_comment=include_comment,
        include_target=include_target,
        column_order=column_order,
        resolve_placeholders=resolve_placeholders,
    )


def tmx(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to a TMX file."""
    from lokit.exporters import export_tmx

    export_tmx(document, filepath, resolve_placeholders=resolve_placeholders)


def xliff(
    document: Structure,
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to an XLIFF container file."""
    from lokit.exporters import export_xliff

    export_xliff(
        document,
        filepath,
        group_by_resource=group_by_resource,
        resolve_placeholders=resolve_placeholders,
    )


def html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data back into HTML format."""
    from lokit.exporters import export_html

    export_html(
        document,
        filepath,
        source_html,
        resolve_placeholders=resolve_placeholders,
    )


def po(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to a Gettext PO file."""
    from lokit.exporters import export_po

    export_po(document, filepath, resolve_placeholders=resolve_placeholders)


def json(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to a standard localization JSON file."""
    from lokit.exporters import export_json_i18n

    export_json_i18n(
        document,
        filepath,
        nested,
        resolve_placeholders=resolve_placeholders,
    )


def json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data to a standard localization JSON file (same as json)."""
    from lokit.exporters import export_json_i18n

    export_json_i18n(
        document,
        filepath,
        nested,
        resolve_placeholders=resolve_placeholders,
    )


def lokit(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports document data to Lokit's sparse interchange format."""
    from lokit.exporters import export_lokit

    export_lokit(document, filepath, resolve_placeholders=resolve_placeholders)


def idml(
    document: Structure,
    filepath: str | Path,
    source_idml: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Exports translation document data by re-inserting targets back into Adobe InDesign IDML."""
    from lokit.exporters import export_idml

    export_idml(
        document,
        filepath,
        source_idml,
        resolve_placeholders=resolve_placeholders,
    )


def docx(
    document: Structure,
    filepath: str | Path,
    source_docx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Exports translation document data by re-inserting targets back into Microsoft Word DOCX."""
    from lokit.exporters import export_docx

    return export_docx(
        document,
        filepath,
        source_docx,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )


def pptx(
    document: Structure,
    filepath: str | Path,
    source_pptx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Exports translation document data by re-inserting targets back into Microsoft PowerPoint PPTX."""
    from lokit.exporters import export_pptx

    return export_pptx(
        document,
        filepath,
        source_pptx,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )
