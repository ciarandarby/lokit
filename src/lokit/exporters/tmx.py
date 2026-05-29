from __future__ import annotations

from pathlib import Path

from lxml import etree
from lxml.etree import _Element

from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Data,
    SegmentPart,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.io.json import load_lokit_json


def export_tmx(document: BaseStructure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        with etree.xmlfile(stream, encoding="UTF-8") as xf:
            xf.write_declaration()
            with xf.element("tmx", version="1.4"):
                xf.write(_build_header(document))
                with xf.element("body"):
                    for unit_id, unit in document.data.items():
                        xf.write(_build_tu(unit_id, unit, document))


def export_tmx_from_json(source_json: str | Path, target_tmx: str | Path) -> None:
    export_tmx(load_lokit_json(source_json), target_tmx)


def _build_header(document: BaseStructure) -> _Element:
    header = etree.Element(
        "header",
        {
            "creationtool": document.extensions.get("tool_name", "lokit"),
            "creationtoolversion": document.extensions.get("tool_version", "0.1"),
            "segtype": document.extensions.get("segmentation", "sentence"),
            "o-tmf": document.extensions.get("translation_memory_format", "lokit"),
            "adminlang": document.extensions.get("admin_locale", document.source_locale),
            "srclang": document.source_locale,
            "datatype": document.extensions.get("data_type", "text"),
        },
    )
    if document.export_timestamp:
        header.attrib["creationdate"] = document.export_timestamp
    for key, value in document.extensions.items():
        if key.startswith("property."):
            prop = etree.SubElement(header, "prop", type=_property_type(key))
            prop.text = value
    return header


def _build_tu(unit_id: str, unit: Data, document: BaseStructure) -> _Element:
    attrs: dict[str, str] = {"tuid": unit_id}
    if unit.meta.created:
        attrs["creationdate"] = unit.meta.created
    if unit.meta.updated:
        attrs["changedate"] = unit.meta.updated
    creator_id = _first_creator_id(unit)
    if creator_id:
        attrs["creationid"] = creator_id
    change_id = unit.meta.extensions.get("change_id")
    if change_id:
        attrs["changeid"] = change_id
    if unit.meta.usage_count is not None:
        attrs["usagecount"] = str(unit.meta.usage_count)

    tu = etree.Element("tu", attrs)
    _append_unit_properties(tu, unit)
    _append_comments(tu, unit)
    tu.append(
        _build_tuv(
            document.source_locale,
            unit.source,
            unit.tags.source_parts if unit.tags else [],
            unit.tags.source_tag_map if unit.tags else {},
        )
    )
    if document.target_locale is not None and unit.target is not None:
        tu.append(
            _build_tuv(
                document.target_locale,
                unit.target,
                unit.tags.target_parts if unit.tags else [],
                unit.tags.target_tag_map if unit.tags else {},
            )
        )
    return tu


def _append_unit_properties(tu: _Element, unit: Data) -> None:
    if unit.status != TranslationStatus.UNKNOWN:
        prop = etree.SubElement(tu, "prop", type="x-status")
        prop.text = unit.status.value

    if unit.previous_context is not None:
        _append_prop_if_present(tu, "x-previous-id", unit.previous_context.unit_id)
        _append_prop_if_present(tu, "x-previous-source-text", unit.previous_context.source)
        _append_prop_if_present(tu, "x-previous-target-text", unit.previous_context.target)

    if unit.next_context is not None:
        _append_prop_if_present(tu, "x-next-id", unit.next_context.unit_id)
        _append_prop_if_present(tu, "x-next-source-text", unit.next_context.source)
        _append_prop_if_present(tu, "x-next-target-text", unit.next_context.target)

    project = _first_project(unit)
    if project:
        _append_prop_if_present(tu, "x-project", project)

    system = _first_system(unit)
    if system:
        _append_prop_if_present(tu, "x-system", system)

    for key, value in unit.extensions.items():
        if key.startswith("property."):
            _append_prop_if_present(tu, _property_type(key), value)


def _append_comments(tu: _Element, unit: Data) -> None:
    for comment in unit.comments:
        if not comment.context:
            continue
        note = etree.SubElement(tu, "note")
        note.text = comment.context


def _build_tuv(
    locale: str,
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> _Element:
    tuv = etree.Element("tuv", {"{http://www.w3.org/XML/1998/namespace}lang": locale})
    tuv.append(_build_seg(text, parts, tag_map))
    return tuv


def _build_seg(
    text: str,
    parts: list[SegmentPart],
    tag_map: dict[str, TieData],
) -> _Element:
    seg = etree.Element("seg")
    effective_parts = parts if parts else [TextPart(text)]
    pair_numbers = _pair_numbers(tag_map)
    last_child: _Element | None = None

    for part in effective_parts:
        if isinstance(part, TextPart):
            last_child = _append_text(seg, last_child, part.value)
        elif isinstance(part, CodePart):
            code = tag_map.get(part.ref)
            if code is None:
                last_child = _append_text(seg, last_child, "")
            else:
                child = _build_code_element(code, pair_numbers)
                seg.append(child)
                last_child = child

    return seg


def _build_code_element(code: TieData, pair_numbers: dict[str, str]) -> _Element:
    if _is_open(code.type):
        element = etree.Element("bpt", i=_pair_number(code, pair_numbers), type=code.type.value)
        element.text = f"<lokit id=\"{code.pair_id or code.id}\">"
        return element
    if _is_close(code.type):
        element = etree.Element("ept", i=_pair_number(code, pair_numbers))
        element.text = "</lokit>"
        return element
    element = etree.Element("ph", x=str(code.order), type=code.type.value)
    element.text = f"<lokit id=\"{code.id}\"/>"
    return element


def _append_text(seg: _Element, last_child: _Element | None, value: str) -> _Element | None:
    if last_child is None:
        seg.text = (seg.text or "") + value
    else:
        last_child.tail = (last_child.tail or "") + value
    return last_child


def _pair_numbers(tag_map: dict[str, TieData]) -> dict[str, str]:
    pair_ids: dict[str, str] = {}
    index = 0
    for code in sorted(tag_map.values(), key=lambda item: item.order):
        if code.pair_id is not None and code.pair_id not in pair_ids:
            pair_ids[code.pair_id] = str(index)
            index += 1
    return pair_ids


def _pair_number(code: TieData, pair_numbers: dict[str, str]) -> str:
    if code.pair_id is None:
        return str(code.order)
    return pair_numbers.get(code.pair_id, str(code.order))


def _is_open(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".open")


def _is_close(tie_type: TieType) -> bool:
    return tie_type.value.endswith(".close")


def _append_prop_if_present(tu: _Element, prop_type: str, value: str | None) -> None:
    if value is None or value == "":
        return
    prop = etree.SubElement(tu, "prop", type=prop_type)
    prop.text = value


def _first_creator_id(unit: Data) -> str | None:
    for comment in unit.comments:
        if comment.origin is not None and comment.origin.creator_id:
            return comment.origin.creator_id
    return None


def _first_project(unit: Data) -> str | None:
    for comment in unit.comments:
        if comment.origin is not None and comment.origin.project:
            return comment.origin.project
    return None


def _first_system(unit: Data) -> str | None:
    for comment in unit.comments:
        if comment.origin is not None and comment.origin.system:
            return comment.origin.system
    return None


def _property_type(key: str) -> str:
    prefix = "property."
    if key.startswith(prefix):
        return key[len(prefix) :]
    return key
