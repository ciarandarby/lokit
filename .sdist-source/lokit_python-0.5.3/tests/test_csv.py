from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from lxml import etree

from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.exporters.csv import export_csv, export_csv_async
from lokit.importers import convert_csv_to_xliff, import_csv, import_csv_async, import_csv_targets

if TYPE_CHECKING:
    from pathlib import Path


def test_csv_roundtrip(sample_document: BaseStructure, tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    export_csv(sample_document, csv_file)

    assert csv_file.exists()
    imported = import_csv(str(csv_file), source_locale="en-US", target_locale="fr-FR")
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
async def test_csv_roundtrip_async(sample_document: BaseStructure, tmp_path: Path) -> None:
    csv_file = tmp_path / "translations_async.csv"
    await export_csv_async(sample_document, csv_file)
    assert csv_file.exists()
    imported_units = {}
    async for unit_id, data in import_csv_async(str(csv_file), source_locale="en-US", target_locale="fr-FR"):
        imported_units[unit_id] = data
    assert imported_units["unit1"].source == "Hello world"
    assert imported_units["unit1"].target == "Bonjour le monde"


def test_csv_import_without_header_uses_first_column(tmp_path: Path) -> None:
    csv_file = tmp_path / "mono.csv"
    csv_file.write_text("Hello\nWorld\n", encoding="utf-8")

    imported = import_csv(str(csv_file), progress=False)

    assert list(imported.data) == ["csv:0", "csv:1"]
    assert imported.data["csv:0"].source == "Hello"
    assert imported.data["csv:0"].target is None
    assert imported.target_locale is None


def test_csv_import_detects_language_headers(tmp_path: Path) -> None:
    csv_file = tmp_path / "translations.csv"
    csv_file.write_text("id,en_US,fr-fr,status\none,Hello,Bonjour,translated\n", encoding="utf-8")

    imported = import_csv(str(csv_file), progress=False)

    assert imported.source_locale == "en-US"
    assert imported.target_locale == "fr-FR"
    assert imported.data["one"].source == "Hello"
    assert imported.data["one"].target == "Bonjour"
    assert imported.data["one"].status == TranslationStatus.TRANSLATED


def test_csv_import_custom_column_selectors(tmp_path: Path) -> None:
    csv_file = tmp_path / "custom.csv"
    csv_file.write_text(
        "key,english,french,note\ngreeting,Hello,Bonjour,Home screen\n",
        encoding="utf-8",
    )
    imported = import_csv(
        str(csv_file),
        source_locale="en-US",
        progress=False,
        header_mode="present",
        source_column="english",
        target_columns={"fr-FR": "french"},
        id_column="key",
        comment_column="note",
    )

    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"
    assert imported.data["greeting"].comments[0].context == "Home screen"


def test_csv_import_spreadsheet_column_letters(tmp_path: Path) -> None:
    csv_file = tmp_path / "letters.csv"
    csv_file.write_text(
        "key,english,french,note\ngreeting,Hello,Bonjour,Home screen\n",
        encoding="utf-8",
    )

    imported = import_csv(
        str(csv_file),
        source_locale="en-US",
        progress=False,
        header_mode="present",
        source_column="B",
        target_columns={"C": "fr_FR"},
        id_column="A",
        comment_column="D",
    )

    assert imported.target_locale == "fr-FR"
    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"
    assert imported.data["greeting"].comments[0].context == "Home screen"


def test_csv_import_single_target_column_letter(tmp_path: Path) -> None:
    csv_file = tmp_path / "letters_single.csv"
    csv_file.write_text("Hello,Bonjour\n", encoding="utf-8")

    imported = import_csv(
        str(csv_file),
        source_locale="en-US",
        target_locale="fr_FR",
        progress=False,
        header_mode="absent",
        source_column="A",
        target_column="B",
    )

    assert imported.target_locale == "fr-FR"
    assert imported.data["csv:0"].source == "Hello"
    assert imported.data["csv:0"].target == "Bonjour"


def test_csv_import_targets_returns_one_structure_per_target(tmp_path: Path) -> None:
    csv_file = tmp_path / "multi.csv"
    csv_file.write_text("id,en,fr,de\none,Hello,Bonjour,Hallo\n", encoding="utf-8")

    imported = import_csv_targets(str(csv_file), progress=False)

    assert set(imported) == {"fr", "de"}
    assert imported["fr"].target_locales == ("fr",)
    assert imported["fr"].target_languages == ("fr",)
    assert imported["fr"].data["one"].target == "Bonjour"
    assert imported["de"].data["one"].target == "Hallo"


def test_csv_import_multiple_targets_by_default(tmp_path: Path) -> None:
    csv_file = tmp_path / "multi.csv"
    csv_file.write_text("id,en,fr,de\none,Hello,Bonjour,Hallo\n", encoding="utf-8")

    imported = import_csv(str(csv_file), progress=False)

    assert imported.source_locale == "en"
    assert imported.target_locale is None
    assert imported.target_locales == ("fr", "de")
    assert imported.data["one"].target is None
    assert imported.data["one"].targets["fr"].text == "Bonjour"
    assert imported.data["one"].targets["de"].text == "Hallo"


def test_csv_export_multitarget_roundtrip(tmp_path: Path) -> None:
    csv_file = tmp_path / "multi.csv"
    roundtrip_file = tmp_path / "roundtrip.csv"
    csv_file.write_text("id,en,fr,de\none,Hello,Bonjour,Hallo\n", encoding="utf-8")

    imported = import_csv(str(csv_file), progress=False)
    export_csv(imported, roundtrip_file, header_style="locale")
    roundtripped = import_csv(str(roundtrip_file), progress=False)

    assert roundtrip_file.read_text(encoding="utf-8").splitlines()[0] == "id,en,fr,de,status,comment"
    assert roundtripped.target_locales == ("fr", "de")
    assert roundtripped.data["one"].targets["fr"].text == "Bonjour"
    assert roundtripped.data["one"].targets["de"].text == "Hallo"


def test_csv_export_locale_headers(sample_document: BaseStructure, tmp_path: Path) -> None:
    csv_file = tmp_path / "locale_headers.csv"

    export_csv(sample_document, csv_file, header_style="locale")

    first_line = csv_file.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "id,en-US,fr-FR,status,comment"


def test_csv_to_xliff_converts_multilingual_targets(tmp_path: Path) -> None:
    csv_file = tmp_path / "multi.csv"
    xliff_file = tmp_path / "multi.xliff"
    csv_file.write_text("id,en,fr,de\none,Hello,Bonjour,Hallo\n", encoding="utf-8")

    convert_csv_to_xliff(str(csv_file), str(xliff_file), progress=False)

    root = etree.parse(str(xliff_file)).getroot()
    ns = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    files = root.findall("x:file", ns)
    assert [file.attrib["target-language"] for file in files] == ["fr", "de"]
    assert [file.attrib["source-language"] for file in files] == ["en", "en"]
    targets = [target.text for target in root.findall(".//x:target", ns)]
    assert targets == ["Bonjour", "Hallo"]
