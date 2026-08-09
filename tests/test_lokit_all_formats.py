from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Literal

import pytest

import lokit
from lokit.data.structure import BaseStructure, Data, TranslationStatus

if TYPE_CHECKING:
    from pathlib import Path

SimpleFormat = Literal["tmx", "xliff", "csv", "xlsx", "po"]


def _simple_document() -> BaseStructure:
    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={
            "greeting": Data(
                source="Hello world",
                target="Bonjour le monde",
                status=TranslationStatus.TRANSLATED,
            )
        },
        target_locales=("fr-FR",),
        source_language="en",
        target_language="fr",
        target_languages=("fr",),
    )


def _through_lokit(document: BaseStructure, path: Path) -> BaseStructure:
    lokit.write.lokit(document, path)
    reparsed = lokit.parse.lokit(str(path))
    assert reparsed == document
    return reparsed


def _write_simple_source(format_name: SimpleFormat, document: BaseStructure, path: Path) -> None:
    if format_name == "tmx":
        lokit.write.tmx(document, path)
    elif format_name == "xliff":
        lokit.write.xliff(document, path)
    elif format_name == "csv":
        lokit.write.csv(document, path)
    elif format_name == "xlsx":
        lokit.write.xlsx(document, path)
    else:
        lokit.write.po(document, path)


def _parse_simple(format_name: SimpleFormat, path: Path) -> BaseStructure:
    if format_name == "tmx":
        return lokit.parse.tmx(str(path), "en-US", "fr-FR", progress=False)
    if format_name == "xliff":
        return lokit.parse.xliff(str(path), progress=False)
    if format_name == "csv":
        return lokit.parse.csv(str(path), "en-US", "fr-FR", progress=False)
    if format_name == "xlsx":
        return lokit.parse.xlsx(str(path), "en-US", "fr-FR", progress=False)
    return lokit.parse.po(str(path), "en-US", "fr-FR", progress=False)


@pytest.mark.parametrize(
    ("format_name", "suffix"),
    [
        ("tmx", ".tmx"),
        ("xliff", ".xliff"),
        ("csv", ".csv"),
        ("xlsx", ".xlsx"),
        ("po", ".po"),
    ],
)
def test_lokit_is_lossless_intermediate_for_simple_formats(
    tmp_path: Path,
    format_name: SimpleFormat,
    suffix: str,
) -> None:
    source = tmp_path / f"source{suffix}"
    output = tmp_path / f"output{suffix}"
    _write_simple_source(format_name, _simple_document(), source)
    parsed_source = _parse_simple(format_name, source)

    intermediate = _through_lokit(parsed_source, tmp_path / f"{format_name}.lokit")
    _write_simple_source(format_name, intermediate, output)
    reparsed_output = _parse_simple(format_name, output)

    assert list(reparsed_output.data) == list(parsed_source.data)
    assert reparsed_output.data["greeting"].source == parsed_source.data["greeting"].source
    assert reparsed_output.data["greeting"].target == parsed_source.data["greeting"].target


def test_lokit_is_lossless_intermediate_for_json_i18n(tmp_path: Path) -> None:
    source = tmp_path / "en.json"
    target = tmp_path / "fr.json"
    source.write_text(json.dumps({"home": {"title": "Hello"}}), encoding="utf-8")
    target.write_text(json.dumps({"home": {"title": "Bonjour"}}), encoding="utf-8")
    parsed_source = lokit.parse.json_i18n(
        str(source),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target),
        progress=False,
    )

    intermediate = _through_lokit(parsed_source, tmp_path / "json-i18n.lokit")
    output = tmp_path / "fr-output.json"
    lokit.write.json_i18n(intermediate, output)
    reparsed_output = lokit.parse.json_i18n(str(output), source_locale="fr", progress=False)

    assert reparsed_output.data["home.title"].source == "Bonjour"


def test_lokit_is_lossless_intermediate_for_lokit_json(tmp_path: Path) -> None:
    source_document = _simple_document()
    source_document.extensions["full-model"] = "true"
    source = tmp_path / "source.json"
    lokit.Lokit.from_document(source_document).output(source)
    parsed_source = lokit.Lokit.parse(source).document

    intermediate = _through_lokit(parsed_source, tmp_path / "lokit-json.lokit")
    output = tmp_path / "output.json"
    lokit.Lokit.from_document(intermediate).output(output)
    reparsed_output = lokit.Lokit.parse(output).document

    assert reparsed_output == parsed_source


def test_lokit_is_lossless_intermediate_for_html(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    source.write_text(
        "<!doctype html><html><head><title>Hello title</title></head>"
        "<body><p>This is <strong>important</strong>.</p></body></html>",
        encoding="utf-8",
    )
    parsed_source = lokit.parse.html(str(source), source_locale="en", target_locale="fr", progress=False)
    parsed_source.data["html:title:0"].target = "Titre bonjour"
    parsed_source.data["html:p:1"].target = "Ceci est important."

    intermediate = _through_lokit(parsed_source, tmp_path / "html.lokit")
    output = tmp_path / "output.html"
    lokit.write.html(intermediate, output, source)
    reparsed_output = lokit.parse.html(str(output), source_locale="fr", progress=False)

    assert reparsed_output.data["html:title:0"].source == "Titre bonjour"
    assert reparsed_output.data["html:p:1"].source == "Ceci est important."


def test_lokit_is_lossless_intermediate_for_idml(tmp_path: Path) -> None:
    source = tmp_path / "source.idml"
    _write_minimal_idml(source)
    parsed_source = lokit.parse.idml(str(source), source_locale="en", target_locale="fr", progress=False)
    parsed_source.data["Story_u123:p0"].target = "Bonjour IDML"

    intermediate = _through_lokit(parsed_source, tmp_path / "idml.lokit")
    output = tmp_path / "output.idml"
    lokit.write.idml(intermediate, output, source)
    reparsed_output = lokit.parse.idml(str(output), source_locale="fr", progress=False)

    assert reparsed_output.data["Story_u123:p0"].source == "Bonjour IDML"


def test_lokit_is_lossless_intermediate_for_docx(tmp_path: Path) -> None:
    source = tmp_path / "source.docx"
    _write_minimal_docx(source)
    parsed_source = lokit.parse.docx(source, source_locale="en", target_locale="fr", progress=False)
    for index, unit in enumerate(parsed_source.data.values()):
        unit.target = f"DOCX traduit {index}"

    intermediate = _through_lokit(parsed_source, tmp_path / "docx.lokit")
    output = tmp_path / "output.docx"
    lokit.write.docx(intermediate, output, source_docx=source)
    reparsed_output = lokit.parse.docx(output, source_locale="fr", progress=False)

    assert [unit.source for unit in reparsed_output.data.values()] == ["DOCX traduit 0", "DOCX traduit 1"]


def test_lokit_is_lossless_intermediate_for_pptx(tmp_path: Path) -> None:
    source = tmp_path / "source.pptx"
    _write_minimal_pptx(source)
    parsed_source = lokit.parse.pptx(source, source_locale="en", target_locale="fr", progress=False)
    for index, unit in enumerate(parsed_source.data.values()):
        unit.target = f"PPTX traduit {index}"

    intermediate = _through_lokit(parsed_source, tmp_path / "pptx.lokit")
    output = tmp_path / "output.pptx"
    lokit.write.pptx(intermediate, output, source_pptx=source)
    reparsed_output = lokit.parse.pptx(output, source_locale="fr", progress=False)

    assert [unit.source for unit in reparsed_output.data.values()] == ["PPTX traduit 0", "PPTX traduit 1"]


def _write_minimal_idml(path: Path) -> None:
    story = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Story xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idms/1.0/">\n'
        "  <ParagraphStyleRange><CharacterStyleRange>"
        "<Content>Hello IDML</Content>"
        "</CharacterStyleRange></ParagraphStyleRange>\n"
        "</Story>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Stories/Story_u123.xml", story)


def _write_minimal_docx(path: Path) -> None:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        "</Types>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def _write_minimal_pptx(path: Path) -> None:
    presentation = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></p:sldIdLst>
</p:presentation>
"""
    slide = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Hello PPTX</a:t></a:r></a:p></p:txBody></p:sp>
    <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Second slide text</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld>
</p:sld>
"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>\n'
        '  <Override PartName="/ppt/slides/slide1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>\n'
        "</Types>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/slides/slide1.xml", slide)
