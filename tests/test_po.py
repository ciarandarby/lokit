from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import (
    BaseStructure,
    Comment,
    Data,
    Plural,
    PluralCategory,
    TranslationStatus,
)
from lokit.exporters.po import export_po, export_po_async
from lokit.importers import import_po, import_po_async, import_po_targets

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def po_sample_document() -> BaseStructure:
    data = {
        "Hello world": Data(
            source="Hello world",
            target="Bonjour le monde",
            status=TranslationStatus.TRANSLATED,
            comments=[Comment(context="Standard greeting")],
        ),
        "Singular source": Data(
            source="Singular source",
            target="Singular target",
            status=TranslationStatus.APPROVED,
        ),
        "Untranslated source": Data(
            source="Untranslated source",
            target=None,
            status=TranslationStatus.NEW,
        ),
        "I have {count} apple": Data(
            source="I have {count} apple",
            target="J'ai {count} pomme",
            plural=Plural(variant="I have {count} apples"),
            status=TranslationStatus.TRANSLATED,
        ),
        "I have {count} apple[1]": Data(
            source="I have {count} apple",
            target="J'ai {count} pommes",
            plural=Plural(
                variant="I have {count} apples",
                category=PluralCategory.OTHER,
            ),
            status=TranslationStatus.TRANSLATED,
        ),
    }

    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data=data,
        source_language="en",
        target_language="fr",
        export_origin="lokit-test",
    )


def test_po_roundtrip(po_sample_document: BaseStructure, tmp_path: Path) -> None:
    po_file = tmp_path / "translations.po"
    export_po(po_sample_document, po_file)

    assert po_file.exists()

    imported = import_po(str(po_file), source_locale="en-US", target_locale="fr-FR")

    assert imported.source_locale == "en-US"
    assert imported.target_locale == "fr-FR"
    assert "Hello world" in imported.data
    assert imported.data["Hello world"].source == "Hello world"
    assert imported.data["Hello world"].target == "Bonjour le monde"
    assert imported.data["Hello world"].status == TranslationStatus.TRANSLATED
    assert len(imported.data["Hello world"].comments) == 1
    assert imported.data["Hello world"].comments[0].context == "Standard greeting"

    assert "Singular source" in imported.data
    assert imported.data["Singular source"].source == "Singular source"
    assert imported.data["Singular source"].target == "Singular target"
    assert imported.data["Singular source"].status == TranslationStatus.TRANSLATED
    assert "I have {count} apple" in imported.data
    assert imported.data["I have {count} apple"].source == "I have {count} apple"
    assert imported.data["I have {count} apple"].target == "J'ai {count} pomme"
    assert imported.data["I have {count} apple"].plural is not None
    assert imported.data["I have {count} apple"].plural.variant == "I have {count} apples"

    assert "I have {count} apple[1]" in imported.data
    assert imported.data["I have {count} apple[1]"].target == "J'ai {count} pommes"
    assert imported.data["I have {count} apple[1]"].plural is not None
    assert imported.data["I have {count} apple[1]"].plural.category == PluralCategory.TWO


@pytest.mark.asyncio
async def test_po_roundtrip_async(po_sample_document: BaseStructure, tmp_path: Path) -> None:
    po_file = tmp_path / "translations_async.po"
    await export_po_async(po_sample_document, po_file)

    assert po_file.exists()

    imported_units = {}
    async for unit_id, data in import_po_async(str(po_file), source_locale="en-US", target_locale="fr-FR"):
        imported_units[unit_id] = data

    assert imported_units["Hello world"].source == "Hello world"
    assert imported_units["Hello world"].target == "Bonjour le monde"


def test_po_source_mode_has_no_target(tmp_path: Path) -> None:
    po_file = tmp_path / "messages.pot"
    po_file.write_text(
        'msgid ""\nmsgstr ""\n\nmsgid "Hello"\nmsgstr ""\n',
        encoding="utf-8",
    )

    imported = import_po(str(po_file), source_locale="en", mode="source", progress=False)

    assert imported.target_locale is None
    assert imported.data["Hello"].source == "Hello"
    assert imported.data["Hello"].target is None


def test_po_target_as_source_mode(tmp_path: Path) -> None:
    po_file = tmp_path / "fr.po"
    po_file.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )

    imported = import_po(str(po_file), source_locale="fr", mode="target_as_source", progress=False)

    assert imported.data["Hello"].source == "Bonjour"
    assert imported.data["Hello"].target is None


def test_po_import_targets_merges_locales(tmp_path: Path) -> None:
    pot_file = tmp_path / "messages.pot"
    fr_file = tmp_path / "fr.po"
    de_file = tmp_path / "de.po"
    pot_file.write_text(
        'msgid ""\nmsgstr ""\n\nmsgid "Hello"\nmsgstr ""\n',
        encoding="utf-8",
    )
    fr_file.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )
    de_file.write_text(
        'msgid ""\nmsgstr ""\n"Language: de\\n"\n\nmsgid "Hello"\nmsgstr "Hallo"\n',
        encoding="utf-8",
    )

    imported = import_po_targets(
        str(pot_file),
        {"fr": str(fr_file), "de": str(de_file)},
        source_locale="en",
        progress=False,
    )

    assert imported.target_locales == ("fr", "de")
    assert imported.data["Hello"].target is None
    assert imported.data["Hello"].targets["fr"].text == "Bonjour"
    assert imported.data["Hello"].targets["de"].text == "Hallo"


def test_po_export_multitarget_directory(tmp_path: Path) -> None:
    pot_file = tmp_path / "messages.pot"
    fr_file = tmp_path / "fr.po"
    de_file = tmp_path / "de.po"
    output_dir = tmp_path / "po"
    pot_file.write_text(
        'msgid ""\nmsgstr ""\n\nmsgid "Hello"\nmsgstr ""\n',
        encoding="utf-8",
    )
    fr_file.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )
    de_file.write_text(
        'msgid ""\nmsgstr ""\n"Language: de\\n"\n\nmsgid "Hello"\nmsgstr "Hallo"\n',
        encoding="utf-8",
    )

    imported = import_po_targets(
        str(pot_file),
        {"fr": str(fr_file), "de": str(de_file)},
        source_locale="en",
        progress=False,
    )
    export_po(imported, output_dir)

    assert 'msgstr "Bonjour"' in (output_dir / "fr.po").read_text(encoding="utf-8")
    assert 'msgstr "Hallo"' in (output_dir / "de.po").read_text(encoding="utf-8")
