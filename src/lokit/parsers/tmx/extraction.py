from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree

from lokit.data.structure import Data, Meta, SegmentPart, Tags, TargetData, TargetTags, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.interchange import iter_native_records, open_native_reader
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.base import TmxParser
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.props import ParsedTmxProps, TmxProps
from lokit.parsers.tmx.tags import TmxTagParser
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    iterparse_safe,
    local_name,
)
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData
    from lokit.parsers.interchange import NativeReader, NativeRecord

ExtractItem = tuple[str, Data]
_ASYNC_BATCH_SIZE = 512


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

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.TMX_14,
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        native_reader = self._ensure_native_reader()
        if native_reader is not None:
            self._sync_native_metadata(native_reader)
            try:
                for record in iter_native_records(native_reader):
                    yield self._native_record(record)
            finally:
                self._sync_native_metadata(native_reader)
            return

        with open(self.filepath, "rb") as stream:
            context = iterparse_safe(
                stream,
                events=("end",),
                tag=("{*}header", "{*}tu"),
            )

            for _, elem in context:
                elem_name = local_name(elem.tag)
                if elem_name == "header":
                    self.initialize_from_header_element(elem)
                    clear_element(elem)
                    continue
                if elem_name != "tu":
                    continue

                self.initialize_from_tu_element(elem)
                yield self.extract_element(elem)

                clear_element(elem)

    def _initialize_from_file(self) -> None:
        native_reader = self._ensure_native_reader()
        if native_reader is None:
            super()._initialize_from_file()
            return
        self._sync_native_metadata(native_reader)

    def _ensure_native_reader(self) -> NativeReader | None:
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
            _, data = self.extract_element(element)
            return unit_id, data

        status = self._native_status(raw_status)
        targets = {
            locale: TargetData(text=text if text else None, status=TranslationStatus.UNKNOWN)
            for locale, text in raw_targets
        }
        return unit_id, Data(
            source=source,
            target=target,
            targets=targets,
            status=status,
            meta=Meta(),
            extensions=extensions,
        )

    def _native_status(self, value: str) -> TranslationStatus:
        try:
            return TranslationStatus(value)
        except ValueError:
            return TranslationStatus.UNKNOWN

    def extract_element(self, elem: _Element) -> tuple[str, Data]:
        unit_id: str = elem.attrib.get("tuid") or self._next_generated_unit_id()

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

        data_obj = Data(
            source=source_text,
            target=target_text if target_text else None,
            targets=targets,
            tags=tags_obj,
            status=status,
            meta=props.meta if props is not None else Meta(),
            comments=props.comments if props is not None else [],
            previous_context=(props.previous_context if props is not None else None),
            next_context=props.next_context if props is not None else None,
            extensions=props.extensions if props is not None else {},
        )

        return unit_id, data_obj

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
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
            ),
            batch_size=_ASYNC_BATCH_SIZE,
        )
