from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.office import export_docx as _export_docx
from lokit.office import export_docx_async as _export_docx_async

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.data.structure import BaseStructure, StreamingStructure
    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions


def export_docx(
    document: BaseStructure | StreamingStructure,
    filepath: str | Path,
    source_docx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    return _export_docx(
        document,
        filepath,
        source_docx=source_docx,
        target_locale=target_locale,
        options=options,
    )


async def export_docx_async(
    document: BaseStructure | StreamingStructure,
    filepath: str | Path,
    source_docx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    return await _export_docx_async(
        document,
        filepath,
        source_docx=source_docx,
        target_locale=target_locale,
        options=options,
    )
