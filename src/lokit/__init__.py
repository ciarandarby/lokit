"""Lokit package bootstrap; routing behavior lives in the compiled ``lokit._api`` module."""

from typing import TYPE_CHECKING

from lokit._api import get_attribute, public_dir

if TYPE_CHECKING:
    import lokit.async_ as async_
    import lokit.convert as convert
    import lokit.database as database
    import lokit.parse as parse
    import lokit.stream as stream
    import lokit.types as types
    import lokit.write as write
    from lokit.logic import Lokit as Lokit

__all__ = ["Lokit", "async_", "convert", "database", "parse", "stream", "types", "write"]


def __getattr__(name: str) -> object:
    return get_attribute(name)


def __dir__() -> list[str]:
    return public_dir()
