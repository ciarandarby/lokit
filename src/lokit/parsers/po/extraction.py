from __future__ import annotations

from typing import AsyncIterator, Iterator, Protocol, cast

import polib

from lokit.data.structure import (
    Comment,
    Data,
    Meta,
    Plural,
    PluralCategory,
    TranslationStatus,
)
from lokit.parsers.async_bridge import AsyncExtractionBridge

ExtractItem = tuple[str, Data]
PoOccurrences = list[tuple[str, str]]


class PoEntryLike(Protocol):
    obsolete: int
    msgctxt: str | None
    msgid: str
    msgid_plural: str
    msgstr: str
    msgstr_plural: dict[int, str]
    flags: list[str]
    comment: str
    tcomment: str
    occurrences: PoOccurrences


class PoFileLike(Protocol):
    metadata: dict[str, str]

    def __iter__(self) -> Iterator[PoEntryLike]: ...

_PLURAL_CATEGORIES: tuple[PluralCategory, ...] = (
    PluralCategory.ONE,
    PluralCategory.TWO,
    PluralCategory.FEW,
    PluralCategory.MANY,
    PluralCategory.OTHER,
)


def _category_from_index(index: int) -> PluralCategory:
    if index < len(_PLURAL_CATEGORIES):
        return _PLURAL_CATEGORIES[index]
    return PluralCategory.OTHER


class PoExtractor:
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
        self.extensions: dict[str, str] = {"input_format": "po"}

    def extract(self) -> Iterator[ExtractItem]:
        po = cast(PoFileLike, polib.pofile(self.filepath))
        self._read_metadata(po)

        for entry in po:
            if entry.obsolete != 0:
                continue

            if entry.msgid_plural:
                yield from self._extract_plural(entry)
            else:
                yield self._extract_singular(entry)

    def extract_async(self) -> AsyncIterator[ExtractItem]:
        return AsyncExtractionBridge(self.extract)

    def _read_metadata(self, po: PoFileLike) -> None:
        metadata: dict[str, str] = po.metadata or {}
        lang = metadata.get("Language", "")
        if lang and not self.target_locale:
            self.target_locale = lang
        if self.target_locale:
            self.target_language = self._base_language(self.target_locale)
        if self.source_locale:
            self.source_language = self._base_language(self.source_locale)
        self.export_origin = metadata.get("X-Generator", "")

    def _extract_singular(self, entry: PoEntryLike) -> ExtractItem:
        unit_id = self._unit_id(entry)
        target = entry.msgstr if entry.msgstr else None
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry)
        data = Data(
            source=entry.msgid,
            target=target,
            meta=Meta(),
            status=status,
            comments=comments,
            extensions=extensions,
        )
        return unit_id, data

    def _extract_plural(self, entry: PoEntryLike) -> Iterator[ExtractItem]:
        unit_id = self._unit_id(entry)
        plural_dict: dict[int, str] = entry.msgstr_plural or {}
        base_target = plural_dict.get(0) or None
        status = self._status(entry)
        comments = self._comments(entry)
        extensions = self._extensions(entry)
        data = Data(
            source=entry.msgid,
            target=base_target,
            plural=Plural(variant=entry.msgid_plural),
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
            plural_data = Data(
                source=entry.msgid,
                target=plural_target,
                plural=Plural(
                    variant=entry.msgid_plural,
                    category=_category_from_index(n),
                ),
                meta=Meta(),
                status=self._plural_form_status(plural_target, entry),
                comments=[],
                extensions=extensions.copy(),
            )
            yield f"{unit_id}[{n}]", plural_data

    def _unit_id(self, entry: PoEntryLike) -> str:
        if entry.msgctxt:
            return f"{entry.msgctxt}\x04{entry.msgid}"
        return str(entry.msgid)

    def _status(self, entry: PoEntryLike) -> TranslationStatus:
        if "fuzzy" in entry.flags:
            return TranslationStatus.DRAFT
        target = entry.msgstr if not entry.msgid_plural else (entry.msgstr_plural or {}).get(0, "")
        if target:
            return TranslationStatus.TRANSLATED
        return TranslationStatus.NEW

    def _plural_form_status(
        self, target: str | None, entry: PoEntryLike
    ) -> TranslationStatus:
        if "fuzzy" in entry.flags:
            return TranslationStatus.DRAFT
        if target:
            return TranslationStatus.TRANSLATED
        return TranslationStatus.NEW

    def _comments(self, entry: PoEntryLike) -> list[Comment]:
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

    def _extensions(self, entry: PoEntryLike) -> dict[str, str]:
        extensions: dict[str, str] = {}
        if entry.occurrences:
            refs = ", ".join(
                f"{path}:{line}" for path, line in entry.occurrences
            )
            extensions["references"] = refs
        non_fuzzy = [f for f in entry.flags if f != "fuzzy"]
        if non_fuzzy:
            extensions["flags"] = ", ".join(non_fuzzy)
        return extensions

    def _base_language(self, locale: str) -> str:
        return locale.replace("_", "-").split("-")[0].lower()
