from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
        self._used_unit_ids: set[str] = set()
        self._next_unit_suffix: dict[str, int] = {}

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
                yield from self._extract_plural(entry, entry_index)
            else:
                yield self._extract_singular(entry, entry_index)
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

    def _extract_singular(self, entry: PoEntryRecord, entry_index: int) -> ExtractItem:
        unit_id = self._unit_id(entry, entry_index)
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

    def _extract_plural(self, entry: PoEntryRecord, entry_index: int) -> Iterator[ExtractItem]:
        unit_id = self._unit_id(entry, entry_index)
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
            yield self._unique_unit_id(f"{unit_id}[{n}]"), plural_data

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

    def _unit_id(self, entry: PoEntryRecord, entry_index: int) -> str:
        if entry.msgctxt or not _is_xml_1_0(entry.msgid):
            return self._unique_unit_id(f"po-{entry_index}")
        return self._unique_unit_id(str(entry.msgid))

    def _unique_unit_id(self, preferred: str) -> str:
        if preferred not in self._used_unit_ids:
            self._used_unit_ids.add(preferred)
            self._next_unit_suffix.setdefault(preferred, 2)
            return preferred
        suffix = self._next_unit_suffix.get(preferred, 2)
        while True:
            candidate = f"{preferred}#{suffix}"
            suffix += 1
            if candidate not in self._used_unit_ids:
                self._next_unit_suffix[preferred] = suffix
                self._used_unit_ids.add(candidate)
                self._next_unit_suffix.setdefault(candidate, 2)
                return candidate

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
