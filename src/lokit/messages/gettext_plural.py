from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeAlias


class GettextPluralError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Number:
    value: int


@dataclass(frozen=True, slots=True)
class _Variable:
    pass


@dataclass(frozen=True, slots=True)
class _Unary:
    operator: str
    operand: _Expression


@dataclass(frozen=True, slots=True)
class _Binary:
    operator: str
    left: _Expression
    right: _Expression


@dataclass(frozen=True, slots=True)
class _Conditional:
    condition: _Expression
    when_true: _Expression
    when_false: _Expression


_Expression: TypeAlias = _Number | _Variable | _Unary | _Binary | _Conditional

_TOKEN = re.compile(r"\s*(\d+|n|\|\||&&|==|!=|<=|>=|[?:()!%*/+\-<>])")
_PRECEDENCE: dict[str, int] = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
}


@dataclass(frozen=True, slots=True)
class GettextPluralRule:
    nplurals: int
    expression: str
    _root: _Expression

    def index(self, value: int) -> int:
        if value < 0:
            value = abs(value)
        result = _evaluate(self._root, value)
        if result < 0 or result >= self.nplurals:
            raise GettextPluralError(
                f"Plural expression returned {result}, outside 0..{self.nplurals - 1} for n={value}"
            )
        return result


class _Parser:
    def __init__(self, expression: str, maximum_tokens: int) -> None:
        self._tokens = _tokenize(expression, maximum_tokens)
        self._position = 0

    def parse(self) -> _Expression:
        expression = self._conditional()
        if self._peek() is not None:
            raise GettextPluralError(f"Unexpected token {self._peek()!r}")
        return expression

    def _conditional(self) -> _Expression:
        condition = self._binary(1)
        if self._peek() != "?":
            return condition
        self._position += 1
        when_true = self._conditional()
        self._expect(":")
        when_false = self._conditional()
        return _Conditional(condition, when_true, when_false)

    def _binary(self, minimum_precedence: int) -> _Expression:
        left = self._unary()
        while True:
            operator = self._peek()
            precedence = _PRECEDENCE.get(operator or "")
            if precedence is None or precedence < minimum_precedence:
                return left
            self._position += 1
            right = self._binary(precedence + 1)
            left = _Binary(operator or "", left, right)

    def _unary(self) -> _Expression:
        token = self._peek()
        if token in {"!", "+", "-"}:
            self._position += 1
            return _Unary(token or "", self._unary())
        return self._primary()

    def _primary(self) -> _Expression:
        token = self._peek()
        if token is None:
            raise GettextPluralError("Unexpected end of plural expression")
        self._position += 1
        if token == "n":
            return _Variable()
        if token.isdigit():
            return _Number(int(token))
        if token == "(":
            expression = self._conditional()
            self._expect(")")
            return expression
        raise GettextPluralError(f"Expected number, n, or parenthesis; got {token!r}")

    def _expect(self, expected: str) -> None:
        if self._peek() != expected:
            raise GettextPluralError(f"Expected {expected!r}, got {self._peek()!r}")
        self._position += 1

    def _peek(self) -> str | None:
        if self._position >= len(self._tokens):
            return None
        return self._tokens[self._position]


def parse_gettext_plural_forms(header: str, *, maximum_tokens: int = 512) -> GettextPluralRule:
    return _parse_gettext_plural_forms_cached(header, maximum_tokens)


@lru_cache(maxsize=256)
def _parse_gettext_plural_forms_cached(header: str, maximum_tokens: int) -> GettextPluralRule:
    nplurals_match = re.search(r"(?:^|;)\s*nplurals\s*=\s*(\d+)\s*;", header)
    expression_match = re.search(r"(?:^|;)\s*plural\s*=\s*(.+?)\s*;\s*(?:$|\n)", header)
    if nplurals_match is None or expression_match is None:
        raise GettextPluralError("Plural-Forms must contain nplurals and a terminated plural expression")
    nplurals = int(nplurals_match.group(1))
    if nplurals < 1 or nplurals > 32:
        raise GettextPluralError("nplurals must be between 1 and 32")
    expression = expression_match.group(1).strip()
    root = _Parser(expression, maximum_tokens).parse()
    return GettextPluralRule(nplurals=nplurals, expression=expression, _root=root)


def _tokenize(expression: str, maximum_tokens: int) -> tuple[str, ...]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            raise GettextPluralError(f"Unsupported token at character {position}")
        tokens.append(match.group(1))
        if len(tokens) > maximum_tokens:
            raise GettextPluralError(f"Plural expression exceeds {maximum_tokens} tokens")
        position = match.end()
    return tuple(tokens)


def _evaluate(expression: _Expression, n: int) -> int:
    if isinstance(expression, _Number):
        return expression.value
    if isinstance(expression, _Variable):
        return n
    if isinstance(expression, _Unary):
        value = _evaluate(expression.operand, n)
        if expression.operator == "!":
            return int(not value)
        if expression.operator == "-":
            return -value
        return value
    if isinstance(expression, _Conditional):
        branch = expression.when_true if _evaluate(expression.condition, n) else expression.when_false
        return _evaluate(branch, n)
    left = _evaluate(expression.left, n)
    if expression.operator == "&&" and not left:
        return 0
    if expression.operator == "||" and left:
        return 1
    right = _evaluate(expression.right, n)
    if expression.operator == "||":
        return int(bool(right))
    if expression.operator == "&&":
        return int(bool(right))
    if expression.operator == "==":
        return int(left == right)
    if expression.operator == "!=":
        return int(left != right)
    if expression.operator == "<":
        return int(left < right)
    if expression.operator == "<=":
        return int(left <= right)
    if expression.operator == ">":
        return int(left > right)
    if expression.operator == ">=":
        return int(left >= right)
    if expression.operator == "+":
        return left + right
    if expression.operator == "-":
        return left - right
    if expression.operator == "*":
        return left * right
    if expression.operator == "/":
        if right == 0:
            raise GettextPluralError("Division by zero in plural expression")
        return left // right
    if expression.operator == "%":
        if right == 0:
            raise GettextPluralError("Modulo by zero in plural expression")
        return left % right
    raise GettextPluralError(f"Unsupported operator {expression.operator!r}")


__all__ = ["GettextPluralError", "GettextPluralRule", "parse_gettext_plural_forms"]
