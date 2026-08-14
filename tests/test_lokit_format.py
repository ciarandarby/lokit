from __future__ import annotations

import asyncio
import errno
import os
import stat
import threading
import time
from typing import TYPE_CHECKING

import pytest

import lokit
from lokit.data.structure import (
    AdjacentContext,
    BaseStructure,
    CodePart,
    Comment,
    Data,
    Meta,
    Origin,
    Plural,
    PluralCategory,
    StreamingStructure,
    Tags,
    TargetData,
    TargetTags,
    TextPart,
    TranslationStatus,
)
from lokit.data.tag_types import TieData, TieType
from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _complete_document() -> BaseStructure:
    source_tags = {
        "source-open-key": TieData(
            id="source-open-id",
            type=TieType.STRONG_OPEN,
            attributes={"class": "hero", "empty": ""},
            attribute_data="class=hero",
            position=0,
            order=0,
            pair_id="source-pair",
            original_name="strong",
            original_text="<strong>",
        ),
        "source-close-key": TieData(
            id="source-close-id",
            type=TieType.STRONG_CLOSE,
            position=8,
            order=2,
            pair_id="source-pair",
            original_name="strong",
            original_text="</strong>",
        ),
    }
    target_tags = {
        "target-code-key": TieData(
            id="target-code-id",
            type=TieType.BR,
            attribute_data="",
            position=0,
            order=0,
            pair_id="",
            original_name="br",
            original_text="",
        )
    }
    unit = Data(
        source='Hello\n"world" 🌍',
        target="",
        targets={
            "fr-FR": TargetData(
                text=None,
                status=TranslationStatus.NEW,
                tags=TargetTags(),
                plural=Plural(
                    variant="formes",
                    count=0,
                    category=PluralCategory.ZERO,
                    extensions={"plural-target": ""},
                ),
                meta=Meta(
                    usage_count=0,
                    last_used="2026-07-18T09:10:11Z",
                    first_used="",
                    created="2026-01-01",
                    updated="2026-07-19",
                    max_length=0,
                    min_length=0,
                    extensions={"target-meta": "value"},
                ),
                comments=[
                    Comment(
                        context="",
                        timestamp="",
                        origin=Origin(),
                        context_key="",
                        extensions={"target-comment": ""},
                    )
                ],
                extensions={"target-extension": ""},
            ),
            "de-DE": TargetData(text="", status=TranslationStatus.APPROVED),
        },
        plural=Plural(
            variant="Hello worlds",
            count=2,
            category=PluralCategory.OTHER,
            extensions={"plural": "source"},
        ),
        tags=Tags(
            source_tag_map=source_tags,
            target_tag_map=target_tags,
            source_parts=[
                CodePart("source-open-key"),
                TextPart("Hello"),
                CodePart("source-close-key"),
                TextPart(" world"),
            ],
            target_parts=[CodePart("target-code-key"), TextPart("")],
        ),
        meta=Meta(
            usage_count=7,
            last_used="2026-07-19T12:34:56Z",
            first_used="2025-01-02T03:04:05Z",
            created="2025-01-01T00:00:00Z",
            updated="",
            max_length=120,
            min_length=0,
            extensions={"quality": "gold", "empty-meta": ""},
        ),
        status=TranslationStatus.REVIEWED,
        comments=[
            Comment(
                context="Translator note",
                timestamp="2026-07-19T12:00:00Z",
                origin=Origin(
                    system="cms",
                    project="storefront",
                    creator_id="user-42",
                    extensions={"origin": "human"},
                ),
                context_key="homepage.hero",
                extensions={"audience": "public"},
            ),
            Comment(context="", origin=Origin()),
        ],
        previous_context=AdjacentContext(),
        next_context=AdjacentContext(
            unit_id="next-unit",
            source="Next",
            target="",
            extensions={"distance": "1"},
        ),
        extensions={"resource": "home", "empty-unit": ""},
    )
    return BaseStructure(
        source_locale="en-US",
        target_locale=None,
        data={
            "unit/complete": unit,
            "unit/minimal": Data(source="", tags=Tags()),
        },
        target_locales=("fr-FR", "de-DE"),
        format_version="0.9",
        export_origin="lokit-tests",
        export_timestamp="2026-07-19T13:14:15Z",
        source_language="en",
        target_language=None,
        target_languages=("fr", "de"),
        extensions={"input_format": "xliff", "empty-document": ""},
    )


def test_lokit_exact_complete_model_round_trip(tmp_path: Path) -> None:
    document = _complete_document()
    output = tmp_path / "complete.lokit"

    lokit.write.lokit(document, output)
    parsed = lokit.parse.lokit(str(output))

    assert parsed == document
    assert list(parsed.data) == list(document.data)
    assert list(parsed.data["unit/complete"].targets) == ["fr-FR", "de-DE"]
    assert parsed.extensions["input_format"] == "xliff"


def test_lokit_sparse_output_has_no_nulls_and_is_canonical(tmp_path: Path) -> None:
    document = BaseStructure(
        source_locale="",
        target_locale=None,
        data={"empty": Data(source="")},
    )
    first = tmp_path / "first.lokit"
    second = tmp_path / "second.lokit"

    lokit.export.lokit(document, first)
    reparsed = lokit.parse.lokit(str(first))
    lokit.write.lokit(reparsed, second)
    encoded = first.read_text(encoding="utf-8")

    assert reparsed == document
    assert encoded == second.read_text(encoding="utf-8")
    assert encoded.startswith("@lokit 1\ndocument {\n")
    assert encoded.endswith("\n")
    assert "null" not in encoded
    assert "target_locale" not in encoded
    assert "format_version" not in encoded
    assert 'source_locale = ""' in encoded
    assert 'source = ""' in encoded


def test_lokit_preserves_empty_optional_values_and_objects(tmp_path: Path) -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale="",
        data={
            "presence": Data(
                source="source",
                target="",
                targets={"fr": TargetData(text="", tags=TargetTags())},
                tags=Tags(),
                previous_context=AdjacentContext(),
                comments=[Comment(context="", origin=Origin())],
            )
        },
    )
    path = tmp_path / "presence.lokit"

    document.export.lokit(path)
    parsed = lokit.parse.lokit(str(path))

    assert parsed == document
    assert parsed.target_locale == ""
    assert parsed.data["presence"].target == ""
    assert parsed.data["presence"].tags == Tags()
    assert parsed.data["presence"].previous_context == AdjacentContext()
    assert parsed.data["presence"].targets["fr"].tags == TargetTags()


def test_lokit_detection_and_lokit_wrapper_round_trip(tmp_path: Path) -> None:
    document = _complete_document()
    path = tmp_path / "detected.lokit"
    lokit.parse.write.lokit(document, path)
    payload = path.read_bytes()

    assert detect_format(path) is LokitInputFormat.LOKIT
    assert detect_format_from_bytes(payload) is LokitInputFormat.LOKIT
    assert lokit.parse.file(str(path)) == document
    assert lokit.Lokit.parse(path).document == document
    assert lokit.Lokit.parse_bytes(payload).document == document


def test_lokit_byte_detection_scans_leading_comments_and_routes_versions() -> None:
    long_comment = b"\t# " + (b"comment,with,commas\t" * 80) + b"\n\n"
    payload = long_comment + b'@lokit 1\ndocument {\n  source_locale = "en"\n}\n'
    unsupported = long_comment + b'@lokit 2\ndocument {\n  source_locale = "en"\n}\n'

    assert len(long_comment) > 1000
    assert detect_format_from_bytes(payload) is LokitInputFormat.LOKIT
    assert lokit.Lokit.parse_bytes(payload).document.source_locale == "en"
    assert detect_format_from_bytes(unsupported) is LokitInputFormat.LOKIT
    with pytest.raises(ValueError, match="version"):
        lokit.Lokit.parse_bytes(unsupported)


@pytest.mark.parametrize("filename", ["messages", "messages.txt", "messages.json"])
def test_lokit_path_detection_uses_magic_with_missing_or_misleading_suffix(
    tmp_path: Path,
    filename: str,
) -> None:
    path = tmp_path / filename
    path.write_text(
        '# preliminary comment\n\n@lokit 1\ndocument {\n  source_locale = "en"\n}\n',
        encoding="utf-8",
    )

    assert detect_format(path) is LokitInputFormat.LOKIT
    assert lokit.Lokit.parse(path).document.source_locale == "en"


def test_lokit_native_reader_batches_and_lifecycle(tmp_path: Path) -> None:
    from lokit._interchange_rust import LokitReader

    document = _complete_document()
    path = tmp_path / "batches.lokit"
    lokit.write.lokit(document, path)
    reader = LokitReader(str(path))

    assert reader.source_locale == document.source_locale
    assert reader.target_locales == list(document.target_locales)
    assert reader.format_version == document.format_version
    assert reader.read_batch(1) == [("unit/complete", document.data["unit/complete"])]
    assert reader.read_batch(1) == [("unit/minimal", document.data["unit/minimal"])]
    assert reader.read_batch(1) == []
    reader.close()
    assert reader.closed
    with pytest.raises(RuntimeError, match="closed"):
        reader.read_batch(1)


def test_lokit_missing_input_preserves_python_filesystem_error_semantics(tmp_path: Path) -> None:
    missing = tmp_path / "missing.lokit"

    with pytest.raises(FileNotFoundError) as raised:
        lokit.parse.lokit(str(missing), progress=False)

    assert raised.value.errno == errno.ENOENT
    assert raised.value.filename == str(missing)


def test_lokit_streaming_reader_yields_valid_prefix_before_a_later_error(tmp_path: Path) -> None:
    from lokit._interchange_rust import LokitReader

    path = tmp_path / "valid-prefix-then-error.lokit"
    path.write_text(
        '@lokit 1\ndocument {\n  source_locale = "en"\n}\n'
        'unit "valid" {\n  source = "Valid source"\n}\n'
        'unit "invalid" {\n  source = "Invalid source"\n  status = impossible\n}\n',
        encoding="utf-8",
    )
    expected = ("valid", Data(source="Valid source"))
    reader = LokitReader(str(path))

    assert reader.read_batch(256) == [expected]
    with pytest.raises(ValueError) as raised:
        reader.read_batch(256)
    assert "line" in str(raised.value).lower()
    assert "column" in str(raised.value).lower()
    assert reader.closed

    streamed = iter(lokit.stream.lokit(str(path)).items)
    assert next(streamed) == expected
    with pytest.raises(ValueError):
        next(streamed)


def test_lokit_streaming_writer_does_not_materialize_input(tmp_path: Path) -> None:
    consumed: list[str] = []

    def items() -> Iterator[tuple[str, Data]]:
        for index in range(5):
            unit_id = f"unit-{index}"
            consumed.append(unit_id)
            yield unit_id, Data(source=f"Source {index}")

    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=items(),
    )
    path = tmp_path / "stream.lokit"

    assert consumed == []
    lokit.write.lokit(document, path)
    assert consumed == [f"unit-{index}" for index in range(5)]
    assert list(lokit.stream.lokit(str(path)).items) == [
        (f"unit-{index}", Data(source=f"Source {index}")) for index in range(5)
    ]


def test_lokit_failed_streaming_export_is_atomic(tmp_path: Path) -> None:
    output = tmp_path / "atomic.lokit"
    output.write_text("existing\n", encoding="utf-8")

    def failing_items() -> Iterator[tuple[str, Data]]:
        yield "before-error", Data(source="Source")
        raise RuntimeError("intentional writer failure")

    document = StreamingStructure(
        source_locale="en",
        target_locale=None,
        items=failing_items(),
    )

    with pytest.raises(RuntimeError, match="intentional writer failure"):
        lokit.write.lokit(document, output)

    assert output.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.glob(".atomic.lokit.*.tmp")) == []


def test_lokit_post_commit_directory_sync_failure_reports_committed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("directory fsync is unavailable on this platform")

    output = tmp_path / "directory-sync.lokit"
    output.write_text("old\n", encoding="utf-8")
    document = BaseStructure("en", None, {"new": Data(source="New")})
    original_fsync = os.fsync
    calls = 0

    def fail_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync failed"):
        lokit.export.lokit(document, output)

    assert calls == 2
    assert lokit.parse.lokit(str(output), progress=False) == document


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_lokit_atomic_export_preserves_or_applies_normal_file_mode(tmp_path: Path) -> None:
    document = BaseStructure("en", None, {"new": Data(source="New")})
    existing = tmp_path / "existing-mode.lokit"
    existing.write_text("old\n", encoding="utf-8")
    existing.chmod(0o640)

    lokit.export.lokit(document, existing)

    assert stat.S_IMODE(existing.stat().st_mode) == 0o640

    control = tmp_path / "umask-control"
    descriptor = os.open(control, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(descriptor)
    expected_new_mode = stat.S_IMODE(control.stat().st_mode)
    new_output = tmp_path / "new-mode.lokit"

    lokit.export.lokit(document, new_output)

    assert stat.S_IMODE(new_output.stat().st_mode) == expected_new_mode


@pytest.mark.asyncio
async def test_lokit_async_parse_stream_and_export(tmp_path: Path) -> None:
    document = _complete_document()
    path = tmp_path / "async.lokit"

    await lokit.export.async_.lokit(document, path)
    parsed_items = [item async for item in lokit.parse.async_.lokit(str(path))]
    streamed_items = [item async for item in lokit.stream.async_.lokit(str(path))]

    assert parsed_items == list(document.data.items())
    assert streamed_items == list(document.data.items())


@pytest.mark.asyncio
async def test_lokit_async_export_cancellation_quiesces_before_returning(tmp_path: Path) -> None:
    output = tmp_path / "cancelled-async-export.lokit"
    output.write_text("existing\n", encoding="utf-8")
    producer_started = threading.Event()

    def slow_items() -> Iterator[tuple[str, Data]]:
        for index in range(20):
            producer_started.set()
            time.sleep(0.01)
            yield f"unit-{index}", Data(source=f"Source {index}")

    document = StreamingStructure("en", None, slow_items())
    export_task = asyncio.create_task(lokit.export.async_.lokit(document, output))
    assert await asyncio.to_thread(producer_started.wait, 2.0)
    export_task.cancel()
    await asyncio.sleep(0)
    export_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await export_task

    settled_payload = output.read_bytes()
    await asyncio.sleep(0.25)
    assert output.read_bytes() == settled_payload == b"existing\n"
    assert list(tmp_path.glob(".cancelled-async-export.lokit.*.tmp")) == []

    recovery = tmp_path / "after-cancellation.lokit"
    expected = BaseStructure("en", None, {"unit": Data(source="Source")})
    await lokit.export.async_.lokit(expected, recovery)
    assert [item async for item in lokit.parse.async_.lokit(str(recovery))] == list(expected.data.items())


@pytest.mark.asyncio
async def test_lokit_async_reader_construction_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lokit._interchange_rust import LokitReader

    path = tmp_path / "async-reader-thread.lokit"
    lokit.export.lokit(BaseStructure("en", None, {"unit": Data(source="Source")}), path)
    original_reader = LokitReader
    constructed_threads: list[int] = []

    def recording_reader(filepath: str) -> LokitReader:
        constructed_threads.append(threading.get_ident())
        return original_reader(filepath)

    monkeypatch.setattr("lokit._interchange_rust.LokitReader", recording_reader)
    event_loop_thread = threading.get_ident()
    extraction = lokit.parse.async_.lokit(str(path))

    assert constructed_threads == []
    assert extraction._batch_iterator_factory is not None
    assert [item async for item in extraction] == [("unit", Data(source="Source"))]
    assert constructed_threads
    assert all(thread_id != event_loop_thread for thread_id in constructed_threads)


@pytest.mark.asyncio
async def test_lokit_async_bridge_consumes_native_sized_batches() -> None:
    from lokit.parsers.async_bridge import AsyncExtractionBridge

    batch_reads = 0

    def batches() -> Iterator[list[int]]:
        nonlocal batch_reads
        for start in range(0, 1_025, 256):
            batch_reads += 1
            yield list(range(start, min(start + 256, 1_025)))

    bridge = AsyncExtractionBridge.from_batches(batches)

    assert [item async for item in bridge] == list(range(1_025))
    assert batch_reads == 5


@pytest.mark.asyncio
async def test_lokit_async_stream_yields_valid_prefix_before_a_later_error(tmp_path: Path) -> None:
    path = tmp_path / "async-valid-prefix-then-error.lokit"
    path.write_text(
        '@lokit 1\ndocument {\n  source_locale = "en"\n}\n'
        'unit "valid" {\n  source = "Valid source"\n}\n'
        'unit "invalid" {\n  source = "Invalid source"\n  status = impossible\n}\n',
        encoding="utf-8",
    )
    received: list[tuple[str, Data]] = []

    with pytest.raises(ValueError):
        async for item in lokit.stream.async_.lokit(str(path)):
            received.append(item)

    assert received == [("valid", Data(source="Valid source"))]


@pytest.mark.asyncio
async def test_lokit_async_early_exit_has_bounded_worker_and_explicit_close(tmp_path: Path) -> None:
    path = tmp_path / "async-early-exit.lokit"
    document = BaseStructure(
        "en",
        None,
        {f"unit-{index}": Data(source=f"Source {index}") for index in range(3_000)},
    )
    lokit.export.lokit(document, path)

    resumable = lokit.stream.async_.lokit(str(path))
    assert await anext(resumable) == ("unit-0", Data(source="Source 0"))
    producer = resumable._producer
    assert producer is not None
    await asyncio.wait_for(asyncio.shield(producer), timeout=2.0)
    assert producer.done()
    await resumable.aclose()

    scoped = lokit.parse.async_.lokit(str(path))
    async with scoped:
        async for unit_id, _data in scoped:
            assert unit_id == "unit-0"
            break
    assert scoped._closed
    assert scoped._producer is None


@pytest.mark.asyncio
async def test_lokit_async_stream_crosses_multiple_producer_windows(tmp_path: Path) -> None:
    path = tmp_path / "async-multiple-windows.lokit"
    expected = [(f"unit-{index}", Data(source=f"Source {index}")) for index in range(4_097)]
    lokit.export.lokit(BaseStructure("en", None, dict(expected)), path)

    async def consume() -> list[tuple[str, Data]]:
        return [item async for item in lokit.stream.async_.lokit(str(path))]

    assert await asyncio.wait_for(consume(), timeout=5.0) == expected


@pytest.mark.asyncio
async def test_lokit_async_close_survives_caller_cancellation() -> None:
    from lokit.parsers.async_bridge import AsyncExtractionBridge

    producer_started = threading.Event()
    producer_release = threading.Event()
    iterator_closed = threading.Event()

    def items() -> Iterator[int]:
        try:
            producer_started.set()
            if not producer_release.wait(timeout=5.0):
                raise TimeoutError("test producer was not released")
            yield 1
        finally:
            iterator_closed.set()

    bridge = AsyncExtractionBridge(items, maxsize=1, batch_size=1)
    next_task = asyncio.create_task(anext(bridge))
    assert await asyncio.to_thread(producer_started.wait, 2.0)

    first_close = asyncio.create_task(bridge.aclose())
    await asyncio.sleep(0)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    next_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_task
    producer_release.set()
    await asyncio.wait_for(bridge.aclose(), timeout=2.0)

    assert iterator_closed.is_set()
    assert bridge._producer is None


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ('document {\n  source_locale = "en"\n}\n', "@lokit"),
        ('@lokit 2\ndocument {\n  source_locale = "en"\n}\n', "version"),
        ('@lokit 1\ndocument {\n  source_locale = "en"\n  source_locale = "fr"\n}\n', "duplicate"),
        (
            '@lokit 1\ndocument {\n  source_locale = "en"\n}\nunit "u" {\n  source = "source"\n  target = null\n}\n',
            "target",
        ),
        (
            '@lokit 1\ndocument {\n  source_locale = "en"\n}\n'
            'unit "u" {\n  source = "source"\n  status = impossible\n}\n',
            "status",
        ),
    ],
)
def test_lokit_malformed_documents_report_located_errors(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    path = tmp_path / "invalid.lokit"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        lokit.parse.lokit(str(path))

    error = str(raised.value).lower()
    assert message in error
    assert "line" in error
    assert "column" in error


def test_lokit_root_export_alias_is_public() -> None:
    expected = ["Lokit", "async_", "convert", "database", "export", "parse", "stream", "types", "write"]

    assert lokit.__all__ == expected
    assert dir(lokit) == expected
    assert callable(lokit.export.lokit)
    assert callable(lokit.export.async_.lokit)
    assert callable(lokit.async_.export.lokit)


def test_lokit_round_trips_every_enum_variant_and_integer_boundary(tmp_path: Path) -> None:
    tag_map = {
        tie_type.value: TieData(id=tie_type.value, type=tie_type, position=-(index + 1), order=index)
        for index, tie_type in enumerate(TieType)
    }
    data: dict[str, Data] = {
        f"status/{status.value}": Data(source=status.value, status=status) for status in TranslationStatus
    }
    for category in PluralCategory:
        data[f"plural/{category.value}"] = Data(
            source=category.value,
            plural=Plural(variant=category.value, category=category),
        )
    data["boundaries"] = Data(
        source="",
        tags=Tags(
            source_tag_map=tag_map,
            source_parts=[CodePart(key) for key in tag_map],
        ),
        meta=Meta(usage_count=-(1 << 63), max_length=(1 << 63) - 1),
    )
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data=data,
        target_locales=("fr", "fr", "de"),
        target_languages=("fr", "fr", "de"),
    )
    path = tmp_path / "enums-and-boundaries.lokit"

    lokit.export.lokit(document, path)

    assert lokit.parse.lokit(str(path), progress=False) == document


def test_lokit_round_trips_json_string_escapes_and_accepts_comments(tmp_path: Path) -> None:
    value = 'quote=" slash=\\ controls=\b\f\n\r\t\u0000 emoji=😀'
    document = BaseStructure(
        source_locale=value,
        target_locale=None,
        data={value: Data(source=value, target=value, extensions={value: value})},
    )
    canonical = tmp_path / "escapes.lokit"
    commented = tmp_path / "comments.lokit"

    lokit.write.lokit(document, canonical)
    source = canonical.read_text(encoding="utf-8")
    commented.write_text(
        "# envelope comment\n\n" + source.replace("document {\n", "document {\n  # metadata comment\n", 1),
        encoding="utf-8",
    )

    assert lokit.parse.lokit(str(canonical), progress=False) == document
    assert lokit.parse.lokit(str(commented), progress=False) == document
    assert detect_format_from_bytes(commented.read_bytes()) is LokitInputFormat.LOKIT
    assert "\\u0000" in source


def test_lokit_preserves_semantically_incomplete_inline_content(tmp_path: Path) -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={
            "draft-inline-edit": Data(
                source="unfinished",
                tags=Tags(source_parts=[CodePart("not-defined-yet")]),
            )
        },
    )
    path = tmp_path / "draft-inline-edit.lokit"

    lokit.write.lokit(document, path)

    assert lokit.parse.lokit(str(path), progress=False) == document


def test_lokit_empty_document_and_native_writer_lifecycle(tmp_path: Path) -> None:
    from lokit._interchange_rust import LokitWriter

    path = tmp_path / "native-writer.lokit"
    writer = LokitWriter(
        str(path),
        "en",
        None,
        (),
        "0.1",
        "",
        "",
        None,
        None,
        (),
        {},
    )
    writer.write("one", Data(source="One"))
    writer.close()
    writer.close()
    assert writer.closed
    with pytest.raises(RuntimeError, match="closed"):
        writer.write("two", Data(source="Two"))
    assert lokit.parse.lokit(str(path), progress=False).data == {"one": Data(source="One")}

    aborted_path = tmp_path / "aborted.lokit"
    aborted = LokitWriter(str(aborted_path), "en", None, (), "0.1", "", "", None, None, (), {})
    aborted.abort()
    aborted.abort()
    assert aborted.closed

    empty = BaseStructure(source_locale="en", target_locale=None, data={})
    empty_path = tmp_path / "empty.lokit"
    lokit.write.lokit(empty, empty_path)
    assert lokit.parse.lokit(str(empty_path), progress=False) == empty


def test_lokit_native_reader_rejects_invalid_batch_sizes(tmp_path: Path) -> None:
    from lokit._interchange_rust import LokitReader

    path = tmp_path / "batch-size.lokit"
    lokit.write.lokit(BaseStructure("en", None, {"one": Data(source="One")}), path)
    reader = LokitReader(str(path))
    try:
        with pytest.raises(ValueError, match="batch_size"):
            reader.read_batch(0)
        with pytest.raises(ValueError, match="batch_size"):
            reader.read_batch(16_385)
    finally:
        reader.close()


def test_lokit_oversized_output_failure_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "line-limit.lokit"
    path.write_text("existing\n", encoding="utf-8")
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={"oversized": Data(source="x" * (1024 * 1024))},
    )

    with pytest.raises(ValueError, match="maximum"):
        lokit.export.lokit(document, path)

    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.glob(".line-limit.lokit.*.tmp")) == []


def test_lokit_out_of_range_python_integer_failure_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "integer-limit.lokit"
    path.write_text("existing\n", encoding="utf-8")
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={"oversized": Data(source="source", meta=Meta(usage_count=1 << 63))},
    )

    with pytest.raises(OverflowError):
        lokit.export.lokit(document, path)

    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.glob(".integer-limit.lokit.*.tmp")) == []


def test_lokit_non_scalar_python_string_failure_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "unicode-scalar-limit.lokit"
    path.write_text("existing\n", encoding="utf-8")
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={"surrogate": Data(source="\ud800")},
    )

    with pytest.raises(UnicodeEncodeError):
        lokit.export.lokit(document, path)

    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.glob(".unicode-scalar-limit.lokit.*.tmp")) == []


@pytest.mark.parametrize(
    "contents",
    [
        b'@lokit 1\ndocument {\n  source_locale = "en"\n}\nunit "same" {\n  source = "a"\n}\n'
        b'unit "same" {\n  source = "b"\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "en"\n}\nunit "u" {\n  source = "a"\n'
        b'  target "fr" {\n  }\n  target "fr" {\n  }\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "en"\n}\nunit "u" {\n}\n',
        b'@lokit 1\ndocument {\n\tsource_locale = "en"\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "\\ud800"\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "en"\n}\nunit "u" {\n'
        b'  source = "a"\n  meta {\n    usage_count = 9223372036854775808\n  }\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "en"\n}\nunknown {\n}\n',
        b'@lokit 1\ndocument {\n  source_locale = "\xff"\n}\n',
    ],
)
def test_lokit_rejects_additional_malformed_and_invalid_utf8_inputs(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "malformed-extra.lokit"
    path.write_bytes(contents)

    with pytest.raises(ValueError) as raised:
        lokit.parse.lokit(str(path), progress=False)

    message = str(raised.value).lower()
    assert "lkt" in message
    assert "line" in message
    assert "column" in message
