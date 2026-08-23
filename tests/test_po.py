from __future__ import annotations

from typing import TYPE_CHECKING
from xml.etree import ElementTree

import polib
import pytest

import lokit
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
from lokit.parsers.po.extraction import PoExtractor, PoImportMode

if TYPE_CHECKING:
    from pathlib import Path


PoEntrySignature = tuple[
    str | None,
    str,
    str,
    str,
    tuple[tuple[int, str], ...],
    str,
    str,
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    str | None,
    str | None,
    str | None,
]
PoSignature = tuple[dict[str, str], str, tuple[PoEntrySignature, ...]]


def _po_signature(path: Path) -> PoSignature:
    document = polib.pofile(str(path))
    entries = tuple(
        (
            entry.msgctxt,
            entry.msgid,
            entry.msgid_plural,
            entry.msgstr,
            tuple(sorted(entry.msgstr_plural.items())),
            entry.comment,
            entry.tcomment,
            tuple(entry.occurrences),
            tuple(entry.flags),
            entry.previous_msgctxt,
            entry.previous_msgid,
            entry.previous_msgid_plural,
        )
        for entry in document
        if not entry.obsolete
    )
    return document.metadata, document.header, entries


def _write_structured_po(path: Path) -> None:
    path.write_text(
        '''# Header one
#
# Header three
msgid ""
msgstr ""
"Language: fr\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Plural-Forms: nplurals=2; plural=(n > 1);\\n"

#. Empty translation
msgid "Empty"
msgstr ""

# Translator one
#
# Translator three
#. Extracted one
#.
#. Extracted three
#: src/main.py:7 src/view.py:11
#, fuzzy, python-format
#| msgctxt "old-files"
#| msgid "Old file"
#| msgid_plural "Old files"
msgctxt "files"
msgid "%d file"
msgid_plural "%d files"
msgstr[0] ""
msgstr[1] "%d fichiers"
''',
        encoding="utf-8",
    )


def _assert_empty_translation_not_serialized(path: Path, format_name: str) -> None:
    root = ElementTree.parse(path).getroot()
    unit_name = "tu" if format_name == "tmx" else "trans-unit"
    for unit in root.iter():
        if unit.tag.rsplit("}", maxsplit=1)[-1] != unit_name:
            continue
        values = [descendant.text or "" for descendant in unit.iter()]
        if "Empty" not in values:
            continue
        target_name = "seg" if format_name == "tmx" else "target"
        targets = [
            descendant for descendant in unit.iter() if descendant.tag.rsplit("}", maxsplit=1)[-1] == target_name
        ]
        assert len(targets) == (1 if format_name == "tmx" else 0)
        return
    raise AssertionError("Empty PO entry was not exported")


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

    assert "I have {count} apple[2]" in imported.data
    assert imported.data["I have {count} apple[2]"].target == "J'ai {count} pommes"
    assert imported.data["I have {count} apple[2]"].plural is not None
    assert imported.data["I have {count} apple[2]"].plural.category == PluralCategory.OTHER


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


def test_po_auto_mode_recognizes_uppercase_pot(tmp_path: Path) -> None:
    po_file = tmp_path / "messages.POT"
    po_file.write_text(
        'msgid ""\nmsgstr ""\n\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )

    imported = import_po(str(po_file), source_locale="en", progress=False)

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


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.parametrize("materialized", [False, True])
def test_native_po_structured_roundtrip(
    tmp_path: Path,
    format_name: str,
    materialized: bool,
) -> None:
    source = tmp_path / "fr.po"
    interchange = tmp_path / f"messages.{format_name}"
    output = tmp_path / f"roundtrip-{format_name}.po"
    _write_structured_po(source)

    outbound = (
        lokit.parse.po(str(source), "en", "fr", progress=False)
        if materialized
        else lokit.stream.po(str(source), "en", "fr")
    )
    getattr(outbound.export, format_name)(interchange)
    _assert_empty_translation_not_serialized(interchange, format_name)

    inbound = (
        getattr(lokit.parse, format_name)(str(interchange), progress=True)
        if materialized
        else getattr(lokit.stream, format_name)(str(interchange))
    )
    inbound.export.po(output)
    assert _po_signature(output) == _po_signature(source)


@pytest.mark.asyncio
async def test_native_po_projection_matches_all_public_routes(tmp_path: Path) -> None:
    source = tmp_path / "fr.po"
    _write_structured_po(source)
    expected = dict(PoExtractor(str(source), "en", "fr", PoImportMode.GETTEXT).extract())

    assert import_po(str(source), "en", "fr", progress=True).data == expected
    assert import_po(str(source), "en", "fr", progress=False).data == expected
    assert dict(lokit.stream.po(str(source), "en", "fr").items) == expected

    parsed_async = {unit_id: data async for unit_id, data in import_po_async(str(source), "en", "fr")}
    assert parsed_async == expected

    from lokit.stream import async_ as stream_async

    streamed_async = {unit_id: data async for unit_id, data in stream_async.po(str(source), "en", "fr")}
    assert streamed_async == expected


def test_po_import_mode_aliases_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "fr.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )

    gettext = import_po(source.as_posix(), "en", "fr", mode=PoImportMode.MSGID_AS_SOURCE, progress=False)
    identifier = import_po(source.as_posix(), "fr", mode=PoImportMode.MSGID_AS_ID, progress=False)
    assert gettext.data["Hello"].source == "Hello"
    assert gettext.data["Hello"].target == "Bonjour"
    assert identifier.data["Hello"].source == "Bonjour"
    assert identifier.data["Hello"].target is None


def test_native_po_duplicate_ids_are_collision_safe(tmp_path: Path) -> None:
    source = tmp_path / "duplicates.po"
    source.write_text(
        'msgid "Same"\nmsgstr "One"\n\nmsgid "Same"\nmsgstr "Two"\n',
        encoding="utf-8",
    )
    document = lokit.stream.po(str(source), "en", "fr")
    assert list(dict(document.items)) == ["Same", "Same#2"]


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
def test_native_po_export_is_atomic_on_late_xml_error(tmp_path: Path, format_name: str) -> None:
    source = tmp_path / "invalid.po"
    output = tmp_path / f"messages.{format_name}"
    source.write_text(
        'msgid "Good"\nmsgstr "Bon"\n\nmsgid "Bad\\004value"\nmsgstr ""\n',
        encoding="utf-8",
    )
    output.write_text("sentinel", encoding="utf-8")

    with pytest.raises(ValueError, match=r"XML 1\.0"):
        getattr(lokit.stream.po(str(source), "en", "fr").export, format_name)(output)
    assert output.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("materialized", [False, True])
def test_translate_style_xliff_to_po_skips_header_and_bounds_plurals(
    tmp_path: Path,
    materialized: bool,
) -> None:
    source = tmp_path / "translate.xliff"
    output = tmp_path / "messages.po"
    source.write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.1" version="1.1">
  <file original="messages.po" source-language="en" datatype="plaintext">
    <body>
      <trans-unit id="1" restype="x-gettext-domain-header">
        <source>Language: ja
Plural-Forms: nplurals=1; plural=0;
</source>
        <target>Language: ja
Plural-Forms: nplurals=1; plural=0;
</target>
      </trans-unit>
      <trans-unit id="2"><source>Privacy Policy</source><target/></trans-unit>
      <group id="files" restype="x-gettext-plurals">
        <trans-unit id="files[0]"><source>%d file</source><target>%d other</target></trans-unit>
        <trans-unit id="files[1]"><source>%d files</source><target/></trans-unit>
      </group>
    </body>
  </file>
</xliff>
''',
        encoding="utf-8",
    )

    document = (
        lokit.parse.xliff(str(source), progress=True) if materialized else lokit.stream.xliff(str(source))
    )
    document.export.po(output)
    parsed = polib.pofile(str(output))
    assert [entry.msgid for entry in parsed] == ["Privacy Policy", "%d file"]
    assert parsed[1].msgid_plural == "%d files"
    assert parsed[1].msgstr_plural == {0: "%d other"}
