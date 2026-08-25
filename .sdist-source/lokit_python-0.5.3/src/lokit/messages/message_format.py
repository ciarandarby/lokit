from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, TypeAlias

from lokit.compat import StrEnum
from lokit.messages.plural_rules import PluralType, plural_category

if TYPE_CHECKING:
    from collections.abc import Mapping


class SelectorKind(StrEnum):
    SELECT = "select"
    PLURAL = "plural"
    SELECT_ORDINAL = "selectordinal"


_SELECTOR_VALUES: frozenset[str] = frozenset(item.value for item in SelectorKind)


@dataclass(frozen=True, slots=True)
class TextNode:
    value: str


@dataclass(frozen=True, slots=True)
class ArgumentNode:
    name: str
    format_type: str | None = None
    style: str | None = None


@dataclass(frozen=True, slots=True)
class PoundNode:
    pass


@dataclass(frozen=True, slots=True)
class MessageOption:
    selector: str
    message: Message


@dataclass(frozen=True, slots=True)
class SelectNode:
    name: str
    kind: SelectorKind
    offset: Decimal
    options: tuple[MessageOption, ...]


MessageNode: TypeAlias = TextNode | ArgumentNode | PoundNode | SelectNode
Message: TypeAlias = tuple[MessageNode, ...]
MessageValue: TypeAlias = str | int | float | Decimal | bool


class MessageFormatError(ValueError):
    pass


class MessageParser:
    def __init__(self, pattern: str, *, maximum_nesting: int = 64, maximum_length: int = 1_000_000) -> None:
        if len(pattern) > maximum_length:
            raise MessageFormatError(f"Message pattern exceeds {maximum_length} characters")
        if maximum_nesting < 1:
            raise ValueError("maximum_nesting must be at least 1")
        self._pattern = pattern
        self._position = 0
        self._maximum_nesting = maximum_nesting

    def parse(self) -> Message:
        message = self._parse_message(0, False, None)
        if self._position != len(self._pattern):
            raise MessageFormatError(f"Unexpected trailing input at character {self._position}")
        return message

    def _parse_message(self, depth: int, in_plural: bool, terminator: str | None) -> Message:
        if depth > self._maximum_nesting:
            raise MessageFormatError(f"Message nesting exceeds {self._maximum_nesting}")
        nodes: list[MessageNode] = []
        text: list[str] = []
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            if terminator is not None and char == terminator:
                break
            if char == "{":
                self._flush_text(nodes, text)
                nodes.append(self._parse_argument(depth + 1))
                continue
            if char == "}" and terminator is None:
                raise MessageFormatError(f"Unmatched closing brace at character {self._position}")
            if char == "#" and in_plural:
                self._flush_text(nodes, text)
                nodes.append(PoundNode())
                self._position += 1
                continue
            if char == "'":
                text.append(self._parse_apostrophe(in_plural))
                continue
            text.append(char)
            self._position += 1
        self._flush_text(nodes, text)
        return tuple(nodes)

    def _parse_argument(self, depth: int) -> MessageNode:
        self._expect("{")
        name = self._read_until({",", "}"}).strip()
        if not name:
            raise MessageFormatError(f"Empty argument name at character {self._position}")
        if self._peek() == "}":
            self._position += 1
            return ArgumentNode(name)
        self._expect(",")
        format_type = self._read_until({",", "}"}).strip()
        if self._peek() == "}":
            self._position += 1
            return ArgumentNode(name, format_type=format_type)
        self._expect(",")
        if format_type not in _SELECTOR_VALUES:
            style = self._read_balanced_style()
            return ArgumentNode(name, format_type=format_type, style=style.strip())
        return self._parse_select(name, SelectorKind(format_type), depth)

    def _parse_select(self, name: str, kind: SelectorKind, depth: int) -> SelectNode:
        self._skip_space()
        offset = Decimal(0)
        if kind != SelectorKind.SELECT and self._pattern.startswith("offset:", self._position):
            self._position += len("offset:")
            self._skip_space()
            offset_text = self._read_until_space_or("{")
            try:
                offset = Decimal(offset_text)
            except Exception as exc:
                raise MessageFormatError(f"Invalid plural offset {offset_text!r}") from exc
        options: list[MessageOption] = []
        while True:
            self._skip_space()
            if self._peek() == "}":
                self._position += 1
                break
            selector = self._read_until_space_or("{")
            self._skip_space()
            self._expect("{")
            message = self._parse_message(depth, kind != SelectorKind.SELECT, "}")
            self._expect("}")
            options.append(MessageOption(selector, message))
        if not any(option.selector == "other" for option in options):
            raise MessageFormatError(f"{kind.value} argument {name!r} requires an 'other' option")
        return SelectNode(name=name, kind=kind, offset=offset, options=tuple(options))

    def _parse_apostrophe(self, in_plural: bool) -> str:
        self._position += 1
        if self._peek() == "'":
            self._position += 1
            return "'"
        syntax = {"{", "}"}
        if in_plural:
            syntax.add("#")
        if self._peek() not in syntax:
            return "'"
        quoted: list[str] = []
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            self._position += 1
            if char != "'":
                quoted.append(char)
                continue
            if self._peek() == "'":
                quoted.append("'")
                self._position += 1
                continue
            return "".join(quoted)
        return "".join(quoted)

    def _read_balanced_style(self) -> str:
        start = self._position
        nested = 0
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            if char == "{":
                nested += 1
            elif char == "}":
                if nested == 0:
                    result = self._pattern[start : self._position]
                    self._position += 1
                    return result
                nested -= 1
            self._position += 1
        raise MessageFormatError("Unterminated argument style")

    def _read_until(self, delimiters: set[str]) -> str:
        start = self._position
        while self._position < len(self._pattern) and self._pattern[self._position] not in delimiters:
            self._position += 1
        if self._position == len(self._pattern):
            raise MessageFormatError("Unterminated argument")
        return self._pattern[start : self._position]

    def _read_until_space_or(self, delimiter: str) -> str:
        start = self._position
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            if char.isspace() or char == delimiter:
                break
            self._position += 1
        if start == self._position:
            raise MessageFormatError(f"Expected token at character {self._position}")
        return self._pattern[start : self._position]

    def _expect(self, value: str) -> None:
        if self._peek() != value:
            raise MessageFormatError(f"Expected {value!r} at character {self._position}")
        self._position += 1

    def _peek(self) -> str | None:
        if self._position >= len(self._pattern):
            return None
        return self._pattern[self._position]

    def _skip_space(self) -> None:
        while self._position < len(self._pattern) and self._pattern[self._position].isspace():
            self._position += 1

    def _flush_text(self, nodes: list[MessageNode], text: list[str]) -> None:
        if text:
            nodes.append(TextNode("".join(text)))
            text.clear()


def parse_message(pattern: str, *, maximum_nesting: int = 64, maximum_length: int = 1_000_000) -> Message:
    return MessageParser(pattern, maximum_nesting=maximum_nesting, maximum_length=maximum_length).parse()


def format_message(message: Message | str, arguments: Mapping[str, MessageValue], *, locale: str) -> str:
    parsed = _parse_default_message(message) if isinstance(message, str) else message
    return _format_nodes(parsed, arguments, locale, None)


@lru_cache(maxsize=1024)
def _parse_default_message(pattern: str) -> Message:
    return MessageParser(pattern).parse()


def _format_nodes(
    message: Message,
    arguments: Mapping[str, MessageValue],
    locale: str,
    pound_value: Decimal | None,
) -> str:
    output: list[str] = []
    for node in message:
        if isinstance(node, TextNode):
            output.append(node.value)
        elif isinstance(node, ArgumentNode):
            output.append(_format_value(_argument(arguments, node.name)))
        elif isinstance(node, PoundNode):
            output.append("#" if pound_value is None else _format_decimal(pound_value))
        else:
            output.append(_format_select(node, arguments, locale))
    return "".join(output)


def _format_select(node: SelectNode, arguments: Mapping[str, MessageValue], locale: str) -> str:
    value = _argument(arguments, node.name)
    option_map = {option.selector: option.message for option in node.options}
    if node.kind == SelectorKind.SELECT:
        selected = option_map.get(str(value), option_map["other"])
        return _format_nodes(selected, arguments, locale, None)
    number = _number(value, node.name)
    exact = option_map.get(f"={_format_decimal(number)}")
    adjusted = number - node.offset
    if exact is None:
        plural_type = PluralType.ORDINAL if node.kind == SelectorKind.SELECT_ORDINAL else PluralType.CARDINAL
        category = plural_category(locale, adjusted, plural_type).value
        exact = option_map.get(category, option_map["other"])
    return _format_nodes(exact, arguments, locale, adjusted)


def _argument(arguments: Mapping[str, MessageValue], name: str) -> MessageValue:
    try:
        return arguments[name]
    except KeyError as exc:
        raise MessageFormatError(f"Missing message argument {name!r}") from exc


def _number(value: MessageValue, name: str) -> Decimal:
    if isinstance(value, bool):
        raise MessageFormatError(f"Plural argument {name!r} must be numeric")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception as exc:
            raise MessageFormatError(f"Plural argument {name!r} must be numeric") from exc
    raise MessageFormatError(f"Plural argument {name!r} must be numeric")


def _format_value(value: MessageValue) -> str:
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral():
        return str(int(value))
    return format(value, "f")


__all__ = [
    "ArgumentNode",
    "Message",
    "MessageFormatError",
    "MessageNode",
    "MessageOption",
    "MessageParser",
    "MessageValue",
    "PoundNode",
    "SelectNode",
    "SelectorKind",
    "TextNode",
    "format_message",
    "parse_message",
]
