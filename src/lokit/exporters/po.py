from __future__ import annotations

import json
import re
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

import polib

from lokit.data.structure import (
    BaseStructure,
    Data,
    PluralCategory,
    StreamingStructure,
    TranslationStatus,
)
from lokit.data.targets import StreamingTargetSplit, target_status, target_text
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import (
    AsyncExportCancelled,
    atomic_output_path,
    raise_if_cancelled,
    run_cancellable_export,
)
from lokit.io.filenames import (
    FILENAME_COLLISION,
    TOO_MANY_OUTPUTS,
    LocaleFilenameError,
    locale_output_names,
)
from lokit.messages import gettext_category_indexes, gettext_plural_forms

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable, Iterator, Mapping
    from types import TracebackType
    from typing import BinaryIO


Structure = BaseStructure | StreamingStructure

_PLURAL_SUFFIX_PATTERN = re.compile(r"^(.*)\[(\d+)\]$")
_NPLURALS_PATTERN = re.compile(r"^nplurals\s*=\s*(\d+)\s*$")
_LOKIT_UNIT_ID_COMMENT_PREFIX = "lokit-unit-id-v1:"
_SINGULAR_ENTRY = 0
_PLURAL_ENTRY = 1
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_METADATA_FIELDS = 4096
_MAX_PO_ENTRY_BYTES = 16 * 1024 * 1024
_MAX_REPEATED_FIELDS = 65_536
_MAX_PLURAL_OUTPUT_FORMS = 256
_MAX_GETTEXT_INDEX = (1 << 32) - 1
_WRITE_CHUNK_CHARS = 64 * 1024


class _Closable(Protocol):
    def close(self) -> None: ...


@dataclass(slots=True)
class _HeaderState:
    metadata: dict[str, str]
    preserve_gettext_indexes: bool
    translator_comments: str
    extracted_comments: str
    flags: str
    previous: str

    @classmethod
    def from_document(cls, document: Structure) -> _HeaderState:
        imported_metadata = _metadata_from_extensions(document.extensions)
        metadata = imported_metadata.copy()
        metadata["Content-Type"] = "text/plain; charset=UTF-8"
        metadata["Content-Transfer-Encoding"] = "8bit"
        if document.target_locale:
            metadata["Language"] = document.target_locale
            metadata.setdefault("Plural-Forms", gettext_plural_forms(document.target_locale))
        if document.export_origin:
            metadata["X-Generator"] = document.export_origin
        _validate_metadata(metadata)
        return cls(
            metadata=metadata,
            preserve_gettext_indexes="Plural-Forms" in imported_metadata,
            translator_comments=_extension_alias(
                document.extensions,
                (
                    "po_header_translator_comments",
                    "property.x_po_header_translator_comments",
                ),
            )
            or "",
            extracted_comments=_extension_alias(
                document.extensions,
                (
                    "po_header_extracted_comments",
                    "property.x_po_header_extracted_comments",
                ),
            )
            or "",
            flags=_extension_alias(
                document.extensions,
                ("po_header_flags", "property.x_po_header_flags"),
            )
            or "",
            previous=_extension_alias(
                document.extensions,
                ("po_header_previous", "property.x_po_header_previous"),
            )
            or "",
        )

    def merge_gettext_header(self, unit: Data, target_locale: str | None) -> None:
        header_metadata = _gettext_header_metadata(unit, target_locale)
        self.metadata.update(header_metadata)
        self.preserve_gettext_indexes |= "Plural-Forms" in header_metadata
        for comment in unit.comments:
            if comment.extensions.get("po_comment_kind") == "translator":
                self.translator_comments = _append_line_block(self.translator_comments, comment.context)
            else:
                self.extracted_comments = _append_line_block(self.extracted_comments, comment.context)
        extra_flags = unit.extensions.get("flags", "")
        if target_status(unit, target_locale) == TranslationStatus.DRAFT:
            self.flags = _append_line_block(self.flags, "fuzzy")
        if extra_flags:
            self.flags = _append_line_block(self.flags, extra_flags)
        previous = unit.extensions.get("po_previous", "")
        if previous:
            self.previous = _append_line_block(self.previous, previous)
        _validate_metadata(self.metadata)


class _PoExportSpool(AbstractContextManager["_PoExportSpool"]):
    """Disk-backed logical PO entries, including non-adjacent plural forms."""

    def __init__(self, target_locale: str | None) -> None:
        temporary_directory = TemporaryDirectory(prefix="lokit-po-export-")
        try:
            connection = sqlite3.connect(Path(temporary_directory.name) / "entries.sqlite3")
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute(
                "CREATE TABLE entries ("
                "sequence INTEGER PRIMARY KEY, group_key TEXT UNIQUE, kind INTEGER NOT NULL, "
                "msgctxt TEXT, msgid TEXT NOT NULL, msgid_plural TEXT NOT NULL, "
                "msgstr TEXT NOT NULL, base_target TEXT NOT NULL, comment TEXT NOT NULL, "
                "tcomment TEXT NOT NULL, previous_msgctxt TEXT, previous_msgid TEXT, "
                "previous_msgid_plural TEXT, obsolete INTEGER NOT NULL"
                ")"
            )
            connection.execute(
                "CREATE TABLE flags ("
                "entry_sequence INTEGER NOT NULL, position INTEGER NOT NULL, value TEXT NOT NULL, "
                "PRIMARY KEY (entry_sequence, position)"
                ") WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE occurrences ("
                "entry_sequence INTEGER NOT NULL, position INTEGER NOT NULL, "
                "path TEXT NOT NULL, line TEXT NOT NULL, "
                "PRIMARY KEY (entry_sequence, position)"
                ") WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE plural_forms ("
                "entry_sequence INTEGER NOT NULL, form_sequence INTEGER NOT NULL, "
                "gettext_index INTEGER, category TEXT, suffix_index INTEGER, target TEXT NOT NULL, "
                "PRIMARY KEY (entry_sequence, form_sequence)"
                ") WITHOUT ROWID"
            )
        except BaseException:
            temporary_directory.cleanup()
            raise
        self._temporary_directory = temporary_directory
        self._connection = connection
        self._target_locale = target_locale
        self._entry_sequence = 0
        self._form_sequence = 0

    def __enter__(self) -> _PoExportSpool:
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
            self._temporary_directory.cleanup()

    def add_singular(self, unit_id: str, unit: Data) -> None:
        self._insert_entry(
            _build_entry(unit_id, unit, target_locale=self._target_locale),
            group_key=None,
            kind=_SINGULAR_ENTRY,
            base_target="",
        )

    def add_plural(self, base_id: str, unit_id: str, unit: Data) -> None:
        _validate_field(base_id, "plural group ID")
        variant = unit.extensions.get(
            "po_msgid_plural",
            unit.plural.variant if unit.plural is not None else "",
        )
        _validate_field(variant, "plural source")
        row = self._connection.execute(
            "SELECT sequence FROM entries WHERE group_key = ?",
            (base_id,),
        ).fetchone()
        if row is None:
            entry = _build_entry(
                base_id,
                unit,
                identity_unit_id=unit_id,
                target_locale=self._target_locale,
            )
            entry.msgid_plural = variant
            entry.msgstr = ""
            entry_sequence = self._insert_entry(
                entry,
                group_key=base_id,
                kind=_PLURAL_ENTRY,
                base_target=target_text(unit, self._target_locale) or "",
            )
        else:
            entry_sequence = cast("int", row[0])
            if variant:
                self._connection.execute(
                    "UPDATE entries SET msgid_plural = ? WHERE sequence = ? AND msgid_plural = ''",
                    (variant, entry_sequence),
                )

        gettext_index = _explicit_gettext_index(unit)
        category = unit.plural.category.value if unit.plural is not None and unit.plural.category is not None else None
        suffix = _plural_suffix(unit_id)
        suffix_index = suffix[1] if suffix is not None else None
        self._form_sequence += 1
        self._connection.execute(
            "INSERT INTO plural_forms ("
            "entry_sequence, form_sequence, gettext_index, category, suffix_index, target"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry_sequence,
                self._form_sequence,
                gettext_index,
                category,
                suffix_index,
                target_text(unit, self._target_locale) or "",
            ),
        )

    def finish(self) -> None:
        self._connection.commit()

    def entries(
        self,
        target_locale: str | None,
        *,
        preserve_gettext_indexes: bool,
        nplurals: int | None,
        cancellation: threading.Event | None,
    ) -> Iterator[polib.POEntry]:
        category_indexes = (
            gettext_category_indexes(target_locale)
            if target_locale is not None and not preserve_gettext_indexes
            else {}
        )
        cursor = self._connection.execute(
            "SELECT sequence, kind, msgctxt, msgid, msgid_plural, msgstr, base_target, "
            "comment, tcomment, previous_msgctxt, previous_msgid, previous_msgid_plural, obsolete "
            "FROM entries ORDER BY sequence"
        )
        for raw_row in cursor:
            raise_if_cancelled(cancellation)
            row = cast("tuple[object, ...]", raw_row)
            entry_sequence = cast("int", row[0])
            kind = cast("int", row[1])
            entry = polib.POEntry(
                msgctxt=cast("str | None", row[2]),
                msgid=cast("str", row[3]),
                msgid_plural=cast("str", row[4]),
                msgstr=cast("str", row[5]),
                comment=cast("str", row[7]),
                tcomment=cast("str", row[8]),
                previous_msgctxt=cast("str | None", row[9]),
                previous_msgid=cast("str | None", row[10]),
                previous_msgid_plural=cast("str | None", row[11]),
            )
            entry.obsolete = bool(cast("int", row[12]))
            entry.flags = self._flags(entry_sequence)
            entry.occurrences = self._occurrences(entry_sequence)
            if kind == _PLURAL_ENTRY:
                if not entry.msgid_plural:
                    entry.msgid_plural = entry.msgid
                entry.msgstr_plural = self._plural_translations(
                    entry_sequence,
                    cast("str", row[6]),
                    category_indexes,
                    nplurals,
                    cancellation,
                )
            yield entry

    def _insert_entry(
        self,
        entry: polib.POEntry,
        *,
        group_key: str | None,
        kind: int,
        base_target: str,
    ) -> int:
        optional_fields = _entry_optional_fields(entry)
        _validate_entry(entry, optional_fields=optional_fields)
        _validate_field(base_target, "plural target")
        self._entry_sequence += 1
        sequence = self._entry_sequence
        self._connection.execute(
            "INSERT INTO entries ("
            "sequence, group_key, kind, msgctxt, msgid, msgid_plural, msgstr, base_target, "
            "comment, tcomment, previous_msgctxt, previous_msgid, previous_msgid_plural, obsolete"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                group_key,
                kind,
                optional_fields[0],
                entry.msgid,
                entry.msgid_plural,
                entry.msgstr,
                base_target,
                entry.comment,
                entry.tcomment,
                optional_fields[1],
                optional_fields[2],
                optional_fields[3],
                int(bool(entry.obsolete)),
            ),
        )
        self._connection.executemany(
            "INSERT INTO flags (entry_sequence, position, value) VALUES (?, ?, ?)",
            _iter_flag_rows(sequence, entry.flags),
        )
        self._connection.executemany(
            "INSERT INTO occurrences (entry_sequence, position, path, line) VALUES (?, ?, ?, ?)",
            _iter_occurrence_rows(sequence, entry.occurrences),
        )
        return sequence

    def _flags(self, entry_sequence: int) -> list[str]:
        rows = self._connection.execute(
            "SELECT value FROM flags WHERE entry_sequence = ? ORDER BY position",
            (entry_sequence,),
        )
        return [cast("str", row[0]) for row in rows]

    def _occurrences(self, entry_sequence: int) -> list[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT path, line FROM occurrences WHERE entry_sequence = ? ORDER BY position",
            (entry_sequence,),
        )
        return [(cast("str", row[0]), cast("str", row[1])) for row in rows]

    def _plural_translations(
        self,
        entry_sequence: int,
        base_target: str,
        category_indexes: Mapping[PluralCategory, int],
        nplurals: int | None,
        cancellation: threading.Event | None,
    ) -> dict[int, str]:
        translations = {index: "" for index in category_indexes.values() if nplurals is None or index < nplurals}
        if len(translations) > _MAX_PLURAL_OUTPUT_FORMS:
            raise ValueError(f"PO plural entry exceeds the {_MAX_PLURAL_OUTPUT_FORMS}-form in-memory rendering limit")
        translation_bytes = 0
        if nplurals is None or nplurals > 0:
            translations[0] = base_target
            translation_bytes = _utf8_size(base_target)
        rows = self._connection.execute(
            "SELECT gettext_index, category, suffix_index, target "
            "FROM plural_forms WHERE entry_sequence = ? ORDER BY form_sequence",
            (entry_sequence,),
        )
        for raw_row in rows:
            raise_if_cancelled(cancellation)
            row = cast("tuple[object, ...]", raw_row)
            raw_gettext_index = cast("int | None", row[0])
            raw_category = cast("str | None", row[1])
            suffix_index = cast("int | None", row[2])
            index = raw_gettext_index
            if index is None and raw_category is not None:
                index = category_indexes.get(PluralCategory(raw_category))
            if index is None:
                index = suffix_index
            if index is None or (nplurals is not None and index >= nplurals):
                continue
            if index not in translations and len(translations) >= _MAX_PLURAL_OUTPUT_FORMS:
                raise ValueError(
                    f"PO plural entry exceeds the {_MAX_PLURAL_OUTPUT_FORMS}-form in-memory rendering limit"
                )
            target = cast("str", row[3])
            previous_target = translations.get(index, "")
            translation_bytes -= _utf8_size(previous_target)
            translation_bytes += _utf8_size(target)
            if translation_bytes > _MAX_PO_ENTRY_BYTES:
                raise ValueError(f"PO plural translations exceed the {_MAX_PO_ENTRY_BYTES}-byte rendering limit")
            translations[index] = target
        return translations


def _iter_flag_rows(entry_sequence: int, flags: Iterable[str]) -> Iterator[tuple[int, int, str]]:
    for position, value in enumerate(flags):
        yield entry_sequence, position, value


def _iter_occurrence_rows(
    entry_sequence: int,
    occurrences: Iterable[tuple[str, str]],
) -> Iterator[tuple[int, int, str, str]]:
    for position, (path, line) in enumerate(occurrences):
        yield entry_sequence, position, path, line


def export_po(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    _export_po(
        prepare_export_document(
            document,
            resolve_placeholders=resolve_placeholders,
        ),
        filepath,
        None,
    )


async def export_po_async(
    document: Structure,
    filepath: str | Path,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_po(
            prepare_export_document(
                document,
                resolve_placeholders=resolve_placeholders,
            ),
            filepath,
            cancellation,
        )
    )


def _export_po(
    document: Structure,
    filepath: str | Path,
    cancellation: threading.Event | None,
) -> None:
    try:
        _export_po_impl(document, Path(filepath), cancellation)
    except AsyncExportCancelled:
        return


def _export_po_impl(
    document: Structure,
    path: Path,
    cancellation: threading.Event | None,
) -> None:
    raise_if_cancelled(cancellation)
    if document.target_locale is None and document.target_locales:
        if path.suffix:
            raise ValueError("PO export needs a selected target locale for a single .po path")
        output_names = _target_output_names(document.target_locales)
        path.mkdir(parents=True, exist_ok=True)
        if isinstance(document, BaseStructure):
            # Public entry points project BaseStructure to a stream first. This
            # branch keeps the internal helper sound for direct callers.
            projected = prepare_export_document(document, resolve_placeholders=True)
        else:
            projected = document
        with StreamingTargetSplit(projected, _cancellation=cancellation) as target_documents:
            for locale, target_document in target_documents.items():
                raise_if_cancelled(cancellation)
                _export_po_impl(
                    target_document,
                    path / output_names[locale],
                    cancellation,
                )
        return

    header = _HeaderState.from_document(document)
    with _PoExportSpool(document.target_locale) as spool:
        items = iter(_iter_items(document))
        try:
            for unit_id, unit in items:
                raise_if_cancelled(cancellation)
                if _is_gettext_header(unit):
                    header.merge_gettext_header(unit, document.target_locale)
                    continue
                if unit.plural is None:
                    spool.add_singular(unit_id, unit)
                    continue
                entry_index = unit.extensions.get("po_entry_index")
                suffix = _plural_suffix(unit_id)
                if entry_index is not None:
                    base_id = f"po-entry-{entry_index}"
                elif suffix is not None:
                    base_id = suffix[0]
                else:
                    base_id = unit_id
                spool.add_plural(base_id, unit_id, unit)
        finally:
            _close_iterator(items)
        spool.finish()
        raise_if_cancelled(cancellation)
        nplurals = _gettext_nplurals(header.metadata.get("Plural-Forms"))
        with atomic_output_path(path, "wb", cancellation=cancellation) as output:
            _write_utf8(output, _render_header(header))
            for entry in spool.entries(
                document.target_locale,
                preserve_gettext_indexes=header.preserve_gettext_indexes,
                nplurals=nplurals,
                cancellation=cancellation,
            ):
                raise_if_cancelled(cancellation)
                output.write(b"\n")
                _write_utf8(output, _render_entry(entry))
            raise_if_cancelled(cancellation)


def _build_metadata(document: Structure) -> dict[str, str]:
    return _HeaderState.from_document(document).metadata


def _metadata_from_extensions(extensions: Mapping[str, str]) -> dict[str, str]:
    raw_metadata = _extension_alias(
        extensions,
        ("po_metadata_json", "property.x_po_metadata_json"),
    )
    if not raw_metadata:
        return {}
    if _utf8_size(raw_metadata) > _MAX_METADATA_BYTES:
        raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_BYTES}-byte limit")
    try:
        parsed: object = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    metadata: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            if len(metadata) >= _MAX_METADATA_FIELDS:
                raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_FIELDS}-field limit")
            metadata[key] = value
    _validate_metadata(metadata)
    return metadata


def _render_header(header: _HeaderState) -> str:
    _validate_metadata(header.metadata)
    po: polib.POFile = polib.POFile()
    po.metadata = header.metadata
    entry = po.metadata_as_entry()
    entry.tcomment = header.translator_comments
    entry.comment = header.extracted_comments
    entry.flags = _header_flags(header.flags)
    _apply_previous(entry, header.previous)
    _validate_entry(entry)
    return str(entry)


def _parse_unit_id(unit_id: str) -> tuple[str | None, str]:
    if "\x04" in unit_id:
        context, msgid = unit_id.split("\x04", 1)
        return context, msgid
    return None, unit_id


def _build_entry(
    unit_id: str,
    unit: Data,
    *,
    identity_unit_id: str | None = None,
    target_locale: str | None = None,
) -> polib.POEntry:
    legacy_context, legacy_msgid = _parse_unit_id(unit_id)
    msgctxt = unit.extensions.get("po_msgctxt", legacy_context)
    msgid = unit.extensions.get("po_msgid", unit.source if legacy_context is None else legacy_msgid)
    context_key = _find_context_key(unit)
    if context_key is not None:
        msgctxt = context_key

    entry = polib.POEntry(
        msgid=msgid,
        msgstr=target_text(unit, target_locale) or "",
        msgctxt=msgctxt,
    )
    _apply_comments(entry, unit)
    _apply_flags(entry, unit, target_locale)
    _apply_occurrences(entry, unit)
    _apply_previous(entry, unit.extensions.get("po_previous", ""))
    entry.obsolete = _extension_bool(unit.extensions, "po_obsolete", "obsolete")
    _apply_unit_id_marker(entry, identity_unit_id or unit_id, unit)
    _validate_entry(entry)
    return entry


def _find_context_key(unit: Data) -> str | None:
    for comment in unit.comments:
        if comment.context_key is not None:
            return comment.context_key
    return None


def _apply_comments(entry: polib.POEntry, unit: Data) -> None:
    if len(unit.comments) > _MAX_REPEATED_FIELDS:
        raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-comment limit")
    translator_comments: list[str] = []
    extracted_comments: list[str] = []
    for comment in unit.comments:
        if comment.extensions.get("po_comment_kind") == "translator":
            translator_comments.append(comment.context)
        else:
            extracted_comments.append(comment.context)
    if translator_comments:
        entry.tcomment = "\n".join(translator_comments)
    if extracted_comments:
        entry.comment = "\n".join(extracted_comments)


def _apply_flags(entry: polib.POEntry, unit: Data, target_locale: str | None) -> None:
    flags: list[str] = []
    if target_status(unit, target_locale) == TranslationStatus.DRAFT:
        flags.append("fuzzy")
    extra = unit.extensions.get("flags")
    if extra:
        for raw_flag in _iter_delimited(extra, ","):
            flag = raw_flag.strip()
            if flag and flag != "fuzzy":
                flags.append(flag)
    if len(flags) > _MAX_REPEATED_FIELDS:
        raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-flag limit")
    entry.flags = flags


def _apply_occurrences(entry: polib.POEntry, unit: Data) -> None:
    refs = unit.extensions.get("references")
    if not refs:
        return
    occurrences: list[tuple[str, str]] = []
    for raw_ref in _iter_delimited(refs, ","):
        ref = raw_ref.strip()
        if not ref:
            continue
        if len(occurrences) >= _MAX_REPEATED_FIELDS:
            raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-reference limit")
        if ":" in ref:
            path, line = ref.rsplit(":", 1)
            occurrences.append((path, line))
        else:
            occurrences.append((ref, ""))
    entry.occurrences = occurrences


def _apply_previous(entry: polib.POEntry, raw_previous: str) -> None:
    if not raw_previous:
        return
    current_field = ""
    _validate_field(raw_previous, "previous fields")
    for line_count, raw_line in enumerate(_iter_lines(raw_previous), start=1):
        if line_count > _MAX_REPEATED_FIELDS:
            raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-previous-line limit")
        line = raw_line.strip()
        field = ""
        literal = ""
        for candidate in ("msgctxt", "msgid_plural", "msgid"):
            prefix = f"{candidate} "
            if line.startswith(prefix):
                field = candidate
                literal = line[len(prefix) :].strip()
                break
        if field:
            value = _decode_po_literal(literal)
            current_field = field
            _set_previous(entry, field, value, append=False)
        elif line.startswith('"'):
            if current_field:
                _set_previous(entry, current_field, _decode_po_literal(line), append=True)


def _set_previous(entry: polib.POEntry, field: str, value: str, *, append: bool) -> None:
    if field == "msgctxt":
        previous = _entry_optional_text(entry, "previous_msgctxt")
        entry.previous_msgctxt = (previous or "") + value if append else value
    elif field == "msgid":
        previous = _entry_optional_text(entry, "previous_msgid")
        entry.previous_msgid = (previous or "") + value if append else value
    elif field == "msgid_plural":
        previous = _entry_optional_text(entry, "previous_msgid_plural")
        entry.previous_msgid_plural = (previous or "") + value if append else value


def _decode_po_literal(literal: str) -> str:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        raise ValueError(f"Invalid previous PO string literal: {literal!r}")
    source = literal[1:-1]
    output: list[str] = []
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
    }
    index = 0
    while index < len(source):
        character = source[index]
        index += 1
        if character != "\\":
            output.append(character)
            continue
        if index >= len(source):
            raise ValueError("Previous PO string ends with an escape prefix")
        escaped = source[index]
        index += 1
        replacement = escapes.get(escaped)
        if replacement is not None:
            output.append(replacement)
            continue
        if escaped in "01234567":
            digits = escaped
            while index < len(source) and len(digits) < 3 and source[index] in "01234567":
                digits += source[index]
                index += 1
            output.append(chr(int(digits, 8)))
            continue
        widths = {"x": 2, "u": 4, "U": 8}
        width = widths.get(escaped)
        if width is not None:
            digits = source[index : index + width]
            if len(digits) != width or any(character not in "0123456789abcdefABCDEF" for character in digits):
                raise ValueError(f"Invalid previous PO hexadecimal escape: \\{escaped}{digits}")
            output.append(chr(int(digits, 16)))
            index += width
            continue
        output.append(escaped)
    return "".join(output)


def _apply_unit_id_marker(entry: polib.POEntry, unit_id: str, unit: Data) -> None:
    suffix = _plural_suffix(unit_id) if unit.plural is not None else None
    derived_unit_id = suffix[0] if suffix is not None else unit_id
    preserved_unit_id = unit.extensions.get("lokit_unit_id", derived_unit_id)
    msgctxt = _entry_optional_text(entry, "msgctxt")
    naturally_roundtrips = (
        msgctxt is None and bool(entry.msgid) and _is_xml_1_0(entry.msgid) and preserved_unit_id == entry.msgid
    )
    preserve_unit_id = "lokit_unit_id" in unit.extensions or "po_msgid" not in unit.extensions
    if not preserve_unit_id or naturally_roundtrips:
        return
    preserved_bytes = _utf8_size(preserved_unit_id)
    maximum_id_bytes = (_MAX_PO_ENTRY_BYTES - len(_LOKIT_UNIT_ID_COMMENT_PREFIX)) // 2
    if preserved_bytes > maximum_id_bytes:
        raise ValueError(f"PO unit ID exceeds the {maximum_id_bytes}-byte marker limit")
    marker = _LOKIT_UNIT_ID_COMMENT_PREFIX + preserved_unit_id.encode("utf-8").hex()
    entry.comment = _append_line_block(entry.comment, marker)


def _explicit_gettext_index(unit: Data) -> int | None:
    raw_index = None
    if unit.plural is not None:
        raw_index = unit.plural.extensions.get("gettext_index")
    if raw_index is None:
        raw_index = unit.extensions.get("gettext_index")
    if raw_index is None:
        return None
    try:
        index = int(raw_index)
    except ValueError as error:
        raise ValueError(f"Invalid gettext plural index: {raw_index!r}") from error
    if not 0 <= index <= _MAX_GETTEXT_INDEX:
        raise ValueError(f"Gettext plural index must be between 0 and {_MAX_GETTEXT_INDEX}, got {index}")
    return index


def _plural_suffix(unit_id: str) -> tuple[str, int] | None:
    match = _PLURAL_SUFFIX_PATTERN.fullmatch(unit_id)
    if match is None:
        return None
    index = int(match.group(2))
    if index > _MAX_GETTEXT_INDEX:
        raise ValueError(f"Gettext plural index must not exceed {_MAX_GETTEXT_INDEX}, got {index}")
    return match.group(1), index


def _gettext_nplurals(plural_forms: str | None) -> int | None:
    if not plural_forms:
        return None
    for field in plural_forms.split(";"):
        match = _NPLURALS_PATTERN.fullmatch(field.strip())
        if match is not None:
            return int(match.group(1))
    return None


def _is_gettext_header(unit: Data) -> bool:
    return unit.extensions.get("xliff_restype") == "x-gettext-domain-header"


def _gettext_header_metadata(unit: Data, target_locale: str | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    value = target_text(unit, target_locale) or unit.source
    if _utf8_size(value) > _MAX_METADATA_BYTES:
        raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_BYTES}-byte limit")
    for line in _iter_lines(value):
        key, separator, field_value = line.partition(":")
        if not separator:
            continue
        if len(metadata) >= _MAX_METADATA_FIELDS:
            raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_FIELDS}-field limit")
        metadata[key.strip()] = field_value.strip()
    _validate_metadata(metadata)
    return metadata


def _extension_alias(extensions: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = extensions.get(key)
        if value is not None:
            return value
    return None


def _extension_bool(extensions: Mapping[str, str], *keys: str) -> bool:
    value = _extension_alias(extensions, keys)
    return value is not None and value.strip().casefold() in {"1", "true", "yes", "on"}


def _header_flags(raw_flags: str) -> list[str]:
    if not raw_flags:
        return []
    flags: list[str] = []
    for raw_flag in _iter_lines(raw_flags):
        flag = raw_flag.strip()
        if not flag:
            continue
        if len(flags) >= _MAX_REPEATED_FIELDS:
            raise ValueError(f"PO header exceeds the {_MAX_REPEATED_FIELDS}-flag limit")
        flags.append(flag)
    return flags


def _append_line_block(existing: str, value: str) -> str:
    if not existing:
        return value
    if not value:
        return existing + "\n"
    return existing + "\n" + value


def _target_output_names(locales: tuple[str, ...]) -> dict[str, str]:
    try:
        return dict(locale_output_names(locales, suffix=".po"))
    except LocaleFilenameError as exc:
        if exc.reason == FILENAME_COLLISION:
            raise ValueError("Target locales produce colliding PO output filenames") from exc
        if exc.reason == TOO_MANY_OUTPUTS:
            raise ValueError("PO export supports at most 256 target locales") from exc
        raise ValueError(f"Unsafe target locale for PO output filename: {exc.locale!r}") from exc


def _validate_metadata(metadata: Mapping[str, str]) -> None:
    if len(metadata) > _MAX_METADATA_FIELDS:
        raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_FIELDS}-field limit")
    total_bytes = 0
    for key, value in metadata.items():
        total_bytes += _utf8_size(key) + _utf8_size(value)
        if total_bytes > _MAX_METADATA_BYTES:
            raise ValueError(f"PO metadata exceeds the {_MAX_METADATA_BYTES}-byte limit")


def _entry_optional_text(entry: polib.POEntry, attribute: str) -> str | None:
    """Read a nullable polib field without trusting its inaccurate type stub."""
    value: object = getattr(cast("object", entry), attribute, None)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"PO entry field {attribute!r} must be a string or None")


def _entry_optional_fields(entry: polib.POEntry) -> tuple[str | None, str | None, str | None, str | None]:
    return (
        _entry_optional_text(entry, "msgctxt"),
        _entry_optional_text(entry, "previous_msgctxt"),
        _entry_optional_text(entry, "previous_msgid"),
        _entry_optional_text(entry, "previous_msgid_plural"),
    )


def _validate_entry(
    entry: polib.POEntry,
    *,
    optional_fields: tuple[str | None, str | None, str | None, str | None] | None = None,
) -> None:
    if len(entry.flags) > _MAX_REPEATED_FIELDS:
        raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-flag limit")
    if len(entry.occurrences) > _MAX_REPEATED_FIELDS:
        raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-reference limit")
    msgctxt, previous_msgctxt, previous_msgid, previous_msgid_plural = (
        _entry_optional_fields(entry) if optional_fields is None else optional_fields
    )
    fields = (
        msgctxt or "",
        entry.msgid,
        entry.msgid_plural,
        entry.msgstr,
        entry.comment,
        entry.tcomment,
        previous_msgctxt or "",
        previous_msgid or "",
        previous_msgid_plural or "",
    )
    total_bytes = sum(_utf8_size(value) for value in fields)
    total_bytes += sum(_utf8_size(value) for value in entry.msgstr_plural.values())
    total_bytes += sum(_utf8_size(value) for value in entry.flags)
    total_bytes += sum(_utf8_size(path) + _utf8_size(line) for path, line in entry.occurrences)
    if total_bytes > _MAX_PO_ENTRY_BYTES:
        raise ValueError(f"PO entry exceeds the {_MAX_PO_ENTRY_BYTES}-byte rendering limit")


def _validate_field(value: str, field_name: str) -> None:
    if _utf8_size(value) > _MAX_PO_ENTRY_BYTES:
        raise ValueError(f"PO {field_name} exceeds the {_MAX_PO_ENTRY_BYTES}-byte rendering limit")


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _is_xml_1_0(value: str) -> bool:
    return all(
        character in "\t\n\r"
        or "\u0020" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
        for character in value
    )


def _write_utf8(output: BinaryIO, value: str) -> None:
    for start in range(0, len(value), _WRITE_CHUNK_CHARS):
        output.write(value[start : start + _WRITE_CHUNK_CHARS].encode("utf-8"))


def _render_entry(entry: polib.POEntry) -> str:
    _validate_entry(entry)
    if not entry.obsolete:
        return str(entry)
    entry.obsolete = False
    try:
        active = str(entry)
    finally:
        entry.obsolete = True
    rendered: list[str] = []
    for line in _iter_lines(active):
        if line and not line.startswith("#"):
            rendered.append("#~ " + line)
        else:
            rendered.append(line)
    return "\n".join(rendered)


def _iter_lines(value: str) -> Iterator[str]:
    start = 0
    while True:
        end = value.find("\n", start)
        if end < 0:
            line = value[start:]
            if line.endswith("\r"):
                line = line[:-1]
            yield line
            return
        line = value[start:end]
        if line.endswith("\r"):
            line = line[:-1]
        yield line
        start = end + 1


def _iter_delimited(value: str, delimiter: str) -> Iterator[str]:
    start = 0
    while True:
        end = value.find(delimiter, start)
        if end < 0:
            yield value[start:]
            return
        yield value[start:end]
        start = end + len(delimiter)


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _close_iterator(items: Iterator[tuple[str, Data]]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_Closable", candidate).close()
