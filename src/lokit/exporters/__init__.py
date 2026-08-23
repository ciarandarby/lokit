from pathlib import Path

from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.exporters.csv import export_csv, export_csv_async
from lokit.exporters.docx import export_docx, export_docx_async
from lokit.exporters.html import export_html, export_html_async
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.exporters.json_i18n import export_json_i18n, export_json_i18n_async
from lokit.exporters.lokit import export_lokit, export_lokit_async
from lokit.exporters.po import export_po, export_po_async
from lokit.exporters.pptx import export_pptx, export_pptx_async
from lokit.exporters.regen import (
    regen_csv,
    regen_csv_async,
    regen_docx,
    regen_docx_async,
    regen_html,
    regen_html_async,
    regen_idml,
    regen_idml_async,
    regen_json_i18n,
    regen_json_i18n_async,
    regen_po,
    regen_po_async,
    regen_pptx,
    regen_pptx_async,
    regen_tmx,
    regen_tmx_async,
    regen_xliff,
    regen_xliff_async,
    regen_xlsx,
    regen_xlsx_async,
)
from lokit.exporters.tmx import export_tmx, export_tmx_async, export_tmx_from_json
from lokit.exporters.xliff import (
    export_xliff,
    export_xliff_async,
    export_xliff_from_json,
    export_xliff_from_json_async,
    export_xliff_targets,
    export_xliff_targets_async,
)
from lokit.exporters.xlsx import export_xlsx, export_xlsx_async
from lokit.office.models import DocumentSource, OfficeExportResult
from lokit.office.options import OfficeExportOptions

Structure = BaseStructure | StreamingStructure


class write:
    @staticmethod
    def csv(
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
        export_csv(
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
        )

    @staticmethod
    def html(
        document: Structure,
        filepath: str | Path,
        source_html: str | Path | None = None,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_html(
            document,
            filepath,
            source_html,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def idml(
        document: BaseStructure,
        filepath: str | Path,
        source_idml: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_idml(
            document,
            filepath,
            source_idml,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def docx(
        document: Structure,
        filepath: str | Path,
        source_docx: DocumentSource | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        return export_docx(
            document,
            filepath,
            source_docx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def json(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_json_i18n(
            document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def json_i18n(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_json_i18n(
            document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def lokit(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_lokit(document, filepath, resolve_placeholders=resolve_placeholders)

    @staticmethod
    def po(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_po(document, filepath, resolve_placeholders=resolve_placeholders)

    @staticmethod
    def pptx(
        document: Structure,
        filepath: str | Path,
        source_pptx: DocumentSource | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        return export_pptx(
            document,
            filepath,
            source_pptx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def tmx(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_tmx(document, filepath, resolve_placeholders=resolve_placeholders)

    @staticmethod
    def xliff(
        document: Structure,
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
        resolve_placeholders: bool = True,
    ) -> None:
        export_xliff(
            document,
            filepath,
            group_by_resource=group_by_resource,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def xliff_targets(
        documents: dict[str, BaseStructure],
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
        resolve_placeholders: bool = True,
    ) -> None:
        export_xliff_targets(
            documents,
            filepath,
            group_by_resource=group_by_resource,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def xlsx(
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
        export_xlsx(
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
        )


class async_:
    @staticmethod
    async def csv(
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
        await export_csv_async(
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
        )

    @staticmethod
    async def html(
        document: Structure,
        filepath: str | Path,
        source_html: str | Path | None = None,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_html_async(
            document,
            filepath,
            source_html,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def idml(
        document: BaseStructure,
        filepath: str | Path,
        source_idml: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_idml_async(
            document,
            filepath,
            source_idml,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def docx(
        document: Structure,
        filepath: str | Path,
        source_docx: DocumentSource | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        return await export_docx_async(
            document,
            filepath,
            source_docx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def json(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_json_i18n_async(
            document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def json_i18n(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_json_i18n_async(
            document,
            filepath,
            nested,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def lokit(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_lokit_async(
            document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def po(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_po_async(
            document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def pptx(
        document: Structure,
        filepath: str | Path,
        source_pptx: DocumentSource | None = None,
        *,
        target_locale: str | None = None,
        options: OfficeExportOptions | None = None,
        resolve_placeholders: bool = True,
    ) -> OfficeExportResult:
        return await export_pptx_async(
            document,
            filepath,
            source_pptx,
            target_locale=target_locale,
            options=options,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def tmx(
        document: Structure,
        filepath: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_tmx_async(
            document,
            filepath,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def xliff(
        document: Structure,
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_xliff_async(
            document,
            filepath,
            group_by_resource=group_by_resource,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def xliff_targets(
        documents: dict[str, BaseStructure],
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_xliff_targets_async(
            documents,
            filepath,
            group_by_resource=group_by_resource,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def xlsx(
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
        await export_xlsx_async(
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
        )


class from_json:
    @staticmethod
    def tmx(
        source_json: str | Path,
        target_tmx: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_tmx_from_json(
            source_json,
            target_tmx,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    def xliff(
        source_json: str | Path,
        target_xliff: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        export_xliff_from_json(
            source_json,
            target_xliff,
            resolve_placeholders=resolve_placeholders,
        )

    @staticmethod
    async def xliff_async(
        source_json: str | Path,
        target_xliff: str | Path,
        *,
        resolve_placeholders: bool = True,
    ) -> None:
        await export_xliff_from_json_async(
            source_json,
            target_xliff,
            resolve_placeholders=resolve_placeholders,
        )


__all__ = [
    "async_",
    "export_csv",
    "export_csv_async",
    "export_docx",
    "export_docx_async",
    "export_html",
    "export_html_async",
    "export_idml",
    "export_idml_async",
    "export_json_i18n",
    "export_json_i18n_async",
    "export_lokit",
    "export_lokit_async",
    "export_po",
    "export_po_async",
    "export_pptx",
    "export_pptx_async",
    "export_tmx",
    "export_tmx_async",
    "export_tmx_from_json",
    "export_xliff",
    "export_xliff_async",
    "export_xliff_from_json",
    "export_xliff_from_json_async",
    "export_xliff_targets",
    "export_xliff_targets_async",
    "export_xlsx",
    "export_xlsx_async",
    "from_json",
    "regen_csv",
    "regen_csv_async",
    "regen_docx",
    "regen_docx_async",
    "regen_html",
    "regen_html_async",
    "regen_idml",
    "regen_idml_async",
    "regen_json_i18n",
    "regen_json_i18n_async",
    "regen_po",
    "regen_po_async",
    "regen_pptx",
    "regen_pptx_async",
    "regen_tmx",
    "regen_tmx_async",
    "regen_xliff",
    "regen_xliff_async",
    "regen_xlsx",
    "regen_xlsx_async",
    "write",
]
