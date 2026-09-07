from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

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
    comment_lines: list[str] = field(default_factory=list)
    tcomment_lines: list[str] = field(default_factory=list)
    occurrences: list[tuple[str, str]] = field(default_factory=list)
    previous: list[str] = field(default_factory=list)


class _Field:
    MSGCTXT: Final[int] = 1
    MSGID: Final[int] = 2
    MSGID_PLURAL: Final[int] = 3
    MSGSTR: Final[int] = 4
    MSGSTR_PLURAL: Final[int] = 5


_PLURAL_FIELD = re.compile(r"msgstr\[(\d+)\]\s+(.*)")
_NON_WHITESPACE = re.compile(r"\S+")
_COMMA_FIELD = re.compile(r"[^,]+")
_MAX_PO_LINE_BYTES = 1024 * 1024
_MAX_PO_ENTRY_BYTES = 16 * 1024 * 1024
_MAX_PO_ENTRY_LINES: Final[int] = 65_536
_MAX_REPEATED_FIELDS: Final[int] = 65_536
_ItemT = TypeVar("_ItemT")
_ESCAPES: Final[dict[str, str]] = {
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


@dataclass(slots=True)
class _PoEntryBuilder:
    obsolete: int = 0
    msgctxt: list[str] | None = None
    msgid: list[str] = field(default_factory=list)
    msgid_plural: list[str] = field(default_factory=list)
    msgstr: list[str] = field(default_factory=list)
    msgstr_plural: dict[int, list[str]] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    comment_lines: list[str] = field(default_factory=list)
    tcomment_lines: list[str] = field(default_factory=list)
    occurrences: list[tuple[str, str]] = field(default_factory=list)
    previous: list[str] = field(default_factory=list)

    def build(self) -> PoEntryRecord:
        return PoEntryRecord(
            obsolete=self.obsolete,
            msgctxt=None if self.msgctxt is None else "".join(self.msgctxt),
            msgid="".join(self.msgid),
            msgid_plural="".join(self.msgid_plural),
            msgstr="".join(self.msgstr),
            msgstr_plural={index: "".join(parts) for index, parts in self.msgstr_plural.items()},
            flags=self.flags,
            comment="\n".join(self.comment_lines),
            tcomment="\n".join(self.tcomment_lines),
            comment_lines=self.comment_lines,
            tcomment_lines=self.tcomment_lines,
            occurrences=self.occurrences,
            previous=self.previous,
        )


def iter_po_entries(path: str | Path) -> Iterator[PoEntryRecord]:
    current = _PoEntryBuilder()
    field_kind = 0
    plural_index = 0
    has_fields = False
    entry_bytes = 0
    entry_lines = 0
    for line, physical_bytes in _iter_physical_lines(Path(path)):
        if not line or line.isspace():
            if has_fields:
                yield current.build()
            current = _PoEntryBuilder()
            field_kind = 0
            plural_index = 0
            has_fields = False
            entry_bytes = 0
            entry_lines = 0
            continue

        entry_bytes += physical_bytes
        entry_lines += 1
        if entry_bytes > _MAX_PO_ENTRY_BYTES:
            raise ValueError(f"PO entry exceeds the {_MAX_PO_ENTRY_BYTES}-byte raw-input limit")
        if entry_lines > _MAX_PO_ENTRY_LINES:
            raise ValueError(f"PO entry exceeds the {_MAX_PO_ENTRY_LINES}-line limit")

        if line.startswith("#~"):
            current.obsolete = 1
            line = line[2:].lstrip()
            if not line:
                continue
        if line.startswith("#,"):
            _extend_flags(current.flags, line[2:])
            continue
        if line.startswith("#:"):
            _extend_occurrences(current.occurrences, line[2:])
            continue
        if line.startswith("#|"):
            _append_bounded(current.previous, line[2:].strip(), "previous-line")
            continue
        if line.startswith("#."):
            _append_bounded(current.comment_lines, line[2:].strip(), "extracted-comment")
            continue
        if line.startswith("#"):
            _append_bounded(current.tcomment_lines, line[1:].strip(), "translator-comment")
            continue
        if line.startswith("msgctxt "):
            current.msgctxt = [_decode_po_string(line[8:].strip())]
            field_kind = _Field.MSGCTXT
            has_fields = True
            continue
        if line.startswith("msgid_plural "):
            current.msgid_plural = [_decode_po_string(line[13:].strip())]
            field_kind = _Field.MSGID_PLURAL
            has_fields = True
            continue
        if line.startswith("msgid "):
            current.msgid = [_decode_po_string(line[6:].strip())]
            field_kind = _Field.MSGID
            has_fields = True
            continue
        if line.startswith("msgstr "):
            current.msgstr = [_decode_po_string(line[7:].strip())]
            field_kind = _Field.MSGSTR
            has_fields = True
            continue
        if line.startswith("msgstr["):
            plural_match = _PLURAL_FIELD.fullmatch(line)
            if plural_match is not None:
                plural_index = int(plural_match.group(1))
                current.msgstr_plural[plural_index] = [_decode_po_string(plural_match.group(2).strip())]
                field_kind = _Field.MSGSTR_PLURAL
                has_fields = True
                continue
        if line.startswith('"'):
            value = _decode_po_string(line.strip())
            _append_field(current, field_kind, plural_index, value)
            continue
        raise ValueError(f"Unsupported PO syntax: {line!r}")
    if has_fields:
        yield current.build()


def _iter_physical_lines(path: Path) -> Iterator[tuple[str, int]]:
    first = True
    with path.open("rb") as source:
        while True:
            raw_line = source.readline(_MAX_PO_LINE_BYTES + 3)
            if not raw_line:
                return
            content = raw_line
            if content.endswith(b"\n"):
                content = content[:-1]
                if content.endswith(b"\r"):
                    content = content[:-1]
            if len(content) > _MAX_PO_LINE_BYTES:
                raise ValueError(f"PO physical line exceeds the {_MAX_PO_LINE_BYTES}-byte content limit")
            encoding = "utf-8-sig" if first else "utf-8"
            first = False
            yield content.decode(encoding), len(raw_line)


def metadata_from_header(entry: PoEntryRecord) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in entry.msgstr.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    return metadata


def _append_field(entry: _PoEntryBuilder, field_kind: int, plural_index: int, value: str) -> None:
    if field_kind == _Field.MSGCTXT:
        if entry.msgctxt is None:
            entry.msgctxt = []
        entry.msgctxt.append(value)
    elif field_kind == _Field.MSGID:
        entry.msgid.append(value)
    elif field_kind == _Field.MSGID_PLURAL:
        entry.msgid_plural.append(value)
    elif field_kind == _Field.MSGSTR:
        entry.msgstr.append(value)
    elif field_kind == _Field.MSGSTR_PLURAL:
        entry.msgstr_plural.setdefault(plural_index, []).append(value)
    else:
        raise ValueError("PO continuation appears before a field")


def _decode_po_string(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError(f"Invalid PO string literal: {value!r}")
    source = value[1:-1]
    if "\\" not in source:
        return source
    output: list[str] = []
    index = 0
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
        replacement = _ESCAPES.get(escaped)
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


def _extend_flags(target: list[str], value: str) -> None:
    for match in _COMMA_FIELD.finditer(value):
        flag = match.group().strip()
        if flag:
            _append_bounded(target, flag, "flag")


def _extend_occurrences(target: list[tuple[str, str]], value: str) -> None:
    for match in _NON_WHITESPACE.finditer(value):
        item = match.group()
        filename, separator, line = item.rpartition(":")
        occurrence = (filename, line) if separator and line.isdigit() else (item, "")
        _append_bounded(target, occurrence, "reference")


def _append_bounded(target: list[_ItemT], value: _ItemT, field_name: str) -> None:
    if len(target) >= _MAX_REPEATED_FIELDS:
        raise ValueError(f"PO entry exceeds the {_MAX_REPEATED_FIELDS}-{field_name} limit")
    target.append(value)


__all__ = ["PoEntryRecord", "iter_po_entries", "metadata_from_header"]
