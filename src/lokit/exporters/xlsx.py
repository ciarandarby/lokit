from __future__ import annotations

import asyncio
import os
import tempfile
import contextlib
from collections.abc import Iterable
from pathlib import Path

from openpyxl import Workbook

from lokit.data.structure import BaseStructure, Data, StreamingStructure, TranslationStatus

_HEADERS = ["id", "source", "target", "status", "comment"]


Structure = BaseStructure | StreamingStructure


def export_xlsx(document: Structure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()

    wb = Workbook(write_only=True)
    ws = wb.create_sheet()

    ws.append(_HEADERS)

    try:
        for unit_id, unit in _iter_items(document):
            comment = "; ".join(c.context for c in unit.comments if c.context)
            status = unit.status.value if unit.status != TranslationStatus.UNKNOWN else ""

            ws.append([
                unit_id,
                unit.source,
                unit.target or "",
                status,
                comment,
            ])

        wb.save(str(tmp_path))
        wb.close()
        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        wb.close()
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


async def export_xlsx_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_xlsx, document, filepath)


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items
