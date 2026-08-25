from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.data.targets import target_text
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import (
    atomic_output_path,
    raise_if_cancelled,
    run_cancellable_export,
)
from lokit.io.filenames import FILENAME_COLLISION, LocaleFilenameError, locale_output_names

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator
    from types import TracebackType

Structure = BaseStructure | StreamingStructure

_BRANCH_NODE = 0
_VALUE_NODE = 1
_ROOT_NODE_ID = 1
_MAX_JSON_DEPTH = 256
_MAX_PATH_COMPONENT_CHARS = 64 * 1024 * 1024
_MAX_TARGET_LOCALES = 256
_JSON_WRITE_CHUNK_CHARS = 64 * 1024


class _Closable(Protocol):
    def close(self) -> None: ...


class _JsonExportSpool(AbstractContextManager["_JsonExportSpool"]):
    """One-pass, disk-backed representation used by repeat/nested exports."""

    def __init__(self, *, nested: bool, target_locales: tuple[str, ...]) -> None:
        self._nested = nested
        self._target_locales = target_locales
        self._directory = TemporaryDirectory(prefix="lokit-json-i18n-export-")
        self._connection = sqlite3.connect(Path(self._directory.name) / "export.sqlite3")
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-2048")
        self._connection.execute(
            "CREATE TABLE units ("
            "sequence INTEGER PRIMARY KEY, key TEXT NOT NULL, source TEXT NOT NULL, "
            "legacy_target TEXT, default_target TEXT"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE targets ("
            "unit_sequence INTEGER NOT NULL, locale TEXT NOT NULL, text TEXT, "
            "PRIMARY KEY (unit_sequence, locale)"
            ") WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE nodes ("
            "id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT NOT NULL, kind INTEGER NOT NULL, "
            "created_sequence INTEGER NOT NULL, unit_sequence INTEGER, "
            "UNIQUE (parent_id, name)"
            ")"
        )
        self._connection.execute(
            "INSERT INTO nodes (id, parent_id, name, kind, created_sequence, unit_sequence) "
            "VALUES (?, NULL, '', ?, 0, NULL)",
            (_ROOT_NODE_ID, _BRANCH_NODE),
        )
        self._unit_sequence = 0
        self._node_sequence = 0

    def __enter__(self) -> _JsonExportSpool:
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

    def add(self, key: str, unit: Data) -> None:
        self._unit_sequence += 1
        sequence = self._unit_sequence
        self._connection.execute(
            "INSERT INTO units (sequence, key, source, legacy_target, default_target) VALUES (?, ?, ?, ?, ?)",
            (sequence, key, unit.source, unit.target, target_text(unit, None)),
        )
        for locale in self._target_locales:
            target = unit.targets.get(locale)
            if target is not None:
                self._connection.execute(
                    "INSERT INTO targets (unit_sequence, locale, text) VALUES (?, ?, ?)",
                    (sequence, locale, target.text),
                )
        if self._nested:
            self._assign_path(_unit_path(key, unit), sequence)

    def finish(self) -> None:
        self._connection.commit()

    def flat_rows(self, locale: str | None) -> Iterator[tuple[str, str]]:
        if locale is None:
            cursor = self._connection.execute(
                "SELECT key, COALESCE(NULLIF(default_target, ''), source) FROM units ORDER BY sequence"
            )
        else:
            cursor = self._connection.execute(
                "SELECT units.key, "
                "CASE WHEN targets.unit_sequence IS NOT NULL "
                "THEN COALESCE(NULLIF(targets.text, ''), units.source) "
                "ELSE COALESCE(NULLIF(units.legacy_target, ''), units.source) END "
                "FROM units LEFT JOIN targets "
                "ON targets.unit_sequence = units.sequence AND targets.locale = ? "
                "ORDER BY units.sequence",
                (locale,),
            )
        for raw_key, raw_value in cursor:
            yield cast("str", raw_key), cast("str", raw_value)

    def nested_rows(self, locale: str | None) -> Iterator[tuple[int, str, int, str | None]]:
        target_join = ""
        value_expression = "COALESCE(NULLIF(units.default_target, ''), units.source)"
        parameters: tuple[str, ...] = ()
        if locale is not None:
            target_join = "LEFT JOIN targets ON targets.unit_sequence = units.sequence AND targets.locale = ? "
            value_expression = (
                "CASE WHEN targets.unit_sequence IS NOT NULL "
                "THEN COALESCE(NULLIF(targets.text, ''), units.source) "
                "ELSE COALESCE(NULLIF(units.legacy_target, ''), units.source) END"
            )
            parameters = (locale,)
        cursor = self._connection.execute(
            "WITH RECURSIVE tree (id, parent_id, name, kind, unit_sequence, depth, sort_path) AS ("
            "SELECT id, parent_id, name, kind, unit_sequence, 0, '' FROM nodes WHERE id = 1 "
            "UNION ALL "
            "SELECT child.id, child.parent_id, child.name, child.kind, child.unit_sequence, "
            "tree.depth + 1, tree.sort_path || '/' || printf('%020d', child.created_sequence) "
            "FROM nodes AS child JOIN tree ON child.parent_id = tree.id"
            ") "
            f"SELECT tree.depth, tree.name, tree.kind, {value_expression} "
            "FROM tree LEFT JOIN units ON units.sequence = tree.unit_sequence "
            f"{target_join}"
            "WHERE tree.depth > 0 ORDER BY tree.sort_path",
            parameters,
        )
        for raw_depth, raw_name, raw_kind, raw_value in cursor:
            yield (
                cast("int", raw_depth),
                cast("str", raw_name),
                cast("int", raw_kind),
                cast("str | None", raw_value),
            )

    def _assign_path(self, path: tuple[str, ...], unit_sequence: int) -> None:
        _validate_path(path)
        parent_id = _ROOT_NODE_ID
        for index, name in enumerate(path):
            is_value = index == len(path) - 1
            row = self._connection.execute(
                "SELECT id, kind FROM nodes WHERE parent_id = ? AND name = ?",
                (parent_id, name),
            ).fetchone()
            if row is None:
                self._node_sequence += 1
                cursor = self._connection.execute(
                    "INSERT INTO nodes (parent_id, name, kind, created_sequence, unit_sequence) VALUES (?, ?, ?, ?, ?)",
                    (
                        parent_id,
                        name,
                        _VALUE_NODE if is_value else _BRANCH_NODE,
                        self._node_sequence,
                        unit_sequence if is_value else None,
                    ),
                )
                parent_id = cast("int", cursor.lastrowid)
                continue

            node_id = cast("int", row[0])
            kind = cast("int", row[1])
            if is_value:
                if kind == _BRANCH_NODE:
                    self._delete_descendants(node_id)
                self._connection.execute(
                    "UPDATE nodes SET kind = ?, unit_sequence = ? WHERE id = ?",
                    (_VALUE_NODE, unit_sequence, node_id),
                )
            elif kind == _VALUE_NODE:
                self._connection.execute(
                    "UPDATE nodes SET kind = ?, unit_sequence = NULL WHERE id = ?",
                    (_BRANCH_NODE, node_id),
                )
            parent_id = node_id

    def _delete_descendants(self, node_id: int) -> None:
        self._connection.execute(
            "WITH RECURSIVE descendants(id) AS ("
            "SELECT id FROM nodes WHERE parent_id = ? "
            "UNION ALL SELECT nodes.id FROM nodes JOIN descendants ON nodes.parent_id = descendants.id"
            ") DELETE FROM nodes WHERE id IN (SELECT id FROM descendants)",
            (node_id,),
        )


def export_json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    _export_json_i18n(
        prepare_export_document(document, resolve_placeholders=resolve_placeholders),
        filepath,
        nested,
        None,
    )


def _export_json_i18n(
    document: Structure,
    filepath: str | Path,
    nested: bool,
    cancellation: threading.Event | None,
) -> None:
    path = Path(filepath)
    target_locales = _document_target_locales(document)
    is_multitarget = document.target_locale is None and len(target_locales) > 1
    if is_multitarget and path.suffix:
        raise ValueError("JSON i18n export needs a target locale or directory output for multi-target documents")

    selected_locale = document.target_locale or (target_locales[0] if target_locales else None)
    output_names = _locale_output_names(target_locales) if is_multitarget else {}
    if not nested and not is_multitarget:
        _write_flat_items(document, path, selected_locale, cancellation)
        return

    indexed_locales = target_locales if is_multitarget else ((selected_locale,) if selected_locale is not None else ())
    with _JsonExportSpool(nested=nested, target_locales=indexed_locales) as spool:
        items = iter(_iter_items(document))
        try:
            for key, unit in items:
                raise_if_cancelled(cancellation)
                spool.add(key, unit)
        finally:
            _close_iterator(items)
        spool.finish()
        raise_if_cancelled(cancellation)

        if is_multitarget:
            path.mkdir(parents=True, exist_ok=True)
            for locale in target_locales:
                raise_if_cancelled(cancellation)
                output = path / output_names[locale]
                _write_spooled(spool, output, nested, locale, cancellation)
            return
        _write_spooled(spool, path, nested, selected_locale, cancellation)


async def export_json_i18n_async(
    document: Structure,
    filepath: str | Path,
    nested: bool = True,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_json_i18n(
            prepare_export_document(document, resolve_placeholders=resolve_placeholders),
            filepath,
            nested,
            cancellation,
        )
    )


def _write_flat_items(
    document: Structure,
    path: Path,
    locale: str | None,
    cancellation: threading.Event | None,
) -> None:
    items = iter(_iter_items(document))
    try:
        _write_flat_rows(path, _iter_flat_rows(items, locale), cancellation)
    finally:
        _close_iterator(items)


def _iter_flat_rows(items: Iterator[tuple[str, Data]], locale: str | None) -> Iterator[tuple[str, str]]:
    for key, unit in items:
        yield key, target_text(unit, locale) or unit.source


def _write_spooled(
    spool: _JsonExportSpool,
    path: Path,
    nested: bool,
    locale: str | None,
    cancellation: threading.Event | None,
) -> None:
    if nested:
        _write_nested_rows(path, spool.nested_rows(locale), cancellation)
    else:
        _write_flat_rows(path, spool.flat_rows(locale), cancellation)


def _write_flat_rows(
    path: Path,
    rows: Iterable[tuple[str, str]],
    cancellation: threading.Event | None,
) -> None:
    with atomic_output_path(path, "wb", cancellation=cancellation) as output:
        output.write(b"{")
        wrote_row = False
        for key, value in rows:
            raise_if_cancelled(cancellation)
            output.write(b",\n" if wrote_row else b"\n")
            output.write(b"  ")
            _write_json_string(output, key)
            output.write(b": ")
            _write_json_string(output, value)
            wrote_row = True
        output.write(b"\n}\n" if wrote_row else b"}\n")


def _write_nested_rows(
    path: Path,
    rows: Iterable[tuple[int, str, int, str | None]],
    cancellation: threading.Event | None,
) -> None:
    with atomic_output_path(path, "wb", cancellation=cancellation) as output:
        output.write(b"{")
        # One boolean per open object records whether it already has a child.
        object_children = [False]
        for depth, name, kind, value in rows:
            raise_if_cancelled(cancellation)
            while len(object_children) > depth:
                had_children = object_children.pop()
                if had_children:
                    output.write(b"\n" + (b"  " * len(object_children)))
                output.write(b"}")
            if depth != len(object_children):
                raise ValueError("Invalid JSON i18n path ordering in export spool")
            if object_children[-1]:
                output.write(b",")
            output.write(b"\n" + (b"  " * depth))
            _write_json_string(output, name)
            output.write(b": ")
            object_children[-1] = True
            if kind == _BRANCH_NODE:
                output.write(b"{")
                object_children.append(False)
            elif kind == _VALUE_NODE and value is not None:
                _write_json_string(output, value)
            else:
                raise ValueError("Invalid value node in JSON i18n export spool")

        while len(object_children) > 1:
            had_children = object_children.pop()
            if had_children:
                output.write(b"\n" + (b"  " * len(object_children)))
            output.write(b"}")
        output.write(b"\n}\n" if object_children[0] else b"}\n")


def _write_json_string(output: BinaryIO, value: str) -> None:
    # json.dumps duplicates the complete string. Escape fixed-size pieces so a
    # single large translation does not require another whole-value allocation.
    output.write(b'"')
    for start in range(0, len(value), _JSON_WRITE_CHUNK_CHARS):
        encoded = json.dumps(
            value[start : start + _JSON_WRITE_CHUNK_CHARS],
            ensure_ascii=False,
        )
        output.write(encoded[1:-1].encode("utf-8"))
    output.write(b'"')


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _close_iterator(items: Iterator[tuple[str, Data]]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_Closable", candidate).close()


def _document_target_locales(document: Structure) -> tuple[str, ...]:
    raw_locales = document.target_locales
    if not raw_locales and document.target_locale is not None:
        raw_locales = (document.target_locale,)
    if len(raw_locales) > _MAX_TARGET_LOCALES:
        raise ValueError(f"JSON i18n export supports at most {_MAX_TARGET_LOCALES} target locales")
    locales = tuple(dict.fromkeys(raw_locales))
    return locales


def _locale_output_names(locales: tuple[str, ...]) -> dict[str, str]:
    try:
        return dict(locale_output_names(locales, suffix=".json"))
    except LocaleFilenameError as exc:
        if exc.reason == FILENAME_COLLISION:
            raise ValueError("Target locales produce colliding JSON filenames") from exc
        raise ValueError(f"Unsafe target locale for JSON filename: {exc.locale!r}") from exc


def _unit_path(key: str, unit: Data) -> tuple[str, ...]:
    raw_path = unit.extensions.get("json_path")
    if raw_path:
        decoded = json.loads(raw_path)
        if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
            return tuple(decoded)
    return tuple(key.split("."))


def _validate_path(path: tuple[str, ...]) -> None:
    if not path:
        raise ValueError("JSON i18n export paths must contain at least one component")
    if len(path) > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON path exceeds the {_MAX_JSON_DEPTH}-level safety limit")
    for component in path:
        if len(component) > _MAX_PATH_COMPONENT_CHARS:
            raise ValueError("JSON path component exceeds the 64-million-character safety limit")
