from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import cast

from lxml import etree
from lxml.etree import _Element

from lokit.data.structure import BaseStructure, CodePart, Data, SegmentPart, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.io.json import load_lokit_json

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NSMAP = cast(dict[str, str], {None: XLIFF_NS})


def export_xliff(document: BaseStructure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        with etree.xmlfile(stream, encoding="UTF-8") as xf:
            xf.write_declaration()
            with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
                for resource_key, units in _group_by_resource(document).items():
                    attrs = {
                        "original": resource_key or "lokit",
                        "datatype": _first_extension(units, "data_type", "plaintext"),
                        "source-language": document.source_locale,
                    }
                    if document.target_locale is not None:
                        attrs["target-language"] = document.target_locale
                    with xf.element(f"{{{XLIFF_NS}}}file", attrs):
                        xf.write(etree.Element(f"{{{XLIFF_NS}}}header"))
                        with xf.element(f"{{{XLIFF_NS}}}body"):
                            for unit_id, unit in units:
                                xf.write(_build_trans_unit(unit_id, unit))


def export_xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    export_xliff(load_lokit_json(source_json), target_xliff)


async def export_xliff_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_xliff, document, filepath)


async def export_xliff_from_json_async(
    source_json: str | Path, target_xliff: str | Path
) -> None:
    await asyncio.to_thread(export_xliff_from_json, source_json, target_xliff)


def _group_by_resource(
    document: BaseStructure,
) -> OrderedDict[str, list[tuple[str, Data]]]:
    groups: OrderedDict[str, list[tuple[str, Data]]] = OrderedDict()
    for unit_id, unit in document.data.items():
        resource = unit.extensions.get("resource", "lokit")
        groups.setdefault(resource, []).append((unit_id, unit))
    return groups


def _build_trans_unit(unit_id: str, unit: Data) -> _Element:
    attrs = {"id": unit.extensions.get("unit_id", unit_id)}
    space = unit.extensions.get("space")
    if space:
        attrs["{http://www.w3.org/XML/1998/namespace}space"] = space
    trans_unit = etree.Element(f"{{{XLIFF_NS}}}trans-unit", attrs)
    trans_unit.append(
        _build_segment(
            "source",
            unit.source,
            unit.tags.source_parts if unit.tags else [],
            unit.tags.source_tag_map if unit.tags else {},
        )
    )
    if unit.target is not None:
        target = _build_segment(
            "target",
            unit.target,
            unit.tags.target_parts if unit.tags else [],
            unit.tags.target_tag_map if unit.tags else {},
        )
        trans_unit.append(target)
    for comment in unit.comments:
        if comment.context:
            note = etree.SubElement(trans_unit, f"{{{XLIFF_NS}}}note")
            note.text = comment.context
    return trans_unit


def _build_segment(
    name: str,
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> _Element:
    element = etree.Element(f"{{{XLIFF_NS}}}{name}")
    effective_parts = parts if parts else [TextPart(text)]
    last_child: _Element | None = None
    for part in effective_parts:
        if isinstance(part, TextPart):
            last_child = _append_text(element, last_child, part.value)
        elif isinstance(part, CodePart):
            code = tag_map.get(part.ref)
            if code is not None:
                child = _build_code(code)
                element.append(child)
                last_child = child
    return element


def _build_code(code: TieData) -> _Element:
    if _is_open(code.type):
        element = etree.Element(f"{{{XLIFF_NS}}}bx", id=code.id)
    elif _is_close(code.type):
        element = etree.Element(f"{{{XLIFF_NS}}}ex", id=code.id)
    else:
        element = etree.Element(f"{{{XLIFF_NS}}}x", id=code.id)
    if code.pair_id is not None:
        element.attrib["rid"] = code.pair_id
    return element


def _append_text(
    parent: _Element, last_child: _Element | None, value: str
) -> _Element | None:
    if last_child is None:
        parent.text = (parent.text or "") + value
    else:
        last_child.tail = (last_child.tail or "") + value
    return last_child


def _is_open(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".open")


def _is_close(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".close")


def _first_extension(
    units: list[tuple[str, Data]], key: str, fallback: str
) -> str:
    for _, unit in units:
        value = unit.extensions.get(key)
        if value:
            return value
    return fallback
