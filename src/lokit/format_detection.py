from __future__ import annotations

import json
import zipfile
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import TYPE_CHECKING

from lokit.compat import StrEnum
from lokit.parsers.tmx.xml_utils import iterparse_safe, local_name

if TYPE_CHECKING:
    from typing import TextIO

_LOKIT_MAX_LINE_BYTES = 1024 * 1024
_JSON_PROBE_CHUNK_CHARS = 8192
_JSON_PROBE_CAPTURE_CHARS = 256
_JSON_PROBE_MAX_DEPTH = 128
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


class _MacroEnabledOfficeError(ValueError):
    pass


class _JsonProbeError(ValueError):
    pass


class _JsonTokenReader:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._buffer = ""
        self._offset = 0

    def next_token(self) -> tuple[str, str | None]:
        character = self._read_non_whitespace()
        if character is None:
            raise _JsonProbeError("Unexpected end of JSON input")
        if character in "{}[]:,":
            return character, None
        if character == '"':
            return "string", self._read_string()
        if character in "-0123456789tfn":
            self._read_primitive()
            return "primitive", None
        raise _JsonProbeError(f"Unexpected JSON character: {character!r}")

    def _read_non_whitespace(self) -> str | None:
        while True:
            character = self._read_character()
            if character is None or not character.isspace():
                return character

    def _read_character(self) -> str | None:
        if self._offset >= len(self._buffer):
            self._buffer = self._stream.read(_JSON_PROBE_CHUNK_CHARS)
            self._offset = 0
            if not self._buffer:
                return None
        character = self._buffer[self._offset]
        self._offset += 1
        return character

    def _unread_character(self) -> None:
        if self._offset < 1:
            raise _JsonProbeError("Cannot unread past the JSON probe buffer")
        self._offset -= 1

    def _read_string(self) -> str | None:
        captured = ['"']
        capture_enabled = True
        while True:
            character = self._read_character()
            if character is None:
                raise _JsonProbeError("Unterminated JSON string")
            capture_enabled = self._capture(captured, character, capture_enabled)
            if character == '"':
                break
            if character != "\\":
                continue
            escaped = self._read_character()
            if escaped is None:
                raise _JsonProbeError("Unterminated JSON escape")
            capture_enabled = self._capture(captured, escaped, capture_enabled)
            if escaped != "u":
                continue
            for _ in range(4):
                hexadecimal = self._read_character()
                if hexadecimal is None:
                    raise _JsonProbeError("Unterminated JSON unicode escape")
                capture_enabled = self._capture(captured, hexadecimal, capture_enabled)
        if not capture_enabled:
            return None
        try:
            decoded = json.loads("".join(captured))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _JsonProbeError("Invalid JSON string") from exc
        return decoded if isinstance(decoded, str) else None

    def _capture(self, captured: list[str], character: str, enabled: bool) -> bool:
        if not enabled:
            return False
        if len(captured) >= _JSON_PROBE_CAPTURE_CHARS:
            captured.clear()
            return False
        captured.append(character)
        return True

    def _read_primitive(self) -> None:
        while True:
            character = self._read_character()
            if character is None or character.isspace():
                return
            if character in "{}[]:,":
                self._unread_character()
                return


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


def detect_format(filepath: str | Path) -> LokitInputFormat:
    path = Path(filepath)
    suffix = path.suffix.lower()
    if suffix == ".lokit":
        return LokitInputFormat.LOKIT
    if suffix == ".csv":
        return LokitInputFormat.CSV
    if suffix == ".xlsx":
        return LokitInputFormat.XLSX
    if suffix == ".docx":
        return LokitInputFormat.DOCX
    if suffix == ".pptx":
        return LokitInputFormat.PPTX
    if suffix in _MACRO_ENABLED_OFFICE_SUFFIXES:
        raise ValueError(f"Macro-enabled Office files are not supported: {path}")
    if suffix in (".html", ".htm"):
        return LokitInputFormat.HTML
    if suffix in (".po", ".pot"):
        return LokitInputFormat.PO
    if suffix == ".idml":
        return LokitInputFormat.IDML
    if suffix == ".json":
        if _path_has_lokit_json_schema(path):
            return LokitInputFormat.LOKIT_JSON
        if _path_has_lokit_magic(path):
            return LokitInputFormat.LOKIT
        return LokitInputFormat.JSON_I18N
    if _path_has_lokit_magic(path):
        # Content wins over a missing or misleading generic/XML/JSON suffix.
        # The Rust parser remains responsible for validating the complete
        # envelope and schema version.
        return LokitInputFormat.LOKIT

    try:
        context = iterparse_safe(str(path), events=("start",))
        for _, element in context:
            return _format_from_root(local_name(element.tag))
    except Exception:
        pass

    raise ValueError(f"Could not detect input format for file: {path}")


def detect_format_from_bytes(data: bytes) -> LokitInputFormat:
    first_significant_line = _first_significant_line(data)
    if first_significant_line.startswith(b"@lokit"):
        # The Rust parser owns magic/version validation and can return its
        # stable, located diagnostic for unsupported or malformed envelopes.
        return LokitInputFormat.LOKIT

    chunk = data[:1000]
    stripped = chunk.lstrip()
    if not stripped:
        raise ValueError("Could not detect input format for empty byte input")

    if stripped.startswith(b"{"):
        with TextIOWrapper(BytesIO(data), encoding="utf-8-sig") as stream:
            if _is_lokit_json_stream(stream):
                return LokitInputFormat.LOKIT_JSON
        return LokitInputFormat.JSON_I18N

    if stripped.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                names = set(z.namelist())
                detected = _detect_zip_office_format(z, names)
                if detected is not None:
                    return detected
                if any(n.startswith("Stories/") for n in names):
                    return LokitInputFormat.IDML
        except _MacroEnabledOfficeError as exc:
            raise ValueError(str(exc)) from exc
        except Exception:
            pass
        else:
            raise ValueError("Could not detect input format for ZIP byte input")

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


def _first_significant_line(data: bytes) -> bytes:
    offset = 0
    while offset < len(data):
        newline = data.find(b"\n", offset)
        end = len(data) if newline < 0 else newline
        line = data[offset:end].rstrip(b"\r ")
        content = line.lstrip(b" ")
        if content and not line.lstrip(b" \t").startswith(b"#"):
            return line
        if newline < 0:
            break
        offset = newline + 1
    return b""


def _path_has_lokit_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            while True:
                line = stream.readline(_LOKIT_MAX_LINE_BYTES + 2)
                if not line:
                    return False
                physical_line = line[:-1] if line.endswith(b"\n") else line
                if len(physical_line) > _LOKIT_MAX_LINE_BYTES:
                    return False
                content = physical_line.rstrip(b"\r ")
                if content and not content.lstrip(b" \t").startswith(b"#"):
                    return content.startswith(b"@lokit")
    except OSError:
        return False


def _path_has_lokit_json_schema(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return _is_lokit_json_stream(stream)
    except (OSError, UnicodeError):
        return False


def _detect_zip_office_format(
    zf: zipfile.ZipFile,
    names: set[str],
) -> LokitInputFormat | None:
    names_by_lower = {name.lower(): name for name in names}
    lower_names = set(names_by_lower)
    content_types = b""
    content_types_name = names_by_lower.get("[content_types].xml")
    if content_types_name is not None:
        try:
            content_types = zf.read(content_types_name).lower()
        except Exception:
            return None
    if b"macroenabled" in content_types or any(
        name == "vbaproject.bin" or name.endswith("/vbaproject.bin") for name in lower_names
    ):
        raise _MacroEnabledOfficeError("Macro-enabled Office byte input is not supported")
    if "word/document.xml" in lower_names:
        return LokitInputFormat.DOCX
    if "ppt/presentation.xml" in lower_names:
        return LokitInputFormat.PPTX
    if "xl/workbook.xml" in lower_names:
        return LokitInputFormat.XLSX
    if not content_types:
        return None
    if b"wordprocessingml.document.main+xml" in content_types:
        return LokitInputFormat.DOCX
    if b"presentationml.presentation.main+xml" in content_types:
        return LokitInputFormat.PPTX
    if b"spreadsheetml.sheet.main+xml" in content_types:
        return LokitInputFormat.XLSX
    return None


def _is_lokit_json_stream(stream: TextIO) -> bool:
    try:
        reader = _JsonTokenReader(stream)
        if reader.next_token()[0] != "{":
            return False
        source_locale_is_string = False
        data_has_lokit_shape: bool | None = None
        token = reader.next_token()
        if token[0] == "}":
            return False
        while True:
            if token[0] != "string":
                raise _JsonProbeError("Expected a JSON object key")
            key = token[1]
            _expect_json_token(reader, ":")
            value = reader.next_token()
            if key == "source_locale":
                source_locale_is_string = value[0] == "string"
                if not source_locale_is_string:
                    _skip_json_value(reader, value, 1)
                if data_has_lokit_shape is not None:
                    return source_locale_is_string and data_has_lokit_shape
            elif key == "data":
                if source_locale_is_string:
                    return _probe_lokit_data(reader, value, consume_all=False)
                data_has_lokit_shape = _probe_lokit_data(reader, value, consume_all=True)
            else:
                _skip_json_value(reader, value, 1)
            separator = reader.next_token()[0]
            if separator == "}":
                return source_locale_is_string and data_has_lokit_shape is True
            if separator != ",":
                raise _JsonProbeError("Expected a JSON object separator")
            token = reader.next_token()
    except (OSError, UnicodeError, _JsonProbeError):
        return False


def _probe_lokit_data(
    reader: _JsonTokenReader,
    first: tuple[str, str | None],
    *,
    consume_all: bool,
) -> bool:
    if first[0] != "{":
        _skip_json_value(reader, first, 1)
        return False
    token = reader.next_token()
    if token[0] == "}":
        return True
    if token[0] != "string":
        raise _JsonProbeError("Expected a Lokit unit identifier")
    _expect_json_token(reader, ":")
    first_unit_has_source = _probe_lokit_unit(reader, reader.next_token(), consume_all=consume_all)
    if not consume_all:
        return first_unit_has_source
    separator = reader.next_token()[0]
    while separator == ",":
        if reader.next_token()[0] != "string":
            raise _JsonProbeError("Expected a Lokit unit identifier")
        _expect_json_token(reader, ":")
        _skip_json_value(reader, reader.next_token(), 2)
        separator = reader.next_token()[0]
    if separator != "}":
        raise _JsonProbeError("Expected the end of the Lokit data object")
    return first_unit_has_source


def _probe_lokit_unit(
    reader: _JsonTokenReader,
    first: tuple[str, str | None],
    *,
    consume_all: bool,
) -> bool:
    if first[0] != "{":
        _skip_json_value(reader, first, 2)
        return False
    source_is_string = False
    token = reader.next_token()
    if token[0] == "}":
        return False
    while True:
        if token[0] != "string":
            raise _JsonProbeError("Expected a Lokit unit field")
        key = token[1]
        _expect_json_token(reader, ":")
        value = reader.next_token()
        if key == "source":
            source_is_string = value[0] == "string"
            if source_is_string and not consume_all:
                return True
            if not source_is_string:
                _skip_json_value(reader, value, 3)
        else:
            _skip_json_value(reader, value, 3)
        separator = reader.next_token()[0]
        if separator == "}":
            return source_is_string
        if separator != ",":
            raise _JsonProbeError("Expected a Lokit unit field separator")
        token = reader.next_token()


def _skip_json_value(
    reader: _JsonTokenReader,
    first: tuple[str, str | None],
    depth: int,
) -> None:
    if depth > _JSON_PROBE_MAX_DEPTH:
        raise _JsonProbeError("JSON probe nesting limit exceeded")
    kind = first[0]
    if kind in ("string", "primitive"):
        return
    if kind == "{":
        token = reader.next_token()
        if token[0] == "}":
            return
        while True:
            if token[0] != "string":
                raise _JsonProbeError("Expected a JSON object key")
            _expect_json_token(reader, ":")
            _skip_json_value(reader, reader.next_token(), depth + 1)
            separator = reader.next_token()[0]
            if separator == "}":
                return
            if separator != ",":
                raise _JsonProbeError("Expected a JSON object separator")
            token = reader.next_token()
    if kind == "[":
        token = reader.next_token()
        if token[0] == "]":
            return
        while True:
            _skip_json_value(reader, token, depth + 1)
            separator = reader.next_token()[0]
            if separator == "]":
                return
            if separator != ",":
                raise _JsonProbeError("Expected a JSON array separator")
            token = reader.next_token()
    raise _JsonProbeError("Expected a JSON value")


def _expect_json_token(reader: _JsonTokenReader, expected: str) -> None:
    if reader.next_token()[0] != expected:
        raise _JsonProbeError(f"Expected JSON token {expected!r}")


def _format_from_root(root_name: str) -> LokitInputFormat:
    root_name_lower = root_name.lower()
    if root_name_lower == "tmx":
        return LokitInputFormat.TMX
    if root_name_lower == "xliff":
        return LokitInputFormat.XLIFF
    if root_name_lower == "html":
        return LokitInputFormat.HTML
    raise ValueError(f"Unsupported localization format root: {root_name}")
