from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import tracemalloc
import zipfile
from typing import TYPE_CHECKING

import pytest
from lxml import etree

import lokit.exporters.idml as idml_exporter
from lokit.data.structure import (
    BaseStructure,
    CodePart,
    Data,
    StreamingStructure,
    Tags,
    TargetData,
    TextPart,
)
from lokit.data.tag_types import TieData, TieType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


ExtractItem = tuple[str, Data]


class _OneShotItems:
    def __init__(self, factory: Callable[[], Iterator[ExtractItem]]) -> None:
        self._factory = factory
        self.iter_calls = 0
        self.yielded = 0
        self.closed = False

    def __iter__(self) -> Iterator[ExtractItem]:
        self.iter_calls += 1
        if self.iter_calls != 1:
            raise RuntimeError("one-shot items were iterated more than once")
        return self._iterate()

    def _iterate(self) -> Iterator[ExtractItem]:
        try:
            for item in self._factory():
                self.yielded += 1
                yield item
        finally:
            self.closed = True


def _story_xml(contents: list[str]) -> bytes:
    paragraphs = "".join(
        "<ParagraphStyleRange><CharacterStyleRange><Content>"
        f"{content}"
        "</Content></CharacterStyleRange></ParagraphStyleRange>"
        for content in contents
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Story>{paragraphs}</Story>'.encode()


def _write_package(path: Path, story_xml: bytes, *, unsafe_name: str | None = None) -> None:
    story_info = zipfile.ZipInfo("Stories/Story_stream.xml", (2022, 3, 4, 5, 6, 8))
    story_info.compress_type = zipfile.ZIP_DEFLATED
    story_info.comment = b"story metadata"
    story_info.external_attr = 0o100640 << 16
    story_info.create_system = 3
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = b"archive metadata"
        archive.writestr("mimetype", b"application/vnd.adobe.indesign-idml-package", compress_type=zipfile.ZIP_STORED)
        archive.writestr(story_info, story_xml)
        asset_info = zipfile.ZipInfo("Resources/asset.bin", (2021, 2, 3, 4, 5, 6))
        asset_info.compress_type = zipfile.ZIP_BZIP2
        asset_info.comment = b"asset metadata"
        archive.writestr(asset_info, b"asset payload")
        if unsafe_name is not None:
            archive.writestr(unsafe_name, b"unsafe")


def _member_bytes(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive, archive.open(name) as member:
        return member.read()


def _content_values(path: Path) -> list[str]:
    root = etree.fromstring(_member_bytes(path, "Stories/Story_stream.xml"))
    return [str(element.text or "") for element in root.iter("Content")]


def test_idml_export_streams_members_and_preserves_zip_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.idml"
    output = tmp_path / "translated.idml"
    _write_package(source, _story_xml(["one", "two"]))
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "Story_stream:p0": Data(
                source="one",
                target="un",
                extensions={"story": "Stories/Story_stream.xml"},
            ),
            "Story_stream:p1": Data(
                source="two",
                target="deux",
                extensions={"story": "Stories/Story_stream.xml"},
            ),
        },
    )

    def forbidden_read(self: zipfile.ZipFile, name: object, pwd: bytes | None = None) -> bytes:
        del self, name, pwd
        raise AssertionError("whole ZIP members must not be read into memory")

    def forbidden_parse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("whole Story trees must not be parsed")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_read)
    monkeypatch.setattr(etree, "parse", forbidden_parse)
    monkeypatch.setattr(etree, "tostring", forbidden_parse)
    idml_exporter.export_idml(document, output, source)

    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as translated:
        assert translated.comment == original.comment
        assert [info.filename for info in translated.infolist()] == [info.filename for info in original.infolist()]
        for name in ("mimetype", "Stories/Story_stream.xml", "Resources/asset.bin"):
            original_info = original.getinfo(name)
            translated_info = translated.getinfo(name)
            assert translated_info.compress_type == original_info.compress_type
            assert translated_info.date_time == original_info.date_time
            assert translated_info.comment == original_info.comment
            assert translated_info.external_attr == original_info.external_attr
            assert translated_info.create_system == original_info.create_system
        with translated.open("Stories/Story_stream.xml") as story:
            root = etree.fromstring(story.read())
        assert [str(element.text or "") for element in root.iter("Content")] == ["un", "deux"]
        with translated.open("Resources/asset.bin") as asset:
            assert asset.read() == b"asset payload"


def test_idml_streaming_multitarget_consumes_one_shot_input_once(tmp_path: Path) -> None:
    source = tmp_path / "source.idml"
    output_directory = tmp_path / "outputs"
    _write_package(source, _story_xml(["hello"]))

    def items() -> Iterator[ExtractItem]:
        yield (
            "Story_stream:p0",
            Data(
                source="hello",
                targets={
                    "fr": TargetData(text="bonjour"),
                    "de": TargetData(text="hallo"),
                },
                extensions={"story": "Stories/Story_stream.xml"},
            ),
        )

    one_shot = _OneShotItems(items)
    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("fr", "de"),
        items=one_shot,
    )

    idml_exporter.export_idml(document, output_directory, source)

    assert one_shot.iter_calls == 1
    assert one_shot.yielded == 1
    assert one_shot.closed
    assert _content_values(output_directory / "fr.idml") == ["bonjour"]
    assert _content_values(output_directory / "de.idml") == ["hallo"]


def test_idml_export_can_atomically_replace_its_source_package(tmp_path: Path) -> None:
    source = tmp_path / "in-place.idml"
    _write_package(source, _story_xml(["hello"]))
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "Story_stream:p0": Data(
                source="hello",
                target="bonjour",
                extensions={"story": "Stories/Story_stream.xml"},
            )
        },
    )

    idml_exporter.export_idml(document, source, source)

    assert _content_values(source) == ["bonjour"]


def test_idml_tag_reinsertion_survives_disk_spooling(tmp_path: Path) -> None:
    source = tmp_path / "source.idml"
    output = tmp_path / "translated.idml"
    story_xml = (
        b"<Story><ParagraphStyleRange><CharacterStyleRange><Content>plain</Content></CharacterStyleRange>"
        b'<CharacterStyleRange AppliedCharacterStyle="CharacterStyle/Bold"><Content>bold</Content>'
        b"</CharacterStyleRange></ParagraphStyleRange></Story>"
    )
    _write_package(source, story_xml)
    tag_map = {
        "open": TieData(
            id="open",
            type=TieType.CUSTOM_OPEN,
            attributes={"style": "CharacterStyle/Bold"},
            pair_id="pair",
        ),
        "close": TieData(id="close", type=TieType.CUSTOM_CLOSE, pair_id="pair"),
    }
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "Story_stream:p0": Data(
                source="plainbold",
                target="normalfort",
                tags=Tags(
                    source_tag_map=tag_map,
                    target_tag_map=tag_map,
                    source_parts=[TextPart("plain"), CodePart("open"), TextPart("bold"), CodePart("close")],
                    target_parts=[TextPart("normal"), CodePart("open"), TextPart("fort"), CodePart("close")],
                ),
                extensions={"story": "Stories/Story_stream.xml"},
            )
        },
    )

    idml_exporter.export_idml(document, output, source)

    root = etree.fromstring(_member_bytes(output, "Stories/Story_stream.xml"))
    values = {
        str(character.get("AppliedCharacterStyle") or ""): str(next(character.iter("Content")).text or "")
        for character in root.iter("CharacterStyleRange")
    }
    assert values[""] == "normal"
    assert values["CharacterStyle/Bold"] == "fort"


def test_idml_resolve_placeholders_option_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.idml"
    _write_package(source, _story_xml(["hello"]))
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "Story_stream:p0": Data(
                source="hello",
                target="input",
                extensions={"story": "Stories/Story_stream.xml"},
            )
        },
    )
    calls: list[str] = []

    def resolved(data: Data) -> Data:
        calls.append("resolve")
        return Data(source=data.source, target="resolved", extensions=data.extensions)

    def literal(data: Data) -> Data:
        calls.append("literal")
        return Data(source=data.source, target="literal", extensions=data.extensions)

    monkeypatch.setattr(idml_exporter, "resolve_data", resolved)
    monkeypatch.setattr(idml_exporter, "literalize_data", literal)
    resolved_output = tmp_path / "resolved.idml"
    literal_output = tmp_path / "literal.idml"
    idml_exporter.export_idml(document, resolved_output, source)
    idml_exporter.export_idml(document, literal_output, source, resolve_placeholders=False)

    assert calls == ["resolve", "literal"]
    assert _content_values(resolved_output) == ["resolved"]
    assert _content_values(literal_output) == ["literal"]


def test_idml_failure_closes_input_and_cleans_disk_and_atomic_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "broken.idml"
    output = tmp_path / "existing.idml"
    output.write_bytes(b"existing output")
    _write_package(source, b"<Story><ParagraphStyleRange>")

    def items() -> Iterator[ExtractItem]:
        yield (
            "Story_stream:p0",
            Data(
                source="hello",
                target="bonjour",
                extensions={"story": "Stories/Story_stream.xml"},
            ),
        )

    one_shot = _OneShotItems(items)
    document = StreamingStructure(source_locale="en", target_locale="fr", items=one_shot)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(etree.XMLSyntaxError):
        idml_exporter.export_idml(document, output, source)

    assert one_shot.iter_calls == 1
    assert one_shot.closed
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob("lokit-idml-export-*"))
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


@pytest.mark.asyncio
async def test_idml_async_cancellation_quiesces_worker_and_cleans_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.idml"
    output = tmp_path / "cancelled.idml"
    _write_package(source, _story_xml(["hello"]))
    started = threading.Event()
    release = threading.Event()

    def items() -> Iterator[ExtractItem]:
        started.set()
        if not release.wait(2):
            raise TimeoutError("test did not release the IDML input stream")
        yield (
            "Story_stream:p0",
            Data(
                source="hello",
                target="bonjour",
                extensions={"story": "Stories/Story_stream.xml"},
            ),
        )

    one_shot = _OneShotItems(items)
    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=one_shot,
    )
    task = asyncio.create_task(idml_exporter.export_idml_async(document, output, source))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    # Let run_cancellable_export publish its cancellation event before the
    # worker is released. This exercises the compiled public path without
    # relying on monkeypatching a mypyc early-bound module global.
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert one_shot.closed
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_idml_rejects_unsafe_package_and_output_paths(tmp_path: Path) -> None:
    unsafe_source = tmp_path / "unsafe.idml"
    output = tmp_path / "output.idml"
    _write_package(unsafe_source, _story_xml(["hello"]), unsafe_name="../escape")
    empty = BaseStructure(source_locale="en", target_locale="fr", data={})

    with pytest.raises(ValueError, match="unsafe IDML ZIP entry"):
        idml_exporter.export_idml(empty, output, unsafe_source)

    def no_items() -> Iterator[ExtractItem]:
        return iter(())

    one_shot = _OneShotItems(no_items)
    unsafe_locale_document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("../fr",),
        items=one_shot,
    )
    with pytest.raises(ValueError, match="unsafe IDML target locale"):
        idml_exporter.export_idml(unsafe_locale_document, tmp_path / "locales", unsafe_source)
    assert one_shot.iter_calls == 0


def test_idml_enforces_decompression_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.idml"
    output = tmp_path / "output.idml"
    _write_package(source, _story_xml(["hello"]))
    empty = BaseStructure(source_locale="en", target_locale="fr", data={})
    monkeypatch.setattr(idml_exporter, "_MAX_UNCOMPRESSED_BYTES", 8)

    with pytest.raises(ValueError, match="decompression limit"):
        idml_exporter.export_idml(empty, output, source)
    assert not output.exists()


def test_large_idml_stream_has_bounded_memory_and_throughput(tmp_path: Path) -> None:
    unit_count = 5_000
    source = tmp_path / "large.idml"
    output = tmp_path / "large-translated.idml"
    _write_package(source, _story_xml([f"source-{index}" for index in range(unit_count)]))

    def items() -> Iterator[ExtractItem]:
        for index in range(unit_count):
            yield (
                f"Story_stream:p{index}",
                Data(
                    source=f"source-{index}",
                    target=f"target-{index}",
                    extensions={"story": "Stories/Story_stream.xml"},
                ),
            )

    one_shot = _OneShotItems(items)
    document = StreamingStructure(source_locale="en", target_locale="fr", items=one_shot)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        idml_exporter.export_idml(document, output, source)
        elapsed = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert one_shot.iter_calls == 1
    assert one_shot.yielded == unit_count
    assert one_shot.closed
    assert output.exists()
    assert peak_bytes < 24 * 1024 * 1024
    assert unit_count / elapsed > 250
