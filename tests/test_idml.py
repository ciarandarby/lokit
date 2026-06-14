from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from lokit.data.structure import BaseStructure, CodePart, Data, TargetData, TextPart, TranslationStatus
from lokit.data.tag_types import TieType
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.importers import import_idml, import_idml_async


@pytest.fixture
def sample_idml_file(tmp_path: Path) -> Path:
    idml_path = tmp_path / "document.idml"
    story_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Story xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idms/1.0/">\n'
        "  <ParagraphStyleRange>\n"
        "    <CharacterStyleRange>\n"
        "      <Content>Hello world</Content>\n"
        "    </CharacterStyleRange>\n"
        "  </ParagraphStyleRange>\n"
        "  <ParagraphStyleRange>\n"
        "    <CharacterStyleRange>\n"
        "      <Content>This is </Content>\n"
        "    </CharacterStyleRange>\n"
        '    <CharacterStyleRange AppliedCharacterStyle="CharacterStyle/MyBold">\n'
        "      <Content>bold text</Content>\n"
        "    </CharacterStyleRange>\n"
        "  </ParagraphStyleRange>\n"
        "</Story>\n"
    )

    with zipfile.ZipFile(idml_path, "w") as zf:
        zf.writestr("Stories/Story_u123.xml", story_xml)

    return idml_path


def test_idml_roundtrip(sample_idml_file: Path, tmp_path: Path) -> None:
    imported = import_idml(
        str(sample_idml_file), source_locale="en", target_locale="fr"
    )

    assert imported.source_locale == "en"
    assert imported.target_locale == "fr"
    assert "Story_u123:p0" in imported.data
    assert imported.data["Story_u123:p0"].source == "Hello world"
    assert imported.data["Story_u123:p0"].status == TranslationStatus.UNKNOWN
    assert "Story_u123:p1" in imported.data
    p1 = imported.data["Story_u123:p1"]
    assert p1.source == "This is bold text"
    assert p1.tags is not None
    assert "t0" in p1.tags.source_tag_map
    assert p1.tags.source_tag_map["t0"].type == TieType.CUSTOM_OPEN
    assert p1.tags.source_tag_map["t0"].attributes["style"] == "CharacterStyle/MyBold"

    imported.data["Story_u123:p0"].target = "Bonjour le monde"
    p1.target = "C'est du texte en gras"
    p1.tags.target_parts = [
        CodePart("t0"),
        TextPart("C'est du texte en gras"),
        CodePart("t1"),
    ]
    p1.tags.target_tag_map = p1.tags.source_tag_map
    imported.extensions["source_file"] = str(sample_idml_file)

    output_idml = tmp_path / "document_fr.idml"
    export_idml(imported, output_idml, source_idml=sample_idml_file)

    reparsed = import_idml(str(output_idml), source_locale="fr")
    assert reparsed.data["Story_u123:p0"].source == "Bonjour le monde"
    assert reparsed.data["Story_u123:p1"].source == "C'est du texte en gras"


@pytest.mark.asyncio
async def test_idml_roundtrip_async(sample_idml_file: Path, tmp_path: Path) -> None:
    imported_units = {}
    async for unit_id, data in import_idml_async(
        str(sample_idml_file), source_locale="en", target_locale="fr"
    ):
        imported_units[unit_id] = data
    imported = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data=imported_units,
    )
    assert imported.data["Story_u123:p0"].source == "Hello world"

    imported.data["Story_u123:p0"].target = "Bonjour"
    output_idml = tmp_path / "document_fr_async.idml"
    await export_idml_async(imported, output_idml, source_idml=sample_idml_file)

    assert output_idml.exists()
    reparsed_units = {}
    async for unit_id, data in import_idml_async(str(output_idml), source_locale="fr"):
        reparsed_units[unit_id] = data
    assert reparsed_units["Story_u123:p0"].source == "Bonjour"


def test_idml_export_multitarget_directory(sample_idml_file: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "idml"
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        data={
            "Story_u123:p0": Data(
                source="Hello world",
                targets={
                    "fr": TargetData(text="Bonjour le monde"),
                    "de": TargetData(text="Hallo Welt"),
                },
                extensions={"story": "Stories/Story_u123.xml"},
            )
        },
    )

    export_idml(document, output_dir, source_idml=sample_idml_file)

    assert (output_dir / "fr.idml").exists()
    assert (output_dir / "de.idml").exists()
    assert import_idml(str(output_dir / "fr.idml"), source_locale="fr").data["Story_u123:p0"].source == "Bonjour le monde"
