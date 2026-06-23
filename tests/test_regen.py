from __future__ import annotations

import json
import time
import tracemalloc
import zipfile
from typing import TYPE_CHECKING

import pytest

from lokit import Lokit
from lokit.importers import import_csv, import_json_i18n, import_po, import_tmx, import_xliff, import_xlsx
from lokit.parse.write import regen

if TYPE_CHECKING:
    from pathlib import Path


def test_regen_proxy_csv_preserves_original_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "target.csv"
    source.write_text("id,en,fr,de,note\none,Hello,Bonjour,Hallo,keep\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    document.data["one"].targets["fr"].text = "Salut"
    document.data["one"].targets["de"].text = "Guten Tag"

    document.regen.csv(source, output)

    assert output.read_text(encoding="utf-8") == "id,en,fr,de,note\none,Hello,Salut,Guten Tag,keep\n"


@pytest.mark.asyncio
async def test_regen_csv_async(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "target.csv"
    source.write_text("id,en,fr\none,Hello,Bonjour\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    document.data["one"].target = "Salut"

    await regen.csv_async(document, source, output)

    assert output.read_text(encoding="utf-8") == "id,en,fr\none,Hello,Salut\n"


def test_lokit_regen_xliff_updates_targets_from_original(tmp_path: Path) -> None:
    source = tmp_path / "source.xliff"
    output = tmp_path / "target.xliff"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en" target-language="fr" datatype="plaintext">
    <body>
      <trans-unit id="one"><source>Hello</source><target>Bonjour</target><note>Keep me</note></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )
    document = import_xliff(str(source), progress=False)
    document.data["one"].target = "Salut"

    Lokit(document).regen.xliff(source, output)
    regenerated = import_xliff(str(output), progress=False)

    assert regenerated.data["one"].target == "Salut"
    assert regenerated.data["one"].comments[0].context == "Keep me"


def test_regen_tmx_updates_each_target_locale(tmp_path: Path) -> None:
    source = tmp_path / "source.tmx"
    output = tmp_path / "target.tmx"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" segtype="sentence" adminlang="en" srclang="en" datatype="text"/>
  <body>
    <tu tuid="one">
      <tuv xml:lang="en"><seg>Hello</seg></tuv>
      <tuv xml:lang="fr"><seg>Bonjour</seg></tuv>
      <tuv xml:lang="de"><seg>Hallo</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )
    document = import_tmx(str(source), source_language="en", progress=False)
    document.data["one"].targets["fr"].text = "Salut"
    document.data["one"].targets["de"].text = "Guten Tag"

    document.regen.tmx(source, output)
    regenerated = import_tmx(str(output), source_language="en", progress=False)

    assert regenerated.data["one"].targets["fr"].text == "Salut"
    assert regenerated.data["one"].targets["de"].text == "Guten Tag"


def test_regen_po_preserves_comments_and_updates_msgstr(tmp_path: Path) -> None:
    source = tmp_path / "source.po"
    output = tmp_path / "target.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\n#. Greeting\nmsgid "Hello"\nmsgstr "Bonjour"\n',
        encoding="utf-8",
    )
    document = import_po(str(source), source_locale="en", target_locale="fr", progress=False)
    document.data["Hello"].target = "Salut"

    regen.po(document, source, output)

    text = output.read_text(encoding="utf-8")
    assert "#. Greeting" in text
    assert 'msgstr "Salut"' in text


def test_regen_json_i18n_reuses_source_shape(tmp_path: Path) -> None:
    source = tmp_path / "en.json"
    target = tmp_path / "fr.json"
    output = tmp_path / "regen.fr.json"
    source.write_text(json.dumps({"common": {"hello": "Hello"}}), encoding="utf-8")
    target.write_text(json.dumps({"common": {"hello": "Bonjour"}}), encoding="utf-8")
    document = import_json_i18n(
        str(source),
        source_locale="en",
        target_locale="fr",
        target_filepath=str(target),
        progress=False,
    )
    document.data["common.hello"].target = "Salut"

    regen.json_i18n(document, source, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"common": {"hello": "Salut"}}


def test_regen_xlsx_rewrites_target_cells_in_original_package(tmp_path: Path) -> None:
    from rustpy_xlsxwriter import FastExcel

    source = tmp_path / "source.xlsx"
    output = tmp_path / "target.xlsx"
    FastExcel(str(source), autofit=False).sheet(
        "Sheet1",
        [{"id": "one", "en": "Hello", "fr": "Bonjour", "note": "keep"}],
    ).save()
    document = import_xlsx(str(source), progress=False)
    document.data["one"].target = "Salut"

    document.regen.xlsx(source, output)
    regenerated = import_xlsx(str(output), progress=False)

    assert regenerated.data["one"].target == "Salut"
    with zipfile.ZipFile(output, "r") as archive:
        assert "xl/workbook.xml" in archive.namelist()


def test_regen_csv_has_bounded_memory_and_runtime(tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    output = tmp_path / "large.out.csv"
    rows = ["id,en,fr"]
    rows.extend(f"u{i},Hello {i},Bonjour {i}" for i in range(1000))
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    document = import_csv(str(source), progress=False)
    for unit_id, unit in document.data.items():
        unit.target = f"{unit_id} translated"

    tracemalloc.start()
    start = time.perf_counter()
    regen.csv(document, source, output)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert "u999 translated" in output.read_text(encoding="utf-8")
    assert peak < 5_000_000
    assert elapsed < 5.0
