from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, StreamingStructure

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.office.models import DocumentSource

Structure = BaseStructure | StreamingStructure

__all__ = ["csv", "docx", "html", "idml", "json", "json_i18n", "po", "pptx", "xliff", "xlsx"]


async def csv(document: BaseStructure, filepath: str | Path) -> None:
    """Asynchronously exports translation document data to a CSV file."""
    from lokit.exporters import export_csv_async

    await export_csv_async(document, filepath)


async def xlsx(document: BaseStructure, filepath: str | Path) -> None:
    """Asynchronously exports translation document data to an Excel sheet."""
    from lokit.exporters import export_xlsx_async

    await export_xlsx_async(document, filepath)


async def xliff(document: Structure, filepath: str | Path) -> None:
    """Asynchronously exports translation document data to an XLIFF container file."""
    from lokit.exporters import export_xliff_async

    await export_xliff_async(document, filepath)


async def html(document: Structure, filepath: str | Path, source_html: str | Path | None = None) -> None:
    """Asynchronously exports translation document data back into HTML format."""
    from lokit.exporters import export_html_async

    await export_html_async(document, filepath, source_html)


async def po(document: BaseStructure, filepath: str | Path) -> None:
    """Asynchronously exports translation document data to a Gettext PO file."""
    from lokit.exporters import export_po_async

    await export_po_async(document, filepath)


async def json(document: BaseStructure, filepath: str | Path, nested: bool = True) -> None:
    """Asynchronously exports translation document data to a standard localization JSON file."""
    from lokit.exporters import export_json_i18n_async

    await export_json_i18n_async(document, filepath, nested)


async def json_i18n(document: BaseStructure, filepath: str | Path, nested: bool = True) -> None:
    """Asynchronously exports translation document data to a standard localization JSON file (same as json)."""
    from lokit.exporters import export_json_i18n_async

    await export_json_i18n_async(document, filepath, nested)


async def idml(document: BaseStructure, filepath: str | Path, source_idml: str | Path) -> None:
    """Asynchronously exports translation document data by re-inserting targets back into Adobe InDesign IDML."""
    from lokit.exporters import export_idml_async

    await export_idml_async(document, filepath, source_idml)


async def docx(
    document: Structure,
    filepath: str | Path,
    source_docx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
) -> None:
    """Asynchronously exports translation document data by re-inserting targets back into Microsoft Word DOCX."""
    from lokit.exporters import export_docx_async

    await export_docx_async(document, filepath, source_docx, target_locale=target_locale)


async def pptx(
    document: Structure,
    filepath: str | Path,
    source_pptx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
) -> None:
    """Asynchronously exports translation document data by re-inserting targets back into Microsoft PowerPoint PPTX."""
    from lokit.exporters import export_pptx_async

    await export_pptx_async(document, filepath, source_pptx, target_locale=target_locale)
