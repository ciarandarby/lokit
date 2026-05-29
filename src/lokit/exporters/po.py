from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any

import polib

from lokit.data.structure import BaseStructure, Data, TranslationStatus

_PLURAL_SUFFIX_PATTERN = "["


def export_po(document: BaseStructure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    po: Any = polib.POFile()
    po.metadata = _build_metadata(document)

    plural_groups: dict[str, list[tuple[str, Data]]] = defaultdict(list)
    singular_units: list[tuple[str, Data]] = []

    for unit_id, unit in document.data.items():
        if _PLURAL_SUFFIX_PATTERN in unit_id and unit.plural is not None:
            base_id = unit_id[: unit_id.index(_PLURAL_SUFFIX_PATTERN)]
            plural_groups[base_id].append((unit_id, unit))
        elif unit.plural is not None:
            plural_groups[unit_id].append((unit_id, unit))
        else:
            singular_units.append((unit_id, unit))

    for unit_id, unit in singular_units:
        po.append(_build_entry(unit_id, unit))

    for base_id, forms in plural_groups.items():
        po.append(_build_plural_entry(base_id, forms))

    po.save(str(path))


async def export_po_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_po, document, filepath)


def _build_metadata(document: BaseStructure) -> dict[str, str]:
    meta: dict[str, str] = {
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
    }
    if document.target_locale:
        meta["Language"] = document.target_locale
    if document.export_origin:
        meta["X-Generator"] = document.export_origin
    return meta


def _parse_unit_id(unit_id: str) -> tuple[str | None, str]:
    if "\x04" in unit_id:
        ctx, msgid = unit_id.split("\x04", 1)
        return ctx, msgid
    return None, unit_id


def _build_entry(unit_id: str, unit: Data) -> Any:
    msgctxt, msgid = _parse_unit_id(unit_id)
    context_key = _find_context_key(unit)
    if context_key is not None:
        msgctxt = context_key

    entry: Any = polib.POEntry(
        msgid=msgid,
        msgstr=unit.target or "",
        msgctxt=msgctxt,
    )

    _apply_comments(entry, unit)
    _apply_flags(entry, unit)
    _apply_occurrences(entry, unit)
    return entry


def _build_plural_entry(
    base_id: str, forms: list[tuple[str, Data]]
) -> Any:
    msgctxt, msgid = _parse_unit_id(base_id)
    base_unit = forms[0][1]
    context_key = _find_context_key(base_unit)
    if context_key is not None:
        msgctxt = context_key

    variant = base_unit.plural.variant if base_unit.plural else msgid

    msgstr_plural: dict[int, str] = {}
    msgstr_plural[0] = base_unit.target or ""

    for uid, unit in forms:
        if _PLURAL_SUFFIX_PATTERN in uid:
            idx_str = uid[uid.index(_PLURAL_SUFFIX_PATTERN) + 1 : uid.rindex("]")]
            idx = int(idx_str)
            msgstr_plural[idx] = unit.target or ""

    entry: Any = polib.POEntry(
        msgid=msgid,
        msgid_plural=variant,
        msgstr_plural=msgstr_plural,
        msgctxt=msgctxt,
    )

    _apply_comments(entry, base_unit)
    _apply_flags(entry, base_unit)
    _apply_occurrences(entry, base_unit)
    return entry


def _find_context_key(unit: Data) -> str | None:
    for comment in unit.comments:
        if comment.context_key is not None:
            return comment.context_key
    return None


def _apply_comments(entry: Any, unit: Data) -> None:
    translator_comments: list[str] = []
    extracted_comments: list[str] = []
    for i, comment in enumerate(unit.comments):
        if i == 0 and comment.context_key is not None:
            translator_comments.append(comment.context)
        elif i == 0:
            translator_comments.append(comment.context)
        else:
            extracted_comments.append(comment.context)
    if translator_comments:
        entry.comment = "\n".join(translator_comments)
    if extracted_comments:
        entry.tcomment = "\n".join(extracted_comments)


def _apply_flags(entry: Any, unit: Data) -> None:
    flags: list[str] = []
    if unit.status == TranslationStatus.DRAFT:
        flags.append("fuzzy")
    extra = unit.extensions.get("flags")
    if extra:
        flags.extend(f.strip() for f in extra.split(","))
    entry.flags = flags


def _apply_occurrences(entry: Any, unit: Data) -> None:
    refs = unit.extensions.get("references")
    if not refs:
        return
    occurrences: list[tuple[str, str]] = []
    for ref in refs.split(","):
        ref = ref.strip()
        if ":" in ref:
            path, line = ref.rsplit(":", 1)
            occurrences.append((path, line))
        else:
            occurrences.append((ref, ""))
    entry.occurrences = occurrences
