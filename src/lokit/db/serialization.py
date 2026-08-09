from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeAlias, cast
from uuid import uuid4

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
    StreamingStructure,
    Tags,
    TargetData,
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

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

Structure: TypeAlias = BaseStructure | StreamingStructure

META_KEY = "_lokit_meta"
PLURAL_EXTENSIONS_KEY = "_lokit_plural_extensions"
PLURAL_PRESENT_KEY = "_lokit_plural_present"
PREVIOUS_CONTEXT_KEY = "_lokit_previous_context"
NEXT_CONTEXT_KEY = "_lokit_next_context"
TAG_ORIGINAL_TEXT_PRESENCE_KEY = "_lokit_tag_original_text_presence"


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


def serialize_unit(
    unit_key: str,
    data: Data,
    source_locale: str,
    target_locale: str,
    project: str = "",
    domain: str = "",
) -> SerializedUnit:
    """Serialize one translation unit into rows suitable for database insertion."""
    load_id = str(uuid4())
    db_id = str(uuid4())
    previous_source = _context_source(data.previous_context)
    next_source = _context_source(data.next_context)
    project_value = project or data.extensions.get("project", "") or _comment_project(data.comments)
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
    """Reconstruct a translation unit from a fetched row and its child rows."""
    row = children.unit
    tags = _deserialize_tags(children.tags, children.parts, row.extensions)
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
        extensions=_deserialize_data_extensions(row.extensions, row.project, row.domain),
    )
    return row.unit_key, data


def iter_serialized_units(
    document: Structure,
    *,
    project: str = "",
    domain: str = "",
) -> Generator[SerializedUnit, None, None]:
    """Lazily serialize every target in an in-memory or streaming document."""
    source_locale = document.source_locale
    document_target_locale = document.target_locale or ""
    items = iter(_iter_document(document))
    try:
        for unit_key, data in items:
            if data.targets:
                for locale, target in data.targets.items():
                    yield serialize_unit(
                        unit_key,
                        _data_for_target(data, target),
                        source_locale,
                        locale,
                        project,
                        domain,
                    )
                continue
            yield serialize_unit(
                unit_key,
                data,
                source_locale,
                document_target_locale,
                project,
                domain,
            )
    finally:
        candidate: object = items
        if hasattr(candidate, "close"):
            cast("_ClosableIterator", candidate).close()


def _iter_document(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _data_for_target(data: Data, target: TargetData) -> Data:
    return Data(
        source=data.source,
        target=target.text,
        plural=target.plural or data.plural,
        tags=_tags_for_target(data.tags, target),
        meta=_merge_meta(data.meta, target.meta),
        status=(target.status if target.status is not TranslationStatus.UNKNOWN else data.status),
        comments=target.comments if target.comments else data.comments,
        previous_context=data.previous_context,
        next_context=data.next_context,
        extensions={**data.extensions, **target.extensions},
    )


def _tags_for_target(base: Tags | None, target: TargetData) -> Tags | None:
    target_tags = target.tags
    if base is None and target_tags is None:
        return None
    return Tags(
        source_tag_map=base.source_tag_map.copy() if base is not None else {},
        target_tag_map=target_tags.tag_map.copy() if target_tags is not None else {},
        source_parts=list(base.source_parts) if base is not None else [],
        target_parts=list(target_tags.parts) if target_tags is not None else [],
    )


def _merge_meta(base: Meta, target: Meta) -> Meta:
    if (
        target.usage_count is None
        and target.last_used is None
        and target.first_used is None
        and target.created is None
        and target.updated is None
        and target.max_length is None
        and target.min_length is None
        and not target.extensions
    ):
        return base
    return Meta(
        usage_count=target.usage_count if target.usage_count is not None else base.usage_count,
        last_used=target.last_used if target.last_used is not None else base.last_used,
        first_used=target.first_used if target.first_used is not None else base.first_used,
        created=target.created if target.created is not None else base.created,
        updated=target.updated if target.updated is not None else base.updated,
        max_length=target.max_length if target.max_length is not None else base.max_length,
        min_length=target.min_length if target.min_length is not None else base.min_length,
        extensions={**base.extensions, **target.extensions},
    )


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
    tag_original_text_presence = _tag_original_text_presence(data.tags)
    if tag_original_text_presence:
        extensions[TAG_ORIGINAL_TEXT_PRESENCE_KEY] = tag_original_text_presence
    return extensions


def _tag_original_text_presence(tags: Tags | None) -> JsonDict:
    if tags is None:
        return {}
    source: list[JsonValue] = [tag.id for tag in tags.source_tag_map.values() if tag.original_text == ""]
    target: list[JsonValue] = [tag.id for tag in tags.target_tag_map.values() if tag.original_text == ""]
    payload: JsonDict = {}
    if source:
        payload["source"] = source
    if target:
        payload["target"] = target
    return payload


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
    serialized_usage_count = _json_int(payload.get("usage_count"))
    return Meta(
        usage_count=row.usage_count if row.usage_count != 0 else serialized_usage_count,
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
    extensions: JsonDict,
) -> Tags | None:
    if not tag_rows and not part_rows:
        return None

    original_text_presence = _json_dict(extensions.get(TAG_ORIGINAL_TEXT_PRESENCE_KEY))
    source_original_text = _json_str_set(original_text_presence.get("source"))
    target_original_text = _json_str_set(original_text_presence.get("target"))
    source_map: dict[str, TieData] = {}
    target_map: dict[str, TieData] = {}
    for row in tag_rows:
        empty_original_text = source_original_text if row.is_source else target_original_text
        tag = TieData(
            id=row.tag_id,
            type=_tie_type(row.tag_type),
            attributes=_json_str_dict(row.attributes),
            attribute_data=row.attribute_data,
            position=row.position,
            order=row.tag_order,
            pair_id=row.pair_id if row.pair_id else None,
            original_name=row.original_name if row.original_name else None,
            original_text=(
                row.original_text if row.original_text else ("" if row.tag_id in empty_original_text else None)
            ),
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


def _deserialize_data_extensions(
    extensions: JsonDict,
    project: str,
    domain: str,
) -> dict[str, str]:
    skipped = {
        META_KEY,
        PLURAL_EXTENSIONS_KEY,
        PLURAL_PRESENT_KEY,
        PREVIOUS_CONTEXT_KEY,
        NEXT_CONTEXT_KEY,
        TAG_ORIGINAL_TEXT_PRESENCE_KEY,
    }
    result: dict[str, str] = {}
    for key, value in extensions.items():
        if key not in skipped:
            result[key] = _json_str(value)
    if project:
        result["project"] = project
    if domain:
        result["domain"] = domain
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


def _json_str_set(value: JsonValue) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


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
