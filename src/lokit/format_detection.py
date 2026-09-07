from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lokit.compat import StrEnum

if TYPE_CHECKING:
    from os import PathLike

_MACRO_ENABLED_OFFICE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".docm",
        ".dotm",
        ".pptm",
        ".potm",
        ".ppsm",
        ".sldm",
        ".xlsm",
        ".xltm",
        ".xlam",
    }
)


class LokitInputFormat(StrEnum):
    LOKIT = "lokit"
    TMX = "tmx"
    XLIFF = "xliff"
    LOKIT_JSON = "lokit_json"
    CSV = "csv"
    XLSX = "xlsx"
    DOCX = "docx"
    PPTX = "pptx"
    HTML = "html"
    PO = "po"
    JSON_I18N = "json_i18n"
    IDML = "idml"


def detect_format(filepath: str | PathLike[str]) -> LokitInputFormat:
    from lokit._interchange_rust import detect_archive_path, detect_text_path

    path = Path(filepath)
    suffix = path.suffix.lower()
    if suffix in _MACRO_ENABLED_OFFICE_SUFFIXES:
        raise ValueError(f"Macro-enabled Office files are not supported: {path}")
    if suffix in (".xlsx", ".docx", ".pptx"):
        detect_archive_path(str(path))
        return LokitInputFormat(suffix[1:])
    if suffix in (".lokit", ".csv", ".idml"):
        return LokitInputFormat(suffix[1:])
    if suffix in (".html", ".htm"):
        return LokitInputFormat.HTML
    if suffix in (".po", ".pot"):
        return LokitInputFormat.PO
    detected = detect_text_path(str(path), suffix == ".json")
    if detected == "zip":
        detected = detect_archive_path(str(path))
    if detected is not None:
        return LokitInputFormat(detected)
    raise ValueError(f"Could not detect input format for file: {path}")


def detect_format_from_bytes(data: bytes) -> LokitInputFormat:
    from lokit._interchange_rust import detect_archive_bytes, detect_text_bytes

    detected = detect_text_bytes(data)
    if detected == "zip":
        detected = detect_archive_bytes(data)
    if detected is not None:
        return LokitInputFormat(detected)
    raise ValueError("Could not detect input format for byte input")
