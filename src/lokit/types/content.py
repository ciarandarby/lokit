from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING

from lokit.compat import StrEnum
from lokit.data.structure import CodePart, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


class TagSyntax(StrEnum):
    """Supported inline-code input and output syntaxes."""

    NATIVE = "native"
    HTML = "html"
    XLIFF_12 = "xliff-1.2"
    XLIFF_20 = "xliff-2.0"
    XLIFF_21 = "xliff-2.1"
    TMX_14 = "tmx-1.4"
    IDML = "idml"
    DOCX = "docx"
    PPTX = "pptx"


class InlineCodeKind(StrEnum):
    """Structural role of an inline code."""

    OPEN = "open"
    CLOSE = "close"
    STANDALONE = "standalone"
    ISOLATED_OPEN = "isolated-open"
    ISOLATED_CLOSE = "isolated-close"
    ANNOTATION_OPEN = "annotation-open"
    ANNOTATION_CLOSE = "annotation-close"


class InlineSemantic(StrEnum):
    """Portable meaning used when rendering into another tag syntax."""

    GENERIC = "generic"
    EMPHASIS = "emphasis"
    STRONG = "strong"
    LINK = "link"
    LINE_BREAK = "line-break"
    IMAGE = "image"
    VARIABLE = "variable"
    ANNOTATION = "annotation"


class UnsupportedTagPolicy(StrEnum):
    """Behavior when a destination cannot represent a native inline code."""

    ERROR = "error"
    PLACEHOLDER = "placeholder"
    DROP = "drop"


class ConversionOutcome(StrEnum):
    EXACT = "exact"
    PLACEHOLDER = "placeholder"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class TagAttribute:
    namespace: str | None
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class NativeCode:
    syntax: TagSyntax
    name: str
    namespace: str | None = None
    attributes: tuple[TagAttribute, ...] = ()
    payload: str | None = None
    equivalent_text: str | None = None


@dataclass(frozen=True, slots=True)
class InlineCode:
    id: str
    kind: InlineCodeKind
    semantic: InlineSemantic
    pair_id: str | None
    alignment_id: str | None
    text_offset: int
    order: int
    native: NativeCode


@dataclass(frozen=True, slots=True)
class ConversionDiagnostic:
    code_id: str
    source_syntax: TagSyntax
    destination_syntax: TagSyntax
    outcome: ConversionOutcome
    message: str


@dataclass(frozen=True, slots=True)
class ConversionReport:
    diagnostics: tuple[ConversionDiagnostic, ...] = ()

    @property
    def is_exact(self) -> bool:
        return not self.diagnostics


class TagIntegrityError(ValueError):
    """Raised when inline-code references are inconsistent or conversion is unsafe."""


@dataclass(slots=True)
class Segment:
    """Canonical text and inline-code sequence.

    Text is derived from ``parts``. It is deliberately not stored a second time.
    """

    parts: list[SegmentPart] = field(default_factory=list)
    codes: dict[str, InlineCode] = field(default_factory=dict)

    @property
    def plain_text(self) -> str:
        chunks: list[str] = []
        for part in self.parts:
            if isinstance(part, TextPart):
                chunks.append(part.value)
            else:
                code = self.codes.get(part.ref)
                if code is not None and code.native.equivalent_text is not None:
                    chunks.append(code.native.equivalent_text)
        return "".join(chunks)

    def validate(self) -> None:
        referenced: set[str] = set()
        pair_kinds: dict[str, set[InlineCodeKind]] = {}
        for part in self.parts:
            if not isinstance(part, CodePart):
                continue
            if part.ref in referenced:
                raise TagIntegrityError(f"Inline code {part.ref!r} is referenced more than once")
            code = self.codes.get(part.ref)
            if code is None:
                raise TagIntegrityError(f"Inline code reference {part.ref!r} is dangling")
            referenced.add(part.ref)
            if code.pair_id is not None:
                pair_kinds.setdefault(code.pair_id, set()).add(code.kind)
        unreferenced = self.codes.keys() - referenced
        if unreferenced:
            names = ", ".join(sorted(unreferenced))
            raise TagIntegrityError(f"Inline codes are not referenced by parts: {names}")
        for pair_id, kinds in pair_kinds.items():
            if kinds in ({InlineCodeKind.OPEN}, {InlineCodeKind.CLOSE}):
                raise TagIntegrityError(f"Inline pair {pair_id!r} is incomplete")


def segment_from_legacy(
    text: str,
    parts: Sequence[SegmentPart],
    tag_map: Mapping[str, TieData],
    *,
    syntax: TagSyntax,
) -> Segment:
    """Adapt the legacy parts/map representation without retaining stale text."""

    if not parts:
        return Segment(parts=[TextPart(text)] if text else [], codes={})
    if not legacy_parts_match_text(text, parts):
        return Segment(parts=[TextPart(text)] if text else [], codes={})
    codes = {code_id: _inline_code(code, syntax) for code_id, code in tag_map.items()}
    segment = Segment(parts=list(parts), codes=codes)
    segment.validate()
    return segment


def legacy_parts_match_text(text: str, parts: Sequence[SegmentPart]) -> bool:
    """Return whether legacy parts are a current projection of ``text``."""

    return "".join(part.value for part in parts if isinstance(part, TextPart)) == text


def iter_rendered(
    segment: Segment,
    syntax: TagSyntax,
    *,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> Iterator[str]:
    """Render a segment incrementally into a supported inline syntax."""

    segment.validate()
    destination = native_syntax if syntax == TagSyntax.NATIVE else syntax
    for part in segment.parts:
        if isinstance(part, TextPart):
            yield _escape_text(part.value, destination)
            continue
        code = segment.codes[part.ref]
        yield _render_code(code, destination, unsupported_tags)


def render_segment(
    segment: Segment,
    syntax: TagSyntax,
    *,
    native_syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy = UnsupportedTagPolicy.ERROR,
) -> str:
    return "".join(
        iter_rendered(
            segment,
            syntax,
            native_syntax=native_syntax,
            unsupported_tags=unsupported_tags,
        )
    )


def _inline_code(code: TieData, syntax: TagSyntax) -> InlineCode:
    return InlineCode(
        id=code.id,
        kind=_kind(code.type),
        semantic=_semantic(code.type),
        pair_id=code.pair_id,
        alignment_id=code.attributes.get("xid") or code.attributes.get("i"),
        text_offset=code.position,
        order=code.order,
        native=NativeCode(
            syntax=syntax,
            name=code.original_name or _default_name(code.type),
            attributes=tuple(TagAttribute(None, name, value) for name, value in code.attributes.items()),
            payload=code.original_text,
            equivalent_text=code.attributes.get("equiv-text"),
        ),
    )


def _kind(tie_type: TieType) -> InlineCodeKind:
    if tie_type.value.endswith(".open"):
        return InlineCodeKind.OPEN
    if tie_type.value.endswith(".close"):
        return InlineCodeKind.CLOSE
    return InlineCodeKind.STANDALONE


def _semantic(tie_type: TieType) -> InlineSemantic:
    value = tie_type.value
    if value.startswith(("b.", "strong.")):
        return InlineSemantic.STRONG
    if value.startswith(("i.", "em.")):
        return InlineSemantic.EMPHASIS
    if value.startswith("a."):
        return InlineSemantic.LINK
    if tie_type == TieType.BR:
        return InlineSemantic.LINE_BREAK
    if tie_type == TieType.IMG:
        return InlineSemantic.IMAGE
    if value.startswith("var."):
        return InlineSemantic.VARIABLE
    return InlineSemantic.GENERIC


def _default_name(tie_type: TieType) -> str:
    value = tie_type.value
    return value.split(".", maxsplit=1)[0] if "." in value else "ph"


def _escape_text(value: str, syntax: TagSyntax) -> str:
    if syntax in {TagSyntax.HTML, TagSyntax.TMX_14, TagSyntax.XLIFF_12, TagSyntax.XLIFF_20, TagSyntax.XLIFF_21}:
        return escape(value, quote=False)
    return value


def _render_code(
    code: InlineCode,
    syntax: TagSyntax,
    unsupported_tags: UnsupportedTagPolicy,
) -> str:
    if syntax == TagSyntax.HTML:
        return _render_html(code, unsupported_tags)
    if syntax == TagSyntax.TMX_14:
        return _render_tmx(code)
    if syntax == TagSyntax.XLIFF_12:
        return _render_xliff_12(code)
    if syntax in {TagSyntax.XLIFF_20, TagSyntax.XLIFF_21}:
        return _render_xliff_2(code)
    if syntax == TagSyntax.IDML:
        return _render_idml(code)
    if syntax == code.native.syntax and code.native.payload is not None:
        return code.native.payload
    return _unsupported(code, syntax, unsupported_tags)


def _render_html(code: InlineCode, policy: UnsupportedTagPolicy) -> str:
    name = _safe_html_name(code)
    if not name:
        return _unsupported(code, TagSyntax.HTML, policy)
    if code.kind == InlineCodeKind.CLOSE:
        return f"</{name}>"
    attributes = _html_attributes(code.native.attributes) if code.native.syntax == TagSyntax.HTML else ""
    if code.kind == InlineCodeKind.STANDALONE:
        return f"<{name}{attributes}>"
    return f"<{name}{attributes}>"


def _safe_html_name(code: InlineCode) -> str:
    if code.semantic == InlineSemantic.STRONG:
        return "strong"
    if code.semantic == InlineSemantic.EMPHASIS:
        return "em"
    if code.semantic == InlineSemantic.LINK:
        return "a"
    if code.semantic == InlineSemantic.LINE_BREAK:
        return "br"
    if code.semantic == InlineSemantic.IMAGE:
        return "img"
    if code.semantic == InlineSemantic.VARIABLE:
        return "var"
    name = code.native.name.lower()
    if name in {"bpt", "ept", "ph", "it", "ut", "x", "bx", "ex", "sc", "ec"}:
        payload_match = re.fullmatch(r"\s*</?([A-Za-z][A-Za-z0-9:-]*)[^>]*>\s*", code.native.payload or "")
        if payload_match is not None:
            name = payload_match.group(1).lower()
    allowed = {
        "abbr",
        "b",
        "bdi",
        "bdo",
        "cite",
        "code",
        "data",
        "dfn",
        "i",
        "kbd",
        "mark",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "small",
        "span",
        "sub",
        "sup",
        "time",
        "u",
        "wbr",
    }
    return name if name in allowed else ""


def _html_attributes(attributes: Sequence[TagAttribute]) -> str:
    rendered: list[str] = []
    for attribute in attributes:
        name = attribute.name.lower()
        if name.startswith("on") or name == "style" or name.startswith("xmlns"):
            continue
        if name in {"href", "src"} and _unsafe_url(attribute.value):
            continue
        rendered.append(f' {escape(name, quote=True)}="{escape(attribute.value, quote=True)}"')
    return "".join(rendered)


def _unsafe_url(value: str) -> bool:
    normalized = "".join(value.lower().split())
    return normalized.startswith(("javascript:", "vbscript:", "data:text/html"))


def _render_tmx(code: InlineCode) -> str:
    attributes = _xml_attributes(code)
    if code.kind == InlineCodeKind.OPEN:
        return f"<bpt{attributes}>{escape(code.native.payload or '', quote=False)}</bpt>"
    if code.kind == InlineCodeKind.CLOSE:
        return f"<ept{attributes}>{escape(code.native.payload or '', quote=False)}</ept>"
    return f"<ph{attributes}>{escape(code.native.payload or '', quote=False)}</ph>"


def _render_xliff_12(code: InlineCode) -> str:
    attributes = _xml_attributes(code)
    if code.kind == InlineCodeKind.OPEN:
        return f"<bpt{attributes}>{escape(code.native.payload or '', quote=False)}</bpt>"
    if code.kind == InlineCodeKind.CLOSE:
        return f"<ept{attributes}>{escape(code.native.payload or '', quote=False)}</ept>"
    return f"<ph{attributes}>{escape(code.native.payload or '', quote=False)}</ph>"


def _render_xliff_2(code: InlineCode) -> str:
    attributes = _xml_attributes(code)
    if code.kind == InlineCodeKind.OPEN:
        return f"<sc{attributes}/>"
    if code.kind == InlineCodeKind.CLOSE:
        return f"<ec{attributes}/>"
    return f"<ph{attributes}/>"


def _render_idml(code: InlineCode) -> str:
    if code.kind == InlineCodeKind.CLOSE:
        return "</CharacterStyleRange>"
    attributes = "".join(
        f' {escape(attribute.name, quote=True)}="{escape(attribute.value, quote=True)}"'
        for attribute in code.native.attributes
        if attribute.name == "style"
    )
    if code.kind == InlineCodeKind.STANDALONE:
        return f"<CharacterStyleRange{attributes}/>"
    return f"<CharacterStyleRange{attributes}>"


def _xml_attributes(code: InlineCode) -> str:
    values: list[tuple[str, str]] = [("id", code.id)]
    if code.pair_id is not None:
        values.append(("rid", code.pair_id))
    if code.native.equivalent_text is not None:
        values.append(("equiv-text", code.native.equivalent_text))
    return "".join(f' {name}="{escape(value, quote=True)}"' for name, value in values)


def _unsupported(code: InlineCode, syntax: TagSyntax, policy: UnsupportedTagPolicy) -> str:
    if policy == UnsupportedTagPolicy.DROP:
        return ""
    if policy == UnsupportedTagPolicy.PLACEHOLDER:
        return f'&lt;lokit-code id="{escape(code.id, quote=True)}"/&gt;'
    raise TagIntegrityError(
        f"Inline code {code.id!r} ({code.native.name!r}) cannot be represented as {syntax.value}"
    )
