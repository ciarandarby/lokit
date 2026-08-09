from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from lokit.io.stream_json import LokitJsonContext
from lokit.parsers.tmx.models import TmxParseMode
from lokit.stream import async_ as async_
from lokit.types import DEFAULT_DICT_FIELDS, DictField, StringMode, TagSyntax, TranslationRow, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from lokit.data.structure import StreamingStructure
    from lokit.office.models import DocumentSource
    from lokit.parsers.tmx.parallel import TmxParallelOptions

__all__ = [
    "LokitJsonContext",
    "async_",
    "docx",
    "lokit",
    "pptx",
    "tmx",
    "tmx_parallel",
    "to_dict",
    "write_jsonl",
    "xliff",
]


def lokit(filepath: str) -> StreamingStructure:
    """Streams a Lokit interchange document as a StreamingStructure."""
    from lokit.importers import stream_lokit

    return stream_lokit(filepath)


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    *,
    domain: str | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> StreamingStructure:
    """Streams a TMX document as a StreamingStructure."""
    from lokit.importers import stream_tmx

    return stream_tmx(
        filepath,
        source_language,
        target_language,
        mode,
        domain=domain,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def tmx_parallel(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
    options: TmxParallelOptions | None = None,
) -> StreamingStructure:
    """Streams a TMX document in parallel as a StreamingStructure."""
    from lokit.importers import stream_tmx_parallel

    return stream_tmx_parallel(filepath, source_language, target_language, domain, mode, options)


def xliff(
    filepath: str,
    *,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> StreamingStructure:
    """Streams an XLIFF document as a StreamingStructure."""
    from lokit.importers import stream_xliff

    return stream_xliff(
        filepath,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
    )


def docx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = False,
) -> StreamingStructure:
    """Streams a Word DOCX file as a StreamingStructure."""
    from lokit.importers import stream_docx

    return stream_docx(filepath, source_locale=source_locale, target_locale=target_locale, progress=progress)


def pptx(
    filepath: DocumentSource,
    source_locale: str = "",
    target_locale: str | None = None,
    *,
    progress: bool = False,
) -> StreamingStructure:
    """Streams a PowerPoint PPTX file as a StreamingStructure."""
    from lokit.importers import stream_pptx

    return stream_pptx(filepath, source_locale=source_locale, target_locale=target_locale, progress=progress)


def write_jsonl(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
) -> Path:
    """Streams a document directly to a newline-delimited JSON file."""
    return asyncio.run(async_.write_jsonl(filepath, output, context))


def to_dict(
    filepath: str | Path,
    source_language: str = "",
    target_language: str = "",
    domain: str = "",
    *,
    fields: Iterable[DictField | str] = DEFAULT_DICT_FIELDS,
    strings: StringMode | str = StringMode.SANITIZED,
) -> Iterator[TranslationRow]:
    """Lazily project an interchange file into flat, string-only rows."""
    from lokit.data.dict_projection import iter_file_rows, normalize_fields, normalize_string_mode

    return iter_file_rows(
        filepath,
        source_language,
        target_language,
        domain,
        normalize_fields(fields),
        normalize_string_mode(strings),
    )
