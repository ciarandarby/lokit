from dataclasses import dataclass, field
from collections.abc import Iterable

from lokit.compat import StrEnum
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
    count: int | None = None
    category: PluralCategory | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Meta:
    usage_count: int | None = None
    last_used: str | None = None
    first_used: str | None = None
    created: str | None = None
    updated: str | None = None
    max_length: int | None = None
    min_length: int | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Origin:
    system: str | None = None
    project: str | None = None
    creator_id: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Comment:
    context: str
    timestamp: str | None = None
    origin: Origin | None = None
    context_key: str | None = None
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
    unit_id: str | None = None
    source: str | None = None
    target: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Data:
    source: str
    target: str | None = None
    plural: Plural | None = None
    tags: Tags | None = None
    meta: Meta = field(default_factory=Meta)
    status: TranslationStatus = TranslationStatus.UNKNOWN
    comments: list[Comment] = field(default_factory=list)
    previous_context: AdjacentContext | None = None
    next_context: AdjacentContext | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BaseStructure:
    source_locale: str
    target_locale: str | None
    data: dict[str, Data]
    format_version: str = "0.1"
    export_origin: str = ""
    export_timestamp: str = ""
    source_language: str | None = None
    target_language: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class StreamingStructure:
    source_locale: str
    target_locale: str | None
    items: Iterable[tuple[str, Data]]
    format_version: str = "0.1"
    export_origin: str = ""
    export_timestamp: str = ""
    source_language: str | None = None
    target_language: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ConversionStats:
    units_read: int
    units_written: int
    input_bytes: int
    output_bytes: int
    seconds: float
