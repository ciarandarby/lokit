from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import BaseStructure, CodePart, Data, TargetData, TextPart
from lokit.data.tag_types import TieType
from lokit.exporters.html import export_html, export_html_async
from lokit.importers import import_html, import_html_async

if TYPE_CHECKING:
    from pathlib import Path


def test_html_roundtrip(tmp_path: Path) -> None:
    source_html = tmp_path / "index.html"
    source_html.write_text(
        "<!DOCTYPE html><html><head><title>My Title</title>"
        '<meta name="description" content="A nice description">'
        "</head><body>"
        "<p>Hello world</p>"
        "<p>This is <b>bold</b> text.</p>"
        '<img src="img.jpg" alt="A lovely photo">'
        "</body></html>",
        encoding="utf-8",
    )
    imported = import_html(str(source_html), source_locale="en", target_locale="fr")
    assert imported.source_locale == "en"
    assert imported.target_locale == "fr"
    assert "html:meta.description:0" in imported.data
    assert imported.data["html:meta.description:0"].source == "A nice description"
    assert "html:title:1" in imported.data
    assert imported.data["html:title:1"].source == "My Title"
    assert "html:p:2" in imported.data
    assert imported.data["html:p:2"].source == "Hello world"
    assert "html:p:3" in imported.data
    p_unit = imported.data["html:p:3"]
    assert p_unit.source == "This is bold text."
    assert p_unit.tags is not None
    assert "t0" in p_unit.tags.source_tag_map
    assert p_unit.tags.source_tag_map["t0"].type == TieType.B_OPEN
    assert "html:img.alt:4" in imported.data
    assert imported.data["html:img.alt:4"].source == "A lovely photo"

    imported.data["html:meta.description:0"].target = "Une belle description"
    imported.data["html:title:1"].target = "Mon Titre"
    imported.data["html:p:2"].target = "Bonjour le monde"

    p_unit.target = "C'est du texte en gras."
    p_unit.tags.target_parts = [
        TextPart("C'est du texte en "),
        CodePart("t0"),
        TextPart("gras"),
        CodePart("t1"),
        TextPart("."),
    ]
    p_unit.tags.target_tag_map = p_unit.tags.source_tag_map
    imported.data["html:img.alt:4"].target = "Une jolie photo"
    output_html = tmp_path / "index_fr.html"
    export_html(imported, output_html, source_html=source_html)

    reparsed = import_html(str(output_html), source_locale="fr")
    assert reparsed.source_locale == "fr"
    assert reparsed.data["html:meta.description:0"].source == "Une belle description"
    assert reparsed.data["html:title:1"].source == "Mon Titre"
    assert reparsed.data["html:p:2"].source == "Bonjour le monde"
    assert reparsed.data["html:p:3"].source == "C'est du texte en gras."
    assert reparsed.data["html:img.alt:4"].source == "Une jolie photo"


@pytest.mark.asyncio
async def test_html_async(tmp_path: Path) -> None:
    source_html = tmp_path / "index_async.html"
    source_html.write_text(
        "<!DOCTYPE html><html><body><p>Hello</p></body></html>",
        encoding="utf-8",
    )

    imported_units = {}
    async for unit_id, data in import_html_async(str(source_html), source_locale="en", target_locale="fr"):
        imported_units[unit_id] = data
    imported = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data=imported_units,
    )
    assert imported.data["html:p:0"].source == "Hello"

    imported.data["html:p:0"].target = "Bonjour"
    output_html = tmp_path / "index_fr_async.html"
    await export_html_async(imported, output_html, source_html=source_html)

    assert output_html.exists()
    content = output_html.read_text(encoding="utf-8")
    assert "Bonjour" in content


def test_html_export_multitarget_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "html"
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        data={
            "html:p:0": Data(
                source="Hello",
                targets={
                    "fr": TargetData(text="Bonjour"),
                    "de": TargetData(text="Hallo"),
                },
            )
        },
    )

    export_html(document, output_dir)

    assert "Bonjour" in (output_dir / "index.fr.html").read_text(encoding="utf-8")
    assert "Hallo" in (output_dir / "index.de.html").read_text(encoding="utf-8")
