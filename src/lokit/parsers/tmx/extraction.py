from __future__ import annotations

from typing import AsyncIterator, Iterator, Optional
from uuid import uuid4

from lxml.etree import _Element

from lokit.data.structure import Data, Meta, SegmentPart, Tags, TranslationStatus
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
        source_language: Optional[str] = None,
        target_language: Optional[str] = None,
        domain: Optional[str] = None,
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

    def extract(self) -> Iterator[tuple[str, Data]]:
        with open(self.filepath, "rb") as stream:
            context = iterparse_safe(stream, events=("end",))

            for _, elem in context:
                if local_name(elem.tag) != "tu":
                    continue

                yield self.extract_element(elem)

                clear_element(elem)

    def extract_element(self, elem: _Element) -> tuple[str, Data]:
        unit_id: str = elem.attrib.get("tuid") or str(uuid4())

        props: ParsedTmxProps | None = None
        status = TranslationStatus.UNKNOWN
        if self.mode is TmxParseMode.FULL:
            props = self.prop_parser.parse_all(elem)
            status = props.status
        elif self.mode is TmxParseMode.TEXT_WITH_STATUS:
            status = self.prop_parser.parse_status(elem)

        source_text: str = ""
        target_text: str = ""
        source_tags: dict[str, TieData] | None = None
        target_tags: dict[str, TieData] | None = None
        source_parts: list[SegmentPart] | None = None
        target_parts: list[SegmentPart] | None = None

        for tuv in elem:
            if not is_tag(tuv, "tuv"):
                continue
            lang: str = tuv.get(f"{self.namespace}lang") or tuv.get("lang") or ""
            seg: _Element | None = None
            for tuv_child in tuv:
                if is_tag(tuv_child, "seg"):
                    seg = tuv_child
                    break

            if seg is not None:
                text, tags, parts = self.tag_parser.parse_fast(seg)

                if self._cached_base_lang(lang) == self.native_source_base:
                    source_text = text
                    source_tags = tags
                    source_parts = parts
                else:
                    target_text = text
                    target_tags = tags
                    target_parts = parts

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
            plural=None,
            tags=tags_obj,
            meta=props.meta if props is not None else Meta(),
            status=status,
            comments=props.comments if props is not None else [],
            previous_context=(props.previous_context if props is not None else None),
            next_context=props.next_context if props is not None else None,
            extensions=props.extensions if props is not None else {},
        )

        return unit_id, data_obj

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)
