from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import BaseStructure, Data
from lokit.db.matching import canonical_match_text
from lokit.db.operations import _bounded_reindex_keys, _candidate_match_limit, _deduplicate_batch
from lokit.db.queries import MATCH_QUERY
from lokit.db.schema import CURRENT_VERSION, MIGRATIONS, schema_for_partitioning
from lokit.db.serialization import serialize_unit

if TYPE_CHECKING:
    from lokit.database import TranslationMemory


def test_database_schema_has_parallel_canonical_match_indexes() -> None:
    sql = schema_for_partitioning(partitioned=False)

    assert CURRENT_VERSION == 2
    assert "source_text TEXT NOT NULL" in sql
    assert "source_match_text TEXT NOT NULL" in sql
    assert "placeholder_signature TEXT NOT NULL" in sql
    assert "placeholder_index_version SMALLINT NOT NULL DEFAULT 1" in sql
    assert "idx_tu_match_source_trgm" in sql
    assert "idx_tu_match_exact" in sql
    assert "idx_tu_match_ice" in sql
    assert "md5(source_match_text)" in sql
    assert "uq_translation_units_dedup" in sql
    assert "source_hash," in sql


def test_database_v2_migration_is_resumable_and_index_versioned() -> None:
    migration = MIGRATIONS[2]

    assert "ADD COLUMN IF NOT EXISTS source_match_text" in migration
    assert "ADD COLUMN IF NOT EXISTS placeholder_signature" in migration
    assert "placeholder_index_version SMALLINT NOT NULL DEFAULT 0" in migration
    assert "WHERE placeholder_index_version < 1" in migration
    assert "GENERATED ALWAYS" not in migration


def test_placeholder_reindex_selection_obeys_byte_and_row_order() -> None:
    rows: list[tuple[object, ...]] = [
        ("first", "en", 7),
        ("second", "en", 5),
        ("third", "fr", 2),
    ]

    ids, locales = _bounded_reindex_keys(rows, max_batch_bytes=10)

    assert ids == ["first"]
    assert locales == ["en"]

    with pytest.raises(ValueError, match="placeholder index limit"):
        _bounded_reindex_keys([("oversized", "en", 65 * 1024 * 1024)], 16 * 1024 * 1024)


def test_database_candidate_safety_margin_is_bounded() -> None:
    assert _candidate_match_limit(1) == 17
    assert _candidate_match_limit(10_000) == 11_000


def test_database_match_query_uses_canonical_fields_and_signature() -> None:
    assert "md5(tu.source_match_text) = p.source_match_hash" in MATCH_QUERY
    assert "tu.source_match_text = p.source_match_text" in MATCH_QUERY
    assert "tu.source_match_text %% p.source_match_text" in MATCH_QUERY
    assert "tu.placeholder_signature = p.placeholder_signature" in MATCH_QUERY
    assert "tu.source_text %%" not in MATCH_QUERY


def test_serialization_preserves_raw_identity_beside_canonical_match_text() -> None:
    first = serialize_unit(
        "first",
        Data(source="Hello {name}", target="Bonjour"),
        "en",
        "fr",
    )
    second = serialize_unit(
        "second",
        Data(source="Hello {customer}", target="Bonjour"),
        "en",
        "fr",
    )

    assert first.unit.source_text == "Hello {name}"
    assert second.unit.source_text == "Hello {customer}"
    assert first.unit.source_match_text == second.unit.source_match_text
    assert first.unit.placeholder_signature == second.unit.placeholder_signature
    # Canonical equivalence must not merge distinct raw source identities.
    assert len(_deduplicate_batch([first, second])) == 2


@pytest.mark.asyncio
async def test_database_matches_and_reforms_placeholder_names(
    tm: TranslationMemory,
    pg_uri: str | None,
) -> None:
    assert pg_uri is not None
    await tm.load(
        BaseStructure(
            source_locale="en",
            target_locale="fr",
            data={
                "route": Data(
                    source="From {origin} to {destination}",
                    target="De {destination} à {origin}; {origin}",
                )
            },
        ),
        progress=False,
    )

    result = (
        await tm.match(
            source="From {start} to {end}",
            source_locale="en",
            target_locale="fr",
            limit=1,
        )
    )[0]

    assert result.kind == "exact"
    assert result.translation == "De {end} à {start}; {start}"
    assert result.placeholders_reformed
    assert result.can_apply

    import psycopg

    canonical = canonical_match_text("From {start} to {end}")
    async with (
        await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute("SET enable_seqscan = off")
        await cursor.execute(
            """
            EXPLAIN (COSTS OFF)
            SELECT unit_key
            FROM translation_units
            WHERE source_locale = %s
              AND target_locale = %s
              AND md5(source_match_text) = md5(%s)
              AND placeholder_signature = %s
            """,
            ("en", "fr", canonical.text, canonical.signature),
        )
        plan = "\n".join(str(row[0]) for row in await cursor.fetchall())

    assert "Index" in plan
    assert "md5(source_match_text)" in plan


@pytest.mark.asyncio
async def test_setup_executes_v2_migration_and_bounded_reindex(
    tm: TranslationMemory,
    pg_uri: str | None,
) -> None:
    assert pg_uri is not None
    import psycopg

    async with (
        await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute("DROP TABLE IF EXISTS translation_units CASCADE")
        await cursor.execute("DROP TABLE IF EXISTS _lokit_meta CASCADE")
        await cursor.execute(
            """
            CREATE TABLE translation_units (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source_locale TEXT NOT NULL,
                target_locale TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL,
                previous_source TEXT NOT NULL DEFAULT '',
                next_source TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await cursor.execute(
            """
            CREATE TABLE _lokit_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await cursor.execute(
            """
            INSERT INTO _lokit_meta (key, value) VALUES
                ('schema_version', '1'),
                ('partitioned', 'false')
            """
        )
        await cursor.execute(
            """
            INSERT INTO translation_units (source_locale, source_text) VALUES
                ('en', 'Hello {name}'),
                ('en', 'Files: %1$s'),
                ('de', 'Willkommen {person}')
            """
        )

    await tm.setup(partitioned=False)

    async with (
        await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute("SELECT value FROM _lokit_meta WHERE key = 'schema_version'")
        version_row = await cursor.fetchone()
        await cursor.execute(
            """
            SELECT source_text, source_match_text, placeholder_signature, placeholder_index_version
            FROM translation_units
            ORDER BY source_locale, source_text
            """
        )
        rows = await cursor.fetchall()
        await cursor.execute(
            """
            UPDATE translation_units
            SET source_match_text = '', placeholder_signature = '', placeholder_index_version = 0
            """
        )

    assert version_row is not None
    assert str(version_row[0]) == "2"
    assert len(rows) == 3
    assert all(str(row[1]) for row in rows)
    assert all(str(row[2]).startswith("v1|") for row in rows)
    assert all(int(str(row[3])) == 1 for row in rows)

    updated = await tm.reindex_placeholders(batch_size=1)
    assert updated == 3
