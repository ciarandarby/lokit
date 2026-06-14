from __future__ import annotations

from pathlib import Path

from lxml import etree

from lokit.exporters.xliff import export_xliff
from lokit.importers import import_xliff


def _write_multitarget_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en" target-language="fr" datatype="plaintext">
    <body>
      <trans-unit id="one"><source>Hello</source><target>Bonjour</target></trans-unit>
    </body>
  </file>
  <file original="messages" source-language="en" target-language="de" datatype="plaintext">
    <body>
      <trans-unit id="one"><source>Hello</source><target>Hallo</target></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def test_xliff_multifile_import_merges_targets(tmp_path: Path) -> None:
    xliff_file = tmp_path / "multi.xliff"
    _write_multitarget_xliff(xliff_file)

    document = import_xliff(str(xliff_file), progress=False)

    assert document.source_locale == "en"
    assert document.target_locale is None
    assert document.target_locales == ("fr", "de")
    assert list(document.data) == ["one"]
    assert document.data["one"].targets["fr"].text == "Bonjour"
    assert document.data["one"].targets["de"].text == "Hallo"


def test_xliff_export_multitarget_document(tmp_path: Path) -> None:
    xliff_file = tmp_path / "multi.xliff"
    exported = tmp_path / "exported.xliff"
    _write_multitarget_xliff(xliff_file)

    document = import_xliff(str(xliff_file), progress=False)
    export_xliff(document, exported)

    root = etree.parse(str(exported)).getroot()
    ns = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    files = root.findall("x:file", ns)
    assert [file.attrib["target-language"] for file in files] == ["fr", "de"]
    assert [target.text for target in root.findall(".//x:target", ns)] == ["Bonjour", "Hallo"]
