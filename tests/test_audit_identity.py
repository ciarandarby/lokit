from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from lxml.etree import _Element

import pytest
from lxml import etree

from lokit.data.structure import StreamingStructure
from lokit.exporters.xliff import export_xliff
from lokit.exporters.xlsx import export_xlsx
from lokit.importers import import_csv, import_csv_targets, import_json_i18n, import_tmx, import_xliff, import_xlsx


@pytest.mark.parametrize("multilingual", [False, True])
@pytest.mark.parametrize("progress", [False, True])
def test_structural_json_identity(tmp_path: Path, multilingual: bool, progress: bool) -> None:
    source_values = {"a.b": "literal", "a": {"b": "nested"}, "a.b#2": "suffix", "missing": "missing"}
    target_values = {"a": {"b": "imbriqué"}, "a.b#2": "suffixe", "a.b": "littéral", "extra": "extra"}
    source = tmp_path / "en.json"
    target = tmp_path / "fr.json"
    target.write_text(json.dumps(target_values), encoding="utf-8")
    source.write_text(
        json.dumps({"en": source_values, "fr": target_values} if multilingual else source_values), encoding="utf-8"
    )
    document = import_json_i18n(
        str(source),
        source_locale="en",
        target_locale="fr",
        target_filepath=None if multilingual else str(target),
        progress=progress,
    )
    assert [(unit.source, unit.target) for unit in document.data.values()] == [
        ("literal", "littéral"),
        ("nested", "imbriqué"),
        ("suffix", "suffixe"),
        ("missing", None),
    ]
    assert len(document.data) == 4


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("format_name", ["csv", "xlsx"])
def test_tabular_duplicates_and_regeneration(tmp_path: Path, progress: bool, format_name: str) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "id,en,fr,status,comment\nsame,First,Premier,approved,first note\n"
        "same,Second,Deuxieme,new,second note\nsame#2,Third,Troisieme,translated,third note\n"
        f",Fourth,Quatrieme,new,fourth note\n{format_name}:3,Fifth,Cinquieme,new,fifth note\n",
        encoding="utf-8",
    )
    if format_name == "xlsx":
        seed = import_csv(str(source), progress=False)
        original = tmp_path / "source.xlsx"
        export_xlsx(seed, original)
        import zipfile

        with zipfile.ZipFile(original) as package:
            members = {name: package.read(name) for name in package.namelist()}
        worksheet = etree.fromstring(members["xl/worksheets/sheet1.xml"])
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for row, raw_id in zip(
            worksheet.findall("s:sheetData/s:row", ns)[1:], ["same", "same", "same#2", "", "xlsx:3"], strict=True
        ):
            cell = row[0]
            cell.clear()
            cell.set("r", f"A{row.get('r')}")
            cell.set("t", "inlineStr")
            inline = etree.SubElement(cell, "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}is")
            etree.SubElement(inline, "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t").text = raw_id
        members["xl/worksheets/sheet1.xml"] = etree.tostring(worksheet)
        with zipfile.ZipFile(original, "w") as package:
            for name, payload in members.items():
                package.writestr(name, payload)
        document = import_xlsx(str(original), source_locale="en", target_locale="fr", progress=progress)
    else:
        original = source
        document = import_csv(str(original), progress=progress)
    assert list(document.data) == ["same", "same#2", "same#2#2", f"{format_name}:3", f"{format_name}:3#2"]
    assert [unit.source for unit in document.data.values()] == ["First", "Second", "Third", "Fourth", "Fifth"]
    assert [unit.comments[0].context for unit in document.data.values()] == [
        "first note",
        "second note",
        "third note",
        "fourth note",
        "fifth note",
    ]
    for key, unit in document.data.items():
        unit.target = f"changed {key}"
        if "fr" in unit.targets:
            unit.targets["fr"].text = f"changed {key}"
    output = tmp_path / f"regenerated.{format_name}"
    if format_name == "xlsx":
        document.regen.xlsx(original, output)
        regenerated = import_xlsx(str(output), source_locale="en", target_locale="fr", progress=False)
    else:
        document.regen.csv(original, output)
        regenerated = import_csv(str(output), progress=False)
    assert [unit.target for unit in regenerated.data.values()] == [f"changed {key}" for key in document.data]


def test_split_csv_duplicates_keep_matching_ids(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("id,en,fr,de\nsame,First,Premier,Erste\nsame,Second,Deuxieme,Zweite\n", encoding="utf-8")
    documents = import_csv_targets(str(source), progress=False)
    assert list(documents["fr"].data) == list(documents["de"].data) == ["same", "same#2"]
    assert documents["de"].data["same#2"].target == "Zweite"


@pytest.mark.parametrize("streaming", [False, True])
def test_tmx_regeneration_uses_resolved_ids(tmp_path: Path, streaming: bool) -> None:
    source = tmp_path / "source.tmx"
    attributes = ['tuid="same"', 'tuid="same#2"', 'tuid="same"', "", 'tuid=""', 'tuid="auto_0"']
    source.write_text(
        '<tmx version="1.4"><header srclang="en" tgtlang="fr"/><body>'
        + "".join(
            f'<tu {attr}><tuv xml:lang="en"><seg>S{i}</seg></tuv><tuv xml:lang="fr"><seg>T{i}</seg></tuv></tu>'
            for i, attr in enumerate(attributes)
        )
        + "</body></tmx>",
        encoding="utf-8",
    )
    document = import_tmx(str(source), progress=False)
    for key, unit in document.data.items():
        unit.target = f"changed {key}"
        if "fr" in unit.targets:
            unit.targets["fr"].text = f"changed {key}"
    output = tmp_path / "output.tmx"
    if streaming:
        stream = StreamingStructure(source_locale="en", target_locale="fr", items=iter(document.data.items()))
        stream.regen.tmx(source, output)
    else:
        document.regen.tmx(source, output)
    tree = etree.parse(str(output))
    assert tree.xpath('//tuv[@xml:lang="fr"]/seg/text()') == [f"changed {key}" for key in document.data]


@pytest.mark.parametrize("version_two", [False, True])
def test_xliff_regeneration_and_export_identity(tmp_path: Path, version_two: bool) -> None:
    source = tmp_path / "source.xlf"
    if version_two:
        payload = '<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.0" srcLang="en" trgLang="fr">'
        for resource in range(2):
            payload += f'<file id="f{resource}"><unit id="u">'
            payload += (
                '<segment id="1"><source>A</source><target>B</target></segment>'
                '<segment id="2"><source>C</source></segment>'
            )
            payload += "</unit></file>"
    else:
        payload = '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
        for resource in range(2):
            payload += f'<file original="f{resource}" source-language="en" target-language="fr"><body>'
            payload += '<trans-unit id="u"><source>A</source><target>B</target></trans-unit>'
            payload += "</body></file>"
    source.write_text(payload + "</xliff>", encoding="utf-8")
    document = import_xliff(str(source), progress=False)
    for key, unit in document.data.items():
        unit.target = f"changed {key}"
        if "fr" in unit.targets:
            unit.targets["fr"].text = f"changed {key}"
    output = tmp_path / "regen.xlf"
    document.regen.xliff(source, output)
    tree = etree.parse(str(output))
    assert tree.xpath('//*[local-name()="target"]/text()') == [f"changed {key}" for key in document.data]
    namespace = "urn:oasis:names:tc:xliff:document:2.0" if version_two else "urn:oasis:names:tc:xliff:document:1.2"
    assert all(
        etree.QName(element).namespace == namespace
        for element in cast("list[_Element]", tree.xpath('//*[local-name()="target"]'))
    )
    exported = tmp_path / "export.xlf"
    export_xliff(document, exported)
    ids = cast("list[str]", etree.parse(str(exported)).xpath('//*[local-name()="trans-unit"]/@id'))
    assert len(ids) == len(set(ids)) == len(document.data)


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("placeholders", [False, True])
@pytest.mark.parametrize("rich", [False, True])
def test_plural_family_is_final_before_projection(
    tmp_path: Path, progress: bool, placeholders: bool, rich: bool
) -> None:
    from lokit.importers import stream_xliff

    source = tmp_path / "plural.xlf"
    note = "<note>Keep</note>" if rich else ""
    source.write_text(
        '<xliff version="1.2"><file source-language="en" target-language="fr"><body>'
        '<group restype="x-gettext-plurals">'
        f'<trans-unit id="apple[0]"><source>apple</source><target>pomme</target>{note}</trans-unit>'
        '<trans-unit id="apple[1]"><source>apples</source><target>pommes</target></trans-unit>'
        '</group><trans-unit id="item[0]"><source>ordinary</source></trans-unit></body></file></xliff>',
        encoding="utf-8",
    )
    document = import_xliff(
        str(source), progress=progress, runtime_placeholders=placeholders, inline_placeholders=placeholders
    )
    assert document.data["item[0]"].plural is None
    for key in ("apple[0]", "apple[1]"):
        plural = document.data[key].plural
        assert plural is not None and plural.variant == "apples"
    stream = stream_xliff(str(source), runtime_placeholders=placeholders, inline_placeholders=placeholders)
    observed = [(key, unit.plural.variant if unit.plural else None) for key, unit in stream.items]
    assert observed == [("apple[0]", "apples"), ("apple[1]", "apples"), ("item[0]", None)]


@pytest.mark.parametrize("progress", [False, True])
def test_xml_encoding_and_newline_parity(tmp_path: Path, progress: bool) -> None:
    source = tmp_path / "encoded.xlf"
    source.write_bytes(
        (
            '<?xml version="1.0" encoding="ISO-8859-1"?><xliff version="1.2">'
            '<file source-language="en"><body>'
            '<trans-unit id="plain"><source>Café\r\nB\rC<![CDATA[\r\nD]]>&#13;</source></trans-unit>'
            '<trans-unit id="rich"><source>Café\r\nB\rC<![CDATA[\r\nD]]>&#13;</source><note>Note</note></trans-unit>'
            "</body></file></xliff>"
        ).encode("iso-8859-1")
    )
    document = import_xliff(str(source), progress=progress)
    assert document.data["plain"].source == document.data["rich"].source == "Café\nB\nC\nD\r"


@pytest.mark.parametrize("progress", [False, True])
def test_explicit_tmx_languages_preserve_header(tmp_path: Path, progress: bool) -> None:
    source = tmp_path / "header.tmx"
    source.write_text(
        '<tmx version="1.4"><header srclang="en" tgtlang="fr" creationtool="Tool" creationtoolversion="2" '
        'creationdate="20260905T120000Z"><prop type="client">Client</prop></header>'
        '<body><tu tuid="one"><tuv xml:lang="en"><seg>A</seg></tuv>'
        '<tuv xml:lang="fr"><seg>B</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    document = import_tmx(str(source), source_language="en", target_language="fr", progress=progress)
    assert document.export_origin == "Tool 2"
    assert document.export_timestamp == "20260905T120000Z"
    assert document.extensions["property.client"] == "Client"


def test_tmx_discovery_updates_stream_and_rich_records(tmp_path: Path) -> None:
    from lokit.db.serialization import iter_serialized_units
    from lokit.importers import stream_tmx

    source = tmp_path / "discovered.tmx"
    source.write_text(
        '<tmx version="1.4"><header/><body>'
        '<tu tuid="plain"><tuv xml:lang="en"><seg>A</seg></tuv><tuv xml:lang="fr"><seg>B</seg></tuv></tu>'
        '<tu tuid="rich"><note>Note</note><tuv xml:lang="en"><seg>C</seg></tuv>'
        '<tuv xml:lang="fr"><seg>D</seg></tuv><tuv xml:lang="de"><seg>E</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    document = stream_tmx(str(source))
    units = dict(document.items)
    assert units["rich"].source == "C"
    assert units["rich"].targets["fr"].text == "D"
    assert units["rich"].targets["de"].text == "E"
    assert document.source_locale == "en"
    assert set(document.target_locales) == {"fr", "de"}
    rows = list(iter_serialized_units(stream_tmx(str(source))))
    assert len(rows) == 3
    assert all(row.unit.source_locale == "en" for row in rows)


def test_plural_family_crosses_native_batches(tmp_path: Path) -> None:
    from lokit._interchange_rust import Reader

    source = tmp_path / "large-family.xlf"
    source.write_text(
        '<xliff version="1.2"><file source-language="en"><body><group restype="x-gettext-plurals">'
        + "".join(
            f'<trans-unit id="u[{i}]"><source>{"one" if i == 0 else "many"}</source></trans-unit>' for i in range(300)
        )
        + "</group></body></file></xliff>",
        encoding="utf-8",
    )
    reader = Reader(str(source), "xliff")
    try:
        for i in range(300):
            records = reader.read_batch(1)
            assert len(records) == 1
            assert records[0][1] == f"u[{i}]"
            assert records[0][6]["po_msgid_plural"] == "many"
        assert reader.read_batch(1) == []
    finally:
        reader.close()


@pytest.mark.asyncio
async def test_public_async_stream_context_manager(tmp_path: Path) -> None:
    from lokit.stream import async_ as stream

    source = tmp_path / "stream.xlf"
    source.write_text(
        '<xliff version="1.2"><file source-language="en"><body>'
        '<trans-unit id="one"><source>A</source></trans-unit></body></file></xliff>',
        encoding="utf-8",
    )
    async with stream.xliff(str(source)) as units:
        key, unit = await anext(units)
        assert key == "one" and unit.source == "A"


def test_sync_stream_context_closes_early(tmp_path: Path) -> None:
    from lokit.importers import stream_xliff

    source = tmp_path / "stream.xlf"
    source.write_text(
        '<xliff version="1.2"><file source-language="en"><body>'
        '<trans-unit id="one"><source>A</source></trans-unit></body></file></xliff>',
        encoding="utf-8",
    )
    with stream_xliff(str(source)) as document:
        iterator = iter(document.items)
        assert next(iterator)[0] == "one"
    assert list(iterator) == []
    document.close()


@pytest.mark.parametrize("progress", [False, True])
def test_xliff_two_default_state_does_not_depend_on_notes(tmp_path: Path, progress: bool) -> None:
    from lokit.data.structure import TranslationStatus

    source = tmp_path / "state.xlf"
    source.write_text(
        '<xliff version="2.0" srcLang="en" trgLang="fr"><file id="f">'
        '<unit id="plain"><segment><source>A</source><target>B</target></segment></unit>'
        '<unit id="rich"><notes><note>Note</note></notes><segment><source>A</source><target>B</target></segment></unit>'
        "</file></xliff>",
        encoding="utf-8",
    )
    document = import_xliff(str(source), progress=progress)
    assert document.data["plain"].status == document.data["rich"].status == TranslationStatus.NEW
