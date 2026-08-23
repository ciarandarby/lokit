from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from lxml import html as lxml_html
from lxml.html import HtmlElement, tostring

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.data.targets import StreamingTargetSplit
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import atomic_output_path
from lokit.types import TagSyntax, render_segment, segment_from_legacy

if TYPE_CHECKING:
    from collections.abc import Iterable

Structure = BaseStructure | StreamingStructure


def export_html(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    export_document = prepare_export_document(
        document,
        resolve_placeholders=resolve_placeholders,
    )
    path = Path(filepath)
    if export_document.target_locale is None and export_document.target_locales:
        if path.suffix:
            raise ValueError("HTML export needs a selected target locale for a single output path")
        path.mkdir(parents=True, exist_ok=True)
        with StreamingTargetSplit(export_document) as documents:
            for locale, target_document in documents.items():
                _export_html_single(
                    target_document,
                    path / f"index.{locale}.html",
                    source_html,
                )
        return
    _export_html_single(export_document, path, source_html)


def _export_html_single(
    document: Structure,
    path: Path,
    source_html: str | Path | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if source_html is not None:
        _export_from_source(document, path, Path(source_html))
    else:
        _export_minimal(document, path)


async def export_html_async(
    document: Structure,
    filepath: str | Path,
    source_html: str | Path | None = None,
    *,
    resolve_placeholders: bool = True,
) -> None:
    await asyncio.to_thread(
        export_html,
        document,
        filepath,
        source_html,
        resolve_placeholders=resolve_placeholders,
    )


def _export_from_source(document: Structure, output: Path, source: Path) -> None:
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
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "td",
        "th",
        "dt",
        "dd",
        "caption",
        "figcaption",
        "blockquote",
        "label",
        "option",
        "title",
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
    # Head metadata and body content must be emitted in different sections, but
    # a StreamingStructure is one-shot. Spool each section to disk in one pass
    # rather than retaining all units or iterating the input twice.
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as head,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n") as body,
    ):
        for unit_id, unit in _iter_items(document):
            if "meta." in unit_id:
                name = unit.extensions.get("meta_name", "")
                text = unit.target or unit.source
                head.write(f'<meta name="{_escape(name)}" content="{_escape(text)}">\n')
                continue
            if "img.alt" in unit_id:
                continue
            text = unit.target or unit.source
            tag = _extract_tag_from_id(unit_id)
            if unit.tags and unit.tags.source_parts:
                content = _rebuild_inline(unit, is_target=unit.target is not None)
                body.write(f"<{tag}>{content}</{tag}>\n")
            else:
                body.write(f"<{tag}>{_escape(text)}</{tag}>\n")

        head.seek(0)
        body.seek(0)
        with atomic_output_path(output, "w") as destination:
            destination.write(
                "\n".join(
                    (
                        "<!DOCTYPE html>",
                        f'<html lang="{_escape(lang)}">',
                        "<head>",
                        '<meta charset="utf-8">',
                        "",
                    )
                )
            )
            shutil.copyfileobj(head, destination)
            destination.write("</head>\n<body>\n")
            shutil.copyfileobj(body, destination)
            destination.write("</body>\n</html>")


def _replace_element_text(element: HtmlElement, unit: Data) -> None:
    if unit.tags and unit.tags.source_parts:
        content = _rebuild_inline(unit, is_target=True)
        for child in list(element):
            element.remove(child)
        element.text = None
        fragment = cast("list[object]", lxml_html.fragments_fromstring(content))
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
    text = unit.target if is_target and unit.target is not None else unit.source
    if unit.tags is None:
        return _escape(text)
    if is_target:
        parts = unit.tags.target_parts
        tag_map = unit.tags.target_tag_map
    else:
        parts = unit.tags.source_parts
        tag_map = unit.tags.source_tag_map
    segment = segment_from_legacy(text, parts, tag_map, syntax=TagSyntax.HTML)
    return render_segment(segment, TagSyntax.HTML, native_syntax=TagSyntax.HTML)


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
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
