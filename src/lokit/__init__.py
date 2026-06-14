from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    ConversionStats,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    SegmentPart,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.logic import Lokit, MatchResult

if TYPE_CHECKING:
    from types import ModuleType

    from lokit import database as database
    from lokit import db as db
    from lokit import exporters as exporters
    from lokit import io as io
    from lokit import office as office
    from lokit import parse as parse
    from lokit import parsers as parsers
    from lokit import quick_parse as quick_parse
    from lokit import stream as stream

__all__ = [
    "AdjacentContext",
    "BaseStructure",
    "CodePart",
    "Comment",
    "ConversionStats",
    "Data",
    "Lokit",
    "MatchResult",
    "Meta",
    "Origin",
    "Plural",
    "PluralCategory",
    "SegmentPart",
    "StreamingStructure",
    "Tags",
    "TargetData",
    "TargetTags",
    "TextPart",
    "TieData",
    "TieType",
    "TranslationStatus",
    "database",
    "db",
    "exporters",
    "io",
    "office",
    "parse",
    "parsers",
    "quick_parse",
    "stream",
]

_LAZY_MODULES = {
    "database",
    "db",
    "exporters",
    "io",
    "office",
    "parse",
    "parsers",
    "quick_parse",
    "stream",
}


def __getattr__(name: str) -> ModuleType:
    if name in _LAZY_MODULES:
        return importlib.import_module(f"lokit.{name}")
    raise AttributeError(name)
