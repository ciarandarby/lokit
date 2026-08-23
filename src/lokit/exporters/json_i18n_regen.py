from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.data.targets import target_text
from lokit.io.atomic import atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.io.legacy_json_stream import _JsonStreamReader
from lokit.tabular import normalize_language_header

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator
    from types import TracebackType


Structure = BaseStructure | StreamingStructure

_MAX_JSON_DEPTH = 256
_MAX_CAPTURE_CHARS = 64 * 1024 * 1024
_MAX_TARGET_LOCALES = 256
_MAX_LOCALE_CHARS = 1024
_MAX_INDENT = 32
_WRITE_CHUNK_CHARS = 64 * 1024


class _BinaryWriter(Protocol):
    def write(self, value: bytes) -> int: ...


class _Closable(Protocol):
    def close(self) -> None: ...


class _DiscardWriter:
    def write(self, value: bytes) -> int:
        return len(value)


@dataclass(frozen=True, slots=True)
class _RootMember:
    name: str
    occurrence: int
    is_object: bool


@dataclass(frozen=True, slots=True)
class _RootLayout:
    source: _RootMember | None
    members: dict[str, _RootMember]


class _ReplacementIndex(AbstractContextManager["_ReplacementIndex"]):
    """Disk-backed replacement map keyed by JSON path and locale slot."""

    def __init__(self, locales: tuple[str | None, ...]) -> None:
        self._directory = TemporaryDirectory(prefix="lokit-json-i18n-regen-")
        self.root_spool_path = Path(self._directory.name) / "source-root.json"
        self._connection = sqlite3.connect(Path(self._directory.name) / "replacements.sqlite3")
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-2048")
        self._connection.execute(
            "CREATE TABLE replacements ("
            "path TEXT NOT NULL, locale_slot INTEGER NOT NULL, text TEXT NOT NULL, "
            "PRIMARY KEY (path, locale_slot)"
            ") WITHOUT ROWID"
        )
        self._locales = locales
        self._slots = {locale: index for index, locale in enumerate(locales)}

    def __enter__(self) -> _ReplacementIndex:
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

    def add(self, unit_id: str, unit: Data) -> None:
        encoded_path = _encoded_unit_path(unit_id, unit)
        # Match dict assignment semantics: the last unit for a path replaces
        # the complete prior unit, including previously present locales.
        self._connection.execute("DELETE FROM replacements WHERE path = ?", (encoded_path,))
        for slot, locale in enumerate(self._locales):
            replacement = target_text(unit, locale)
            if replacement is not None:
                self._connection.execute(
                    "INSERT INTO replacements (path, locale_slot, text) VALUES (?, ?, ?)",
                    (encoded_path, slot, replacement),
                )

    def finish(self) -> None:
        self._connection.commit()

    def replacement(self, path: tuple[str, ...], locale: str | None) -> str | None:
        slot = self._slots[locale]
        encoded_path = _encode_path(path)
        row = self._connection.execute(
            "SELECT text FROM replacements WHERE path = ? AND locale_slot = ?",
            (encoded_path, slot),
        ).fetchone()
        if row is None:
            return None
        return cast("str", row[0])


def regenerate_json_i18n(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
) -> None:
    _regenerate_json_i18n(
        document,
        original_filepath,
        output_path,
        target_locale=target_locale,
        indent=indent,
        cancellation=None,
    )


async def regenerate_json_i18n_async(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None = None,
    indent: int = 2,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _regenerate_json_i18n(
            document,
            original_filepath,
            output_path,
            target_locale=target_locale,
            indent=indent,
            cancellation=cancellation,
        )
    )


def _regenerate_json_i18n(
    document: Structure,
    original_filepath: str | Path,
    output_path: str | Path,
    *,
    target_locale: str | None,
    indent: int,
    cancellation: threading.Event | None,
) -> None:
    normalized_indent = _normalize_indent(indent)
    source_path = Path(original_filepath)
    selected_locale = target_locale or document.target_locale
    target_locales = _target_locales(document, selected_locale)
    watched_names = _watched_root_names(document.source_locale, target_locales)
    layout = _inspect_root(source_path, document.source_locale, watched_names, cancellation)
    multilingual = _is_multilingual(layout, document, target_locales)
    indexed_locales: tuple[str | None, ...] = target_locales if multilingual else (selected_locale,)

    with _ReplacementIndex(indexed_locales) as replacements:
        _index_document(document, replacements, cancellation)
        clone_locales = _clone_locales(layout, target_locales) if multilingual else ()
        if clone_locales:
            source_root = layout.source
            if source_root is None or not source_root.is_object:
                raise ValueError("Multilingual JSON source root is unavailable for target cloning")
            _spool_source_root(
                source_path,
                replacements.root_spool_path,
                source_root,
                cancellation,
            )

        with (
            source_path.open("r", encoding="utf-8-sig", newline="") as source,
            atomic_output_path(Path(output_path), "wb", cancellation=cancellation) as output,
        ):
            reader = _JsonStreamReader(source)
            if multilingual:
                _write_multilingual_document(
                    reader,
                    output,
                    replacements,
                    layout,
                    target_locales,
                    clone_locales,
                    normalized_indent,
                    cancellation,
                )
            else:
                _write_value(
                    reader,
                    output,
                    replacements,
                    (),
                    selected_locale,
                    apply_replacements=True,
                    depth=0,
                    indent=normalized_indent,
                    cancellation=cancellation,
                )
            reader.ensure_finished()
            output.write(b"\n")


def _index_document(
    document: Structure,
    replacements: _ReplacementIndex,
    cancellation: threading.Event | None,
) -> None:
    items = iter(_iter_items(document))
    try:
        for unit_id, unit in items:
            raise_if_cancelled(cancellation)
            replacements.add(unit_id, unit)
        replacements.finish()
    finally:
        _close_iterator(items)


def _inspect_root(
    path: Path,
    source_locale: str,
    watched_names: frozenset[str],
    cancellation: threading.Event | None,
) -> _RootLayout:
    occurrences: dict[str, int] = {}
    latest: dict[str, _RootMember] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = _JsonStreamReader(source)
        _expect_object_root(reader)
        reader.take()
        reader.skip_whitespace()
        if reader.peek() != "}":
            while True:
                raise_if_cancelled(cancellation)
                name = reader.read_member_name()
                reader.expect(":")
                reader.skip_whitespace()
                root_key = _root_key(name)
                if root_key in watched_names:
                    occurrence = occurrences.get(root_key, 0) + 1
                    occurrences[root_key] = occurrence
                    latest[root_key] = _RootMember(name, occurrence, reader.peek() == "{")
                reader.skip_value()
                reader.skip_whitespace()
                separator = reader.take()
                if separator == "}":
                    break
                if separator != ",":
                    reader.fail("Expected ',' or '}' in top-level JSON object")
                reader.skip_whitespace()
        else:
            reader.take()
        reader.ensure_finished()
    return _RootLayout(source=latest.get(_root_key(source_locale)), members=latest)


def _write_multilingual_document(
    reader: _JsonStreamReader,
    output: _BinaryWriter,
    replacements: _ReplacementIndex,
    layout: _RootLayout,
    locales: tuple[str, ...],
    clone_locales: tuple[str, ...],
    indent: int,
    cancellation: threading.Event | None,
) -> None:
    locale_by_root_key = {_root_key(locale): locale for locale in locales}
    locale_set = frozenset(locale_by_root_key)
    clone_set = frozenset(clone_locales)
    occurrences: dict[str, int] = {}
    reader.expect("{")
    output.write(b"{")
    reader.skip_whitespace()
    wrote_member = False
    if reader.peek() != "}":
        while True:
            raise_if_cancelled(cancellation)
            raw_name, name = _read_raw_member_name(reader)
            reader.expect(":")
            root_key = _root_key(name)
            occurrence = 0
            if root_key in locale_set:
                occurrence = occurrences.get(root_key, 0) + 1
                occurrences[root_key] = occurrence
            selection = layout.members.get(root_key)
            selected = selection is not None and occurrence == selection.occurrence and root_key in locale_set
            selected_locale = locale_by_root_key.get(root_key)

            _write_item_prefix(output, wrote_member, 1, indent)
            _write_raw_text(output, raw_name)
            _write_name_separator(output, indent)
            if selected and selected_locale in clone_set:
                _write_value(
                    reader,
                    _DiscardWriter(),
                    replacements,
                    (),
                    selected_locale,
                    apply_replacements=False,
                    depth=1,
                    indent=None,
                    cancellation=cancellation,
                )
                _write_cloned_root(
                    replacements.root_spool_path,
                    output,
                    replacements,
                    selected_locale,
                    1,
                    indent,
                    cancellation,
                )
            elif selected:
                _write_value(
                    reader,
                    output,
                    replacements,
                    (),
                    cast("str", selected_locale),
                    apply_replacements=True,
                    depth=1,
                    indent=indent,
                    cancellation=cancellation,
                )
            else:
                _write_value(
                    reader,
                    output,
                    replacements,
                    (),
                    None,
                    apply_replacements=False,
                    depth=1,
                    indent=indent,
                    cancellation=cancellation,
                )
            wrote_member = True
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "}":
                break
            if separator != ",":
                reader.fail("Expected ',' or '}' in top-level JSON object")
            reader.skip_whitespace()
    else:
        reader.take()

    for locale in clone_locales:
        if _root_key(locale) in layout.members:
            continue
        _write_item_prefix(output, wrote_member, 1, indent)
        _write_json_string(output, locale)
        _write_name_separator(output, indent)
        _write_cloned_root(
            replacements.root_spool_path,
            output,
            replacements,
            locale,
            1,
            indent,
            cancellation,
        )
        wrote_member = True
    if wrote_member:
        _write_line_break(output, 0, indent)
    output.write(b"}")


def _spool_source_root(
    source_path: Path,
    spool_path: Path,
    selection: _RootMember,
    cancellation: threading.Event | None,
) -> None:
    occurrences = 0
    with (
        source_path.open("r", encoding="utf-8-sig", newline="") as source,
        spool_path.open("wb") as output,
    ):
        reader = _JsonStreamReader(source)
        reader.expect("{")
        reader.skip_whitespace()
        if reader.peek() == "}":
            raise ValueError("Multilingual JSON source root is unavailable for target cloning")
        while True:
            raise_if_cancelled(cancellation)
            name = reader.read_member_name()
            reader.expect(":")
            if _root_key(name) == _root_key(selection.name):
                occurrences += 1
                if occurrences == selection.occurrence:
                    _write_value(
                        reader,
                        output,
                        None,
                        (),
                        None,
                        apply_replacements=False,
                        depth=0,
                        indent=None,
                        cancellation=cancellation,
                    )
                    return
            reader.skip_value()
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "}":
                break
            if separator != ",":
                reader.fail("Expected ',' or '}' in top-level JSON object")
            reader.skip_whitespace()
    raise ValueError("Multilingual JSON source root is unavailable for target cloning")


def _write_cloned_root(
    spool_path: Path,
    output: _BinaryWriter,
    replacements: _ReplacementIndex,
    locale: str,
    depth: int,
    indent: int,
    cancellation: threading.Event | None,
) -> None:
    with spool_path.open("r", encoding="utf-8", newline="") as source:
        reader = _JsonStreamReader(source)
        _write_value(
            reader,
            output,
            replacements,
            (),
            locale,
            apply_replacements=True,
            depth=depth,
            indent=indent,
            cancellation=cancellation,
        )
        reader.ensure_finished()


def _write_value(
    reader: _JsonStreamReader,
    output: _BinaryWriter,
    replacements: _ReplacementIndex | None,
    path: tuple[str, ...],
    locale: str | None,
    *,
    apply_replacements: bool,
    depth: int,
    indent: int | None,
    cancellation: threading.Event | None,
) -> None:
    raise_if_cancelled(cancellation)
    if depth > _MAX_JSON_DEPTH:
        reader.fail(f"JSON nesting exceeds the {_MAX_JSON_DEPTH}-level safety limit")
    reader.skip_whitespace()
    marker = reader.peek()
    if marker == "{":
        _write_object(
            reader,
            output,
            replacements,
            path,
            locale,
            apply_replacements,
            depth,
            indent,
            cancellation,
        )
        return
    if marker == "[":
        _write_array(
            reader,
            output,
            replacements,
            path,
            locale,
            apply_replacements,
            depth,
            indent,
            cancellation,
        )
        return

    raw = reader.read_raw_value()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(exc.msg, raw, exc.pos) from exc
    if isinstance(decoded, str) and apply_replacements:
        if replacements is None:
            raise RuntimeError("JSON replacement index is unavailable")
        replacement = replacements.replacement(path, locale)
        if replacement is not None:
            _write_json_string(output, replacement)
            return
    _write_raw_text(output, raw)


def _write_object(
    reader: _JsonStreamReader,
    output: _BinaryWriter,
    replacements: _ReplacementIndex | None,
    path: tuple[str, ...],
    locale: str | None,
    apply_replacements: bool,
    depth: int,
    indent: int | None,
    cancellation: threading.Event | None,
) -> None:
    reader.expect("{")
    output.write(b"{")
    reader.skip_whitespace()
    wrote_member = False
    if reader.peek() != "}":
        while True:
            raise_if_cancelled(cancellation)
            raw_name, name = _read_raw_member_name(reader)
            reader.expect(":")
            _write_item_prefix(output, wrote_member, depth + 1, indent)
            _write_raw_text(output, raw_name)
            _write_name_separator(output, indent)
            _write_value(
                reader,
                output,
                replacements,
                (*path, name),
                locale,
                apply_replacements=apply_replacements,
                depth=depth + 1,
                indent=indent,
                cancellation=cancellation,
            )
            wrote_member = True
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "}":
                break
            if separator != ",":
                reader.fail("Expected ',' or '}' in JSON object")
            reader.skip_whitespace()
    else:
        reader.take()
    if wrote_member:
        _write_line_break(output, depth, indent)
    output.write(b"}")


def _write_array(
    reader: _JsonStreamReader,
    output: _BinaryWriter,
    replacements: _ReplacementIndex | None,
    path: tuple[str, ...],
    locale: str | None,
    apply_replacements: bool,
    depth: int,
    indent: int | None,
    cancellation: threading.Event | None,
) -> None:
    reader.expect("[")
    output.write(b"[")
    reader.skip_whitespace()
    wrote_item = False
    if reader.peek() != "]":
        while True:
            _write_item_prefix(output, wrote_item, depth + 1, indent)
            _write_value(
                reader,
                output,
                replacements,
                path,
                locale,
                apply_replacements=apply_replacements,
                depth=depth + 1,
                indent=indent,
                cancellation=cancellation,
            )
            wrote_item = True
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "]":
                break
            if separator != ",":
                reader.fail("Expected ',' or ']' in JSON array")
            reader.skip_whitespace()
    else:
        reader.take()
    if wrote_item:
        _write_line_break(output, depth, indent)
    output.write(b"]")


def _read_raw_member_name(reader: _JsonStreamReader) -> tuple[str, str]:
    reader.skip_whitespace()
    raw = reader.read_raw_value()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(exc.msg, raw, exc.pos) from exc
    if not isinstance(decoded, str):
        reader.fail("Expected a JSON object member name")
    return raw, decoded


def _write_item_prefix(output: _BinaryWriter, wrote_item: bool, depth: int, indent: int | None) -> None:
    if wrote_item:
        output.write(b",")
    _write_line_break(output, depth, indent)


def _write_line_break(output: _BinaryWriter, depth: int, indent: int | None) -> None:
    if indent is None:
        return
    output.write(b"\n")
    if indent:
        output.write(b" " * (depth * indent))


def _write_name_separator(output: _BinaryWriter, indent: int | None) -> None:
    output.write(b":" if indent is None else b": ")


def _write_raw_text(output: _BinaryWriter, value: str) -> None:
    for start in range(0, len(value), _WRITE_CHUNK_CHARS):
        output.write(value[start : start + _WRITE_CHUNK_CHARS].encode("utf-8"))


def _write_json_string(output: _BinaryWriter, value: str) -> None:
    output.write(b'"')
    for start in range(0, len(value), _WRITE_CHUNK_CHARS):
        encoded = json.dumps(value[start : start + _WRITE_CHUNK_CHARS], ensure_ascii=False)
        output.write(encoded[1:-1].encode("utf-8"))
    output.write(b'"')


def _target_locales(document: Structure, selected_locale: str | None) -> tuple[str, ...]:
    raw_locales: tuple[str, ...]
    if selected_locale is not None:
        raw_locales = (selected_locale,)
    elif document.target_locales:
        raw_locales = document.target_locales
    elif document.target_locale is not None:
        raw_locales = (document.target_locale,)
    else:
        raw_locales = ()
    locales = tuple(dict.fromkeys(raw_locales))
    if len(locales) > _MAX_TARGET_LOCALES:
        raise ValueError(f"JSON i18n regeneration supports at most {_MAX_TARGET_LOCALES} target locales")
    if any(len(locale) > _MAX_LOCALE_CHARS for locale in locales):
        raise ValueError("JSON i18n locale exceeds the 1024-character safety limit")
    return locales


def _watched_root_names(source_locale: str, locales: tuple[str, ...]) -> frozenset[str]:
    if len(source_locale) > _MAX_LOCALE_CHARS:
        raise ValueError("JSON i18n source locale exceeds the 1024-character safety limit")
    return frozenset(_root_key(locale) for locale in (source_locale, *locales))


def _is_multilingual(layout: _RootLayout, document: Structure, locales: tuple[str, ...]) -> bool:
    source = layout.source
    if source is None or not source.is_object:
        return False
    document_target_keys = frozenset(_root_key(locale) for locale in document.target_locales)
    return any(_root_key(locale) in layout.members or _root_key(locale) in document_target_keys for locale in locales)


def _clone_locales(layout: _RootLayout, locales: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        locale
        for locale in locales
        if (member := layout.members.get(_root_key(locale))) is None or not member.is_object
    )


def _root_key(locale: str) -> str:
    return normalize_language_header(locale) or locale


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _close_iterator(items: Iterator[tuple[str, Data]]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_Closable", candidate).close()


def _encoded_unit_path(unit_id: str, unit: Data) -> str:
    raw_path = unit.extensions.get("json_path")
    if raw_path:
        if len(raw_path) > _MAX_CAPTURE_CHARS:
            raise ValueError("JSON path metadata exceeds the 64-million-character safety limit")
        parsed = json.loads(raw_path)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return _encode_path(tuple(parsed))
    return _encode_path(tuple(unit_id.split(".")))


def _encode_path(path: tuple[str, ...]) -> str:
    if len(path) > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON path exceeds the {_MAX_JSON_DEPTH}-level safety limit")
    if any(len(component) > _MAX_CAPTURE_CHARS for component in path):
        raise ValueError("JSON path component exceeds the 64-million-character safety limit")
    encoded = json.dumps(path, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > _MAX_CAPTURE_CHARS:
        raise ValueError("Encoded JSON path exceeds the 64-million-character safety limit")
    return encoded


def _normalize_indent(indent: int) -> int:
    if indent > _MAX_INDENT:
        raise ValueError(f"JSON indentation exceeds the {_MAX_INDENT}-space safety limit")
    return max(indent, 0)


def _expect_object_root(reader: _JsonStreamReader) -> None:
    reader.skip_whitespace()
    if reader.peek() != "{":
        raise TypeError("Expected JSON object at translation root")


__all__ = ["regenerate_json_i18n", "regenerate_json_i18n_async"]
