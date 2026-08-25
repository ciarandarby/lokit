from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lokit.io.legacy_json_stream import _JsonStreamReader
from lokit.tabular import normalize_language_header

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


_MAX_JSON_DEPTH = 256


@dataclass(frozen=True, slots=True)
class LocaleRoot:
    locale: str
    member_name: str
    occurrence: int


def inspect_locale_roots(filepath: str) -> tuple[LocaleRoot, ...]:
    """Return locale-looking top-level object members without loading them.

    The occurrence is retained so a subsequent pass can select the same value
    even when malformed input repeats a top-level member name. JSON object
    semantics are last-value-wins, matching ``json.load``.
    """
    path = Path(filepath)
    latest_by_member: dict[str, LocaleRoot | None] = {}
    member_order: list[str] = []
    occurrences: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = _JsonStreamReader(stream)
        _expect_object_root(reader)
        reader.take()
        reader.skip_whitespace()
        if reader.peek() == "}":
            reader.take()
            reader.ensure_finished()
            return ()

        while True:
            member_name = reader.read_member_name()
            reader.expect(":")
            reader.skip_whitespace()
            locale = normalize_language_header(member_name)
            if locale:
                occurrence = occurrences.get(member_name, 0) + 1
                occurrences[member_name] = occurrence
                if member_name not in latest_by_member:
                    member_order.append(member_name)
                latest_by_member[member_name] = (
                    LocaleRoot(locale, member_name, occurrence) if reader.peek() == "{" else None
                )
            reader.skip_value()
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "}":
                break
            if separator != ",":
                reader.fail("Expected ',' or '}' in top-level JSON object")
            reader.skip_whitespace()
        reader.ensure_finished()

    # Canonical spellings such as en_US and en-US identify the same locale.
    # Retain the last value while preserving the first locale's ordering.
    roots_by_locale: dict[str, LocaleRoot] = {}
    locale_order: list[str] = []
    for member_name in member_order:
        root = latest_by_member[member_name]
        if root is None:
            continue
        if root.locale not in roots_by_locale:
            locale_order.append(root.locale)
        roots_by_locale[root.locale] = root
    return tuple(roots_by_locale[locale] for locale in locale_order)


def iter_string_leaves(filepath: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield string-valued object leaves from a JSON translation file."""
    path = Path(filepath)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = _JsonStreamReader(stream)
        _expect_object_root(reader)
        yield from _iter_object(reader, (), 0)
        reader.ensure_finished()


def iter_selected_root_leaves(
    filepath: str,
    selections: Mapping[tuple[str, int], str],
) -> Iterator[tuple[str, tuple[str, ...], str]]:
    """Yield leaves below selected top-level members in one file pass.

    ``selections`` maps ``(raw member name, occurrence)`` to a caller-owned
    identifier (normally a canonical locale). Unselected values are validated
    and discarded with a fixed-size read buffer.
    """
    path = Path(filepath)
    occurrences: dict[str, int] = {}
    selected_member_names = frozenset(member_name for member_name, _occurrence in selections)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = _JsonStreamReader(stream)
        _expect_object_root(reader)
        reader.take()
        reader.skip_whitespace()
        if reader.peek() == "}":
            reader.take()
            reader.ensure_finished()
            return

        while True:
            member_name = reader.read_member_name()
            occurrence = 0
            if member_name in selected_member_names:
                occurrence = occurrences.get(member_name, 0) + 1
                occurrences[member_name] = occurrence
            reader.expect(":")
            selection = selections.get((member_name, occurrence))
            if selection is None:
                reader.skip_value()
            else:
                reader.skip_whitespace()
                if reader.peek() != "{":
                    reader.fail("Selected locale root is not a JSON object")
                for leaf_path, text in _iter_object(reader, (), 0):
                    yield selection, leaf_path, text
            reader.skip_whitespace()
            separator = reader.take()
            if separator == "}":
                break
            if separator != ",":
                reader.fail("Expected ',' or '}' in top-level JSON object")
            reader.skip_whitespace()
        reader.ensure_finished()


def _iter_object(
    reader: _JsonStreamReader,
    path: tuple[str, ...],
    depth: int,
) -> Iterator[tuple[tuple[str, ...], str]]:
    if depth >= _MAX_JSON_DEPTH:
        reader.fail(f"JSON nesting exceeds the {_MAX_JSON_DEPTH}-level safety limit")
    reader.expect("{")
    reader.skip_whitespace()
    if reader.peek() == "}":
        reader.take()
        return

    while True:
        member_name = reader.read_member_name()
        member_path = (*path, member_name)
        reader.expect(":")
        reader.skip_whitespace()
        marker = reader.peek()
        if marker == "{":
            yield from _iter_object(reader, member_path, depth + 1)
        elif marker == '"':
            value = reader.read_value()
            if not isinstance(value, str):
                reader.fail("Expected a JSON string")
            yield member_path, value
        else:
            # JSON i18n historically ignores arrays and non-string scalars.
            reader.skip_value(depth + 1)
        reader.skip_whitespace()
        separator = reader.take()
        if separator == "}":
            return
        if separator != ",":
            reader.fail("Expected ',' or '}' in JSON object")
        reader.skip_whitespace()


def _expect_object_root(reader: _JsonStreamReader) -> None:
    reader.skip_whitespace()
    if reader.peek() != "{":
        raise TypeError("Expected JSON object at translation root")
