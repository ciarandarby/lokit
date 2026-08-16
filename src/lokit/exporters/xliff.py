from __future__ import annotations

from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lxml import etree

from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Data,
    SegmentPart,
    StreamingStructure,
    TextPart,
    TranslationStatus,
)
from lokit.data.targets import StreamingTargetSplit, split_targets
from lokit.io.atomic import AsyncExportCancelled, atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.io.json import load_lokit_json
from lokit.types import legacy_parts_match_text

if TYPE_CHECKING:
    import threading
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


class _Closable(Protocol):
    def close(self) -> None: ...


def export_xliff(
    document: Structure,
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    _export_xliff(document, filepath, group_by_resource, None)


def _export_xliff(
    document: Structure,
    filepath: str | Path,
    group_by_resource: bool,
    cancellation: threading.Event | None,
) -> None:
    if isinstance(document, BaseStructure) and document.target_locale is None:
        base_documents = split_targets(document)
        if base_documents:
            _export_xliff_targets(base_documents, filepath, group_by_resource, cancellation)
            return
    if isinstance(document, StreamingStructure) and document.target_locale is None:
        context = StreamingTargetSplit(document, _cancellation=cancellation)
        try:
            with context as stream_documents:
                if stream_documents:
                    _export_xliff_targets(stream_documents, filepath, group_by_resource, cancellation)
                else:
                    _export_xliff_single(
                        context._source_stream(),
                        filepath,
                        group_by_resource,
                        cancellation,
                    )
        except AsyncExportCancelled:
            return
        return
    _export_xliff_single(document, filepath, group_by_resource, cancellation)


def _export_xliff_single(
    document: Structure,
    filepath: str | Path,
    group_by_resource: bool,
    cancellation: threading.Event | None,
) -> None:
    path = Path(filepath)
    items = iter(_iter_items(document))
    try:
        raise_if_cancelled(cancellation)
        with atomic_output_path(path, "wb", cancellation=cancellation) as stream:
            with etree.xmlfile(stream, encoding="UTF-8") as xf:
                xf.write_declaration()
                with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
                    if group_by_resource:
                        _write_resource_files(xf, document, items, cancellation)
                    else:
                        _write_file(xf, document, "lokit", items, cancellation)
                    raise_if_cancelled(cancellation)
                    _indent(xf, 0)
            raise_if_cancelled(cancellation)
            stream.write(b"\n")
    except AsyncExportCancelled:
        return
    finally:
        _close_iterator(items)


def export_xliff_targets(
    documents: Mapping[str, BaseStructure],
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    _export_xliff_targets(documents, filepath, group_by_resource, None)


def _export_xliff_targets(
    documents: Mapping[str, Structure],
    filepath: str | Path,
    group_by_resource: bool,
    cancellation: threading.Event | None,
) -> None:
    path = Path(filepath)
    try:
        raise_if_cancelled(cancellation)
        with atomic_output_path(path, "wb", cancellation=cancellation) as stream:
            with etree.xmlfile(stream, encoding="UTF-8") as xf:
                xf.write_declaration()
                with xf.element(f"{{{XLIFF_NS}}}xliff", nsmap=NSMAP, version="1.2"):
                    for _target_locale, document in documents.items():
                        raise_if_cancelled(cancellation)
                        items = iter(_iter_items(document))
                        try:
                            if group_by_resource:
                                wrote_file = False
                                for resource_key, units in _iter_resource_groups(items):
                                    wrote_file = True
                                    _write_file(
                                        xf,
                                        document,
                                        resource_key,
                                        units,
                                        cancellation,
                                    )
                                if not wrote_file:
                                    _write_file(
                                        xf,
                                        document,
                                        "lokit",
                                        (),
                                        cancellation,
                                    )
                            else:
                                _write_file(
                                    xf,
                                    document,
                                    "lokit",
                                    items,
                                    cancellation,
                                )
                        finally:
                            _close_iterator(items)
                    raise_if_cancelled(cancellation)
                    _indent(xf, 0)
            raise_if_cancelled(cancellation)
            stream.write(b"\n")
    except AsyncExportCancelled:
        return


def export_xliff_from_json(source_json: str | Path, target_xliff: str | Path) -> None:
    export_xliff(load_lokit_json(source_json), target_xliff)


async def export_xliff_async(
    document: Structure,
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_xliff(
            document,
            filepath,
            group_by_resource,
            cancellation,
        )
    )


async def export_xliff_targets_async(
    documents: Mapping[str, BaseStructure],
    filepath: str | Path,
    *,
    group_by_resource: bool = False,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_xliff_targets(
            documents,
            filepath,
            group_by_resource,
            cancellation,
        )
    )


async def export_xliff_from_json_async(source_json: str | Path, target_xliff: str | Path) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_xliff(
            load_lokit_json(source_json),
            target_xliff,
            False,
            cancellation,
        )
    )


def _write_resource_files(
    xf: XmlWriter,
    document: Structure,
    items: Iterator[tuple[str, Data]],
    cancellation: threading.Event | None,
) -> None:
    wrote_file = False
    for resource_key, units in _iter_resource_groups(items):
        raise_if_cancelled(cancellation)
        wrote_file = True
        _write_file(xf, document, resource_key, units, cancellation)
    if not wrote_file:
        _write_file(xf, document, "lokit", (), cancellation)


def _iter_resource_groups(
    items: Iterable[tuple[str, Data]],
) -> Iterator[tuple[str, Iterator[tuple[str, Data]]]]:
    """Group adjacent resources without retaining a streaming document.

    A resource that reappears later is emitted as another valid XLIFF ``file``
    element.  This keeps memory bounded for one-shot iterables while preserving
    document order.
    """
    yield from groupby(
        items,
        key=lambda item: item[1].extensions.get("resource", "lokit"),
    )


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _close_iterator(items: Iterator[tuple[str, Data]]) -> None:
    candidate: object = items
    if hasattr(candidate, "close"):
        cast("_Closable", candidate).close()


def _write_file(
    xf: XmlWriter,
    document: Structure,
    resource_key: str,
    units: Iterable[tuple[str, Data]],
    cancellation: threading.Event | None,
) -> None:
    unit_iter = iter(units)
    raise_if_cancelled(cancellation)
    first_item = next(unit_iter, None)
    raise_if_cancelled(cancellation)
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
                _write_trans_unit(xf, first_item[0], first_item[1], document.target_locale)
            for unit_id, unit in unit_iter:
                raise_if_cancelled(cancellation)
                _write_trans_unit(xf, unit_id, unit, document.target_locale)
            raise_if_cancelled(cancellation)
            _indent(xf, 2)
        _indent(xf, 1)


def _write_trans_unit(
    xf: XmlWriter,
    unit_id: str,
    unit: Data,
    target_locale: str | None,
) -> None:
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
        target_text = unit.target
        target_status = unit.status
        target_parts = unit.tags.target_parts if unit.tags else []
        target_tag_map = unit.tags.target_tag_map if unit.tags else {}
        selected = unit.targets.get(target_locale) if target_locale is not None else None
        if selected is None and target_text is None and len(unit.targets) == 1:
            selected = next(iter(unit.targets.values()))
        if selected is not None:
            target_text = selected.text
            if selected.status is not TranslationStatus.UNKNOWN:
                target_status = selected.status
            if selected.tags is None:
                target_parts = []
                target_tag_map = {}
            else:
                target_parts = selected.tags.parts
                target_tag_map = selected.tags.tag_map
        if target_text is not None:
            _indent(xf, 4)
            _write_segment(
                xf,
                "target",
                target_text,
                target_parts,
                target_tag_map,
                _target_attributes(target_status),
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
    attributes: dict[str, str] | None = None,
) -> None:
    with xf.element(f"{{{XLIFF_NS}}}{name}", attributes):
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


def _target_attributes(status: TranslationStatus) -> dict[str, str] | None:
    if status is TranslationStatus.APPROVED:
        return {"state": "final"}
    if status is TranslationStatus.REVIEWED:
        return {"state": "needs-review-l10n"}
    if status is TranslationStatus.TRANSLATED:
        return {"state": "translated"}
    if status is TranslationStatus.NEW:
        return {"state": "new"}
    if status in {TranslationStatus.DRAFT, TranslationStatus.REJECTED}:
        return {"state": "needs-translation"}
    return None


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
