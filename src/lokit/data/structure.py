from __future__ import annotations

from collections.abc import Iterable  # noqa: TC003 - mypyc needs this for compiled dataclass annotations.
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - mypyc needs this for compiled dataclass annotations.
from typing import TYPE_CHECKING

from lokit.compat import StrEnum

if TYPE_CHECKING:
    from lokit.data.tag_types import TieData


class TranslationStatus(StrEnum):
    """Status of a translation unit."""

    NEW = "new"
    DRAFT = "draft"
    TRANSLATED = "translated"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PluralCategory(StrEnum):
    """Standard plural category types."""

    GENERIC = "generic"
    ZERO = "zero"
    ONE = "one"
    TWO = "two"
    FEW = "few"
    MANY = "many"
    OTHER = "other"


@dataclass(slots=True)
class Plural:
    """Represents plural variations and constraints."""

    variant: str
    count: int | None = None
    category: PluralCategory | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Meta:
    """Usage stats, timestamps and limits metadata."""

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
    """Represents creation origin context of a segment."""

    system: str | None = None
    project: str | None = None
    creator_id: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Comment:
    """Translation metadata comment."""

    context: str
    timestamp: str | None = None
    origin: Origin | None = None
    context_key: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TextPart:
    """Plain text segment piece."""

    value: str


@dataclass(slots=True)
class CodePart:
    """Code placeholder / tag segment piece."""

    ref: str


SegmentPart = TextPart | CodePart


@dataclass(slots=True)
class Tags:
    """Inline tag map and parts for source/target texts."""

    source_tag_map: dict[str, TieData] = field(default_factory=dict)
    target_tag_map: dict[str, TieData] = field(default_factory=dict)
    source_parts: list[SegmentPart] = field(default_factory=list)
    target_parts: list[SegmentPart] = field(default_factory=list)


@dataclass(slots=True)
class TargetTags:
    """Target-specific inline tags."""

    tag_map: dict[str, TieData] = field(default_factory=dict)
    parts: list[SegmentPart] = field(default_factory=list)


@dataclass(slots=True)
class TargetData:
    """Translation target payload for a specific locale."""

    text: str | None = None
    status: TranslationStatus = TranslationStatus.UNKNOWN
    tags: TargetTags | None = None
    plural: Plural | None = None
    meta: Meta = field(default_factory=Meta)
    comments: list[Comment] = field(default_factory=list)
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AdjacentContext:
    """Structural/contextual details of adjacent units."""

    unit_id: str | None = None
    source: str | None = None
    target: str | None = None
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Data:
    """A translation unit's source text, targets, and metadata."""

    source: str
    target: str | None = None
    targets: dict[str, TargetData] = field(default_factory=dict)
    plural: Plural | None = None
    tags: Tags | None = None
    meta: Meta = field(default_factory=Meta)
    status: TranslationStatus = TranslationStatus.UNKNOWN
    comments: list[Comment] = field(default_factory=list)
    previous_context: AdjacentContext | None = None
    next_context: AdjacentContext | None = None
    extensions: dict[str, str] = field(default_factory=dict)


class ExportProxy:
    def __init__(self, document: BaseStructure | StreamingStructure) -> None:
        self._document = document

    def csv(
        self,
        filepath: str | Path,
        *,
        header_style: str = "generic",
        write_header: bool = True,
        source_column_name: str = "",
        target_column_name: str = "",
        include_id: bool = True,
        include_status: bool = True,
        include_comment: bool = True,
        include_target: bool = True,
        column_order: tuple[str, ...] = (),
    ) -> None:
        from lokit.parse import write

        write.csv(
            self._document,
            filepath,
            header_style=header_style,
            write_header=write_header,
            source_column_name=source_column_name,
            target_column_name=target_column_name,
            include_id=include_id,
            include_status=include_status,
            include_comment=include_comment,
            include_target=include_target,
            column_order=column_order,
        )

    def docx(
        self,
        filepath: str | Path,
        source_docx: str | Path | bytes | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        from lokit.parse import write

        write.docx(self._document, filepath, source_docx=source_docx, target_locale=target_locale)

    def html(self, filepath: str | Path, source_html: str | Path | None = None) -> None:
        from lokit.parse import write

        write.html(self._document, filepath, source_html)

    def idml(self, filepath: str | Path, source_idml: str | Path) -> None:
        from lokit.parse import write

        write.idml(_as_base_structure(self._document), filepath, source_idml)

    def json(self, filepath: str | Path, nested: bool = True) -> None:
        from lokit.parse import write

        write.json(self._document, filepath, nested)

    def json_i18n(self, filepath: str | Path, nested: bool = True) -> None:
        from lokit.parse import write

        write.json_i18n(self._document, filepath, nested)

    def po(self, filepath: str | Path) -> None:
        from lokit.parse import write

        write.po(self._document, filepath)

    def pptx(
        self,
        filepath: str | Path,
        source_pptx: str | Path | bytes | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        from lokit.parse import write

        write.pptx(self._document, filepath, source_pptx=source_pptx, target_locale=target_locale)

    def tmx(self, filepath: str | Path) -> None:
        from lokit.parse import write

        write.tmx(self._document, filepath)

    def xliff(self, filepath: str | Path, *, group_by_resource: bool = False) -> None:
        from lokit.parse import write

        write.xliff(self._document, filepath, group_by_resource=group_by_resource)

    def xlsx(
        self,
        filepath: str | Path,
        *,
        header_style: str = "generic",
        write_header: bool = True,
        source_column_name: str = "",
        target_column_name: str = "",
        include_id: bool = True,
        include_status: bool = True,
        include_comment: bool = True,
        include_target: bool = True,
        column_order: tuple[str, ...] = (),
    ) -> None:
        from lokit.parse import write

        write.xlsx(
            self._document,
            filepath,
            header_style=header_style,
            write_header=write_header,
            source_column_name=source_column_name,
            target_column_name=target_column_name,
            include_id=include_id,
            include_status=include_status,
            include_comment=include_comment,
            include_target=include_target,
            column_order=column_order,
        )


def _as_base_structure(document: BaseStructure | StreamingStructure) -> BaseStructure:
    if isinstance(document, BaseStructure):
        return document
    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=document.target_locale,
        data=dict(document.items),
        target_locales=document.target_locales,
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=document.target_language,
        target_languages=document.target_languages,
        extensions=document.extensions.copy(),
    )


@dataclass(slots=True)
class BaseStructure:
    """Root document container holding parsed translation units."""

    source_locale: str
    target_locale: str | None
    data: dict[str, Data]
    target_locales: tuple[str, ...] = ()
    format_version: str = "0.1"
    export_origin: str = ""
    export_timestamp: str = ""
    source_language: str | None = None
    target_language: str | None = None
    target_languages: tuple[str, ...] = ()
    extensions: dict[str, str] = field(default_factory=dict)

    @property
    def export(self) -> ExportProxy:
        return ExportProxy(self)


@dataclass(slots=True)
class StreamingStructure:
    """Streaming document container for translation units."""

    source_locale: str
    target_locale: str | None
    items: Iterable[tuple[str, Data]]
    target_locales: tuple[str, ...] = ()
    format_version: str = "0.1"
    export_origin: str = ""
    export_timestamp: str = ""
    source_language: str | None = None
    target_language: str | None = None
    target_languages: tuple[str, ...] = ()
    extensions: dict[str, str] = field(default_factory=dict)

    @property
    def export(self) -> ExportProxy:
        return ExportProxy(self)


@dataclass(slots=True)
class ConversionStats:
    """Performance and volume conversion statistics."""

    units_read: int
    units_written: int
    input_bytes: int
    output_bytes: int
    seconds: float
