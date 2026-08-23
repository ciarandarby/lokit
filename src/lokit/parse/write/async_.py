from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, StreamingStructure

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions

Structure = BaseStructure | StreamingStructure

__all__ = ["csv", "docx", "html", "idml", "json", "json_i18n", "lokit", "po", "pptx", "tmx", "xliff", "xlsx"]


async def csv(
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
    """Asynchronously exports translation document data to a CSV file."""
    from lokit.exporters import export_csv_async

    await export_csv_async(
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


async def xlsx(
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
    """Asynchronously exports translation document data to an Excel sheet."""
    from lokit.exporters import export_xlsx_async

    await export_xlsx_async(
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


async def tmx(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data to a TMX file."""
    from lokit.exporters import export_tmx_async

    await export_tmx_async(
        document,
        filepath,
        resolve_placeholders=resolve_placeholders,
    )


async def xliff(
    document: Structure,
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data to an XLIFF container file."""
    from lokit.exporters import export_xliff_async

    await export_xliff_async(
        document,
        filepath,
        group_by_resource=group_by_resource,
        resolve_placeholders=resolve_placeholders,
    )


async def html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data back into HTML format."""
    from lokit.exporters import export_html_async

    await export_html_async(
        document,
        filepath,
        source_html,
        resolve_placeholders=resolve_placeholders,
    )


async def po(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data to a Gettext PO file."""
    from lokit.exporters import export_po_async

    await export_po_async(
        document,
        filepath,
        resolve_placeholders=resolve_placeholders,
    )


async def json(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data to a standard localization JSON file."""
    from lokit.exporters import export_json_i18n_async

    await export_json_i18n_async(
        document,
        filepath,
        nested,
        resolve_placeholders=resolve_placeholders,
    )


async def json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data to a standard localization JSON file (same as json)."""
    from lokit.exporters import export_json_i18n_async

    await export_json_i18n_async(
        document,
        filepath,
        nested,
        resolve_placeholders=resolve_placeholders,
    )


async def lokit(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports document data to Lokit's sparse interchange format."""
    from lokit.exporters import export_lokit_async

    await export_lokit_async(
        document,
        filepath,
        resolve_placeholders=resolve_placeholders,
    )


async def idml(
    document: BaseStructure,
    filepath: str | Path,
    source_idml: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    """Asynchronously exports translation document data by re-inserting targets back into Adobe InDesign IDML."""
    from lokit.exporters import export_idml_async

    await export_idml_async(
        document,
        filepath,
        source_idml,
        resolve_placeholders=resolve_placeholders,
    )


async def docx(
    document: Structure,
    filepath: str | Path,
    source_docx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Asynchronously exports translation document data by re-inserting targets back into Microsoft Word DOCX."""
    from lokit.exporters import export_docx_async

    return await export_docx_async(
        document,
        filepath,
        source_docx,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )


async def pptx(
    document: Structure,
    filepath: str | Path,
    source_pptx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
    resolve_placeholders: bool = True,
) -> OfficeExportResult:
    """Asynchronously exports translation document data by re-inserting targets back into Microsoft PowerPoint PPTX."""
    from lokit.exporters import export_pptx_async

    return await export_pptx_async(
        document,
        filepath,
        source_pptx,
        target_locale=target_locale,
        options=options,
        resolve_placeholders=resolve_placeholders,
    )
