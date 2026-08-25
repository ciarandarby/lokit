from __future__ import annotations

from decimal import Decimal

import pytest

from lokit._data.cldr_plural_rules import CARDINAL_RULES, ORDINAL_RULES
from lokit.data.structure import PluralCategory
from lokit.messages import (
    GettextPluralError,
    MessageFormatError,
    PluralType,
    format_message,
    gettext_plural_forms,
    parse_gettext_plural_forms,
    parse_message,
    plural_category,
)


@pytest.mark.parametrize(
    ("locale", "value", "expected"),
    [
        ("en", 1, PluralCategory.ONE),
        ("en", "1.0", PluralCategory.OTHER),
        ("fr", 0, PluralCategory.ONE),
        ("ja", 1, PluralCategory.OTHER),
        ("ar", 0, PluralCategory.ZERO),
        ("ar", 1, PluralCategory.ONE),
        ("ar", 2, PluralCategory.TWO),
        ("ar", 3, PluralCategory.FEW),
        ("ar", 11, PluralCategory.MANY),
        ("ar", 100, PluralCategory.OTHER),
        ("ru", 1, PluralCategory.ONE),
        ("ru", 2, PluralCategory.FEW),
        ("ru", 5, PluralCategory.MANY),
        ("ru", Decimal("1.5"), PluralCategory.OTHER),
        ("sl", 1, PluralCategory.ONE),
        ("sl", 2, PluralCategory.TWO),
        ("sl", 3, PluralCategory.FEW),
    ],
)
def test_cldr_cardinal_categories(
    locale: str,
    value: int | str | Decimal,
    expected: PluralCategory,
) -> None:
    assert plural_category(locale, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, PluralCategory.ONE),
        (2, PluralCategory.TWO),
        (3, PluralCategory.FEW),
        (4, PluralCategory.OTHER),
        (11, PluralCategory.OTHER),
        (21, PluralCategory.ONE),
    ],
)
def test_cldr_english_ordinals(value: int, expected: PluralCategory) -> None:
    assert plural_category("en-US", value, PluralType.ORDINAL) == expected


def test_every_generated_cldr_rule_is_executable() -> None:
    for plural_type, rules in (
        (PluralType.CARDINAL, CARDINAL_RULES),
        (PluralType.ORDINAL, ORDINAL_RULES),
    ):
        for locale in rules:
            for value in (0, 1, 2, 3, 5, 11, 21, "1.0", "1.2", "1000000"):
                assert isinstance(plural_category(locale, value, plural_type), PluralCategory)


def test_message_format_plural_exact_offset_and_pound() -> None:
    pattern = (
        "{count, plural, offset:1 "
        "=0 {Nobody came.} "
        "=1 {{name} came alone.} "
        "one {{name} and one guest came.} "
        "other {{name} and # guests came.}}"
    )

    assert format_message(pattern, {"count": 0, "name": "Ada"}, locale="en") == "Nobody came."
    assert format_message(pattern, {"count": 1, "name": "Ada"}, locale="en") == "Ada came alone."
    assert format_message(pattern, {"count": 2, "name": "Ada"}, locale="en") == "Ada and one guest came."
    assert format_message(pattern, {"count": 4, "name": "Ada"}, locale="en") == "Ada and 3 guests came."


def test_message_format_nested_select_and_ordinal() -> None:
    pattern = (
        "{gender, select, female {{place, selectordinal, one {Her #st} two {Her #nd} few {Her #rd} other {Her #th}}} "
        "other {{place, selectordinal, one {Their #st} two {Their #nd} few {Their #rd} other {Their #th}}}}"
    )

    assert format_message(pattern, {"gender": "female", "place": 22}, locale="en") == "Her 22nd"
    assert format_message(pattern, {"gender": "unknown", "place": 23}, locale="en") == "Their 23rd"


def test_message_format_icu_apostrophe_rules() -> None:
    pattern = "This '{is}' a {thing}; don''t replace '#'."

    assert format_message(pattern, {"thing": "test"}, locale="en") == "This {is} a test; don't replace '#'."


def test_message_format_validation_and_limits() -> None:
    with pytest.raises(MessageFormatError, match="requires an 'other'"):
        parse_message("{count, plural, one {One}}")
    with pytest.raises(MessageFormatError, match="exceeds"):
        parse_message("abc", maximum_length=2)
    with pytest.raises(MessageFormatError, match="Missing message argument"):
        format_message("Hello {name}", {}, locale="en")


@pytest.mark.parametrize(
    ("header", "values"),
    [
        ("nplurals=2; plural=(n != 1);", {0: 1, 1: 0, 2: 1}),
        (
            "nplurals=3; plural=((n % 10 == 1 && n % 100 != 11) ? 0 : "
            "((n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) ? 1 : 2));",
            {1: 0, 2: 1, 5: 2, 11: 2, 22: 1},
        ),
        (
            "nplurals=6; plural=((n == 0) ? 0 : ((n == 1) ? 1 : ((n == 2) ? 2 : "
            "((n % 100 >= 3 && n % 100 <= 10) ? 3 : ((n % 100 >= 11 && n % 100 <= 99) ? 4 : 5)))));",
            {0: 0, 1: 1, 2: 2, 3: 3, 11: 4, 100: 5},
        ),
    ],
)
def test_gettext_plural_formula_is_evaluated_without_eval(header: str, values: dict[int, int]) -> None:
    rule = parse_gettext_plural_forms(header)

    assert {value: rule.index(value) for value in values} == values


def test_gettext_plural_formula_rejects_code_and_out_of_range_results() -> None:
    with pytest.raises(GettextPluralError, match="Unsupported token"):
        parse_gettext_plural_forms("nplurals=2; plural=__import__('os');")
    rule = parse_gettext_plural_forms("nplurals=2; plural=3;")
    with pytest.raises(GettextPluralError, match="outside"):
        rule.index(1)


def test_generated_gettext_rules_match_cldr_integer_categories() -> None:
    for locale in CARDINAL_RULES:
        header = gettext_plural_forms(locale)
        rule = parse_gettext_plural_forms(header)
        category_indexes: dict[PluralCategory, int] = {}
        for value in range(0, 201):
            category = plural_category(locale, value)
            index = rule.index(value)
            previous = category_indexes.setdefault(category, index)
            assert previous == index
