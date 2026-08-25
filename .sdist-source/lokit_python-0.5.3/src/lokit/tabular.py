from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence  # noqa: TC003 - mypyc needs these for compiled dataclasses.
from dataclasses import dataclass, field
from functools import lru_cache

from lokit.compat import StrEnum
from lokit.data.lang_codes import Language
from lokit.data.structure import BaseStructure, Comment, Data, StreamingStructure, TargetData, TranslationStatus
from lokit.data.targets import target_status, target_text


class HeaderMode(StrEnum):
    AUTO = "auto"
    PRESENT = "present"
    ABSENT = "absent"


class ColumnSelectorMode(StrEnum):
    AUTO = "auto"
    NAME = "name"
    INDEX = "index"
    ABSENT = "absent"


class ExportHeaderStyle(StrEnum):
    GENERIC = "generic"
    LOCALE = "locale"


@dataclass(frozen=True, slots=True)
class ColumnSelector:
    mode: ColumnSelectorMode = ColumnSelectorMode.AUTO
    name: str = ""
    index: int = -1

    @classmethod
    def auto(cls) -> ColumnSelector:
        return cls()

    @classmethod
    def name_selector(cls, name: str) -> ColumnSelector:
        return cls(mode=ColumnSelectorMode.NAME, name=name)

    @classmethod
    def index_selector(cls, index: int) -> ColumnSelector:
        return cls(mode=ColumnSelectorMode.INDEX, index=index)

    @classmethod
    def absent(cls) -> ColumnSelector:
        return cls(mode=ColumnSelectorMode.ABSENT)


@dataclass(frozen=True, slots=True)
class TabularImportOptions:
    header_mode: HeaderMode = HeaderMode.AUTO
    include_header_as_data: bool = False
    source_column: ColumnSelector = field(default_factory=ColumnSelector.auto)
    target_columns: Mapping[str, ColumnSelector] = field(default_factory=dict)
    id_column: ColumnSelector = field(default_factory=ColumnSelector.auto)
    status_column: ColumnSelector = field(default_factory=ColumnSelector.auto)
    comment_column: ColumnSelector = field(default_factory=ColumnSelector.auto)
    sheet_name: str = ""
    sheet_index: int = 0
    preserve_extra_columns: bool = True
    strict_language_headers: bool = True


@dataclass(frozen=True, slots=True)
class TabularExportOptions:
    header_style: ExportHeaderStyle = ExportHeaderStyle.GENERIC
    write_header: bool = True
    source_column_name: str = ""
    target_column_name: str = ""
    include_id: bool = True
    include_status: bool = True
    include_comment: bool = True
    include_target: bool = True
    column_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedTabularLayout:
    has_header: bool
    include_header_as_data: bool
    source_column: int
    target_columns: Mapping[str, int]
    id_column: int
    status_column: int
    comment_column: int
    extra_columns: Mapping[str, int]
    source_locale: str
    target_locale: str | None
    target_locales: tuple[str, ...]
    source_language: str | None
    target_language: str | None
    target_languages: tuple[str, ...]


Structure = BaseStructure | StreamingStructure

_METADATA_ALIASES = {
    "id": "id",
    "source": "source",
    "target": "target",
    "status": "status",
    "comment": "comment",
}
_LANGUAGE_HEADER_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
_COLUMN_LETTER_RE = re.compile(r"^[A-Za-z]+$")
_LANGUAGE_CODES = frozenset(name.rstrip("_").lower() for name in Language.__members__)
_STATUS_BY_VALUE: dict[str, TranslationStatus] = {status.value: status for status in TranslationStatus}


def infer_locales_from_filename(filepath: str) -> tuple[str, str | None]:
    stem = filepath.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "-" in stem:
        parts = stem.split("-")
        if len(parts) == 2:
            return _filename_locale_pair(parts[0], parts[1])
        if len(parts) == 4:
            return _filename_locale_pair(f"{parts[0]}-{parts[1]}", f"{parts[2]}-{parts[3]}")
    if "_" in stem:
        parts = stem.split("_")
        if len(parts) == 2:
            return _filename_locale_pair(parts[0], parts[1])
        if len(parts) == 4:
            return _filename_locale_pair(f"{parts[0]}_{parts[1]}", f"{parts[2]}_{parts[3]}")
    return "", None


def parse_base_lang(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()


def selector_from_user(value: str, *, allow_absent: bool = True) -> ColumnSelector:
    normalized = value.strip()
    lowered = normalized.lower()
    if not normalized or lowered == "auto":
        return ColumnSelector.auto()
    if allow_absent and lowered in {"absent", "none", "off", "false"}:
        return ColumnSelector.absent()
    if _looks_like_column_reference(normalized):
        return ColumnSelector.index_selector(column_reference_to_index(normalized))
    return ColumnSelector.name_selector(normalized)


def column_reference_to_index(value: str) -> int:
    normalized = value.strip().upper()
    if not _looks_like_column_reference(normalized):
        raise ValueError(f"Column reference {value!r} must use spreadsheet letters such as A or AB")
    index = 0
    for char in normalized:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def build_import_options(
    *,
    header_mode: str = "auto",
    include_header_as_data: bool = False,
    source_column: str = "auto",
    target_column: str = "auto",
    target_columns: Mapping[str, str] | None = None,
    id_column: str = "auto",
    status_column: str = "auto",
    comment_column: str = "auto",
    sheet_name: str = "",
    sheet_index: int = 0,
    preserve_extra_columns: bool = True,
    strict_language_headers: bool = True,
) -> TabularImportOptions:
    return TabularImportOptions(
        header_mode=HeaderMode(header_mode),
        include_header_as_data=include_header_as_data,
        source_column=selector_from_user(source_column, allow_absent=False),
        target_columns=_target_selectors_from_user(target_column, target_columns),
        id_column=selector_from_user(id_column),
        status_column=selector_from_user(status_column),
        comment_column=selector_from_user(comment_column),
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        preserve_extra_columns=preserve_extra_columns,
        strict_language_headers=strict_language_headers,
    )


def build_export_options(
    *,
    header_style: str = "generic",
    write_header: bool = True,
    source_column_name: str = "",
    target_column_name: str = "",
    include_id: bool = True,
    include_status: bool = True,
    include_comment: bool = True,
    include_target: bool = True,
    column_order: Sequence[str] = (),
) -> TabularExportOptions:
    return TabularExportOptions(
        header_style=ExportHeaderStyle(header_style),
        write_header=write_header,
        source_column_name=source_column_name,
        target_column_name=target_column_name,
        include_id=include_id,
        include_status=include_status,
        include_comment=include_comment,
        include_target=include_target,
        column_order=tuple(column_order),
    )


def _filename_locale_pair(source: str, target: str) -> tuple[str, str | None]:
    normalized_source = normalize_language_header(source)
    normalized_target = normalize_language_header(target)
    if normalized_source and normalized_target:
        return normalized_source, normalized_target
    return "", None


def _looks_like_column_reference(value: str) -> bool:
    normalized = value.strip()
    if not _COLUMN_LETTER_RE.fullmatch(normalized):
        return False
    if len(normalized) == 1:
        return True
    return len(normalized) <= 3 and normalized.isupper()


def _target_selectors_from_user(
    target_column: str,
    target_columns: Mapping[str, str] | None,
) -> dict[str, ColumnSelector]:
    if target_columns:
        selectors: dict[str, ColumnSelector] = {}
        for key, value in target_columns.items():
            if _looks_like_column_reference(key):
                selectors[value] = selector_from_user(key, allow_absent=False)
            else:
                selectors[key] = selector_from_user(value, allow_absent=False)
        return selectors
    if target_column.strip().lower() != "auto":
        return {"": selector_from_user(target_column, allow_absent=False)}
    return {}


def normalize_language_header(value: str) -> str:
    candidate = value.strip().lstrip("\ufeff")
    if not _LANGUAGE_HEADER_RE.fullmatch(candidate):
        return ""
    parts = candidate.replace("_", "-").split("-")
    base = parts[0].lower()
    if base not in _LANGUAGE_CODES:
        return ""

    normalized: list[str] = [base]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def resolve_tabular_layout(
    raw_header_cells: Sequence[str],
    row_width: int,
    options: TabularImportOptions,
    source_locale: str,
    target_locale: str | None,
    format_label: str,
) -> ResolvedTabularLayout:
    header_cells = _normalize_header_cells(raw_header_cells)
    metadata_columns = _metadata_columns(header_cells)
    language_columns = _language_columns(header_cells, metadata_columns, options)
    has_header = _has_header(options.header_mode, metadata_columns, language_columns)

    if options.header_mode == HeaderMode.PRESENT and not raw_header_cells:
        raise ValueError(f"{format_label.upper()} header row is required but file is empty")

    width = max(row_width, len(header_cells), 1)
    source_column = _resolve_source_column(
        options.source_column,
        has_header,
        header_cells,
        metadata_columns,
        language_columns,
        source_locale,
        width,
        format_label,
    )
    targets = _resolve_target_columns(
        options.target_columns,
        has_header,
        header_cells,
        metadata_columns,
        language_columns,
        source_column,
        target_locale,
        width,
        format_label,
    )

    resolved_source_locale = _resolve_source_locale(source_locale, source_column, language_columns)
    resolved_target_locale = _resolve_target_locale(target_locale, targets)
    resolved_target_locales = _resolve_target_locales(target_locale, targets)
    id_column = _resolve_optional_column(
        options.id_column,
        "id",
        has_header,
        header_cells,
        metadata_columns,
        width,
        format_label,
    )
    status_column = _resolve_optional_column(
        options.status_column,
        "status",
        has_header,
        header_cells,
        metadata_columns,
        width,
        format_label,
    )
    comment_column = _resolve_optional_column(
        options.comment_column,
        "comment",
        has_header,
        header_cells,
        metadata_columns,
        width,
        format_label,
    )

    extra_columns = _extra_columns(
        header_cells,
        width,
        {
            source_column,
            id_column,
            status_column,
            comment_column,
            *targets.values(),
        },
        has_header and options.preserve_extra_columns,
    )
    return ResolvedTabularLayout(
        has_header=has_header,
        include_header_as_data=options.include_header_as_data,
        source_column=source_column,
        target_columns=targets,
        id_column=id_column,
        status_column=status_column,
        comment_column=comment_column,
        extra_columns=extra_columns,
        source_locale=resolved_source_locale,
        target_locale=resolved_target_locale,
        target_locales=resolved_target_locales,
        source_language=parse_base_lang(resolved_source_locale) if resolved_source_locale else None,
        target_language=parse_base_lang(resolved_target_locale) if resolved_target_locale else None,
        target_languages=tuple(parse_base_lang(locale) for locale in resolved_target_locales),
    )


def make_tabular_data(
    row: Sequence[str],
    row_index: int,
    layout: ResolvedTabularLayout,
    format_label: str,
    target_locale: str | None,
) -> tuple[str, Data]:
    row_length = len(row)
    id_column = layout.id_column
    unit_id = row[id_column] if 0 <= id_column < row_length else ""
    if not unit_id:
        unit_id = f"{format_label}:{row_index}"

    status_column = layout.status_column
    status_text = row[status_column] if 0 <= status_column < row_length else ""
    status = parse_status(status_text)
    targets: dict[str, TargetData] = {}
    if target_locale is None:
        for locale, target_index in layout.target_columns.items():
            if not locale:
                continue
            raw_text = row[target_index] if 0 <= target_index < row_length else ""
            targets[locale] = TargetData(
                text=raw_text if raw_text else None,
                status=status,
            )

    raw_target = ""
    if target_locale is not None:
        target_index = layout.target_columns.get(target_locale, -1)
        if 0 <= target_index < row_length:
            raw_target = row[target_index]

    comments: list[Comment] = []
    comment_column = layout.comment_column
    comment_text = row[comment_column].strip() if 0 <= comment_column < row_length else ""
    if comment_text:
        comments.append(Comment(context=comment_text))

    extensions: dict[str, str] = {}
    for name, index in layout.extra_columns.items():
        value = row[index] if 0 <= index < row_length else ""
        if value:
            extensions[name] = value

    source_column = layout.source_column
    source = row[source_column] if 0 <= source_column < row_length else ""

    return unit_id, Data(
        source=source,
        target=raw_target if raw_target else None,
        targets=targets,
        status=status,
        comments=comments,
        extensions=extensions,
    )


def parse_status(value: str) -> TranslationStatus:
    if not value:
        return TranslationStatus.UNKNOWN
    return _parse_status_cached(value)


@lru_cache(maxsize=64)
def _parse_status_cached(value: str) -> TranslationStatus:
    normalized = value.strip().lower()
    if not normalized:
        return TranslationStatus.UNKNOWN
    return _STATUS_BY_VALUE.get(normalized, TranslationStatus.UNKNOWN)


def ensure_single_target(layout: ResolvedTabularLayout, requested_target: str | None) -> str | None:
    if requested_target is not None:
        return layout.target_locale
    return layout.target_locale


def export_fieldnames(document: Structure, options: TabularExportOptions) -> list[str]:
    if options.column_order:
        return list(options.column_order)

    fields: list[str] = []
    if options.include_id:
        fields.append("id")
    fields.append(_source_export_name(document, options))
    if options.include_target and _document_has_target(document):
        fields.extend(_target_export_names(document, options))
    if options.include_status:
        fields.append("status")
    if options.include_comment:
        fields.append("comment")
    return fields


def export_record(
    document: Structure,
    unit_id: str,
    unit: Data,
    fieldnames: Sequence[str],
    options: TabularExportOptions,
) -> dict[str, str]:
    source_name = _source_export_name(document, options)
    target_name = _target_export_name(document, options)
    comment = "; ".join(c.context for c in unit.comments if c.context)
    status = unit.status.value if unit.status != TranslationStatus.UNKNOWN else ""

    values: dict[str, str] = {
        "id": unit_id,
        source_name: unit.source,
        "source": unit.source,
        target_name: unit.target or "",
        "target": unit.target or "",
        "status": status,
        "comment": comment,
    }
    for locale in _document_target_locales(document):
        text = target_text(unit, locale)
        values[_target_export_name_for_locale(document, options, locale)] = text or ""
        values[locale] = text or ""
    if document.target_locale is None and unit.targets:
        first_status = _first_non_unknown_status(unit)
        if first_status != TranslationStatus.UNKNOWN:
            values["status"] = first_status.value
    return {name: values.get(name, "") for name in fieldnames}


def iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _normalize_header_cells(raw_header_cells: Sequence[str]) -> list[str]:
    header_cells = [cell.strip() for cell in raw_header_cells]
    if header_cells:
        header_cells[0] = header_cells[0].lstrip("\ufeff")
    return header_cells


def _metadata_columns(header_cells: Sequence[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, cell in enumerate(header_cells):
        role = _METADATA_ALIASES.get(cell.strip().lower())
        if role is not None and role not in columns:
            columns[role] = index
    return columns


def _language_columns(
    header_cells: Sequence[str],
    metadata_columns: Mapping[str, int],
    options: TabularImportOptions,
) -> dict[str, int]:
    metadata_indexes = set(metadata_columns.values())
    languages: dict[str, int] = {}
    for index, cell in enumerate(header_cells):
        if index in metadata_indexes:
            continue
        language = normalize_language_header(cell)
        if not language:
            continue
        if language in languages and options.strict_language_headers:
            raise ValueError(f"Duplicate language header {language!r} is ambiguous")
        languages[language] = index
    return languages


def _has_header(
    header_mode: HeaderMode,
    metadata_columns: Mapping[str, int],
    language_columns: Mapping[str, int],
) -> bool:
    if header_mode == HeaderMode.PRESENT:
        return True
    if header_mode == HeaderMode.ABSENT:
        return False
    return bool(metadata_columns or language_columns)


def _resolve_source_column(
    selector: ColumnSelector,
    has_header: bool,
    header_cells: Sequence[str],
    metadata_columns: Mapping[str, int],
    language_columns: Mapping[str, int],
    source_locale: str,
    width: int,
    format_label: str,
) -> int:
    explicit = _resolve_selector(selector, has_header, header_cells, width, "source", format_label)
    if explicit >= 0:
        return explicit
    if has_header:
        locale_match = _match_locale(source_locale, language_columns)
        if locale_match >= 0:
            return locale_match
        if "source" in metadata_columns:
            return metadata_columns["source"]
        if language_columns:
            return next(iter(language_columns.values()))
        for index in range(width):
            if index not in set(metadata_columns.values()):
                return index
    return 0


def _resolve_target_columns(
    selectors: Mapping[str, ColumnSelector],
    has_header: bool,
    header_cells: Sequence[str],
    metadata_columns: Mapping[str, int],
    language_columns: Mapping[str, int],
    source_column: int,
    target_locale: str | None,
    width: int,
    format_label: str,
) -> dict[str, int]:
    if selectors:
        targets: dict[str, int] = {}
        for locale, selector in selectors.items():
            target_name = target_locale if not locale and target_locale is not None else locale
            canonical = normalize_language_header(target_name) or target_name
            index = _resolve_selector(selector, has_header, header_cells, width, "target", format_label)
            if index < 0:
                raise ValueError(f"Target column selector for {target_name!r} does not resolve")
            targets[canonical] = index
        return targets

    if not has_header:
        return {}

    if target_locale is not None:
        target_match = _match_locale(target_locale, language_columns)
        if target_match >= 0:
            return {_canonical_locale(target_locale): target_match}
        if "target" in metadata_columns:
            return {target_locale: metadata_columns["target"]}
        return {}

    if "target" in metadata_columns:
        return {"": metadata_columns["target"]}

    targets = {locale: index for locale, index in language_columns.items() if index != source_column}
    return targets


def _resolve_optional_column(
    selector: ColumnSelector,
    role: str,
    has_header: bool,
    header_cells: Sequence[str],
    metadata_columns: Mapping[str, int],
    width: int,
    format_label: str,
) -> int:
    if selector.mode == ColumnSelectorMode.AUTO:
        if has_header:
            return metadata_columns.get(role, -1)
        return -1
    return _resolve_selector(selector, has_header, header_cells, width, role, format_label)


def _resolve_selector(
    selector: ColumnSelector,
    has_header: bool,
    header_cells: Sequence[str],
    width: int,
    role: str,
    format_label: str,
) -> int:
    if selector.mode == ColumnSelectorMode.AUTO:
        return -1
    if selector.mode == ColumnSelectorMode.ABSENT:
        return -1
    if selector.mode == ColumnSelectorMode.INDEX:
        if 0 <= selector.index < width:
            return selector.index
        raise ValueError(f"{role.title()} column index {selector.index} is outside the {format_label} row width")
    if selector.mode == ColumnSelectorMode.NAME:
        if not has_header:
            raise ValueError(f"{role.title()} column selector {selector.name!r} requires a header row")
        wanted = selector.name.strip().lower()
        for index, cell in enumerate(header_cells):
            if cell.strip().lower() == wanted:
                return index
        raise ValueError(f"{role.title()} column selector {selector.name!r} does not resolve")


def _match_locale(locale: str, language_columns: Mapping[str, int]) -> int:
    if not locale:
        return -1
    canonical = _canonical_locale(locale)
    if canonical in language_columns:
        return language_columns[canonical]
    base = parse_base_lang(locale)
    matches = [index for language, index in language_columns.items() if parse_base_lang(language) == base]
    if len(matches) == 1:
        return matches[0]
    return -1


def _canonical_locale(locale: str) -> str:
    return normalize_language_header(locale) or locale


def _resolve_source_locale(
    source_locale: str,
    source_column: int,
    language_columns: Mapping[str, int],
) -> str:
    for locale, index in language_columns.items():
        if index == source_column:
            return locale
    if source_locale:
        return _canonical_locale(source_locale)
    return ""


def _resolve_target_locale(
    target_locale: str | None,
    targets: Mapping[str, int],
) -> str | None:
    if target_locale is not None:
        return _canonical_locale(target_locale)
    if len(targets) == 1:
        locale = next(iter(targets))
        return locale or None
    return None


def _resolve_target_locales(
    target_locale: str | None,
    targets: Mapping[str, int],
) -> tuple[str, ...]:
    if target_locale is not None:
        return (_canonical_locale(target_locale),)
    return tuple(locale for locale in targets if locale)


def _extra_columns(
    header_cells: Sequence[str],
    width: int,
    used_columns: set[int],
    preserve: bool,
) -> dict[str, int]:
    if not preserve:
        return {}
    extra: dict[str, int] = {}
    for index in range(width):
        if index in used_columns:
            continue
        name = header_cells[index] if index < len(header_cells) and header_cells[index] else f"column_{index}"
        extra[name] = index
    return extra


def _cell(row: Sequence[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _source_export_name(document: Structure, options: TabularExportOptions) -> str:
    if options.source_column_name:
        return options.source_column_name
    if options.header_style == ExportHeaderStyle.LOCALE and document.source_locale:
        return document.source_locale
    return "source"


def _target_export_name(document: Structure, options: TabularExportOptions) -> str:
    if options.target_column_name:
        return options.target_column_name
    if options.header_style == ExportHeaderStyle.LOCALE and document.target_locale:
        return document.target_locale
    return "target"


def _document_has_target(document: Structure) -> bool:
    return document.target_locale is not None or bool(document.target_locales)


def _document_target_locales(document: Structure) -> tuple[str, ...]:
    if document.target_locales:
        return document.target_locales
    if document.target_locale is not None:
        return (document.target_locale,)
    return ()


def _target_export_names(document: Structure, options: TabularExportOptions) -> list[str]:
    locales = _document_target_locales(document)
    if len(locales) > 1 and not options.target_column_name:
        return [_target_export_name_for_locale(document, options, locale) for locale in locales]
    return [_target_export_name(document, options)]


def _target_export_name_for_locale(
    document: Structure,
    options: TabularExportOptions,
    locale: str,
) -> str:
    if len(_document_target_locales(document)) == 1:
        return _target_export_name(document, options)
    if options.header_style == ExportHeaderStyle.LOCALE:
        return locale
    return f"target_{locale}"


def _first_non_unknown_status(unit: Data) -> TranslationStatus:
    for locale in unit.targets:
        status = target_status(unit, locale)
        if status != TranslationStatus.UNKNOWN:
            return status
    return TranslationStatus.UNKNOWN
