from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional

from lxml.etree import _Element

from lokit.data.structure import Comment, Data, Meta, SegmentPart, Tags, TranslationStatus
from lokit.data.tag_types import TieData
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    element_children,
    find_child,
    iterparse_safe,
    local_name,
)
from lokit.parsers.xliff.tags import XliffTagParser

ExtractItem = tuple[str, Data]


@dataclass(slots=True)
class XliffFileContext:
    index: int
    original: str
    source_locale: str
    target_locale: Optional[str]
    data_type: str
    tool_name: Optional[str] = None
    tool_version: Optional[str] = None


class XliffExtractor:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.version = "1.2"
        self.source_locale: str | None = None
        self.target_locale: str | None = None
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {"input_format": "xliff"}
        self.tag_parser = XliffTagParser()

    def extract(self) -> Iterator[ExtractItem]:
        context = iterparse_safe(self.filepath, events=("start", "end"))
        file_stack: list[XliffFileContext] = []
        file_index = 0

        for event, elem in context:
            name = local_name(elem.tag)
            if event == "start" and name == "xliff":
                self.version = elem.attrib.get("version", "1.2")
                self.extensions["xliff_version"] = self.version
            elif event == "start" and name == "file":
                current = self._file_context(elem, file_index)
                file_index += 1
                file_stack.append(current)
                self._set_document_languages(current)
            elif event == "end" and name == "file":
                if file_stack:
                    file_stack.pop()
                clear_element(elem)
            elif event == "end" and name == "trans-unit" and file_stack:
                current_file = file_stack[-1]
                yield self._parse_unit(elem, current_file)
                clear_element(elem)

    def _initialize_from_file(self) -> None:
        context = iterparse_safe(self.filepath, events=("start",))
        file_index = 0
        for _, elem in context:
            name = local_name(elem.tag)
            if name == "xliff":
                self.version = elem.attrib.get("version", "1.2")
                self.extensions["xliff_version"] = self.version
            elif name == "file":
                self._set_document_languages(self._file_context(elem, file_index))
                return

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)

    def _file_context(self, element: _Element, index: int) -> XliffFileContext:
        original = element.attrib.get("original", "")
        source_locale = element.attrib.get("source-language", "")
        target_locale = element.attrib.get("target-language")
        data_type = element.attrib.get("datatype", "")
        return XliffFileContext(
            index=index,
            original=original,
            source_locale=source_locale,
            target_locale=target_locale,
            data_type=data_type,
        )

    def _set_document_languages(self, context: XliffFileContext) -> None:
        if self.source_locale is None and context.source_locale:
            self.source_locale = context.source_locale
            self.source_language = self._base_language(context.source_locale)
        if self.target_locale is None and context.target_locale:
            self.target_locale = context.target_locale
            self.target_language = self._base_language(context.target_locale)

    def _parse_unit(
        self,
        element: _Element,
        file_context: XliffFileContext,
    ) -> ExtractItem:
        source = find_child(element, "source")
        target = find_child(element, "target")
        source_text, source_tags, source_parts = self._parse_segment(source)
        target_text, target_tags, target_parts = self._parse_segment(target)
        unit_id = element.attrib.get("id", "")
        stable_id = f"{file_context.index}:{unit_id}" if unit_id else f"{file_context.index}"
        tags = Tags(
            source_tag_map=source_tags,
            target_tag_map=target_tags,
            source_parts=source_parts,
            target_parts=target_parts,
        )
        data = Data(
            source=source_text,
            target=target_text if target is not None else None,
            tags=tags if source_tags or target_tags else None,
            meta=Meta(),
            status=self._status(target),
            comments=self._comments(element),
            extensions=self._extensions(element, file_context, unit_id),
        )
        return stable_id, data

    def _parse_segment(
        self, element: _Element | None
    ) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        if element is None:
            return "", {}, []
        return self.tag_parser.parse(element)

    def _status(self, target: _Element | None) -> TranslationStatus:
        if target is None:
            return TranslationStatus.NEW
        state = (target.attrib.get("state") or "").lower()
        if state in ("final", "signed-off"):
            return TranslationStatus.APPROVED
        if state in ("translated", "needs-review-translation"):
            return TranslationStatus.TRANSLATED
        if state in ("needs-review-adaptation", "needs-review-l10n"):
            return TranslationStatus.REVIEWED
        if state in ("new", "needs-translation"):
            return TranslationStatus.NEW
        return TranslationStatus.UNKNOWN

    def _comments(self, element: _Element) -> list[Comment]:
        comments: list[Comment] = []
        for child in element_children(element, "note"):
            if child.text:
                comments.append(Comment(context=child.text.strip()))
        return comments

    def _extensions(
        self,
        element: _Element,
        file_context: XliffFileContext,
        unit_id: str,
    ) -> dict[str, str]:
        extensions = {
            "resource": file_context.original,
            "resource_index": str(file_context.index),
            "unit_id": unit_id,
        }
        if file_context.data_type:
            extensions["data_type"] = file_context.data_type
        xml_space = element.attrib.get("{http://www.w3.org/XML/1998/namespace}space")
        if xml_space:
            extensions["space"] = xml_space
        return extensions

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
