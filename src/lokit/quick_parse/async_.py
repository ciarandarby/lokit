from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["tmx_to_json", "xliff_from_json"]


async def tmx_to_json(
    source: str | Path,
    output: str | Path,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> Path:
    """Asynchronously streams selected TMX languages to Lokit JSON."""
    from lokit.io.stream_json import write_lokit_json_stream

    return await write_lokit_json_stream(
        source,
        output,
        source_language=source_language,
        target_language=target_language,
    )


async def xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    """Asynchronously converts a JSON source to an XLIFF file."""
    from lokit.exporters import export_xliff_from_json_async

    await export_xliff_from_json_async(source_json, target_xliff)
