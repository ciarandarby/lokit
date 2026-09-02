from __future__ import annotations

import sqlite3
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.data.targets import StreamingTargetSplit
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.io.filenames import FILENAME_COLLISION, LocaleFilenameError, locale_output_names
from lokit.parsers.html.extraction import _BLOCK_TAGS, _INLINE_TAGS, HtmlExtractor
from lokit.types import TagSyntax, render_segment, segment_from_legacy

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator, Mapping
    from types import TracebackType
    from typing import BinaryIO, TextIO

    from lxml.etree import _Element

Structure = BaseStructure | StreamingStructure
_MAX_TARGET_LOCALES = 256
_MAX_SPOOL_BYTES = 4 * 1024 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

_KIND_META = 0
_KIND_IMAGE = 1
_KIND_BLOCK_TEXT = 2
_KIND_BLOCK_DIRECT = 3
_KIND_BLOCK_INLINE = 4

_HEAD_METADATA = 0
_HEAD_CONTENT = 1

_VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_RAW_TEXT_TAGS: frozenset[str] = frozenset({"script", "style"})


class _Closable(Protocol):
    def close(self) -> None: ...


class _CancellationAwareReader:
    """Bound source reads so cancellation does not wait for the next SAX event."""

    __slots__ = ("_cancellation", "_source")

    def __init__(
        self,
        source: BinaryIO,
        cancellation: threading.Event | None,
    ) -> None:
        self._source = source
        self._cancellation = cancellation

    def read(self, size: int = -1) -> bytes:
        raise_if_cancelled(self._cancellation)
        if size < 0 or size > _READ_CHUNK_BYTES:
            size = _READ_CHUNK_BYTES
        return self._source.read(size)


def export_html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    _export_html(
        document,
        filepath,
        source_html,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _export_html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None,
    *,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    export_document = prepare_export_document(
        document,
        resolve_placeholders=resolve_placeholders,
    )
    path = Path(filepath)
    if export_document.target_locale is None and export_document.target_locales:
        if path.suffix:
            raise ValueError("HTML export needs a selected target locale for a single output path")
        output_names = _locale_output_names(export_document.target_locales)
        path.mkdir(parents=True, exist_ok=True)
        with StreamingTargetSplit(export_document, _cancellation=cancellation) as documents:
            for locale, target_document in documents.items():
                raise_if_cancelled(cancellation)
                _export_html_single(
                    target_document,
                    path / output_names[locale],
                    source_html,
                    cancellation,
                )
        return
    _export_html_single(export_document, path, source_html, cancellation)


def _export_html_single(
    document: Structure,
    path: Path,
    source_html: str | Path | None,
    cancellation: threading.Event | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if source_html is not None:
        _export_from_source(document, path, Path(source_html), cancellation)
    else:
        _export_minimal(document, path, cancellation)


async def export_html_async(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_html(
            document,
            filepath,
            source_html,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def _export_from_source(
    document: Structure,
    output: Path,
    source: Path,
    cancellation: threading.Event | None,
) -> None:
    raise_if_cancelled(cancellation)
    source_fingerprint = _source_fingerprint(source)
    with _HtmlExportSpool(cancellation) as spool:
        if not spool.index_source(source):
            _export_minimal(document, output, cancellation)
            return
        spool.add_units(document)
        if _source_fingerprint(source) != source_fingerprint:
            raise RuntimeError("source HTML changed while it was being exported")
        with atomic_output_path(
            output,
            "w",
            cancellation=cancellation,
            encoding="utf-8",
            newline="\n",
        ) as destination:
            spool.write_document(
                source,
                destination,
                target_locale=document.target_locale,
            )
            if _source_fingerprint(source) != source_fingerprint:
                raise RuntimeError("source HTML changed while it was being exported")


class _HtmlExportSpool(AbstractContextManager["_HtmlExportSpool"]):
    """Disk-backed unit and source-position index for retained HTML exports."""

    def __init__(self, cancellation: threading.Event | None) -> None:
        self._cancellation = cancellation
        self._directory = tempfile.TemporaryDirectory(prefix="lokit-html-export-")
        try:
            self._connection = sqlite3.connect(Path(self._directory.name) / "export.sqlite3")
            self._configure()
            self._create_schema()
        except BaseException:
            self._directory.cleanup()
            raise
        self._unit_index = 0

    def __enter__(self) -> _HtmlExportSpool:
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

    def _configure(self) -> None:
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

    def _create_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE replacements ("
            "unit_id TEXT PRIMARY KEY, target TEXT NOT NULL, rendered TEXT, "
            "is_markup INTEGER NOT NULL"
            ") WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE mappings ("
            "ordinal INTEGER PRIMARY KEY, unit_id TEXT NOT NULL, kind INTEGER NOT NULL, tag TEXT NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE pending ("
            "ordinal INTEGER PRIMARY KEY, prefix TEXT NOT NULL, kind INTEGER NOT NULL, "
            "tag TEXT NOT NULL, owner INTEGER, head_category INTEGER"
            ")"
        )
        self._connection.execute("CREATE INDEX pending_owner ON pending(owner, ordinal)")
        self._connection.execute("CREATE INDEX pending_head ON pending(head_category, ordinal)")

    def index_source(self, source: Path) -> bool:
        has_root = _HtmlSourceIndexer(self, source, self._cancellation).run()
        self._connection.commit()
        return has_root

    def add_units(self, document: Structure) -> None:
        items = iter(_iter_items(document))
        try:
            for unit_id, unit in items:
                raise_if_cancelled(self._cancellation)
                if unit.target is None:
                    self._connection.execute(
                        "DELETE FROM replacements WHERE unit_id = ?",
                        (unit_id,),
                    )
                    continue
                is_markup = unit.tags is not None and bool(unit.tags.source_parts)
                rendered = _rebuild_inline(unit, is_target=True) if is_markup else None
                self._connection.execute(
                    "INSERT INTO replacements (unit_id, target, rendered, is_markup) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(unit_id) DO UPDATE SET "
                    "target=excluded.target, rendered=excluded.rendered, is_markup=excluded.is_markup",
                    (unit_id, unit.target, rendered, int(is_markup)),
                )
        finally:
            _close_iterator(items)
        self._connection.commit()

    def write_document(
        self,
        source: Path,
        destination: TextIO,
        *,
        target_locale: str | None,
    ) -> None:
        cursor = self._connection.execute(
            "SELECT mappings.ordinal, mappings.kind, mappings.tag, "
            "replacements.target, replacements.rendered, replacements.is_markup "
            "FROM mappings LEFT JOIN replacements ON replacements.unit_id = mappings.unit_id "
            "ORDER BY mappings.ordinal"
        )
        serializer = _StreamingHtmlSerializer(
            destination,
            cursor,
            cancellation=self._cancellation,
            target_locale=target_locale,
        )
        parser = etree.HTMLParser(
            target=serializer,
            recover=True,
            no_network=True,
            huge_tree=False,
        )
        with source.open("rb") as source_stream:
            while chunk := source_stream.read(_READ_CHUNK_BYTES):
                raise_if_cancelled(self._cancellation)
                parser.feed(chunk)
        raise_if_cancelled(self._cancellation)
        parser.close()
        serializer.verify_complete()

    def record_pending(
        self,
        ordinal: int,
        prefix: str,
        kind: int,
        tag: str,
        *,
        owner: int | None = None,
        head_category: int | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO pending (ordinal, prefix, kind, tag, owner, head_category) VALUES (?, ?, ?, ?, ?, ?)",
            (ordinal, prefix, kind, tag, owner, head_category),
        )

    def emit_owner(self, owner: int) -> None:
        rows = self._connection.execute(
            "SELECT ordinal, prefix, kind, tag FROM pending WHERE owner = ? AND head_category IS NULL ORDER BY ordinal",
            (owner,),
        )
        for ordinal, prefix, kind, tag in rows:
            self._emit(
                cast("int", ordinal),
                cast("str", prefix),
                cast("int", kind),
                cast("str", tag),
            )
        self._connection.execute(
            "DELETE FROM pending WHERE owner = ? AND head_category IS NULL",
            (owner,),
        )

    def emit_head(self) -> None:
        rows = self._connection.execute(
            "SELECT ordinal, prefix, kind, tag FROM pending "
            "WHERE head_category IS NOT NULL ORDER BY head_category, ordinal"
        )
        for ordinal, prefix, kind, tag in rows:
            self._emit(
                cast("int", ordinal),
                cast("str", prefix),
                cast("int", kind),
                cast("str", tag),
            )
        self._connection.execute("DELETE FROM pending WHERE head_category IS NOT NULL")

    def emit_direct(self, ordinal: int, prefix: str, kind: int, tag: str) -> None:
        self._emit(ordinal, prefix, kind, tag)

    def _emit(self, ordinal: int, prefix: str, kind: int, tag: str) -> None:
        unit_id = f"{prefix}:{self._unit_index}"
        self._connection.execute(
            "INSERT INTO mappings (ordinal, unit_id, kind, tag) VALUES (?, ?, ?, ?)",
            (ordinal, unit_id, kind, tag),
        )
        self._unit_index += 1


class _HtmlSourceIndexer:
    """Mirror the importer's emission order while retaining only active elements."""

    def __init__(
        self,
        spool: _HtmlExportSpool,
        source: Path,
        cancellation: threading.Event | None,
    ) -> None:
        self._spool = spool
        self._source = source
        self._cancellation = cancellation
        self._extractor = HtmlExtractor(str(source))
        self._ordinal = 0
        self._element_stack: list[tuple[_Element, int]] = []
        self._open_blocks: list[tuple[_Element, int]] = []
        self._block_had_released_children: dict[_Element, bool] = {}
        self._in_head = False
        self._head_emitted = False

    def run(self) -> bool:
        try:
            with self._source.open("rb") as source_stream:
                context = etree.iterparse(
                    _CancellationAwareReader(source_stream, self._cancellation),
                    events=("start", "end"),
                    html=True,
                    recover=True,
                    no_network=True,
                    huge_tree=False,
                )
                for event, element in context:
                    raise_if_cancelled(self._cancellation)
                    tag = self._extractor._tag_name(element)
                    if event == "start":
                        self._start(element, tag)
                    else:
                        self._end(element, tag)
        except etree.XMLSyntaxError:
            if self._ordinal != 0:
                raise
        if self._element_stack or self._open_blocks:
            raise RuntimeError("HTML parser returned an unbalanced element stream")
        if not self._head_emitted:
            self._spool.emit_head()
        return self._ordinal != 0

    def _start(self, element: _Element, tag: str) -> None:
        self._ordinal += 1
        self._element_stack.append((element, self._ordinal))
        if tag == "head":
            self._in_head = True
        elif tag == "body" and not self._head_emitted:
            self._spool.emit_head()
            self._head_emitted = True
        if tag in _BLOCK_TAGS:
            self._open_blocks.append((element, self._ordinal))
            self._block_had_released_children[element] = False

    def _end(self, element: _Element, tag: str) -> None:
        if not self._element_stack or self._element_stack[-1][0] is not element:
            raise RuntimeError("HTML parser returned an unbalanced element stream")
        _, ordinal = self._element_stack.pop()
        is_block = tag in _BLOCK_TAGS
        if is_block and self._open_blocks and self._open_blocks[-1][0] is element:
            block_ancestor = self._open_blocks[-2][0] if len(self._open_blocks) > 1 else None
        else:
            block_ancestor = self._open_blocks[-1][0] if self._open_blocks else None

        if tag == "meta" and self._in_head:
            item = self._extractor._extract_meta_element(element)
            if item is not None and not self._head_emitted:
                prefix, _ = item
                self._spool.record_pending(
                    ordinal,
                    prefix,
                    _KIND_META,
                    tag,
                    head_category=_HEAD_METADATA,
                )
        elif is_block:
            had_released_children = self._block_had_released_children.pop(element)
            item = self._extractor._extract_block(element)
            if item is not None:
                prefix, _ = item
                kind = self._block_kind(element, had_released_children)
                self._record_content(ordinal, prefix, kind, tag)
            if not self._in_head and block_ancestor is None:
                self._spool.emit_owner(ordinal)
        elif tag == "img":
            item = self._extractor._extract_image(element)
            if item is not None:
                prefix, _ = item
                self._record_content(ordinal, prefix, _KIND_IMAGE, tag)

        if tag == "head":
            self._in_head = False
            if not self._head_emitted:
                self._spool.emit_head()
                self._head_emitted = True

        if block_ancestor is not None and element.getparent() is block_ancestor and tag not in _INLINE_TAGS:
            self._block_had_released_children[block_ancestor] = True
        self._extractor._release_element(element, tag, block_ancestor)
        if is_block and self._open_blocks and self._open_blocks[-1][0] is element:
            self._open_blocks.pop()

    def _record_content(self, ordinal: int, prefix: str, kind: int, tag: str) -> None:
        if self._in_head:
            if not self._head_emitted:
                self._spool.record_pending(
                    ordinal,
                    prefix,
                    kind,
                    tag,
                    head_category=_HEAD_CONTENT,
                )
            return
        if self._open_blocks:
            self._spool.record_pending(
                ordinal,
                prefix,
                kind,
                tag,
                owner=self._open_blocks[0][1],
            )
            return
        self._spool.emit_direct(ordinal, prefix, kind, tag)

    def _block_kind(self, element: _Element, had_released_children: bool) -> int:
        if len(element) == 0 and not had_released_children:
            return _KIND_BLOCK_TEXT
        if self._extractor._has_inline_children(element):
            return _KIND_BLOCK_INLINE
        return _KIND_BLOCK_DIRECT


@dataclass(slots=True)
class _OutputFrame:
    tag: str
    suppressed: bool
    translated_kind: int | None


class _StreamingHtmlSerializer:
    """Serialize the recovered HTML event stream without building a document tree."""

    def __init__(
        self,
        destination: TextIO,
        mappings: Iterator[tuple[object, ...]],
        *,
        cancellation: threading.Event | None,
        target_locale: str | None,
    ) -> None:
        self._destination = destination
        self._mappings = mappings
        self._cancellation = cancellation
        self._target_locale = target_locale
        self._next_mapping = self._read_mapping()
        self._ordinal = 0
        self._frames: list[_OutputFrame] = []
        self._doctype_written = False
        self._encoding_declared = False

    def start(
        self,
        tag: str | bytes,
        attributes: Mapping[str | bytes, str | bytes],
    ) -> None:
        raise_if_cancelled(self._cancellation)
        self._ordinal += 1
        normalized_tag = _parser_text(tag).lower()
        mapping = self._mapping_for(self._ordinal, normalized_tag)
        parent = self._frames[-1] if self._frames else None
        suppressed = bool(parent and parent.suppressed)
        if parent is not None and (
            parent.translated_kind == _KIND_BLOCK_TEXT
            or (parent.translated_kind == _KIND_BLOCK_INLINE and normalized_tag in _INLINE_TAGS)
        ):
            suppressed = True

        translated_kind: int | None = None
        if not suppressed:
            if not self._doctype_written:
                self._write("<!DOCTYPE html>")
                self._doctype_written = True
            output_attributes = {_parser_text(name): _parser_text(value) for name, value in attributes.items()}
            if normalized_tag == "html" and self._target_locale:
                output_attributes["lang"] = self._target_locale
            if normalized_tag == "meta":
                _normalize_html_encoding(output_attributes)
            target: str | None = None
            rendered: str | None = None
            kind: int | None = None
            if mapping is not None:
                kind, target, rendered = mapping
                if target is not None:
                    if kind == _KIND_META:
                        output_attributes["content"] = target
                    elif kind == _KIND_IMAGE:
                        output_attributes["alt"] = target
            if normalized_tag == "body" and not self._encoding_declared:
                self._write('<head><meta charset="utf-8"></head>')
                self._encoding_declared = True
            self._write_start(normalized_tag, output_attributes)
            if normalized_tag == "head" and not self._encoding_declared:
                # Retained exports are always encoded as UTF-8.  Put the
                # declaration first so HTML encoding sniffers cannot decode
                # translated non-ASCII text using the legacy fallback.
                self._write('<meta charset="utf-8">')
                self._encoding_declared = True
            if target is not None and kind in {_KIND_BLOCK_TEXT, _KIND_BLOCK_DIRECT, _KIND_BLOCK_INLINE}:
                self._write(_escape(target) if rendered is None else rendered)
                translated_kind = kind

        self._frames.append(
            _OutputFrame(
                tag=normalized_tag,
                suppressed=suppressed,
                translated_kind=translated_kind,
            )
        )

    def end(self, tag: str | bytes) -> None:
        raise_if_cancelled(self._cancellation)
        normalized_tag = _parser_text(tag).lower()
        if not self._frames:
            raise RuntimeError("HTML parser returned an unbalanced element stream")
        frame = self._frames.pop()
        if frame.tag != normalized_tag:
            raise RuntimeError("HTML parser returned an unbalanced element stream")
        if not frame.suppressed and normalized_tag not in _VOID_TAGS:
            self._write(f"</{normalized_tag}>")

    def data(self, data: str | bytes) -> None:
        raise_if_cancelled(self._cancellation)
        data = _parser_text(data)
        if not data:
            return
        frame = self._frames[-1] if self._frames else None
        if frame is not None and (frame.suppressed or frame.translated_kind is not None):
            return
        if frame is not None and frame.tag in _RAW_TEXT_TAGS:
            self._write(data)
        else:
            self._write(_escape_text(data))

    def comment(self, comment: str | bytes) -> None:
        raise_if_cancelled(self._cancellation)
        comment = _parser_text(comment)
        frame = self._frames[-1] if self._frames else None
        if frame is not None and (frame.suppressed or frame.translated_kind is not None):
            return
        safe_comment = comment.replace("--", "- -")
        if safe_comment.endswith("-"):
            safe_comment += " "
        self._write(f"<!--{safe_comment}-->")

    def doctype(self, _name: str, _public_id: str | None, _system_id: str | None) -> None:
        raise_if_cancelled(self._cancellation)
        if not self._doctype_written:
            self._write("<!DOCTYPE html>")
            self._doctype_written = True

    def pi(self, target: str, data: str) -> None:
        raise_if_cancelled(self._cancellation)
        frame = self._frames[-1] if self._frames else None
        if frame is not None and (frame.suppressed or frame.translated_kind is not None):
            return
        safe_target = target.replace("?>", "? >")
        safe_data = data.replace("?>", "? >")
        self._write(f"<?{safe_target} {safe_data}?>")

    def close(self) -> None:
        if self._frames:
            raise RuntimeError("HTML parser returned an unbalanced element stream")

    def verify_complete(self) -> None:
        if self._next_mapping is not None:
            raise RuntimeError("HTML source changed between indexing and serialization")

    def _mapping_for(self, ordinal: int, tag: str) -> tuple[int, str | None, str | None] | None:
        mapping = self._next_mapping
        if mapping is None or mapping[0] > ordinal:
            return None
        if mapping[0] < ordinal:
            raise RuntimeError("HTML source changed between indexing and serialization")
        if mapping[2] != tag:
            raise RuntimeError("HTML source changed between indexing and serialization")
        self._next_mapping = self._read_mapping()
        return mapping[1], mapping[3], mapping[4]

    def _read_mapping(self) -> tuple[int, int, str, str | None, str | None] | None:
        row = next(self._mappings, None)
        if row is None:
            return None
        ordinal, kind, tag, target, rendered, _is_markup = row
        return (
            cast("int", ordinal),
            cast("int", kind),
            cast("str", tag),
            cast("str | None", target),
            cast("str | None", rendered),
        )

    def _write_start(self, tag: str, attributes: Mapping[str, str]) -> None:
        self._write(f"<{tag}")
        for name, value in attributes.items():
            self._write(f' {name}="{_escape(value)}"')
        self._write(">")

    def _write(self, value: str) -> None:
        self._destination.write(value)


def _export_minimal(
    document: Structure,
    output: Path,
    cancellation: threading.Event | None,
) -> None:
    lang = document.target_locale or document.source_locale
    # Head metadata and body content must be emitted in different sections, but
    # a StreamingStructure is one-shot. Spool each section to disk in one pass
    # rather than retaining all units or iterating the input twice.
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as head,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as body,
    ):
        items = iter(_iter_items(document))
        try:
            for unit_id, unit in items:
                raise_if_cancelled(cancellation)
                if "meta." in unit_id:
                    name = unit.extensions.get("meta_name", "")
                    text = unit.target if unit.target is not None else unit.source
                    head.write(f'<meta name="{_escape(name)}" content="{_escape(text)}">\n')
                    continue
                if "img.alt" in unit_id:
                    continue
                text = unit.target if unit.target is not None else unit.source
                tag = _extract_tag_from_id(unit_id)
                if unit.tags and unit.tags.source_parts:
                    content = _rebuild_inline(unit, is_target=unit.target is not None)
                    body.write(f"<{tag}>{content}</{tag}>\n")
                else:
                    body.write(f"<{tag}>{_escape(text)}</{tag}>\n")
        finally:
            _close_iterator(items)

        head.seek(0)
        body.seek(0)
        with atomic_output_path(
            output,
            "w",
            cancellation=cancellation,
            encoding="utf-8",
            newline="\n",
        ) as destination:
            destination.write(
                "\n".join(
                    (
                        "<!DOCTYPE html>",
                        f'<html lang="{_escape(lang)}">',
                        "<head>",
                        '<meta charset="utf-8">',
                        "",
                    )
                )
            )
            _copy_text(head, destination, cancellation)
            destination.write("</head>\n<body>\n")
            _copy_text(body, destination, cancellation)
            destination.write("</body>\n</html>")


def _rebuild_inline(unit: Data, is_target: bool) -> str:
    text = unit.target if is_target and unit.target is not None else unit.source
    if unit.tags is None:
        return _escape(text)
    if is_target:
        parts = unit.tags.target_parts
        tag_map = unit.tags.target_tag_map
    else:
        parts = unit.tags.source_parts
        tag_map = unit.tags.source_tag_map
    segment = segment_from_legacy(text, parts, tag_map, syntax=TagSyntax.HTML)
    return render_segment(segment, TagSyntax.HTML, native_syntax=TagSyntax.HTML)


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _extract_tag_from_id(unit_id: str) -> str:
    parts = unit_id.split(":")
    if len(parts) >= 2:
        return parts[1]
    return "p"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _copy_text(source: TextIO, destination: TextIO, cancellation: threading.Event | None) -> None:
    while chunk := source.read(_READ_CHUNK_BYTES):
        raise_if_cancelled(cancellation)
        destination.write(chunk)


def _source_fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    stat_result = path.stat()
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _parser_text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _normalize_html_encoding(attributes: dict[str, str]) -> None:
    names = {name.lower(): name for name in attributes}
    charset_name = names.get("charset")
    if charset_name is not None:
        attributes[charset_name] = "utf-8"
        return
    http_equiv_name = names.get("http-equiv")
    if http_equiv_name is None or attributes[http_equiv_name].strip().lower() != "content-type":
        return
    content_name = names.get("content", "content")
    attributes[content_name] = "text/html; charset=utf-8"


def _locale_output_names(locales: tuple[str, ...]) -> dict[str, str]:
    if len(locales) > _MAX_TARGET_LOCALES:
        raise ValueError(f"HTML export supports at most {_MAX_TARGET_LOCALES} target locales")
    unique_locales = tuple(dict.fromkeys(locales))
    try:
        return dict(locale_output_names(unique_locales, prefix="index.", suffix=".html"))
    except LocaleFilenameError as exc:
        if exc.reason == FILENAME_COLLISION:
            raise ValueError("Target locales produce colliding HTML filenames") from exc
        raise ValueError(f"Unsafe target locale for HTML filename: {exc.locale!r}") from exc


def _close_iterator(items: object) -> None:
    if hasattr(items, "close"):
        cast("_Closable", items).close()
