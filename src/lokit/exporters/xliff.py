from __future__ import annotations

import asyncio
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit.data.structure import BaseStructure, CodePart, Data, SegmentPart, StreamingStructure, TextPart
from lokit.data.targets import split_targets
from lokit.io.atomic import atomic_output_path
from lokit.io.json import load_lokit_json
from lokit.types import legacy_parts_match_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
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
    with atomic_output_path(path, "wb") as stream:
        with etree.xmlfile(stream, encoding="UTF-8") as xf:
            xf.write_declaration()
            with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
                if group_by_resource:
                    _write_resource_files(xf, document)
                else:
                    _write_file(xf, document, "lokit", _iter_items(document))
                _indent(xf, 0)
        stream.write(b"\n")


def export_xliff_targets(
    documents: Mapping[str, BaseStructure],
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    path = Path(filepath)
    with atomic_output_path(path, "wb") as stream:
        with etree.xmlfile(stream, encoding="UTF-8") as xf:
            xf.write_declaration()
            with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
                for target_locale, document in documents.items():
                    if group_by_resource:
                        wrote_file = False
                        for resource_key, units in _iter_resource_groups(document):
                            wrote_file = True
                            _write_file(
                                xf,
                                document,
                                _target_resource_key(resource_key, target_locale),
                                units,
                            )
                        if not wrote_file:
                            _write_file(
                                xf,
                                document,
                                _target_resource_key("lokit", target_locale),
                                (),
                            )
                    else:
                        _write_file(
                            xf,
                            document,
                            _target_resource_key("lokit", target_locale),
                            _iter_items(document),
                        )
                _indent(xf, 0)
        stream.write(b"\n")


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


def _write_resource_files(xf: XmlWriter, document: Structure) -> None:
    wrote_file = False
    for resource_key, units in _iter_resource_groups(document):
        wrote_file = True
        _write_file(xf, document, resource_key, units)
    if not wrote_file:
        _write_file(xf, document, "lokit", ())


def _iter_resource_groups(
    document: Structure,
) -> Iterator[tuple[str, Iterator[tuple[str, Data]]]]:
    """Group adjacent resources without retaining a streaming document.

    A resource that reappears later is emitted as another valid XLIFF ``file``
    element.  This keeps memory bounded for one-shot iterables while preserving
    document order.
    """
    yield from groupby(
        _iter_items(document),
        key=lambda item: item[1].extensions.get("resource", "lokit"),
    )


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
    first_item = next(unit_iter, None)
    attrs = {
        "original": resource_key or "lokit",
        "datatype": (first_item[1].extensions.get("data_type", "plaintext") if first_item is not None else "plaintext"),
        "source-language": document.source_locale,
    }
    if document.target_locale is not None:
        attrs["target-language"] = document.target_locale
    _indent(xf, 1)
    with xf.element(f"{{{XLIFF_NS}}}file", attrs):
        _indent(xf, 2)
        xf.write(etree.Element("header"))
        _indent(xf, 2)
        with xf.element(f"{{{XLIFF_NS}}}body"):
            if first_item is not None:
                _write_trans_unit(xf, first_item[0], first_item[1])
            for unit_id, unit in unit_iter:
                _write_trans_unit(xf, unit_id, unit)
            _indent(xf, 2)
        _indent(xf, 1)


def _write_trans_unit(xf: XmlWriter, unit_id: str, unit: Data) -> None:
    attrs = {"id": unit.extensions.get("unit_id", unit_id)}
    space = unit.extensions.get("space")
    if space:
        attrs["{http://www.w3.org/XML/1998/namespace}space"] = space
    _indent(xf, 3)
    with xf.element(f"{{{XLIFF_NS}}}trans-unit", attrs):
        _indent(xf, 4)
        _write_segment(
            xf,
            "source",
            unit.source,
            unit.tags.source_parts if unit.tags else [],
            unit.tags.source_tag_map if unit.tags else {},
        )
        if unit.target is not None:
            _indent(xf, 4)
            _write_segment(
                xf,
                "target",
                unit.target,
                unit.tags.target_parts if unit.tags else [],
                unit.tags.target_tag_map if unit.tags else {},
            )
        for comment in unit.comments:
            if comment.context:
                _indent(xf, 4)
                with xf.element(f"{{{XLIFF_NS}}}note"):
                    xf.write(comment.context)
        _indent(xf, 3)


def _write_segment(
    xf: XmlWriter,
    name: str,
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> None:
    with xf.element(f"{{{XLIFF_NS}}}{name}"):
        parts_are_current = bool(parts) and legacy_parts_match_text(text, parts)
        effective_parts = parts if parts_are_current else [TextPart(text)]
        effective_tag_map = tag_map if parts_are_current else {}
        for part in effective_parts:
            if isinstance(part, TextPart):
                xf.write(part.value)
            elif isinstance(part, CodePart):
                code = effective_tag_map.get(part.ref)
                if code is not None:
                    # The root declares XLIFF as the default namespace, so an
                    # unqualified serialized child inherits it without an
                    # unnecessary ``ns0`` prefix declaration.
                    xf.write(_build_code(code, qualified=False))


def _build_segment(
    name: str,
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> _Element:
    element = etree.Element(f"{{{XLIFF_NS}}}{name}")
    parts_are_current = bool(parts) and legacy_parts_match_text(text, parts)
    effective_parts = parts if parts_are_current else [TextPart(text)]
    effective_tag_map = tag_map if parts_are_current else {}
    last_child: _Element | None = None
    for part in effective_parts:
        if isinstance(part, TextPart):
            last_child = _append_text(element, last_child, part.value)
        elif isinstance(part, CodePart):
            code = effective_tag_map.get(part.ref)
            if code is not None:
                child = _build_code(code)
                element.append(child)
                last_child = child
    return element


def _build_code(code: TieData, *, qualified: bool = True) -> _Element:
    namespace = f"{{{XLIFF_NS}}}" if qualified else ""
    if _is_open(code.type):
        element = etree.Element(f"{namespace}bx", id=code.id)
    elif _is_close(code.type):
        element = etree.Element(f"{namespace}ex", id=code.id)
    else:
        element = etree.Element(f"{namespace}x", id=code.id)
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


def _indent(xf: XmlWriter, level: int) -> None:
    xf.write("\n" + "  " * level)


def _first_extension(units: list[tuple[str, Data]], key: str, fallback: str) -> str:
    for _, unit in units:
        value = unit.extensions.get(key)
        if value:
            return value
    return fallback
