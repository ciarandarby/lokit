from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree

from lokit.data.structure import (
    Comment,
    Data,
    Meta,
    Plural,
    SegmentPart,
    Tags,
    TargetData,
    TargetTags,
    TranslationStatus,
)
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.interchange import iter_native_records, open_native_reader
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.base import TmxParser
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.props import ParsedTmxProps, TmxProps
from lokit.parsers.tmx.tags import TmxTagParser
from lokit.parsers.tmx.xml_utils import local_name
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData
    from lokit.parsers.interchange import NativeReader, NativeRecord
    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
_ASYNC_BATCH_SIZE = 512
_NATIVE_STATUSES = {status.value: status for status in TranslationStatus}


class TmxExtractor(TmxParser):
    def __init__(
        self,
        filepath: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        parse_header: bool = True,
        mode: TmxParseMode = TmxParseMode.FULL,
    ) -> None:
        super().__init__(
            tmx_file_path=filepath,
            source_language=source_language,
            target_language=target_language,
            domain=domain,
            parse_header=parse_header,
        )
        self.tag_parser: TmxTagParser = TmxTagParser()
        self.prop_parser: TmxProps = TmxProps()
        self.namespace: str = "{http://www.w3.org/XML/1998/namespace}"
        self.mode = mode
        self._generated_id: int = 0
        self._native_reader: NativeReader | None = None
        self._gettext_plural_roots: dict[str, tuple[str, Plural, dict[str, str]]] = {}

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.TMX_14,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)
        try:
            for record in iter_native_records(native_reader):
                yield self._native_record(record)
        finally:
            self._sync_native_metadata(native_reader)

    def _initialize_from_file(self) -> None:
        native_reader = self._ensure_native_reader()
        self._sync_native_metadata(native_reader)

    def _ensure_native_reader(self) -> NativeReader:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            return reader
        reader = open_native_reader(
            self.filepath,
            "tmx",
            self.native_source or None,
            self.native_target or None,
            self.mode.value,
        )
        self._native_reader = reader
        return reader

    def close(self) -> None:
        reader = self._native_reader
        if reader is not None and not reader.closed:
            reader.close()

    def _sync_native_metadata(self, reader: NativeReader) -> None:
        if reader.source_locale is not None:
            self.source_locale = reader.source_locale
            self.native_source = reader.source_locale
        if reader.source_language is not None:
            self.source_language = reader.source_language
        if reader.target_locale is not None:
            self.target_locale = reader.target_locale
            self.native_target = reader.target_locale
        if reader.target_language is not None:
            self.target_language = reader.target_language
        self.target_locales = tuple(reader.target_locales)
        self.target_languages = tuple(reader.target_languages)
        if self._parse_header:
            self.export_origin = reader.export_origin
            self.export_timestamp = reader.export_timestamp
            self.extensions.update(reader.extensions)
            self._normalize_po_header_extensions()
        self.native_source_base = self._base_lang(self.native_source)
        self.native_target_base = self._base_lang(self.native_target)
        self._header_initialized = True

    def _native_record(self, record: NativeRecord) -> ExtractItem:
        is_complex, unit_id, source, target, raw_targets, raw_status, extensions, fragment = record
        if is_complex:
            if fragment is None:
                raise ValueError("Native TMX parser returned a complex unit without XML")
            parser = etree.XMLParser(no_network=True, resolve_entities=False)
            element = etree.fromstring(fragment, parser)
            if local_name(element.tag) != "tu":
                unit_element = next(
                    (child for child in element if local_name(child.tag) == "tu"),
                    None,
                )
                if unit_element is None:
                    raise ValueError("Native TMX parser returned an invalid unit fragment")
                element = unit_element
            _, data = self.extract_element(element)
            return unit_id, data

        status = self._native_status(raw_status)
        extensions.setdefault("unit_id", unit_id)
        targets = {
            locale: TargetData(text=text if text else None, status=TranslationStatus.UNKNOWN)
            for locale, text in raw_targets
        }
        if self.domain:
            extensions["domain"] = self.domain
        return unit_id, Data(
            source=source,
            target=target,
            targets=targets,
            status=status,
            meta=Meta(),
            extensions=extensions,
        )

    def _native_status(self, value: str) -> TranslationStatus:
        return _NATIVE_STATUSES.get(value, TranslationStatus.UNKNOWN)

    def extract_element(self, elem: _Element) -> tuple[str, Data]:
        raw_unit_id = elem.attrib.get("tuid", "")
        unit_id: str = raw_unit_id or self._next_generated_unit_id()

        props: ParsedTmxProps | None = None
        status = TranslationStatus.UNKNOWN
        source_text: str = ""
        target_text: str = ""
        source_tags: dict[str, TieData] | None = None
        target_tags: dict[str, TieData] | None = None
        source_parts: list[SegmentPart] | None = None
        target_parts: list[SegmentPart] | None = None
        targets: dict[str, TargetData] = {}
        needs_full_props = self.mode is TmxParseMode.FULL and self._has_metadata_attrs(elem)
        status_values: list[str] | None = [] if self.mode is TmxParseMode.TEXT_WITH_STATUS else None

        for child in elem:
            child_name = local_name(child.tag)
            if child_name == "prop":
                if self.mode is TmxParseMode.FULL:
                    needs_full_props = True
                elif status_values is not None:
                    prop_type = child.attrib.get("type", "").lower()
                    if self.prop_parser.is_status_prop(prop_type):
                        status_values.append((child.text or "").strip().lower())
                continue
            if child_name == "note":
                if self.mode is TmxParseMode.FULL:
                    needs_full_props = True
                continue
            if child_name != "tuv":
                continue
            lang: str = child.get(f"{self.namespace}lang") or child.get("lang") or ""
            locale = self._canonical_locale(lang) if lang else ""
            is_source = self._is_source_locale(locale)
            if self._requested_target_language and not is_source and not self._is_requested_target_locale(locale):
                continue
            seg: _Element | None = None
            for tuv_child in child:
                if local_name(tuv_child.tag) == "seg":
                    seg = tuv_child
                    break

            if seg is not None:
                text, tags, parts = self.tag_parser.parse_fast(seg)

                if is_source:
                    source_text = text
                    source_tags = tags
                    source_parts = parts
                elif self._requested_target_language:
                    target_text = text
                    target_tags = tags
                    target_parts = parts
                elif locale:
                    self._register_target_locale(locale)
                    targets[locale] = TargetData(
                        text=text if text else None,
                        status=TranslationStatus.UNKNOWN,
                        tags=TargetTags(tag_map=tags or {}, parts=parts or []) if tags or parts else None,
                    )

        if self.mode is TmxParseMode.FULL and needs_full_props:
            props = self.prop_parser.parse_all(elem)
            status = props.status
        elif status_values is not None:
            status = self.prop_parser.status_from_values(status_values)

        tags_obj: Tags | None = None
        if source_tags is not None or target_tags is not None:
            tags_obj = Tags(
                source_tag_map=source_tags or {},
                target_tag_map=target_tags or {},
                source_parts=source_parts or [],
                target_parts=target_parts or [],
            )

        extensions = props.extensions.copy() if props is not None else {}
        extensions["unit_id"] = raw_unit_id
        plural, po_comments = self._gettext_fields(elem, unit_id, extensions)
        if self._has_fuzzy_flag(extensions):
            status = TranslationStatus.DRAFT
        comments = list(props.comments) if props is not None else []
        self._merge_po_comments(comments, po_comments)
        if self.domain:
            extensions["domain"] = self.domain
        data_obj = Data(
            source=source_text,
            target=target_text if target_text else None,
            targets=targets,
            plural=plural,
            tags=tags_obj,
            status=status,
            meta=props.meta if props is not None else Meta(),
            comments=comments,
            previous_context=(props.previous_context if props is not None else None),
            next_context=props.next_context if props is not None else None,
            extensions=extensions,
        )

        return unit_id, data_obj

    def _normalize_po_header_extensions(self) -> None:
        aliases = {
            "property.x_po_metadata_json": "po_metadata_json",
            "property.x_po_header_translator_comments": "po_header_translator_comments",
            "property.x_po_header_extracted_comments": "po_header_extracted_comments",
            "property.x_po_header_flags": "po_header_flags",
            "property.x_po_header_previous": "po_header_previous",
        }
        for source, target in aliases.items():
            value = self.extensions.get(source)
            if value is not None:
                self.extensions[target] = value

    def _gettext_fields(
        self,
        element: _Element,
        unit_id: str,
        extensions: dict[str, str],
    ) -> tuple[Plural | None, list[Comment]]:
        keys = {
            "x-po-msgid": "po_msgid",
            "x-po-msgctxt": "po_msgctxt",
            "x-po-msgid-plural": "po_msgid_plural",
            "x-po-plural-index": "gettext_index",
            "x-po-entry-index": "po_entry_index",
            "x-po-flags": "flags",
            "x-po-references": "references",
            "x-po-previous": "po_previous",
        }
        comments: list[Comment] = []
        for child in element:
            if local_name(child.tag) != "prop":
                continue
            prop_type = child.attrib.get("type", "").lower()
            value = child.text or ""
            key = keys.get(prop_type)
            if key is not None:
                if key == "po_previous" and key in extensions:
                    extensions[key] = f"{extensions[key]}\n{value}"
                else:
                    extensions[key] = value
            elif prop_type in ("x-po-translator-comment", "x-po-extracted-comment"):
                comments.append(
                    Comment(
                        context=value,
                        extensions={
                            "po_comment_kind": ("translator" if prop_type == "x-po-translator-comment" else "extracted")
                        },
                    )
                )

        plural_index = extensions.get("gettext_index")
        msgid_plural = extensions.get("po_msgid_plural")
        if plural_index is None and msgid_plural is None:
            return None, comments
        index = plural_index or "0"
        suffix = unit_id.rsplit("[", maxsplit=1)
        base_id = extensions.get("po_entry_index") or suffix[0]
        msgid = extensions.get("po_msgid", "")
        variant = msgid_plural or ""
        root = self._gettext_plural_roots.get(base_id)
        if index == "0" or root is None:
            plural = Plural(variant=variant, extensions={"gettext_index": index})
            self._gettext_plural_roots[base_id] = (msgid, plural, extensions)
        else:
            root_msgid, root_plural, root_extensions = root
            msgid = msgid or root_msgid
            if not root_plural.variant and variant:
                root_plural.variant = variant
                root_extensions["po_msgid_plural"] = variant
            plural = Plural(variant=variant, extensions={"gettext_index": index})
        extensions.setdefault("po_msgid", msgid)
        if variant:
            extensions.setdefault("po_msgid_plural", variant)
        extensions.setdefault("gettext_index", index)
        extensions.setdefault("po_entry_index", base_id)
        context_key = extensions.get("po_msgctxt")
        for comment in comments:
            if comment.extensions.get("po_comment_kind") == "extracted":
                comment.context_key = context_key
        return plural, comments

    def _merge_po_comments(self, comments: list[Comment], structured: list[Comment]) -> None:
        if not structured:
            return
        structured_values = {comment.context for comment in structured}
        comments[:] = [comment for comment in comments if comment.context not in structured_values]
        comments.extend(structured)

    def _has_fuzzy_flag(self, extensions: dict[str, str]) -> bool:
        return any(flag.strip() == "fuzzy" for flag in extensions.get("flags", "").split(","))

    def _is_source_locale(self, locale: str) -> bool:
        if not locale:
            return False
        if self.source_locale:
            return locale == self.source_locale
        return self._cached_base_lang(locale) == self.native_source_base

    def _is_requested_target_locale(self, locale: str) -> bool:
        if not locale:
            return False
        if self.target_locale:
            return locale == self.target_locale
        return self._cached_base_lang(locale) == self.native_target_base

    def _register_target_locale(self, locale: str) -> None:
        if locale in self.target_locales:
            return
        self.target_locales = (*self.target_locales, locale)
        self.target_languages = (*self.target_languages, self._base_lang(locale))
        if len(self.target_locales) == 1:
            self.target_locale = locale
            self.target_language = self._base_lang(locale)
        else:
            self.target_locale = None
            self.target_language = None

    def _has_metadata_attrs(self, elem: _Element) -> bool:
        attrs = elem.attrib
        return (
            attrs.get("changedate") is not None
            or attrs.get("creationid") is not None
            or attrs.get("creationdate") is not None
            or attrs.get("lastusagedate") is not None
            or attrs.get("changeid") is not None
            or attrs.get("usagecount") is not None
        )

    def _next_generated_unit_id(self) -> str:
        unit_id = f"auto_{self._generated_id}"
        self._generated_id += 1
        return unit_id

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            ),
            batch_size=_ASYNC_BATCH_SIZE,
        )
