from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import Data
from lokit.parsers.tmx.models import TmxParseMode
from lokit.types import (
    DEFAULT_DICT_FIELDS,
    DictField,
    StringMode,
    TagSyntax,
    TranslationRow,
    UnsupportedTagPolicy,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from pathlib import Path

    from lokit.io.stream_json import LokitJsonContext
    from lokit.office.models import DocumentSource
    from lokit.parsers.async_bridge import AsyncExtractionBridge

ExtractItem = tuple[str, Data]
TmxBatch = list[ExtractItem]

__all__ = ["docx", "lokit", "pptx", "tmx", "tmx_batches", "to_dict", "write_jsonl", "xliff"]


def lokit(filepath: str) -> AsyncExtractionBridge[ExtractItem]:
    """Asynchronously streams units from a Lokit interchange file."""
    from lokit.importers import import_lokit_async

    return import_lokit_async(filepath)


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from a TMX file."""
    from lokit.importers import import_tmx_async

    return import_tmx_async(
        filepath,
        source_language,
        target_language,
        domain,
        mode,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def tmx_batches(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    *,
    batch_size: int = 1000,
    mode: TmxParseMode = TmxParseMode.FULL,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[TmxBatch]:
    """Asynchronously streams translation units from a TMX file in batches."""
    from lokit.importers import import_tmx_batches_async

    return import_tmx_batches_async(
        filepath,
        source_language=source_language,
        target_language=target_language,
        domain=domain,
        batch_size=batch_size,
        mode=mode,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def xliff(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from an XLIFF file."""
    from lokit.importers import import_xliff_async

    return import_xliff_async(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from a Word DOCX file."""
    from lokit.importers import import_docx_async

    return import_docx_async(filepath, source_locale, target_locale)


def pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from a PowerPoint PPTX file."""
    from lokit.importers import import_pptx_async

    return import_pptx_async(filepath, source_locale, target_locale)


async def write_jsonl(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
) -> Path:
    """Asynchronously streams document data to newline-delimited JSON."""
    from lokit.logic import Lokit

    return await Lokit.to_jsonl_async(filepath, output, context)


def to_dict(
    filepath: str | Path,
    source_language: str = "",
    target_language: str = "",
    domain: str = "",
    *,
    fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
    strings: StringMode | str = StringMode.SANITIZED,
) -> AsyncExtractionBridge[TranslationRow]:
    """Return a bounded row bridge with explicit early-exit cleanup."""
    from lokit.parsers.async_bridge import AsyncExtractionBridge
    from lokit.stream import to_dict as to_dict_sync

    normalized_fields = tuple(fields)
    return AsyncExtractionBridge(
        lambda: to_dict_sync(
            filepath,
            source_language,
            target_language,
            domain,
            fields=normalized_fields,
            strings=strings,
        ),
        batch_size=256,
    )
