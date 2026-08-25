from __future__ import annotations

import stat
import zipfile
from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import BaseStructure, Data
from lokit.exporters.html import export_html
from lokit.importers import _collect_items, import_xlsx
from lokit.parsers.po import stream as po_stream
from lokit.parsers.po.extraction import _PoIdIndex

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _FailingItems:
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[tuple[str, Data]]:
        try:
            yield "first", Data(source="First")
            raise RuntimeError("injected parser failure")
        finally:
            self.closed = True


def test_collect_items_closes_source_after_failure() -> None:
    items = _FailingItems()

    with pytest.raises(RuntimeError, match="injected parser failure"):
        _collect_items(items, "test", progress=False)

    assert items.closed


def test_po_id_index_spills_but_preserves_collision_order() -> None:
    with _PoIdIndex() as index:
        for number in range(4097):
            assert index.unique(f"unit-{number}") == f"unit-{number}"
        assert index._connection is not None
        assert index.unique("unit-0#2") == "unit-0#2"
        assert index.unique("unit-0") == "unit-0#3"


def test_po_fallback_rejects_oversized_physical_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized-line.po"
    source.write_bytes(b'msgid "' + (b"a" * 65) + b'"\n')
    monkeypatch.setattr(po_stream, "_MAX_PO_LINE_BYTES", 64)

    with pytest.raises(ValueError, match="physical line exceeds"):
        list(po_stream.iter_po_entries(source))


def test_po_fallback_rejects_oversized_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized-entry.po"
    source.write_text('msgid "a"\n"bbbbbbbb"\n"cccccccc"\nmsgstr ""\n', encoding="utf-8")
    monkeypatch.setattr(po_stream, "_MAX_PO_ENTRY_BYTES", 32)

    with pytest.raises(ValueError, match="raw-input limit"):
        list(po_stream.iter_po_entries(source))


def test_po_fallback_joins_many_continuations_once(tmp_path: Path) -> None:
    source = tmp_path / "continuations.po"
    fragments = tuple(str(number % 10) for number in range(10_000))
    source.write_text(
        'msgid ""\n' + "".join(f'"{fragment}"\n' for fragment in fragments) + 'msgstr "ok"\n',
        encoding="utf-8",
    )

    entries = list(po_stream.iter_po_entries(source))

    assert len(entries) == 1
    assert entries[0].msgid == "".join(fragments)


def test_xlsx_import_rejects_symbolic_link_member(tmp_path: Path) -> None:
    workbook = tmp_path / "unsafe.xlsx"
    link = zipfile.ZipInfo("xl/worksheets/sheet1.xml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(link, b"target")

    with pytest.raises(ValueError, match="symbolic-link XLSX ZIP entry"):
        import_xlsx(str(workbook), progress=False)


def test_html_multitarget_export_rejects_unsafe_locale(tmp_path: Path) -> None:
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=("../escape",),
        data={"html:p:0": Data(source="Hello")},
    )

    with pytest.raises(ValueError, match="Unsafe target locale"):
        export_html(document, tmp_path / "output")

    assert not (tmp_path / "escape.html").exists()
