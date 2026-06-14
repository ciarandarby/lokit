from __future__ import annotations

from typing import AsyncIterator, Iterator
from lxml.etree import _Element

from lokit.data.structure import Data, Meta, SegmentPart, Tags, TargetData, TargetTags, TranslationStatus
from lokit.data.tag_types import TieData
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.tmx.base import TmxParser
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.props import ParsedTmxProps, TmxProps
from lokit.parsers.tmx.tags import TmxTagParser
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    is_tag,
    iterparse_safe,
    local_name,
)

ExtractItem = tuple[str, Data]


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

    def extract(self) -> Iterator[tuple[str, Data]]:
        with open(self.filepath, "rb") as stream:
            context = iterparse_safe(stream, events=("end",))

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
            if is_tag(child, "prop"):
                if self.mode is TmxParseMode.FULL:
                    needs_full_props = True
                elif status_values is not None:
                    prop_type = child.attrib.get("type", "").lower()
                    if self.prop_parser.is_status_prop(prop_type):
                        status_values.append((child.text or "").strip().lower())
                continue
            if is_tag(child, "note"):
                if self.mode is TmxParseMode.FULL:
                    needs_full_props = True
                continue
            if not is_tag(child, "tuv"):
                continue
            lang: str = child.get(f"{self.namespace}lang") or child.get("lang") or ""
            seg: _Element | None = None
            for tuv_child in child:
                if is_tag(tuv_child, "seg"):
                    seg = tuv_child
                    break

            if seg is not None:
                text, tags, parts = self.tag_parser.parse_fast(seg)

                locale = self._canonical_locale(lang) if lang else ""
                if self._is_source_locale(locale):
                    source_text = text
                    source_tags = tags
                    source_parts = parts
                elif self._requested_target_language:
                    if not self._is_requested_target_locale(locale):
                        continue
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

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)
