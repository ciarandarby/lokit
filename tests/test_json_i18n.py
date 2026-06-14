from __future__ import annotations

import json
from pathlib import Path

import pytest

from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.exporters.json_i18n import export_json_i18n, export_json_i18n_async
from lokit.importers import import_json_i18n, import_json_i18n_async


def test_json_i18n_flat_roundtrip(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"

    source_data = {
        "greeting": "Hello",
        "nested.key": "This is nested key",
    }
    target_data = {
        "greeting": "Bonjour",
        "nested.key": "C'est une clé imbriquée",
    }

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")
    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    )

    assert imported.source_locale == "en"
    assert imported.target_locale == "fr"

    assert "greeting" in imported.data
    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"
    assert imported.data["greeting"].status == TranslationStatus.TRANSLATED

    assert "nested.key" in imported.data
    assert imported.data["nested.key"].source == "This is nested key"
    assert imported.data["nested.key"].target == "C'est une clé imbriquée"

    exported_flat = tmp_path / "fr_flat.json"
    export_json_i18n(imported, exported_flat, nested=False)

    with exported_flat.open("r", encoding="utf-8") as f:
        exported_data = json.load(f)

    assert exported_data == target_data


def test_json_i18n_nested_roundtrip(tmp_path: Path) -> None:
    source_file = tmp_path / "en_nested.json"
    target_file = tmp_path / "fr_nested.json"

    source_data = {
        "common": {
            "greeting": "Hello",
            "button": {
                "save": "Save",
            },
        }
    }
    target_data = {
        "common": {
            "greeting": "Bonjour",
            "button": {
                "save": "Sauvegarder",
            },
        }
    }

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")
    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    )

    assert "common.greeting" in imported.data
    assert imported.data["common.greeting"].source == "Hello"
    assert imported.data["common.greeting"].target == "Bonjour"

    assert "common.button.save" in imported.data
    assert imported.data["common.button.save"].source == "Save"
    assert imported.data["common.button.save"].target == "Sauvegarder"
    exported_nested = tmp_path / "fr_nested_exported.json"
    export_json_i18n(imported, exported_nested, nested=True)

    with exported_nested.open("r", encoding="utf-8") as f:
        exported_data = json.load(f)

    assert exported_data == target_data


def test_json_i18n_import_multiple_target_files(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    fr_file = tmp_path / "fr.json"
    de_file = tmp_path / "de.json"
    source_file.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")
    fr_file.write_text(json.dumps({"greeting": "Bonjour"}), encoding="utf-8")
    de_file.write_text(json.dumps({"greeting": "Hallo"}), encoding="utf-8")

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_filepaths={"fr": str(fr_file), "de": str(de_file)},
        progress=False,
    )

    assert imported.target_locale is None
    assert imported.target_locales == ("fr", "de")
    assert imported.data["greeting"].target is None
    assert imported.data["greeting"].targets["fr"].text == "Bonjour"
    assert imported.data["greeting"].targets["de"].text == "Hallo"


def test_json_i18n_import_multilingual_root(tmp_path: Path) -> None:
    multilingual_file = tmp_path / "messages.json"
    multilingual_file.write_text(
        json.dumps(
            {
                "en": {"greeting": "Hello"},
                "fr": {"greeting": "Bonjour"},
                "de": {"greeting": "Hallo"},
            }
        ),
        encoding="utf-8",
    )

    imported = import_json_i18n(str(multilingual_file), source_locale="en", progress=False)

    assert imported.source_locale == "en"
    assert imported.target_locales == ("fr", "de")
    assert imported.data["greeting"].targets["fr"].text == "Bonjour"
    assert imported.data["greeting"].targets["de"].text == "Hallo"


def test_json_i18n_export_multitarget_directory(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    fr_file = tmp_path / "fr.json"
    de_file = tmp_path / "de.json"
    output_dir = tmp_path / "targets"
    source_file.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")
    fr_file.write_text(json.dumps({"greeting": "Bonjour"}), encoding="utf-8")
    de_file.write_text(json.dumps({"greeting": "Hallo"}), encoding="utf-8")

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_filepaths={"fr": str(fr_file), "de": str(de_file)},
        progress=False,
    )
    export_json_i18n(imported, output_dir)

    assert json.loads((output_dir / "fr.json").read_text(encoding="utf-8")) == {"greeting": "Bonjour"}
    assert json.loads((output_dir / "de.json").read_text(encoding="utf-8")) == {"greeting": "Hallo"}


def test_json_i18n_flat_and_nested_key_collision_export(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"
    exported = tmp_path / "fr_exported.json"
    source_file.write_text(
        json.dumps({"a.b": "Flat", "a": {"b": "Nested"}}),
        encoding="utf-8",
    )
    target_file.write_text(
        json.dumps({"a.b": "Plat", "a": {"b": "Imbrique"}}),
        encoding="utf-8",
    )

    imported = import_json_i18n(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
        progress=False,
    )
    export_json_i18n(imported, exported, nested=True)

    assert json.loads(exported.read_text(encoding="utf-8")) == {
        "a.b": "Plat",
        "a": {"b": "Imbrique"},
    }


@pytest.mark.asyncio
async def test_json_i18n_async(tmp_path: Path) -> None:
    source_file = tmp_path / "en.json"
    target_file = tmp_path / "fr.json"

    source_data = {"greeting": "Hello"}
    target_data = {"greeting": "Bonjour"}

    source_file.write_text(json.dumps(source_data), encoding="utf-8")
    target_file.write_text(json.dumps(target_data), encoding="utf-8")

    imported_units = {}
    async for unit_id, data in import_json_i18n_async(
        str(source_file),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target_file),
    ):
        imported_units[unit_id] = data
    imported = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data=imported_units,
        extensions={"input_format": "json_i18n"},
    )
    assert imported.data["greeting"].source == "Hello"
    assert imported.data["greeting"].target == "Bonjour"

    exported = tmp_path / "fr_exported.json"
    await export_json_i18n_async(imported, exported, nested=False)
    assert exported.exists()
