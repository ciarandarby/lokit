from __future__ import annotations

from pathlib import Path

import pytest

from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.exporters.csv import export_csv, export_csv_async
from lokit.importers import import_csv, import_csv_async


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
async def test_csv_roundtrip_async(
    sample_document: BaseStructure, tmp_path: Path
) -> None:
    csv_file = tmp_path / "translations_async.csv"
    await export_csv_async(sample_document, csv_file)
    assert csv_file.exists()
    imported_units = {}
    async for unit_id, data in import_csv_async(
        str(csv_file), source_locale="en-US", target_locale="fr-FR"
    ):
        imported_units[unit_id] = data
    assert imported_units["unit1"].source == "Hello world"
    assert imported_units["unit1"].target == "Bonjour le monde"
