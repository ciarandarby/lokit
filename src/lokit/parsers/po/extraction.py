from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from lokit.compat import StrEnum
from lokit.data.structure import (
    Comment,
    Data,
    Meta,
    Plural,
    PluralCategory,
    TranslationStatus,
)
from lokit.messages import GettextPluralRule, parse_gettext_plural_forms, plural_category
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.po.stream import PoEntryRecord, iter_po_entries, metadata_from_header
from lokit.parsers.projection import project_items
from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from lokit.placeholders import PlaceholderSyntax

ExtractItem = tuple[str, Data]
_PO_ID_MEMORY_COUNT = 4096
_PO_ID_MEMORY_BYTES = 2 * 1024 * 1024


class _PoIdIndex(AbstractContextManager["_PoIdIndex"]):
    """Exact bounded ID registry with a small-memory fast path."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._id_bytes = 0
        self._directory: TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _PoIdIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        connection = self._connection
        self._connection = None
        directory = self._directory
        self._directory = None
        try:
            if connection is not None:
                connection.close()
        finally:
            if directory is not None:
                directory.cleanup()
        self._ids.clear()
        self._id_bytes = 0

    def unique(self, preferred: str) -> str:
        if self._insert(preferred):
            return preferred
        while True:
            suffix = self._next_suffix(preferred)
            candidate = f"{preferred}#{suffix}"
            if self._insert(candidate):
                return candidate

    def _insert(self, value: str) -> bool:
        connection = self._connection
        if connection is None:
            if value in self._ids:
                return False
            value_bytes = len(value.encode("utf-8"))
            if len(self._ids) < _PO_ID_MEMORY_COUNT and self._id_bytes + value_bytes <= _PO_ID_MEMORY_BYTES:
                self._ids[value] = 2
                self._id_bytes += value_bytes
                return True
            connection = self._spill()
        try:
            connection.execute(
                "INSERT INTO ids (value, next_suffix) VALUES (?, 2)",
                (value,),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def _next_suffix(self, value: str) -> int:
        connection = self._connection
        if connection is None:
            suffix = self._ids.get(value)
            if suffix is None:
                raise RuntimeError("PO unit ID registry lost its base entry")
            self._ids[value] = suffix + 1
            return suffix
        row = connection.execute(
            "SELECT next_suffix FROM ids WHERE value = ?",
            (value,),
        ).fetchone()
        if row is None:
            raise RuntimeError("PO unit ID registry lost its base entry")
        suffix = cast("int", row[0])
        connection.execute(
            "UPDATE ids SET next_suffix = ? WHERE value = ?",
            (suffix + 1, value),
        )
        return suffix

    def _spill(self) -> sqlite3.Connection:
        directory = TemporaryDirectory(prefix="lokit-po-ids-")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                Path(directory.name) / "ids.sqlite3",
                check_same_thread=False,
            )
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("CREATE TABLE ids (value TEXT PRIMARY KEY, next_suffix INTEGER NOT NULL) WITHOUT ROWID")
            connection.executemany(
                "INSERT INTO ids (value, next_suffix) VALUES (?, ?)",
                self._ids.items(),
            )
        except BaseException:
            if connection is not None:
                connection.close()
            directory.cleanup()
            raise
        if connection is None:
            raise RuntimeError("could not open PO unit ID spill database")
        self._ids.clear()
        self._id_bytes = 0
        self._directory = directory
        self._connection = connection
        return connection


class PoImportMode(StrEnum):
    AUTO = "auto"
    GETTEXT = "gettext"
    MSGID_AS_SOURCE = "msgid_as_source"
    SOURCE = "source"
    TARGET_AS_SOURCE = "target_as_source"
    MSGID_AS_ID = "msgid_as_id"


def normalize_po_import_mode(mode: PoImportMode | str) -> PoImportMode:
    value = mode.value if isinstance(mode, PoImportMode) else mode
    aliases = {
        PoImportMode.MSGID_AS_SOURCE.value: PoImportMode.GETTEXT,
        PoImportMode.MSGID_AS_ID.value: PoImportMode.TARGET_AS_SOURCE,
    }
    alias = aliases.get(value)
    return alias if alias is not None else PoImportMode(value)


class PoExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        mode: PoImportMode = PoImportMode.AUTO,
    ) -> None:
        self.filepath = filepath
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.mode = PoImportMode.SOURCE if mode is PoImportMode.AUTO and filepath.lower().endswith(".pot") else mode
        if self.mode is PoImportMode.AUTO:
            self.mode = PoImportMode.GETTEXT
        self.source_language: str | None = None
        self.target_language: str | None = None
        self.export_origin = ""
        self.extensions: dict[str, str] = {"input_format": "po"}
        self._plural_rule: GettextPluralRule | None = None
        self._plural_category_cache: dict[int, PluralCategory | None] = {}

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
        with _PoIdIndex() as id_index:
            metadata_read = False
            entry_index = 0
            for entry in iter_po_entries(self.filepath):
                if entry.msgid == "" and not metadata_read:
                    self._read_metadata(metadata_from_header(entry), entry)
                    metadata_read = True
                    continue
                if not metadata_read:
                    self._read_metadata({})
                    metadata_read = True
                if entry.obsolete != 0:
                    continue

                if entry.msgid_plural:
                    yield from self._extract_plural(entry, entry_index, id_index)
                else:
                    yield self._extract_singular(entry, entry_index, id_index)
                entry_index += 1

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

    def _read_metadata(self, metadata: dict[str, str], entry: PoEntryRecord | None = None) -> None:
        if metadata:
            self.extensions["po_metadata_json"] = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        lang = metadata.get("Language", "")
        if lang and not self.target_locale and self.mode is PoImportMode.GETTEXT:
            self.target_locale = lang
        if self.target_locale:
            self.target_language = self._base_language(self.target_locale)
        if self.source_locale:
            self.source_language = self._base_language(self.source_locale)
        self.export_origin = metadata.get("X-Generator", "")
        plural_forms = metadata.get("Plural-Forms")
        if plural_forms:
            self._plural_rule = parse_gettext_plural_forms(plural_forms)
        if entry is not None:
            if entry.tcomment_lines:
                self.extensions["po_header_translator_comments"] = "\n".join(entry.tcomment_lines)
            if entry.comment_lines:
                self.extensions["po_header_extracted_comments"] = "\n".join(entry.comment_lines)
            if entry.flags:
                self.extensions["po_header_flags"] = "\n".join(entry.flags)
            if entry.previous:
                self.extensions["po_header_previous"] = "\n".join(entry.previous)

    def _extract_singular(
        self,
        entry: PoEntryRecord,
        entry_index: int,
        id_index: _PoIdIndex,
    ) -> ExtractItem:
        unit_id = self._unit_id(entry, entry_index, id_index)
        source = self._source_text(entry)
        target = self._target_text(entry)
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry, entry_index)
        data = Data(
            source=source,
            target=target,
            meta=Meta(),
            status=status,
            comments=comments,
            extensions=extensions,
        )
        return unit_id, data

    def _extract_plural(
        self,
        entry: PoEntryRecord,
        entry_index: int,
        id_index: _PoIdIndex,
    ) -> Iterator[ExtractItem]:
        unit_id = self._unit_id(entry, entry_index, id_index)
        plural_dict: dict[int, str] = entry.msgstr_plural or {}
        base_translation = plural_dict.get(0, "")
        base_source = self._plural_source(entry, 0, base_translation)
        base_target = base_translation or None if self.mode is PoImportMode.GETTEXT else None
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry, entry_index)
        extensions["gettext_index"] = "0"
        data = Data(
            source=base_source,
            target=base_target,
            plural=Plural(
                variant=entry.msgid_plural,
                category=self._category_from_index(0),
                extensions={"gettext_index": "0"},
            ),
            meta=Meta(),
            status=status,
            comments=comments,
            extensions=extensions,
        )
        yield unit_id, data

        for n in sorted(plural_dict):
            if n == 0:
                continue
            translation = plural_dict[n]
            plural_target = translation or None if self.mode is PoImportMode.GETTEXT else None
            plural_extensions = extensions.copy()
            plural_extensions["gettext_index"] = str(n)
            plural_data = Data(
                source=self._plural_source(entry, n, translation),
                target=plural_target,
                plural=Plural(
                    variant=entry.msgid_plural,
                    category=self._category_from_index(n),
                    extensions={"gettext_index": str(n)},
                ),
                meta=Meta(),
                status=self._plural_form_status(plural_target, entry),
                comments=[],
                extensions=plural_extensions,
            )
            yield id_index.unique(f"{unit_id}[{n}]"), plural_data

    def _category_from_index(self, index: int) -> PluralCategory | None:
        if index in self._plural_category_cache:
            return self._plural_category_cache[index]
        if self._plural_rule is None:
            return None
        locale = self.target_locale if self.mode is PoImportMode.GETTEXT else self.source_locale
        if not locale:
            return None
        categories: set[PluralCategory] = set()
        sample_values = (*range(0, 1001), 10_000, 100_000, 1_000_000)
        for value in sample_values:
            if self._plural_rule.index(value) == index:
                categories.add(plural_category(locale, value))
        category = next(iter(categories)) if len(categories) == 1 else None
        self._plural_category_cache[index] = category
        return category

    def _unit_id(self, entry: PoEntryRecord, entry_index: int, id_index: _PoIdIndex) -> str:
        if entry.msgctxt or not _is_xml_1_0(entry.msgid):
            return id_index.unique(f"po-{entry_index}")
        return id_index.unique(str(entry.msgid))

    def _status(self, entry: PoEntryRecord) -> TranslationStatus:
        if self.mode is PoImportMode.SOURCE:
            return TranslationStatus.NEW
        if "fuzzy" in entry.flags:
            return TranslationStatus.DRAFT
        target = entry.msgstr if not entry.msgid_plural else (entry.msgstr_plural or {}).get(0, "")
        if target:
            return TranslationStatus.TRANSLATED
        return TranslationStatus.NEW

    def _source_text(self, entry: PoEntryRecord) -> str:
        if self.mode is PoImportMode.TARGET_AS_SOURCE and entry.msgstr:
            return entry.msgstr
        return entry.msgid

    def _target_text(self, entry: PoEntryRecord) -> str | None:
        if self.mode is not PoImportMode.GETTEXT:
            return None
        return entry.msgstr if entry.msgstr else None

    def _plural_form_status(self, target: str | None, entry: PoEntryRecord) -> TranslationStatus:
        if "fuzzy" in entry.flags:
            return TranslationStatus.DRAFT
        if target:
            return TranslationStatus.TRANSLATED
        return TranslationStatus.NEW

    def _comments(self, entry: PoEntryRecord) -> list[Comment]:
        comments: list[Comment] = []
        for context in entry.tcomment_lines:
            comments.append(
                Comment(
                    context=context,
                    extensions={"po_comment_kind": "translator"},
                )
            )
        for context in entry.comment_lines:
            comments.append(
                Comment(
                    context=context,
                    context_key=entry.msgctxt or None,
                    extensions={"po_comment_kind": "extracted"},
                )
            )
        return comments

    def _extensions(self, entry: PoEntryRecord, entry_index: int) -> dict[str, str]:
        extensions: dict[str, str] = {
            "po_entry_index": str(entry_index),
            "po_msgid": entry.msgid,
        }
        if entry.msgctxt is not None:
            extensions["po_msgctxt"] = entry.msgctxt
        if entry.msgid_plural:
            extensions["po_msgid_plural"] = entry.msgid_plural
        if entry.occurrences:
            refs = ", ".join(f"{path}:{line}" for path, line in entry.occurrences)
            extensions["references"] = refs
        non_fuzzy = [f for f in entry.flags if f != "fuzzy"]
        if non_fuzzy:
            extensions["flags"] = ", ".join(non_fuzzy)
        if entry.previous:
            extensions["po_previous"] = "\n".join(entry.previous)
        return extensions

    def _plural_source(self, entry: PoEntryRecord, index: int, translation: str) -> str:
        original = entry.msgid if index == 0 else entry.msgid_plural
        if self.mode is PoImportMode.TARGET_AS_SOURCE and translation:
            return translation
        return original

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()


def _is_xml_1_0(value: str) -> bool:
    return all(
        character in "\t\n\r"
        or "\u0020" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
        for character in value
    )
