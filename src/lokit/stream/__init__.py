from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from lokit.parsers.tmx.models import TmxParseMode
from lokit.stream import async_ as async_

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from lokit.data.structure import StreamingStructure
    from lokit.io.stream_json import LokitJsonContext
    from lokit.office.models import DocumentSource
    from lokit.parsers.tmx.parallel import TmxParallelOptions

__all__ = ["async_", "docx", "json", "pptx", "tmx", "tmx_parallel", "xliff"]


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> StreamingStructure:
    """Streams a TMX document as a StreamingStructure."""
    from lokit.importers import stream_tmx

    return stream_tmx(filepath, source_language, target_language, mode)


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


def xliff(filepath: str) -> StreamingStructure:
    """Streams an XLIFF document as a StreamingStructure."""
    from lokit.importers import stream_xliff

    return stream_xliff(filepath)


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


def json(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
) -> Path:
    """Streams a document directly to a JSON file."""
    return asyncio.run(async_.json(filepath, output, context))
