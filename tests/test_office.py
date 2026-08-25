from __future__ import annotations

import asyncio
import io
import threading
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest

import lokit
from lokit.data.structure import BaseStructure, Data, StreamingStructure, TargetData
from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes
from lokit.office import (
    DocumentSource,
    export_docx,
    export_docx_async,
    export_pptx,
    export_pptx_async,
    import_docx,
    import_pptx,
    stream_docx,
    stream_pptx,
)
from lokit.office.errors import OfficeReinsertionError, OfficeUnsupportedPackageError
from lokit.office.options import OfficeExportOptions, OfficeImportOptions

if TYPE_CHECKING:
    from collections.abc import Iterator


class _ClosableItems(Protocol):
    def close(self) -> None: ...


DOCX_FIXTURE = Path("test_data/docx/Teleported Driving Hazard Report.docx")
PPTX_FIXTURE = Path("test_data/pptx/000528_workplan_timeline_powerpoint_template.pptx")


@pytest.fixture
def docx_fixture(tmp_path: Path) -> Path:
    if DOCX_FIXTURE.exists():
        return DOCX_FIXTURE
    path = tmp_path / "minimal.docx"
    _write_minimal_docx(path)
    return path


@pytest.fixture
def pptx_fixture(tmp_path: Path) -> Path:
    if PPTX_FIXTURE.exists():
        return PPTX_FIXTURE
    path = tmp_path / "minimal.pptx"
    _write_minimal_pptx(path)
    return path


def test_office_format_detection(docx_fixture: Path, pptx_fixture: Path) -> None:
    assert detect_format(docx_fixture) == LokitInputFormat.DOCX
    assert detect_format(pptx_fixture) == LokitInputFormat.PPTX
    assert detect_format_from_bytes(docx_fixture.read_bytes()) == LokitInputFormat.DOCX
    assert detect_format_from_bytes(pptx_fixture.read_bytes()) == LokitInputFormat.PPTX


def test_docx_import_and_roundtrip_export(docx_fixture: Path, tmp_path: Path) -> None:
    document = lokit.parse.docx(docx_fixture, source_locale="en", target_locale="fr", progress=False)
    from_bytes = lokit.parse.docx(docx_fixture.read_bytes(), source_locale="en", progress=False)

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "docx"
    assert len(document.data) >= 1
    assert len(from_bytes.data) == len(document.data)
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("docx:body:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "word/document.xml"

    document.data[first_unit_id].target = "Titre traduit DOCX"
    output = tmp_path / "translated.docx"
    result = lokit.office.export_docx(document, output, source_docx=docx_fixture)

    assert result.units_written == 1
    reparsed = lokit.parse.docx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit DOCX"


def test_pptx_import_and_roundtrip_export(pptx_fixture: Path, tmp_path: Path) -> None:
    document = lokit.parse.pptx(pptx_fixture, source_locale="en", target_locale="fr")

    assert document.source_locale == "en"
    assert document.target_locale == "fr"
    assert document.extensions["input_format"] == "pptx"
    assert len(document.data) >= 1
    first_unit_id = next(iter(document.data))
    assert first_unit_id.startswith("pptx:slide/1:p/")
    assert document.data[first_unit_id].extensions["office.part"] == "ppt/slides/slide1.xml"

    document.data[first_unit_id].target = "Titre traduit PPTX"
    output = tmp_path / "translated.pptx"
    lokit.parse.write.pptx(document, output, source_pptx=pptx_fixture)

    reparsed = lokit.parse.pptx(output, source_locale="fr", progress=False)
    assert reparsed.data[first_unit_id].source == "Titre traduit PPTX"


@pytest.mark.asyncio
async def test_office_async_imports(docx_fixture: Path, pptx_fixture: Path) -> None:
    docx_items = [
        item
        async for item in lokit.parse.async_.docx(
            docx_fixture,
            source_locale="en",
            target_locale="fr",
        )
    ]
    pptx_items = [
        item
        async for item in lokit.parse.async_.pptx(
            pptx_fixture,
            source_locale="en",
            target_locale="fr",
        )
    ]

    assert docx_items
    assert pptx_items
    assert docx_items[0][0].startswith("docx:")
    assert pptx_items[0][0].startswith("pptx:")


def test_office_fixture_parse_performance(docx_fixture: Path, pptx_fixture: Path) -> None:
    started = time.perf_counter()
    docx = lokit.parse.docx(docx_fixture, progress=False)
    pptx = lokit.parse.pptx(pptx_fixture, progress=False)
    elapsed = time.perf_counter() - started

    assert len(docx.data) >= 1
    assert len(pptx.data) >= 1
    assert elapsed < 5.0


def test_python_office_stream_yields_before_later_parts_are_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "incremental.docx"
    _write_minimal_docx(source)
    with zipfile.ZipFile(source, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/header1.xml", b"<w:hdr")
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")

    document = lokit.stream.docx(source, source_locale="en")
    items: Iterator[tuple[str, Data]] = iter(document.items)
    try:
        unit_id, data = next(items)
        assert unit_id == "docx:body:p/0"
        assert data.source == "Hello DOCX"
    finally:
        cast("_ClosableItems", items).close()


def test_python_office_export_streams_unmodified_zip_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "with-asset.docx"
    output = tmp_path / "translated.docx"
    _write_minimal_docx(source)
    asset = b"asset payload" * 1024
    with zipfile.ZipFile(source, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("word/media/asset.bin", asset)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = lokit.parse.docx(source, source_locale="en", target_locale="fr", progress=False)
    first = next(iter(document.data.values()))
    first.target = "Bonjour"

    def forbidden_read(self: zipfile.ZipFile, name: object, pwd: bytes | None = None) -> bytes:
        del self, name, pwd
        raise AssertionError("Office export must not load complete ZIP members")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_read)
    lokit.parse.write.docx(document, output, source_docx=source)

    with zipfile.ZipFile(output) as archive, archive.open("word/media/asset.bin") as member:
        assert member.read() == asset


def test_python_office_export_spools_bounded_stream_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "translated.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    closed = threading.Event()

    def translation_items() -> Iterator[tuple[str, Data]]:
        try:
            yield "docx:body:p/0", Data(source="Hello DOCX", target="Bonjour")
            yield "docx:body:p/1", Data(source="Second paragraph", target="Deuxième")
        finally:
            closed.set()

    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=translation_items(),
    )
    with pytest.raises(OfficeReinsertionError, match="max_translation_units"):
        export_docx(
            document,
            output,
            source_docx=source,
            options=OfficeExportOptions(max_translation_units=1),
        )

    assert closed.is_set()
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_python_office_export_accepts_bytes_source_and_binary_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"docx:body:p/0": Data(source="Hello DOCX", target="Bonjour")},
    )
    sink = io.BytesIO()

    result = export_docx(document, sink, source_docx=source.read_bytes())

    payload = sink.getvalue()
    assert result.output_path is None
    assert result.output_bytes == len(payload)
    assert result.units_written == 1
    reparsed = lokit.parse.docx(payload, source_locale="fr", progress=False)
    assert reparsed.data["docx:body:p/0"].source == "Bonjour"


def test_python_office_units_written_counts_only_consumed_translations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "translated.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={
            "docx:body:p/0": Data(source="Hello DOCX", target="Bonjour"),
            "docx:body:p/404": Data(source="extra", target="supplémentaire"),
        },
    )

    result = export_docx(document, output, source_docx=source)

    assert result.units_written == 1
    assert [warning.code for warning in result.warnings] == ["office.extra_translation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("file_format", ["docx", "pptx"])
async def test_async_office_export_cancellation_quiesces_and_cleans_output(
    file_format: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / f"source.{file_format}"
    output = tmp_path / f"translated.{file_format}"
    if file_format == "docx":
        _write_minimal_docx(source)
        unit_id = "docx:body:p/0"
        source_text = "Hello DOCX"
    else:
        _write_minimal_pptx(source)
        unit_id = "pptx:slide/1:p/0"
        source_text = "Hello PPTX"
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def translation_items() -> Iterator[tuple[str, Data]]:
        try:
            started.set()
            release.wait(timeout=5.0)
            yield unit_id, Data(source=source_text, target="Translated")
        finally:
            closed.set()

    document = StreamingStructure(
        source_locale="en",
        target_locale="fr",
        items=translation_items(),
    )
    if file_format == "docx":
        task = asyncio.create_task(export_docx_async(document, output, source_docx=source))
    else:
        task = asyncio.create_task(export_pptx_async(document, output, source_pptx=source))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


@pytest.mark.parametrize(
    "target_locales",
    [
        ("../escape", "fr"),
        ("bad\x00locale", "fr"),
        ("a" * 251,),
        ("fr", "FR"),
        ("é", "e\N{COMBINING ACUTE ACCENT}"),
        ("CON", "fr"),
    ],
)
def test_multitarget_office_output_rejects_unsafe_or_colliding_locale_filenames(
    target_locales: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "localized"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    targets = {locale: TargetData(text=f"Translated {index}") for index, locale in enumerate(target_locales)}
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={"docx:body:p/0": Data(source="Hello DOCX", targets=targets)},
        target_locales=target_locales,
    )

    with pytest.raises(OfficeReinsertionError):
        export_docx(document, output, source_docx=source)

    assert not output.exists()
    assert not (tmp_path / "escape.docx").exists()
    assert not list(tmp_path.glob(".localized.office-targets-*"))


def test_multitarget_office_staging_rolls_back_all_outputs_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "localized"
    output.mkdir()
    fr_output = output / "fr.docx"
    de_output = output / "de.docx"
    fr_output.write_bytes(b"existing-fr")
    de_output.write_bytes(b"existing-de")
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={
            "docx:body:p/0": Data(
                source="Hello DOCX",
                targets={
                    "fr": TargetData(text="Bonjour"),
                    "de": TargetData(text="invalid\x00translation"),
                },
            )
        },
        target_locales=("fr", "de"),
    )

    with pytest.raises(ValueError):
        export_docx(document, output, source_docx=source)

    assert fr_output.read_bytes() == b"existing-fr"
    assert de_output.read_bytes() == b"existing-de"
    assert not list(tmp_path.glob(".localized.office-targets-*"))


def test_office_explicit_target_locale_writes_single_file_from_multitarget_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "selected.docx"
    _write_minimal_docx(source)
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        data={
            "docx:body:p/0": Data(
                source="Hello DOCX",
                targets={
                    "fr": TargetData(text="Bonjour"),
                    "de": TargetData(text="Guten Tag"),
                },
            )
        },
        target_locales=("fr", "de"),
    )

    result = export_docx(document, output, source_docx=source, target_locale="de")

    assert result.output_path == output
    assert output.is_file()
    reparsed = lokit.parse.docx(output, source_locale="de", progress=False)
    assert reparsed.data["docx:body:p/0"].source == "Guten Tag"


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
@pytest.mark.parametrize("macro_marker", ["part", "content_type"])
def test_python_office_backend_rejects_macro_packages_for_all_source_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_format: str,
    macro_marker: str,
) -> None:
    source_path = tmp_path / f"disguised.{file_format}"
    _write_minimal_office(source_path, file_format)
    _add_macro_marker(source_path, file_format, macro_marker)
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")

    import_sources: tuple[DocumentSource, ...] = (
        source_path,
        source_bytes,
        io.BytesIO(source_bytes),
    )
    for source in import_sources:
        with pytest.raises(OfficeUnsupportedPackageError, match="Macro-enabled Office packages"):
            _import_office(source, file_format)

    stream_sources: tuple[DocumentSource, ...] = (
        source_path,
        source_bytes,
        io.BytesIO(source_bytes),
    )
    for source in stream_sources:
        with pytest.raises(OfficeUnsupportedPackageError, match="Macro-enabled Office packages"):
            list(_stream_office(source, file_format).items)


@pytest.mark.parametrize("file_format", ["docx", "pptx"])
@pytest.mark.parametrize("macro_marker", ["part", "content_type"])
def test_python_office_reinsertion_rejects_macro_sources_for_all_source_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_format: str,
    macro_marker: str,
) -> None:
    source_path = tmp_path / f"disguised.{file_format}"
    _write_minimal_office(source_path, file_format)
    _add_macro_marker(source_path, file_format, macro_marker)
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python")
    document = BaseStructure(source_locale="en", target_locale="fr", data={})
    sources: tuple[DocumentSource, ...] = (
        source_path,
        source_bytes,
        io.BytesIO(source_bytes),
    )

    for index, source in enumerate(sources):
        output = tmp_path / f"reinserted-{index}.{file_format}"
        with pytest.raises(OfficeUnsupportedPackageError, match="Macro-enabled Office packages"):
            _export_office(document, output, source, file_format)
        assert not output.exists()


def _import_office(source: DocumentSource, file_format: str) -> BaseStructure:
    options = OfficeImportOptions()
    if file_format == "docx":
        return import_docx(source, options=options, progress=False)
    return import_pptx(source, options=options, progress=False)


def _stream_office(source: DocumentSource, file_format: str) -> StreamingStructure:
    options = OfficeImportOptions()
    if file_format == "docx":
        return stream_docx(source, options=options)
    return stream_pptx(source, options=options)


def _export_office(
    document: BaseStructure,
    output: Path,
    source: DocumentSource,
    file_format: str,
) -> None:
    if file_format == "docx":
        export_docx(document, output, source_docx=source)
    else:
        export_pptx(document, output, source_pptx=source)


def _write_minimal_office(path: Path, file_format: str) -> None:
    if file_format == "docx":
        _write_minimal_docx(path)
    else:
        _write_minimal_pptx(path)


def _add_macro_marker(path: Path, file_format: str, macro_marker: str) -> None:
    if macro_marker == "part":
        prefix = "word" if file_format == "docx" else "ppt"
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{prefix}/VBAPROJECT.BIN", b"macro payload")
        return

    with zipfile.ZipFile(path, "r") as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    if file_format == "docx":
        safe_type = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        macro_type = b"application/vnd.ms-word.document.macroEnabled.main+xml"
    else:
        safe_type = b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
        macro_type = b"application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
    rewritten = False
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            if info.filename == "[Content_Types].xml":
                updated = data.replace(safe_type, macro_type)
                rewritten = updated != data
                data = updated
            archive.writestr(info, data)
    assert rewritten


def _write_minimal_docx(path: Path) -> None:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        "</Types>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)


def _write_minimal_pptx(path: Path) -> None:
    presentation = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></p:sldIdLst>
</p:presentation>
"""
    slide = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Hello PPTX</a:t></a:r></a:p></p:txBody></p:sp>
      <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Second slide text</a:t></a:r></a:p></p:txBody></p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>\n'
        '  <Override PartName="/ppt/slides/slide1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>\n'
        "</Types>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/slides/slide1.xml", slide)
