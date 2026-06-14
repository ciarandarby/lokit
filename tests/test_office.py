from __future__ import annotations

import time
from pathlib import Path

import pytest

import lokit
from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes

DOCX_FIXTURE = Path("test_data/docx/Teleported Driving Hazard Report.docx")
PPTX_FIXTURE = Path("test_data/pptx/000528_workplan_timeline_powerpoint_template.pptx")


def test_office_format_detection() -> None:
    assert detect_format(DOCX_FIXTURE) == LokitInputFormat.DOCX
    assert detect_format(PPTX_FIXTURE) == LokitInputFormat.PPTX
    assert detect_format_from_bytes(DOCX_FIXTURE.read_bytes()) == LokitInputFormat.DOCX
    assert detect_format_from_bytes(PPTX_FIXTURE.read_bytes()) == LokitInputFormat.PPTX


def test_docx_import_and_roundtrip_export(tmp_path: Path) -> None:
    document = lokit.import_docx(DOCX_FIXTURE, source_locale="en", target_locale="fr", progress=False)
    from_bytes = lokit.import_docx(DOCX_FIXTURE.read_bytes(), source_locale="en", progress=False)

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "docx"
    assert len(document.data) >= 100
    assert len(from_bytes.data) == len(document.data)
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("docx:body:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "word/document.xml"

    document.data[first_unit_id].target = "Titre traduit DOCX"
    output = tmp_path / "translated.docx"
    result = lokit.export_docx(document, output, source_docx=DOCX_FIXTURE)

    assert result.units_written == 1
    reparsed = lokit.import_docx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit DOCX"


def test_pptx_import_and_roundtrip_export(tmp_path: Path) -> None:
    document = lokit.parsers.read.pptx(PPTX_FIXTURE, source_locale="en", target_locale="fr")

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "pptx"
    assert len(document.data) >= 20
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("pptx:slide/1:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "ppt/slides/slide1.xml"

    document.data[first_unit_id].target = "Titre traduit PPTX"
    output = tmp_path / "translated.pptx"
    lokit.exporters.write.pptx(document, output, source_pptx=PPTX_FIXTURE)

    reparsed = lokit.import_pptx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit PPTX"


@pytest.mark.asyncio
async def test_office_async_imports() -> None:
    docx_items = [
        item
        async for item in lokit.import_docx_async(
            DOCX_FIXTURE,
            source_locale="en",
            target_locale="fr",
        )
    ]
    pptx_items = [
        item
        async for item in lokit.parsers.async_.pptx(
            PPTX_FIXTURE,
            source_locale="en",
            target_locale="fr",
        )
    ]

    assert docx_items
    assert pptx_items
    assert docx_items[0][0].startswith("docx:")
    assert pptx_items[0][0].startswith("pptx:")


def test_office_fixture_parse_performance() -> None:
    started = time.perf_counter()
    docx = lokit.import_docx(DOCX_FIXTURE, progress=False)
    pptx = lokit.import_pptx(PPTX_FIXTURE, progress=False)
    elapsed = time.perf_counter() - started

    assert len(docx.data) >= 100
    assert len(pptx.data) >= 20
    assert elapsed < 5.0
