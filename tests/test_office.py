from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

import lokit
from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes

DOCX_FIXTURE = Path("test_data/docx/Teleported Driving Hazard Report.docx")
PPTX_FIXTURE = Path("test_data/pptx/000528_workplan_timeline_powerpoint_template.pptx")


@pytest.fixture
def docx_fixture(tmp_path: Path) -> Path:
    if DOCX_FIXTURE.exists():
        return DOCX_FIXTURE
    path = tmp_path / "minimal.docx"
    _write_minimal_docx(path)
    return path


@pytest.fixture
def pptx_fixture(tmp_path: Path) -> Path:
    if PPTX_FIXTURE.exists():
        return PPTX_FIXTURE
    path = tmp_path / "minimal.pptx"
    _write_minimal_pptx(path)
    return path


def test_office_format_detection(docx_fixture: Path, pptx_fixture: Path) -> None:
    assert detect_format(docx_fixture) == LokitInputFormat.DOCX
    assert detect_format(pptx_fixture) == LokitInputFormat.PPTX
    assert detect_format_from_bytes(docx_fixture.read_bytes()) == LokitInputFormat.DOCX
    assert detect_format_from_bytes(pptx_fixture.read_bytes()) == LokitInputFormat.PPTX


def test_docx_import_and_roundtrip_export(docx_fixture: Path, tmp_path: Path) -> None:
    document = lokit.parse.docx(docx_fixture, source_locale="en", target_locale="fr", progress=False)
    from_bytes = lokit.parse.docx(docx_fixture.read_bytes(), source_locale="en", progress=False)

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "docx"
    assert len(document.data) >= 1
    assert len(from_bytes.data) == len(document.data)
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("docx:body:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "word/document.xml"

    document.data[first_unit_id].target = "Titre traduit DOCX"
    output = tmp_path / "translated.docx"
    result = lokit.office.export_docx(document, output, source_docx=docx_fixture)

    assert result.units_written == 1
    reparsed = lokit.parse.docx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit DOCX"


def test_pptx_import_and_roundtrip_export(pptx_fixture: Path, tmp_path: Path) -> None:
    document = lokit.parse.pptx(pptx_fixture, source_locale="en", target_locale="fr")

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "pptx"
    assert len(document.data) >= 1
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("pptx:slide/1:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "ppt/slides/slide1.xml"

    document.data[first_unit_id].target = "Titre traduit PPTX"
    output = tmp_path / "translated.pptx"
    lokit.parse.write.pptx(document, output, source_pptx=pptx_fixture)

    reparsed = lokit.parse.pptx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit PPTX"


@pytest.mark.asyncio
async def test_office_async_imports(docx_fixture: Path, pptx_fixture: Path) -> None:
    docx_items = [
        item
        async for item in lokit.parse.async_.docx(
            docx_fixture,
            source_locale="en",
            target_locale="fr",
        )
    ]
    pptx_items = [
        item
        async for item in lokit.parse.async_.pptx(
            pptx_fixture,
            source_locale="en",
            target_locale="fr",
        )
    ]

    assert docx_items
    assert pptx_items
    assert docx_items[0][0].startswith("docx:")
    assert pptx_items[0][0].startswith("pptx:")


def test_office_fixture_parse_performance(docx_fixture: Path, pptx_fixture: Path) -> None:
    started = time.perf_counter()
    docx = lokit.parse.docx(docx_fixture, progress=False)
    pptx = lokit.parse.pptx(pptx_fixture, progress=False)
    elapsed = time.perf_counter() - started

    assert len(docx.data) >= 1
    assert len(pptx.data) >= 1
    assert elapsed < 5.0


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
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)


def _write_minimal_pptx(path: Path) -> None:
    presentation = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></p:sldIdLst>
</p:presentation>
"""
    slide = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Hello PPTX</a:t></a:r></a:p></p:txBody></p:sp>
      <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Second slide text</a:t></a:r></a:p></p:txBody></p:sp>
    </p:spTree>
  </p:cSld>
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
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/slides/slide1.xml", slide)
