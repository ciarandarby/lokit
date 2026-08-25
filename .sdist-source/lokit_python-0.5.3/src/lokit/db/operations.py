from __future__ import annotations

import asyncio
from collections import deque
from json import JSONEncoder
from operator import length_hint
from time import perf_counter
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeVar, cast

from psycopg import AsyncConnection
from psycopg.rows import class_row
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb
from tqdm import tqdm

from lokit.core.logger import logger
from lokit.data.structure import BaseStructure, Data, StreamingStructure, Tags, TargetData, TargetTags
from lokit.db.matching import (
    PLACEHOLDER_INDEX_VERSION,
    TagSignature,
    canonical_match_text,
    rows_to_match_results,
    tag_rows_signature,
    tags_signature_from_tags,
)
from lokit.db.models import (
    CommentFetchRow,
    CommentInsertRow,
    JsonDict,
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
from lokit.db.queries import (
    COUNT_MAPPED_UNITS_QUERY,
    DELETE_MAPPED_COMMENTS_QUERY,
    DELETE_MAPPED_PARTS_QUERY,
    DELETE_MAPPED_STAGED_UNITS_QUERY,
    DELETE_MAPPED_TAGS_QUERY,
    FETCH_COMMENTS_FOR_UNITS_QUERY,
    FETCH_PARTS_FOR_UNITS_QUERY,
    FETCH_TAG_SIGNATURES_QUERY,
    FETCH_TAGS_FOR_UNITS_QUERY,
    FETCH_UNIT_BY_KEY_QUERY,
    FETCH_UNITS_BY_SOURCE_QUERY,
    FETCH_UNITS_BY_SOURCE_TARGETS_QUERY,
    FETCH_UNITS_QUERY,
    INSERT_COMMENTS_QUERY,
    INSERT_PARTS_QUERY,
    INSERT_TAGS_QUERY,
    MAP_EXISTING_UNTRANSLATED_QUERY,
    MAP_LOADED_UNITS_QUERY,
    MATCH_QUERY,
    UPDATE_EXISTING_UNTRANSLATED_QUERY,
    UPSERT_UNITS_QUERY,
)
from lokit.db.schema import (
    CREATE_EXTENSIONS,
    CREATE_META_TABLE,
    CREATE_PLACEHOLDER_INDEXES,
    CREATE_TEMP_COMMENTS,
    CREATE_TEMP_PARTS,
    CREATE_TEMP_TAGS,
    CREATE_TEMP_UNIT_MAP,
    CREATE_TEMP_UNITS,
    CURRENT_VERSION,
    MIGRATIONS,
    partition_name_for_locale,
    schema_for_partitioning,
)
from lokit.db.serialization import deserialize_unit, iter_serialized_units

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from types import TracebackType

    from lokit.db.connection import WriterReaderPool
    from lokit.types.match import MatchResult


_T_co = TypeVar("_T_co", covariant=True)


class _AsyncContext(Protocol[_T_co]):
    async def __aenter__(self) -> _T_co: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _UnitCursor(Protocol):
    async def execute(self, query: str, params: tuple[str, str]) -> object: ...

    def __aiter__(self) -> _UnitCursor: ...

    async def __anext__(self) -> UnitFetchRow: ...


class _UnitConnection(Protocol):
    def cursor(self, *, name: str, row_factory: object) -> object: ...


Structure = BaseStructure | StreamingStructure
Connection: TypeAlias = AsyncConnection[tuple[object, ...]]

_COPY_UNITS = """
COPY tmp_lokit_units (
    load_id,
    id,
    unit_key,
    source_text,
    source_match_text,
    placeholder_signature,
    placeholder_index_version,
    target_text,
    source_locale,
    target_locale,
    status,
    previous_source,
    next_source,
    project,
    domain,
    usage_count,
    plural_variant,
    plural_count,
    plural_category,
    extensions
) FROM STDIN
"""

_FETCH_PLACEHOLDER_REINDEX_BATCH = """
SELECT id::text, source_locale, octet_length(source_text)
FROM translation_units
WHERE placeholder_index_version < 1
ORDER BY source_locale, id
LIMIT %s
FOR UPDATE;
"""

_FETCH_PLACEHOLDER_REINDEX_SOURCES = """
SELECT target.id::text, target.source_locale, target.source_text
FROM unnest(
    %s::uuid[],
    %s::text[]
) WITH ORDINALITY AS requested(id, source_locale, position)
JOIN translation_units AS target
  ON target.id = requested.id
 AND target.source_locale = requested.source_locale
ORDER BY requested.position;
"""

_UPDATE_PLACEHOLDER_REINDEX_BATCH = """
UPDATE translation_units AS target
SET
    source_match_text = incoming.source_match_text,
    placeholder_signature = incoming.placeholder_signature,
    placeholder_index_version = %s
FROM unnest(
    %s::uuid[],
    %s::text[],
    %s::text[],
    %s::text[]
) AS incoming(id, source_locale, source_match_text, placeholder_signature)
WHERE target.id = incoming.id
  AND target.source_locale = incoming.source_locale;
"""

_DEFAULT_REINDEX_BATCH_BYTES = 16 * 1024 * 1024
_MAX_REINDEX_SOURCE_BYTES = 64 * 1024 * 1024
_DEFAULT_LOAD_BATCH_BYTES = 16 * 1024 * 1024
_COMPACT_JSON_ENCODER = JSONEncoder(ensure_ascii=False, separators=(",", ":"))

_COPY_TAGS = """
COPY tmp_lokit_tags (
    load_id,
    source_locale,
    tag_id,
    tag_type,
    position,
    tag_order,
    attribute_data,
    pair_id,
    original_name,
    original_text,
    attributes,
    is_source
) FROM STDIN
"""

_COPY_PARTS = """
COPY tmp_lokit_parts (
    load_id,
    source_locale,
    is_source,
    position,
    part_type,
    value
) FROM STDIN
"""

_COPY_COMMENTS = """
COPY tmp_lokit_comments (
    load_id,
    source_locale,
    context,
    timestamp,
    context_key,
    system,
    project,
    creator_id,
    extensions
) FROM STDIN
"""


class TranslationMemory:
    def __init__(self, pools: WriterReaderPool, pipeline: bool) -> None:
        self._pools = pools
        self._pipeline = pipeline
        self._partitioned = True

    async def __aenter__(self) -> TranslationMemory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def setup(self, partitioned: bool = True) -> None:
        """Asynchronously sets up the database schema and extensions."""
        logger.info("Setting up database schema (partitioned=%s)", partitioned)
        async with self._pools.writer.connection() as conn, conn.cursor() as cur:
            logger.debug("Creating database extensions")
            await cur.execute(CREATE_EXTENSIONS)
            logger.debug("Checking lokit schema metadata")
            await cur.execute(CREATE_META_TABLE)
            await cur.execute("SELECT value FROM _lokit_meta WHERE key = 'schema_version'")
            version_row = await cur.fetchone()
            if version_row is None:
                logger.info("Creating lokit database schema version %d", CURRENT_VERSION)
                await cur.execute(schema_for_partitioning(partitioned))
                await cur.execute(
                    """
                        INSERT INTO _lokit_meta (key, value) VALUES
                            ('schema_version', %s),
                            ('created_at', now()::text),
                            ('partitioned', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                        """,
                    (str(CURRENT_VERSION), "true" if partitioned else "false"),
                )
                self._partitioned = partitioned
            else:
                version = int(str(version_row[0]))
                if version > CURRENT_VERSION:
                    raise RuntimeError(
                        "Database schema is newer than this lokit version. "
                        "Upgrade lokit before using this translation memory."
                    )
                await cur.execute("SELECT value FROM _lokit_meta WHERE key = 'partitioned'")
                partitioned_row = await cur.fetchone()
                existing_partitioned = partitioned_row is None or str(partitioned_row[0]) == "true"
                if existing_partitioned != partitioned:
                    raise RuntimeError(
                        "translation_units already exists with different partitioning. "
                        "Use the existing setup or create a fresh database."
                    )
                self._partitioned = existing_partitioned
                for target_version in range(version + 1, CURRENT_VERSION + 1):
                    migration = MIGRATIONS.get(target_version)
                    if migration is None:
                        raise RuntimeError(f"No database migration is available for version {target_version}")
                    logger.info("Migrating lokit database schema to version %d", target_version)
                    await cur.execute(migration)
                    await cur.execute(
                        """
                        INSERT INTO _lokit_meta (key, value)
                        VALUES ('schema_version', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                        """,
                        (str(target_version),),
                    )

        # Reindexing happens after the DDL transaction and commits each bounded
        # batch independently.  A crash can therefore resume from the row-level
        # version marker without retaining an unbounded transaction or row set.
        await self.reindex_placeholders()
        async with self._pools.writer.connection() as conn, conn.cursor() as cur:
            await cur.execute(CREATE_PLACEHOLDER_INDEXES)
        logger.info("Database schema ready (version %d)", CURRENT_VERSION)

    def setup_sync(self, partitioned: bool = True) -> None:
        """Synchronously sets up the database schema and extensions."""
        asyncio.run(self.setup(partitioned))

    async def reindex_placeholders(
        self,
        *,
        batch_size: int = 1000,
        max_batch_bytes: int = _DEFAULT_REINDEX_BATCH_BYTES,
    ) -> int:
        """Backfill canonical match fields in row- and byte-bounded transactions.

        A single source may exceed ``max_batch_bytes`` but can never exceed the
        native placeholder scanner's 64 MiB input ceiling.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be at least 1")

        updated = 0
        exhausted = False
        while not exhausted:
            async with self._pools.writer.connection() as conn, conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    _FETCH_PLACEHOLDER_REINDEX_BATCH,
                    (batch_size,),
                )
                raw_keys = await cur.fetchall()
                if not raw_keys:
                    exhausted = True
                else:
                    ids, locales = _bounded_reindex_keys(raw_keys, max_batch_bytes)
                    await cur.execute(
                        _FETCH_PLACEHOLDER_REINDEX_SOURCES,
                        (ids, locales),
                    )
                    raw_sources = await cur.fetchall()
                    rows = [(str(row[0]), str(row[1]), str(row[2])) for row in raw_sources]
                    if len(rows) != len(ids):
                        raise RuntimeError("placeholder reindex source rows changed while locked")
                    ids, locales, match_texts, signatures = await asyncio.to_thread(
                        _canonicalize_reindex_batch,
                        rows,
                    )
                    await cur.execute(
                        _UPDATE_PLACEHOLDER_REINDEX_BATCH,
                        (
                            PLACEHOLDER_INDEX_VERSION,
                            ids,
                            locales,
                            match_texts,
                            signatures,
                        ),
                    )
                    updated += len(rows)
        if updated:
            logger.info("Reindexed %d translation-memory placeholder sources", updated)
        return updated

    def reindex_placeholders_sync(
        self,
        *,
        batch_size: int = 1000,
        max_batch_bytes: int = _DEFAULT_REINDEX_BATCH_BYTES,
    ) -> int:
        """Synchronously backfill canonical placeholder match fields."""
        return asyncio.run(
            self.reindex_placeholders(
                batch_size=batch_size,
                max_batch_bytes=max_batch_bytes,
            )
        )

    async def load(
        self,
        document: Structure,
        *,
        batch_size: int = 5000,
        max_batch_bytes: int = _DEFAULT_LOAD_BATCH_BYTES,
        project: str = "",
        domain: str = "",
        progress: bool = True,
    ) -> LoadStats:
        """Asynchronously load translation data in row- and byte-bounded batches.

        ``max_batch_bytes`` measures the UTF-8 and JSON payload retained by a
        serialized batch.  A single unit that exceeds the limit is rejected so
        the byte bound remains strict rather than becoming a best-effort hint.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be at least 1")

        start = perf_counter()
        source_locale = document.source_locale
        units_read = 0
        units_written = 0
        total = len(document.data) if isinstance(document, BaseStructure) else None
        logger.info(
            "Loading data (source_locale=%s, target_locale=%s)",
            source_locale,
            document.target_locale or ",".join(document.target_locales),
        )

        serialized_units = iter_serialized_units(document, project=project, domain=domain)
        try:
            progress_units = tqdm(
                serialized_units,
                total=total,
                desc="Loading TM",
                unit="units",
                disable=not progress,
            )
            try:
                async with self._pools.writer.connection() as conn, conn.transaction():
                    await self._ensure_partition(conn, source_locale)
                    await self._create_temp_tables(conn)
                    for batch in _iter_load_batches(
                        progress_units,
                        batch_size=batch_size,
                        max_batch_bytes=max_batch_bytes,
                    ):
                        units_read += len(batch)
                        deduped = _deduplicate_batch(batch)
                        logger.debug("Flushing batch of %d units", len(deduped))
                        units_written += await self._flush_batch(conn, deduped)
            finally:
                progress_units.close()
        finally:
            serialized_units.close()

        stats = LoadStats(
            units_read=units_read,
            units_written=units_written,
            seconds=perf_counter() - start,
        )
        logger.info(
            "Loaded %d units (%d written) in %.2fs",
            stats.units_read,
            stats.units_written,
            stats.seconds,
        )
        return stats

    def load_sync(
        self,
        document: Structure,
        *,
        batch_size: int = 5000,
        max_batch_bytes: int = _DEFAULT_LOAD_BATCH_BYTES,
        project: str = "",
        domain: str = "",
        progress: bool = True,
    ) -> LoadStats:
        """Synchronously loads translation document data into the translation memory database."""
        return asyncio.run(
            self.load(
                document,
                batch_size=batch_size,
                max_batch_bytes=max_batch_bytes,
                project=project,
                domain=domain,
                progress=progress,
            )
        )

    async def match(
        self,
        *,
        source: str,
        source_locale: str,
        target_locale: str,
        previous_source: str = "",
        next_source: str = "",
        limit: int = 5,
        threshold: float = 0.5,
        source_tags: Tags | None = None,
        tag_signature: TagSignature | None = None,
        require_tags: bool = False,
    ) -> list[MatchResult]:
        """Asynchronously matches a source sequence against translation memory."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")

        signature = tag_signature or (tags_signature_from_tags(source_tags) if source_tags is not None else ())
        require_context = bool(previous_source or next_source)
        logger.debug(
            "Matching source_locale=%s target_locale=%s limit=%d threshold=%.2f",
            source_locale,
            target_locale,
            limit,
            threshold,
        )
        rows = await self._match_rows(
            source,
            source_locale,
            target_locale,
            previous_source,
            next_source,
            limit,
            threshold,
            require_context or require_tags,
            require_context,
        )
        candidate_signatures = (
            await self._candidate_tag_signatures(rows, source_locale) if require_tags and rows else {}
        )
        results = rows_to_match_results(
            rows,
            source,
            previous_source,
            next_source,
            require_context,
            require_tags,
            signature,
            candidate_signatures,
        )[:limit]
        logger.debug("Match returned %d results", len(results))
        return results

    def match_sync(
        self,
        *,
        source: str,
        source_locale: str,
        target_locale: str,
        previous_source: str = "",
        next_source: str = "",
        limit: int = 5,
        threshold: float = 0.5,
        source_tags: Tags | None = None,
        tag_signature: TagSignature | None = None,
        require_tags: bool = False,
    ) -> list[MatchResult]:
        """Synchronously matches a source sequence against translation memory."""
        return asyncio.run(
            self.match(
                source=source,
                source_locale=source_locale,
                target_locale=target_locale,
                previous_source=previous_source,
                next_source=next_source,
                limit=limit,
                threshold=threshold,
                source_tags=source_tags,
                tag_signature=tag_signature,
                require_tags=require_tags,
            )
        )

    async def match_batch(
        self,
        inputs: Iterable[MatchInput],
        *,
        limit: int = 5,
        threshold: float = 0.5,
        progress: bool = True,
    ) -> list[list[MatchResult]]:
        """Asynchronously matches a batch of source sequences against translation memory."""
        results: list[list[MatchResult]] = []
        input_iterator = iter(inputs)
        hinted_total = length_hint(input_iterator)
        total = hinted_total if hinted_total > 0 else None
        logger.debug(
            "Running batch match%s",
            f" for {hinted_total} queries" if total is not None else "",
        )
        if self._pipeline:
            async with self._pools.reader.connection() as conn, conn.pipeline():
                for item in tqdm(
                    input_iterator,
                    total=total,
                    desc="Matching",
                    unit="queries",
                    disable=not progress,
                ):
                    results.append(
                        await self._match_from_input_on_connection(
                            conn,
                            item,
                            limit,
                            threshold,
                        )
                    )
            return results
        for item in tqdm(
            input_iterator,
            total=total,
            desc="Matching",
            unit="queries",
            disable=not progress,
        ):
            results.append(await self._match_from_input(item, limit, threshold))
        return results

    def match_batch_sync(
        self,
        inputs: Iterable[MatchInput],
        *,
        limit: int = 5,
        threshold: float = 0.5,
        progress: bool = True,
    ) -> list[list[MatchResult]]:
        """Synchronously matches a batch of source sequences against translation memory."""
        return asyncio.run(
            self.match_batch(
                inputs,
                limit=limit,
                threshold=threshold,
                progress=progress,
            )
        )

    async def unit(
        self,
        unit_key: str,
        *,
        source_locale: str = "",
        target_locale: str = "",
        include_tags: bool = True,
    ) -> Data:
        """Asynchronously retrieves details for a single segment unit key."""
        async with self._pools.reader.connection() as conn, conn.cursor(row_factory=class_row(UnitFetchRow)) as cur:
            await cur.execute(
                FETCH_UNIT_BY_KEY_QUERY,
                (
                    unit_key,
                    source_locale,
                    source_locale,
                    target_locale,
                    target_locale,
                ),
            )
            row = await cur.fetchone()
        if row is None:
            raise KeyError(unit_key)
        children = await self._children_for_units([row], include_tags)
        _, data = deserialize_unit(children[0])
        return data

    def unit_sync(
        self,
        unit_key: str,
        *,
        source_locale: str = "",
        target_locale: str = "",
        include_tags: bool = True,
    ) -> Data:
        """Synchronously retrieves details for a single segment unit key."""
        return asyncio.run(
            self.unit(
                unit_key,
                source_locale=source_locale,
                target_locale=target_locale,
                include_tags=include_tags,
            )
        )

    async def to_document(
        self,
        *,
        source_locale: str,
        target_locale: str,
        include_tags: bool = True,
    ) -> BaseStructure:
        """Asynchronously exports translation units matching source/target locales to BaseStructure."""
        async with self._pools.reader.connection() as conn, conn.cursor(row_factory=class_row(UnitFetchRow)) as cur:
            await cur.execute(FETCH_UNITS_QUERY, (source_locale, target_locale))
            rows = await cur.fetchall()

        children = await self._children_for_units(rows, include_tags)
        data = {unit_key: unit for unit_key, unit in map(deserialize_unit, children)}
        return BaseStructure(
            source_locale=source_locale,
            target_locale=target_locale if target_locale else None,
            data=data,
        )

    def to_document_sync(
        self,
        *,
        source_locale: str,
        target_locale: str,
        include_tags: bool = True,
    ) -> BaseStructure:
        """Synchronously exports translation units matching source/target locales to BaseStructure."""
        return asyncio.run(
            self.to_document(
                source_locale=source_locale,
                target_locale=target_locale,
                include_tags=include_tags,
            )
        )

    async def to_multilingual_document(
        self,
        *,
        source_locale: str,
        target_locales: Iterable[str] = (),
        include_tags: bool = True,
    ) -> BaseStructure:
        requested = tuple(target_locales)
        async with self._pools.reader.connection() as conn, conn.cursor(row_factory=class_row(UnitFetchRow)) as cur:
            if requested:
                await cur.execute(FETCH_UNITS_BY_SOURCE_TARGETS_QUERY, (source_locale, list(requested)))
            else:
                await cur.execute(FETCH_UNITS_BY_SOURCE_QUERY, (source_locale,))
            rows = await cur.fetchall()

        children = await self._children_for_units(rows, include_tags)
        grouped: dict[tuple[str, str, str, str], list[UnitWithChildren]] = {}
        for child in children:
            row = child.unit
            identity = (
                row.unit_key,
                row.source_text,
                row.previous_source,
                row.next_source,
            )
            grouped.setdefault(identity, []).append(child)

        data: dict[str, Data] = {}
        locales: list[str] = []
        reserved_keys = {child.unit.unit_key for child in children}
        used_keys: set[str] = set()
        next_suffix: dict[str, int] = {}
        for identity, identity_children in grouped.items():
            locale_counts: dict[str, int] = {}
            for child in identity_children:
                locale = child.unit.target_locale
                locale_counts[locale] = locale_counts.get(locale, 0) + 1
            chunks = (
                [[child] for child in identity_children]
                if any(count > 1 for count in locale_counts.values())
                else [identity_children]
            )
            for chunk in chunks:
                output_key = _allocate_multilingual_key(
                    identity[0],
                    reserved_keys,
                    used_keys,
                    next_suffix,
                )
                data[output_key] = _multilingual_data(chunk, locales)
        return BaseStructure(
            source_locale=source_locale,
            target_locale=None,
            data=data,
            target_locales=tuple(locales),
            target_languages=tuple(locale.replace("_", "-").split("-")[0].lower() for locale in locales),
        )

    def to_multilingual_document_sync(
        self,
        *,
        source_locale: str,
        target_locales: Iterable[str] = (),
        include_tags: bool = True,
    ) -> BaseStructure:
        return asyncio.run(
            self.to_multilingual_document(
                source_locale=source_locale,
                target_locales=target_locales,
                include_tags=include_tags,
            )
        )

    def stream(
        self,
        *,
        source_locale: str,
        target_locale: str,
        include_tags: bool = True,
        batch_size: int = 1000,
    ) -> TranslationMemoryStream:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        return TranslationMemoryStream(
            self,
            source_locale=source_locale,
            target_locale=target_locale,
            include_tags=include_tags,
            batch_size=batch_size,
        )

    async def close(self) -> None:
        logger.info("Closing database connection pools")
        await self._pools.close()

    def close_sync(self) -> None:
        asyncio.run(self.close())

    async def _match_from_input(
        self,
        item: MatchInput,
        limit: int,
        threshold: float,
    ) -> list[MatchResult]:
        return await self.match(
            source=_required_match_value(item, "source"),
            source_locale=_required_match_value(item, "source_locale"),
            target_locale=_required_match_value(item, "target_locale"),
            previous_source=item.get("previous_source", ""),
            next_source=item.get("next_source", ""),
            limit=limit,
            threshold=threshold,
        )

    async def _match_from_input_on_connection(
        self,
        conn: Connection,
        item: MatchInput,
        limit: int,
        threshold: float,
    ) -> list[MatchResult]:
        source = _required_match_value(item, "source")
        source_locale = _required_match_value(item, "source_locale")
        target_locale = _required_match_value(item, "target_locale")
        previous_source = item.get("previous_source", "")
        next_source = item.get("next_source", "")
        rows = await self._match_rows_on_connection(
            conn,
            source,
            source_locale,
            target_locale,
            previous_source,
            next_source,
            limit,
            threshold,
            bool(previous_source or next_source),
            bool(previous_source or next_source),
        )
        return rows_to_match_results(
            rows,
            source,
            previous_source,
            next_source,
            bool(previous_source or next_source),
            False,
            (),
            {},
        )[:limit]

    async def _match_rows(
        self,
        source: str,
        source_locale: str,
        target_locale: str,
        previous_source: str,
        next_source: str,
        limit: int,
        threshold: float,
        check_ice: bool,
        require_context: bool,
    ) -> list[MatchRow]:
        async with self._pools.reader.connection() as conn:
            return await self._match_rows_on_connection(
                conn,
                source,
                source_locale,
                target_locale,
                previous_source,
                next_source,
                limit,
                threshold,
                check_ice,
                require_context,
            )

    async def _match_rows_on_connection(
        self,
        conn: Connection,
        source: str,
        source_locale: str,
        target_locale: str,
        previous_source: str,
        next_source: str,
        limit: int,
        threshold: float,
        check_ice: bool,
        require_context: bool,
    ) -> list[MatchRow]:
        query_match = canonical_match_text(source)
        async with conn.cursor(row_factory=class_row(MatchRow)) as cur:
            await cur.execute(
                "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                (str(threshold),),
            )
            await cur.execute(
                MATCH_QUERY,
                (
                    query_match.text,
                    query_match.signature,
                    source_locale,
                    target_locale,
                    previous_source,
                    next_source,
                    check_ice,
                    require_context,
                    _candidate_match_limit(limit),
                    threshold,
                ),
                prepare=True,
            )
            return await cur.fetchall()

    async def _candidate_tag_signatures(
        self,
        rows: list[MatchRow],
        source_locale: str,
    ) -> dict[str, TagSignature]:
        signatures: dict[str, list[tuple[str, str]]] = {row.id: [] for row in rows}
        ids = list(signatures)
        async with self._pools.reader.connection() as conn, conn.cursor() as cur:
            await cur.execute(FETCH_TAG_SIGNATURES_QUERY, (ids, source_locale))
            for item in await cur.fetchall():
                unit_id = str(item[0])
                signatures.setdefault(unit_id, []).append((str(item[1]), str(item[2])))
        return {unit_id: tag_rows_signature(tag_rows) for unit_id, tag_rows in signatures.items()}

    async def _children_for_units(
        self,
        units: list[UnitFetchRow],
        include_tags: bool,
        connection: Connection | None = None,
    ) -> list[UnitWithChildren]:
        if not units:
            return []
        if connection is not None:
            return await self._children_for_units_on_connection(units, include_tags, connection)
        async with self._pools.reader.connection() as acquired_connection:
            return await self._children_for_units_on_connection(units, include_tags, acquired_connection)

    async def _children_for_units_on_connection(
        self,
        units: list[UnitFetchRow],
        include_tags: bool,
        connection: Connection,
    ) -> list[UnitWithChildren]:
        ids = [unit.id for unit in units]
        tags_by_unit: dict[str, list[TagFetchRow]] = {unit.id: [] for unit in units}
        parts_by_unit: dict[str, list[PartFetchRow]] = {unit.id: [] for unit in units}
        comments_by_unit: dict[str, list[CommentFetchRow]] = {unit.id: [] for unit in units}

        if include_tags:
            async with connection.cursor(row_factory=class_row(TagFetchRow)) as cur:
                await cur.execute(FETCH_TAGS_FOR_UNITS_QUERY, (ids,))
                for tag_row in await cur.fetchall():
                    tags_by_unit[tag_row.unit_id].append(tag_row)
            async with connection.cursor(row_factory=class_row(PartFetchRow)) as cur:
                await cur.execute(FETCH_PARTS_FOR_UNITS_QUERY, (ids,))
                for part_row in await cur.fetchall():
                    parts_by_unit[part_row.unit_id].append(part_row)
        async with connection.cursor(row_factory=class_row(CommentFetchRow)) as cur:
            await cur.execute(FETCH_COMMENTS_FOR_UNITS_QUERY, (ids,))
            for comment_row in await cur.fetchall():
                comments_by_unit[comment_row.unit_id].append(comment_row)

        return [
            UnitWithChildren(
                unit=unit,
                tags=tags_by_unit[unit.id],
                parts=parts_by_unit[unit.id],
                comments=comments_by_unit[unit.id],
            )
            for unit in units
        ]

    async def _ensure_partition(
        self,
        conn: Connection,
        source_locale: str,
    ) -> None:
        if not self._partitioned:
            return
        partition_name = partition_name_for_locale(source_locale)
        logger.debug(
            "Ensuring partition %s for source_locale=%s",
            partition_name,
            source_locale,
        )
        async with conn.cursor() as cur:
            await cur.execute(
                SQL("CREATE TABLE IF NOT EXISTS {} PARTITION OF translation_units FOR VALUES IN ({})").format(
                    Identifier(partition_name),
                    Literal(source_locale),
                )
            )
            await cur.execute(
                """
                INSERT INTO _lokit_meta (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (f"partition:{source_locale}", partition_name),
            )

    async def _create_temp_tables(self, conn: Connection) -> None:
        async with conn.cursor() as cur:
            await cur.execute(CREATE_TEMP_UNITS)
            await cur.execute(CREATE_TEMP_TAGS)
            await cur.execute(CREATE_TEMP_PARTS)
            await cur.execute(CREATE_TEMP_COMMENTS)
            await cur.execute(CREATE_TEMP_UNIT_MAP)

    async def _flush_batch(
        self,
        conn: Connection,
        batch: list[SerializedUnit],
    ) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                TRUNCATE
                    tmp_lokit_units,
                    tmp_lokit_tags,
                    tmp_lokit_parts,
                    tmp_lokit_comments,
                    tmp_lokit_unit_map
                """
            )
            async with cur.copy(_COPY_UNITS) as copy:
                for item in batch:
                    unit = item.unit
                    await copy.write_row(
                        (
                            unit.load_id,
                            unit.id,
                            unit.unit_key,
                            unit.source_text,
                            unit.source_match_text,
                            unit.placeholder_signature,
                            unit.placeholder_index_version,
                            unit.target_text,
                            unit.source_locale,
                            unit.target_locale,
                            unit.status,
                            unit.previous_source,
                            unit.next_source,
                            unit.project,
                            unit.domain,
                            unit.usage_count,
                            unit.plural_variant,
                            unit.plural_count,
                            unit.plural_category,
                            Jsonb(unit.extensions),
                        )
                    )
            has_tags = False
            has_parts = False
            has_comments = False
            for item in batch:
                has_tags = has_tags or bool(item.tags)
                has_parts = has_parts or bool(item.parts)
                has_comments = has_comments or bool(item.comments)
            if has_tags:
                async with cur.copy(_COPY_TAGS) as copy:
                    for item in batch:
                        for tag in item.tags:
                            await copy.write_row(
                                (
                                    tag.load_id,
                                    tag.source_locale,
                                    tag.tag_id,
                                    tag.tag_type,
                                    tag.position,
                                    tag.tag_order,
                                    tag.attribute_data,
                                    tag.pair_id,
                                    tag.original_name,
                                    tag.original_text,
                                    Jsonb(tag.attributes),
                                    tag.is_source,
                                )
                            )
            if has_parts:
                async with cur.copy(_COPY_PARTS) as copy:
                    for item in batch:
                        for part in item.parts:
                            await copy.write_row(
                                (
                                    part.load_id,
                                    part.source_locale,
                                    part.is_source,
                                    part.position,
                                    part.part_type,
                                    part.value,
                                )
                            )
            if has_comments:
                async with cur.copy(_COPY_COMMENTS) as copy:
                    for item in batch:
                        for comment in item.comments:
                            await copy.write_row(
                                (
                                    comment.load_id,
                                    comment.source_locale,
                                    comment.context,
                                    comment.timestamp,
                                    comment.context_key,
                                    comment.system,
                                    comment.project,
                                    comment.creator_id,
                                    Jsonb(comment.extensions),
                                )
                            )

            await cur.execute(UPDATE_EXISTING_UNTRANSLATED_QUERY)
            await cur.execute(MAP_EXISTING_UNTRANSLATED_QUERY)
            await cur.execute(DELETE_MAPPED_STAGED_UNITS_QUERY)
            await cur.execute(UPSERT_UNITS_QUERY)
            await cur.execute(MAP_LOADED_UNITS_QUERY)
            await cur.execute(DELETE_MAPPED_TAGS_QUERY)
            await cur.execute(DELETE_MAPPED_PARTS_QUERY)
            await cur.execute(DELETE_MAPPED_COMMENTS_QUERY)
            await cur.execute(INSERT_TAGS_QUERY)
            await cur.execute(INSERT_PARTS_QUERY)
            await cur.execute(INSERT_COMMENTS_QUERY)
            await cur.execute(COUNT_MAPPED_UNITS_QUERY)
            count_row = await cur.fetchone()
            if count_row is None:
                return 0
            count_value = count_row[0]
            if isinstance(count_value, int):
                return count_value
            return int(str(count_value))


class TranslationMemoryStream:
    """Closeable async iterator returned by :meth:`TranslationMemory.stream`."""

    def __init__(
        self,
        memory: TranslationMemory,
        *,
        source_locale: str,
        target_locale: str,
        include_tags: bool,
        batch_size: int,
    ) -> None:
        self._memory = memory
        self._source_locale = source_locale
        self._target_locale = target_locale
        self._include_tags = include_tags
        self._batch_size = batch_size
        self._connection_context: _AsyncContext[_UnitConnection] | None = None
        self._connection: Connection | None = None
        self._cursor_context: _AsyncContext[_UnitCursor] | None = None
        self._cursor: _UnitCursor | None = None
        self._pending: deque[tuple[str, Data]] = deque()
        self._closed = False

    def __aiter__(self) -> TranslationMemoryStream:
        return self

    async def __aenter__(self) -> TranslationMemoryStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def __anext__(self) -> tuple[str, Data]:
        if self._closed:
            raise StopAsyncIteration
        if self._pending:
            return self._pending.popleft()
        await self._initialize()
        cursor = self._cursor
        connection = self._connection
        if cursor is None or connection is None:
            raise StopAsyncIteration
        rows: list[UnitFetchRow] = []
        while len(rows) < self._batch_size:
            try:
                rows.append(await cursor.__anext__())
            except StopAsyncIteration:
                break
        if not rows:
            await self.aclose()
            raise StopAsyncIteration
        children = await self._memory._children_for_units(
            rows,
            self._include_tags,
            connection,
        )
        self._pending.clear()
        for item in children:
            self._pending.append(deserialize_unit(item))
        if not self._pending:
            return await self.__anext__()
        return self._pending.popleft()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cursor_context is not None:
            await self._cursor_context.__aexit__(None, None, None)
            self._cursor_context = None
        if self._connection_context is not None:
            await self._connection_context.__aexit__(None, None, None)
            self._connection_context = None
        self._connection = None
        self._cursor = None

    async def _initialize(self) -> None:
        if self._cursor is not None:
            return
        connection_context_value: object = self._memory._pools.reader.connection()
        connection_context = cast("_AsyncContext[_UnitConnection]", connection_context_value)
        self._connection_context = connection_context
        connection = await connection_context.__aenter__()
        self._connection = cast("Connection", connection)
        cursor_context_value = connection.cursor(
            name="lokit_stream",
            row_factory=class_row(UnitFetchRow),
        )
        cursor_context = cast("_AsyncContext[_UnitCursor]", cursor_context_value)
        self._cursor_context = cursor_context
        cursor = await cursor_context.__aenter__()
        self._cursor = cursor
        await cursor.execute(FETCH_UNITS_QUERY, (self._source_locale, self._target_locale))


def _source_tags(tags: Tags | None) -> Tags | None:
    if tags is None:
        return None
    return Tags(
        source_tag_map=tags.source_tag_map.copy(),
        source_parts=list(tags.source_parts),
    )


def _allocate_multilingual_key(
    unit_key: str,
    reserved_keys: set[str],
    used_keys: set[str],
    next_suffix: dict[str, int],
) -> str:
    if unit_key not in used_keys:
        used_keys.add(unit_key)
        return unit_key
    suffix = next_suffix.get(unit_key, 2)
    while True:
        candidate = f"{unit_key}#{suffix}"
        suffix += 1
        if candidate in reserved_keys or candidate in used_keys:
            continue
        next_suffix[unit_key] = suffix
        used_keys.add(candidate)
        return candidate


def _multilingual_data(
    children: list[UnitWithChildren],
    locales: list[str],
) -> Data:
    _, first = deserialize_unit(children[0])
    result = Data(
        source=first.source,
        tags=_source_tags(first.tags),
        previous_context=first.previous_context,
        next_context=first.next_context,
    )
    for child in children:
        _, unit = deserialize_unit(child)
        locale = child.unit.target_locale
        if not locale:
            result.plural = unit.plural
            result.meta = unit.meta
            result.status = unit.status
            result.comments = unit.comments
            result.extensions = unit.extensions
            continue
        result.targets[locale] = TargetData(
            text=unit.target,
            status=unit.status,
            tags=_target_tags(unit.tags),
            plural=unit.plural,
            meta=unit.meta,
            comments=unit.comments,
            extensions=unit.extensions,
        )
        if locale not in locales:
            locales.append(locale)
    return result


def _target_tags(tags: Tags | None) -> TargetTags | None:
    if tags is None or (not tags.target_tag_map and not tags.target_parts):
        return None
    return TargetTags(
        tag_map=tags.target_tag_map.copy(),
        parts=list(tags.target_parts),
    )


def _iter_load_batches(
    units: Iterable[SerializedUnit],
    *,
    batch_size: int,
    max_batch_bytes: int,
) -> Generator[list[SerializedUnit], None, None]:
    """Yield strict row- and payload-bounded serialized-unit batches."""
    batch: list[SerializedUnit] = []
    batch_bytes = 0
    for item in units:
        item_bytes = _serialized_unit_bytes(item)
        if item_bytes > max_batch_bytes:
            raise ValueError(
                f"translation-memory unit {item.unit.unit_key!r} requires {item_bytes} serialized bytes; "
                f"max_batch_bytes is {max_batch_bytes}"
            )
        if batch and (len(batch) >= batch_size or batch_bytes + item_bytes > max_batch_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += item_bytes
    if batch:
        yield batch


def _serialized_unit_bytes(item: SerializedUnit) -> int:
    """Return the deterministic logical COPY payload size for one unit."""
    total = _unit_row_bytes(item.unit)
    for tag in item.tags:
        total += _tag_row_bytes(tag)
    for part in item.parts:
        total += _part_row_bytes(part)
    for comment in item.comments:
        total += _comment_row_bytes(comment)
    return total


def _unit_row_bytes(row: UnitInsertRow) -> int:
    return (
        20
        + _text_bytes(row.load_id)
        + _text_bytes(row.id)
        + _text_bytes(row.unit_key)
        + _text_bytes(row.source_text)
        + _text_bytes(row.source_match_text)
        + _text_bytes(row.placeholder_signature)
        + _integer_bytes(row.placeholder_index_version)
        + _text_bytes(row.target_text)
        + _text_bytes(row.source_locale)
        + _text_bytes(row.target_locale)
        + _text_bytes(row.status)
        + _text_bytes(row.previous_source)
        + _text_bytes(row.next_source)
        + _text_bytes(row.project)
        + _text_bytes(row.domain)
        + _integer_bytes(row.usage_count)
        + _text_bytes(row.plural_variant)
        + _integer_bytes(row.plural_count)
        + _text_bytes(row.plural_category)
        + _json_bytes(row.extensions)
    )


def _tag_row_bytes(row: TagInsertRow) -> int:
    return (
        12
        + _text_bytes(row.load_id)
        + _text_bytes(row.source_locale)
        + _text_bytes(row.tag_id)
        + _text_bytes(row.tag_type)
        + _integer_bytes(row.position)
        + _integer_bytes(row.tag_order)
        + _text_bytes(row.attribute_data)
        + _text_bytes(row.pair_id)
        + _text_bytes(row.original_name)
        + _text_bytes(row.original_text)
        + _json_bytes(row.attributes)
        + 1
    )


def _part_row_bytes(row: PartInsertRow) -> int:
    return (
        6
        + _text_bytes(row.load_id)
        + _text_bytes(row.source_locale)
        + 1
        + _integer_bytes(row.position)
        + _text_bytes(row.part_type)
        + _text_bytes(row.value)
    )


def _comment_row_bytes(row: CommentInsertRow) -> int:
    return (
        9
        + _text_bytes(row.load_id)
        + _text_bytes(row.source_locale)
        + _text_bytes(row.context)
        + _text_bytes(row.timestamp)
        + _text_bytes(row.context_key)
        + _text_bytes(row.system)
        + _text_bytes(row.project)
        + _text_bytes(row.creator_id)
        + _json_bytes(row.extensions)
    )


def _text_bytes(value: str | None) -> int:
    return 2 if value is None else len(value.encode("utf-8"))


def _integer_bytes(value: int | None) -> int:
    return 2 if value is None else len(str(value))


def _json_bytes(value: JsonDict) -> int:
    total = 0
    for chunk in _COMPACT_JSON_ENCODER.iterencode(value):
        total += len(chunk.encode("utf-8"))
    return total


def _bounded_reindex_keys(
    rows: list[tuple[object, ...]],
    max_batch_bytes: int,
) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    locales: list[str] = []
    selected_bytes = 0
    for row in rows:
        unit_id = str(row[0])
        source_locale = str(row[1])
        byte_count = row[2] if isinstance(row[2], int) else int(str(row[2]))
        if byte_count > _MAX_REINDEX_SOURCE_BYTES:
            raise ValueError(
                f"translation-memory source {unit_id!r} is {byte_count} bytes; "
                f"the placeholder index limit is {_MAX_REINDEX_SOURCE_BYTES}"
            )
        if ids and selected_bytes + byte_count > max_batch_bytes:
            break
        ids.append(unit_id)
        locales.append(source_locale)
        selected_bytes += byte_count
    return ids, locales


def _canonicalize_reindex_batch(
    rows: list[tuple[str, str, str]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    ids: list[str] = []
    locales: list[str] = []
    match_texts: list[str] = []
    signatures: list[str] = []
    for unit_id, source_locale, source_text in rows:
        canonical = canonical_match_text(source_text)
        ids.append(unit_id)
        locales.append(source_locale)
        match_texts.append(canonical.text)
        signatures.append(canonical.signature)
    return ids, locales, match_texts, signatures


def _deduplicate_batch(batch: list[SerializedUnit]) -> list[SerializedUnit]:
    seen: set[tuple[str, str | None, str, str, str, str]] = set()
    deduped: list[SerializedUnit] = []
    for item in batch:
        unit = item.unit
        signature = (
            unit.source_text.lower(),
            unit.target_text,
            unit.source_locale,
            unit.target_locale,
            unit.previous_source.lower(),
            unit.next_source.lower(),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(item)
    return deduped


def _required_match_value(item: MatchInput, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"match input is missing {key!r}")
    return value


def _candidate_match_limit(limit: int) -> int:
    # Fetch a bounded safety margin so a malformed target placeholder graph
    # cannot crowd a reformable candidate out of the public result limit.
    return limit + min(max(limit * 3, 16), 1000)
