from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.data.structure import Data
from lokit.parsers.tmx.models import TmxParseMode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from pathlib import Path

    from lokit.io.stream_json import LokitJsonContext
    from lokit.office.models import DocumentSource

ExtractItem = tuple[str, Data]
TmxBatch = list[ExtractItem]

__all__ = ["docx", "json", "pptx", "tmx", "tmx_batches", "xliff"]


def tmx(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    mode: TmxParseMode = TmxParseMode.FULL,
) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from a TMX file."""
    from lokit.importers import import_tmx_async

    return import_tmx_async(filepath, source_language, target_language, domain, mode)


def tmx_batches(
    filepath: str,
    source_language: str | None = None,
    target_language: str | None = None,
    domain: str | None = None,
    *,
    batch_size: int = 1000,
    mode: TmxParseMode = TmxParseMode.FULL,
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
    )


def xliff(filepath: str) -> AsyncIterator[ExtractItem]:
    """Asynchronously streams translation units from an XLIFF file."""
    from lokit.importers import import_xliff_async

    return import_xliff_async(filepath)


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


async def json(
    filepath: str | Path,
    output: str | Path,
    context: Iterable[LokitJsonContext | str] | None = None,
) -> Path:
    """Asynchronously streams document data directly to a JSON file."""
    from lokit.logic import Lokit

    return await Lokit.to_json_async(filepath, output, context)
