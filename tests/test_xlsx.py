from __future__ import annotations

from pathlib import Path

from lxml import etree
import pytest

from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.exporters.xlsx import export_xlsx, export_xlsx_async
from lokit.importers import convert_xlsx_to_xliff, import_xlsx, import_xlsx_async, import_xlsx_targets


def test_xlsx_roundtrip(sample_document: BaseStructure, tmp_path: Path) -> None:
    xlsx_file = tmp_path / "translations.xlsx"
    export_xlsx(sample_document, xlsx_file)

    assert xlsx_file.exists()
    imported = import_xlsx(str(xlsx_file), source_locale="en-US", target_locale="fr-FR")

    assert imported.source_locale == "en-US"
    assert imported.target_locale == "fr-FR"
    assert "unit1" in imported.data
    assert imported.data["unit1"].source == "Hello world"
    assert imported.data["unit1"].target == "Bonjour le monde"
    assert imported.data["unit1"].status == TranslationStatus.TRANSLATED
    assert len(imported.data["unit1"].comments) == 1
    assert imported.data["unit1"].comments[0].context == "Standard greeting"

    assert "unit2" in imported.data
    assert imported.data["unit2"].status == TranslationStatus.APPROVED

    assert "unit3" in imported.data
    assert imported.data["unit3"].target is None
    assert imported.data["unit3"].status == TranslationStatus.NEW


@pytest.mark.asyncio
async def test_xlsx_roundtrip_async(
    sample_document: BaseStructure, tmp_path: Path
) -> None:
    xlsx_file = tmp_path / "translations_async.xlsx"
    await export_xlsx_async(sample_document, xlsx_file)

    assert xlsx_file.exists()
    imported_units = {}
    async for unit_id, data in import_xlsx_async(
        str(xlsx_file), source_locale="en-US", target_locale="fr-FR"
    ):
        imported_units[unit_id] = data

    assert imported_units["unit1"].source == "Hello world"
    assert imported_units["unit1"].target == "Bonjour le monde"


def test_xlsx_import_detects_locale_headers(
    sample_document: BaseStructure,
    tmp_path: Path,
) -> None:
    xlsx_file = tmp_path / "locale_headers.xlsx"
    export_xlsx(sample_document, xlsx_file, header_style="locale")

    imported = import_xlsx(str(xlsx_file), progress=False)

    assert imported.source_locale == "en-US"
    assert imported.target_locale == "fr-FR"
    assert imported.data["unit1"].source == "Hello world"
    assert imported.data["unit1"].target == "Bonjour le monde"


def test_xlsx_import_targets(sample_document: BaseStructure, tmp_path: Path) -> None:
    xlsx_file = tmp_path / "locale_headers.xlsx"
    export_xlsx(sample_document, xlsx_file, header_style="locale")

    imported = import_xlsx_targets(str(xlsx_file), progress=False)

    assert set(imported) == {"fr-FR"}
    assert imported["fr-FR"].data["unit1"].target == "Bonjour le monde"


def test_xlsx_import_multiple_targets_by_default(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "multi.xlsx"

    from rustpy_xlsxwriter import FastExcel

    FastExcel(str(xlsx_file), autofit=False).sheet(
        "Sheet1",
        [{"id": "one", "en": "Hello", "fr": "Bonjour", "de": "Hallo"}],
    ).save()

    imported = import_xlsx(str(xlsx_file), progress=False)

    assert imported.source_locale == "en"
    assert imported.target_locale is None
    assert imported.target_locales == ("fr", "de")
    assert imported.data["one"].target is None
    assert imported.data["one"].targets["fr"].text == "Bonjour"
    assert imported.data["one"].targets["de"].text == "Hallo"


def test_xlsx_export_multitarget_roundtrip(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "multi.xlsx"
    roundtrip_file = tmp_path / "roundtrip.xlsx"

    from rustpy_xlsxwriter import FastExcel

    FastExcel(str(xlsx_file), autofit=False).sheet(
        "Sheet1",
        [{"id": "one", "en": "Hello", "fr": "Bonjour", "de": "Hallo"}],
    ).save()

    imported = import_xlsx(str(xlsx_file), progress=False)
    export_xlsx(imported, roundtrip_file, header_style="locale")
    roundtripped = import_xlsx(str(roundtrip_file), progress=False)

    assert roundtripped.target_locales == ("fr", "de")
    assert roundtripped.data["one"].targets["fr"].text == "Bonjour"
    assert roundtripped.data["one"].targets["de"].text == "Hallo"


def test_xlsx_to_xliff_converts_multilingual_targets(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "multi.xlsx"
    xliff_file = tmp_path / "multi.xliff"

    from rustpy_xlsxwriter import FastExcel

    FastExcel(str(xlsx_file), autofit=False).sheet(
        "Sheet1",
        [{"id": "one", "en": "Hello", "fr": "Bonjour", "de": "Hallo"}],
    ).save()

    convert_xlsx_to_xliff(str(xlsx_file), str(xliff_file), progress=False)

    root = etree.parse(str(xliff_file)).getroot()
    ns = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    files = root.findall("x:file", ns)
    assert [file.attrib["target-language"] for file in files] == ["fr", "de"]
    assert [target.text for target in root.findall(".//x:target", ns)] == ["Bonjour", "Hallo"]
