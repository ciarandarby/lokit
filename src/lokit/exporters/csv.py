from __future__ import annotations

import asyncio
import csv
from collections.abc import Iterable
from pathlib import Path

from lokit.data.structure import BaseStructure, Data, StreamingStructure, TranslationStatus
from lokit.io.atomic import atomic_output_path


Structure = BaseStructure | StreamingStructure


def export_csv(document: Structure, filepath: str | Path) -> None:
    path = Path(filepath)

    with atomic_output_path(path, "w") as fh:
        writer = csv.DictWriter(fh, fieldnames=["id", "source", "target", "status", "comment"])
        writer.writeheader()

        for unit_id, unit in _iter_items(document):
            comment = "; ".join(c.context for c in unit.comments if c.context)
            status = unit.status.value if unit.status != TranslationStatus.UNKNOWN else ""

            writer.writerow({
                "id": unit_id,
                "source": unit.source,
                "target": unit.target or "",
                "status": status,
                "comment": comment,
            })


async def export_csv_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_csv, document, filepath)


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items
