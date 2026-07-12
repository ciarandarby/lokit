from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(slots=True)
class PoEntryRecord:
    obsolete: int = 0
    msgctxt: str | None = None
    msgid: str = ""
    msgid_plural: str = ""
    msgstr: str = ""
    msgstr_plural: dict[int, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    comment: str = ""
    tcomment: str = ""
    occurrences: list[tuple[str, str]] = field(default_factory=list)


class _Field:
    MSGCTXT: Final[int] = 1
    MSGID: Final[int] = 2
    MSGID_PLURAL: Final[int] = 3
    MSGSTR: Final[int] = 4
    MSGSTR_PLURAL: Final[int] = 5


_PLURAL_FIELD = re.compile(r"msgstr\[(\d+)\]\s+(.*)")


def iter_po_entries(path: str | Path) -> Iterator[PoEntryRecord]:
    current = PoEntryRecord()
    field_kind = 0
    plural_index = 0
    has_fields = False
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        for raw_line in source:
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                if has_fields:
                    yield current
                current = PoEntryRecord()
                field_kind = 0
                plural_index = 0
                has_fields = False
                continue
            if line.startswith("#~"):
                current.obsolete = 1
                line = line[2:].lstrip()
                if not line:
                    continue
            if line.startswith("#,"):
                current.flags.extend(item.strip() for item in line[2:].split(",") if item.strip())
                continue
            if line.startswith("#:"):
                current.occurrences.extend(_occurrences(line[2:].strip()))
                continue
            if line.startswith("#."):
                current.comment = _append_comment(current.comment, line[2:].strip())
                continue
            if line.startswith("#"):
                current.tcomment = _append_comment(current.tcomment, line[1:].strip())
                continue
            if line.startswith("msgctxt "):
                current.msgctxt = _decode_po_string(line[8:].strip())
                field_kind = _Field.MSGCTXT
                has_fields = True
                continue
            if line.startswith("msgid_plural "):
                current.msgid_plural = _decode_po_string(line[13:].strip())
                field_kind = _Field.MSGID_PLURAL
                has_fields = True
                continue
            if line.startswith("msgid "):
                current.msgid = _decode_po_string(line[6:].strip())
                field_kind = _Field.MSGID
                has_fields = True
                continue
            if line.startswith("msgstr "):
                current.msgstr = _decode_po_string(line[7:].strip())
                field_kind = _Field.MSGSTR
                has_fields = True
                continue
            plural_match = _PLURAL_FIELD.fullmatch(line)
            if plural_match is not None:
                plural_index = int(plural_match.group(1))
                current.msgstr_plural[plural_index] = _decode_po_string(plural_match.group(2).strip())
                field_kind = _Field.MSGSTR_PLURAL
                has_fields = True
                continue
            if line.startswith('"'):
                value = _decode_po_string(line.strip())
                _append_field(current, field_kind, plural_index, value)
                continue
            raise ValueError(f"Unsupported PO syntax: {line!r}")
    if has_fields:
        yield current


def metadata_from_header(entry: PoEntryRecord) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in entry.msgstr.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    return metadata


def _append_field(entry: PoEntryRecord, field_kind: int, plural_index: int, value: str) -> None:
    if field_kind == _Field.MSGCTXT:
        entry.msgctxt = (entry.msgctxt or "") + value
    elif field_kind == _Field.MSGID:
        entry.msgid += value
    elif field_kind == _Field.MSGID_PLURAL:
        entry.msgid_plural += value
    elif field_kind == _Field.MSGSTR:
        entry.msgstr += value
    elif field_kind == _Field.MSGSTR_PLURAL:
        entry.msgstr_plural[plural_index] = entry.msgstr_plural.get(plural_index, "") + value
    else:
        raise ValueError("PO continuation appears before a field")


def _decode_po_string(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError(f"Invalid PO string literal: {value!r}")
    source = value[1:-1]
    output: list[str] = []
    index = 0
    escapes: dict[str, str] = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
    }
    while index < len(source):
        char = source[index]
        index += 1
        if char != "\\":
            output.append(char)
            continue
        if index >= len(source):
            raise ValueError("PO string ends with an escape prefix")
        escaped = source[index]
        index += 1
        replacement = escapes.get(escaped)
        if replacement is not None:
            output.append(replacement)
            continue
        if escaped in "01234567":
            digits = escaped
            while index < len(source) and len(digits) < 3 and source[index] in "01234567":
                digits += source[index]
                index += 1
            output.append(chr(int(digits, 8)))
            continue
        if escaped == "x":
            start = index
            while index < len(source) and source[index].lower() in "0123456789abcdef":
                index += 1
            if start == index:
                raise ValueError("PO hexadecimal escape has no digits")
            output.append(chr(int(source[start:index], 16)))
            continue
        output.append(escaped)
    return "".join(output)


def _occurrences(value: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in value.split():
        filename, separator, line = item.rpartition(":")
        result.append((filename, line) if separator and line.isdigit() else (item, ""))
    return result


def _append_comment(current: str, value: str) -> str:
    return f"{current}\n{value}" if current else value


__all__ = ["PoEntryRecord", "iter_po_entries", "metadata_from_header"]
