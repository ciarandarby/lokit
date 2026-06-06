import importlib

from lokit.db.models import LoadStats, MatchInput, MatchRow

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
