from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.office import export_pptx as _export_pptx
from lokit.office import export_pptx_async as _export_pptx_async

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.data.structure import BaseStructure, StreamingStructure
    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions


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
