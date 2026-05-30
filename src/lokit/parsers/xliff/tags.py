from __future__ import annotations

from lxml.etree import _Element

from lokit.data.structure import CodePart, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.tmx.xml_utils import element_children, local_name


class XliffTagParser:
    def parse(
        self, element: _Element
    ) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
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
            tag_map[open_id] = TieData(
                id=open_id,
                type=self._open_type(element),
                position=text_length,
                order=order,
                pair_id=pair_id,
            )
            parts.append(CodePart(open_id))
            order += 1

        if element.text:
            text_chunks.append(element.text)
            text_length += len(element.text)
            parts.append(TextPart(element.text))

        for child in element_children(element):
            child_name = local_name(child.tag)
            if child_name in ("g", "mrk", "sub"):
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
                tag_map[code_id] = TieData(
                    id=code_id,
                    type=self._inline_type(child),
                    position=text_length,
                    order=order,
                    pair_id=self._pair_id(child, pair_ids),
                )
                parts.append(CodePart(code_id))
                order += 1

            if child.tail:
                text_chunks.append(child.tail)
                text_length += len(child.tail)
                parts.append(TextPart(child.tail))

        if include_element_code:
            close_id = f"c{order}"
            tag_map[close_id] = TieData(
                id=close_id,
                type=self._close_type(element),
                position=text_length,
                order=order,
                pair_id=self._pair_id(element, pair_ids),
            )
            parts.append(CodePart(close_id))
            order += 1

        return text_length, order

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
        if name in ("bpt", "bx"):
            return TieType.CUSTOM_OPEN
        if name in ("ept", "ex"):
            return TieType.CUSTOM_CLOSE
        return TieType.CUSTOM_STANDALONE

    def _open_type(self, element: _Element) -> TieType:
        return TieType.CUSTOM_OPEN

    def _close_type(self, element: _Element) -> TieType:
        return TieType.CUSTOM_CLOSE
