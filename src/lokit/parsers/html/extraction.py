from __future__ import annotations

import pickle
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from lxml import etree

from lokit.data.structure import CodePart, Data, Meta, Tags, TextPart, TranslationStatus
from lokit.data.tag_types import TieData, TieType
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.projection import project_items
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator, Sequence
    from types import TracebackType

    from lxml.etree import _Element

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
RawExtractItem = tuple[str, Data]

_BODY_ITEM = 0
_HEAD_METADATA = 1
_HEAD_ITEM = 2
_MAX_SPOOL_BYTES = 4 * 1024 * 1024 * 1024

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


class _HtmlPendingSpool(AbstractContextManager["_HtmlPendingSpool"]):
    """Private disk-backed preorder queue for nested HTML extraction.

    An enclosing block must be emitted before units found inside it, although
    its own direct text is not complete until the block's end event. The
    parser therefore records completed descendants by their start-event
    ordinal and reads them back only after the enclosing unit is known. The
    payloads are created and consumed exclusively inside a private temporary
    directory; no external or caller-provided pickle data is ever loaded.
    """

    def __init__(self) -> None:
        self._directory = TemporaryDirectory(prefix="lokit-html-import-")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                Path(self._directory.name) / "pending.sqlite3",
                check_same_thread=False,
            )
            self._connection = connection
            self._connection.execute("PRAGMA journal_mode=OFF")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute("PRAGMA cache_size=-2048")
            row = self._connection.execute("PRAGMA page_size").fetchone()
            if row is None:
                raise RuntimeError("could not determine SQLite page size")
            page_size = int(row[0])
            max_pages = max(1, _MAX_SPOOL_BYTES // page_size)
            self._connection.execute(f"PRAGMA max_page_count={max_pages}")
            self._connection.execute(
                "CREATE TABLE items ("
                "ordinal INTEGER PRIMARY KEY, category INTEGER NOT NULL, payload BLOB NOT NULL"
                ")"
            )
            self._connection.execute("CREATE INDEX items_category_ordinal ON items(category, ordinal)")
        except BaseException:
            if connection is not None:
                connection.close()
            self._directory.cleanup()
            raise

    def __enter__(self) -> _HtmlPendingSpool:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._connection.close()
        finally:
            self._directory.cleanup()

    def add(self, ordinal: int, category: int, item: RawExtractItem) -> None:
        payload = pickle.dumps(item, protocol=pickle.HIGHEST_PROTOCOL)
        self._connection.execute(
            "INSERT INTO items (ordinal, category, payload) VALUES (?, ?, ?)",
            (ordinal, category, sqlite3.Binary(payload)),
        )

    def pop_body(self, start: int, stop: int) -> Generator[RawExtractItem, None, None]:
        return self._pop(
            "SELECT payload FROM items "
            "WHERE category = ? AND ordinal >= ? AND ordinal < ? ORDER BY ordinal",
            (_BODY_ITEM, start, stop),
            "DELETE FROM items WHERE category = ? AND ordinal >= ? AND ordinal < ?",
            (_BODY_ITEM, start, stop),
        )

    def pop_head(self) -> Generator[RawExtractItem, None, None]:
        return self._pop(
            "SELECT payload FROM items WHERE category IN (?, ?) ORDER BY category, ordinal",
            (_HEAD_METADATA, _HEAD_ITEM),
            "DELETE FROM items WHERE category IN (?, ?)",
            (_HEAD_METADATA, _HEAD_ITEM),
        )

    def _pop(
        self,
        select_sql: str,
        select_parameters: tuple[int, ...],
        delete_sql: str,
        delete_parameters: tuple[int, ...],
    ) -> Generator[RawExtractItem, None, None]:
        cursor = self._connection.execute(select_sql, select_parameters)
        try:
            for row in cursor:
                row_value = cast("object", row)
                if not isinstance(row_value, tuple) or len(row_value) != 1:
                    raise RuntimeError("HTML pending spool returned an invalid row")
                yield self._deserialize(cast("object", row_value[0]))
        finally:
            cursor.close()
            self._connection.execute(delete_sql, delete_parameters)

    @staticmethod
    def _deserialize(payload: object) -> RawExtractItem:
        if isinstance(payload, memoryview):
            encoded = payload.tobytes()
        elif isinstance(payload, bytes):
            encoded = payload
        else:
            raise RuntimeError("HTML pending spool returned an invalid payload")
        value = cast("object", pickle.loads(encoded))
        if not isinstance(value, tuple) or len(value) != 2:
            raise RuntimeError("HTML pending spool returned an invalid item")
        prefix = cast("object", value[0])
        data = cast("object", value[1])
        if not isinstance(prefix, str) or not isinstance(data, Data):
            raise RuntimeError("HTML pending spool returned an invalid item")
        return prefix, data


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
        self.extensions: dict[str, str] = {
            "input_format": "html",
            "source_file": filepath,
            "source_html": filepath,
        }

    def extract(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
            runtime_placeholders=runtime_placeholders,
            inline_placeholders=inline_placeholders,
            placeholder_syntaxes=placeholder_syntaxes,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        index = 0
        event_ordinal = 0
        in_head = False
        head_emitted = False
        open_blocks: list[_Element] = []
        block_ordinals: dict[_Element, int] = {}
        block_tails: dict[_Element, list[str]] = {}
        item_ordinals: dict[_Element, int] = {}
        with _HtmlPendingSpool() as spool:
            context = etree.iterparse(
                self.filepath,
                events=("start", "end"),
                html=True,
                recover=True,
                no_network=True,
            )

            for event, element in context:
                tag = self._tag_name(element)
                if event == "start":
                    if tag == "html":
                        self._initialize_languages(element)
                    elif tag == "head":
                        in_head = True
                    elif tag == "body" and not head_emitted:
                        head = spool.pop_head()
                        try:
                            for prefix, data in head:
                                yield f"{prefix}:{index}", data
                                index += 1
                        finally:
                            head.close()
                        head_emitted = True
                    if tag in _BLOCK_TAGS:
                        block_ordinals[element] = event_ordinal
                        block_tails[element] = []
                        open_blocks.append(element)
                        event_ordinal += 1
                    elif tag == "img" or (tag == "meta" and in_head):
                        item_ordinals[element] = event_ordinal
                        event_ordinal += 1
                    continue

                ready: Generator[RawExtractItem, None, None] | None = None
                direct_item: RawExtractItem | None = None
                is_block = tag in _BLOCK_TAGS
                if is_block and open_blocks and open_blocks[-1] is element:
                    block_ancestor = open_blocks[-2] if len(open_blocks) > 1 else None
                else:
                    block_ancestor = open_blocks[-1] if open_blocks else None
                if tag == "meta" and in_head:
                    item = self._extract_meta_element(element)
                    if item is not None:
                        spool.add(item_ordinals.pop(element), _HEAD_METADATA, item)
                elif is_block:
                    ordinal = block_ordinals.pop(element)
                    released_tails = block_tails.pop(element)
                    item = self._extract_block(element, released_tails)
                    category = _HEAD_ITEM if in_head else _BODY_ITEM
                    if item is not None:
                        spool.add(ordinal, category, item)
                    if block_ancestor is None and not in_head:
                        ready = spool.pop_body(ordinal, event_ordinal)
                elif tag == "img":
                    ordinal = item_ordinals.pop(element)
                    item = self._extract_image(element)
                    if item is not None:
                        if block_ancestor is not None:
                            spool.add(ordinal, _HEAD_ITEM if in_head else _BODY_ITEM, item)
                        elif in_head:
                            spool.add(ordinal, _HEAD_ITEM, item)
                        else:
                            direct_item = item

                if tag == "head":
                    in_head = False
                    if not head_emitted:
                        ready = spool.pop_head()
                        head_emitted = True

                ancestor_tails = block_tails.get(block_ancestor) if block_ancestor is not None else None
                self._release_element(element, tag, block_ancestor, ancestor_tails)
                if is_block and open_blocks and open_blocks[-1] is element:
                    open_blocks.pop()

                if direct_item is not None:
                    prefix, data = direct_item
                    yield f"{prefix}:{index}", data
                    index += 1
                if ready is not None:
                    try:
                        for prefix, data in ready:
                            yield f"{prefix}:{index}", data
                            index += 1
                    finally:
                        ready.close()

            if not head_emitted:
                head = spool.pop_head()
                try:
                    for prefix, data in head:
                        yield f"{prefix}:{index}", data
                        index += 1
                finally:
                    head.close()

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
        runtime_placeholders: bool = True,
        inline_placeholders: bool = True,
        placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
                runtime_placeholders=runtime_placeholders,
                inline_placeholders=inline_placeholders,
                placeholder_syntaxes=placeholder_syntaxes,
            )
        )

    def _extract_block(self, element: _Element, released_tails: Sequence[str]) -> RawExtractItem | None:
        tag = self._tag_name(element)
        has_inline = self._has_inline_children(element)

        if has_inline:
            return self._extract_with_tags(element, tag)

        text = self._get_direct_text(element, released_tails)
        if not text:
            return None

        return f"html:{tag}", Data(
            source=text,
            meta=Meta(),
            status=TranslationStatus.UNKNOWN,
        )

    def _has_inline_children(self, element: _Element) -> bool:
        for child in element:  # noqa: SIM110 - avoids a generator allocation on every block.
            if self._tag_name(child) in _INLINE_TAGS:
                return True
        return False

    def _extract_with_tags(self, element: _Element, tag: str) -> RawExtractItem | None:
        parts: list[TextPart | CodePart] = []
        tag_map: dict[str, TieData] = {}
        self._build_parts(element, parts, tag_map, 0, 0)
        self._trim_parts(parts)
        full_text = "".join(part.value for part in parts if isinstance(part, TextPart))
        if not full_text.strip():
            return None

        tags = Tags(
            source_tag_map=tag_map,
            target_tag_map={},
            source_parts=parts,
            target_parts=[],
        )
        return f"html:{tag}", Data(
            source=full_text,
            tags=tags,
            meta=Meta(),
            status=TranslationStatus.UNKNOWN,
        )

    def _build_parts(
        self,
        element: _Element,
        parts: list[TextPart | CodePart],
        tag_map: dict[str, TieData],
        tag_order: int,
        pair_counter: int,
    ) -> tuple[int, int]:
        text = element.text or ""
        if text:
            parts.append(TextPart(value=text))

        for child in element:
            child_tag = self._tag_name(child)
            if child_tag not in _INLINE_TAGS:
                continue

            if child_tag in _STANDALONE_TAGS:
                ref_id = f"t{tag_order}"
                type_info = _TAG_TYPE_MAP.get(child_tag)
                tie_type = type_info[0] if type_info else TieType.CUSTOM_STANDALONE
                attrs: dict[str, str] = {str(key): str(value) for key, value in child.attrib.items()}
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
                attrs = {str(key): str(value) for key, value in child.attrib.items()}
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

                tag_order, pair_counter = self._build_parts(
                    child,
                    parts,
                    tag_map,
                    tag_order,
                    pair_counter,
                )

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

        return tag_order, pair_counter

    def _trim_parts(self, parts: list[TextPart | CodePart]) -> None:
        for part in parts:
            if isinstance(part, TextPart):
                part.value = part.value.lstrip()
                if not part.value:
                    parts.remove(part)
                break
        for part in reversed(parts):
            if isinstance(part, TextPart):
                part.value = part.value.rstrip()
                if not part.value:
                    parts.remove(part)
                break

    def _get_direct_text(self, element: _Element, released_tails: Sequence[str]) -> str:
        parts: list[str] = []
        if element.text:
            parts.append(element.text)
        parts.extend(released_tails)
        for child in element:
            if child.tail:
                parts.append(child.tail)
        return "".join(parts).strip()

    def _tag_name(self, element: _Element) -> str:
        tag: object = element.tag
        if isinstance(tag, str):
            return tag.lower()
        return ""

    def _initialize_languages(self, root: _Element) -> None:
        lang = root.get("lang")
        if lang and not self.source_locale:
            self.source_locale = lang
            self.source_language = self._base_language(lang)
        if self.source_locale and self.source_language is None:
            self.source_language = self._base_language(self.source_locale)
        if self.target_locale and self.target_language is None:
            self.target_language = self._base_language(self.target_locale)

    def _extract_meta_element(self, element: _Element) -> RawExtractItem | None:
        name = (element.get("name") or "").lower()
        content = element.get("content") or ""
        if name not in ("description", "keywords") or not content.strip():
            return None
        return (
            f"html:meta.{name}",
            Data(
                source=content.strip(),
                meta=Meta(),
                status=TranslationStatus.UNKNOWN,
                extensions={"meta_name": name},
            ),
        )

    def _extract_image(self, element: _Element) -> RawExtractItem | None:
        alt = element.get("alt")
        if not alt or not alt.strip():
            return None
        return (
            "html:img.alt",
            Data(
                source=alt.strip(),
                meta=Meta(),
                status=TranslationStatus.UNKNOWN,
            ),
        )

    def _release_element(
        self,
        element: _Element,
        tag: str,
        block_ancestor: _Element | None,
        ancestor_tails: list[str] | None,
    ) -> None:
        if tag in _INLINE_TAGS and block_ancestor is not None:
            return
        tail = element.tail
        parent = element.getparent()
        if parent is block_ancestor and tail and ancestor_tails is not None:
            ancestor_tails.append(tail)
        element.clear()
        element.tail = tail
        if block_ancestor is not None:
            if parent is not None:
                parent.remove(element)
            return
        if parent is None:
            return
        while element.getprevious() is not None:
            del parent[0]

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
