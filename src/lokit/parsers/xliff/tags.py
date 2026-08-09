from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import CodePart, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.tmx.xml_utils import element_children, local_name, qualified_name, xml_namespace_data

if TYPE_CHECKING:
    from lxml.etree import _Element


_STRUCTURAL_ELEMENTS = frozenset({"g", "mrk", "pc", "sub"})
_ATOMIC_ELEMENTS = frozenset({"bpt", "bx", "cp", "ec", "em", "ept", "ex", "it", "ph", "sc", "sm", "ut", "x"})
_XLIFF_NAMESPACE_PREFIX = "urn:oasis:names:tc:xliff:document:"


class XliffTagParser:
    def parse_fast(self, element: _Element) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        if len(element) == 0:
            # Plain segments do not need a legacy part projection.  Keeping a
            # TextPart here made every tag-free XLIFF target allocate a
            # TargetTags object even though there was no inline code to retain.
            return element.text or "", {}, []
        return self.parse(element)

    def parse(self, element: _Element) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        text_chunks: list[str] = []
        text_length = 0
        tag_map: dict[str, TieData] = {}
        parts: list[SegmentPart] = []
        pair_ids: dict[str, str] = {}
        order = 0

        text_length, order = self._append_content(
            element,
            text_chunks,
            text_length,
            parts,
            tag_map,
            pair_ids,
            order,
            include_element_code=False,
        )
        return "".join(text_chunks), tag_map, parts

    def _append_content(
        self,
        element: _Element,
        text_chunks: list[str],
        text_length: int,
        parts: list[SegmentPart],
        tag_map: dict[str, TieData],
        pair_ids: dict[str, str],
        order: int,
        include_element_code: bool,
    ) -> tuple[int, int]:
        if include_element_code:
            open_id = f"c{order}"
            pair_id = self._pair_id(element, pair_ids)
            tag_map[open_id] = self._tie_data(
                element,
                open_id,
                self._open_type(element),
                text_length,
                order,
                pair_id,
            )
            parts.append(CodePart(open_id))
            order += 1

        if element.text:
            text_chunks.append(element.text)
            text_length += len(element.text)
            parts.append(TextPart(element.text))

        for child in element_children(element):
            if self._is_container(child):
                text_length, order = self._append_content(
                    child,
                    text_chunks,
                    text_length,
                    parts,
                    tag_map,
                    pair_ids,
                    order,
                    include_element_code=True,
                )
            else:
                code_id = f"c{order}"
                tag_map[code_id] = self._tie_data(
                    child,
                    code_id,
                    self._inline_type(child),
                    text_length,
                    order,
                    self._pair_id(child, pair_ids),
                )
                parts.append(CodePart(code_id))
                order += 1

            if child.tail:
                text_chunks.append(child.tail)
                text_length += len(child.tail)
                parts.append(TextPart(child.tail))

        if include_element_code:
            close_id = f"c{order}"
            tag_map[close_id] = self._tie_data(
                element,
                close_id,
                self._close_type(element),
                text_length,
                order,
                self._pair_id(element, pair_ids),
            )
            parts.append(CodePart(close_id))
            order += 1

        return text_length, order

    def _is_container(self, element: _Element) -> bool:
        name = local_name(element.tag)
        if name in _STRUCTURAL_ELEMENTS:
            return True
        namespace = self._namespace(element.tag)
        has_content = element.text is not None or len(element) > 0
        if namespace and not namespace.startswith(_XLIFF_NAMESPACE_PREFIX):
            return has_content
        return name not in _ATOMIC_ELEMENTS and has_content

    def _namespace(self, tag: object) -> str:
        if not isinstance(tag, str) or not tag.startswith("{"):
            return ""
        namespace, separator, _ = tag[1:].partition("}")
        return namespace if separator else ""

    def _pair_id(self, element: _Element, pair_ids: dict[str, str]) -> str | None:
        source_id = (
            element.attrib.get("rid")
            or element.attrib.get("id")
            or element.attrib.get("xid")
            or element.attrib.get("ctype")
        )
        if source_id is None:
            return None
        existing = pair_ids.get(source_id)
        if existing is not None:
            return existing
        normalized = f"p{len(pair_ids)}"
        pair_ids[source_id] = normalized
        return normalized

    def _inline_type(self, element: _Element) -> TieType:
        name = local_name(element.tag)
        if name in ("bpt", "bx", "sc"):
            return TieType.CUSTOM_OPEN
        if name in ("ept", "ex", "ec"):
            return TieType.CUSTOM_CLOSE
        return TieType.CUSTOM_STANDALONE

    def _open_type(self, element: _Element) -> TieType:
        return self._semantic_type(element, True)

    def _close_type(self, element: _Element) -> TieType:
        return self._semantic_type(element, False)

    def _semantic_type(self, element: _Element, is_open: bool) -> TieType:
        hint = (element.attrib.get("ctype") or element.attrib.get("type") or "").lower()
        if hint in {"bold", "b", "x-bold", "fmt:bold"}:
            return TieType.B_OPEN if is_open else TieType.B_CLOSE
        if hint in {"italic", "i", "x-italic", "fmt:italic"}:
            return TieType.I_OPEN if is_open else TieType.I_CLOSE
        if hint in {"emphasis", "em", "fmt:emphasis"}:
            return TieType.EM_OPEN if is_open else TieType.EM_CLOSE
        if hint in {"strong", "fmt:strong"}:
            return TieType.STRONG_OPEN if is_open else TieType.STRONG_CLOSE
        return TieType.CUSTOM_OPEN if is_open else TieType.CUSTOM_CLOSE

    def _tie_data(
        self,
        element: _Element,
        code_id: str,
        tie_type: TieType,
        position: int,
        order: int,
        pair_id: str | None,
    ) -> TieData:
        return TieData(
            id=code_id,
            type=tie_type,
            attributes=self._qualified_attributes(element),
            attribute_data=xml_namespace_data(element),
            position=position,
            order=order,
            pair_id=pair_id,
            original_name=qualified_name(element),
            original_text=element.text,
        )

    def _qualified_attributes(self, element: _Element) -> dict[str, str]:
        attributes: dict[str, str] = {}
        for index, (raw_name, raw_value) in enumerate(element.attrib.items(), start=1):
            fallback = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else raw_name
            name = self._qualified_attribute_name(element, fallback, index)
            attributes[name] = str(raw_value)
        return attributes

    def _qualified_attribute_name(self, element: _Element, name: str, index: int) -> str:
        if not name.startswith("{") or "}" not in name:
            return name
        namespace, local = name[1:].split("}", 1)
        if namespace == "http://www.w3.org/XML/1998/namespace":
            return f"xml:{local}"
        matched_prefix = ""
        match_count = 0
        for prefix, value in element.nsmap.items():
            if prefix is not None and value == namespace:
                matched_prefix = prefix
                match_count += 1
        if match_count == 1:
            return f"{matched_prefix}:{local}"
        if match_count > 1:
            xpath_name: object = element.xpath(f"name(@*[{index}])")
            if isinstance(xpath_name, str) and xpath_name:
                return xpath_name
        return local
