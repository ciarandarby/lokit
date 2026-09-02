from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from lokit.data.structure import BaseStructure, CodePart, Data, StreamingStructure, TargetData, TextPart
from lokit.data.tag_types import TieType
from lokit.exporters.html import export_html, export_html_async
from lokit.importers import import_html, import_html_async, stream_html
from lokit.logic import Lokit
from lokit.parsers.html import extraction as html_extraction
from lokit.parsers.html.extraction import HtmlExtractor

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from lokit.parsers.async_bridge import AsyncExtractionBridge


class _ChunkTrackingHtmlReader:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if self.bytes_read >= len(self._content):
            return b""
        if size < 0:
            size = len(self._content) - self.bytes_read
        start = self.bytes_read
        stop = min(len(self._content), start + size)
        self.bytes_read = stop
        return self._content[start:stop]


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
    assert imported.extensions["source_file"] == str(source_html)
    assert imported.extensions["source_html"] == str(source_html)
    assert "html:meta.description:0" in imported.data
    assert imported.data["html:meta.description:0"].source == "A nice description"
    assert "html:title:1" in imported.data
    assert imported.data["html:title:1"].source == "My Title"
    assert "html:p:2" in imported.data
    assert imported.data["html:p:2"].source == "Hello world"
    assert "html:p:3" in imported.data
    p_unit = imported.data["html:p:3"]
    assert p_unit.source == "This is {LOKIT_P1}bold{LOKIT_P2} text."
    assert p_unit.tags is not None
    assert "t0" in p_unit.tags.source_tag_map
    assert p_unit.tags.source_tag_map["t0"].type == TieType.B_OPEN
    unprojected = import_html(
        str(source_html),
        source_locale="en",
        target_locale="fr",
        progress=False,
        runtime_placeholders=False,
        inline_placeholders=False,
    )
    assert unprojected.data["html:p:3"].source == "This is bold text."
    assert "html:img.alt:4" in imported.data
    assert imported.data["html:img.alt:4"].source == "A lovely photo"

    imported.data["html:meta.description:0"].target = "Une belle description"
    imported.data["html:title:1"].target = "Mon Titre"
    imported.data["html:p:2"].target = "Bonjour le monde"

    p_unit.target = "C'est du texte en {LOKIT_P1}gras{LOKIT_P2}."
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
    assert reparsed.data["html:p:3"].source == "C'est du texte en {LOKIT_P1}gras{LOKIT_P2}."
    assert reparsed.data["html:img.alt:4"].source == "Une jolie photo"


def test_html_output_reuses_retained_source_file(tmp_path: Path) -> None:
    source_html = tmp_path / "source.html"
    source_html.write_text(
        '<!doctype html><html lang="en"><body><nav>Keep me</nav><p>Hello</p></body></html>',
        encoding="utf-8",
    )
    document = import_html(str(source_html), source_locale="en", target_locale="fr", progress=False)
    document.data["html:p:0"].target = "Bonjour"

    output_html = tmp_path / "translated.html"
    Lokit(document).output(output_html)

    content = output_html.read_text(encoding="utf-8")
    assert "Keep me" in content
    assert "Bonjour" in content


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


def test_minimal_html_export_consumes_stream_once(tmp_path: Path) -> None:
    output = tmp_path / "streamed.html"
    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=iter(
            (
                (
                    "html:meta.description:0",
                    Data(
                        source="Description",
                        target="Description traduite",
                        extensions={"meta_name": "description"},
                    ),
                ),
                ("html:p:1", Data(source="Hello", target="Bonjour")),
            )
        ),
    )

    export_html(document, output)

    content = output.read_text(encoding="utf-8")
    assert 'content="Description traduite"' in content
    assert "<p>Bonjour</p>" in content


def test_retained_html_export_matches_importer_unit_order(tmp_path: Path) -> None:
    source = tmp_path / "ordering.html"
    output = tmp_path / "ordering.fr.html"
    source.write_text(
        '<!doctype html><html lang="en"><head>'
        '<title>Title</title><meta name="keywords" content="   ">'
        '<meta name="description" content="Description"></head><body>'
        "<p> </p><div><p></p></div><p>Hello</p>"
        "<blockquote>Outer <p>Inner</p> tail</blockquote>"
        '<img alt="Photo"></body></html>',
        encoding="utf-8",
    )
    document = import_html(str(source), source_locale="en", target_locale="fr", progress=False)
    assert [(unit_id, unit.source) for unit_id, unit in document.data.items()] == [
        ("html:meta.description:0", "Description"),
        ("html:title:1", "Title"),
        ("html:p:2", "Hello"),
        ("html:blockquote:3", "Outer  tail"),
        ("html:p:4", "Inner"),
        ("html:img.alt:5", "Photo"),
    ]
    translations = {
        "html:meta.description:0": "Description FR",
        "html:title:1": "Titre",
        "html:p:2": "Bonjour",
        "html:blockquote:3": "Extérieur",
        "html:p:4": "Intérieur",
        "html:img.alt:5": "Photo FR",
    }
    for unit_id, target in translations.items():
        document.data[unit_id].target = target

    export_html(document, output, source_html=source)

    reparsed = import_html(str(output), source_locale="fr", progress=False)
    assert {unit_id: unit.source for unit_id, unit in reparsed.data.items()} == translations
    assert "<p>Intérieur</p>" in output.read_text(encoding="utf-8")


def test_retained_html_export_recovers_malformed_source_in_place(tmp_path: Path) -> None:
    source = tmp_path / "malformed.html"
    source.write_text(
        "<!doctype html><html lang=en><body><p>Hello <b>bold</b><p>Second",
        encoding="utf-8",
    )
    document = import_html(str(source), source_locale="en", target_locale="fr", progress=False)
    for index, unit in enumerate(document.data.values()):
        unit.target = f"translation-{index}"

    export_html(document, source, source_html=source)

    content = source.read_text(encoding="utf-8")
    assert content.startswith("<!DOCTYPE html>")
    reparsed = import_html(str(source), source_locale="fr", progress=False)
    assert [unit.source for unit in reparsed.data.values()] == [
        f"translation-{index}" for index in range(len(document.data))
    ]
    assert not list(tmp_path.glob(f".{source.name}.*.tmp"))


def test_empty_retained_html_falls_back_to_streaming_document(tmp_path: Path) -> None:
    source = tmp_path / "empty.html"
    source.write_text("   ", encoding="utf-8")
    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=iter((("html:p:0", Data(source="Hello", target="Bonjour")),)),
    )

    export_html(document, source, source_html=source)

    assert "<p>Bonjour</p>" in source.read_text(encoding="utf-8")


def test_html_export_preserves_explicit_empty_targets(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    retained_output = tmp_path / "retained.html"
    minimal_output = tmp_path / "minimal.html"
    source.write_text(
        "<!doctype html><html><body><p>Hello</p></body></html>",
        encoding="utf-8",
    )
    retained = import_html(str(source), source_locale="en", target_locale="fr", progress=False)
    retained.data["html:p:0"].target = ""

    export_html(retained, retained_output, source_html=source)
    export_html(
        BaseStructure(
            source_locale="en",
            target_locale="fr",
            data={"html:p:0": Data(source="Hello", target="")},
        ),
        minimal_output,
    )

    assert "<p></p>" in retained_output.read_text(encoding="utf-8")
    assert "<p></p>" in minimal_output.read_text(encoding="utf-8")


def test_retained_html_streaming_export_has_bounded_memory(tmp_path: Path) -> None:
    unit_count = 4_000
    payload = "x" * 4_096
    source = tmp_path / "large.html"
    output = tmp_path / "large.fr.html"
    with source.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("<!doctype html><html><body>")
        for index in range(unit_count):
            stream.write(f"<p>source-{index}</p>")
        stream.write("</body></html>")

    closed = False

    def items() -> Iterator[tuple[str, Data]]:
        nonlocal closed
        try:
            for index in range(unit_count):
                yield (
                    f"html:p:{index}",
                    Data(
                        source=f"source-{index}",
                        target=f"target-{index}-{payload}",
                    ),
                )
        finally:
            closed = True

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    tracemalloc.start()
    started = time.perf_counter()
    try:
        export_html(document, output, source_html=source)
        elapsed = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert closed
    assert output.stat().st_size > unit_count * len(payload)
    assert peak_bytes < 16 * 1024 * 1024
    assert unit_count / elapsed > 200


@pytest.mark.asyncio
async def test_retained_html_async_cancellation_closes_stream_and_is_atomic(tmp_path: Path) -> None:
    source = tmp_path / "cancel-source.html"
    output = tmp_path / "cancel-output.html"
    source.write_text("<!doctype html><html><body><p>Hello</p></body></html>", encoding="utf-8")
    output.write_text("existing output", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def items() -> Iterator[tuple[str, Data]]:
        try:
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release the HTML input stream")
            yield "html:p:0", Data(source="Hello", target="Bonjour")
        finally:
            closed.set()

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    task = asyncio.create_task(export_html_async(document, output, source_html=source))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert output.read_text(encoding="utf-8") == "existing output"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_nested_html_stream_spools_before_first_yield_with_bounded_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit_count = 3_000
    payload = "x" * 4_096
    source = tmp_path / "nested-large.html"
    with source.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("<!doctype html><html><body><blockquote>Outer")
        for index in range(unit_count):
            stream.write(f"<p>{index}-{payload}</p>")
        stream.write("</blockquote><p>After</p></body></html>")

    spool_directories: list[Path] = []

    def tracked_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory(prefix=prefix, dir=tmp_path)
        spool_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(html_extraction, "TemporaryDirectory", tracked_directory)
    tracemalloc.start()
    try:
        document = stream_html(
            str(source),
            runtime_placeholders=False,
            inline_placeholders=False,
        )
        items = cast("Generator[tuple[str, Data], None, None]", document.items)
        unit_id, unit = next(items)
        _, peak_bytes = tracemalloc.get_traced_memory()
        assert unit_id == "html:blockquote:0"
        assert unit.source == "Outer"
        assert spool_directories and all(path.exists() for path in spool_directories)
        items.close()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 4 * 1024 * 1024
    assert all(not path.exists() for path in spool_directories)


def test_html_stream_yields_before_reading_later_top_level_content() -> None:
    trailing = b" " * (256 * 1024) + b"<p>Later</p></body></html>"
    reader = _ChunkTrackingHtmlReader(b"<html><body><blockquote>Outer<p>Inner</p></blockquote>" + trailing)
    extractor = HtmlExtractor(reader)
    items = cast(
        "Generator[tuple[str, Data], None, None]",
        extractor.extract(runtime_placeholders=False, inline_placeholders=False),
    )
    try:
        unit_id, unit = next(items)
        assert unit_id == "html:blockquote:0"
        assert unit.source == "Outer"
        assert reader.bytes_read < len(reader._content)
    finally:
        items.close()


def test_nested_blocks_preserve_surrounding_inline_tails(tmp_path: Path) -> None:
    source = tmp_path / "mixed-nesting.html"
    output = tmp_path / "mixed-nesting.fr.html"
    source.write_text(
        "<html><body><blockquote>Before <strong>bold</strong> before-child "
        "<p>Inner</p> after-child <em>end</em>.</blockquote></body></html>",
        encoding="utf-8",
    )

    units = list(
        HtmlExtractor(str(source)).extract(
            runtime_placeholders=False,
            inline_placeholders=False,
        )
    )

    assert [(unit_id, unit.source) for unit_id, unit in units] == [
        ("html:blockquote:0", "Before bold before-child  after-child end."),
        ("html:p:1", "Inner"),
    ]
    outer = units[0][1]
    assert outer.tags is not None
    assert [part.value if isinstance(part, TextPart) else part.ref for part in outer.tags.source_parts] == [
        "Before ",
        "t0",
        "bold",
        "t1",
        " before-child  after-child ",
        "t2",
        "end",
        "t3",
        ".",
    ]

    retained = import_html(
        str(source),
        source_locale="en",
        target_locale="fr",
        progress=False,
    )
    retained.data["html:p:1"].target = "Intérieur"
    export_html(retained, output, source_html=source)
    reparsed = import_html(str(output), source_locale="fr", progress=False)
    assert list(reparsed.data) == ["html:blockquote:0", "html:p:1"]
    assert reparsed.data["html:p:1"].source == "Intérieur"
    assert "before-child  after-child" in reparsed.data["html:blockquote:0"].source


@pytest.mark.asyncio
async def test_cancelled_nested_html_import_removes_pending_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "nested-cancel.html"
    source.write_text(
        "<html><body><blockquote>Outer"
        + "".join(f"<p>Child {index}</p>" for index in range(512))
        + "</blockquote></body></html>",
        encoding="utf-8",
    )
    spool_directories: list[Path] = []

    def tracked_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory(prefix=prefix, dir=tmp_path)
        spool_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(html_extraction, "TemporaryDirectory", tracked_directory)
    first_received = asyncio.Event()
    release = asyncio.Event()
    units = cast(
        "AsyncExtractionBridge[tuple[str, Data]]",
        HtmlExtractor(str(source)).extract_async(
            runtime_placeholders=False,
            inline_placeholders=False,
        ),
    )

    async def consume() -> None:
        async with units:
            async for _unit_id, _unit in units:
                first_received.set()
                await release.wait()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_received.wait(), timeout=2)
    assert spool_directories and all(path.exists() for path in spool_directories)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(not path.exists() for path in spool_directories)
