"""Typed public access to Lokit's bounded native placeholder engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lokit.compat import StrEnum

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lokit.data.structure import Data, SegmentPart
    from lokit.data.tag_types import TieData


class PlaceholderSyntax(StrEnum):
    """Runtime placeholder grammars recognized by the Rust scanner."""

    C_PRINTF = "c-printf"
    OBJECTIVE_C_PRINTF = "objective-c-printf"
    CXX_PRINTF = "cxx-printf"
    CXX_STD_FORMAT = "cxx-std-format"
    PYTHON_PERCENT = "python-percent"
    PYTHON_BRACE = "python-brace"
    JAVA_MESSAGE_FORMAT = "java-message-format"
    JAVA_FORMATTER = "java-formatter"
    ICU_MESSAGE_FORMAT_1 = "icu-message-format-1"
    UNICODE_MESSAGE_FORMAT_2 = "unicode-message-format-2"
    DOTNET_COMPOSITE = "dotnet-composite"
    JAVASCRIPT_PRINTF = "javascript-printf"
    ECMASCRIPT_TEMPLATE = "ecmascript-template"
    RUST_FORMAT = "rust-format"
    GO_FORMAT = "go-format"
    RUBY_FORMAT = "ruby-format"
    PHP_PRINTF = "php-printf"
    SHELL_PRINTF = "shell-printf"
    AWK_PRINTF = "awk-printf"
    LUA_PRINTF = "lua-printf"
    OBJECT_PASCAL = "object-pascal"
    MODULA2_PRINTF = "modula2-printf"
    D_FORMAT = "d-format"
    OCAML_PRINTF = "ocaml-printf"
    QT_ARG = "qt-arg"
    QT_PLURAL = "qt-plural"
    KDE = "kde"
    KDE_KUIT = "kde-kuit"
    BOOST = "boost"
    TCL_PRINTF = "tcl-printf"
    PERL_PRINTF = "perl-printf"
    PERL_BRACE = "perl-brace"
    SCHEME = "scheme"
    LISP = "lisp"
    ELISP = "elisp"
    LIBREP = "librep"
    SMALLTALK = "smalltalk"
    FLUENT = "fluent"
    MUSTACHE = "mustache"
    HANDLEBARS = "handlebars"
    SHELL_PARAMETER = "shell-parameter"
    SWIFT_INTERPOLATION = "swift-interpolation"
    GCC_INTERNAL = "gcc-internal"
    GFC_INTERNAL = "gfc-internal"
    YCP = "ycp"


class PlaceholderRole(StrEnum):
    VALUE = "value"
    WIDTH = "width"
    PRECISION = "precision"
    SELECTOR = "selector"
    MARKUP = "markup"


class PlaceholderValueType(StrEnum):
    ANY = "any"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    FLOAT = "float"
    CHARACTER = "character"
    DATE = "date"
    TIME = "time"
    POINTER = "pointer"
    COUNT = "count"


@dataclass(frozen=True, slots=True)
class PlaceholderOccurrence:
    syntax: PlaceholderSyntax
    role: PlaceholderRole
    value_type: PlaceholderValueType
    key: str
    start_byte: int
    end_byte: int
    key_start_byte: int | None
    key_end_byte: int | None
    original_text: str


@dataclass(frozen=True, slots=True)
class PlaceholderProjection:
    text: str
    tag_map: dict[str, TieData]
    parts: list[SegmentPart]
    token_prefix: str


@dataclass(frozen=True, slots=True)
class CanonicalPlaceholderText:
    text: str
    signature: str


@dataclass(frozen=True, slots=True)
class ReformedTarget:
    text: str
    changed: bool
    signature: str


def detect(
    text: str,
    syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    gettext_flags: Sequence[str] | None = None,
    *,
    auto_detect: bool = True,
    max_input_bytes: int | None = None,
    max_occurrences: int | None = None,
    max_placeholder_bytes: int | None = None,
    max_nesting: int | None = None,
) -> tuple[PlaceholderOccurrence, ...]:
    """Detect runtime placeholders without evaluating their expressions."""
    from lokit._interchange_rust import detect_placeholders

    raw = detect_placeholders(
        text,
        _syntax_values(syntaxes),
        gettext_flags,
        auto_detect,
        max_input_bytes,
        max_occurrences,
        max_placeholder_bytes,
        max_nesting,
    )
    return tuple(
        PlaceholderOccurrence(
            syntax=PlaceholderSyntax(syntax),
            role=PlaceholderRole(role),
            value_type=PlaceholderValueType(value_type),
            key=key,
            start_byte=start,
            end_byte=end,
            key_start_byte=key_start,
            key_end_byte=key_end,
            original_text=original_text,
        )
        for syntax, role, value_type, key, start, end, key_start, key_end, original_text in raw
    )


def project(
    text: str,
    syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    gettext_flags: Sequence[str] | None = None,
    *,
    auto_detect: bool = True,
) -> PlaceholderProjection:
    """Replace runtime placeholders with ordered collision-safe Lokit tokens."""
    from lokit._interchange_rust import project_placeholders

    projected, tag_map, parts, token_prefix = project_placeholders(
        text,
        _syntax_values(syntaxes),
        gettext_flags,
        auto_detect,
    )
    return PlaceholderProjection(projected, tag_map, parts, token_prefix)


def project_data(
    data: Data,
    *,
    runtime_placeholders: bool = True,
    inline_placeholders: bool = True,
    project_targets: bool = True,
    syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    gettext_flags: Sequence[str] | None = None,
    auto_detect: bool = True,
) -> Data:
    """Project all source/target placeholder graphs in Rust."""
    from lokit._interchange_rust import project_data_placeholders

    return project_data_placeholders(
        data,
        runtime_placeholders,
        inline_placeholders,
        project_targets,
        _syntax_values(syntaxes),
        gettext_flags,
        auto_detect,
    )


def resolve_data(data: Data) -> Data:
    """Restore exact runtime placeholders and native inline-code positions."""
    from lokit._interchange_rust import resolve_data_placeholders

    return resolve_data_placeholders(data)


def literalize_data(data: Data) -> Data:
    """Keep generic projection tokens as literal export text."""
    from lokit._interchange_rust import literalize_data_placeholders

    return literalize_data_placeholders(data)


def canonicalize(
    text: str,
    syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    gettext_flags: Sequence[str] | None = None,
    *,
    auto_detect: bool = True,
) -> CanonicalPlaceholderText:
    """Return placeholder-name-independent match text and its signature."""
    from lokit._interchange_rust import canonicalize_placeholders

    canonical, signature = canonicalize_placeholders(
        text,
        _syntax_values(syntaxes),
        gettext_flags,
        auto_detect,
    )
    return CanonicalPlaceholderText(canonical, signature)


def reform(
    candidate_source: str,
    candidate_target: str,
    query_source: str,
    syntaxes: Sequence[PlaceholderSyntax | str] | None = None,
    gettext_flags: Sequence[str] | None = None,
    *,
    auto_detect: bool = True,
) -> ReformedTarget:
    """Reform a matched target's placeholders to the query source names."""
    from lokit._interchange_rust import reform_placeholders

    text, changed, signature = reform_placeholders(
        candidate_source,
        candidate_target,
        query_source,
        _syntax_values(syntaxes),
        gettext_flags,
        auto_detect,
    )
    return ReformedTarget(text, changed, signature)


def _syntax_values(
    syntaxes: Sequence[PlaceholderSyntax | str] | None,
) -> tuple[str, ...] | None:
    if syntaxes is None:
        return None
    return tuple(value.value if isinstance(value, PlaceholderSyntax) else value for value in syntaxes)


__all__ = [
    "CanonicalPlaceholderText",
    "PlaceholderOccurrence",
    "PlaceholderProjection",
    "PlaceholderRole",
    "PlaceholderSyntax",
    "PlaceholderValueType",
    "ReformedTarget",
    "canonicalize",
    "detect",
    "literalize_data",
    "project",
    "project_data",
    "reform",
    "resolve_data",
]
