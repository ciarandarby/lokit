from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Iterator

NativeRecord: TypeAlias = tuple[
    bool,
    str,
    str,
    str | None,
    list[tuple[str, str]],
    str,
    dict[str, str],
    bytes | None,
]


class NativeReader(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def source_locale(self) -> str | None: ...

    @property
    def target_locale(self) -> str | None: ...

    @property
    def source_language(self) -> str | None: ...

    @property
    def target_language(self) -> str | None: ...

    @property
    def target_locales(self) -> list[str]: ...

    @property
    def target_languages(self) -> list[str]: ...

    @property
    def export_origin(self) -> str: ...

    @property
    def export_timestamp(self) -> str: ...

    @property
    def extensions(self) -> dict[str, str]: ...

    @property
    def closed(self) -> bool: ...

    def read_batch(self, batch_size: int = 256) -> list[NativeRecord]: ...

    def close(self) -> None: ...


_DISABLE_ENV: Final = "LOKIT_DISABLE_RUST_INTERCHANGE"
_DEFAULT_BATCH_SIZE: Final = 256


def native_interchange_enabled() -> bool:
    value = os.environ.get(_DISABLE_ENV, "")
    return value.lower() not in {"1", "true", "yes", "on"}


def open_native_reader(
    path: str,
    format_name: str,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: str = "full",
) -> NativeReader | None:
    if not native_interchange_enabled():
        return None
    try:
        from lokit._interchange_rust import Reader
    except ImportError:
        return None

    try:
        return Reader(path, format_name, source_language, target_language, mode)
    except NotImplementedError:
        return None


def iter_native_records(
    reader: NativeReader,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> Iterator[NativeRecord]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    try:
        while True:
            batch = reader.read_batch(batch_size)
            if not batch:
                return
            yield from batch
    finally:
        reader.close()


def native_backend_version() -> str | None:
    if not native_interchange_enabled():
        return None
    try:
        from lokit._interchange_rust import backend_version
    except ImportError:
        return None
    return backend_version()
