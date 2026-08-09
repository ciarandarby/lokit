"""Public translation-memory database API."""

from lokit.database.schema import DATABASE_SCHEMA_VERSION, database_schema_statements
from lokit.db.connection import connect, connect_sync
from lokit.db.models import (
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
    UnitFetchRow,
    UnitInsertRow,
    UnitWithChildren,
)
from lokit.db.operations import TranslationMemory, TranslationMemoryStream
from lokit.db.serialization import deserialize_unit, iter_serialized_units, serialize_unit

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
