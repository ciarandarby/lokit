"""Compatibility database bootstrap; prefer :mod:`lokit.database`."""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lokit.database import (
        CommentFetchRow,
        CommentInsertRow,
        JsonDict,
        JsonScalar,
        JsonValue,
        LoadStats,
        MatchInput,
        MatchRow,
        PartFetchRow,
        PartInsertRow,
        SerializedUnit,
        TagFetchRow,
        TagInsertRow,
        TranslationMemory,
        TranslationMemoryStream,
        UnitFetchRow,
        UnitInsertRow,
        UnitWithChildren,
        connect,
        connect_sync,
        database_schema_statements,
        deserialize_unit,
        iter_serialized_units,
        serialize_unit,
    )
    from lokit.database.schema import DATABASE_SCHEMA_VERSION

__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "CommentFetchRow",
    "CommentInsertRow",
    "JsonDict",
    "JsonScalar",
    "JsonValue",
    "LoadStats",
    "MatchInput",
    "MatchRow",
    "PartFetchRow",
    "PartInsertRow",
    "SerializedUnit",
    "TagFetchRow",
    "TagInsertRow",
    "TranslationMemory",
    "TranslationMemoryStream",
    "UnitFetchRow",
    "UnitInsertRow",
    "UnitWithChildren",
    "connect",
    "connect_sync",
    "database_schema_statements",
    "deserialize_unit",
    "iter_serialized_units",
    "serialize_unit",
]

_MODEL_NAMES = frozenset(
    {
        "CommentFetchRow",
        "CommentInsertRow",
        "JsonDict",
        "JsonScalar",
        "JsonValue",
        "LoadStats",
        "MatchInput",
        "MatchRow",
        "PartFetchRow",
        "PartInsertRow",
        "SerializedUnit",
        "TagFetchRow",
        "TagInsertRow",
        "UnitFetchRow",
        "UnitInsertRow",
        "UnitWithChildren",
    }
)
_SERIALIZATION_NAMES = frozenset({"deserialize_unit", "iter_serialized_units", "serialize_unit"})
_SCHEMA_NAMES = frozenset({"DATABASE_SCHEMA_VERSION", "database_schema_statements"})


def __getattr__(name: str) -> object:
    if name in _MODEL_NAMES:
        return getattr(importlib.import_module("lokit.db.models"), name)
    if name in _SERIALIZATION_NAMES:
        return getattr(importlib.import_module("lokit.db.serialization"), name)
    if name in _SCHEMA_NAMES:
        return getattr(importlib.import_module("lokit.database.schema"), name)
    if name in {"connect", "connect_sync"}:
        return getattr(importlib.import_module("lokit.db.connection"), name)
    if name in {"TranslationMemory", "TranslationMemoryStream"}:
        return getattr(importlib.import_module("lokit.db.operations"), name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    return list(__all__)
