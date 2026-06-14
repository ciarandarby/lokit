from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit.data.structure import BaseStructure, CodePart, Data, SegmentPart, StreamingStructure, TextPart
from lokit.data.targets import split_targets
from lokit.io.atomic import atomic_output_path
from lokit.io.json import load_lokit_json

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from contextlib import AbstractContextManager

    from lxml.etree import _Element

    from lokit.data.tag_types import TieData, TieType

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NSMAP = cast("dict[str, str]", {None: XLIFF_NS})


Structure = BaseStructure | StreamingStructure


class XmlWriter(Protocol):
    def element(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        **kwargs: object,
    ) -> AbstractContextManager[object]: ...

    def write(self, value: str | _Element) -> None: ...


def export_xliff(
    document: Structure,
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    if isinstance(document, BaseStructure) and document.target_locale is None and document.target_locales:
        export_xliff_targets(split_targets(document), filepath, group_by_resource=group_by_resource)
        return
    path = Path(filepath)
    with atomic_output_path(path, "wb") as stream, etree.xmlfile(stream, encoding="UTF-8") as xf:
        xf.write_declaration()
        with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
            if group_by_resource:
                for resource_key, units in _group_by_resource(document).items():
                    _write_file(xf, document, resource_key, units)
            else:
                _write_file(xf, document, "lokit", _iter_items(document))


def export_xliff_targets(
    documents: Mapping[str, BaseStructure],
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    path = Path(filepath)
    with atomic_output_path(path, "wb") as stream, etree.xmlfile(stream, encoding="UTF-8") as xf:
        xf.write_declaration()
        with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
            for target_locale, document in documents.items():
                if group_by_resource:
                    for resource_key, units in _group_by_resource(document).items():
                        _write_file(
                            xf,
                            document,
                            _target_resource_key(resource_key, target_locale),
                            units,
                        )
                else:
                    _write_file(
                        xf,
                        document,
                        _target_resource_key("lokit", target_locale),
                        _iter_items(document),
                    )


def export_xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    export_xliff(load_lokit_json(source_json), target_xliff)


async def export_xliff_async(document: Structure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_xliff, document, filepath)


async def export_xliff_targets_async(
    documents: Mapping[str, BaseStructure],
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    await asyncio.to_thread(
        export_xliff_targets,
        documents,
        filepath,
        group_by_resource=group_by_resource,
    )


async def export_xliff_from_json_async(source_json: str | Path, target_xliff: str | Path) -> None:
    await asyncio.to_thread(export_xliff_from_json, source_json, target_xliff)


def _group_by_resource(
    document: Structure,
) -> OrderedDict[str, list[tuple[str, Data]]]:
    groups: OrderedDict[str, list[tuple[str, Data]]] = OrderedDict()
    for unit_id, unit in _iter_items(document):
        resource = unit.extensions.get("resource", "lokit")
        groups.setdefault(resource, []).append((unit_id, unit))
    return groups


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _write_file(
    xf: XmlWriter,
    document: Structure,
    resource_key: str,
    units: Iterable[tuple[str, Data]],
) -> None:
    unit_iter = iter(units)
    try:
        first_id, first_unit = next(unit_iter)
    except StopIteration:
        return
    attrs = {
        "original": resource_key or "lokit",
        "datatype": first_unit.extensions.get("data_type", "plaintext"),
        "source-language": document.source_locale,
    }
    if document.target_locale is not None:
        attrs["target-language"] = document.target_locale
    with xf.element(f"{{{XLIFF_NS}}}file", attrs):
        xf.write(etree.Element(f"{{{XLIFF_NS}}}header"))
        with xf.element(f"{{{XLIFF_NS}}}body"):
            _write_trans_unit(xf, first_id, first_unit)
            for unit_id, unit in unit_iter:
                _write_trans_unit(xf, unit_id, unit)


def _write_trans_unit(xf: XmlWriter, unit_id: str, unit: Data) -> None:
    attrs = {"id": unit.extensions.get("unit_id", unit_id)}
    space = unit.extensions.get("space")
    if space:
        attrs["{http://www.w3.org/XML/1998/namespace}space"] = space
    with xf.element(f"{{{XLIFF_NS}}}trans-unit", attrs):
        _write_segment(
            xf,
            "source",
            unit.source,
            unit.tags.source_parts if unit.tags else [],
            unit.tags.source_tag_map if unit.tags else {},
        )
        if unit.target is not None:
            _write_segment(
                xf,
                "target",
                unit.target,
                unit.tags.target_parts if unit.tags else [],
                unit.tags.target_tag_map if unit.tags else {},
            )
        for comment in unit.comments:
            if comment.context:
                with xf.element(f"{{{XLIFF_NS}}}note"):
                    xf.write(comment.context)


def _write_segment(
    xf: XmlWriter,
    name: str,
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> None:
    with xf.element(f"{{{XLIFF_NS}}}{name}"):
        effective_parts = parts if parts else [TextPart(text)]
        for part in effective_parts:
            if isinstance(part, TextPart):
                xf.write(part.value)
            elif isinstance(part, CodePart):
                code = tag_map.get(part.ref)
                if code is not None:
                    xf.write(_build_code(code))


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


def _target_resource_key(resource_key: str, target_locale: str) -> str:
    return f"{resource_key}:{target_locale}" if target_locale else resource_key


def _append_text(parent: _Element, last_child: _Element | None, value: str) -> _Element | None:
    if last_child is None:
        parent.text = (parent.text or "") + value
    else:
        last_child.tail = (last_child.tail or "") + value
    return last_child


def _is_open(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".open")


def _is_close(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".close")


def _first_extension(units: list[tuple[str, Data]], key: str, fallback: str) -> str:
    for _, unit in units:
        value = unit.extensions.get(key)
        if value:
            return value
    return fallback
