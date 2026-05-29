from __future__ import annotations

import asyncio
import csv
from pathlib import Path

from lokit.data.structure import BaseStructure, TranslationStatus


def export_csv(document: BaseStructure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["id", "source", "target", "status", "comment"])
        writer.writeheader()

        for unit_id, unit in document.data.items():
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
