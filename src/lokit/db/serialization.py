from __future__ import annotations

from uuid import uuid4

from lokit.data.structure import (
    AdjacentContext,
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
from lokit.db.models import (
    CommentFetchRow,
    CommentInsertRow,
    JsonDict,
    JsonValue,
    PartFetchRow,
    PartInsertRow,
    SerializedUnit,
    TagFetchRow,
    TagInsertRow,
    UnitFetchRow,
    UnitInsertRow,
    UnitWithChildren,
)

META_KEY = "_lokit_meta"
PLURAL_EXTENSIONS_KEY = "_lokit_plural_extensions"
PLURAL_PRESENT_KEY = "_lokit_plural_present"
PREVIOUS_CONTEXT_KEY = "_lokit_previous_context"
NEXT_CONTEXT_KEY = "_lokit_next_context"


def serialize_unit(
    unit_key: str,
    data: Data,
    source_locale: str,
    target_locale: str,
    project: str = "",
    domain: str = "",
) -> SerializedUnit:
    load_id = str(uuid4())
    db_id = str(uuid4())
    previous_source = _context_source(data.previous_context)
    next_source = _context_source(data.next_context)
    project_value = project or _comment_project(data.comments)
    domain_value = domain or data.extensions.get("domain", "")

    unit = UnitInsertRow(
        load_id=load_id,
        id=db_id,
        unit_key=unit_key,
        source_text=data.source,
        target_text=data.target,
        source_locale=source_locale,
        target_locale=target_locale,
        status=data.status.value,
        previous_source=previous_source,
        next_source=next_source,
        project=project_value,
        domain=domain_value,
        usage_count=data.meta.usage_count or 0,
        plural_variant=data.plural.variant if data.plural is not None else "",
        plural_count=data.plural.count if data.plural is not None else None,
        plural_category=(
            data.plural.category.value if data.plural is not None and data.plural.category is not None else ""
        ),
        extensions=_data_extensions(data),
    )
    tags, parts = _serialize_tags(load_id, source_locale, data.tags)
    comments = _serialize_comments(load_id, source_locale, data.comments)
    return SerializedUnit(unit=unit, tags=tags, parts=parts, comments=comments)


def deserialize_unit(children: UnitWithChildren) -> tuple[str, Data]:
    row = children.unit
    tags = _deserialize_tags(children.tags, children.parts)
    data = Data(
        source=row.source_text,
        target=row.target_text,
        plural=_deserialize_plural(row),
        tags=tags,
        meta=_deserialize_meta(row),
        status=_translation_status(row.status),
        comments=_deserialize_comments(children.comments),
        previous_context=_deserialize_context(
            row.previous_source,
            row.extensions,
            PREVIOUS_CONTEXT_KEY,
        ),
        next_context=_deserialize_context(
            row.next_source,
            row.extensions,
            NEXT_CONTEXT_KEY,
        ),
        extensions=_deserialize_data_extensions(row.extensions),
    )
    return row.unit_key, data


def _data_extensions(data: Data) -> JsonDict:
    extensions: JsonDict = {key: value for key, value in data.extensions.items()}
    meta_payload = _meta_payload(data.meta)
    if meta_payload:
        extensions[META_KEY] = meta_payload
    if data.plural is not None:
        extensions[PLURAL_PRESENT_KEY] = True
        if data.plural.extensions:
            extensions[PLURAL_EXTENSIONS_KEY] = _str_dict_json(data.plural.extensions)
    previous_payload = _context_payload(data.previous_context)
    if previous_payload:
        extensions[PREVIOUS_CONTEXT_KEY] = previous_payload
    next_payload = _context_payload(data.next_context)
    if next_payload:
        extensions[NEXT_CONTEXT_KEY] = next_payload
    return extensions


def _meta_payload(meta: Meta) -> JsonDict:
    payload: JsonDict = {}
    _put_optional_int(payload, "usage_count", meta.usage_count)
    _put_optional_str(payload, "last_used", meta.last_used)
    _put_optional_str(payload, "first_used", meta.first_used)
    _put_optional_str(payload, "created", meta.created)
    _put_optional_str(payload, "updated", meta.updated)
    _put_optional_int(payload, "max_length", meta.max_length)
    _put_optional_int(payload, "min_length", meta.min_length)
    if meta.extensions:
        payload["extensions"] = _str_dict_json(meta.extensions)
    return payload


def _context_payload(context: AdjacentContext | None) -> JsonDict:
    if context is None:
        return {}
    payload: JsonDict = {}
    _put_optional_str(payload, "unit_id", context.unit_id)
    _put_optional_str(payload, "target", context.target)
    if context.extensions:
        payload["extensions"] = _str_dict_json(context.extensions)
    return payload


def _serialize_tags(
    load_id: str,
    source_locale: str,
    tags: Tags | None,
) -> tuple[list[TagInsertRow], list[PartInsertRow]]:
    if tags is None:
        return [], []

    tag_rows: list[TagInsertRow] = []
    part_rows: list[PartInsertRow] = []
    for tag in tags.source_tag_map.values():
        tag_rows.append(_serialize_tag(load_id, source_locale, tag, True))
    for tag in tags.target_tag_map.values():
        tag_rows.append(_serialize_tag(load_id, source_locale, tag, False))
    part_rows.extend(_serialize_parts(load_id, source_locale, tags.source_parts, True))
    part_rows.extend(_serialize_parts(load_id, source_locale, tags.target_parts, False))
    return tag_rows, part_rows


def _serialize_tag(
    load_id: str,
    source_locale: str,
    tag: TieData,
    is_source: bool,
) -> TagInsertRow:
    return TagInsertRow(
        load_id=load_id,
        source_locale=source_locale,
        tag_id=tag.id,
        tag_type=tag.type.value,
        position=tag.position,
        tag_order=tag.order,
        attribute_data=tag.attribute_data,
        pair_id=tag.pair_id or "",
        original_name=tag.original_name or "",
        original_text=tag.original_text or "",
        attributes=_str_dict_json(tag.attributes),
        is_source=is_source,
    )


def _serialize_parts(
    load_id: str,
    source_locale: str,
    parts: list[SegmentPart],
    is_source: bool,
) -> list[PartInsertRow]:
    rows: list[PartInsertRow] = []
    for position, part in enumerate(parts):
        if isinstance(part, CodePart):
            rows.append(
                PartInsertRow(
                    load_id=load_id,
                    source_locale=source_locale,
                    is_source=is_source,
                    position=position,
                    part_type="code",
                    value=part.ref,
                )
            )
        else:
            rows.append(
                PartInsertRow(
                    load_id=load_id,
                    source_locale=source_locale,
                    is_source=is_source,
                    position=position,
                    part_type="text",
                    value=part.value,
                )
            )
    return rows


def _serialize_comments(
    load_id: str,
    source_locale: str,
    comments: list[Comment],
) -> list[CommentInsertRow]:
    rows: list[CommentInsertRow] = []
    for comment in comments:
        origin = comment.origin
        rows.append(
            CommentInsertRow(
                load_id=load_id,
                source_locale=source_locale,
                context=comment.context,
                timestamp=comment.timestamp or "",
                context_key=comment.context_key or "",
                system=origin.system if origin is not None and origin.system is not None else "",
                project=(origin.project if origin is not None and origin.project is not None else ""),
                creator_id=(origin.creator_id if origin is not None and origin.creator_id is not None else ""),
                extensions=_str_dict_json(comment.extensions),
            )
        )
    return rows


def _deserialize_meta(row: UnitFetchRow) -> Meta:
    payload = _json_dict(row.extensions.get(META_KEY))
    return Meta(
        usage_count=_json_int(payload.get("usage_count")),
        last_used=_json_str_or_none(payload.get("last_used")),
        first_used=_json_str_or_none(payload.get("first_used")),
        created=_json_str_or_none(payload.get("created")),
        updated=_json_str_or_none(payload.get("updated")),
        max_length=_json_int(payload.get("max_length")),
        min_length=_json_int(payload.get("min_length")),
        extensions=_json_str_dict(payload.get("extensions")),
    )


def _deserialize_plural(row: UnitFetchRow) -> Plural | None:
    payload = row.extensions
    present = _json_bool(payload.get(PLURAL_PRESENT_KEY))
    if not present and not row.plural_variant and row.plural_count is None and not row.plural_category:
        return None
    category = _plural_category(row.plural_category) if row.plural_category else None
    return Plural(
        variant=row.plural_variant,
        count=row.plural_count,
        category=category,
        extensions=_json_str_dict(payload.get(PLURAL_EXTENSIONS_KEY)),
    )


def _deserialize_tags(
    tag_rows: list[TagFetchRow],
    part_rows: list[PartFetchRow],
) -> Tags | None:
    if not tag_rows and not part_rows:
        return None

    source_map: dict[str, TieData] = {}
    target_map: dict[str, TieData] = {}
    for row in tag_rows:
        tag = TieData(
            id=row.tag_id,
            type=_tie_type(row.tag_type),
            attributes=_json_str_dict(row.attributes),
            attribute_data=row.attribute_data,
            position=row.position,
            order=row.tag_order,
            pair_id=row.pair_id if row.pair_id else None,
            original_name=row.original_name if row.original_name else None,
            original_text=row.original_text if row.original_text else None,
        )
        if row.is_source:
            source_map[row.tag_id] = tag
        else:
            target_map[row.tag_id] = tag

    return Tags(
        source_tag_map=source_map,
        target_tag_map=target_map,
        source_parts=_deserialize_parts(part_rows, True),
        target_parts=_deserialize_parts(part_rows, False),
    )


def _deserialize_parts(rows: list[PartFetchRow], is_source: bool) -> list[SegmentPart]:
    parts: list[SegmentPart] = []
    for row in rows:
        if row.is_source != is_source:
            continue
        if row.part_type == "code":
            parts.append(CodePart(row.value))
        else:
            parts.append(TextPart(row.value))
    return parts


def _deserialize_comments(rows: list[CommentFetchRow]) -> list[Comment]:
    comments: list[Comment] = []
    for row in rows:
        origin = (
            Origin(
                system=row.system if row.system else None,
                project=row.project if row.project else None,
                creator_id=row.creator_id if row.creator_id else None,
            )
            if row.system or row.project or row.creator_id
            else None
        )
        comments.append(
            Comment(
                context=row.context,
                timestamp=row.timestamp if row.timestamp else None,
                origin=origin,
                context_key=row.context_key if row.context_key else None,
                extensions=_json_str_dict(row.extensions),
            )
        )
    return comments


def _deserialize_context(
    source: str,
    extensions: JsonDict,
    key: str,
) -> AdjacentContext | None:
    payload = _json_dict(extensions.get(key))
    if not source and not payload:
        return None
    return AdjacentContext(
        unit_id=_json_str_or_none(payload.get("unit_id")),
        source=source if source else None,
        target=_json_str_or_none(payload.get("target")),
        extensions=_json_str_dict(payload.get("extensions")),
    )


def _deserialize_data_extensions(extensions: JsonDict) -> dict[str, str]:
    skipped = {
        META_KEY,
        PLURAL_EXTENSIONS_KEY,
        PLURAL_PRESENT_KEY,
        PREVIOUS_CONTEXT_KEY,
        NEXT_CONTEXT_KEY,
    }
    result: dict[str, str] = {}
    for key, value in extensions.items():
        if key not in skipped:
            result[key] = _json_str(value)
    return result


def _translation_status(value: str) -> TranslationStatus:
    try:
        return TranslationStatus(value)
    except ValueError:
        return TranslationStatus.UNKNOWN


def _plural_category(value: str) -> PluralCategory | None:
    try:
        return PluralCategory(value)
    except ValueError:
        return None


def _tie_type(value: str) -> TieType:
    try:
        return TieType(value)
    except ValueError:
        return TieType.CUSTOM_STANDALONE


def _context_source(context: AdjacentContext | None) -> str:
    if context is None or context.source is None:
        return ""
    return context.source


def _comment_project(comments: list[Comment]) -> str:
    for comment in comments:
        if comment.origin is not None and comment.origin.project is not None:
            return comment.origin.project
    return ""


def _put_optional_str(payload: JsonDict, key: str, value: str | None) -> None:
    if value is not None:
        payload[key] = value


def _put_optional_int(payload: JsonDict, key: str, value: int | None) -> None:
    if value is not None:
        payload[key] = value


def _str_dict_json(values: dict[str, str]) -> JsonDict:
    return {key: value for key, value in values.items()}


def _json_dict(value: JsonValue) -> JsonDict:
    if isinstance(value, dict):
        return value
    return {}


def _json_str_dict(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: _json_str(item) for key, item in value.items()}


def _json_str_or_none(value: JsonValue) -> str | None:
    if value is None:
        return None
    return _json_str(value)


def _json_str(value: JsonValue) -> str:
    if value is None:
        return ""
    return str(value)


def _json_int(value: JsonValue) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    return None


def _json_bool(value: JsonValue) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False
