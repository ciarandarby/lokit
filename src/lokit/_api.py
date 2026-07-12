from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lokit.logic import Lokit as Lokit

PUBLIC_NAMES = ["Lokit", "async_", "convert", "database", "parse", "stream", "types", "write"]

_PUBLIC_MODULES: frozenset[str] = frozenset({"async_", "convert", "database", "parse", "stream", "types", "write"})
_LEGACY_MODULES: frozenset[str] = frozenset({"db", "exporters", "io", "office", "parsers", "quick_parse"})
_LEGACY_TYPES: dict[str, tuple[str, str]] = {
    "AdjacentContext": ("lokit.data.structure", "AdjacentContext"),
    "BaseStructure": ("lokit.data.structure", "BaseStructure"),
    "CodePart": ("lokit.data.structure", "CodePart"),
    "Comment": ("lokit.data.structure", "Comment"),
    "ConversionStats": ("lokit.data.structure", "ConversionStats"),
    "Data": ("lokit.data.structure", "Data"),
    "MatchResult": ("lokit.logic", "MatchResult"),
    "Meta": ("lokit.data.structure", "Meta"),
    "Origin": ("lokit.data.structure", "Origin"),
    "Plural": ("lokit.data.structure", "Plural"),
    "PluralCategory": ("lokit.data.structure", "PluralCategory"),
    "SegmentPart": ("lokit.data.structure", "SegmentPart"),
    "StreamingStructure": ("lokit.data.structure", "StreamingStructure"),
    "Tags": ("lokit.data.structure", "Tags"),
    "TargetData": ("lokit.data.structure", "TargetData"),
    "TargetTags": ("lokit.data.structure", "TargetTags"),
    "TextPart": ("lokit.data.structure", "TextPart"),
    "TieData": ("lokit.data.tag_types", "TieData"),
    "TieType": ("lokit.data.tag_types", "TieType"),
    "TranslationStatus": ("lokit.data.structure", "TranslationStatus"),
}


def get_attribute(name: str) -> object:
    if name == "Lokit":
        value = getattr(importlib.import_module("lokit.logic"), name)
    elif name in _PUBLIC_MODULES or name in _LEGACY_MODULES:
        value = importlib.import_module(f"lokit.{name}")
    elif name in _LEGACY_TYPES:
        module_name, attribute = _LEGACY_TYPES[name]
        value = getattr(importlib.import_module(module_name), attribute)
    else:
        raise AttributeError(name)
    return value


def public_dir() -> list[str]:
    return list(PUBLIC_NAMES)
