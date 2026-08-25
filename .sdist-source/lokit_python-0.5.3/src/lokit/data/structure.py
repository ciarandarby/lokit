from __future__ import annotations

from collections.abc import Iterable  # noqa: TC003 - mypyc needs this for compiled dataclass annotations.
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - mypyc needs this for compiled dataclass annotations.
from typing import TYPE_CHECKING

from lokit.compat import StrEnum
from lokit.data.interchange_types import DEFAULT_DICT_FIELDS, DictField, StringMode, TranslationRow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lokit.data.tag_types import TieData
    from lokit.data.targets import StreamingTargetSplit
    from lokit.office.models import DocumentSource, OfficeExportResult
    from lokit.office.options import OfficeExportOptions


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
        resolve_placeholders: bool = True,
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
            resolve_placeholders=resolve_placeholders,
        )

    def docx(
        self,
        filepath: str | Path,
        source_docx: str | Path | bytes | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        from lokit.parse import write

        return write.docx(
            self._document,
            filepath,
            source_docx=source_docx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    def html(
        self,
        filepath: str | Path,
        source_html: str | Path | None = None,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse import write

        write.html(
            self._document,
            filepath,
            source_html,
            resolve_placeholders=resolve_placeholders,
        )

    def idml(
        self,
        filepath: str | Path,
        source_idml: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse import write

        write.idml(
            self._document,
            filepath,
            source_idml,
            resolve_placeholders=resolve_placeholders,
        )

    def json(
        self,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse import write

        write.json(
            self._document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    def json_i18n(
        self,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse import write

        write.json_i18n(
            self._document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    def lokit(self, filepath: str | Path, *, resolve_placeholders: bool = True) -> None:
        from lokit.parse import write

        write.lokit(
            self._document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    def po(self, filepath: str | Path, *, resolve_placeholders: bool = True) -> None:
        from lokit.parse import write

        write.po(
            self._document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    def pptx(
        self,
        filepath: str | Path,
        source_pptx: str | Path | bytes | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        from lokit.parse import write

        return write.pptx(
            self._document,
            filepath,
            source_pptx=source_pptx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    def tmx(self, filepath: str | Path, *, resolve_placeholders: bool = True) -> None:
        if resolve_placeholders and isinstance(self._document, BaseStructure):
            from lokit.parsers.interchange import try_native_base_export

            count = try_native_base_export(self._document, filepath, "tmx")
            if count is not None:
                return
        elif resolve_placeholders and isinstance(self._document, StreamingStructure):
            from lokit.parsers.interchange import try_native_interchange_export

            count = try_native_interchange_export(self._document, filepath, "tmx")
            if count is not None:
                return
        from lokit.parse import write

        write.tmx(
            self._document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    def xliff(
        self,
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
        resolve_placeholders: bool = True,
    ) -> None:
        if resolve_placeholders and isinstance(self._document, BaseStructure):
            from lokit.parsers.interchange import try_native_base_export

            count = try_native_base_export(
                self._document,
                filepath,
                "xliff",
                group_by_resource=group_by_resource,
            )
            if count is not None:
                return
        elif resolve_placeholders and isinstance(self._document, StreamingStructure):
            from lokit.parsers.interchange import try_native_interchange_export

            count = try_native_interchange_export(
                self._document,
                filepath,
                "xliff",
                group_by_resource=group_by_resource,
            )
            if count is not None:
                return
        from lokit.parse import write

        write.xliff(
            self._document,
            filepath,
            group_by_resource=group_by_resource,
            resolve_placeholders=resolve_placeholders,
        )

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
        resolve_placeholders: bool = True,
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
            resolve_placeholders=resolve_placeholders,
        )


class RegenProxy:
    def __init__(self, document: BaseStructure | StreamingStructure) -> None:
        self._document = document

    def csv(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        source_locale: str = "",
        header_mode: str = "auto",
        include_header_as_data: bool = False,
        source_column: str = "auto",
        target_column: str = "auto",
        target_columns: dict[str, str] | None = None,
        id_column: str = "auto",
        status_column: str = "auto",
        comment_column: str = "auto",
        preserve_extra_columns: bool = True,
        strict_language_headers: bool = True,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.csv(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            source_locale=source_locale,
            header_mode=header_mode,
            include_header_as_data=include_header_as_data,
            source_column=source_column,
            target_column=target_column,
            target_columns=target_columns,
            id_column=id_column,
            status_column=status_column,
            comment_column=comment_column,
            preserve_extra_columns=preserve_extra_columns,
            strict_language_headers=strict_language_headers,
            resolve_placeholders=resolve_placeholders,
        )

    def xlsx(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        source_locale: str = "",
        header_mode: str = "auto",
        include_header_as_data: bool = False,
        source_column: str = "auto",
        target_column: str = "auto",
        target_columns: dict[str, str] | None = None,
        id_column: str = "auto",
        status_column: str = "auto",
        comment_column: str = "auto",
        sheet_name: str = "",
        sheet_index: int = 0,
        preserve_extra_columns: bool = True,
        strict_language_headers: bool = True,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.xlsx(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            source_locale=source_locale,
            header_mode=header_mode,
            include_header_as_data=include_header_as_data,
            source_column=source_column,
            target_column=target_column,
            target_columns=target_columns,
            id_column=id_column,
            status_column=status_column,
            comment_column=comment_column,
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            preserve_extra_columns=preserve_extra_columns,
            strict_language_headers=strict_language_headers,
            resolve_placeholders=resolve_placeholders,
        )

    def xliff(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.xliff(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
        )

    def tmx(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.tmx(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
        )

    def po(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.po(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
        )

    def json(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        indent: int = 2,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.json(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            indent=indent,
            resolve_placeholders=resolve_placeholders,
        )

    def json_i18n(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        indent: int = 2,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.json_i18n(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            indent=indent,
            resolve_placeholders=resolve_placeholders,
        )

    def html(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.html(
            self._document,
            original_filepath,
            output_path,
            resolve_placeholders=resolve_placeholders,
        )

    def idml(
        self,
        original_filepath: str | Path,
        output_path: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        from lokit.parse.write import regen

        regen.idml(
            self._document,
            original_filepath,
            output_path,
            resolve_placeholders=resolve_placeholders,
        )

    def docx(
        self,
        original_filepath: DocumentSource,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        from lokit.parse.write import regen

        return regen.docx(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            resolve_placeholders=resolve_placeholders,
        )

    def pptx(
        self,
        original_filepath: DocumentSource,
        output_path: str | Path,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        from lokit.parse.write import regen

        return regen.pptx(
            self._document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
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

    @property
    def regen(self) -> RegenProxy:
        return RegenProxy(self)

    def to_dict(
        self,
        source_language: str = "",
        target_language: str = "",
        domain: str = "",
        *,
        fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
        strings: StringMode | str = StringMode.SANITIZED,
    ) -> list[TranslationRow]:
        """Materialize a flat, string-only projection of this document."""
        from lokit.data.dict_projection import iter_structure_rows, normalize_fields, normalize_string_mode

        streaming = StreamingStructure(
            source_locale=self.source_locale,
            target_locale=self.target_locale,
            items=self.data.items(),
            target_locales=self.target_locales,
            format_version=self.format_version,
            export_origin=self.export_origin,
            export_timestamp=self.export_timestamp,
            source_language=self.source_language,
            target_language=self.target_language,
            target_languages=self.target_languages,
            extensions=self.extensions,
        )
        return list(
            iter_structure_rows(
                streaming,
                fields=normalize_fields(fields),
                strings=normalize_string_mode(strings),
                source_language=source_language,
                target_language=target_language,
                domain=domain,
            )
        )

    def split_targets(
        self,
        target_locales: tuple[str, ...] = (),
        *,
        include_missing: bool = True,
    ) -> dict[str, BaseStructure]:
        """Create independent single-target documents."""
        from lokit.data.targets import split_targets

        return split_targets(
            self,
            target_locales,
            include_missing=include_missing,
        )


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

    @property
    def regen(self) -> RegenProxy:
        return RegenProxy(self)

    def to_dict(
        self,
        source_language: str = "",
        target_language: str = "",
        domain: str = "",
        *,
        fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
        strings: StringMode | str = StringMode.SANITIZED,
    ) -> Iterator[TranslationRow]:
        """Lazily project this document into flat, string-only rows."""
        from lokit.data.dict_projection import iter_structure_rows, normalize_fields, normalize_string_mode

        return iter_structure_rows(
            self,
            fields=normalize_fields(fields),
            strings=normalize_string_mode(strings),
            source_language=source_language,
            target_language=target_language,
            domain=domain,
        )

    def split_targets(
        self,
        target_locales: tuple[str, ...] = (),
        *,
        include_missing: bool = True,
    ) -> StreamingTargetSplit:
        """Spool this one-shot stream into bounded, target-specific streams."""
        from lokit.data.targets import StreamingTargetSplit

        return StreamingTargetSplit(
            self,
            target_locales,
            include_missing=include_missing,
        )


@dataclass(slots=True)
class ConversionStats:
    """Performance and volume conversion statistics."""

    units_read: int
    units_written: int
    input_bytes: int
    output_bytes: int
    seconds: float
