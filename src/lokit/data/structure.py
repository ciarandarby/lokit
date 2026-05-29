from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

from lokit.data.tag_types import TieData


class TranslationStatus(StrEnum):
    NEW = "new"
    DRAFT = "draft"
    TRANSLATED = "translated"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PluralCategory(StrEnum):
    GENERIC = "generic"
    ZERO = "zero"
    ONE = "one"
    TWO = "two"
    FEW = "few"
    MANY = "many"
    OTHER = "other"


@dataclass(slots=True)
class Plural:
    variant: str
    count: Optional[int] = None
    category: Optional[PluralCategory] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Meta:
    usage_count: Optional[int] = None
    last_used: Optional[str] = None
    first_used: Optional[str] = None
    created: Optional[str] = None
    updated: Optional[str] = None
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Origin:
    system: Optional[str] = None
    project: Optional[str] = None
    creator_id: Optional[str] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Comment:
    context: str
    timestamp: Optional[str] = None
    origin: Optional[Origin] = None
    context_key: Optional[str] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TextPart:
    value: str


@dataclass(slots=True)
class CodePart:
    ref: str


SegmentPart = TextPart | CodePart


@dataclass(slots=True)
class Tags:
    source_tag_map: dict[str, TieData] = field(default_factory=dict)
    target_tag_map: dict[str, TieData] = field(default_factory=dict)
    source_parts: list[SegmentPart] = field(default_factory=list)
    target_parts: list[SegmentPart] = field(default_factory=list)


@dataclass(slots=True)
class AdjacentContext:
    unit_id: Optional[str] = None
    source: Optional[str] = None
    target: Optional[str] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Data:
    source: str
    target: Optional[str] = None
    plural: Optional[Plural] = None
    tags: Optional[Tags] = None
    meta: Meta = field(default_factory=Meta)
    status: TranslationStatus = TranslationStatus.UNKNOWN
    comments: list[Comment] = field(default_factory=list)
    previous_context: Optional[AdjacentContext] = None
    next_context: Optional[AdjacentContext] = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BaseStructure:
    source_locale: str
    target_locale: Optional[str]
    data: dict[str, Data]
    format_version: str = "0.1"
    export_origin: str = ""
    export_timestamp: str = ""
    source_language: Optional[str] = None
    target_language: Optional[str] = None
    extensions: dict[str, str] = field(default_factory=dict)
