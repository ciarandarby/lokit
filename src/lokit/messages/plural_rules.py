from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from lokit._data.cldr_plural_rules import CARDINAL_RULES, CLDR_VERSION, ORDINAL_RULES
from lokit.compat import StrEnum
from lokit.data.structure import PluralCategory


class PluralType(StrEnum):
    CARDINAL = "cardinal"
    ORDINAL = "ordinal"


@dataclass(frozen=True, slots=True)
class DecimalQuantity:
    value: Decimal
    i: int
    v: int
    w: int
    f: int
    t: int
    c: int = 0
    e: int = 0

    @classmethod
    def from_value(cls, value: int | float | str | Decimal) -> DecimalQuantity:
        source = str(value)
        exponent_match = re.search(r"[eE]([+-]?\d+)$", source)
        scientific_exponent = int(exponent_match.group(1)) if exponent_match is not None else 0
        try:
            decimal_value = abs(Decimal(source))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid plural number {value!r}") from exc
        mantissa = source.split("e", maxsplit=1)[0].split("E", maxsplit=1)[0].lstrip("+-")
        fraction = mantissa.partition(".")[2]
        visible_fraction = fraction
        trimmed_fraction = visible_fraction.rstrip("0")
        return cls(
            value=decimal_value,
            i=int(decimal_value),
            v=len(visible_fraction),
            w=len(trimmed_fraction),
            f=int(visible_fraction) if visible_fraction else 0,
            t=int(trimmed_fraction) if trimmed_fraction else 0,
            e=scientific_exponent,
        )

    def operand(self, name: str) -> Decimal:
        if name == "n":
            return self.value
        if name == "i":
            return Decimal(self.i)
        if name == "v":
            return Decimal(self.v)
        if name == "w":
            return Decimal(self.w)
        if name == "f":
            return Decimal(self.f)
        if name == "t":
            return Decimal(self.t)
        if name == "c":
            return Decimal(self.c)
        if name == "e":
            return Decimal(self.e)
        raise ValueError(f"Unknown CLDR plural operand {name!r}")


@dataclass(frozen=True, slots=True)
class _Range:
    start: Decimal
    end: Decimal

    def contains(self, value: Decimal) -> bool:
        return self.start <= value <= self.end


@dataclass(frozen=True, slots=True)
class _Relation:
    operand: str
    modulus: int | None
    negated: bool
    ranges: tuple[_Range, ...]

    def matches(self, quantity: DecimalQuantity) -> bool:
        value = quantity.operand(self.operand)
        if self.modulus is not None:
            value %= self.modulus
        found = any(item.contains(value) for item in self.ranges)
        return not found if self.negated else found

    def gettext_expression(self) -> str:
        if self.operand in {"n", "i"}:
            value = "n"
            if self.modulus is not None:
                value = f"(n % {self.modulus})"
        else:
            value = "0"
        comparisons: list[str] = []
        for item in self.ranges:
            if item.start == item.end:
                comparisons.append(f"{value} == {_decimal_literal(item.start)}")
            else:
                comparisons.append(
                    f"({value} >= {_decimal_literal(item.start)} && {value} <= {_decimal_literal(item.end)})"
                )
        expression = " || ".join(comparisons)
        return f"!({expression})" if self.negated else f"({expression})"


@dataclass(frozen=True, slots=True)
class _Rule:
    alternatives: tuple[tuple[_Relation, ...], ...]

    def matches(self, quantity: DecimalQuantity) -> bool:
        return any(all(relation.matches(quantity) for relation in conjunction) for conjunction in self.alternatives)

    def gettext_expression(self) -> str:
        alternatives = [
            " && ".join(relation.gettext_expression() for relation in conjunction)
            for conjunction in self.alternatives
        ]
        return "(" + " || ".join(f"({value})" for value in alternatives) + ")"


def plural_category(
    locale: str,
    value: int | float | str | Decimal,
    plural_type: PluralType = PluralType.CARDINAL,
) -> PluralCategory:
    quantity = DecimalQuantity.from_value(value)
    rules = _locale_rules(locale, plural_type)
    for category, rule in rules:
        if rule.matches(quantity):
            return PluralCategory(category)
    return PluralCategory.OTHER


def gettext_plural_forms(locale: str) -> str:
    raw_rules = _raw_locale_rules(locale, PluralType.CARDINAL)
    categories = _gettext_categories(locale, raw_rules)
    expression = str(len(categories) - 1)
    raw_by_category = dict(raw_rules)
    for index in range(len(categories) - 2, -1, -1):
        rule = _parse_rule(raw_by_category[categories[index]])
        expression = f"({rule.gettext_expression()} ? {index} : {expression})"
    return f"nplurals={len(categories)}; plural={expression};"


def gettext_category_indexes(locale: str) -> dict[PluralCategory, int]:
    raw_rules = _raw_locale_rules(locale, PluralType.CARDINAL)
    categories = _gettext_categories(locale, raw_rules)
    return {PluralCategory(category): index for index, category in enumerate(categories)}


def _gettext_categories(locale: str, raw_rules: tuple[tuple[str, str], ...]) -> list[str]:
    sample_values = (*range(0, 2001), 10_000, 100_000, 1_000_000, 2_000_000, 10_000_000)
    reachable = {plural_category(locale, value).value for value in sample_values}
    ordered = [category for category, _ in raw_rules if category in reachable]
    if PluralCategory.OTHER.value in reachable:
        ordered.append(PluralCategory.OTHER.value)
    if not ordered:
        ordered.append(PluralCategory.OTHER.value)
    return ordered


@lru_cache(maxsize=512)
def _locale_rules(locale: str, plural_type: PluralType) -> tuple[tuple[str, _Rule], ...]:
    return tuple((category, _parse_rule(rule)) for category, rule in _raw_locale_rules(locale, plural_type))


def _raw_locale_rules(locale: str, plural_type: PluralType) -> tuple[tuple[str, str], ...]:
    source = CARDINAL_RULES if plural_type == PluralType.CARDINAL else ORDINAL_RULES
    key = locale.replace("_", "-").lower()
    while key:
        raw = source.get(key)
        if raw is not None:
            return raw
        key = key.rpartition("-")[0]
    return ()


@lru_cache(maxsize=1024)
def _parse_rule(expression: str) -> _Rule:
    alternatives: list[tuple[_Relation, ...]] = []
    for alternative in expression.split(" or "):
        alternatives.append(tuple(_parse_relation(value) for value in alternative.split(" and ")))
    return _Rule(tuple(alternatives))


def _parse_relation(expression: str) -> _Relation:
    match = re.fullmatch(r"([nivwftce])(?:\s*%\s*(\d+))?\s*(!=|=)\s*(.+)", expression.strip())
    if match is None:
        raise ValueError(f"Unsupported CLDR {CLDR_VERSION} plural relation: {expression!r}")
    operand, modulus, operator, range_list = match.groups()
    ranges: list[_Range] = []
    for raw_range in range_list.split(","):
        bounds = raw_range.strip().split("..", maxsplit=1)
        start = Decimal(bounds[0])
        end = Decimal(bounds[1]) if len(bounds) == 2 else start
        ranges.append(_Range(start, end))
    return _Relation(
        operand=operand,
        modulus=int(modulus) if modulus is not None else None,
        negated=operator == "!=",
        ranges=tuple(ranges),
    )


def _decimal_literal(value: Decimal) -> str:
    if value == value.to_integral():
        return str(int(value))
    return format(value, "f")


__all__ = [
    "CLDR_VERSION",
    "DecimalQuantity",
    "PluralType",
    "gettext_category_indexes",
    "gettext_plural_forms",
    "plural_category",
]
