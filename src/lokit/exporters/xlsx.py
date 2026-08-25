from __future__ import annotations

import contextlib
import html
import os
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from rustpy_xlsxwriter import FastExcel

from lokit.data.structure import BaseStructure, Data, StreamingStructure
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import raise_if_cancelled, run_cancellable_export
from lokit.tabular import TabularExportOptions, build_export_options, export_fieldnames, export_record, iter_items

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterator, Sequence

Structure = BaseStructure | StreamingStructure


class _Closable(Protocol):
    def close(self) -> None: ...


def export_xlsx(
    document: Structure,
    filepath: str | Path,
    *,
    header_style: str = "generic",
    write_header: bool = True,
    source_column_name: str = "",
    target_column_name: str = "",
    include_id: bool = True,
    include_status: bool = True,
    include_comment: bool = True,
    include_target: bool = True,
    column_order: tuple[str, ...] = (),
    resolve_placeholders: bool = True,
) -> None:
    _export_xlsx(
        document,
        filepath,
        header_style=header_style,
        write_header=write_header,
        source_column_name=source_column_name,
        target_column_name=target_column_name,
        include_id=include_id,
        include_status=include_status,
        include_comment=include_comment,
        include_target=include_target,
        column_order=column_order,
        resolve_placeholders=resolve_placeholders,
        cancellation=None,
    )


def _export_xlsx(
    document: Structure,
    filepath: str | Path,
    *,
    header_style: str,
    write_header: bool,
    source_column_name: str,
    target_column_name: str,
    include_id: bool,
    include_status: bool,
    include_comment: bool,
    include_target: bool,
    column_order: tuple[str, ...],
    resolve_placeholders: bool,
    cancellation: threading.Event | None,
) -> None:
    export_document = prepare_export_document(
        document,
        resolve_placeholders=resolve_placeholders,
    )
    path = Path(filepath)
    export_options = build_export_options(
        header_style=header_style,
        write_header=write_header,
        source_column_name=source_column_name,
        target_column_name=target_column_name,
        include_id=include_id,
        include_status=include_status,
        include_comment=include_comment,
        include_target=include_target,
        column_order=column_order,
    )
    fieldnames = export_fieldnames(export_document, export_options)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    records: Iterator[dict[str, str]] = iter(())
    try:
        first_item, records = _records_with_first(
            export_document,
            iter(iter_items(export_document)),
            fieldnames,
            export_options,
            cancellation,
        )
        if first_item is None:
            _write_header_only_xlsx(tmp_path, fieldnames if export_options.write_header else [])
        else:
            FastExcel(str(tmp_path), autofit=False).sheet("Sheet1", records).save()

        raise_if_cancelled(cancellation)
        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        raise_if_cancelled(cancellation)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    finally:
        _close_iterator(records)


async def export_xlsx_async(
    document: Structure,
    filepath: str | Path,
    *,
    header_style: str = "generic",
    write_header: bool = True,
    source_column_name: str = "",
    target_column_name: str = "",
    include_id: bool = True,
    include_status: bool = True,
    include_comment: bool = True,
    include_target: bool = True,
    column_order: tuple[str, ...] = (),
    resolve_placeholders: bool = True,
) -> None:
    await run_cancellable_export(
        lambda cancellation: _export_xlsx(
            document,
            filepath,
            header_style=header_style,
            write_header=write_header,
            source_column_name=source_column_name,
            target_column_name=target_column_name,
            include_id=include_id,
            include_status=include_status,
            include_comment=include_comment,
            include_target=include_target,
            column_order=column_order,
            resolve_placeholders=resolve_placeholders,
            cancellation=cancellation,
        )
    )


def _records_with_first(
    document: Structure,
    items: Iterator[tuple[str, Data]],
    fieldnames: Sequence[str],
    options: TabularExportOptions,
    cancellation: threading.Event | None,
) -> tuple[dict[str, str] | None, Iterator[dict[str, str]]]:
    iterator = iter(items)
    try:
        first = next(iterator, None)
    except BaseException:
        _close_iterator(iterator)
        raise
    if first is None:
        _close_iterator(iterator)
        return None, iter(())

    try:
        first_record = export_record(document, first[0], first[1], fieldnames, options)
    except BaseException:
        _close_iterator(iterator)
        raise

    def records() -> Iterator[dict[str, str]]:
        try:
            raise_if_cancelled(cancellation)
            yield first_record
            for unit_id, unit in iterator:
                raise_if_cancelled(cancellation)
                yield export_record(document, unit_id, unit, fieldnames, options)
        finally:
            _close_iterator(iterator)

    return first_record, records()


def _inline_string_cell(column: str, row: int, value: str) -> str:
    escaped = html.escape(value, quote=False)
    return f'<c r="{column}{row}" t="inlineStr"><is><t>{escaped}</t></is></c>'


def _write_header_only_xlsx(path: Path, headers: Sequence[str]) -> None:
    cells = "".join(_inline_string_cell(chr(ord("A") + index), 1, header) for index, header in enumerate(headers))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="1">{cells}</row></sheetData>'
        "</worksheet>"
    )

    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
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
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents)


def _close_iterator(items: object) -> None:
    if hasattr(items, "close"):
        cast("_Closable", items).close()
