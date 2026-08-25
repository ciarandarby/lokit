from __future__ import annotations

import copy
import json
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit.data.structure import BaseStructure, CodePart, Data, StreamingStructure, TextPart
from lokit.data.targets import _clone_for_target
from lokit.io.atomic import atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.io.filenames import FILENAME_COLLISION, LocaleFilenameError, locale_output_names
from lokit.placeholders import literalize_data, resolve_data

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator
    from typing import BinaryIO

    from lxml.etree import _Element


Structure = BaseStructure | StreamingStructure
ExtractItem = tuple[str, Data]

_COPY_BUFFER_BYTES = 1024 * 1024
_MAX_ZIP_ENTRIES = 100_000
_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_STORY_BYTES = 512 * 1024 * 1024
_MAX_OUTPUT_STORY_BYTES = 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000.0
_MAX_MEMBER_NAME_BYTES = 4096
_MAX_TRANSLATION_UNITS = 10_000_000
_MAX_TARGET_LOCALES = 256
_MAX_UNIT_ID_BYTES = 4096
_MAX_SPOOL_BYTES = 4 * 1024 * 1024 * 1024
_SINGLE_TARGET_KEY = ""


class _ClosableIterator(Protocol):
    def close(self) -> None: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _BinaryWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...


@dataclass(frozen=True, slots=True)
class _ReplacementPlan:
    paragraph_index: int
    target_text: str
    range_texts: dict[str, str] | None
    plain_text: str | None


class _BoundedReader:
    def __init__(
        self,
        stream: _BinaryReader,
        limit: int,
        label: str,
        cancellation: threading.Event | None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._label = label
        self._cancellation = cancellation
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        raise_if_cancelled(self._cancellation)
        remaining_with_probe = self._limit - self.bytes_read + 1
        requested = remaining_with_probe if size < 0 else min(size, remaining_with_probe)
        data = self._stream.read(max(0, requested))
        self.bytes_read += len(data)
        if self.bytes_read > self._limit:
            raise ValueError(f"{self._label} exceeds its decompression limit")
        return data


class _BoundedWriter:
    def __init__(
        self,
        stream: _BinaryWriter,
        limit: int,
        label: str,
        cancellation: threading.Event | None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._label = label
        self._cancellation = cancellation
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        raise_if_cancelled(self._cancellation)
        if self.bytes_written + len(data) > self._limit:
            raise ValueError(f"{self._label} exceeds its output limit")
        written = self._stream.write(data)
        self.bytes_written += written
        return written


class _ReplacementStore:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        try:
            self._configure()
            self._connection.execute(
                """
                CREATE TABLE replacements (
                    locale TEXT NOT NULL,
                    story TEXT NOT NULL,
                    paragraph_index INTEGER NOT NULL,
                    target_text TEXT NOT NULL,
                    range_texts TEXT,
                    plain_text TEXT,
                    PRIMARY KEY (locale, story, paragraph_index)
                ) WITHOUT ROWID
                """
            )
        except BaseException:
            self._connection.close()
            raise

    def _configure(self) -> None:
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA temp_store = FILE")
        row = self._connection.execute("PRAGMA page_size").fetchone()
        if row is None:
            raise RuntimeError("could not determine SQLite page size")
        page_size = int(row[0])
        max_pages = max(1, _MAX_SPOOL_BYTES // page_size)
        self._connection.execute(f"PRAGMA max_page_count = {max_pages}")

    def put(self, locale: str, story: str, plan: _ReplacementPlan) -> None:
        encoded_ranges = (
            json.dumps(plan.range_texts, ensure_ascii=False, separators=(",", ":"))
            if plan.range_texts is not None
            else None
        )
        self._connection.execute(
            """
            INSERT OR REPLACE INTO replacements (
                locale, story, paragraph_index, target_text, range_texts, plain_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                locale,
                story,
                plan.paragraph_index,
                plan.target_text,
                encoded_ranges,
                plan.plain_text,
            ),
        )

    def finish(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def has_story(self, locale: str, story: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM replacements WHERE locale = ? AND story = ? LIMIT 1",
            (locale, story),
        ).fetchone()
        return row is not None

    def iter_story(self, locale: str, story: str) -> Iterator[_ReplacementPlan]:
        cursor = self._connection.execute(
            """
            SELECT paragraph_index, target_text, range_texts, plain_text
            FROM replacements
            WHERE locale = ? AND story = ?
            ORDER BY paragraph_index
            """,
            (locale, story),
        )
        try:
            for paragraph_index, target_text, range_texts, plain_text in cursor:
                yield _ReplacementPlan(
                    paragraph_index=int(paragraph_index),
                    target_text=str(target_text),
                    range_texts=_decode_range_texts(cast("str | None", range_texts)),
                    plain_text=cast("str | None", plain_text),
                )
        finally:
            cursor.close()

    def close(self) -> None:
        self._connection.close()


class _PlanCursor:
    def __init__(self, plans: Iterator[_ReplacementPlan]) -> None:
        self._plans = plans
        self._current = next(plans, None)

    def take(self, paragraph_index: int) -> _ReplacementPlan | None:
        current = self._current
        while current is not None and current.paragraph_index < paragraph_index:
            current = next(self._plans, None)
        if current is None or current.paragraph_index != paragraph_index:
            self._current = current
            return None
        self._current = next(self._plans, None)
        return current

    def close(self) -> None:
        _close_iterator(self._plans)


def export_idml(
    document: Structure,
    filepath: str | Path,
    source_idml: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    _export_idml(
        document,
        filepath,
        source_idml,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _export_idml(
    document: Structure,
    filepath: str | Path,
    source_idml: str | Path,
    *,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    output_path = Path(filepath)
    source_path = Path(source_idml)
    outputs = _resolve_outputs(document, output_path)
    locale_keys = tuple(locale for locale, _ in outputs)

    with tempfile.TemporaryDirectory(prefix="lokit-idml-export-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        package_path = _stage_in_place_source(source_path, outputs, temporary_path, cancellation)
        with zipfile.ZipFile(package_path, "r") as source:
            infos = _preflight_package(package_path, source)
            store = _ReplacementStore(Path(temporary_directory) / "replacements.sqlite3")
            try:
                _spool_replacements(
                    document,
                    store,
                    locale_keys,
                    resolve_placeholders=resolve_placeholders,
                    cancellation=cancellation,
                )
                raise_if_cancelled(cancellation)
                for locale, destination in outputs:
                    raise_if_cancelled(cancellation)
                    _write_package(
                        source,
                        infos,
                        destination,
                        store,
                        locale,
                        cancellation,
                    )
            finally:
                store.close()


async def export_idml_async(
    document: Structure,
    filepath: str | Path,
    source_idml: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_idml(
            document,
            filepath,
            source_idml,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def _resolve_outputs(document: Structure, output_path: Path) -> tuple[tuple[str, Path], ...]:
    if document.target_locale is not None or not document.target_locales:
        return ((_SINGLE_TARGET_KEY, output_path),)
    if output_path.suffix:
        raise ValueError("IDML export needs a selected target locale for a single output path")
    if len(document.target_locales) > _MAX_TARGET_LOCALES:
        raise ValueError(f"IDML export supports at most {_MAX_TARGET_LOCALES} target locales")
    locales = tuple(dict.fromkeys(document.target_locales))
    try:
        names = locale_output_names(locales, suffix=".idml")
    except LocaleFilenameError as exc:
        if exc.reason == FILENAME_COLLISION:
            raise ValueError("IDML target locales produce colliding output filenames") from exc
        raise ValueError(f"unsafe IDML target locale filename: {exc.locale!r}") from exc
    return tuple((locale, output_path / filename) for locale, filename in names)


def _stage_in_place_source(
    source_path: Path,
    outputs: tuple[tuple[str, Path], ...],
    temporary_directory: Path,
    cancellation: threading.Event | None,
) -> Path:
    resolved_source = source_path.resolve()
    if not any(destination.resolve() == resolved_source for _, destination in outputs):
        return source_path
    if source_path.stat().st_size > _MAX_COMPRESSED_BYTES:
        raise ValueError("IDML package exceeds its compressed size limit")
    staged_path = temporary_directory / "source.idml"
    copied = 0
    with source_path.open("rb") as source, staged_path.open("xb") as target:
        while True:
            raise_if_cancelled(cancellation)
            chunk = source.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > _MAX_COMPRESSED_BYTES:
                raise ValueError("IDML package exceeds its compressed size limit")
            target.write(chunk)
    return staged_path


def _spool_replacements(
    document: Structure,
    store: _ReplacementStore,
    locale_keys: tuple[str, ...],
    *,
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    source_items: Iterable[ExtractItem] = (
        document.data.items() if isinstance(document, BaseStructure) else document.items
    )
    items = iter(source_items)
    transform = resolve_data if resolve_placeholders else literalize_data
    multiple_targets = locale_keys != (_SINGLE_TARGET_KEY,)
    if len(document.target_locales) > _MAX_TARGET_LOCALES:
        raise ValueError(f"IDML export supports at most {_MAX_TARGET_LOCALES} target locales")
    unique_document_locales = tuple(dict.fromkeys(document.target_locales))
    legacy_locale = unique_document_locales[0] if len(unique_document_locales) == 1 else None
    units_seen = 0
    try:
        for unit_id, unit in items:
            raise_if_cancelled(cancellation)
            units_seen += 1
            if units_seen > _MAX_TRANSLATION_UNITS:
                raise ValueError(f"IDML export exceeds {_MAX_TRANSLATION_UNITS} translation units")
            if len(unit_id.encode("utf-8")) > _MAX_UNIT_ID_BYTES:
                raise ValueError("IDML translation unit ID exceeds its size limit")
            story = unit.extensions.get("story", "")
            if not story:
                continue
            _validate_member_name(story, "IDML translation story path")
            paragraph_index = _paragraph_index(unit_id, story)
            if paragraph_index is None:
                continue
            for locale in locale_keys:
                selected = (
                    _clone_for_target(
                        unit,
                        unit.targets.get(locale),
                        keep_legacy=unit.target is not None and legacy_locale == locale,
                    )
                    if multiple_targets
                    else unit
                )
                if not selected.target:
                    continue
                prepared = transform(selected)
                store.put(locale, story, _replacement_plan(paragraph_index, prepared))
        store.finish()
    except BaseException:
        store.rollback()
        raise
    finally:
        _close_iterator(items)


def _paragraph_index(unit_id: str, story: str) -> int | None:
    story_name = _story_name(story)
    prefix = f"{story_name}:p"
    if not unit_id.startswith(prefix):
        return None
    index_text = unit_id[len(prefix) :]
    if not index_text.isascii() or not index_text.isdigit():
        return None
    index = int(index_text)
    return index if index < _MAX_TRANSLATION_UNITS else None


def _replacement_plan(paragraph_index: int, unit: Data) -> _ReplacementPlan:
    target_text = unit.target or ""
    if unit.tags is None or not unit.tags.target_parts:
        return _ReplacementPlan(paragraph_index, target_text, None, None)
    range_texts, plain_text = _tagged_replacement_texts(unit)
    return _ReplacementPlan(paragraph_index, target_text, range_texts, plain_text)


def _decode_range_texts(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    decoded: object = json.loads(value)
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in decoded.items()
    ):
        raise ValueError("invalid IDML replacement spool data")
    return cast("dict[str, str]", decoded)


def _preflight_package(source_path: Path, source: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    if source_path.stat().st_size > _MAX_COMPRESSED_BYTES:
        raise ValueError("IDML package exceeds its compressed size limit")
    infos = tuple(source.infolist())
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise ValueError(f"IDML package has more than {_MAX_ZIP_ENTRIES} ZIP entries")

    names: set[str] = set()
    compressed_bytes = 0
    uncompressed_bytes = 0
    for info in infos:
        _validate_member_name(info.filename, "IDML ZIP entry")
        if info.filename in names:
            raise ValueError(f"duplicate IDML ZIP entry: {info.filename}")
        names.add(info.filename)
        if info.flag_bits & 0x1:
            raise ValueError("encrypted IDML ZIP entries are not supported")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise ValueError(f"symbolic-link IDML ZIP entry is not supported: {info.filename}")
        if info.file_size > _MAX_MEMBER_BYTES:
            raise ValueError(f"IDML ZIP entry exceeds its size limit: {info.filename}")
        if _is_story(info.filename) and info.file_size > _MAX_STORY_BYTES:
            raise ValueError(f"IDML Story exceeds its decompression limit: {info.filename}")
        compressed_bytes += info.compress_size
        uncompressed_bytes += info.file_size
        if compressed_bytes > _MAX_COMPRESSED_BYTES:
            raise ValueError("IDML package exceeds its compressed size limit")
        if uncompressed_bytes > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("IDML package exceeds its decompression limit")
        if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO):
            raise ValueError(f"suspicious compression ratio in IDML ZIP entry: {info.filename}")
    return infos


def _validate_member_name(name: str, label: str) -> None:
    encoded_size = len(name.encode("utf-8"))
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed
        or encoded_size > _MAX_MEMBER_NAME_BYTES
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or (len(name) >= 2 and name[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe {label}: {name!r}")


def _write_package(
    source: zipfile.ZipFile,
    infos: tuple[zipfile.ZipInfo, ...],
    output_path: Path,
    store: _ReplacementStore,
    locale: str,
    cancellation: threading.Event | None,
) -> None:
    with (
        atomic_output_path(output_path, "w+b", cancellation=cancellation) as output_stream,
        zipfile.ZipFile(output_stream, "w", allowZip64=True) as target,
    ):
        target.comment = source.comment
        for info in infos:
            raise_if_cancelled(cancellation)
            if _is_story(info.filename) and store.has_story(locale, info.filename):
                _rewrite_story_member(source, target, info, store, locale, cancellation)
            else:
                _copy_member(source, target, info, cancellation)


def _copy_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    cancellation: threading.Event | None,
) -> None:
    copied_info = copy.copy(info)
    if info.is_dir():
        target.writestr(copied_info, b"")
        return
    with (
        source.open(info, "r") as source_member,
        target.open(
            copied_info,
            "w",
            force_zip64=info.file_size >= 2_000_000_000,
        ) as target_member,
    ):
        copied = 0
        while True:
            raise_if_cancelled(cancellation)
            chunk = source_member.read(_COPY_BUFFER_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > info.file_size or copied > _MAX_MEMBER_BYTES:
                raise ValueError(f"IDML ZIP entry exceeds its declared size: {info.filename}")
            target_member.write(chunk)
        if copied != info.file_size:
            raise ValueError(f"IDML ZIP entry size does not match its directory record: {info.filename}")


def _rewrite_story_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    store: _ReplacementStore,
    locale: str,
    cancellation: threading.Event | None,
) -> None:
    copied_info = copy.copy(info)
    with source.open(info, "r") as source_member, target.open(copied_info, "w") as target_member:
        bounded_source = _BoundedReader(
            source_member,
            min(info.file_size, _MAX_STORY_BYTES),
            info.filename,
            cancellation,
        )
        bounded_target = _BoundedWriter(
            target_member,
            _MAX_OUTPUT_STORY_BYTES,
            info.filename,
            cancellation,
        )
        plans = _PlanCursor(store.iter_story(locale, info.filename))
        try:
            _stream_rewrite_story(bounded_source, bounded_target, plans, cancellation)
        finally:
            plans.close()
        if bounded_source.bytes_read != info.file_size:
            raise ValueError(f"IDML Story size does not match its directory record: {info.filename}")


def _stream_rewrite_story(
    source: _BoundedReader,
    target: _BoundedWriter,
    plans: _PlanCursor,
    cancellation: threading.Event | None,
) -> None:
    context = etree.iterparse(
        cast("BinaryIO", source),
        events=("start", "end", "comment", "pi"),
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )
    events = iter(context)
    root: _Element | None = None
    for event, node in events:
        if event == "start":
            root = cast("_Element", node)
            break
    if root is None:
        raise ValueError("IDML Story XML has no document element")

    root_attributes = cast("dict[str, str]", dict(root.attrib))
    root_namespaces: dict[str | None, str] = dict(root.nsmap)
    root_text_written = False
    paragraph_index = 0
    root_ended = False
    with etree.xmlfile(target, encoding="UTF-8") as output:
        output.write_declaration()
        with output.element(root.tag, root_attributes, nsmap=root_namespaces):
            for event, raw_node in events:
                raise_if_cancelled(cancellation)
                node = cast("_Element", raw_node)
                if event == "end" and node is root:
                    root_ended = True
                    if not root_text_written and root.text:
                        output.write(root.text)
                        root_text_written = True
                    continue
                if node.getparent() is not root:
                    continue
                if not root_text_written and root.text:
                    output.write(root.text)
                    root_text_written = True
                if event == "end":
                    paragraph_index = _apply_plans(node, paragraph_index, plans)
                    output.write(node)
                    _clear_written_element(node)
                elif event in {"comment", "pi"}:
                    output.write(node)
    if not root_ended:
        raise ValueError("IDML Story XML ended before its document element closed")


def _apply_plans(element: _Element, paragraph_index: int, plans: _PlanCursor) -> int:
    for paragraph in element.iter():
        if _element_local_name(paragraph) != "ParagraphStyleRange":
            continue
        plan = plans.take(paragraph_index)
        if plan is not None:
            _apply_plan(paragraph, plan)
        paragraph_index += 1
    return paragraph_index


def _clear_written_element(element: _Element) -> None:
    parent = element.getparent()
    tail = element.tail
    element.clear()
    element.tail = tail
    if parent is None:
        return
    while element.getprevious() is not None:
        del parent[0]


def _apply_plan(psr: _Element, plan: _ReplacementPlan) -> None:
    char_ranges = [element for element in psr if _element_local_name(element) == "CharacterStyleRange"]
    if not char_ranges:
        return
    if plan.range_texts is not None:
        _replace_with_tagged_texts(char_ranges, plan.range_texts, plan.plain_text)
    else:
        _distribute_text(char_ranges, plan.target_text)


def _apply_translations(
    root: _Element,
    units: dict[str, Data],
    *,
    resolve_placeholders: bool,
) -> None:
    paragraph_index = 0
    story_name = _story_name_from_units(units)

    for psr in root.iter():
        if _element_local_name(psr) != "ParagraphStyleRange":
            continue

        unit_id = f"{story_name}:p{paragraph_index}"
        unit = units.get(unit_id)
        if unit is not None and unit.target:
            prepared = resolve_data(unit) if resolve_placeholders else literalize_data(unit)
            _replace_paragraph_text(psr, prepared)
        paragraph_index += 1


def _replace_paragraph_text(psr: _Element, unit: Data) -> None:
    char_ranges = [element for element in psr if _element_local_name(element) == "CharacterStyleRange"]
    if not char_ranges:
        return

    if unit.tags and unit.tags.target_parts:
        _replace_with_tagged_parts(char_ranges, unit)
    else:
        _distribute_text(char_ranges, unit.target or "")


def _replace_with_tagged_parts(char_ranges: list[_Element], unit: Data) -> None:
    range_texts, plain_text = _tagged_replacement_texts(unit)
    _replace_with_tagged_texts(char_ranges, range_texts, plain_text)


def _tagged_replacement_texts(unit: Data) -> tuple[dict[str, str], str | None]:
    if unit.tags is None:
        return {}, None

    parts = unit.tags.target_parts
    tag_map = unit.tags.target_tag_map
    range_texts: dict[str, str] = {}
    plain_text_parts: list[str] = []
    current_style: str | None = None
    current_text_parts: list[str] = []

    for part in parts:
        if isinstance(part, TextPart):
            if current_style is None:
                plain_text_parts.append(part.value)
            else:
                current_text_parts.append(part.value)
        elif isinstance(part, CodePart):
            tie = tag_map.get(part.ref)
            if tie is None:
                continue
            if tie.type.value.endswith(".open"):
                style = tie.attributes.get("style", "")
                if current_style is not None:
                    range_texts[current_style] = "".join(current_text_parts)
                    current_text_parts = []
                current_style = style
            elif tie.type.value.endswith(".close"):
                if current_style is not None:
                    range_texts[current_style] = "".join(current_text_parts)
                    current_text_parts = []
                    current_style = None

    if current_style is not None:
        range_texts[current_style] = "".join(current_text_parts)
    plain_text = "".join(plain_text_parts) if plain_text_parts else None
    return range_texts, plain_text


def _replace_with_tagged_texts(
    char_ranges: list[_Element],
    range_texts: dict[str, str],
    plain_text: str | None,
) -> None:
    for csr in char_ranges:
        style = csr.get("AppliedCharacterStyle") or ""
        if style in range_texts:
            _set_content_text(csr, range_texts[style])
        elif plain_text is not None and (not style or style == "CharacterStyle/$ID/[No character style]"):
            _set_content_text(csr, plain_text)
            plain_text = None
        else:
            _set_content_text(csr, "")


def _distribute_text(char_ranges: list[_Element], text: str) -> None:
    if len(char_ranges) == 1:
        _set_content_text(char_ranges[0], text)
        return

    first = char_ranges[0]
    _set_content_text(first, text)
    for csr in char_ranges[1:]:
        _set_content_text(csr, "")


def _set_content_text(csr: _Element, text: str) -> None:
    for child in csr.iter():
        if _element_local_name(child) == "Content":
            child.text = text
            text = ""


def _story_name(story_file: str) -> str:
    name = story_file
    if name.startswith("Stories/"):
        name = name[len("Stories/") :]
    if name.endswith(".xml"):
        name = name[: -len(".xml")]
    return name


def _story_name_from_units(units: dict[str, Data]) -> str:
    for unit_id in units:
        parts = unit_id.split(":")
        if parts:
            return parts[0]
    return ""


def _is_story(name: str) -> bool:
    return name.startswith("Stories/Story_") and name.endswith(".xml")


def _close_iterator(items: Iterator[object]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_ClosableIterator", candidate).close()


def _local_name(tag: object) -> str:
    if isinstance(tag, str):
        name = tag
    elif isinstance(tag, bytes):
        name = tag.decode("utf-8")
    else:
        return ""
    if "}" in name:
        return name.split("}", 1)[1]
    return name


def _element_local_name(element: _Element) -> str:
    tag: object = getattr(element, "tag", "")
    return _local_name(tag)


__all__ = ["export_idml", "export_idml_async"]
