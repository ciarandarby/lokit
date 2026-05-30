from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, is_dataclass
from enum import StrEnum
from typing import TextIO
from pathlib import Path

from lokit.data.structure import Data
from lokit.format_detection import LokitInputFormat, detect_format
from lokit.io.atomic import atomic_output_path
from lokit.parsers.tmx.models import TmxParseMode


class LokitJsonContext(StrEnum):
    SOURCE = "source"
    TARGET = "target"
    PLURAL = "plural"
    TAGS = "tags"
    META = "meta"
    STATUS = "status"
    COMMENTS = "comments"
    PREVIOUS_CONTEXT = "previous_context"
    NEXT_CONTEXT = "next_context"
    EXTENSIONS = "extensions"


DEFAULT_JSON_CONTEXT: tuple[LokitJsonContext, LokitJsonContext] = (
    LokitJsonContext.SOURCE,
    LokitJsonContext.TARGET,
)


async def write_lokit_json_stream(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
) -> Path:
    input_path = Path(filepath)
    output_path = _resolve_output_path(input_path, Path(output))
    selected = _normalize_context(context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_format = detect_format(input_path)

    with atomic_output_path(output_path, "w") as f:
        if input_format is LokitInputFormat.TMX:
            from lokit.parsers.tmx.extraction import TmxExtractor

            for unit_id, data in TmxExtractor(
                str(input_path),
                mode=_tmx_mode(selected),
            ).extract():
                _write_record(f, unit_id, data, selected)
        else:
            async for unit_id, data in _stream_units(input_path):
                _write_record(f, unit_id, data, selected)
    return output_path


def _resolve_output_path(input_path: Path, output: Path) -> Path:
    if output.suffix:
        return output
    return output / f"{input_path.stem}.jsonl"


def _normalize_context(
    context: Iterable[LokitJsonContext | str] | None,
) -> tuple[LokitJsonContext, ...]:
    if context is None:
        return DEFAULT_JSON_CONTEXT
    return tuple(_normalize_context_item(item) for item in context)


def _normalize_context_item(item: LokitJsonContext | str) -> LokitJsonContext:
    if isinstance(item, LokitJsonContext):
        return item
    return LokitJsonContext(item)


def _write_record(
    f: TextIO,
    unit_id: str,
    data: Data,
    selected: tuple[LokitJsonContext, ...],
) -> None:
    if selected == DEFAULT_JSON_CONTEXT:
        dumps = json.dumps
        f.write(
            '{"id":'
            + dumps(unit_id, ensure_ascii=False, separators=(",", ":"), default=str)
            + ',"source":'
            + dumps(data.source, ensure_ascii=False, separators=(",", ":"), default=str)
            + ',"target":'
            + dumps(data.target, ensure_ascii=False, separators=(",", ":"), default=str)
            + "}\n"
        )
        return
    record: dict[str, object] = {"id": unit_id}
    for key in selected:
        record[key.value] = _json_value(data, key)
    json.dump(record, f, ensure_ascii=False, separators=(",", ":"), default=str)
    f.write("\n")


def _stream_units(input_path: Path) -> AsyncIterator[tuple[str, Data]]:
    from lokit.importers import import_file_async

    return import_file_async(str(input_path))


def _tmx_mode(selected: tuple[LokitJsonContext, ...]) -> TmxParseMode:
    full_keys = {
        LokitJsonContext.PLURAL,
        LokitJsonContext.TAGS,
        LokitJsonContext.META,
        LokitJsonContext.COMMENTS,
        LokitJsonContext.PREVIOUS_CONTEXT,
        LokitJsonContext.NEXT_CONTEXT,
        LokitJsonContext.EXTENSIONS,
    }
    if any(key in full_keys for key in selected):
        return TmxParseMode.FULL
    if LokitJsonContext.STATUS in selected:
        return TmxParseMode.TEXT_WITH_STATUS
    return TmxParseMode.TEXT


def _json_value(data: Data, key: LokitJsonContext) -> object:
    if key is LokitJsonContext.SOURCE:
        return data.source
    if key is LokitJsonContext.TARGET:
        return data.target
    if key is LokitJsonContext.PLURAL:
        return _to_jsonable(data.plural)
    if key is LokitJsonContext.TAGS:
        return _to_jsonable(data.tags)
    if key is LokitJsonContext.META:
        return _to_jsonable(data.meta)
    if key is LokitJsonContext.STATUS:
        return data.status.value
    if key is LokitJsonContext.COMMENTS:
        return _to_jsonable(data.comments)
    if key is LokitJsonContext.PREVIOUS_CONTEXT:
        return _to_jsonable(data.previous_context)
    if key is LokitJsonContext.NEXT_CONTEXT:
        return _to_jsonable(data.next_context)
    return _to_jsonable(data.extensions)


def _to_jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
