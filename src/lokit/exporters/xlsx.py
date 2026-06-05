from __future__ import annotations

import asyncio
import html
import os
import tempfile
import contextlib
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from rustpy_xlsxwriter import FastExcel

from lokit.data.structure import BaseStructure, Data, StreamingStructure, TranslationStatus

_HEADERS = ["id", "source", "target", "status", "comment"]


Structure = BaseStructure | StreamingStructure


def export_xlsx(document: Structure, filepath: str | Path) -> None:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        first_item, records = _records_with_first(_iter_items(document))
        if first_item is None:
            _write_header_only_xlsx(tmp_path)
        else:
            FastExcel(str(tmp_path), autofit=False).sheet("Sheet1", records).save()

        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


async def export_xlsx_async(document: BaseStructure, filepath: str | Path) -> None:
    await asyncio.to_thread(export_xlsx, document, filepath)


def _iter_items(document: Structure) -> Iterable[tuple[str, Data]]:
    if isinstance(document, BaseStructure):
        return document.data.items()
    return document.items


def _records_with_first(
    items: Iterable[tuple[str, Data]],
) -> tuple[dict[str, str] | None, Iterator[dict[str, str]]]:
    iterator = iter(items)
    first = next(iterator, None)
    if first is None:
        return None, iter(())

    first_record = _row_record(*first)

    def records() -> Iterator[dict[str, str]]:
        yield first_record
        for unit_id, unit in iterator:
            yield _row_record(unit_id, unit)

    return first_record, records()


def _row_record(unit_id: str, unit: Data) -> dict[str, str]:
    comment = "; ".join(c.context for c in unit.comments if c.context)
    status = unit.status.value if unit.status != TranslationStatus.UNKNOWN else ""
    return {
        "id": unit_id,
        "source": unit.source,
        "target": unit.target or "",
        "status": status,
        "comment": comment,
    }


def _inline_string_cell(column: str, row: int, value: str) -> str:
    escaped = html.escape(value, quote=False)
    return f'<c r="{column}{row}" t="inlineStr"><is><t>{escaped}</t></is></c>'


def _write_header_only_xlsx(path: Path) -> None:
    cells = "".join(
        _inline_string_cell(chr(ord("A") + index), 1, header)
        for index, header in enumerate(_HEADERS)
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData><row r=\"1\">{cells}</row></sheetData>"
        "</worksheet>"
    )

    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents)
