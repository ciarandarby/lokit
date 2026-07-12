from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lokit.data.structure import Comment, Data, Meta, SegmentPart, Tags, TargetData, TargetTags, TranslationStatus
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.parsers.tmx.xml_utils import (
    clear_element,
    element_children,
    find_child,
    iterparse_safe,
    local_name,
)
from lokit.parsers.xliff.tags import XliffTagParser
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData

ExtractItem = tuple[str, Data]


@dataclass(slots=True)
class XliffFileContext:
    index: int
    original: str
    source_locale: str
    target_locale: str | None
    data_type: str
    tool_name: str | None = None
    tool_version: str | None = None


class XliffExtractor:
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.version = "1.2"
        self.source_locale: str | None = None
        self.target_locale: str | None = None
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.target_locales: tuple[str, ...] = ()
        self.target_languages: tuple[str, ...] = ()
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {"input_format": "xliff"}
        self.tag_parser = XliffTagParser()
        self._initialized = False

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    ) -> Iterator[ExtractItem]:
        self._initialize_from_file()
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=self._native_syntax(),
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        context = iterparse_safe(
            self.filepath,
            events=("start", "end"),
            tag=("{*}xliff", "{*}file", "{*}trans-unit", "{*}segment"),
        )
        file_stack: list[XliffFileContext] = []
        file_index = 0

        for event, elem in context:
            name = local_name(elem.tag)
            if event == "start" and name == "xliff":
                self.version = elem.attrib.get("version", "1.2")
                self.extensions["xliff_version"] = self.version
                if not self.version.startswith("1"):
                    self._set_root_languages(elem)
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
            elif (
                event == "end"
                and name == "segment"
                and file_stack
                and not self.version.startswith("1")
            ):
                yield self._parse_v2_segment(elem, file_stack[-1])
                clear_element(elem)

    def _initialize_from_file(self) -> None:
        if self._initialized:
            return
        context = iterparse_safe(
            self.filepath,
            events=("start",),
            tag=("{*}xliff", "{*}file"),
        )
        file_index = 0
        for _, elem in context:
            name = local_name(elem.tag)
            if name == "xliff":
                self.version = elem.attrib.get("version", "1.2")
                self.extensions["xliff_version"] = self.version
                if not self.version.startswith("1"):
                    self._set_root_languages(elem)
            elif name == "file":
                self._set_document_languages(self._file_context(elem, file_index))
                self._initialized = True
                return
        self._initialized = True

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
            )
        )

    def _file_context(self, element: _Element, index: int) -> XliffFileContext:
        original = element.attrib.get("original") or element.attrib.get("id") or ""
        source_locale = element.attrib.get("source-language") or self.source_locale or ""
        target_locale = element.attrib.get("target-language") or self.target_locale
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
        if context.target_locale and context.target_locale not in self.target_locales:
            self.target_locales = (*self.target_locales, context.target_locale)
            self.target_languages = (*self.target_languages, self._base_language(context.target_locale))
        if len(self.target_locales) > 1:
            self.target_locale = None
            self.target_language = None

    def _parse_unit(
        self,
        element: _Element,
        file_context: XliffFileContext,
    ) -> ExtractItem:
        source = find_child(element, "source")
        target = find_child(element, "target")
        source_text, source_tags, source_parts = self._parse_segment(source)
        target_text, target_tags, target_parts = self._parse_segment(target)
        status = self._status(target)
        unit_id = element.attrib.get("id", "")
        stable_id = unit_id or f"{file_context.index}"
        tags: Tags | None = None
        if source_tags or target_tags:
            tags = Tags(
                source_tag_map=source_tags,
                target_tag_map=target_tags,
                source_parts=source_parts,
                target_parts=target_parts,
            )
        targets: dict[str, TargetData] = {}
        if file_context.target_locale is not None and target is not None:
            targets[file_context.target_locale] = TargetData(
                text=target_text,
                status=status,
                tags=TargetTags(tag_map=target_tags, parts=target_parts) if target_tags or target_parts else None,
            )
        data = Data(
            source=source_text,
            target=None if targets else (target_text if target is not None else None),
            targets=targets,
            tags=tags,
            meta=Meta(),
            status=status,
            comments=self._comments(element),
            extensions=self._extensions(element, file_context, unit_id),
        )
        return stable_id, data

    def _parse_v2_segment(
        self,
        element: _Element,
        file_context: XliffFileContext,
    ) -> ExtractItem:
        source = find_child(element, "source")
        target = find_child(element, "target")
        source_text, source_tags, source_parts = self._parse_segment(source)
        target_text, target_tags, target_parts = self._parse_segment(target)
        status = self._status(target)
        unit = element.getparent()
        while unit is not None and local_name(unit.tag) != "unit":
            unit = unit.getparent()
        unit_id = unit.attrib.get("id", "") if unit is not None else ""
        segment_id = element.attrib.get("id", "")
        stable_id = unit_id
        if segment_id:
            stable_id = f"{unit_id}:{segment_id}" if unit_id else segment_id
        if not stable_id:
            stable_id = f"{file_context.index}"
        tags: Tags | None = None
        if source_tags or target_tags:
            tags = Tags(
                source_tag_map=source_tags,
                target_tag_map=target_tags,
                source_parts=source_parts,
                target_parts=target_parts,
            )
        targets: dict[str, TargetData] = {}
        if file_context.target_locale is not None and target is not None:
            targets[file_context.target_locale] = TargetData(
                text=target_text,
                status=status,
                tags=TargetTags(tag_map=target_tags, parts=target_parts) if target_tags or target_parts else None,
            )
        extensions = self._extensions(element, file_context, unit_id)
        if segment_id:
            extensions["segment_id"] = segment_id
        extensions["xliff_version"] = self.version
        return stable_id, Data(
            source=source_text,
            target=None if targets else (target_text if target is not None else None),
            targets=targets,
            tags=tags,
            meta=Meta(),
            status=status,
            comments=self._comments(unit if unit is not None else element),
            extensions=extensions,
        )

    def _parse_segment(self, element: _Element | None) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        if element is None:
            return "", {}, []
        return self.tag_parser.parse_fast(element)

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

    def _set_root_languages(self, element: _Element) -> None:
        source_locale = element.attrib.get("srcLang")
        target_locale = element.attrib.get("trgLang")
        if source_locale:
            self.source_locale = source_locale
            self.source_language = self._base_language(source_locale)
        if target_locale:
            self.target_locale = target_locale
            self.target_language = self._base_language(target_locale)
            self.target_locales = (target_locale,)
            self.target_languages = (self._base_language(target_locale),)

    def _native_syntax(self) -> TagSyntax:
        if self.version.startswith("2.1"):
            return TagSyntax.XLIFF_21
        if self.version.startswith("2"):
            return TagSyntax.XLIFF_20
        return TagSyntax.XLIFF_12
