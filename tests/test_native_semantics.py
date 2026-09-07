from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, NoReturn

import pytest

from lokit import parse, stream
from lokit._interchange_rust import Reader
from lokit.data.structure import TranslationStatus
from lokit.importers import import_json_i18n

if TYPE_CHECKING:
    from pathlib import Path


def _write_xml_with_long_preamble(path: Path, format_name: str) -> None:
    preamble = '<?xml version="1.0"?><!-- ' + "x" * 8192 + " <not-the-root/> -->"
    if format_name == "tmx":
        contents = (
            '<tmx version="1.4"><header srclang="en"/><body><tu tuid="u"><note>Note</note>'
            '<tuv xml:lang="en"><seg>Source</seg></tuv></tu></body></tmx>'
        )
    else:
        contents = (
            '<xliff version="1.2"><file source-language="en"><body><trans-unit id="u">'
            "<source>Source</source><note>Note</note></trans-unit></body></file></xliff>"
        )
    path.write_text(preamble + contents, encoding="utf-8")


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.parametrize("progress", [False, True])
def test_explicit_xml_import_accepts_long_preamble(tmp_path: Path, format_name: str, progress: bool) -> None:
    path = tmp_path / f"preamble.{format_name}"
    _write_xml_with_long_preamble(path, format_name)
    document = parse.tmx(path, progress=progress) if format_name == "tmx" else parse.xliff(path, progress=progress)
    assert document.data["u"].source == "Source"
    assert document.data["u"].comments[0].context == "Note"
    streaming = stream.tmx(path) if format_name == "tmx" else stream.xliff(path)
    with streaming:
        units = list(streaming.items)
    assert [(unit_id, unit.source) for unit_id, unit in units] == [("u", "Source")]


def test_parallel_tmx_import_accepts_long_preamble(tmp_path: Path) -> None:
    path = tmp_path / "preamble.tmx"
    _write_xml_with_long_preamble(path, "tmx")
    document = parse.tmx_parallel(path, progress=False)
    assert document.data["u"].source == "Source"
    with stream.tmx_parallel(path) as streaming:
        assert [(unit_id, unit.source) for unit_id, unit in streaming.items] == [("u", "Source")]


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
def test_explicit_xml_import_still_rejects_wrong_root(tmp_path: Path, format_name: str) -> None:
    path = tmp_path / f"wrong.{format_name}"
    path.write_text("<wrong/>", encoding="utf-8")
    with pytest.raises(ValueError, match=r"[Ee]xpected.*XML root"):
        if format_name == "tmx":
            parse.tmx(path, progress=False)
        else:
            parse.xliff(path, progress=False)


@pytest.mark.parametrize("include_tags", [False, True])
@pytest.mark.asyncio
async def test_async_tmx_batches_are_lazy_closeable_and_bounded(tmp_path: Path, include_tags: bool) -> None:
    path = tmp_path / "lazy.tmx"
    batches = stream.async_.tmx_batches(path, batch_size=100_000, include_tags=include_tags)
    path.write_text(
        '<tmx version="1.4"><header srclang="en"/><body>'
        + "".join(f'<tu tuid="u{i}"><tuv xml:lang="en"><seg>Source {i}</seg></tuv></tu>' for i in range(150))
        + "</body></tmx>",
        encoding="utf-8",
    )
    ids: list[str] = []
    async with batches:
        async for batch in batches:
            assert 0 < len(batch) <= 64
            ids.extend(unit_id for unit_id, _ in batch)
    assert ids == [f"u{i}" for i in range(150)]
    with pytest.raises(StopAsyncIteration):
        await anext(batches)


@pytest.mark.parametrize("include_tags", [False, True])
@pytest.mark.asyncio
async def test_async_tmx_batch_error_preserves_prefix(tmp_path: Path, include_tags: bool) -> None:
    path = tmp_path / "broken.tmx"
    path.write_text(
        '<tmx version="1.4"><header srclang="en"/><body>'
        '<tu tuid="valid"><tuv xml:lang="en"><seg>Good</seg></tuv></tu>'
        '<tu tuid="bad"><tuv xml:lang="en"><seg>Bad</tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    batches = stream.async_.tmx_batches(path, include_tags=include_tags)
    async with batches:
        assert [unit_id for unit_id, _ in await anext(batches)] == ["valid"]
        with pytest.raises(ValueError):
            await anext(batches)
        with pytest.raises(StopAsyncIteration):
            await anext(batches)


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.asyncio
async def test_async_xml_preserves_native_batch_boundaries(tmp_path: Path, format_name: str) -> None:
    from lokit.parsers.tmx.extraction import TmxExtractor
    from lokit.parsers.xliff.extraction import XliffExtractor

    path = tmp_path / f"large.{format_name}"
    payload = "x" * (6 * 1024 * 1024)
    if format_name == "tmx":
        unit = f'<tu><tuv xml:lang="en"><seg>{payload}</seg></tuv></tu>'
        path.write_text(f'<tmx version="1.4"><header srclang="en"/><body>{unit * 4}</body></tmx>')
        extractor = TmxExtractor(str(path))
        bridge = extractor.extract_async(runtime_placeholders=False, inline_placeholders=False)
    else:
        units = "".join(f'<trans-unit id="{i}"><source>{payload}</source></trans-unit>' for i in range(4))
        path.write_text(f'<xliff version="1.2"><file source-language="en"><body>{units}</body></file></xliff>')
        xliff_extractor = XliffExtractor(str(path))
        bridge = xliff_extractor.extract_async(runtime_placeholders=False, inline_placeholders=False)
    del payload
    async with bridge:
        first = await anext(bridge)
        assert len(first[1].source) == 6 * 1024 * 1024
        assert bridge._batch_iterator_factory is not None
        assert len(bridge._current_batch) < 4
    if format_name == "tmx":
        assert extractor._native_reader is not None and extractor._native_reader.closed
    else:
        assert xliff_extractor._native_reader is not None and xliff_extractor._native_reader.closed


@pytest.mark.asyncio
async def test_async_tmx_callback_failure_closes_batch_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lokit.importers import TmxBatch, process_tmx_async

    path = tmp_path / "callback.tmx"
    path.write_text(
        '<tmx version="1.4"><header srclang="en"/><body>'
        + "".join(f'<tu tuid="u{i}"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>' for i in range(400))
        + "</body></tmx>"
    )
    readers: list[Reader] = []

    def open_reader(
        filepath: str,
        format_name: str,
        source_locale: str | None = None,
        target_locale: str | None = None,
        mode: str = "full",
    ) -> Reader:
        reader = Reader(filepath, format_name, source_locale, target_locale, mode)
        readers.append(reader)
        return reader

    monkeypatch.setattr("lokit._interchange_rust.Reader", open_reader)

    async def fail(batch: TmxBatch) -> None:
        assert batch
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await process_tmx_async(path, fail, batch_size=1)
    assert readers and all(reader.closed for reader in readers)


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.asyncio
async def test_async_export_uses_native_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_name: str
) -> None:
    from lokit import _interchange_rust as native
    from lokit import export
    from lokit.data.structure import BaseStructure, Data

    original = native.export_base_interchange
    calls: list[str] = []

    def write(document: BaseStructure, target_path: str, output_format: str) -> int | None:
        calls.append(output_format)
        return original(document, target_path, output_format)

    monkeypatch.setattr(native, "export_base_interchange", write)
    document = BaseStructure(source_locale="en", target_locale="fr", data={"u": Data(source="A", target="B")})
    synchronous = tmp_path / f"sync.{format_name}"
    asynchronous = tmp_path / f"async.{format_name}"
    if format_name == "tmx":
        export.tmx(document, synchronous)
        await export.async_.tmx(document, asynchronous)
    else:
        export.xliff(document, synchronous)
        await export.async_.xliff(document, asynchronous)
    assert calls == [format_name, format_name]
    assert synchronous.read_bytes() == asynchronous.read_bytes()


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
@pytest.mark.asyncio
async def test_cancelled_native_export_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_name: str
) -> None:
    import asyncio
    import threading

    from lokit import _interchange_rust as native
    from lokit import export
    from lokit.data.structure import BaseStructure, Data

    original = native.export_base_interchange
    started = threading.Event()
    release = threading.Event()

    def write(document: BaseStructure, target_path: str, output_format: str) -> int | None:
        started.set()
        assert release.wait(5.0)
        return original(document, target_path, output_format)

    monkeypatch.setattr(native, "export_base_interchange", write)
    document = BaseStructure(source_locale="en", target_locale="fr", data={"u": Data(source="A", target="B")})
    output = tmp_path / f"cancel.{format_name}"
    output.write_bytes(b"existing")
    operation = export.async_.tmx(document, output) if format_name == "tmx" else export.async_.xliff(document, output)
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert output.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("progress", [False, True])
def test_custom_pathlike_and_paired_payloads(tmp_path: Path, progress: bool) -> None:
    source = tmp_path / "paired.xlf"
    source.write_text(
        '<xliff version="2.0" srcLang="en" trgLang="fr"><file id="f"><unit id="u">'
        '<originalData><data id="a">&lt;b&gt;</data><data id="b">&lt;/b&gt;</data></originalData>'
        '<segment id="s"><source><pc id="p" dataRefStart="a" dataRefEnd="b">A</pc></source>'
        "<target>B</target></segment></unit></file></xliff>",
        encoding="utf-8",
    )

    class InputPath:
        def __fspath__(self) -> str:
            return str(source)

    unit = parse.xliff(InputPath(), progress=progress, inline_placeholders=False).data["u:s"]
    assert unit.tags is not None
    assert [code.original_text for code in unit.tags.source_tag_map.values()] == ["<b>", "</b>"]


def test_xml_bytes_do_not_use_temporary_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from lokit import Lokit

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("XML bytes must stay in memory")

    monkeypatch.setattr("tempfile.TemporaryDirectory", forbidden)
    document = Lokit.parse_bytes(
        b'<xliff version="1.2"><file source-language="en"><body><trans-unit id="u">'
        b"<source>A</source><note>Note</note></trans-unit></body></file></xliff>",
        format_hint="xliff",
    ).document
    assert document.data["u"].comments[0].context == "Note"


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
def test_native_byte_input_ownership_and_parallel_reads(tmp_path: Path, format_name: str) -> None:
    from lokit._interchange_rust import materialize_interchange_bytes

    path = tmp_path / f"input.{format_name}"
    _write_xml_with_long_preamble(path, format_name)
    payload = path.read_bytes()
    references = sys.getrefcount(payload)

    def read() -> str:
        document = materialize_interchange_bytes(payload, format_name)
        unit = document.data["u"]
        assert unit.comments[0].context == "Note"
        return unit.source

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(read) for _ in range(12)]
        assert [future.result() for future in futures] == ["Source"] * 12
    assert sys.getrefcount(payload) == references
    assert payload == path.read_bytes()


@pytest.mark.parametrize("format_name", ["tmx", "xliff"])
def test_native_byte_input_released_after_error(format_name: str) -> None:
    from lokit._interchange_rust import materialize_interchange_bytes

    payload = f"<{format_name}><unfinished>".encode()
    references = sys.getrefcount(payload)
    for _ in range(3):
        with pytest.raises(ValueError):
            materialize_interchange_bytes(payload, format_name)
    assert sys.getrefcount(payload) == references


@pytest.mark.parametrize("progress", [False, True])
def test_xliff_nested_groups_keep_distinct_context(tmp_path: Path, progress: bool) -> None:
    source = tmp_path / "groups.xlf"
    source.write_text(
        '<xliff version="2.0" srcLang="en" trgLang="fr"><file id="f">'
        '<group id="outer" translate="no"><group id="inner" translate="yes">'
        '<unit id="u"><segment id="s"><source>A</source><target>B</target></segment></unit>'
        "</group></group></file></xliff>",
        encoding="utf-8",
    )
    unit = parse.xliff(source, progress=progress).data["u:s"]
    assert unit.extensions["xliff.group.0.id"] == "outer"
    assert unit.extensions["xliff.group.0.translate"] == "no"
    assert unit.extensions["xliff.group.1.id"] == "inner"
    assert unit.extensions["xliff.group.1.translate"] == "yes"
    assert unit.extensions["xliff.group.translate"] == "yes"


def test_projection_error_preserves_valid_batch_prefix(tmp_path: Path) -> None:
    source = tmp_path / "limits.tmx"
    source.write_text(
        '<tmx><header srclang="en"/><body><tu tuid="good"><tuv xml:lang="en"><seg>Good</seg></tuv></tu>'
        '<tu tuid="bad"><tuv xml:lang="en"><seg>' + "%s " * 4097 + "</seg></tuv></tu></body></tmx>",
        encoding="utf-8",
    )
    reader = Reader(str(source), "tmx")
    assert [key for key, _ in reader.read_data_batch(runtime_placeholders=True)] == ["good"]
    with pytest.raises(ValueError):
        reader.read_data_batch(runtime_placeholders=True)
    assert reader.closed


def test_native_model_batches_bound_residency_without_losing_units(tmp_path: Path) -> None:
    source = tmp_path / "batches.tmx"
    source.write_text(
        '<tmx><header srclang="en"/><body>'
        + "".join(f'<tu tuid="u{i}"><tuv xml:lang="en"><seg>A</seg></tuv></tu>' for i in range(257))
        + "</body></tmx>",
        encoding="utf-8",
    )
    reader = Reader(str(source), "tmx")
    keys: list[str] = []
    try:
        while batch := reader.read_data_batch(batch_size=1024):
            assert len(batch) <= 64
            keys.extend(key for key, _ in batch)
        assert keys == [f"u{i}" for i in range(257)]
    finally:
        reader.close()


def test_inline_payload_expansion_error_preserves_the_valid_prefix(tmp_path: Path) -> None:
    source = tmp_path / "expanded.xlf"
    source.write_text(
        '<xliff version="2.0" srcLang="en"><file id="f">'
        '<unit id="good"><segment id="s"><source>A</source></segment></unit>'
        '<unit id="large"><originalData><data id="d">'
        + "x" * (2 * 1024 * 1024)
        + '</data></originalData><segment id="s"><source>'
        + '<ph dataRef="d"/>' * 20
        + "</source></segment></unit></file></xliff>",
        encoding="utf-8",
    )
    reader = Reader(str(source), "xliff")
    assert [key for key, _ in reader.read_data_batch()] == ["good:s"]
    with pytest.raises(ValueError, match="expanded semantic data"):
        reader.read_data_batch()
    assert reader.closed


@pytest.mark.parametrize("status", [TranslationStatus.DRAFT, TranslationStatus.REJECTED])
@pytest.mark.parametrize("proxy", [False, True])
def test_xliff_preserves_status_distinctions(tmp_path: Path, status: TranslationStatus, proxy: bool) -> None:
    from lokit import export
    from lokit.data.structure import BaseStructure, Data

    document = BaseStructure(
        source_locale="en", target_locale="fr", data={"u": Data(source="A", target="B", status=status)}
    )
    output = tmp_path / "status.xlf"
    if proxy:
        document.export.xliff(output)
    else:
        export.xliff(document, output)
    for progress in (False, True):
        assert parse.xliff(output, progress=progress).data["u"].status is status


@pytest.mark.asyncio
async def test_async_document_finalizes_metadata_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lokit.format_detection import LokitInputFormat, detect_format

    source = tmp_path / "languages.tmx"
    source.write_text(
        '<tmx><header srclang="en"/><body><tu tuid="u"><tuv xml:lang="en"><seg>A</seg></tuv>'
        '<tuv xml:lang="fr"><seg>B</seg></tuv><tuv xml:lang="de"><seg>C</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    caller = threading.get_ident()

    def detect(path: str) -> LokitInputFormat:
        assert threading.get_ident() != caller
        return detect_format(path)

    monkeypatch.setattr("lokit.importers.detect_format", detect)
    document = await parse.async_.document(source)
    assert document.target_locales == ("fr", "de")
    assert tuple(document.data["u"].targets) == ("fr", "de")


def test_csv_multiline_quoted_fields_cross_input_buffers(tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    text = 'é,"quoted"\n' * 9000
    source.write_text('id,en,fr\nu,"' + text.replace('"', '""') + '",Cible\n', encoding="utf-8")
    document = parse.csv(source, progress=False)
    assert document.data["u"].source == text


def test_csv_invalid_utf8_preserves_valid_prefix(tmp_path: Path) -> None:
    source = tmp_path / "invalid.csv"
    source.write_bytes(b"id,en,fr\nu,Source,Cible\nbad,\xff,Target\n")
    with stream.csv(source) as document:
        items = iter(document.items)
        assert next(items)[0] == "u"
        with pytest.raises(ValueError, match="UTF-8"):
            next(items)


def test_native_rows_continue_after_filtered_batches(tmp_path: Path) -> None:
    source = tmp_path / "rows.tmx"
    with source.open("w", encoding="utf-8") as output:
        output.write('<tmx><header srclang="en"/><body>')
        for index in range(300):
            locale = "de-DE" if index == 299 else "fr"
            output.write(
                f'<tu tuid="u{index}"><tuv xml:lang="en"><seg>Source</seg></tuv>'
                f'<tuv xml:lang="{locale}"><seg>Target</seg></tuv></tu>'
            )
        output.write("</body></tmx>")
    reader = Reader(str(source), "tmx")
    try:
        assert reader.read_row_batch(["unit_id", "target_locale"], target_language="de") == [
            {"unit_id": "u299", "target_locale": "de-DE"}
        ]
        assert reader.read_row_batch(["unit_id"], target_language="de") == []
    finally:
        reader.close()


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("placeholders", [False, True])
def test_xliff_scoped_metadata_and_original_data(tmp_path: Path, progress: bool, placeholders: bool) -> None:
    source = tmp_path / "scoped.xlf"
    source.write_text(
        '<xliff version="2.0" srcLang="en" trgLang="fr"><file id="f"><group id="g" translate="no">'
        '<unit id="u" translate="no"><notes><note id="n" category="instruction" priority="2"> Keep this </note></notes>'
        '<originalData><data id="d">&lt;br&gt;</data></originalData>'
        '<segment id="s" state="final" subState="vendor:approved"><source>A<ph id="p" dataRef="d"/></source>'
        '<target>B<ph id="p" dataRef="d"/></target></segment>'
        '<ignorable id="i"><source> </source><target> </target></ignorable>'
        "</unit></group></file></xliff>",
        encoding="utf-8",
    )
    document = parse.xliff(str(source), progress=progress, runtime_placeholders=placeholders, inline_placeholders=False)
    unit = document.data["u:s"]
    assert unit.source == "A"
    assert unit.status is TranslationStatus.APPROVED
    assert unit.extensions["xliff.unit.translate"] == "no"
    assert unit.extensions["xliff.group.translate"] == "no"
    assert unit.extensions["xliff.segment.subState"] == "vendor:approved"
    assert unit.extensions["xliff.originalData.d"] == "<br>"
    assert unit.comments[0].context == " Keep this "
    assert unit.comments[0].extensions["xliff.note.id"] == "n"
    assert unit.comments[0].extensions["xliff.note.category"] == "instruction"
    assert unit.comments[0].extensions["xliff.note.priority"] == "2"
    assert unit.tags is not None
    assert unit.tags.source_tag_map["c0"].original_text == "<br>"
    assert document.data["u:i"].source == " "
    assert document.data["u:i"].extensions["xliff.ignorable"] == "true"


@pytest.mark.parametrize("progress", [False, True])
def test_tmx_variant_metadata(tmp_path: Path, progress: bool) -> None:
    source = tmp_path / "variants.tmx"
    source.write_text(
        '<tmx><header srclang="en"/><body><tu tuid="u" creationdate="20260101T000000Z">'
        '<tuv xml:lang="en" creationid="author"><seg>Source</seg><note>Source note</note></tuv>'
        '<tuv xml:lang="fr" changeid="reviewer" changedate="20260201T000000Z">'
        '<prop type="status">approved</prop><prop type="vendor-key">value</prop>'
        "<note> French note </note><seg>Cible</seg>"
        "</tuv></tu></body></tmx>",
        encoding="utf-8",
    )
    document = parse.tmx(str(source), progress=progress)
    unit = document.data["u"]
    target = unit.targets["fr"]
    assert target.text == "Cible"
    assert target.status is TranslationStatus.APPROVED
    assert target.meta.updated == "20260201T000000Z"
    assert target.meta.extensions["changeid"] == "reviewer"
    assert target.extensions["property.vendor_key"] == "value"
    assert target.comments[0].context == " French note "
    assert unit.extensions["tmx.source.creationid"] == "author"
    assert unit.status is TranslationStatus.UNKNOWN


def test_native_data_batch_preserves_prefix_before_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.xlf"
    source.write_text(
        '<xliff version="1.2"><file source-language="en"><body>'
        '<trans-unit id="a"><source>A</source><note>N</note></trans-unit>'
        '<trans-unit id="bad"><source>B</target></body></file></xliff>',
        encoding="utf-8",
    )
    reader = Reader(str(source), "xliff")
    assert reader.read_data_batch()[0][1].comments[0].context == "N"
    with pytest.raises(ValueError):
        reader.read_data_batch()
    assert reader.closed


def test_rich_stream_models_are_final_before_yield(tmp_path: Path) -> None:
    source = tmp_path / "family.xlf"
    source.write_text(
        '<xliff version="1.2"><file source-language="en" target-language="fr"><body>'
        '<group restype="x-gettext-plurals"><trans-unit id="apple[0]"><source>apple</source>'
        '<target>pomme</target><note>N</note></trans-unit><trans-unit id="apple[1]">'
        "<source>apples</source><target>pommes</target></trans-unit></group></body></file></xliff>",
        encoding="utf-8",
    )
    with stream.xliff(str(source)) as document:
        unit = next(iter(document.items))[1]
        assert unit.plural is not None
        assert unit.plural.variant == "apples"


@pytest.mark.parametrize(
    "payload",
    [
        '{"hello":"old","hello":"new"}',
        '{"removed":"text","removed":42}',
        '{"a":{"b":"old"},"a":{"c":"new"}}',
        '{"a":{"b":"old","b":"new"}}',
        '{"en":{"a":"old"},"en":{"a":"new"},"fr":{"a":"cible"}}',
    ],
)
def test_json_duplicates_rejected_with_location(tmp_path: Path, payload: str) -> None:
    source = tmp_path / "en.json"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=r"Duplicate JSON member.*JSON byte"):
        import_json_i18n(str(source), source_locale="en", progress=False)
