from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from lokit.core.logger import logger
from lokit.db.operations import TranslationMemory

PasswordFactory = Callable[[], str]
Connection: TypeAlias = AsyncConnection[tuple[object, ...]]


@dataclass(slots=True)
class WriterReaderPool:
    writer: AsyncConnectionPool[Connection]
    reader: AsyncConnectionPool[Connection]

    async def close(self) -> None:
        logger.debug("Closing database connection pools")
        await self.writer.close()
        if self.reader is not self.writer:
            await self.reader.close()


async def connect(
    uri: str,
    *,
    reader_uri: str | None = None,
    pool_size: int = 4,
    min_size: int = 2,
    password: str | None = None,
    password_factory: PasswordFactory | None = None,
    ssl: bool = False,
    pipeline: bool = True,
    timeout: float = 30.0,
) -> TranslationMemory:
    if pool_size < 1:
        raise ValueError("pool_size must be at least 1")
    if min_size < 0:
        raise ValueError("min_size cannot be negative")
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    password_factory = _resolve_password_factory(password, password_factory)

    logger.info(
        "Connecting to database at %s (pool_size=%d, min_size=%d, ssl=%s)",
        _sanitize_uri(uri),
        pool_size,
        min(min_size, pool_size),
        ssl,
    )
    writer = await _create_pool(
        uri,
        pool_size=pool_size,
        min_size=min(min_size, pool_size),
        password_factory=password_factory,
        ssl=ssl,
        name="lokit-writer",
        timeout=timeout,
    )
    reader = writer
    try:
        if reader_uri is None:
            reader = writer
        else:
            logger.info("Connecting reader pool at %s", _sanitize_uri(reader_uri))
            reader = await _create_pool(
                reader_uri,
                pool_size=pool_size,
                min_size=min(min_size, pool_size),
                password_factory=password_factory,
                ssl=ssl,
                name="lokit-reader",
                timeout=timeout,
            )

        await _health_check(writer, "writer")
        if reader is not writer:
            await _health_check(reader, "reader")

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
            logger.info("PostgreSQL %d detected; pipeline mode enabled.", version)
        else:
            logger.info("Database pipeline mode disabled.")
        logger.info("Database connection established successfully.")
        return TranslationMemory(WriterReaderPool(writer=writer, reader=reader), pipeline)
    except BaseException as exc:
        logger.error("Database connection failed for %s: %s", _sanitize_uri(uri), exc)
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
    password: str | None = None,
    password_factory: PasswordFactory | None = None,
    ssl: bool = False,
    pipeline: bool = True,
    timeout: float = 30.0,
) -> TranslationMemory:
    return asyncio.run(
        connect(
            uri,
            reader_uri=reader_uri,
            pool_size=pool_size,
            min_size=min_size,
            password=password,
            password_factory=password_factory,
            ssl=ssl,
            pipeline=pipeline,
            timeout=timeout,
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
    timeout: float,
) -> AsyncConnectionPool[Connection]:
    conninfo = _connection_info(uri, password_factory, ssl)
    pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=pool_size,
        open=False,
        name=name,
        timeout=timeout,
    )
    try:
        await pool.open()
    except Exception as exc:
        await pool.close()
        raise ConnectionError(
            f"Failed to open connection pool '{name}'. "
            "Check that the database is reachable and credentials are valid. "
            f"Original error: {exc}"
        ) from exc
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


def _resolve_password_factory(
    password: str | None,
    password_factory: PasswordFactory | None,
) -> PasswordFactory | None:
    if password is not None and password_factory is not None:
        raise ValueError("Pass either password or password_factory, not both")
    if password is None:
        return password_factory
    return lambda: password


async def _health_check(pool: AsyncConnectionPool[Connection], name: str) -> None:
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
    except Exception as exc:
        raise ConnectionError(
            f"Database health check failed for '{name}'. "
            "Verify the connection URI, credentials, network access, "
            "and that the database server is running. "
            f"Original error: {exc}"
        ) from exc


async def _pg_major_version(pool: AsyncConnectionPool[Connection]) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SHOW server_version_num")
            row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Could not determine PostgreSQL server version")
    version_num = int(str(row[0]))
    return version_num // 10000


def _sanitize_uri(uri: str) -> str:
    try:
        values = conninfo_to_dict(uri)
    except Exception:
        return "<invalid connection info>"
    if "password" in values:
        values["password"] = "***"
    return make_conninfo("", **values)
