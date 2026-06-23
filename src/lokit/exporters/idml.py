from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from lokit.data.structure import BaseStructure, CodePart, Data, TextPart
from lokit.data.targets import select_target

if TYPE_CHECKING:
    from lxml.etree import _Element


def export_idml(
    document: BaseStructure,
    filepath: str | Path,
    source_idml: str | Path,
) -> None:
    output_path = Path(filepath)
    source_path = Path(source_idml)
    if document.target_locale is None and document.target_locales:
        if output_path.suffix:
            raise ValueError("IDML export needs a selected target locale for a single output path")
        output_path.mkdir(parents=True, exist_ok=True)
        for locale in document.target_locales:
            export_idml(select_target(document, locale), output_path / f"{locale}.idml", source_path)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    story_units = _group_by_story(document)
    replacements: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(str(source_path), "r") as zf_in:
            story_files = [
                name for name in zf_in.namelist() if name.startswith("Stories/Story_") and name.endswith(".xml")
            ]
            for story_file in story_files:
                units = story_units.get(story_file)
                if not units:
                    continue

                with zf_in.open(story_file) as stream:
                    tree = etree.parse(stream)
                    root = tree.getroot()
                    _apply_translations(root, units)
                    modified_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8")

                replacements[story_file] = modified_xml
            _write_replaced_zip(zf_in, tmp_path, replacements)
        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


async def export_idml_async(
    document: BaseStructure,
    filepath: str | Path,
    source_idml: str | Path,
) -> None:
    await asyncio.to_thread(export_idml, document, filepath, source_idml)


def _group_by_story(
    document: BaseStructure,
) -> dict[str, dict[str, Data]]:
    groups: dict[str, dict[str, Data]] = {}
    for unit_id, unit in document.data.items():
        story = unit.extensions.get("story", "")
        if story:
            groups.setdefault(story, {})[unit_id] = unit
    return groups


def _apply_translations(root: _Element, units: dict[str, Data]) -> None:
    paragraph_index = 0
    story_name = _story_name_from_units(units)

    for psr in root.iter():
        if _element_local_name(psr) != "ParagraphStyleRange":
            continue

        unit_id = f"{story_name}:p{paragraph_index}"
        unit = units.get(unit_id)
        if unit is not None and unit.target:
            _replace_paragraph_text(psr, unit)
        paragraph_index += 1


def _replace_paragraph_text(psr: _Element, unit: Data) -> None:
    char_ranges = [el for el in psr if _element_local_name(el) == "CharacterStyleRange"]
    if not char_ranges:
        return

    if unit.tags and unit.tags.target_parts:
        _replace_with_tagged_parts(char_ranges, unit)
    else:
        target_text = unit.target or ""
        _distribute_text(char_ranges, target_text)


def _replace_with_tagged_parts(char_ranges: list[_Element], unit: Data) -> None:
    if unit.tags is None:
        return

    parts = unit.tags.target_parts
    tag_map = unit.tags.target_tag_map

    range_texts: dict[str, str] = {}
    current_style: str | None = None
    current_text_parts: list[str] = []

    for part in parts:
        if isinstance(part, TextPart):
            current_text_parts.append(part.value)
        elif isinstance(part, CodePart):
            tie = tag_map.get(part.ref)
            if tie is None:
                continue
            if tie.type.value.endswith(".open"):
                style = tie.attributes.get("style", "")
                if current_text_parts and current_style is not None:
                    range_texts[current_style] = "".join(current_text_parts)
                    current_text_parts = []
                current_style = style
            elif tie.type.value.endswith(".close"):
                if current_style is not None:
                    range_texts[current_style] = "".join(current_text_parts)
                    current_text_parts = []
                    current_style = None

    plain_text = "".join(current_text_parts) if current_text_parts else None

    for csr in char_ranges:
        style = csr.get("AppliedCharacterStyle") or ""
        if style in range_texts:
            _set_content_text(csr, range_texts[style])
        elif plain_text is not None and (not style or style == "CharacterStyle/$ID/[No character style]"):
            _set_content_text(csr, plain_text)
            plain_text = None
        else:
            _set_content_text(csr, "")


def _distribute_text(char_ranges: list[_Element], text: str) -> None:
    if len(char_ranges) == 1:
        _set_content_text(char_ranges[0], text)
        return

    first = char_ranges[0]
    _set_content_text(first, text)
    for csr in char_ranges[1:]:
        _set_content_text(csr, "")


def _set_content_text(csr: _Element, text: str) -> None:
    for child in csr.iter():
        if _element_local_name(child) == "Content":
            child.text = text
            text = ""


def _write_replaced_zip(
    source: zipfile.ZipFile,
    output_path: Path,
    replacements: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(output_path, "w") as target:
        for info in source.infolist():
            data = replacements.get(info.filename)
            if data is not None:
                target.writestr(info, data)
                continue
            with source.open(info, "r") as source_member, target.open(info, "w") as target_member:
                shutil.copyfileobj(source_member, target_member, length=1024 * 1024)


def _story_name_from_units(units: dict[str, Data]) -> str:
    for unit_id in units:
        parts = unit_id.split(":")
        if parts:
            return parts[0]
    return ""


def _local_name(tag: object) -> str:
    if isinstance(tag, str):
        name = tag
    elif isinstance(tag, bytes):
        name = tag.decode("utf-8")
    else:
        return ""
    if "}" in name:
        return name.split("}", 1)[1]
    return name


def _element_local_name(element: _Element) -> str:
    tag: object = getattr(element, "tag", "")
    return _local_name(tag)
