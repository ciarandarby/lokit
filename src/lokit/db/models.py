from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TypeAlias, TypedDict


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]


class MatchInput(TypedDict, total=False):
    source: str
    source_locale: str
    target_locale: str
    previous_source: str
    next_source: str


@dataclass(slots=True)
class LoadStats:
    units_read: int
    units_written: int
    seconds: float


@dataclass(slots=True)
class UnitInsertRow:
    load_id: str
    id: str
    unit_key: str
    source_text: str
    target_text: Optional[str]
    source_locale: str
    target_locale: str
    status: str
    previous_source: str
    next_source: str
    project: str
    domain: str
    usage_count: int
    plural_variant: str
    plural_count: Optional[int]
    plural_category: str
    extensions: JsonDict = field(default_factory=dict)


@dataclass(slots=True)
class TagInsertRow:
    load_id: str
    source_locale: str
    tag_id: str
    tag_type: str
    position: int
    tag_order: int
    attribute_data: str
    pair_id: str
    original_name: str
    original_text: str
    attributes: JsonDict
    is_source: bool


@dataclass(slots=True)
class PartInsertRow:
    load_id: str
    source_locale: str
    is_source: bool
    position: int
    part_type: str
    value: str


@dataclass(slots=True)
class CommentInsertRow:
    load_id: str
    source_locale: str
    context: str
    timestamp: str
    context_key: str
    system: str
    project: str
    creator_id: str
    extensions: JsonDict


@dataclass(slots=True)
class SerializedUnit:
    unit: UnitInsertRow
    tags: list[TagInsertRow]
    parts: list[PartInsertRow]
    comments: list[CommentInsertRow]


@dataclass(slots=True)
class MatchRow:
    id: str
    unit_key: str
    source_text: str
    target_text: str
    status: str
    previous_source: str
    next_source: str
    score: float
    kind: str


@dataclass(slots=True)
class UnitFetchRow:
    id: str
    unit_key: str
    source_text: str
    target_text: Optional[str]
    source_locale: str
    target_locale: str
    status: str
    previous_source: str
    next_source: str
    usage_count: int
    plural_variant: str
    plural_count: Optional[int]
    plural_category: str
    extensions: JsonDict


@dataclass(slots=True)
class TagFetchRow:
    unit_id: str
    source_locale: str
    tag_id: str
    tag_type: str
    position: int
    tag_order: int
    attribute_data: str
    pair_id: str
    original_name: str
    original_text: str
    attributes: JsonDict
    is_source: bool


@dataclass(slots=True)
class PartFetchRow:
    unit_id: str
    source_locale: str
    is_source: bool
    position: int
    part_type: str
    value: str


@dataclass(slots=True)
class CommentFetchRow:
    unit_id: str
    source_locale: str
    context: str
    timestamp: str
    context_key: str
    system: str
    project: str
    creator_id: str
    extensions: JsonDict


@dataclass(slots=True)
class UnitWithChildren:
    unit: UnitFetchRow
    tags: list[TagFetchRow]
    parts: list[PartFetchRow]
    comments: list[CommentFetchRow]
