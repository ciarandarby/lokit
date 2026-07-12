from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from lokit.data.structure import BaseStructure, CodePart, Data, Tags, TargetData, TargetTags, TextPart
from lokit.data.tag_types import TieData, TieType
from lokit.data.targets import select_target
from lokit.exporters.html import export_html
from lokit.exporters.tmx import export_tmx
from lokit.io.json import load_lokit_json
from lokit.parsers.async_bridge import AsyncExtractionBridge
from lokit.parsers.html.extraction import HtmlExtractor
from lokit.parsers.tmx.extraction import TmxExtractor
from lokit.parsers.xliff.extraction import XliffExtractor
from lokit.types import TagSyntax, render_segment, segment_from_legacy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_stale_legacy_parts_are_replaced_by_plain_segment() -> None:
    parts: list[TextPart | CodePart] = [TextPart("Old "), CodePart("c0"), TextPart("text")]
    codes = {"c0": TieData(id="c0", type=TieType.BR, original_name="br")}

    segment = segment_from_legacy("New text", parts, codes, syntax=TagSyntax.HTML)

    assert segment.plain_text == "New text"
    assert segment.codes == {}
    assert render_segment(segment, TagSyntax.HTML, native_syntax=TagSyntax.HTML) == "New text"


def test_html_nested_inline_tags_have_unique_ordered_codes(tmp_path: Path) -> None:
    source = tmp_path / "nested.html"
    source.write_text("<html><body><p>A <b>bold <i>nested</i> value</b> tail.</p></body></html>", encoding="utf-8")

    _, unit = next(HtmlExtractor(str(source)).extract())

    assert unit.source == "A bold nested value tail."
    assert unit.tags is not None
    assert list(unit.tags.source_tag_map) == ["t0", "t1", "t2", "t3"]
    assert [code.order for code in unit.tags.source_tag_map.values()] == [0, 1, 2, 3]


def test_xliff_21_extracts_segments_and_converts_codes_to_html(tmp_path: Path) -> None:
    source = tmp_path / "messages.xlf"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.1" srcLang="en" trgLang="fr">
  <file id="f1"><unit id="welcome"><segment id="s1">
    <source>Hello <pc id="1" type="fmt:bold">world</pc>.</source>
    <target>Bonjour <pc id="1" type="fmt:bold">monde</pc>.</target>
  </segment></unit></file>
</xliff>
""",
        encoding="utf-8",
    )

    extractor = XliffExtractor(str(source))
    unit_id, unit = next(extractor.extract(include_tags=True, tag_syntax=TagSyntax.HTML))

    assert unit_id == "welcome:s1"
    assert extractor.source_locale == "en"
    assert extractor.target_locale == "fr"
    assert unit.source.strip() == "Hello <strong>world</strong>."
    assert unit.targets["fr"].text is not None
    assert unit.targets["fr"].text.strip() == "Bonjour <strong>monde</strong>."


def test_tmx_nested_level_two_content_roundtrips(tmp_path: Path) -> None:
    source = tmp_path / "nested.tmx"
    output = tmp_path / "roundtrip.tmx"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="en" adminlang="en" datatype="text" segtype="sentence"/>
<body><tu tuid="u1"><tuv xml:lang="en"><seg>A <hi type="bold">bold <sub>nested</sub></hi> value.</seg></tuv>
<tuv xml:lang="fr"><seg>Une <hi type="bold">valeur</hi>.</seg></tuv></tu></body></tmx>
""",
        encoding="utf-8",
    )

    extractor = TmxExtractor(str(source), source_language="en", target_language="fr")
    unit_id, data = next(extractor.extract())
    document = BaseStructure(source_locale="en", target_locale="fr", data={unit_id: data})
    export_tmx(document, output)
    reparsed = next(TmxExtractor(str(output), source_language="en", target_language="fr").extract())[1]

    assert data.source == "A bold nested value."
    assert reparsed.source == data.source
    assert "<hi" in output.read_text(encoding="utf-8")
    assert "<sub>" in output.read_text(encoding="utf-8")


def test_plain_target_edit_does_not_reuse_stale_source_tags(tmp_path: Path) -> None:
    output = tmp_path / "target.html"
    tags = Tags(
        source_tag_map={
            "o": TieData(id="o", type=TieType.B_OPEN, pair_id="p", original_name="b"),
            "c": TieData(id="c", type=TieType.B_CLOSE, pair_id="p", original_name="b"),
        },
        source_parts=[CodePart("o"), TextPart("Old"), CodePart("c")],
    )
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"html:p:0": Data(source="Old", target="Nouveau", tags=tags)},
    )

    export_html(document, output)

    rendered = output.read_text(encoding="utf-8")
    assert "<p>Nouveau</p>" in rendered
    assert "<b>Nouveau</b>" not in rendered


def test_select_target_copies_target_specific_tags() -> None:
    target_tags = TargetTags(
        tag_map={"br": TieData(id="br", type=TieType.BR, original_name="br")},
        parts=[TextPart("Bonjour"), CodePart("br")],
    )
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr",),
        data={
            "u": Data(
                source="Hello",
                targets={"fr": TargetData(text="Bonjour", tags=target_tags)},
                tags=Tags(),
            )
        },
    )

    selected = select_target(document, "fr")

    assert selected.data["u"].tags is not None
    assert selected.data["u"].tags.target_parts == target_tags.parts
    assert selected.data["u"].tags.target_tag_map == target_tags.tag_map


def test_lokit_json_restores_native_code_payload(tmp_path: Path) -> None:
    source = tmp_path / "document.json"
    source.write_text(
        """{
  "source_locale": "en", "target_locale": null,
  "data": {"u": {"source": "Hello", "tags": {
    "source_tag_map": {"c": {"id": "c", "type": "custom.standalone", "original_text": "<x/>"}},
    "source_parts": [{"value": "Hello"}, {"ref": "c"}]
  }}}
}
""",
        encoding="utf-8",
    )

    document = load_lokit_json(source)

    assert document.data["u"].tags is not None
    assert document.data["u"].tags.source_tag_map["c"].original_text == "<x/>"


async def _close_bridge_while_full() -> None:
    def values() -> Iterator[int]:
        yield from range(100_000)

    bridge = AsyncExtractionBridge(values, maxsize=1)
    assert await anext(bridge) == 0
    await asyncio.wait_for(bridge.aclose(), timeout=1.0)


def test_async_bridge_early_close_cannot_deadlock() -> None:
    asyncio.run(_close_bridge_while_full())
