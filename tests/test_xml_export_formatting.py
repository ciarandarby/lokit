from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree

from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Tags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import XLIFF_NS, export_xliff
from lokit.importers import import_tmx, import_xliff

if TYPE_CHECKING:
    from pathlib import Path

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _document() -> BaseStructure:
    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={
            "unit-1": Data(
                source=" Hello & world ",
                target=" Bonjour & monde ",
                status=TranslationStatus.TRANSLATED,
                comments=[Comment(context="Translator note")],
            )
        },
        extensions={"property.domain": "release"},
    )


def test_tmx_export_is_indented_standard_xml_and_roundtrips(tmp_path: Path) -> None:
    output = tmp_path / "formatted.tmx"
    export_tmx(_document(), output)

    rendered = output.read_text(encoding="utf-8")
    assert rendered.endswith("</tmx>\n")
    assert "\n  <header " in rendered
    assert '\n    <prop type="domain">release</prop>' in rendered
    assert '\n    <tu tuid="unit-1">' in rendered
    assert '\n      <tuv xml:lang="en-US">' in rendered
    assert "\n        <seg> Hello &amp; world </seg>" in rendered

    root = etree.parse(str(output)).getroot()
    source_tuv = root.find("./body/tu/tuv")
    assert source_tuv is not None
    assert source_tuv.get(_XML_LANG) == "en-US"

    imported = import_tmx(str(output), "en-US", "fr-FR", progress=False)
    assert imported.data["unit-1"].source == " Hello & world "
    assert imported.data["unit-1"].target == " Bonjour & monde "


def test_xliff_export_is_indented_without_changing_segments(tmp_path: Path) -> None:
    output = tmp_path / "formatted.xliff"
    export_xliff(_document(), output)

    rendered = output.read_text(encoding="utf-8")
    assert rendered.endswith("</xliff>\n")
    assert "\n  <file " in rendered
    assert "\n    <header/>" in rendered
    assert '\n      <trans-unit id="unit-1">' in rendered
    assert "\n        <source> Hello &amp; world </source>" in rendered
    assert "\n        <target> Bonjour &amp; monde </target>" in rendered

    root = etree.parse(str(output)).getroot()
    assert root.tag == f"{{{XLIFF_NS}}}xliff"

    imported = import_xliff(str(output), progress=False)
    assert imported.data["unit-1"].source == " Hello & world "
    assert imported.data["unit-1"].target == " Bonjour & monde "


def test_xliff_inline_codes_inherit_default_namespace_without_prefix(tmp_path: Path) -> None:
    output = tmp_path / "inline.xliff"
    tags = Tags(
        source_tag_map={
            "open": TieData(id="open", type=TieType.STRONG_OPEN, pair_id="pair"),
            "close": TieData(id="close", type=TieType.STRONG_CLOSE, pair_id="pair"),
        },
        source_parts=[
            TextPart("A "),
            CodePart("open"),
            TextPart("bold"),
            CodePart("close"),
            TextPart(" value"),
        ],
    )
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={"inline": Data(source="A bold value", tags=tags)},
    )

    export_xliff(document, output)

    rendered = output.read_text(encoding="utf-8")
    assert '<bx id="open" rid="pair"/>' in rendered
    assert '<ex id="close" rid="pair"/>' in rendered
    assert "ns0:" not in rendered
    root = etree.parse(str(output)).getroot()
    assert root.find(f".//{{{XLIFF_NS}}}source/{{{XLIFF_NS}}}bx") is not None


def test_xliff_empty_grouped_document_contains_schema_required_file(tmp_path: Path) -> None:
    output = tmp_path / "empty.xliff"
    document = BaseStructure(source_locale="en", target_locale=None, data={})

    export_xliff(document, output, group_by_resource=True)

    root = etree.parse(str(output)).getroot()
    files = root.findall(f"{{{XLIFF_NS}}}file")
    assert len(files) == 1
    assert files[0].attrib == {
        "original": "lokit",
        "datatype": "plaintext",
        "source-language": "en",
    }
    body = files[0].find(f"{{{XLIFF_NS}}}body")
    assert body is not None
    assert len(body) == 0


def test_xliff_empty_grouped_multitarget_document_keeps_target_files(tmp_path: Path) -> None:
    output = tmp_path / "empty-multitarget.xliff"
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        data={},
    )

    export_xliff(document, output, group_by_resource=True)

    root = etree.parse(str(output)).getroot()
    files = root.findall(f"{{{XLIFF_NS}}}file")
    assert [file.attrib["target-language"] for file in files] == ["fr", "de"]
    assert [file.attrib["original"] for file in files] == ["lokit:fr", "lokit:de"]
