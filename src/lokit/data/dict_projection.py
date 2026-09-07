from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from lokit.data.interchange_types import DictField, StringMode, TranslationRow
from lokit.data.structure import (
    CodePart,
    Data,
    StreamingStructure,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.types.content import TagIntegrityError, legacy_parts_match_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from lokit.data.structure import SegmentPart
    from lokit.data.tag_types import TieData
    from lokit.format_detection import LokitInputFormat
    from lokit.placeholders import PlaceholderSyntax


class _ProjectionOptions(TypedDict):
    runtime_placeholders: bool
    inline_placeholders: bool
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None


class _Closable(Protocol):
    def close(self) -> None: ...


def normalize_fields(fields: Iterable[DictField | str]) -> tuple[DictField, ...]:
    normalized: list[DictField] = []
    seen: set[DictField] = set()
    for value in fields:
        field = value if isinstance(value, DictField) else DictField(value)
        if field in seen:
            raise ValueError(f"duplicate dictionary field: {field.value}")
        normalized.append(field)
        seen.add(field)
    return tuple(normalized)


def normalize_string_mode(strings: StringMode | str) -> StringMode:
    return strings if isinstance(strings, StringMode) else StringMode(strings)


def iter_file_rows(
    filepath: str | Path,
    source_language: str,
    target_language: str,
    domain: str,
    fields: tuple[DictField, ...],
    strings: StringMode,
    *,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> Iterator[TranslationRow]:
    from lokit.format_detection import LokitInputFormat, detect_format

    detected = detect_format(filepath)
    if strings is StringMode.SANITIZED and detected in (LokitInputFormat.TMX, LokitInputFormat.XLIFF):
        from lokit._interchange_rust import Reader

        reader = Reader(str(filepath), detected.value, source_language or None)
        try:
            selected_fields = [field.value for field in fields]
            syntaxes = [str(syntax) for syntax in placeholder_syntaxes] if placeholder_syntaxes is not None else None
            while rows := reader.read_row_batch(
                selected_fields,
                source_language,
                target_language,
                domain,
                runtime_placeholders,
                inline_placeholders,
                syntaxes,
            ):
                yield from rows
        finally:
            reader.close()
        return
    document = _open_streaming_document(
        filepath,
        source_language,
        target_language,
        domain,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
        detected=detected,
    )
    yield from iter_structure_rows(
        document,
        fields=fields,
        strings=strings,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
    )


def iter_structure_rows(
    document: StreamingStructure,
    *,
    fields: tuple[DictField, ...],
    strings: StringMode,
    source_language: str = "",
    target_language: str = "",
    domain: str = "",
) -> Iterator[TranslationRow]:
    source_locale = document.source_locale
    resolved_source_language = (
        _base_language(source_language) or document.source_language or _base_language(source_locale)
    )
    input_format = document.extensions.get("input_format", "")
    items = iter(document.items)
    try:
        for unit_id, data in items:
            emitted = False
            if data.target is not None:
                legacy_locale = document.target_locale or target_language
                if _matches_language(legacy_locale, target_language):
                    yield _row(
                        fields,
                        strings,
                        input_format,
                        unit_id,
                        data,
                        None,
                        legacy_locale,
                        source_locale,
                        resolved_source_language,
                        target_language,
                        domain,
                    )
                    emitted = True
            for locale, target in data.targets.items():
                if not _matches_language(locale, target_language):
                    continue
                if data.target is not None and locale == document.target_locale:
                    continue
                yield _row(
                    fields,
                    strings,
                    input_format,
                    unit_id,
                    data,
                    target,
                    locale,
                    source_locale,
                    resolved_source_language,
                    target_language,
                    domain,
                )
                emitted = True
            if not emitted and data.target is None and not data.targets and not target_language:
                yield _row(
                    fields,
                    strings,
                    input_format,
                    unit_id,
                    data,
                    None,
                    "",
                    source_locale,
                    resolved_source_language,
                    target_language,
                    domain,
                )
    finally:
        candidate: object = items
        if hasattr(candidate, "close"):
            cast("_Closable", candidate).close()


def _open_streaming_document(
    filepath: str | Path,
    source_language: str,
    target_language: str,
    domain: str,
    *,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None,
    detected: LokitInputFormat | None = None,
) -> StreamingStructure:
    from lokit.format_detection import LokitInputFormat, detect_format
    from lokit.importers import (
        stream_csv,
        stream_docx,
        stream_html,
        stream_idml,
        stream_json_i18n,
        stream_lokit,
        stream_lokit_json,
        stream_po,
        stream_pptx,
        stream_tmx,
        stream_xliff,
        stream_xlsx,
    )

    path = str(filepath)
    if detected is None:
        detected = detect_format(filepath)
    projection_options: _ProjectionOptions = {
        "runtime_placeholders": runtime_placeholders,
        "inline_placeholders": inline_placeholders,
        "placeholder_syntaxes": placeholder_syntaxes,
    }
    if detected is LokitInputFormat.LOKIT:
        return stream_lokit(path, **projection_options)
    if detected is LokitInputFormat.LOKIT_JSON:
        return stream_lokit_json(path, **projection_options)
    if detected is LokitInputFormat.TMX:
        return stream_tmx(
            path,
            source_language=source_language or None,
            # Leave targets unselected so a base-language request such as
            # ``de`` can match every concrete locale (for example ``de-DE``)
            # in the bounded per-unit projection below.
            target_language=None,
            domain=domain or None,
            **projection_options,
        )
    if detected is LokitInputFormat.XLIFF:
        return stream_xliff(path, **projection_options)
    if detected is LokitInputFormat.CSV:
        return stream_csv(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.XLSX:
        return stream_xlsx(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.DOCX:
        return stream_docx(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.PPTX:
        return stream_pptx(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.HTML:
        return stream_html(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.PO:
        return stream_po(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.JSON_I18N:
        return stream_json_i18n(path, source_language, target_language or None, **projection_options)
    if detected is LokitInputFormat.IDML:
        return stream_idml(path, source_language, target_language or None, **projection_options)
    raise ValueError(f"Unsupported input format for dictionary projection: {detected.value}")


def _row(
    fields: tuple[DictField, ...],
    strings: StringMode,
    input_format: str,
    unit_id: str,
    data: Data,
    selected: TargetData | None,
    target_locale: str,
    source_locale: str,
    source_language: str,
    requested_target_language: str,
    explicit_domain: str,
) -> TranslationRow:
    source = data.source
    target = (data.target or "") if selected is None else (selected.text or "")
    if strings is StringMode.RAW:
        if DictField.SOURCE in fields:
            source = _raw_source(data, input_format)
        if DictField.TARGET in fields:
            target = _raw_target(data, selected, target, input_format)
    row: TranslationRow = {}
    for field in fields:
        if field is DictField.SOURCE_LANGUAGE:
            value = source_language
        elif field is DictField.TARGET_LANGUAGE:
            value = _base_language(target_locale) or _base_language(requested_target_language)
        elif field is DictField.SOURCE:
            value = source
        elif field is DictField.TARGET:
            value = target
        elif field is DictField.DOMAIN:
            value = _domain(data, explicit_domain)
        elif field is DictField.UNIT_ID:
            value = unit_id
        elif field is DictField.SOURCE_LOCALE:
            value = source_locale
        elif field is DictField.TARGET_LOCALE:
            value = target_locale
        elif field is DictField.STATUS:
            value = (
                data.status.value
                if selected is None or selected.status is TranslationStatus.UNKNOWN
                else selected.status.value
            )
        elif field is DictField.RESOURCE:
            value = data.extensions.get("resource", "")
        else:
            value = _project(data, selected)
        row[field.value] = value
    return row


def _raw_source(data: Data, input_format: str) -> str:
    tags = data.tags
    if tags is None or not tags.source_tag_map:
        return data.source
    return _render_native_fragment(
        data.source,
        tags.source_parts,
        tags.source_tag_map,
        input_format,
    )


def _raw_target(data: Data, selected: TargetData | None, target: str, input_format: str) -> str:
    if selected is not None:
        target_tags = selected.tags
        if target_tags is None or not target_tags.tag_map:
            return target
        return _render_target_fragment(target, target_tags, input_format)
    tags = data.tags
    if tags is None or not tags.target_tag_map:
        return target
    return _render_native_fragment(target, tags.target_parts, tags.target_tag_map, input_format)


def _render_target_fragment(text: str, tags: TargetTags, input_format: str) -> str:
    return _render_native_fragment(text, tags.parts, tags.tag_map, input_format)


def _render_native_fragment(
    text: str,
    parts: Sequence[SegmentPart],
    tag_map: Mapping[str, TieData],
    input_format: str,
) -> str:
    if not parts or not legacy_parts_match_text(text, parts, tag_map):
        raise TagIntegrityError("raw strings require current inline-code parts")
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(escape(part.value, quote=False))
            continue
        if not isinstance(part, CodePart):
            raise TagIntegrityError("raw strings contain an unsupported segment part")
        code = tag_map.get(part.ref)
        if code is None:
            raise TagIntegrityError(f"raw string contains dangling inline code {part.ref!r}")
        chunks.append(_render_native_code(code, input_format))
    return "".join(chunks)


def _render_native_code(code: TieData, input_format: str) -> str:
    name = code.original_name or ""
    if not name:
        if code.original_text is not None:
            return code.original_text
        raise TagIntegrityError(f"inline code {code.id!r} has no native tag name")
    namespaces = _xml_namespaces(code.attribute_data)
    structural = _is_structural_container(code, input_format, namespaces)
    if structural and code.type.value.endswith(".close"):
        return f"</{name}>"
    attributes = _render_attributes(code.attributes, namespaces)
    if structural:
        return f"<{name}{attributes}>"
    if input_format == "html":
        if code.type.value.endswith(".close"):
            return f"</{name}>"
        if code.type.value.endswith(".open"):
            return f"<{name}{attributes}>"
    payload = code.original_text
    if payload is None:
        return f"<{name}{attributes}/>"
    return f"<{name}{attributes}>{escape(payload, quote=False)}</{name}>"


def _is_structural_container(code: TieData, input_format: str, namespaces: Mapping[str, str]) -> bool:
    name = code.original_name or ""
    local = name.rsplit(":", 1)[-1]
    if input_format == "tmx":
        return local in {"hi", "sub"}
    if input_format == "xliff":
        if local in {"g", "mrk", "sub", "pc"}:
            return True
        if not code.type.value.endswith((".open", ".close")):
            return False
        prefix, separator, _ = name.partition(":")
        if separator:
            namespace = namespaces.get(prefix, "")
            if namespace and not namespace.startswith("urn:oasis:names:tc:xliff:document:"):
                return True
        return local not in {"bpt", "bx", "cp", "ec", "em", "ept", "ex", "it", "ph", "sc", "sm", "ut", "x"}
    return local in {"g", "hi", "mrk", "pc", "sub"}


def _render_attributes(attributes: Mapping[str, str], namespaces: Mapping[str, str]) -> str:
    return "".join(
        f' {_attribute_name(name, namespaces)}="{escape(value, quote=True)}"'
        for name, value in attributes.items()
        if not name.startswith("lokit.placeholder.")
    )


def _attribute_name(name: str, namespaces: Mapping[str, str]) -> str:
    if name.startswith("{http://www.w3.org/XML/1998/namespace}"):
        return f"xml:{name.rsplit('}', 1)[-1]}"
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].split("}", 1)
        for prefix, value in namespaces.items():
            if value == namespace:
                return f"{prefix}:{local}"
        return local
    return name


def _xml_namespaces(attribute_data: str) -> dict[str, str]:
    prefix = "lokit:xml-namespaces\n"
    if not attribute_data.startswith(prefix):
        return {}
    namespaces: dict[str, str] = {}
    for entry in attribute_data[len(prefix) :].splitlines():
        name, separator, value = entry.partition("=")
        if separator and name and value:
            namespaces[name] = value
    return namespaces


def _domain(data: Data, explicit_domain: str) -> str:
    if explicit_domain:
        return explicit_domain
    return data.extensions.get("domain", data.extensions.get("property.domain", ""))


def _project(data: Data, selected: TargetData | None) -> str:
    if selected is not None:
        value = selected.extensions.get("project")
        if value:
            return value
        for comment in selected.comments:
            if comment.origin is not None and comment.origin.project:
                return comment.origin.project
    value = data.extensions.get("project")
    if value:
        return value
    for comment in data.comments:
        if comment.origin is not None and comment.origin.project:
            return comment.origin.project
    return ""


def _matches_language(locale: str, requested: str) -> bool:
    if not requested:
        return True
    normalized_locale = _normalize_locale(locale)
    normalized_requested = _normalize_locale(requested)
    if "-" in normalized_requested:
        return normalized_locale == normalized_requested
    return _base_language(normalized_locale) == normalized_requested


def _normalize_locale(locale: str) -> str:
    return locale.replace("_", "-").lower()


def _base_language(locale: str) -> str:
    normalized = _normalize_locale(locale)
    return normalized.split("-", 1)[0] if normalized else ""
