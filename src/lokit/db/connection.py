from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

try:
    from psycopg import AsyncConnection
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import AsyncConnectionPool
except ModuleNotFoundError as exc:  # pragma: no cover - exercised without db extra
    raise ModuleNotFoundError(
        "lokit.db requires the optional database dependencies. "
        "Install with `pip install lokit-python[db]`."
    ) from exc

from lokit.db.operations import TranslationMemory

PasswordFactory = Callable[[], str]
Connection: TypeAlias = AsyncConnection[tuple[object, ...]]


@dataclass(slots=True)
class WriterReaderPool:
    writer: AsyncConnectionPool[Connection]
    reader: AsyncConnectionPool[Connection]

    async def close(self) -> None:
        await self.writer.close()
        if self.reader is not self.writer:
            await self.reader.close()


async def connect(
    uri: str,
    *,
    reader_uri: str | None = None,
    pool_size: int = 4,
    min_size: int = 2,
    password_factory: PasswordFactory | None = None,
    ssl: bool = False,
    pipeline: bool = True,
) -> TranslationMemory:
    if pool_size < 1:
        raise ValueError("pool_size must be at least 1")
    if min_size < 0:
        raise ValueError("min_size cannot be negative")

    writer = await _create_pool(
        uri,
        pool_size=pool_size,
        min_size=min(min_size, pool_size),
        password_factory=password_factory,
        ssl=ssl,
        name="lokit-writer",
    )
    if reader_uri is None:
        reader = writer
    else:
        reader = await _create_pool(
            reader_uri,
            pool_size=pool_size,
            min_size=min(min_size, pool_size),
            password_factory=password_factory,
            ssl=ssl,
            name="lokit-reader",
        )

    try:
        if pipeline:
            writer_version = await _pg_major_version(writer)
            reader_version = writer_version if reader is writer else await _pg_major_version(reader)
            version = min(writer_version, reader_version)
            if version < 14:
                raise RuntimeError(
                    f"PostgreSQL {version} detected. Pipeline mode requires "
                    "PostgreSQL 14+. Pass pipeline=False to connect without "
                    "pipeline support."
                )
        return TranslationMemory(WriterReaderPool(writer=writer, reader=reader), pipeline)
    except BaseException:
        await writer.close()
        if reader is not writer:
            await reader.close()
        raise


def connect_sync(
    uri: str,
    *,
    reader_uri: str | None = None,
    pool_size: int = 4,
    min_size: int = 2,
    password_factory: PasswordFactory | None = None,
    ssl: bool = False,
    pipeline: bool = True,
) -> TranslationMemory:
    return asyncio.run(
        connect(
            uri,
            reader_uri=reader_uri,
            pool_size=pool_size,
            min_size=min_size,
            password_factory=password_factory,
            ssl=ssl,
            pipeline=pipeline,
        )
    )


async def _create_pool(
    uri: str,
    *,
    pool_size: int,
    min_size: int,
    password_factory: PasswordFactory | None,
    ssl: bool,
    name: str,
) -> AsyncConnectionPool[Connection]:
    conninfo = _connection_info(uri, password_factory, ssl)
    pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=pool_size,
        open=False,
        name=name,
    )
    await pool.open()
    return pool


def _connection_info(
    uri: str,
    password_factory: PasswordFactory | None,
    ssl: bool,
) -> str:
    conninfo = uri
    if password_factory is not None:
        conninfo = make_conninfo(conninfo, password=password_factory())
    if ssl and "sslmode=" not in conninfo:
        conninfo = make_conninfo(conninfo, sslmode="require")
    return conninfo


async def _pg_major_version(pool: AsyncConnectionPool[Connection]) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SHOW server_version_num")
            row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Could not determine PostgreSQL server version")
    version_num = int(str(row[0]))
    return version_num // 10000
