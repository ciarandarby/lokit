from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    SegmentPart,
    Tags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType


def load_lokit_json(filepath: str | Path) -> BaseStructure:
    raw = json.loads(Path(filepath).read_text(encoding="utf-8"))
    return _parse_base(cast(dict[str, Any], raw))


def load_lokit_json_bytes(data: bytes) -> BaseStructure:
    raw = json.loads(data.decode("utf-8-sig"))
    return _parse_base(cast(dict[str, Any], raw))


def _parse_base(raw: dict[str, Any]) -> BaseStructure:
    data_raw = cast(dict[str, Any], raw.get("data", {}))
    return BaseStructure(
        source_locale=str(raw["source_locale"]),
        target_locale=_optional_str(raw.get("target_locale")),
        data={
            str(unit_id): _parse_data(cast(dict[str, Any], unit_raw))
            for unit_id, unit_raw in data_raw.items()
        },
        format_version=str(raw.get("format_version", "0.1")),
        export_origin=str(raw.get("export_origin", "")),
        export_timestamp=str(raw.get("export_timestamp", "")),
        source_language=_optional_str(raw.get("source_language")),
        target_language=_optional_str(raw.get("target_language")),
        extensions=_str_dict(raw.get("extensions")),
    )


def _parse_data(raw: dict[str, Any]) -> Data:
    return Data(
        source=str(raw["source"]),
        target=_optional_str(raw.get("target")),
        plural=_parse_plural(raw.get("plural")),
        tags=_parse_tags(raw.get("tags")),
        meta=_parse_meta(cast(dict[str, Any], raw.get("meta", {}))),
        status=TranslationStatus(str(raw.get("status", TranslationStatus.UNKNOWN))),
        comments=[
            _parse_comment(cast(dict[str, Any], item))
            for item in cast(list[Any], raw.get("comments", []))
        ],
        previous_context=_parse_adjacent_context(raw.get("previous_context")),
        next_context=_parse_adjacent_context(raw.get("next_context")),
        extensions=_str_dict(raw.get("extensions")),
    )


def _parse_plural(raw: object) -> Plural | None:
    if raw is None:
        return None
    data = cast(dict[str, Any], raw)
    category = data.get("category")
    return Plural(
        variant=str(data["variant"]),
        count=_optional_int(data.get("count")),
        category=PluralCategory(str(category)) if category is not None else None,
        extensions=_str_dict(data.get("extensions")),
    )


def _parse_meta(raw: dict[str, Any]) -> Meta:
    return Meta(
        usage_count=_optional_int(raw.get("usage_count")),
        last_used=_optional_str(raw.get("last_used")),
        first_used=_optional_str(raw.get("first_used")),
        created=_optional_str(raw.get("created")),
        updated=_optional_str(raw.get("updated")),
        max_length=_optional_int(raw.get("max_length")),
        min_length=_optional_int(raw.get("min_length")),
        extensions=_str_dict(raw.get("extensions")),
    )


def _parse_comment(raw: dict[str, Any]) -> Comment:
    return Comment(
        context=str(raw.get("context", "")),
        timestamp=_optional_str(raw.get("timestamp")),
        origin=_parse_origin(raw.get("origin")),
        context_key=_optional_str(raw.get("context_key")),
        extensions=_str_dict(raw.get("extensions")),
    )


def _parse_origin(raw: object) -> Origin | None:
    if raw is None:
        return None
    data = cast(dict[str, Any], raw)
    return Origin(
        system=_optional_str(data.get("system")),
        project=_optional_str(data.get("project")),
        creator_id=_optional_str(data.get("creator_id")),
        extensions=_str_dict(data.get("extensions")),
    )


def _parse_adjacent_context(raw: object) -> AdjacentContext | None:
    if raw is None:
        return None
    data = cast(dict[str, Any], raw)
    return AdjacentContext(
        unit_id=_optional_str(data.get("unit_id")),
        source=_optional_str(data.get("source")),
        target=_optional_str(data.get("target")),
        extensions=_str_dict(data.get("extensions")),
    )


def _parse_tags(raw: object) -> Tags | None:
    if raw is None:
        return None
    data = cast(dict[str, Any], raw)
    return Tags(
        source_tag_map=_parse_tag_map(data.get("source_tag_map")),
        target_tag_map=_parse_tag_map(data.get("target_tag_map")),
        source_parts=_parse_parts(data.get("source_parts")),
        target_parts=_parse_parts(data.get("target_parts")),
    )


def _parse_tag_map(raw: object) -> dict[str, TieData]:
    data = cast(dict[str, Any], raw or {})
    return {
        str(tag_id): _parse_tie_data(cast(dict[str, Any], tag_raw))
        for tag_id, tag_raw in data.items()
    }


def _parse_tie_data(raw: dict[str, Any]) -> TieData:
    return TieData(
        id=str(raw["id"]),
        type=TieType(str(raw["type"])),
        attributes=_str_dict(raw.get("attributes")),
        attribute_data=str(raw.get("attribute_data", "")),
        position=int(raw.get("position", 0)),
        order=int(raw.get("order", 0)),
        pair_id=_optional_str(raw.get("pair_id")),
        original_name=_optional_str(raw.get("original_name")),
    )


def _parse_parts(raw: object) -> list[SegmentPart]:
    parts: list[SegmentPart] = []
    for item in cast(list[Any], raw or []):
        data = cast(dict[str, Any], item)
        if "ref" in data:
            parts.append(CodePart(ref=str(data["ref"])))
        else:
            parts.append(TextPart(value=str(data.get("value", ""))))
    return parts


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected int-compatible value, got {type(value).__name__}")


def _str_dict(value: object) -> dict[str, str]:
    if value is None:
        return {}
    return {str(key): str(item) for key, item in cast(dict[Any, Any], value).items()}
