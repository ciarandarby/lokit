from __future__ import annotations

import asyncio
from pathlib import Path

from openpyxl import Workbook

from lokit.data.structure import BaseStructure, TranslationStatus

_HEADERS = ["id", "source", "target", "status", "comment"]


def export_xlsx(document: BaseStructure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet()

    ws.append(_HEADERS)

    for unit_id, unit in document.data.items():
        comment = "; ".join(c.context for c in unit.comments if c.context)
        status = unit.status.value if unit.status != TranslationStatus.UNKNOWN else ""

        ws.append([
            unit_id,
            unit.source,
            unit.target or "",
            status,
            comment,
        ])

    wb.save(str(path))
    wb.close()


async def export_xlsx_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_xlsx, document, filepath)
