from __future__ import annotations

from pathlib import Path

from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.documents.models import DocumentSource, OfficeExportResult
from lokit.documents.office import export_pptx as _export_pptx
from lokit.documents.office import export_pptx_async as _export_pptx_async
from lokit.documents.options import OfficeExportOptions


def export_pptx(
    document: BaseStructure | StreamingStructure,
    filepath: str | Path,
    source_pptx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    return _export_pptx(
        document,
        filepath,
        source_pptx=source_pptx,
        target_locale=target_locale,
        options=options,
    )


async def export_pptx_async(
    document: BaseStructure | StreamingStructure,
    filepath: str | Path,
    source_pptx: DocumentSource | None = None,
    *,
    target_locale: str | None = None,
    options: OfficeExportOptions | None = None,
) -> OfficeExportResult:
    return await _export_pptx_async(
        document,
        filepath,
        source_pptx=source_pptx,
        target_locale=target_locale,
        options=options,
    )
