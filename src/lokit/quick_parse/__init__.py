from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.quick_parse import async_ as async_

if TYPE_CHECKING:
    from pathlib import Path

    from lokit.data.structure import ConversionStats

__all__ = [
    "async_",
    "csv_to_xliff",
    "tmx_from_json",
    "tmx_to_csv",
    "tmx_to_json",
    "tmx_to_tmx",
    "tmx_to_xliff",
    "xliff_from_json",
    "xlsx_to_xliff",
]


def tmx_to_json(
    source: str | Path,
    output: str | Path,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> Path:
    """Converts a TMX file directly to Lokit JSON format."""
    from lokit.stream import json as stream_json

    context = ()
    return stream_json(source, output, context)


def tmx_to_csv(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Converts a TMX file directly to CSV format."""
    from lokit.importers import convert_tmx_to_csv

    return convert_tmx_to_csv(source, target, source_language=source_language, target_language=target_language)


def tmx_to_tmx(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Standardizes/rewrites a TMX file from source to target path."""
    from lokit.importers import convert_tmx_to_tmx

    return convert_tmx_to_tmx(source, target, source_language=source_language, target_language=target_language)


def tmx_to_xliff(
    source: str,
    target: str,
    *,
    source_language: str | None = None,
    target_language: str | None = None,
) -> ConversionStats:
    """Converts a TMX file directly to XLIFF format."""
    from lokit.importers import convert_tmx_to_xliff

    return convert_tmx_to_xliff(source, target, source_language=source_language, target_language=target_language)


def csv_to_xliff(
    source: str,
    target: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    progress: bool = True,
) -> None:
    """Converts a CSV file directly to XLIFF format."""
    from lokit.importers import convert_csv_to_xliff

    convert_csv_to_xliff(source, target, source_locale=source_locale, target_locale=target_locale, progress=progress)


def xlsx_to_xliff(
    source: str,
    target: str,
    *,
    source_locale: str = "",
    target_locale: str | None = None,
    progress: bool = True,
) -> None:
    """Converts an Excel sheet (XLSX) directly to XLIFF format."""
    from lokit.importers import convert_xlsx_to_xliff

    convert_xlsx_to_xliff(source, target, source_locale=source_locale, target_locale=target_locale, progress=progress)


def xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    """Exports an XLIFF file directly from a JSON source."""
    from lokit.exporters import export_xliff_from_json

    export_xliff_from_json(source_json, target_xliff)


def tmx_from_json(source_json: str | Path, target_tmx: str | Path) -> None:
    """Exports a TMX file directly from a JSON source."""
    from lokit.exporters import export_tmx_from_json

    export_tmx_from_json(source_json, target_tmx)
