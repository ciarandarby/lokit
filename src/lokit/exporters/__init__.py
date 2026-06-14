from pathlib import Path

from lokit.data.structure import BaseStructure, StreamingStructure
from lokit.exporters.csv import export_csv, export_csv_async
from lokit.exporters.docx import export_docx, export_docx_async
from lokit.exporters.html import export_html, export_html_async
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.exporters.json_i18n import export_json_i18n, export_json_i18n_async
from lokit.exporters.po import export_po, export_po_async
from lokit.exporters.pptx import export_pptx, export_pptx_async
from lokit.exporters.tmx import export_tmx, export_tmx_from_json
from lokit.exporters.xliff import (
    export_xliff,
    export_xliff_async,
    export_xliff_from_json,
    export_xliff_from_json_async,
    export_xliff_targets,
    export_xliff_targets_async,
)
from lokit.exporters.xlsx import export_xlsx, export_xlsx_async

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
        )

    @staticmethod
    def html(
        document: Structure,
        filepath: str | Path,
        source_html: str | Path | None = None,
    ) -> None:
        export_html(document, filepath, source_html)

    @staticmethod
    def idml(
        document: BaseStructure,
        filepath: str | Path,
        source_idml: str | Path,
    ) -> None:
        export_idml(document, filepath, source_idml)

    @staticmethod
    def docx(
        document: Structure,
        filepath: str | Path,
        source_docx: str | Path | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        export_docx(document, filepath, source_docx, target_locale=target_locale)

    @staticmethod
    def json(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
    ) -> None:
        export_json_i18n(document, filepath, nested)

    @staticmethod
    def json_i18n(
        document: Structure,
        filepath: str | Path,
        nested: bool = True,
    ) -> None:
        export_json_i18n(document, filepath, nested)

    @staticmethod
    def po(document: Structure, filepath: str | Path) -> None:
        export_po(document, filepath)

    @staticmethod
    def pptx(
        document: Structure,
        filepath: str | Path,
        source_pptx: str | Path | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        export_pptx(document, filepath, source_pptx, target_locale=target_locale)

    @staticmethod
    def tmx(document: Structure, filepath: str | Path) -> None:
        export_tmx(document, filepath)

    @staticmethod
    def xliff(
        document: Structure,
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
    ) -> None:
        export_xliff(document, filepath, group_by_resource=group_by_resource)

    @staticmethod
    def xliff_targets(
        documents: dict[str, BaseStructure],
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
    ) -> None:
        export_xliff_targets(documents, filepath, group_by_resource=group_by_resource)

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
        )


class async_:
    @staticmethod
    async def csv(
        document: BaseStructure,
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
        )

    @staticmethod
    async def html(
        document: Structure,
        filepath: str | Path,
        source_html: str | Path | None = None,
    ) -> None:
        await export_html_async(document, filepath, source_html)

    @staticmethod
    async def idml(
        document: BaseStructure,
        filepath: str | Path,
        source_idml: str | Path,
    ) -> None:
        await export_idml_async(document, filepath, source_idml)

    @staticmethod
    async def docx(
        document: Structure,
        filepath: str | Path,
        source_docx: str | Path | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        await export_docx_async(document, filepath, source_docx, target_locale=target_locale)

    @staticmethod
    async def json(
        document: BaseStructure,
        filepath: str | Path,
        nested: bool = True,
    ) -> None:
        await export_json_i18n_async(document, filepath, nested)

    @staticmethod
    async def json_i18n(
        document: BaseStructure,
        filepath: str | Path,
        nested: bool = True,
    ) -> None:
        await export_json_i18n_async(document, filepath, nested)

    @staticmethod
    async def po(document: BaseStructure, filepath: str | Path) -> None:
        await export_po_async(document, filepath)

    @staticmethod
    async def pptx(
        document: Structure,
        filepath: str | Path,
        source_pptx: str | Path | None = None,
        *,
        target_locale: str | None = None,
    ) -> None:
        await export_pptx_async(document, filepath, source_pptx, target_locale=target_locale)

    @staticmethod
    async def xliff(document: Structure, filepath: str | Path) -> None:
        await export_xliff_async(document, filepath)

    @staticmethod
    async def xliff_targets(
        documents: dict[str, BaseStructure],
        filepath: str | Path,
        *,
        group_by_resource: bool = False,
    ) -> None:
        await export_xliff_targets_async(
            documents,
            filepath,
            group_by_resource=group_by_resource,
        )

    @staticmethod
    async def xlsx(
        document: BaseStructure,
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
        )


class from_json:
    @staticmethod
    def tmx(source_json: str | Path, target_tmx: str | Path) -> None:
        export_tmx_from_json(source_json, target_tmx)

    @staticmethod
    def xliff(source_json: str | Path, target_xliff: str | Path) -> None:
        export_xliff_from_json(source_json, target_xliff)

    @staticmethod
    async def xliff_async(source_json: str | Path, target_xliff: str | Path) -> None:
        await export_xliff_from_json_async(source_json, target_xliff)

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
    "export_po",
    "export_po_async",
    "export_pptx",
    "export_pptx_async",
    "export_tmx",
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
    "write",
]
