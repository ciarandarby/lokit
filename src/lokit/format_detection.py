from __future__ import annotations

import json
import re
import zipfile
from enum import StrEnum
from io import BytesIO
from pathlib import Path

from lokit.parsers.tmx.xml_utils import iterparse_safe, local_name

_JSON_FORMAT_RE = re.compile(r'"(?:format_version|data)"\s*:')


class LokitInputFormat(StrEnum):
    TMX = "tmx"
    XLIFF = "xliff"
    LOKIT_JSON = "lokit_json"
    CSV = "csv"
    XLSX = "xlsx"
    HTML = "html"
    PO = "po"
    JSON_I18N = "json_i18n"
    IDML = "idml"


def detect_format(filepath: str | Path) -> LokitInputFormat:
    path = Path(filepath)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return LokitInputFormat.CSV
    if suffix == ".xlsx":
        return LokitInputFormat.XLSX
    if suffix in (".html", ".htm"):
        return LokitInputFormat.HTML
    if suffix == ".po":
        return LokitInputFormat.PO
    if suffix == ".idml":
        return LokitInputFormat.IDML
    if suffix == ".json":
        try:
            with path.open("rb") as f:
                data = f.read(4096)
            if _JSON_FORMAT_RE.search(data.decode("utf-8", errors="ignore")):
                return LokitInputFormat.LOKIT_JSON
        except Exception:
            pass
        return LokitInputFormat.JSON_I18N

    try:
        context = iterparse_safe(str(path), events=("start",))
        for _, element in context:
            return _format_from_root(local_name(element.tag))
    except Exception:
        pass

    raise ValueError(f"Could not detect input format for file: {path}")


def detect_format_from_bytes(data: bytes) -> LokitInputFormat:
    chunk = data[:1000]
    stripped = chunk.lstrip()
    if not stripped:
        raise ValueError("Could not detect input format for empty byte input")

    if stripped.startswith(b"{"):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict) and ("format_version" in parsed or "data" in parsed):
                return LokitInputFormat.LOKIT_JSON
        except Exception:
            pass
        return LokitInputFormat.JSON_I18N

    if stripped.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                names = z.namelist()
                if any(n.startswith("Stories/") for n in names):
                    return LokitInputFormat.IDML
                return LokitInputFormat.XLSX
        except Exception:
            pass

    if stripped.startswith(b"<"):
        try:
            context = iterparse_safe(BytesIO(data), events=("start",))
            for _, element in context:
                tag = local_name(element.tag).lower()
                if tag == "tmx":
                    return LokitInputFormat.TMX
                if tag == "xliff":
                    return LokitInputFormat.XLIFF
                if tag in ("html", "head", "body", "p", "div"):
                    return LokitInputFormat.HTML
        except Exception:
            pass
        if b"<!doctype html" in stripped.lower() or b"<html" in stripped.lower():
            return LokitInputFormat.HTML

    if b"msgid" in stripped:
        return LokitInputFormat.PO

    if b"," in stripped or b";" in stripped or b"\t" in stripped:
        return LokitInputFormat.CSV

    raise ValueError("Could not detect input format for byte input")


def _format_from_root(root_name: str) -> LokitInputFormat:
    root_name_lower = root_name.lower()
    if root_name_lower == "tmx":
        return LokitInputFormat.TMX
    if root_name_lower == "xliff":
        return LokitInputFormat.XLIFF
    if root_name_lower == "html":
        return LokitInputFormat.HTML
    raise ValueError(f"Unsupported localization format root: {root_name}")
