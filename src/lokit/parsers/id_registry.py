from __future__ import annotations

import sqlite3
from os import getpid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

_MEMORY_ENTRY_LIMIT: Final = 65_536
_MEMORY_BYTE_LIMIT: Final = 8 * 1024 * 1024
_INITIAL_NEXT_SUFFIX: Final = 2
_MAX_SUFFIX: Final = (1 << 63) - 1
_INSERT_ID: Final = "INSERT OR IGNORE INTO unit_ids (value, next_suffix) VALUES (?, ?)"
_SELECT_SUFFIX: Final = "SELECT next_suffix FROM unit_ids WHERE value = ?"
_UPDATE_SUFFIX: Final = "UPDATE unit_ids SET next_suffix = ? WHERE value = ? AND next_suffix = ?"


class BoundedIdRegistry:
    """Exact ID membership with a capped in-memory hot path and disk spill."""

    __slots__ = (
        "_byte_limit",
        "_connection",
        "_entry_limit",
        "_maximum_suffix",
        "_memory",
        "_memory_bytes",
        "_temporary_directory",
    )

    def __init__(
        self,
        *,
        memory_entry_limit: int = _MEMORY_ENTRY_LIMIT,
        memory_byte_limit: int = _MEMORY_BYTE_LIMIT,
        maximum_suffix: int = _MAX_SUFFIX,
    ) -> None:
        if memory_entry_limit < 0:
            raise ValueError("memory_entry_limit must not be negative")
        if memory_byte_limit < 0:
            raise ValueError("memory_byte_limit must not be negative")
        if maximum_suffix <= _INITIAL_NEXT_SUFFIX:
            raise ValueError("maximum_suffix must be greater than 2")
        self._entry_limit = memory_entry_limit
        self._byte_limit = memory_byte_limit
        self._maximum_suffix = maximum_suffix
        self._memory: dict[str, int] = {}
        self._memory_bytes = 0
        self._connection: sqlite3.Connection | None = None
        self._temporary_directory: TemporaryDirectory[str] | None = None

    def add(self, value: str) -> bool:
        connection = self._connection
        if connection is not None:
            return connection.execute(_INSERT_ID, (value, _INITIAL_NEXT_SUFFIX)).rowcount == 1
        if value in self._memory:
            return False
        if len(self._memory) < self._entry_limit and self._memory_bytes + value.__sizeof__() <= self._byte_limit:
            self._memory[value] = _INITIAL_NEXT_SUFFIX
            self._memory_bytes += value.__sizeof__()
            return True

        self._spill()
        connection = self._connection
        if connection is None:
            raise RuntimeError("unit ID registry failed to initialize its disk index")
        return connection.execute(_INSERT_ID, (value, _INITIAL_NEXT_SUFFIX)).rowcount == 1

    def next_suffix(self, value: str) -> int:
        connection = self._connection
        if connection is None:
            try:
                current = self._memory[value]
            except KeyError as error:
                raise KeyError(f"unit ID is not registered: {value!r}") from error
            self._memory[value] = self._increment_suffix(current)
            return current

        row = cast("tuple[int] | None", connection.execute(_SELECT_SUFFIX, (value,)).fetchone())
        if row is None:
            raise KeyError(f"unit ID is not registered: {value!r}")
        current = row[0]
        next_suffix = self._increment_suffix(current)
        updated = connection.execute(_UPDATE_SUFFIX, (next_suffix, value, current)).rowcount
        if updated != 1:
            raise RuntimeError("unit ID suffix counter changed unexpectedly")
        return current

    @property
    def spilled(self) -> bool:
        return self._connection is not None

    @property
    def temporary_path(self) -> Path | None:
        temporary_directory = self._temporary_directory
        if temporary_directory is None:
            return None
        return Path(temporary_directory.name)

    def close(self) -> None:
        connection = self._connection
        temporary_directory = self._temporary_directory
        self._connection = None
        self._temporary_directory = None
        try:
            if connection is not None:
                connection.close()
        finally:
            if temporary_directory is not None:
                temporary_directory.cleanup()

    def _spill(self) -> None:
        temporary_directory = TemporaryDirectory(prefix=f"lokit-interchange-ids-{getpid()}-")
        connection = sqlite3.connect(Path(temporary_directory.name, "ids.sqlite3"))
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute(
                "CREATE TABLE unit_ids (value TEXT PRIMARY KEY, next_suffix INTEGER NOT NULL) WITHOUT ROWID"
            )
            connection.executemany(_INSERT_ID, self._memory.items())
        except BaseException:
            connection.close()
            temporary_directory.cleanup()
            raise
        self._connection = connection
        self._temporary_directory = temporary_directory
        self._memory = {}
        self._memory_bytes = 0

    def _increment_suffix(self, current: int) -> int:
        if current >= self._maximum_suffix:
            raise OverflowError("unit ID suffix counter overflow")
        return current + 1
