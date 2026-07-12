from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import CodePart, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.tmx.helpers import TMX_TAG_MAP
from lokit.parsers.tmx.xml_utils import local_name

if TYPE_CHECKING:
    from lxml.etree import _Element


class TmxTagParser:
    def parse_fast(self, element: _Element) -> tuple[str, dict[str, TieData] | None, list[SegmentPart] | None]:
        if len(element) == 0:
            return element.text or "", None, None
        return self.parse(element)

    def parse(self, element: _Element) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        text_chunks: list[str] = []
        tag_map: dict[str, TieData] = {}
        parts: list[SegmentPart] = []
        pair_ids: dict[str, str] = {}
        self._append_content(element, text_chunks, parts, tag_map, pair_ids, 0, 0)
        return "".join(text_chunks), tag_map, parts

    def _append_content(
        self,
        element: _Element,
        text_chunks: list[str],
        parts: list[SegmentPart],
        tag_map: dict[str, TieData],
        pair_ids: dict[str, str],
        text_length: int,
        order: int,
    ) -> tuple[int, int]:
        if element.text:
            text_chunks.append(element.text)
            parts.append(TextPart(element.text))
            text_length += len(element.text)

        for child in element:
            tag_name = local_name(child.tag)
            if tag_name in {"hi", "sub"}:
                pair_id = self._container_pair_id(child, pair_ids)
                open_id = f"c{order}"
                tag_map[open_id] = self._code(
                    child,
                    open_id,
                    TieType.CUSTOM_OPEN,
                    text_length,
                    order,
                    pair_id,
                    original_text=None,
                )
                parts.append(CodePart(open_id))
                order += 1
                text_length, order = self._append_content(
                    child,
                    text_chunks,
                    parts,
                    tag_map,
                    pair_ids,
                    text_length,
                    order,
                )
                close_id = f"c{order}"
                tag_map[close_id] = self._code(
                    child,
                    close_id,
                    TieType.CUSTOM_CLOSE,
                    text_length,
                    order,
                    pair_id,
                    original_text=None,
                )
                parts.append(CodePart(close_id))
                order += 1
            else:
                tie_type = self._tie_type(child, tag_name)
                source_pair_id = child.attrib.get("i") or child.attrib.get("id")
                normalized_pair_id = self._normalize_pair_id(source_pair_id, pair_ids)
                code_id = f"c{order}"
                tag_map[code_id] = self._code(
                    child,
                    code_id,
                    tie_type,
                    text_length,
                    order,
                    normalized_pair_id,
                    original_text=child.text,
                )
                parts.append(CodePart(code_id))
                order += 1

            if child.tail:
                text_chunks.append(child.tail)
                parts.append(TextPart(child.tail))
                text_length += len(child.tail)

        return text_length, order

    def _tie_type(self, element: _Element, tag_name: str) -> TieType:
        if tag_name == "it":
            position = (element.attrib.get("pos") or "").lower()
            if position == "begin":
                return TieType.CUSTOM_OPEN
            if position == "end":
                return TieType.CUSTOM_CLOSE
        return TMX_TAG_MAP.get(tag_name, TieType.CUSTOM_STANDALONE)

    def _code(
        self,
        element: _Element,
        code_id: str,
        tie_type: TieType,
        text_length: int,
        order: int,
        pair_id: str | None,
        *,
        original_text: str | None,
    ) -> TieData:
        return TieData(
            id=code_id,
            type=tie_type,
            attributes={str(key): str(value) for key, value in element.attrib.items()},
            position=text_length,
            order=order,
            pair_id=pair_id,
            original_name=local_name(element.tag),
            original_text=original_text,
        )

    def _container_pair_id(self, element: _Element, pair_ids: dict[str, str]) -> str:
        source_pair_id = element.attrib.get("i") or element.attrib.get("id")
        if source_pair_id is None:
            generated = f"p{len(pair_ids)}"
            pair_ids[f"__generated_{len(pair_ids)}"] = generated
            return generated
        normalized = self._normalize_pair_id(source_pair_id, pair_ids)
        if normalized is None:
            raise AssertionError("A non-empty source pair id must normalize")
        return normalized

    def _normalize_pair_id(self, source_pair_id: str | None, pair_ids: dict[str, str]) -> str | None:
        if source_pair_id is None:
            return None
        existing = pair_ids.get(source_pair_id)
        if existing is not None:
            return existing
        normalized = f"p{len(pair_ids)}"
        pair_ids[source_pair_id] = normalized
        return normalized
