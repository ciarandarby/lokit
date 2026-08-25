from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from lokit.types import TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from lokit.data.structure import ConversionStats
    from lokit.placeholders import PlaceholderSyntax

__all__ = [
    "csv_to_xliff",
    "tmx_from_json",
    "tmx_to_csv",
    "tmx_to_json",
    "tmx_to_tmx",
    "tmx_to_xliff",
    "xliff_from_json",
    "xlsx_to_xliff",
]


async def tmx_to_json(
    source: str | Path,
    output: str | Path,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
    include_tags: bool = False,
    tag_syntax: TagSyntax = TagSyntax.NATIVE,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    placeholder_syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
) -> Path:
    """Asynchronously streams selected TMX languages to Lokit JSON."""
    from lokit.io.stream_json import write_lokit_json_stream

    return await write_lokit_json_stream(
        source,
        output,
        source_language=source_language,
        target_language=target_language,
        include_tags=include_tags,
        tag_syntax=tag_syntax,
        unsupported_tags=unsupported_tags,
        runtime_placeholders=runtime_placeholders,
        inline_placeholders=inline_placeholders,
        placeholder_syntaxes=placeholder_syntaxes,
    )


async def tmx_to_csv(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Asynchronously converts TMX to CSV without blocking the event loop."""
    from lokit.importers import convert_tmx_to_csv

    return await asyncio.to_thread(
        convert_tmx_to_csv,
        source,
        target,
        source_language=source_language,
        target_language=target_language,
    )


async def tmx_to_tmx(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Asynchronously standardizes a TMX file."""
    from lokit.importers import convert_tmx_to_tmx

    return await asyncio.to_thread(
        convert_tmx_to_tmx,
        source,
        target,
        source_language=source_language,
        target_language=target_language,
    )


async def tmx_to_xliff(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Asynchronously converts TMX to XLIFF."""
    from lokit.importers import convert_tmx_to_xliff

    return await asyncio.to_thread(
        convert_tmx_to_xliff,
        source,
        target,
        source_language=source_language,
        target_language=target_language,
    )


async def csv_to_xliff(
    source: str,
    target: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    progress: bool = True,
) -> None:
    """Asynchronously converts CSV to XLIFF."""
    from lokit.importers import convert_csv_to_xliff

    await asyncio.to_thread(
        convert_csv_to_xliff,
        source,
        target,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
    )


async def xlsx_to_xliff(
    source: str,
    target: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    progress: bool = True,
) -> None:
    """Asynchronously converts XLSX to XLIFF."""
    from lokit.importers import convert_xlsx_to_xliff

    await asyncio.to_thread(
        convert_xlsx_to_xliff,
        source,
        target,
        source_locale=source_locale,
        target_locale=target_locale,
        progress=progress,
    )


async def xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    """Asynchronously converts a JSON source to an XLIFF file."""
    from lokit.exporters import export_xliff_from_json_async

    await export_xliff_from_json_async(source_json, target_xliff)


async def tmx_from_json(source_json: str | Path, target_tmx: str | Path) -> None:
    """Asynchronously converts a JSON source to TMX."""
    from lokit.exporters import export_tmx_from_json

    await asyncio.to_thread(export_tmx_from_json, source_json, target_tmx)
