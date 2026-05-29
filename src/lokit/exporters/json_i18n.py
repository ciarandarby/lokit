from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.io.atomic import atomic_output_path

Structure = BaseStructure | StreamingStructure


def export_json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    output: dict[str, Any] = {}
    for key, unit in _iter_items(document):
        value = unit.target if unit.target is not None else unit.source
        if nested:
            _set_nested(output, key, value)
        else:
            output[key] = value

    with atomic_output_path(path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")


async def export_json_i18n_async(
    document: BaseStructure,
    filepath: str | Path,
    nested: bool = True,
) -> None:
    await asyncio.to_thread(export_json_i18n, document, filepath, nested)


def _set_nested(obj: dict[str, Any], dot_key: str, value: str) -> None:
    parts = dot_key.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items
