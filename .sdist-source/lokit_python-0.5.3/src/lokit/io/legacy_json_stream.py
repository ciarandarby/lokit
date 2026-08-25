from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TextIO, cast

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.io.json import JsonObject, JsonValue, _parse_base, _parse_data

if TYPE_CHECKING:
    from collections.abc import Iterator

_READ_CHUNK_CHARS = 64 * 1024
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
_MAX_JSON_DEPTH = 256
_STRING_SPECIAL_RE = re.compile(r'["\\\x00-\x1f]')
_RAW_STRING_BOUNDARY_RE = re.compile(r'["\\]')
_COMPOSITE_BOUNDARY_RE = re.compile(r'["{}\[\]]')
_METADATA_KEYS = frozenset(
    {
        "source_locale",
        "target_locale",
        "target_locales",
        "format_version",
        "export_origin",
        "export_timestamp",
        "source_language",
        "target_language",
        "target_languages",
        "extensions",
    }
)


class _JsonStreamReader:
    """Small bounded-buffer JSON reader used for envelope navigation.

    Complete metadata values and individual translation units are decoded by
    the standard-library JSON decoder. Values that the legacy schema ignores
    are validated and discarded without first materializing them.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._buffer = ""
        self._position = 0
        self._buffer_start = 0
        self._at_eof = False

    def skip_whitespace(self) -> None:
        while True:
            character = self.peek()
            if character is None or not character.isspace():
                return
            self._position += 1

    def peek(self) -> str | None:
        if self._position >= len(self._buffer) and not self._refill():
            return None
        return self._buffer[self._position]

    def take(self) -> str:
        character = self.peek()
        if character is None:
            self.fail("Unexpected end of JSON input")
        self._position += 1
        return character

    def expect(self, expected: str) -> None:
        self.skip_whitespace()
        actual = self.take()
        if actual != expected:
            self.fail(f"Expected {expected!r}, got {actual!r}")

    def read_member_name(self) -> str:
        self.skip_whitespace()
        if self.peek() != '"':
            self.fail("Expected a JSON object member name")
        value = self.read_value()
        if not isinstance(value, str):
            self.fail("Expected a JSON object member name")
        return value

    def read_value(self) -> JsonValue:
        raw = self.read_raw_value()
        try:
            return cast("JsonValue", json.loads(raw))
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(exc.msg, raw, exc.pos) from exc

    def read_raw_value(self) -> str:
        """Read one bounded, syntactically delimited JSON value as source text."""
        self.skip_whitespace()
        return self._read_raw_value()

    def skip_value(self, depth: int = 0) -> None:
        if depth > _MAX_JSON_DEPTH:
            self.fail(f"JSON nesting exceeds the {_MAX_JSON_DEPTH}-level safety limit")
        self.skip_whitespace()
        character = self.peek()
        if character is None:
            self.fail("Expected a JSON value")
        if character == '"':
            self._skip_string()
            return
        if character == "{":
            self._skip_object(depth)
            return
        if character == "[":
            self._skip_array(depth)
            return
        if character in "-0123456789":
            self._skip_number()
            return
        if character == "t":
            self._skip_literal("true")
            return
        if character == "f":
            self._skip_literal("false")
            return
        if character == "n":
            self._skip_literal("null")
            return
        self.fail(f"Unexpected JSON character {character!r}")

    def ensure_finished(self) -> None:
        self.skip_whitespace()
        if self.peek() is not None:
            self.fail("Extra data after the top-level JSON object")

    def fail(self, message: str) -> NoReturn:
        absolute_position = self._buffer_start + self._position
        located_message = f"{message} at character offset {absolute_position}"
        raise json.JSONDecodeError(located_message, self._buffer, self._position)

    def _refill(self) -> bool:
        if self._at_eof:
            return False
        self._buffer_start += len(self._buffer)
        self._buffer = self._stream.read(_READ_CHUNK_CHARS)
        self._position = 0
        if not self._buffer:
            self._at_eof = True
            return False
        return True

    def _read_raw_value(self) -> str:
        first = self.peek()
        if first is None:
            self.fail("Expected a JSON value")
        if first == '"':
            return self._read_raw_string()
        if first in "[{":
            return self._read_raw_composite()
        return self._read_raw_primitive()

    def _read_raw_string(self) -> str:
        if self.take() != '"':
            self.fail("Expected a JSON string")
        parts = ['"']
        captured_bytes = 1
        escaped_across_buffer = False
        while True:
            if self._position >= len(self._buffer) and not self._refill():
                self.fail("Unterminated JSON string")
            start = self._position
            if escaped_across_buffer:
                self._position += 1
                escaped_across_buffer = False
            while self._position < len(self._buffer):
                match = _STRING_SPECIAL_RE.search(self._buffer, self._position)
                if match is None:
                    self._position = len(self._buffer)
                    break
                self._position = match.end()
                character = match.group()
                if character == "\\":
                    if self._position >= len(self._buffer):
                        escaped_across_buffer = True
                        break
                    self._position += 1
                    continue
                if character == '"':
                    captured_bytes = self._capture(
                        parts,
                        self._buffer[start : self._position],
                        captured_bytes,
                    )
                    return "".join(parts)
            captured_bytes = self._capture(
                parts,
                self._buffer[start : self._position],
                captured_bytes,
            )

    def _read_raw_composite(self) -> str:
        parts: list[str] = []
        stack: list[str] = []
        in_string = False
        escaped_across_buffer = False
        captured_bytes = 0
        while True:
            if self._position >= len(self._buffer) and not self._refill():
                self.fail("Unterminated JSON value")
            start = self._position
            if escaped_across_buffer:
                self._position += 1
                escaped_across_buffer = False
            while self._position < len(self._buffer):
                pattern = _RAW_STRING_BOUNDARY_RE if in_string else _COMPOSITE_BOUNDARY_RE
                match = pattern.search(self._buffer, self._position)
                if match is None:
                    self._position = len(self._buffer)
                    break
                self._position = match.end()
                character = match.group()
                if in_string:
                    if character == "\\":
                        if self._position >= len(self._buffer):
                            escaped_across_buffer = True
                            break
                        self._position += 1
                    else:
                        in_string = False
                    continue
                if character == '"':
                    in_string = True
                elif character == "{":
                    stack.append("}")
                    if len(stack) > _MAX_JSON_DEPTH:
                        self.fail(f"JSON nesting exceeds the {_MAX_JSON_DEPTH}-level safety limit")
                elif character == "[":
                    stack.append("]")
                    if len(stack) > _MAX_JSON_DEPTH:
                        self.fail(f"JSON nesting exceeds the {_MAX_JSON_DEPTH}-level safety limit")
                elif character in "}]":
                    if not stack or character != stack.pop():
                        self.fail("Mismatched JSON delimiter")
                    if not stack:
                        captured_bytes = self._capture(
                            parts,
                            self._buffer[start : self._position],
                            captured_bytes,
                        )
                        return "".join(parts)
            captured_bytes = self._capture(
                parts,
                self._buffer[start : self._position],
                captured_bytes,
            )

    def _read_raw_primitive(self) -> str:
        parts: list[str] = []
        captured_bytes = 0
        while True:
            if self._position >= len(self._buffer) and not self._refill():
                break
            start = self._position
            while self._position < len(self._buffer):
                character = self._buffer[self._position]
                if character.isspace() or character in ",]}":
                    captured_bytes = self._capture(
                        parts,
                        self._buffer[start : self._position],
                        captured_bytes,
                    )
                    return "".join(parts)
                self._position += 1
            captured_bytes = self._capture(
                parts,
                self._buffer[start : self._position],
                captured_bytes,
            )
        return "".join(parts)

    def _capture(self, parts: list[str], fragment: str, captured_bytes: int) -> int:
        captured_bytes += len(fragment.encode("utf-8"))
        if captured_bytes > _MAX_CAPTURE_BYTES:
            self.fail(f"JSON value exceeds the {_MAX_CAPTURE_BYTES}-byte safety limit")
        parts.append(fragment)
        return captured_bytes

    def _skip_string(self) -> None:
        if self.take() != '"':
            self.fail("Expected a JSON string")
        while True:
            if self._position >= len(self._buffer) and not self._refill():
                self.fail("Unterminated JSON string")
            match = _STRING_SPECIAL_RE.search(self._buffer, self._position)
            if match is None:
                self._position = len(self._buffer)
                continue
            self._position = match.end()
            character = match.group()
            if character == '"':
                return
            if character == "\\":
                self._skip_escape()
                continue
            self.fail("Unescaped control character in JSON string")

    def _skip_escape(self) -> None:
        escaped = self.take()
        if escaped in '"\\/bfnrt':
            return
        if escaped != "u":
            self.fail("Invalid JSON escape")
        for _ in range(4):
            if self.take() not in "0123456789abcdefABCDEF":
                self.fail("Invalid JSON unicode escape")

    def _skip_object(self, depth: int) -> None:
        self.expect("{")
        self.skip_whitespace()
        if self.peek() == "}":
            self.take()
            return
        while True:
            self._skip_string()
            self.expect(":")
            self.skip_value(depth + 1)
            self.skip_whitespace()
            separator = self.take()
            if separator == "}":
                return
            if separator != ",":
                self.fail("Expected ',' or '}' in JSON object")
            self.skip_whitespace()

    def _skip_array(self, depth: int) -> None:
        self.expect("[")
        self.skip_whitespace()
        if self.peek() == "]":
            self.take()
            return
        while True:
            self.skip_value(depth + 1)
            self.skip_whitespace()
            separator = self.take()
            if separator == "]":
                return
            if separator != ",":
                self.fail("Expected ',' or ']' in JSON array")

    def _skip_number(self) -> None:
        if self.peek() == "-":
            self.take()
        first = self.peek()
        if first == "0":
            self.take()
            following = self.peek()
            if following is not None and following.isdigit():
                self.fail("Leading zero in JSON number")
        elif first is not None and first in "123456789":
            self._skip_digits()
        else:
            self.fail("Invalid JSON number")
        if self.peek() == ".":
            self.take()
            if self.peek() is None or not cast("str", self.peek()).isdigit():
                self.fail("Expected digit after JSON decimal point")
            self._skip_digits()
        if self.peek() in ("e", "E"):
            self.take()
            if self.peek() in ("+", "-"):
                self.take()
            if self.peek() is None or not cast("str", self.peek()).isdigit():
                self.fail("Expected digit in JSON exponent")
            self._skip_digits()
        self._ensure_value_boundary()

    def _skip_digits(self) -> None:
        while True:
            character = self.peek()
            if character is None or not character.isdigit():
                return
            self.take()

    def _skip_literal(self, literal: str) -> None:
        for expected in literal:
            if self.take() != expected:
                self.fail(f"Invalid JSON literal; expected {literal!r}")
        self._ensure_value_boundary()

    def _ensure_value_boundary(self) -> None:
        character = self.peek()
        if character is not None and not character.isspace() and character not in ",]}":
            self.fail("Invalid character after JSON value")


def stream_lokit_json(filepath: str | Path) -> StreamingStructure:
    """Open a legacy BaseStructure JSON file as a bounded-memory stream.

    JSON permits top-level members in any order, including metadata after the
    potentially very large ``data`` object. The bounded metadata pass therefore
    precedes a lazy item pass over the selected ``data`` member.
    """

    path = Path(filepath)
    metadata, data_occurrence = _read_metadata(path)
    return StreamingStructure(
        source_locale=metadata.source_locale,
        target_locale=metadata.target_locale,
        items=_iter_data(path, data_occurrence),
        target_locales=metadata.target_locales,
        format_version=metadata.format_version,
        export_origin=metadata.export_origin,
        export_timestamp=metadata.export_timestamp,
        source_language=metadata.source_language,
        target_language=metadata.target_language,
        target_languages=metadata.target_languages,
        extensions=metadata.extensions,
    )


def _read_metadata(path: Path) -> tuple[BaseStructure, int | None]:
    raw_metadata: dict[str, JsonValue] = {}
    data_occurrences = 0
    last_data_is_object = True
    last_data_type = "dict"
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = _JsonStreamReader(stream)
        reader.expect("{")
        reader.skip_whitespace()
        if reader.peek() != "}":
            while True:
                key = reader.read_member_name()
                reader.expect(":")
                reader.skip_whitespace()
                if key == "data":
                    data_occurrences += 1
                    last_data_is_object = reader.peek() == "{"
                    if last_data_is_object:
                        last_data_type = "dict"
                        reader.skip_value()
                    else:
                        last_data_type = type(reader.read_value()).__name__
                elif key in _METADATA_KEYS:
                    raw_metadata[key] = reader.read_value()
                else:
                    reader.skip_value()
                reader.skip_whitespace()
                separator = reader.take()
                if separator == "}":
                    break
                if separator != ",":
                    reader.fail("Expected ',' or '}' in top-level JSON object")
                reader.skip_whitespace()
        else:
            reader.take()
        reader.ensure_finished()

    if data_occurrences and not last_data_is_object:
        raise TypeError(f"Expected JSON object, got {last_data_type}")
    raw_metadata["data"] = {}
    metadata = _parse_base(cast("JsonObject", raw_metadata))
    return metadata, data_occurrences or None


def _iter_data(path: Path, selected_occurrence: int | None) -> Iterator[tuple[str, Data]]:
    if selected_occurrence is None:
        return
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = _JsonStreamReader(stream)
        reader.expect("{")
        reader.skip_whitespace()
        occurrence = 0
        if reader.peek() != "}":
            while True:
                key = reader.read_member_name()
                reader.expect(":")
                if key == "data":
                    occurrence += 1
                    if occurrence == selected_occurrence:
                        yield from _iter_data_object(reader)
                    else:
                        reader.skip_value()
                else:
                    reader.skip_value()
                reader.skip_whitespace()
                separator = reader.take()
                if separator == "}":
                    break
                if separator != ",":
                    reader.fail("Expected ',' or '}' in top-level JSON object")
                reader.skip_whitespace()
        else:
            reader.take()
        reader.ensure_finished()


def _iter_data_object(reader: _JsonStreamReader) -> Iterator[tuple[str, Data]]:
    reader.expect("{")
    reader.skip_whitespace()
    if reader.peek() == "}":
        reader.take()
        return
    while True:
        unit_id = reader.read_member_name()
        reader.expect(":")
        raw_data = reader.read_value()
        if not isinstance(raw_data, dict):
            raise TypeError(f"Expected JSON object, got {type(raw_data).__name__}")
        yield unit_id, _parse_data(cast("JsonObject", raw_data))
        reader.skip_whitespace()
        separator = reader.take()
        if separator == "}":
            return
        if separator != ",":
            reader.fail("Expected ',' or '}' in JSON data object")
        reader.skip_whitespace()
