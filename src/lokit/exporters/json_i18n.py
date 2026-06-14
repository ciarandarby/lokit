from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TypeAlias, cast

from lokit.data.targets import target_text
from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.io.atomic import atomic_output_path

Structure = BaseStructure | StreamingStructure
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def export_json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
) -> None:
    path = Path(filepath)
    target_locales = _document_target_locales(document)
    if document.target_locale is None and len(target_locales) > 1:
        if path.suffix:
            raise ValueError(
                "JSON i18n export needs a target locale or directory output for multi-target documents"
            )
        path.mkdir(parents=True, exist_ok=True)
        for locale in target_locales:
            _export_one(document, path / f"{locale}.json", nested, locale)
        return

    selected_locale = document.target_locale or (target_locales[0] if target_locales else None)
    _export_one(document, path, nested, selected_locale)


def _export_one(
    document: Structure,
    path: Path,
    nested: bool,
    locale: str | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    output: JsonObject = {}
    for key, unit in _iter_items(document):
        value = target_text(unit, locale) or unit.source
        if nested:
            _set_nested(output, _unit_path(key, unit), value)
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


def _set_nested(obj: JsonObject, path: tuple[str, ...], value: str) -> None:
    parts = path
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = cast(JsonObject, current[part])
    current[parts[-1]] = value


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _document_target_locales(document: Structure) -> tuple[str, ...]:
    if document.target_locales:
        return document.target_locales
    if document.target_locale is not None:
        return (document.target_locale,)
    return ()


def _unit_path(key: str, unit: Data) -> tuple[str, ...]:
    raw_path = unit.extensions.get("json_path")
    if raw_path:
        decoded = json.loads(raw_path)
        if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
            return tuple(decoded)
    return tuple(key.split("."))
