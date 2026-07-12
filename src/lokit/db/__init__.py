"""Compatibility database bootstrap; prefer :mod:`lokit.database`."""

import importlib

from lokit.db.models import LoadStats, MatchInput, MatchRow

__all__ = ["LoadStats", "MatchInput", "MatchRow", "TranslationMemory", "connect", "connect_sync"]


def __getattr__(name: str) -> object:
    if name in {"connect", "connect_sync"}:
        return getattr(importlib.import_module("lokit.db.connection"), name)
    if name == "TranslationMemory":
        return getattr(importlib.import_module("lokit.db.operations"), name)
    raise AttributeError(name)
