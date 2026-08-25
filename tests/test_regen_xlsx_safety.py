from __future__ import annotations

import asyncio
import threading
import time
import tracemalloc
import zipfile
from typing import TYPE_CHECKING, Protocol

import pytest
from lxml import etree

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.exporters import regen as regen_exporter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class _RegenRunner(Protocol):
    def __call__(self, document: StreamingStructure, source: Path, output: Path) -> None: ...


def _shared_string_package(path: Path, *, rows: int = 1, asset: bytes = b"") -> None:
    strings = ["id", "en", "fr"]
    worksheet_rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
    ]
    for row_index in range(rows):
        first_string = len(strings)
        strings.extend((f"u{row_index}", f"Source {row_index}", f"Old {row_index}"))
        spreadsheet_row = row_index + 2
        worksheet_rows.append(
            f'<row r="{spreadsheet_row}"><c r="A{spreadsheet_row}" t="s"><v>{first_string}</v></c>'
            f'<c r="B{spreadsheet_row}" t="s"><v>{first_string + 1}</v></c>'
            f'<c r="C{spreadsheet_row}" t="s"><v>{first_string + 2}</v></c></row>'
        )

    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{SHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{PACKAGE_REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{SHEET_NS}"><sheetData>{"".join(worksheet_rows)}</sheetData></worksheet>'
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{SHEET_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in strings)
        + "</sst>"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.comment = b"preserve this comment"
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        if asset:
            archive.writestr("xl/media/asset.bin", asset, compress_type=zipfile.ZIP_STORED)


def _target_cell(path: Path, reference: str) -> str:
    with zipfile.ZipFile(path, "r") as archive, archive.open("xl/worksheets/sheet1.xml") as member:
        root = etree.parse(member).getroot()
    cell = root.find(f'.//{{{SHEET_NS}}}c[@r="{reference}"]')
    assert cell is not None
    return "".join(text.text or "" for text in cell.findall(f".//{{{SHEET_NS}}}t"))


def test_regen_xlsx_streams_worksheet_shared_strings_and_untouched_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = 2_000
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    asset = b"asset" * (1024 * 1024)
    _shared_string_package(source, rows=rows, asset=asset)
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={f"u{index}": Data(source=f"Source {index}", target=f"Translated {index}") for index in range(rows)},
    )

    def forbidden_read(self: zipfile.ZipFile, name: object, pwd: bytes | None = None) -> bytes:
        raise AssertionError(f"XLSX regeneration must not whole-read ZIP members: {name!r}, {pwd!r}")

    with monkeypatch.context() as scoped:
        scoped.setattr(zipfile.ZipFile, "read", forbidden_read)
        tracemalloc.start()
        started = time.perf_counter()
        regen_exporter.regen_xlsx(document, source, output)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert peak < 12_000_000
    assert elapsed < 8.0
    assert _target_cell(output, "C2") == "Translated 0"
    assert _target_cell(output, f"C{rows + 1}") == f"Translated {rows - 1}"
    with zipfile.ZipFile(output, "r") as archive:
        assert archive.comment == b"preserve this comment"
        assert archive.getinfo("xl/media/asset.bin").compress_type == zipfile.ZIP_STORED
        with archive.open("xl/media/asset.bin") as member:
            assert member.read() == asset


def test_regen_xlsx_rejects_unsafe_or_duplicate_members_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe.xlsx"
    output = tmp_path / "output.xlsx"
    _shared_string_package(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("../escape", b"unsafe")
    output.write_bytes(b"existing output")
    document = BaseStructure(source_locale="en", target_locale="fr", data={})

    with pytest.raises(ValueError, match="unsafe XLSX ZIP entry"):
        regen_exporter.regen_xlsx(document, source, output)

    assert output.read_bytes() == b"existing output"

    duplicate = tmp_path / "duplicate.xlsx"
    _shared_string_package(duplicate)
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(duplicate, "a") as archive,
    ):
        archive.writestr("xl/workbook.xml", b"<duplicate/>")
    with pytest.raises(ValueError, match="duplicate XLSX ZIP entry"):
        regen_exporter.regen_xlsx(document, duplicate, output)
    assert output.read_bytes() == b"existing output"


def test_regen_xlsx_bounded_metadata_read_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    _shared_string_package(source)
    output.write_bytes(b"existing output")
    document = BaseStructure(source_locale="en", target_locale="fr", data={})
    monkeypatch.setattr(regen_exporter, "_MAX_WORKBOOK_XML_BYTES", 32)

    with pytest.raises(ValueError, match=r"workbook\.xml exceeds its decompression limit"):
        regen_exporter.regen_xlsx(document, source, output)

    assert output.read_bytes() == b"existing output"


def test_regen_xlsx_supports_atomic_same_path_replacement(tmp_path: Path) -> None:
    workbook = tmp_path / "same-path.xlsx"
    _shared_string_package(workbook)
    document = BaseStructure(
        source_locale="en",
        target_locale="fr",
        data={"u0": Data(source="Source 0", target="Translated")},
    )

    regen_exporter.regen_xlsx(document, workbook, workbook)

    assert _target_cell(workbook, "C2") == "Translated"


def test_regen_xlsx_closes_surplus_stream_items(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    _shared_string_package(source)
    closed = threading.Event()

    def items() -> Iterator[tuple[str, Data]]:
        try:
            yield "u0", Data(source="Source 0", target="Translated")
            yield "surplus", Data(source="unused", target="unused")
        finally:
            closed.set()

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    regen_exporter.regen_xlsx(document, source, output)

    assert closed.is_set()
    assert _target_cell(output, "C2") == "Translated"


@pytest.mark.asyncio
async def test_regen_xlsx_async_cancellation_quiesces_and_closes_stream(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    _shared_string_package(source)
    output.write_bytes(b"existing output")
    started = threading.Event()
    closed = threading.Event()

    def items() -> Iterator[tuple[str, Data]]:
        try:
            started.set()
            time.sleep(0.05)
            yield "u0", Data(source="Source 0", target="Translated")
            yield "surplus", Data(source="unused", target="unused")
        finally:
            closed.set()

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    task = asyncio.create_task(regen_exporter.regen_xlsx_async(document, source, output))
    assert await asyncio.to_thread(started.wait, 1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert output.read_bytes() == b"existing output"


def _run_csv(document: StreamingStructure, source: Path, output: Path) -> None:
    regen_exporter.regen_csv(document, source, output)


def _run_xliff(document: StreamingStructure, source: Path, output: Path) -> None:
    regen_exporter.regen_xliff(document, source, output)


def _run_tmx(document: StreamingStructure, source: Path, output: Path) -> None:
    regen_exporter.regen_tmx(document, source, output)


def _run_po(document: StreamingStructure, source: Path, output: Path) -> None:
    regen_exporter.regen_po(document, source, output)


_TEXT_REGEN_CASES: tuple[tuple[str, str, _RegenRunner], ...] = (
    ("csv", "id,en,fr\none,Source,Old\n", _run_csv),
    (
        "xliff",
        '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
        '<file source-language="en" target-language="fr"><body><trans-unit id="one">'
        "<source>Source</source><target>Old</target></trans-unit></body></file></xliff>",
        _run_xliff,
    ),
    (
        "tmx",
        '<tmx version="1.4"><header srclang="en"/><body><tu tuid="one">'
        '<tuv xml:lang="en"><seg>Source</seg></tuv><tuv xml:lang="fr"><seg>Old</seg></tuv>'
        "</tu></body></tmx>",
        _run_tmx,
    ),
    ("po", 'msgid "one"\nmsgstr "Old"\n', _run_po),
)


@pytest.mark.parametrize(("suffix", "payload", "runner"), _TEXT_REGEN_CASES)
def test_text_regen_closes_surplus_stream_items(
    suffix: str,
    payload: str,
    runner: _RegenRunner,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"source.{suffix}"
    output = tmp_path / f"output.{suffix}"
    source.write_text(payload, encoding="utf-8")
    closed = threading.Event()

    def items() -> Iterator[tuple[str, Data]]:
        try:
            yield "one", Data(source="Source", target="Translated")
            yield "surplus", Data(source="unused", target="unused")
        finally:
            closed.set()

    document = StreamingStructure(source_locale="en", target_locale="fr", items=items())
    runner(document, source, output)

    assert closed.is_set()
