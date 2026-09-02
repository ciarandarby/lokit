from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import BaseStructure, Data, Meta
from lokit.db.matching import canonical_match_text
from lokit.db.operations import (
    _bounded_reindex_keys,
    _BoundedMatchResults,
    _candidate_match_limit,
    _deduplicate_batch,
    _match_page_size,
)
from lokit.db.queries import FUZZY_MATCH_QUERY, ICE_MATCH_QUERY, MATCH_QUERY
from lokit.db.schema import CURRENT_VERSION, MIGRATIONS, schema_for_partitioning
from lokit.db.serialization import serialize_unit
from lokit.types.match import MatchResult

if TYPE_CHECKING:
    from lokit.database import TranslationMemory
    from lokit.db.models import MatchInput


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
    assert _match_page_size(1) == 17
    assert _match_page_size(10_000) == 512


def test_database_match_retention_scans_beyond_unsafe_fallback_margin() -> None:
    retained = _BoundedMatchResults(limit=1)
    unsafe = MatchResult(
        unit_id="unsafe",
        score=1.0,
        kind="exact",
        source_equal=True,
        tags_equal=True,
        previous_equal=True,
        next_equal=True,
    )
    safe = MatchResult(
        unit_id="safe",
        score=0.9,
        kind="fuzzy",
        source_equal=False,
        tags_equal=True,
        previous_equal=True,
        next_equal=True,
        translation="Safe target",
        can_apply=True,
    )

    retained.add([unsafe] * (_candidate_match_limit(1) + 5))

    assert not retained.full
    assert len(retained._fallback) == _candidate_match_limit(1)

    retained.add([safe])

    assert retained.finish() == [safe]


def test_database_match_query_uses_canonical_fields_and_signature() -> None:
    assert "md5(tu.source_match_text) = p.source_match_hash" in MATCH_QUERY
    assert "tu.source_match_text = p.source_match_text" in MATCH_QUERY
    assert "tu.placeholder_signature = p.placeholder_signature" in MATCH_QUERY
    assert "tu.source_match_text %% p.source_match_text" in FUZZY_MATCH_QUERY
    assert "tu.placeholder_signature = p.placeholder_signature" in FUZZY_MATCH_QUERY
    assert "NOT EXISTS (SELECT 1 FROM exact)" not in FUZZY_MATCH_QUERY
    assert "match_context_hash" in ICE_MATCH_QUERY
    assert "lower(tu.previous_source) = lower(p.previous_source)" in ICE_MATCH_QUERY
    assert "c.id::uuid DESC" in MATCH_QUERY
    assert "c.id::uuid DESC" in ICE_MATCH_QUERY
    assert "c.id::uuid DESC" in FUZZY_MATCH_QUERY
    assert "tu.source_text %%" not in FUZZY_MATCH_QUERY


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
async def test_database_batch_match_validates_public_bounds(
    tm: TranslationMemory,
) -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await tm.match_batch([], limit=0, progress=False)
    with pytest.raises(ValueError, match="threshold must be between"):
        await tm.match_batch([], threshold=1.1, progress=False)


@pytest.mark.asyncio
async def test_database_exact_match_pages_past_unsafe_placeholder_targets(
    tm: TranslationMemory,
) -> None:
    unsafe_count = _match_page_size(1) + 5
    data = {
        "safe-exact": Data(
            source="Paged exact {stored}",
            target="Cible {stored}",
            meta=Meta(usage_count=0),
        ),
        **{
            f"unsafe-exact-{index}": Data(
                source=f"Paged exact {{source_{index}}}",
                target=f"Cible incorrecte {{target_{index}}}",
                meta=Meta(usage_count=100 + index),
            )
            for index in range(unsafe_count)
        },
    }
    await tm.load(
        BaseStructure(source_locale="en", target_locale="fr", data=data),
        progress=False,
    )

    results = await tm.match(
        source="Paged exact {query}",
        source_locale="en",
        target_locale="fr",
        limit=1,
    )

    assert len(results) == 1
    assert results[0].unit_id == "safe-exact"
    assert results[0].translation == "Cible {query}"
    assert results[0].can_apply


@pytest.mark.asyncio
async def test_database_unsafe_exact_rows_do_not_suppress_safe_fuzzy_pipeline_match(
    tm: TranslationMemory,
    pg_uri: str | None,
) -> None:
    assert pg_uri is not None
    unsafe_count = _match_page_size(1) + 5
    data = {
        **{
            f"unsafe-exact-{index}": Data(
                source=f"Paged source {{source_{index}}}",
                target=f"Cible incorrecte {{target_{index}}}",
                meta=Meta(usage_count=100 + index),
            )
            for index in range(unsafe_count)
        },
        "safe-fuzzy": Data(
            source="Paged source {stored}!",
            target="Cible sûre {stored}",
        ),
    }
    await tm.load(
        BaseStructure(source_locale="en", target_locale="fr", data=data),
        progress=False,
    )

    from lokit.db.connection import connect

    memory = await connect(pg_uri, pool_size=1, min_size=1, pipeline=True)
    inputs: list[MatchInput] = [
        {
            "source": "Paged source {query}",
            "source_locale": "en",
            "target_locale": "fr",
        }
    ]
    async with memory:
        batches = await memory.match_batch(inputs, limit=1, threshold=0.3, progress=False)

    assert len(batches) == 1
    assert len(batches[0]) == 1
    assert batches[0][0].unit_id == "safe-fuzzy"
    assert batches[0][0].translation == "Cible sûre {query}"
    assert batches[0][0].kind == "fuzzy"
    assert batches[0][0].can_apply


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
