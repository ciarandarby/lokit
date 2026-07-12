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
    from collections.abc import AsyncIterator, Iterator

ExtractItem = tuple[str, Data]
class PoImportMode(StrEnum):
    GETTEXT = "gettext"
    SOURCE = "source"
    TARGET_AS_SOURCE = "target_as_source"

class PoExtractor:
    def __init__(
        self,
        filepath: str,
        source_locale: str = "",
        target_locale: str | None = None,
        mode: PoImportMode = PoImportMode.GETTEXT,
    ) -> None:
        self.filepath = filepath
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.mode = mode
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
    ) -> Iterator[ExtractItem]:
        return project_items(
            self._extract(),
            include_tags=include_tags,
            tag_syntax=tag_syntax,
            native_syntax=TagSyntax.HTML,
            unsupported_tags=unsupported_tags,
        )

    def _extract(self) -> Iterator[ExtractItem]:
        metadata_read = False
        for entry in iter_po_entries(self.filepath):
            if entry.msgid == "" and not metadata_read:
                self._read_metadata(metadata_from_header(entry))
                metadata_read = True
                continue
            if not metadata_read:
                self._read_metadata({})
                metadata_read = True
            if entry.obsolete != 0:
                continue

            if entry.msgid_plural:
                yield from self._extract_plural(entry)
            else:
                yield self._extract_singular(entry)

    def extract_async(
        self,
        *,
        include_tags: bool = False,
        tag_syntax: TagSyntax = TagSyntax.NATIVE,
        unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    ) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(
            lambda: self.extract(
                include_tags=include_tags,
                tag_syntax=tag_syntax,
                unsupported_tags=unsupported_tags,
            )
        )

    def _read_metadata(self, metadata: dict[str, str]) -> None:
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

    def _extract_singular(self, entry: PoEntryRecord) -> ExtractItem:
        unit_id = self._unit_id(entry)
        source = self._source_text(entry)
        target = self._target_text(entry)
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry)
        data = Data(
            source=source,
            target=target,
            meta=Meta(),
            status=status,
            comments=comments,
            extensions=extensions,
        )
        return unit_id, data

    def _extract_plural(self, entry: PoEntryRecord) -> Iterator[ExtractItem]:
        unit_id = self._unit_id(entry)
        plural_dict: dict[int, str] = entry.msgstr_plural or {}
        base_target = None if self.mode is PoImportMode.SOURCE else plural_dict.get(0) or None
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry)
        data = Data(
            source=entry.msgid,
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
            plural_target = plural_dict[n] if plural_dict[n] else None
            if self.mode is PoImportMode.SOURCE:
                plural_target = None
            plural_data = Data(
                source=entry.msgid,
                target=plural_target,
                plural=Plural(
                    variant=entry.msgid_plural,
                    category=self._category_from_index(n),
                    extensions={"gettext_index": str(n)},
                ),
                meta=Meta(),
                status=self._plural_form_status(plural_target, entry),
                comments=[],
                extensions=extensions.copy(),
            )
            yield f"{unit_id}[{n}]", plural_data

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

    def _unit_id(self, entry: PoEntryRecord) -> str:
        if entry.msgctxt:
            return f"{entry.msgctxt}\x04{entry.msgid}"
        return str(entry.msgid)

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
        if entry.comment:
            comments.append(
                Comment(
                    context=entry.comment,
                    context_key=entry.msgctxt or None,
                )
            )
        if entry.tcomment:
            comments.append(Comment(context=entry.tcomment))
        return comments

    def _extensions(self, entry: PoEntryRecord) -> dict[str, str]:
        extensions: dict[str, str] = {}
        if entry.occurrences:
            refs = ", ".join(f"{path}:{line}" for path, line in entry.occurrences)
            extensions["references"] = refs
        non_fuzzy = [f for f in entry.flags if f != "fuzzy"]
        if non_fuzzy:
            extensions["flags"] = ", ".join(non_fuzzy)
        return extensions

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
