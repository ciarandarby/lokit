from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import html as lxml_html

from lokit.data.structure import CodePart, Data, Meta, Tags, TextPart, TranslationStatus
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.async_bridge import AsyncExtractionBridge

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from lxml.html import HtmlElement

ExtractItem = tuple[str, Data]

_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "td",
        "th",
        "dt",
        "dd",
        "caption",
        "figcaption",
        "blockquote",
        "label",
        "option",
        "title",
    }
)

_INLINE_TAGS: frozenset[str] = frozenset(
    {
        "b",
        "i",
        "em",
        "strong",
        "a",
        "span",
        "u",
        "s",
        "small",
        "mark",
        "code",
        "sub",
        "sup",
        "abbr",
        "q",
        "cite",
        "dfn",
        "kbd",
        "samp",
        "var",
        "br",
        "img",
        "wbr",
    }
)

_SKIP_TAGS: frozenset[str] = frozenset({"script", "style"})

_STANDALONE_TAGS: frozenset[str] = frozenset({"br", "img", "wbr"})

_TAG_TYPE_MAP: dict[str, tuple[TieType, TieType | None]] = {
    "a": (TieType.A_OPEN, TieType.A_CLOSE),
    "abbr": (TieType.ABBR_OPEN, TieType.ABBR_CLOSE),
    "b": (TieType.B_OPEN, TieType.B_CLOSE),
    "bdi": (TieType.BDI_OPEN, TieType.BDI_CLOSE),
    "bdo": (TieType.BDO_OPEN, TieType.BDO_CLOSE),
    "br": (TieType.BR, None),
    "cite": (TieType.CITE_OPEN, TieType.CITE_CLOSE),
    "code": (TieType.CODE_OPEN, TieType.CODE_CLOSE),
    "dfn": (TieType.DFN_OPEN, TieType.DFN_CLOSE),
    "em": (TieType.EM_OPEN, TieType.EM_CLOSE),
    "i": (TieType.I_OPEN, TieType.I_CLOSE),
    "img": (TieType.IMG, None),
    "kbd": (TieType.KBD_OPEN, TieType.KBD_CLOSE),
    "mark": (TieType.MARK_OPEN, TieType.MARK_CLOSE),
    "q": (TieType.Q_OPEN, TieType.Q_CLOSE),
    "s": (TieType.S_OPEN, TieType.S_CLOSE),
    "samp": (TieType.SAMP_OPEN, TieType.SAMP_CLOSE),
    "small": (TieType.SMALL_OPEN, TieType.SMALL_CLOSE),
    "span": (TieType.SPAN_OPEN, TieType.SPAN_CLOSE),
    "strong": (TieType.STRONG_OPEN, TieType.STRONG_CLOSE),
    "sub": (TieType.SUB_OPEN, TieType.SUB_CLOSE),
    "sup": (TieType.SUP_OPEN, TieType.SUP_CLOSE),
    "u": (TieType.U_OPEN, TieType.U_CLOSE),
    "var": (TieType.VAR_OPEN, TieType.VAR_CLOSE),
    "wbr": (TieType.WBR, None),
}


class HtmlExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
    ) -> None:
        self.filepath = filepath
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.export_origin = ""
        self.export_timestamp = ""
        self.extensions: dict[str, str] = {"input_format": "html"}

    def extract(self) -> Iterator[ExtractItem]:
        doc = lxml_html.parse(self.filepath)
        root = doc.getroot()
        if root is None:
            return

        lang = root.get("lang")
        if lang and not self.source_locale:
            self.source_locale = lang
            self.source_language = self._base_language(lang)
        if self.source_locale and self.source_language is None:
            self.source_language = self._base_language(self.source_locale)
        if self.target_locale and self.target_language is None:
            self.target_language = self._base_language(self.target_locale)

        index = 0
        for unit_id, data in self._extract_meta(root, index):
            yield unit_id, data
            index += 1

        for unit_id, data in self._walk(root, index):
            yield unit_id, data

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)

    def _extract_meta(self, root: HtmlElement, start_index: int) -> Iterator[ExtractItem]:
        index = start_index
        head = root.find(".//head")
        if head is None:
            return
        for meta_el in head.iterfind(".//meta"):
            name = (meta_el.get("name") or "").lower()
            content = meta_el.get("content") or ""
            if name in ("description", "keywords") and content.strip():
                unit_id = f"html:meta.{name}:{index}"
                yield (
                    unit_id,
                    Data(
                        source=content.strip(),
                        meta=Meta(),
                        status=TranslationStatus.UNKNOWN,
                        extensions={"meta_name": name},
                    ),
                )
                index += 1

    def _walk(self, element: HtmlElement, start_index: int) -> Iterator[ExtractItem]:
        index = start_index
        for child in element.iter():
            tag = self._tag_name(child)
            if tag in _SKIP_TAGS:
                continue

            if tag in _BLOCK_TAGS:
                result = self._extract_block(child, index)
                if result is not None:
                    yield result
                    index += 1

            if tag == "img":
                alt = child.get("alt")
                if alt and alt.strip():
                    unit_id = f"html:img.alt:{index}"
                    yield (
                        unit_id,
                        Data(
                            source=alt.strip(),
                            meta=Meta(),
                            status=TranslationStatus.UNKNOWN,
                        ),
                    )
                    index += 1

    def _extract_block(self, element: HtmlElement, index: int) -> ExtractItem | None:
        tag = self._tag_name(element)
        has_inline = self._has_inline_children(element)

        if has_inline:
            return self._extract_with_tags(element, tag, index)

        text = self._get_direct_text(element)
        if not text:
            return None

        unit_id = f"html:{tag}:{index}"
        return unit_id, Data(
            source=text,
            meta=Meta(),
            status=TranslationStatus.UNKNOWN,
        )

    def _has_inline_children(self, element: HtmlElement) -> bool:
        return any(self._tag_name(child) in _INLINE_TAGS for child in element)

    def _extract_with_tags(self, element: HtmlElement, tag: str, index: int) -> ExtractItem | None:
        parts: list[TextPart | CodePart] = []
        tag_map: dict[str, TieData] = {}
        tag_order = 0
        pair_counter = 0

        full_text = self._build_parts(element, parts, tag_map, tag_order, pair_counter)
        if not full_text.strip():
            return None

        unit_id = f"html:{tag}:{index}"
        tags = Tags(
            source_tag_map=tag_map,
            target_tag_map={},
            source_parts=parts,
            target_parts=[],
        )
        return unit_id, Data(
            source=full_text.strip(),
            tags=tags,
            meta=Meta(),
            status=TranslationStatus.UNKNOWN,
        )

    def _build_parts(
        self,
        element: HtmlElement,
        parts: list[TextPart | CodePart],
        tag_map: dict[str, TieData],
        tag_order: int,
        pair_counter: int,
    ) -> str:
        full_text = ""

        text = element.text or ""
        if text:
            parts.append(TextPart(value=text))
            full_text += text

        for child in element:
            child_tag = self._tag_name(child)
            if child_tag not in _INLINE_TAGS:
                continue

            if child_tag in _STANDALONE_TAGS:
                ref_id = f"t{tag_order}"
                type_info = _TAG_TYPE_MAP.get(child_tag)
                tie_type = type_info[0] if type_info else TieType.CUSTOM_STANDALONE
                attrs = dict(child.attrib)
                tag_map[ref_id] = TieData(
                    id=ref_id,
                    type=tie_type,
                    attributes=attrs,
                    position=tag_order,
                    order=tag_order,
                    original_name=child_tag,
                )
                parts.append(CodePart(ref=ref_id))
                tag_order += 1
            else:
                pair_id = f"pair{pair_counter}"
                pair_counter += 1
                type_info = _TAG_TYPE_MAP.get(child_tag)

                open_id = f"t{tag_order}"
                open_type = type_info[0] if type_info else TieType.CUSTOM_OPEN
                attrs = dict(child.attrib)
                tag_map[open_id] = TieData(
                    id=open_id,
                    type=open_type,
                    attributes=attrs,
                    position=tag_order,
                    order=tag_order,
                    pair_id=pair_id,
                    original_name=child_tag,
                )
                parts.append(CodePart(ref=open_id))
                tag_order += 1

                inner_text = child.text or ""
                if inner_text:
                    parts.append(TextPart(value=inner_text))
                    full_text += inner_text

                for grandchild in child:
                    gc_tag = self._tag_name(grandchild)
                    if gc_tag in _INLINE_TAGS:
                        nested_text = self._build_parts(grandchild, parts, tag_map, tag_order, pair_counter)
                        full_text += nested_text

                close_id = f"t{tag_order}"
                close_type = type_info[1] if type_info and type_info[1] else TieType.CUSTOM_CLOSE
                tag_map[close_id] = TieData(
                    id=close_id,
                    type=close_type,
                    position=tag_order,
                    order=tag_order,
                    pair_id=pair_id,
                    original_name=child_tag,
                )
                parts.append(CodePart(ref=close_id))
                tag_order += 1

            tail = child.tail or ""
            if tail:
                parts.append(TextPart(value=tail))
                full_text += tail

        return full_text

    def _get_direct_text(self, element: HtmlElement) -> str:
        parts: list[str] = []
        if element.text:
            parts.append(element.text)
        for child in element:
            if child.tail:
                parts.append(child.tail)
        return "".join(parts).strip()

    def _tag_name(self, element: HtmlElement) -> str:
        tag = element.tag
        if isinstance(tag, str):
            return tag.lower()
        return ""

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
