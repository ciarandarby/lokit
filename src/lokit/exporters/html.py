from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from lxml import html as lxml_html
from lxml.html import HtmlElement, tostring

from lokit.data.structure import BaseStructure, CodePart, Data, StreamingStructure, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.io.atomic import atomic_output_path

Structure = BaseStructure | StreamingStructure


def export_html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    if source_html is not None:
        _export_from_source(document, path, Path(source_html))
    else:
        _export_minimal(document, path)


async def export_html_async(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
) -> None:
    await asyncio.to_thread(export_html, document, filepath, source_html)


def _export_from_source(
    document: Structure, output: Path, source: Path
) -> None:
    doc = lxml_html.parse(str(source))
    root = doc.getroot()
    if root is None:
        _export_minimal(document, output)
        return

    if document.target_locale:
        root.set("lang", document.target_locale)

    unit_lookup = _build_unit_lookup(document)
    index = 0

    head = root.find(".//head")
    if head is not None:
        for meta_el in head.iterfind(".//meta"):
            name = (meta_el.get("name") or "").lower()
            if name in ("description", "keywords"):
                key = f"html:meta.{name}:{index}"
                unit = unit_lookup.get(key)
                if unit is not None and unit.target:
                    meta_el.set("content", unit.target)
                index += 1

    block_tags = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "td", "th", "dt", "dd", "caption",
        "figcaption", "blockquote", "label", "option", "title",
    }

    for el in list(root.iter()):
        tag = el.tag if isinstance(el.tag, str) else ""
        tag_lower = tag.lower()

        if tag_lower in block_tags:
            key = f"html:{tag_lower}:{index}"
            unit = unit_lookup.get(key)
            if unit is not None and unit.target:
                _replace_element_text(el, unit)
            index += 1

        if tag_lower == "img":
            alt = el.get("alt")
            if alt and alt.strip():
                key = f"html:img.alt:{index}"
                unit = unit_lookup.get(key)
                if unit is not None and unit.target:
                    el.set("alt", unit.target)
                index += 1

    result = tostring(root, encoding="unicode", doctype="<!DOCTYPE html>")
    with atomic_output_path(output, "w") as f:
        f.write(result)


def _export_minimal(document: Structure, output: Path) -> None:
    lang = document.target_locale or document.source_locale
    lines: list[str] = [
        "<!DOCTYPE html>",
        f'<html lang="{_escape(lang)}">',
        "<head>",
        '<meta charset="utf-8">',
    ]

    for unit_id, unit in _iter_items(document):
        if "meta." in unit_id:
            name = unit.extensions.get("meta_name", "")
            text = unit.target or unit.source
            lines.append(f'<meta name="{_escape(name)}" content="{_escape(text)}">')

    lines.append("</head>")
    lines.append("<body>")

    for unit_id, unit in _iter_items(document):
        if "meta." in unit_id or "img.alt" in unit_id:
            continue
        text = unit.target or unit.source
        tag = _extract_tag_from_id(unit_id)
        if unit.tags and unit.tags.source_parts:
            content = _rebuild_inline(unit, is_target=unit.target is not None)
            lines.append(f"<{tag}>{content}</{tag}>")
        else:
            lines.append(f"<{tag}>{_escape(text)}</{tag}>")

    lines.append("</body>")
    lines.append("</html>")
    with atomic_output_path(output, "w") as f:
        f.write("\n".join(lines))


def _replace_element_text(element: HtmlElement, unit: Data) -> None:
    if unit.tags and unit.tags.source_parts:
        content = _rebuild_inline(unit, is_target=True)
        for child in list(element):
            element.remove(child)
        element.text = None
        fragment = cast(list[object], lxml_html.fragments_fromstring(content))
        if isinstance(fragment[0], str):
            element.text = fragment[0]
            children = fragment[1:]
        else:
            children = fragment
        for child in children:
            if isinstance(child, HtmlElement):
                element.append(child)
            elif isinstance(child, str):
                if len(element):
                    last = element[-1]
                    last.tail = (last.tail or "") + child
                else:
                    element.text = (element.text or "") + child
    else:
        for child in list(element):
            element.remove(child)
        element.text = unit.target


def _rebuild_inline(unit: Data, is_target: bool) -> str:
    if is_target and unit.tags and unit.tags.target_parts:
        parts = unit.tags.target_parts
        tag_map = unit.tags.target_tag_map
    elif unit.tags:
        parts = unit.tags.source_parts
        tag_map = unit.tags.source_tag_map
    else:
        return _escape(unit.target or unit.source)

    result: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            result.append(_escape(part.value))
        elif isinstance(part, CodePart):
            tie = tag_map.get(part.ref)
            if tie is None:
                continue
            result.append(_tie_to_html(tie))
    return "".join(result)


def _tie_to_html(tie: TieData) -> str:
    name = tie.original_name or ""
    if tie.type.value.endswith(".open"):
        attrs = _format_attrs(tie.attributes)
        return f"<{name}{attrs}>"
    if tie.type.value.endswith(".close"):
        return f"</{name}>"
    if tie.type == TieType.BR:
        return "<br>"
    if tie.type == TieType.WBR:
        return "<wbr>"
    if tie.type == TieType.IMG:
        attrs = _format_attrs(tie.attributes)
        return f"<img{attrs}>"
    attrs = _format_attrs(tie.attributes)
    return f"<{name}{attrs}/>"


def _format_attrs(attributes: dict[str, str]) -> str:
    if not attributes:
        return ""
    parts = [f' {k}="{_escape(v)}"' for k, v in attributes.items()]
    return "".join(parts)


def _build_unit_lookup(document: Structure) -> dict[str, Data]:
    return dict(_iter_items(document))


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _extract_tag_from_id(unit_id: str) -> str:
    parts = unit_id.split(":")
    if len(parts) >= 2:
        return parts[1]
    return "p"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
