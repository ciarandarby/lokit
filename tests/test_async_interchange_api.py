from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest
from lxml import etree

import lokit
from lokit.types import BaseStructure, Data, StreamingStructure, TagSyntax, UnsupportedTagPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _write_inline_tmx(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header srclang="en" adminlang="en" datatype="text" segtype="sentence"/>
  <body>
    <tu tuid="welcome">
      <tuv xml:lang="en"><seg>Hello <bpt i="1">&lt;b&gt;</bpt>world<ept i="1">&lt;/b&gt;</ept>.</seg></tuv>
      <tuv xml:lang="fr"><seg>Bonjour <bpt i="1">&lt;b&gt;</bpt>monde<ept i="1">&lt;/b&gt;</ept>.</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )


def _write_inline_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">
  <file original="app" source-language="en" target-language="fr" datatype="plaintext">
    <body>
      <trans-unit id="welcome">
        <source>Hello <g id="1" ctype="bold">world</g>.</source>
        <target>Bonjour <g id="1" ctype="bold">monde</g>.</target>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_async_tmx_parse_and_stream_wrappers_render_inline_tags(tmp_path: Path) -> None:
    source = tmp_path / "inline.tmx"
    _write_inline_tmx(source)

    parsed = [
        item
        async for item in lokit.parse.async_.tmx(
            str(source),
            source_language="en",
            target_language="fr",
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.ERROR,
        )
    ]
    streamed = [
        item
        async for item in lokit.stream.async_.tmx(
            str(source),
            source_language="en",
            target_language="fr",
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.ERROR,
        )
    ]
    batches = [
        batch
        async for batch in lokit.stream.async_.tmx_batches(
            str(source),
            source_language="en",
            target_language="fr",
            batch_size=1,
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.ERROR,
        )
    ]

    assert parsed[0][1].source == "Hello <b>world</b>."
    assert streamed[0][1].source == "Hello <b>world</b>."
    assert batches[0][0][1].source == "Hello <b>world</b>."


@pytest.mark.asyncio
async def test_async_xliff_parse_and_stream_wrappers_render_inline_tags(tmp_path: Path) -> None:
    source = tmp_path / "inline.xliff"
    _write_inline_xliff(source)

    parsed = [
        item
        async for item in lokit.parse.async_.xliff(
            str(source),
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.ERROR,
        )
    ]
    streamed = [
        item
        async for item in lokit.stream.async_.xliff(
            str(source),
            include_tags=True,
            tag_syntax=TagSyntax.HTML,
            unsupported_tags=UnsupportedTagPolicy.ERROR,
        )
    ]

    assert parsed[0][1].source.strip() == "Hello <strong>world</strong>."
    assert streamed[0][1].source.strip() == "Hello <strong>world</strong>."


@pytest.mark.asyncio
async def test_canonical_async_tmx_writer(tmp_path: Path) -> None:
    output = tmp_path / "translations.tmx"
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"welcome": Data(source="Hello", target="Bonjour")},
    )

    await lokit.export.async_.tmx(document, output)

    root = etree.parse(str(output)).getroot()
    assert [unit.attrib["tuid"] for unit in root.findall("./body/tu")] == ["welcome"]
    assert callable(lokit.async_.write.tmx)


@pytest.mark.asyncio
async def test_async_xliff_writer_groups_by_resource(tmp_path: Path) -> None:
    output = tmp_path / "translations.xliff"
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "first": Data(source="First", target="Premier", extensions={"resource": "a.json"}),
            "second": Data(source="Second", target="Deuxième", extensions={"resource": "b.json"}),
        },
    )

    await lokit.async_.write.xliff(document, output, group_by_resource=True)

    namespace = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    root = etree.parse(str(output)).getroot()
    assert [element.attrib["original"] for element in root.findall("x:file", namespace)] == ["a.json", "b.json"]
    assert callable(lokit.export.async_.xliff)


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.asyncio
async def test_async_xml_writer_cancellation_is_atomic_and_closes_input(
    tmp_path: Path,
    format_name: str,
) -> None:
    output = tmp_path / f"cancelled.{format_name}"
    output.write_bytes(b"existing\n")
    producer_started = threading.Event()
    producer_closed = threading.Event()

    def slow_items() -> Iterator[tuple[str, Data]]:
        try:
            for index in range(20):
                producer_started.set()
                time.sleep(0.01)
                yield (
                    f"unit-{index}",
                    Data(
                        source=f"Source {index}",
                        target=f"Target {index}",
                        extensions={"resource": "messages.json"},
                    ),
                )
        finally:
            producer_closed.set()

    document = StreamingStructure("en", "fr", slow_items())
    if format_name == "tmx":
        export_task = asyncio.create_task(lokit.export.async_.tmx(document, output))
    else:
        export_task = asyncio.create_task(lokit.export.async_.xliff(document, output, group_by_resource=True))
    assert await asyncio.to_thread(producer_started.wait, 2.0)

    export_task.cancel()
    await asyncio.sleep(0)
    export_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await export_task

    assert producer_closed.is_set()
    settled_payload = output.read_bytes()
    await asyncio.sleep(0.25)
    assert output.read_bytes() == settled_payload == b"existing\n"
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []
