from lxml.etree import _Element

from lokit.data.structure import CodePart, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.tmx.helpers import TMX_TAG_MAP
from lokit.parsers.tmx.xml_utils import element_children, local_name


class TmxTagParser:
    def __init__(self) -> None:
        pass

    def parse(
        self, element: _Element
    ) -> tuple[str, dict[str, TieData], list[SegmentPart]]:
        raw_txt: str = ""
        tag_map: dict[str, TieData] = {}
        parts: list[SegmentPart] = []
        order: int = 0
        pair_ids: dict[str, str] = {}

        if element.text:
            raw_txt += element.text
            parts.append(TextPart(element.text))

        for child in element_children(element):
            tag_name: str = local_name(child.tag)
            tie_type: TieType = TMX_TAG_MAP.get(tag_name, TieType.CUSTOM_STANDALONE)
            source_pair_id: str | None = child.attrib.get("i") or child.attrib.get("id")
            pair_id = self._normalize_pair_id(source_pair_id, pair_ids)
            tie_id = f"c{order}"

            tag_map[tie_id] = TieData(
                id=tie_id,
                type=tie_type,
                position=len(raw_txt),
                order=order,
                pair_id=pair_id,
            )
            parts.append(CodePart(tie_id))
            order += 1

            if child.tail:
                raw_txt += child.tail
                parts.append(TextPart(child.tail))

        return raw_txt, tag_map, parts

    def _normalize_pair_id(
        self, source_pair_id: str | None, pair_ids: dict[str, str]
    ) -> str | None:
        if source_pair_id is None:
            return None
        existing = pair_ids.get(source_pair_id)
        if existing is not None:
            return existing
        normalized = f"p{len(pair_ids)}"
        pair_ids[source_pair_id] = normalized
        return normalized
