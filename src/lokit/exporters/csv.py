from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.export_projection import prepare_export_document
from lokit.io.atomic import atomic_output_path, raise_if_cancelled, run_cancellable_export
from lokit.tabular import build_export_options, export_fieldnames, export_record, iter_items

if TYPE_CHECKING:
    import threading


class _Closable(Protocol):
    def close(self) -> None: ...


Structure = BaseStructure | StreamingStructure


def export_csv(
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
    _export_csv(
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


def _export_csv(
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

    items = iter(iter_items(export_document))
    try:
        with atomic_output_path(
            path,
            "w",
            cancellation=cancellation,
            encoding="utf-8",
            newline="",
        ) as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            if export_options.write_header:
                writer.writeheader()

            for unit_id, unit in items:
                raise_if_cancelled(cancellation)
                writer.writerow(export_record(export_document, unit_id, unit, fieldnames, export_options))
    finally:
        _close_iterator(items)


async def export_csv_async(
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
        lambda cancellation: _export_csv(
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


def _close_iterator(items: object) -> None:
    if hasattr(items, "close"):
        cast("_Closable", items).close()
