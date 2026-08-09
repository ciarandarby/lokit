from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lokit.database import TranslationMemory

import pytest

import lokit.database as database
import lokit.database.schema as schema
import lokit.database.serialization as serialization
from lokit.data.structure import Data, StreamingStructure
from lokit.database import (
    DATABASE_SCHEMA_VERSION,
    SerializedUnit,
    database_schema_statements,
    iter_serialized_units,
)
from lokit.db.schema import CURRENT_VERSION


def test_database_public_api_is_explicit_and_stable() -> None:
    assert database.__all__ == [
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
    assert serialization.__all__ == [
        "CommentFetchRow",
        "CommentInsertRow",
        "JsonDict",
        "JsonScalar",
        "JsonValue",
        "PartFetchRow",
        "PartInsertRow",
        "SerializedUnit",
        "TagFetchRow",
        "TagInsertRow",
        "UnitFetchRow",
        "UnitInsertRow",
        "UnitWithChildren",
        "deserialize_unit",
        "iter_serialized_units",
        "serialize_unit",
    ]
    assert database.serialize_unit is serialization.serialize_unit
    assert database.deserialize_unit is serialization.deserialize_unit
    assert database.iter_serialized_units is serialization.iter_serialized_units
    assert database.database_schema_statements is schema.database_schema_statements
    assert schema.__all__ == ["DATABASE_SCHEMA_VERSION", "database_schema_statements"]


def test_iter_serialized_units_consumes_stream_lazily() -> None:
    consumed: list[str] = []

    def items() -> Iterator[tuple[str, Data]]:
        consumed.append("started")
        yield "hello", Data(source="Hello", target="Bonjour")

    document = StreamingStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        items=items(),
    )
    serialized = iter_serialized_units(document, project="checkout", domain="web")

    assert consumed == []
    first: SerializedUnit = next(serialized)
    assert consumed == ["started"]
    assert first.unit.unit_key == "hello"
    assert first.unit.project == "checkout"
    assert first.unit.domain == "web"
    assert first.unit.source_text == "Hello"
    assert first.unit.target_text == "Bonjour"


def test_database_schema_statements_are_ordered_and_alembic_ready() -> None:
    statements = database_schema_statements()

    assert DATABASE_SCHEMA_VERSION == CURRENT_VERSION
    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
    assert statements[1] == "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
    assert statements[2].startswith("CREATE TABLE IF NOT EXISTS _lokit_meta")
    assert "PARTITION BY LIST (source_locale)" in "\n".join(statements)
    assert statements[-1].startswith("INSERT INTO _lokit_meta")
    assert all(statement.endswith(";") for statement in statements)
    assert all(statement.count(";") == 1 for statement in statements)

    recorded: list[str] = []
    for statement in statements:
        recorded.append(statement)
    assert tuple(recorded) == statements


def test_database_schema_options_omit_extensions_and_partitioning() -> None:
    statements = database_schema_statements(
        partitioned=False,
        include_extensions=False,
    )
    sql = "\n".join(statements)

    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS _lokit_meta")
    assert "CREATE EXTENSION" not in sql
    assert "PARTITION BY LIST" not in sql
    assert "CREATE TABLE IF NOT EXISTS translation_units" in sql
    assert "('partitioned', 'false')" in statements[-1]


@pytest.mark.asyncio
async def test_database_schema_statements_execute_on_postgresql(
    tm: TranslationMemory,
    pg_uri: str | None,
) -> None:
    assert pg_uri is not None
    import psycopg

    async with (
        await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        for statement in database_schema_statements():
            await cursor.execute(statement)
        await cursor.execute("SELECT value FROM _lokit_meta WHERE key = 'schema_version'")
        row = await cursor.fetchone()

    assert row is not None
    assert str(row[0]) == str(DATABASE_SCHEMA_VERSION)


def test_legacy_database_module_forwards_exports_lazily() -> None:
    script = """
import sys
import lokit.db as database

assert "lokit.db.models" not in sys.modules
assert "lokit.db.serialization" not in sys.modules
assert "lokit.db.connection" not in sys.modules
assert "lokit.db.operations" not in sys.modules
_ = database.serialize_unit
assert "lokit.db.models" in sys.modules
assert "lokit.db.serialization" in sys.modules
assert "lokit.db.connection" not in sys.modules
assert "lokit.db.operations" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
