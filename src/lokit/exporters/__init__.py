from typing import Any

from lokit.exporters.csv import export_csv, export_csv_async
from lokit.exporters.html import export_html, export_html_async
from lokit.exporters.idml import export_idml, export_idml_async
from lokit.exporters.json_i18n import export_json_i18n, export_json_i18n_async
from lokit.exporters.po import export_po, export_po_async
from lokit.exporters.tmx import export_tmx, export_tmx_from_json
from lokit.exporters.xliff import (
    export_xliff,
    export_xliff_async,
    export_xliff_from_json,
    export_xliff_from_json_async,
)
from lokit.exporters.xlsx import export_xlsx, export_xlsx_async


class write:
    @staticmethod
    def csv(*args: Any, **kwargs: Any) -> None:
        export_csv(*args, **kwargs)

    @staticmethod
    def html(*args: Any, **kwargs: Any) -> None:
        export_html(*args, **kwargs)

    @staticmethod
    def idml(*args: Any, **kwargs: Any) -> None:
        export_idml(*args, **kwargs)

    @staticmethod
    def json(*args: Any, **kwargs: Any) -> None:
        export_json_i18n(*args, **kwargs)

    @staticmethod
    def json_i18n(*args: Any, **kwargs: Any) -> None:
        export_json_i18n(*args, **kwargs)

    @staticmethod
    def po(*args: Any, **kwargs: Any) -> None:
        export_po(*args, **kwargs)

    @staticmethod
    def tmx(*args: Any, **kwargs: Any) -> None:
        export_tmx(*args, **kwargs)

    @staticmethod
    def xliff(*args: Any, **kwargs: Any) -> None:
        export_xliff(*args, **kwargs)

    @staticmethod
    def xlsx(*args: Any, **kwargs: Any) -> None:
        export_xlsx(*args, **kwargs)


class async_:
    @staticmethod
    async def csv(*args: Any, **kwargs: Any) -> None:
        await export_csv_async(*args, **kwargs)

    @staticmethod
    async def html(*args: Any, **kwargs: Any) -> None:
        await export_html_async(*args, **kwargs)

    @staticmethod
    async def idml(*args: Any, **kwargs: Any) -> None:
        await export_idml_async(*args, **kwargs)

    @staticmethod
    async def json(*args: Any, **kwargs: Any) -> None:
        await export_json_i18n_async(*args, **kwargs)

    @staticmethod
    async def json_i18n(*args: Any, **kwargs: Any) -> None:
        await export_json_i18n_async(*args, **kwargs)

    @staticmethod
    async def po(*args: Any, **kwargs: Any) -> None:
        await export_po_async(*args, **kwargs)

    @staticmethod
    async def xliff(*args: Any, **kwargs: Any) -> None:
        await export_xliff_async(*args, **kwargs)

    @staticmethod
    async def xlsx(*args: Any, **kwargs: Any) -> None:
        await export_xlsx_async(*args, **kwargs)


class from_json:
    @staticmethod
    def tmx(*args: Any, **kwargs: Any) -> None:
        export_tmx_from_json(*args, **kwargs)

    @staticmethod
    def xliff(*args: Any, **kwargs: Any) -> None:
        export_xliff_from_json(*args, **kwargs)

    @staticmethod
    async def xliff_async(*args: Any, **kwargs: Any) -> None:
        await export_xliff_from_json_async(*args, **kwargs)

__all__ = [
    "async_",
    "export_csv",
    "export_csv_async",
    "export_html",
    "export_html_async",
    "export_idml",
    "export_idml_async",
    "export_json_i18n",
    "export_json_i18n_async",
    "export_po",
    "export_po_async",
    "export_tmx",
    "export_tmx_from_json",
    "export_xliff",
    "export_xliff_async",
    "export_xliff_from_json",
    "export_xliff_from_json_async",
    "export_xlsx",
    "export_xlsx_async",
    "from_json",
    "write",
]
