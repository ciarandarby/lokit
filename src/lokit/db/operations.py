from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from time import perf_counter
from typing import TYPE_CHECKING, TypeAlias

from psycopg import AsyncConnection
from psycopg.rows import class_row
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb
from tqdm import tqdm

from lokit.core.logger import logger
from lokit.data.structure import BaseStructure, Data, StreamingStructure, Tags
from lokit.db.matching import (
    TagSignature,
    rows_to_match_results,
    tag_rows_signature,
    tags_signature_from_tags,
)
from lokit.db.models import (
    CommentFetchRow,
    LoadStats,
    MatchInput,
    MatchRow,
    PartFetchRow,
    SerializedUnit,
    TagFetchRow,
    UnitFetchRow,
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
    FETCH_TAG_SIGNATURE_QUERY,
    FETCH_TAGS_FOR_UNITS_QUERY,
    FETCH_UNIT_BY_KEY_QUERY,
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
    CREATE_TEMP_COMMENTS,
    CREATE_TEMP_PARTS,
    CREATE_TEMP_TAGS,
    CREATE_TEMP_UNIT_MAP,
    CREATE_TEMP_UNITS,
    CURRENT_VERSION,
    partition_name_for_locale,
    schema_for_partitioning,
)
from lokit.db.serialization import deserialize_unit, serialize_unit
from lokit.logic import MatchResult

if TYPE_CHECKING:
    from lokit.db.connection import WriterReaderPool

Structure = BaseStructure | StreamingStructure
Connection: TypeAlias = AsyncConnection[tuple[object, ...]]

_COPY_UNITS = """
COPY tmp_lokit_units (
    load_id,
    id,
    unit_key,
    source_text,
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
        logger.info("Setting up database schema (partitioned=%s)", partitioned)
        async with self._pools.writer.connection() as conn:
            async with conn.cursor() as cur:
                logger.debug("Creating database extensions")
                await cur.execute(CREATE_EXTENSIONS)
                logger.debug("Checking lokit schema metadata")
                await cur.execute(CREATE_META_TABLE)
                await cur.execute(
                    "SELECT value FROM _lokit_meta WHERE key = 'schema_version'"
                )
                version_row = await cur.fetchone()
                if version_row is None:
                    logger.info(
                        "Creating lokit database schema version %d", CURRENT_VERSION
                    )
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
                    logger.info("Database schema ready (version %d)", CURRENT_VERSION)
                    return

                version = int(str(version_row[0]))
                if version > CURRENT_VERSION:
                    raise RuntimeError(
                        "Database schema is newer than this lokit version. "
                        "Upgrade lokit before using this translation memory."
                    )
                await cur.execute(
                    "SELECT value FROM _lokit_meta WHERE key = 'partitioned'"
                )
                partitioned_row = await cur.fetchone()
                existing_partitioned = (
                    partitioned_row is None or str(partitioned_row[0]) == "true"
                )
                if existing_partitioned != partitioned:
                    raise RuntimeError(
                        "translation_units already exists with different partitioning. "
                        "Use the existing setup or create a fresh database."
                    )
                self._partitioned = existing_partitioned
        logger.info("Database schema ready (version %d)", CURRENT_VERSION)

    def setup_sync(self, partitioned: bool = True) -> None:
        asyncio.run(self.setup(partitioned))

    async def load(
        self,
        document: Structure,
        *,
        batch_size: int = 5000,
        project: str = "",
        domain: str = "",
        progress: bool = True,
    ) -> LoadStats:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        start = perf_counter()
        source_locale = document.source_locale
        target_locale = document.target_locale or ""
        units_read = 0
        units_written = 0
        batch: list[SerializedUnit] = []
        total = len(document.data) if isinstance(document, BaseStructure) else None
        logger.info(
            "Loading data (source_locale=%s, target_locale=%s)",
            source_locale,
            target_locale,
        )

        async with self._pools.writer.connection() as conn:
            async with conn.transaction():
                await self._ensure_partition(conn, source_locale)
                await self._create_temp_tables(conn)
                for unit_key, data in tqdm(
                    _iter_document(document),
                    total=total,
                    desc="Loading TM",
                    unit="units",
                    disable=not progress,
                ):
                    batch.append(
                        serialize_unit(
                            unit_key,
                            data,
                            source_locale,
                            target_locale,
                            project,
                            domain,
                        )
                    )
                    units_read += 1
                    if len(batch) >= batch_size:
                        deduped = _deduplicate_batch(batch)
                        logger.debug("Flushing batch of %d units", len(deduped))
                        units_written += await self._flush_batch(conn, deduped)
                        batch = []
                if batch:
                    deduped = _deduplicate_batch(batch)
                    logger.debug("Flushing batch of %d units", len(deduped))
                    units_written += await self._flush_batch(conn, deduped)

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
        project: str = "",
        domain: str = "",
        progress: bool = True,
    ) -> LoadStats:
        return asyncio.run(
            self.load(
                document,
                batch_size=batch_size,
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
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")

        signature = tag_signature or (
            tags_signature_from_tags(source_tags) if source_tags is not None else ()
        )
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
        )
        candidate_signatures = (
            await self._candidate_tag_signatures(rows, source_locale)
            if require_tags and rows
            else {}
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
        results: list[list[MatchResult]] = []
        input_list = list(inputs)
        logger.debug("Running batch match for %d queries", len(input_list))
        if self._pipeline:
            async with self._pools.reader.connection() as conn:
                async with conn.pipeline():
                    for item in tqdm(
                        input_list,
                        total=len(input_list),
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
            input_list,
            total=len(input_list),
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
        async with self._pools.reader.connection() as conn:
            async with conn.cursor(row_factory=class_row(UnitFetchRow)) as cur:
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
        async with self._pools.reader.connection() as conn:
            async with conn.cursor(row_factory=class_row(UnitFetchRow)) as cur:
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
        return asyncio.run(
            self.to_document(
                source_locale=source_locale,
                target_locale=target_locale,
                include_tags=include_tags,
            )
        )

    async def stream(
        self,
        *,
        source_locale: str,
        target_locale: str,
        include_tags: bool = True,
        batch_size: int = 1000,
    ) -> AsyncIterator[tuple[str, Data]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        batch: list[UnitFetchRow] = []
        async with self._pools.reader.connection() as conn:
            async with conn.cursor(
                name="lokit_stream",
                row_factory=class_row(UnitFetchRow),
            ) as cur:
                await cur.execute(FETCH_UNITS_QUERY, (source_locale, target_locale))
                async for row in cur:
                    batch.append(row)
                    if len(batch) >= batch_size:
                        for children in await self._children_for_units(
                            batch,
                            include_tags,
                        ):
                            yield deserialize_unit(children)
                        batch = []
        if batch:
            for children in await self._children_for_units(batch, include_tags):
                yield deserialize_unit(children)

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
    ) -> list[MatchRow]:
        async with conn.cursor(row_factory=class_row(MatchRow)) as cur:
            await cur.execute(
                "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                (str(threshold),),
            )
            await cur.execute(
                MATCH_QUERY,
                (
                    source,
                    source_locale,
                    target_locale,
                    previous_source,
                    next_source,
                    source,
                    source,
                    previous_source,
                    next_source,
                    limit,
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
        signatures: dict[str, TagSignature] = {}
        async with self._pools.reader.connection() as conn:
            async with conn.cursor() as cur:
                for row in rows:
                    await cur.execute(
                        FETCH_TAG_SIGNATURE_QUERY, (row.id, source_locale)
                    )
                    tag_rows = [
                        (str(item[0]), str(item[1])) for item in await cur.fetchall()
                    ]
                    signatures[row.id] = tag_rows_signature(tag_rows)
        return signatures

    async def _children_for_units(
        self,
        units: list[UnitFetchRow],
        include_tags: bool,
    ) -> list[UnitWithChildren]:
        if not units:
            return []
        ids = [unit.id for unit in units]
        tags_by_unit: dict[str, list[TagFetchRow]] = {unit.id: [] for unit in units}
        parts_by_unit: dict[str, list[PartFetchRow]] = {unit.id: [] for unit in units}
        comments_by_unit: dict[str, list[CommentFetchRow]] = {
            unit.id: [] for unit in units
        }

        async with self._pools.reader.connection() as conn:
            if include_tags:
                async with conn.cursor(row_factory=class_row(TagFetchRow)) as cur:
                    await cur.execute(FETCH_TAGS_FOR_UNITS_QUERY, (ids,))
                    for tag_row in await cur.fetchall():
                        tags_by_unit[tag_row.unit_id].append(tag_row)
                async with conn.cursor(row_factory=class_row(PartFetchRow)) as cur:
                    await cur.execute(FETCH_PARTS_FOR_UNITS_QUERY, (ids,))
                    for part_row in await cur.fetchall():
                        parts_by_unit[part_row.unit_id].append(part_row)
            async with conn.cursor(row_factory=class_row(CommentFetchRow)) as cur:
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
                SQL(
                    "CREATE TABLE IF NOT EXISTS {} PARTITION OF translation_units FOR VALUES IN ({})"
                ).format(
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
            tag_rows = [row for item in batch for row in item.tags]
            if tag_rows:
                async with cur.copy(_COPY_TAGS) as copy:
                    for tag in tag_rows:
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
            part_rows = [row for item in batch for row in item.parts]
            if part_rows:
                async with cur.copy(_COPY_PARTS) as copy:
                    for part in part_rows:
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
            comment_rows = [row for item in batch for row in item.comments]
            if comment_rows:
                async with cur.copy(_COPY_COMMENTS) as copy:
                    for comment in comment_rows:
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


def _iter_document(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


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
