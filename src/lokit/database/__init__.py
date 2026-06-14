from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from lokit.db.models import LoadStats, MatchInput, MatchRow

if TYPE_CHECKING:
    from lokit.db.connection import connect as connect
    from lokit.db.connection import connect_sync as connect_sync
    from lokit.db.operations import TranslationMemory as TranslationMemory

__all__ = [
    "LoadStats",
    "MatchInput",
    "MatchRow",
    "TranslationMemory",
    "connect",
    "connect_sync",
]


def __getattr__(name: str) -> object:
    if name in ("connect", "connect_sync"):
        module = importlib.import_module("lokit.db.connection")
        return getattr(module, name)
    if name == "TranslationMemory":
        module = importlib.import_module("lokit.db.operations")
        return getattr(module, name)
    raise AttributeError(name)
