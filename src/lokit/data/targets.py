from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy

from lokit.data.structure import BaseStructure, Data, TargetData, TranslationStatus


def has_targets(data: Data) -> bool:
    return bool(data.targets) or data.target is not None


def get_target(data: Data, locale: str) -> TargetData | None:
    return data.targets.get(locale)


def selected_target(data: Data, locale: str | None) -> TargetData | None:
    if locale is not None:
        found = data.targets.get(locale)
        if found is not None:
            return found
        return None
    if len(data.targets) == 1:
        return next(iter(data.targets.values()))
    if data.target is not None:
        return TargetData(text=data.target, status=data.status)
    return None


def target_text(data: Data, locale: str | None) -> str | None:
    target = selected_target(data, locale)
    if target is not None:
        return target.text
    return data.target


def target_status(data: Data, locale: str | None) -> TranslationStatus:
    target = selected_target(data, locale)
    if target is not None:
        return target.status
    return data.status


def split_targets(document: BaseStructure) -> dict[str, BaseStructure]:
    locales = _document_target_locales(document)
    return {locale: select_target(document, locale) for locale in locales}


def select_target(document: BaseStructure, locale: str) -> BaseStructure:
    data: dict[str, Data] = {}
    for unit_id, unit in document.data.items():
        selected = unit.targets.get(locale)
        cloned = deepcopy(unit)
        cloned.targets = {}
        if selected is not None:
            cloned.target = selected.text
            cloned.status = selected.status
            cloned.plural = selected.plural or cloned.plural
            cloned.meta = selected.meta
            cloned.comments = list(selected.comments) or cloned.comments
            cloned.extensions = {**cloned.extensions, **selected.extensions}
        data[unit_id] = cloned

    return BaseStructure(
        source_locale=document.source_locale,
        target_locale=locale,
        data=data,
        target_locales=(locale,),
        format_version=document.format_version,
        export_origin=document.export_origin,
        export_timestamp=document.export_timestamp,
        source_language=document.source_language,
        target_language=_base_language(locale),
        target_languages=(_base_language(locale),),
        extensions=document.extensions.copy(),
    )


def _document_target_locales(document: BaseStructure) -> tuple[str, ...]:
    if document.target_locales:
        return document.target_locales
    if document.target_locale is not None:
        return (document.target_locale,)
    seen: list[str] = []
    for unit in document.data.values():
        _extend_unique(seen, unit.targets)
    return tuple(seen)


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _base_language(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()
